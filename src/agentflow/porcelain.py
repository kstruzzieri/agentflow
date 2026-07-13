"""Porcelain commands that report and sequence the Agentflow loop.

Read-only inspection (`next_action`) plus thin sequencers (`finish_step`,
`finish_run`) over existing plumbing. No new durable state.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import read_json, read_jsonl
from .execution import (
    attempt_is_verified,
    complete_step,
    current_step_attempt,
    mark_step_verified,
    next_step,
    require_lifecycle_owner,
    resolve_attempt,
)
from .execution_coverage import verify_run, verify_step
from .git import changed_file_records
from .proof import verify_proof
from .receipts import file_receipts
from .validation import audit_drift, path_in_effective_scope


@dataclass
class Action:
    state: str
    reason: str
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    step_id: Optional[str] = None
    gate: Optional[str] = None
    diagnostics: List[str] = field(default_factory=list)
    blocking: bool = True

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "state": self.state,
            "reason": self.reason,
            "blocking": self.blocking,
            "diagnostics": list(self.diagnostics),
        }
        if self.command is not None:
            data["command"] = self.command
            data["args"] = list(self.args)
        if self.step_id:
            data["step_id"] = self.step_id
        if self.gate:
            data["gate"] = self.gate
        return data


_IGNORED_PREFIXES = (".agent/", ".git/")
_STUCK_ADVISORY_RULES = {
    "repeated_command_failure",
    "repeated_verify_failure",
    "alternating_no_op",
}


def _cmd(args: List[str]) -> str:
    """Render an argv list as a copy-paste 'agentflow ...' command."""
    rendered = []
    for token in args:
        rendered.append(token if token == "$USER" else shlex.quote(token))
    return "agentflow " + " ".join(rendered)


def _load_plan(root: Path) -> Optional[Dict[str, Any]]:
    plan_path = root / ".agent/plan.lock.json"
    if not plan_path.exists():
        return None
    try:
        return read_json(plan_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _inflight(root: Path, plan: Dict[str, Any]) -> Optional[tuple[str, str, Dict[str, Any]]]:
    """Return (step_id, attempt_id, step) for the open claimed attempt, else None."""
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str):
            continue
        attempt = current_step_attempt(root, step_id)
        if attempt:
            return step_id, attempt, step
    return None


def _scoped_unrecorded_paths(root: Path, plan: Dict[str, Any], step_id: str,
                             attempt_id: str) -> List[str]:
    """Scoped changed files with no file receipt for this open attempt."""
    recorded = {
        r.get("path")
        for r in file_receipts(root)
        if r.get("step_id") == step_id and r.get("attempt_id") == attempt_id
    }
    missing: List[str] = []
    for record in changed_file_records(root):
        path = record.get("path")
        if not isinstance(path, str):
            continue
        if path.startswith(_IGNORED_PREFIXES):
            continue
        if not path_in_effective_scope(plan, step_id, path):
            continue
        if path not in recorded:
            missing.append(path)
    return sorted(set(missing))


def _command_gate_commands(step: Dict[str, Any]) -> List[tuple[str, List[str], List[str]]]:
    """Return command gate labels, commands, and aliases in plan order."""
    validation = [item for item in step.get("validation", []) if isinstance(item, str)]
    gates = step.get("gates")
    commands: List[tuple[str, List[str], List[str]]] = []
    if isinstance(gates, list):
        for index, gate in enumerate(gates):
            if isinstance(gate, dict) and gate.get("kind") == "command":
                run = gate.get("run")
                if isinstance(run, list) and all(isinstance(item, str) for item in run):
                    label = " ".join(run)
                    aliases = [label]
                    if index < len(validation):
                        aliases.append(validation[index])
                    commands.append((label, run, aliases))
    if commands:
        return commands
    if validation:
        return [(validation[0], ["<cmd>"], [validation[0]])]
    return [("<gate>", ["<cmd>"], ["<gate>"])]


def _finding_mentions_gate(finding: Dict[str, str], aliases: List[str]) -> bool:
    message = finding.get("message", "")
    return any(
        message == f"missing command receipt for gate: {alias}"
        or message.startswith(f"gate {alias} ")
        for alias in aliases
    )


def _first_gate_command(
    step: Dict[str, Any],
    findings: Optional[List[Dict[str, str]]] = None,
) -> tuple[str, List[str]]:
    """Return the unmet gate label and command for next-action guidance."""
    commands = _command_gate_commands(step)
    for label, command, aliases in commands:
        if any(_finding_mentions_gate(finding, aliases) for finding in findings or []):
            return label, command
    label, command, _aliases = commands[0]
    return label, command


def _latest_run_verification(root: Path) -> Optional[Dict[str, Any]]:
    latest: Optional[Dict[str, Any]] = None
    for row in read_jsonl(root / ".agent/verification-runs.jsonl"):
        if isinstance(row, dict) and row.get("scope") == "run":
            latest = row
    return latest


def next_action(root: Path, strict: bool = False) -> Action:
    plan = _load_plan(root)
    if plan is None:
        return Action("uninitialized", "no .agent/plan.lock.json",
                      command=_cmd(["init"]), args=["init"])
    if not plan.get("locked"):
        args = ["lock-plan", ".agent/plan.lock.json"]
        return Action("plan_unlocked", "plan exists but is not locked",
                      command=_cmd(args), args=args)
    if not (root / ".agent/execution.contract.json").exists():
        return Action("execution_uninitialized", "execution contract missing",
                      command=_cmd(["init-execution"]), args=["init-execution"])

    inflight = _inflight(root, plan)
    if inflight is None:
        eligible = next_step(root, plan)
        if eligible is not None:
            step_id = eligible["id"]
            args = ["claim-step", step_id, "--agent", "$USER"]
            return Action("step_unclaimed", f"step {step_id} is eligible and unclaimed",
                          command=_cmd(args), args=args, step_id=step_id)
        # All steps complete: run/drift/proof states.
        drift = audit_drift(root, plan)
        if drift.get("status") == "fail":
            return Action("drift_failing", "changes fall outside plan scope",
                          command=_cmd(["finish-run"]), args=["finish-run"],
                          diagnostics=[f"out of scope: {p}" for p in
                                       drift.get("out_of_scope_files", [])])
        run_result = verify_run(root, plan, strict=strict, record=False)
        run_record = _latest_run_verification(root)
        run_warnings = [
            finding for finding in run_result.get("warnings", [])
            if finding.get("rule") not in _STUCK_ADVISORY_RULES
        ]
        run_findings = [*run_result.get("errors", []), *run_warnings]
        run_failed = run_result.get("status") == "failed" or (
            strict and bool(run_warnings)
        )
        if run_record is None or run_failed:
            diagnostics = [f["message"] for f in run_findings]
            if run_record is None:
                diagnostics.insert(0, "run verification has not been recorded")
            return Action("run_unverified", "run-level verification not satisfied",
                          command=_cmd(["finish-run"]), args=["finish-run"],
                          diagnostics=diagnostics)
        proof_path = root / ".agent/proof-pack.json"
        if not proof_path.exists():
            return Action("proof_missing", "proof pack not built",
                          command=_cmd(["finish-run"]), args=["finish-run"])
        findings = verify_proof(root, proof_path, strict=strict)
        errors = [f for f in findings if f.get("severity") == "error"]
        if errors:
            # Stale = inputs changed since the proof was built; verify_proof
            # signals that as a "hash mismatch", "path mismatch", or other
            # checksum/receipt mismatch (all reported as "... mismatch ...").
            # Everything else (malformed metadata, failing checks) is failing.
            stale = any("hash" in f["message"] or "mismatch" in f["message"]
                        for f in errors)
            state = "proof_stale" if stale else "proof_failing"
            return Action(state, "proof verification failed",
                          command=_cmd(["finish-run"]), args=["finish-run"],
                          diagnostics=[f["message"] for f in errors])
        return Action("complete", "run complete and proof verified",
                      command=None, args=[], blocking=False)

    step_id, attempt_id, step = inflight
    unrecorded = _scoped_unrecorded_paths(root, plan, step_id, attempt_id)
    if unrecorded:
        args = ["record-file-change", "--step", step_id, "--path", unrecorded[0]]
        return Action(
            "file_receipts_missing",
            f"step {step_id} has changed scoped files with no receipt",
            command=_cmd(args), args=args, step_id=step_id,
            diagnostics=[f"unrecorded: {p}" for p in unrecorded],
        )
    verify_result = verify_step(root, plan, step_id, attempt_id,
                                strict=strict, record=False)
    step_failed = verify_result.get("status") == "failed" or (
        strict and verify_result.get("status") == "warning"
    )
    if step_failed:
        findings = [*verify_result["errors"], *verify_result["warnings"]]
        gate, command = _first_gate_command(step, findings)
        args = ["run", "--step", step_id, "--gate", gate, "--", *command]
        return Action(
            "validation_missing",
            f"step {step_id} has an unmet validation gate",
            command=_cmd(args), args=args, step_id=step_id, gate=gate,
            diagnostics=[f["message"] for f in findings],
        )
    if not attempt_is_verified(root, step_id, attempt_id):
        args = ["finish-step", step_id]
        return Action("step_unverified",
                      f"step {step_id} validated but not verified",
                      command=_cmd(args), args=args, step_id=step_id)
    args = ["finish-step", step_id]
    return Action("step_uncompleted",
                  f"step {step_id} verified but not completed",
                  command=_cmd(args), args=args, step_id=step_id)


def finish_step(root: Path, plan: Dict[str, Any], step_id: str,
                attempt_id: Optional[str], strict: bool = False,
                replay: bool = False,
                agent_id: Optional[str] = None) -> Dict[str, Any]:
    resolved = require_lifecycle_owner(root, step_id, attempt_id, agent_id, action="verify")
    result = verify_step(root, plan, step_id, resolved, strict=strict, replay=replay)
    findings = [*result["errors"], *result["warnings"]]
    out: Dict[str, Any] = {
        "step_id": step_id,
        "attempt_id": resolved,
        "verification_status": result["status"],
        "verified": False,
        "completed": False,
        "diagnostics": [f["message"] for f in findings],
    }
    if result["status"] == "failed":
        return out
    mark_step_verified(root, step_id, resolved, findings, agent_id=agent_id)
    out["verified"] = True
    complete_step(root, step_id, resolved, agent_id=agent_id)
    out["completed"] = True
    return out


def finish_run(root: Path, plan_path: Path, strict: bool = False) -> Dict[str, Any]:
    # Import inside the function: cli.py imports porcelain, so a top-level
    # `from . import cli` here would create a circular import.
    import contextlib
    import io

    from . import cli as _cli

    root_args = ["--root", str(root)]
    plan_args = ["--plan", str(plan_path)]
    strict_args = ["--strict"] if strict else []
    gates = [
        ("audit-drift", ["audit-drift", *root_args, *plan_args]),
        ("verify-run", ["verify-run", *root_args, *plan_args, *strict_args]),
        ("build-proof", ["build-proof", *root_args, *plan_args, *strict_args]),
        ("verify-proof", ["verify-proof", *root_args, *strict_args]),
    ]
    recorded: List[Dict[str, str]] = []
    for name, argv in gates:
        # Capture each gate's output so finish_run never leaks gate chatter to
        # stdout (keeping --json parseable) while still surfacing the failing
        # gate's real output in diagnostics instead of swallowing it.
        out_buffer, err_buffer = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buffer), contextlib.redirect_stderr(err_buffer):
            code = _cli.main(argv)
        status = "ok" if code == 0 else "fail"
        recorded.append({"name": name, "status": status})
        if code != 0:
            output = out_buffer.getvalue() + err_buffer.getvalue()
            diagnostics = [line for line in output.splitlines() if line.strip()]
            return {
                "ok": False,
                "stopped_at": name,
                "gates": recorded,
                "diagnostics": diagnostics or [f"gate {name} failed"],
            }
    return {"ok": True, "stopped_at": None, "gates": recorded, "diagnostics": []}
