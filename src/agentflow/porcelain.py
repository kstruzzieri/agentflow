"""Porcelain commands that report and sequence the Agentflow loop.

Read-only inspection (`next_action`) plus thin sequencers (`finish_step`,
`finish_run`) over existing plumbing. No new durable state.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import plan_binding_sha256, read_json, read_jsonl
from .contracts import CHANGE_KINDS, EXECUTION_ARTIFACT_PATHS, PROVENANCE_VALUES
from .execution import (
    ATTEMPT_OPENING_EVENTS,
    LEASE_EVENTS,
    OPEN_EVENTS,
    TERMINAL_EVENTS,
    attempt_deadline,
    attempt_is_expired,
    attempt_is_verified,
    complete_step,
    lease_grace_seconds,
    lease_policy,
    lease_ttl_minutes,
    load_execution_contract,
    mark_step_verified,
    next_step,
    read_step_state,
    require_lifecycle_owner,
    validate_execution_contract,
)
from .execution_coverage import verify_run, verify_step
from .git import changed_file_records
from .proof import verify_proof
from .receipts import command_receipts, file_receipts, sha256_path
from .validation import audit_drift, path_in_effective_scope, validate_plan


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
    resumability: Optional[Dict[str, Any]] = None

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
        if self.resumability is not None:
            data["resumability"] = self.resumability
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


def _load_plan(root: Path) -> Optional[Any]:
    plan_path = root / ".agent/plan.lock.json"
    if not plan_path.exists():
        return None
    return read_json(plan_path)


_RECOVERY_ACTIONS = ("claim", "continue", "renew", "reclaim", "fail")


def _recovery_action(
    action: str,
    allowed: bool,
    reason: str,
    *,
    automatic: bool = True,
    break_glass: bool = False,
) -> Dict[str, Any]:
    return {
        "action": action,
        "allowed": allowed,
        "automatic": automatic,
        "break_glass": break_glass,
        "reason": reason,
    }


def _denied_recovery_actions(reason: str) -> List[Dict[str, Any]]:
    return [
        _recovery_action(
            action,
            False,
            reason,
            automatic=action != "fail",
            break_glass=action == "fail",
        )
        for action in _RECOVERY_ACTIONS
    ]


def _receipt_string(
    row: Dict[str, Any],
    key: str,
    relative: str,
    line_number: int,
    *,
    non_empty: bool = True,
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (non_empty and not value):
        requirement = "non-empty string" if non_empty else "string"
        raise ValueError(
            f"{relative}:{line_number}: receipt {key} must be a {requirement}"
        )
    return value


def _validate_command_receipt(
    row: Dict[str, Any],
    relative: str,
    line_number: int,
) -> None:
    for key in ("id", "step_id", "attempt_id", "provenance"):
        _receipt_string(row, key, relative, line_number)
    for key in ("cwd", "started_at", "finished_at"):
        _receipt_string(row, key, relative, line_number, non_empty=False)
    if row["provenance"] not in PROVENANCE_VALUES:
        raise ValueError(
            f"{relative}:{line_number}: receipt provenance is invalid"
        )
    command = row.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        raise ValueError(
            f"{relative}:{line_number}: receipt command must be a non-empty string array"
        )
    exit_code = row.get("exit_code")
    if "exit_code" not in row or (
        exit_code is not None
        and (not isinstance(exit_code, int) or isinstance(exit_code, bool))
    ):
        raise ValueError(
            f"{relative}:{line_number}: receipt exit_code must be an integer or null"
        )
    if not isinstance(row.get("truncated"), bool):
        raise ValueError(
            f"{relative}:{line_number}: receipt truncated must be boolean"
        )
    gate = row.get("gate")
    if gate is not None and not isinstance(gate, str):
        raise ValueError(
            f"{relative}:{line_number}: receipt gate must be a string or null"
        )


def _validate_file_receipt(
    row: Dict[str, Any],
    relative: str,
    line_number: int,
) -> None:
    for key in ("id", "step_id", "attempt_id", "path"):
        _receipt_string(row, key, relative, line_number)
    _receipt_string(row, "recorded_at", relative, line_number, non_empty=False)
    if row.get("change_kind") not in CHANGE_KINDS:
        raise ValueError(
            f"{relative}:{line_number}: receipt change_kind is invalid"
        )
    for key in ("before_git_blob", "after_sha256"):
        value = row.get(key)
        if key not in row or (value is not None and not isinstance(value, str)):
            raise ValueError(
                f"{relative}:{line_number}: receipt {key} must be a string or null"
            )


def _projection_ledgers(root: Path) -> Dict[str, List[Dict[str, Any]]]:
    ledgers: Dict[str, List[Dict[str, Any]]] = {}
    for name, relative in EXECUTION_ARTIFACT_PATHS.items():
        if name == "execution-contract":
            continue
        path = root / relative
        if not path.is_file():
            raise ValueError(f"{relative} is missing")
        rows = read_jsonl(path)
        for line_number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError(
                    f"{relative}:{line_number}: ledger entry must be a JSON object"
                )
            if name == "command-receipts":
                _validate_command_receipt(row, relative, line_number)
            elif name == "file-receipts":
                _validate_file_receipt(row, relative, line_number)
        ledgers[name] = rows
    return ledgers


def _validate_step_event_identity(
    events: List[Dict[str, Any]],
    policy: str,
) -> None:
    valid_events = OPEN_EVENTS | TERMINAL_EVENTS | LEASE_EVENTS
    attempt_steps: Dict[str, str] = {}
    attempt_owners: Dict[str, Optional[str]] = {}
    opened_attempts = set()
    terminal_attempts = set()
    for line_number, event in enumerate(events, start=1):
        event_kind = event.get("event")
        step_id = event.get("step_id")
        attempt_id = event.get("attempt_id")
        if event_kind not in valid_events:
            raise ValueError(
                f".agent/step-runs.jsonl:{line_number}: invalid event {event_kind!r}"
            )
        if not isinstance(step_id, str) or not step_id:
            raise ValueError(
                f".agent/step-runs.jsonl:{line_number}: step_id must be a non-empty string"
            )
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError(
                f".agent/step-runs.jsonl:{line_number}: attempt_id must be a non-empty string"
            )
        owner = event.get("agent_id")
        if owner is not None and not isinstance(owner, str):
            raise ValueError(
                f".agent/step-runs.jsonl:{line_number}: agent_id must be a string"
            )
        deadline = event.get("lease_expires_at")
        if deadline is not None and not isinstance(deadline, str):
            raise ValueError(
                f".agent/step-runs.jsonl:{line_number}: lease_expires_at must be a string or null"
            )
        prior_step = attempt_steps.setdefault(attempt_id, step_id)
        if prior_step != step_id:
            raise ValueError(
                f"attempt {attempt_id} belongs to multiple steps: {prior_step}, {step_id}"
            )
        if attempt_id in terminal_attempts:
            raise ValueError(f"attempt {attempt_id} has events after a terminal event")
        if event_kind in ATTEMPT_OPENING_EVENTS:
            if attempt_id in opened_attempts:
                raise ValueError(f"attempt {attempt_id} has multiple opening events")
            opened_attempts.add(attempt_id)
            attempt_owners[attempt_id] = owner
        elif attempt_id not in opened_attempts and event_kind != "failed":
            raise ValueError(
                f"attempt {attempt_id} has no opening event before {event_kind}"
            )
        if event_kind in LEASE_EVENTS and policy == "enforce":
            expected_owner = attempt_owners[attempt_id]
            if not owner or owner != expected_owner:
                raise ValueError(
                    f"attempt {attempt_id} renewed by {owner} instead of {expected_owner}"
                )
        if event_kind in TERMINAL_EVENTS:
            terminal_attempts.add(attempt_id)


def _base_resumability(
    plan: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> Dict[str, Any]:
    contract = {
        "plan_schema_version": None,
        "plan_sha256": None,
        "locked": False,
        "locked_at": None,
        "execution_schema_version": None,
        "execution_contract_sha256": None,
    }
    if isinstance(plan, dict):
        schema_version = plan.get("schema_version")
        locked_at = plan.get("locked_at")
        contract.update({
            "plan_schema_version": (
                schema_version if isinstance(schema_version, str) else None
            ),
            "plan_sha256": plan_binding_sha256(plan),
            "locked": plan.get("locked") is True,
            "locked_at": locked_at if isinstance(locked_at, str) else None,
        })
    return {
        "contract": contract,
        "agent_id": agent_id or None,
        "step": None,
        "attempt": None,
        "lease": {
            "policy": None,
            "ttl_minutes": None,
            "grace_seconds": None,
            "expires_at": None,
            "state": "unknown",
            "exclusive": False,
        },
        "receipts": {"commands": [], "files": []},
        "gates": [],
        "recovery_actions": _denied_recovery_actions("state is not resumable"),
        "diagnostics": [],
    }


def _diagnose(
    projection: Dict[str, Any],
    code: str,
    message: str,
    artifact: str,
) -> Dict[str, Any]:
    projection["diagnostics"].append({
        "code": code,
        "message": message,
        "artifact": artifact,
    })
    projection["recovery_actions"] = _denied_recovery_actions(message)
    return projection


def _invalid_action(projection: Dict[str, Any]) -> Action:
    messages = [item["message"] for item in projection["diagnostics"]]
    return Action(
        "state_invalid",
        "Agentflow state is invalid or ambiguous",
        diagnostics=messages,
        resumability=projection,
    )


def _attach(action: Action, projection: Dict[str, Any]) -> Action:
    action.resumability = projection
    return action


def _project_receipts(root: Path, step_id: str, attempt_id: str) -> Dict[str, Any]:
    commands = [
        {
            "id": row.get("id"),
            "gate": row.get("gate"),
            "command": row.get("command"),
            "exit_code": row.get("exit_code"),
            "decision": row.get("decision"),
            "timed_out": row.get("timed_out") is True,
            "provenance": row.get("provenance"),
            "finished_at": row.get("finished_at"),
        }
        for row in command_receipts(root)
        if row.get("step_id") == step_id
        and row.get("attempt_id") == attempt_id
        and isinstance(row.get("id"), str)
    ]
    files = [
        {
            "id": row.get("id"),
            "path": row.get("path"),
            "change_kind": row.get("change_kind"),
            "recorded_at": row.get("recorded_at"),
        }
        for row in file_receipts(root)
        if row.get("step_id") == step_id
        and row.get("attempt_id") == attempt_id
        and isinstance(row.get("id"), str)
    ]
    return {"commands": commands, "files": files}


def _project_recovery_actions(
    step_id: Optional[str],
    attempt: Optional[Dict[str, Any]],
    agent_id: Optional[str],
    policy: str,
    lease_state: str,
) -> List[Dict[str, Any]]:
    if step_id is None:
        return _denied_recovery_actions("no actionable step")
    if attempt is None:
        claim_allowed = bool(agent_id)
        claim_reason = (
            f"step {step_id} is eligible for {agent_id}"
            if claim_allowed
            else "claim requires an agent identity"
        )
        return [
            _recovery_action("claim", claim_allowed, claim_reason),
            _recovery_action("continue", False, "no open attempt"),
            _recovery_action("renew", False, "no open attempt"),
            _recovery_action("reclaim", False, "no open attempt"),
            _recovery_action(
                "fail", False, "no open attempt",
                automatic=False, break_glass=True,
            ),
        ]

    owner = attempt.get("agent_id")
    enforce = policy == "enforce"
    if not enforce:
        continue_allowed = True
        continue_reason = "advisory policy does not enforce attempt ownership"
        renew_allowed = True
        renew_reason = "advisory renewal is metadata and does not establish exclusivity"
    elif not agent_id:
        continue_allowed = renew_allowed = False
        continue_reason = renew_reason = "owner-only action requires an agent identity"
    elif owner is None:
        continue_allowed = renew_allowed = False
        continue_reason = renew_reason = "attempt has no recorded owner"
    elif agent_id != owner:
        continue_allowed = renew_allowed = False
        continue_reason = renew_reason = f"attempt is owned by {owner}"
    else:
        renew_allowed = True
        renew_reason = "owner may renew a finite or no-deadline lease"
        continue_allowed = lease_state != "expired"
        continue_reason = (
            "owner holds the active lease"
            if continue_allowed
            else "expired enforced lease must be renewed or reclaimed"
        )

    reclaim_allowed = bool(agent_id) and lease_state == "expired"
    reclaim_reason = (
        "finite lease is expired and an agent identity was supplied"
        if reclaim_allowed
        else (
            "reclaim requires an agent identity"
            if not agent_id and lease_state == "expired"
            else "only an expired finite lease is reclaimable"
        )
    )
    return [
        _recovery_action("claim", False, "step already has an open attempt"),
        _recovery_action("continue", continue_allowed, continue_reason),
        _recovery_action("renew", renew_allowed, renew_reason),
        _recovery_action("reclaim", reclaim_allowed, reclaim_reason),
        _recovery_action(
            "fail",
            True,
            "fail-step is an explicit operator break-glass action",
            automatic=False,
            break_glass=True,
        ),
    ]


def resumability_projection(
    root: Path,
    plan: Any,
    agent_id: Optional[str] = None,
    strict: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    projection = _base_resumability(plan, agent_id)
    if not isinstance(plan, dict):
        return _diagnose(
            projection,
            "plan_invalid",
            ".agent/plan.lock.json top-level value must be a JSON object",
            ".agent/plan.lock.json",
        )
    try:
        plan_errors = validate_plan(plan)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        plan_errors = [f"plan structure is invalid: {exc}"]
    locked_at = plan.get("locked_at")
    if locked_at is not None and not isinstance(locked_at, str):
        plan_errors.append("locked_at must be a string or null")
    if plan_errors:
        return _diagnose(
            projection,
            "plan_invalid",
            "; ".join(plan_errors),
            ".agent/plan.lock.json",
        )
    contract_path = root / ".agent/execution.contract.json"
    if not contract_path.exists():
        return _diagnose(
            projection,
            "execution_contract_missing",
            ".agent/execution.contract.json is missing",
            ".agent/execution.contract.json",
        )
    try:
        contract = load_execution_contract(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _diagnose(
            projection,
            "execution_contract_invalid",
            str(exc),
            ".agent/execution.contract.json",
        )
    if not isinstance(contract, dict):
        return _diagnose(
            projection,
            "execution_contract_invalid",
            "execution contract must be a JSON object",
            ".agent/execution.contract.json",
        )
    try:
        contract_errors = validate_execution_contract(contract)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _diagnose(
            projection,
            "execution_contract_invalid",
            str(exc),
            ".agent/execution.contract.json",
        )
    if contract_errors:
        return _diagnose(
            projection,
            "execution_contract_invalid",
            "; ".join(item["message"] for item in contract_errors),
            ".agent/execution.contract.json",
        )

    try:
        # Inside the fail-closed handler: these re-read .agent files, and a
        # concurrent unlink between the contract load above and here must
        # yield a diagnostic, not a traceback.
        policy = lease_policy(root)
        ttl = lease_ttl_minutes(root)
        grace = lease_grace_seconds(root)
        projection["contract"].update({
            "execution_schema_version": contract.get("schema_version"),
            "execution_contract_sha256": sha256_path(contract_path),
        })
        projection["lease"].update({
            "policy": policy,
            "ttl_minutes": ttl,
            "grace_seconds": grace,
            "exclusive": policy == "enforce",
        })
        ledgers = _projection_ledgers(root)
        _validate_step_event_identity(ledgers["step-runs"], policy)
        state = read_step_state(root)
        plan_steps = {
            step["id"]: step
            for step in plan.get("steps", [])
            if isinstance(step, dict) and isinstance(step.get("id"), str)
        }
        unknown_steps = sorted(set(state["steps"]) - set(plan_steps))
        if unknown_steps:
            return _diagnose(
                projection,
                "execution_state_invalid",
                f"execution ledger references unknown steps: {', '.join(unknown_steps)}",
                ".agent/step-runs.jsonl",
            )
        open_attempts = [
            (step_id, attempt_id)
            for step_id, step in state["steps"].items()
            for attempt_id in step.get("open_attempts", [])
        ]
        if len(open_attempts) > 1:
            return _diagnose(
                projection,
                "ambiguous_open_attempts",
                "multiple open attempts prevent safe resumability selection",
                ".agent/step-runs.jsonl",
            )
        if not open_attempts:
            eligible = next_step(root, plan)
            if eligible is not None:
                step_id = eligible["id"]
                step_state = state["steps"].get(step_id, {})
                projection["step"] = {
                    "id": step_id,
                    "state": step_state.get("status", "pending"),
                    "completed": bool(step_state.get("completed")),
                }
            projection["lease"]["state"] = "not_applicable"
            projection["recovery_actions"] = _project_recovery_actions(
                projection["step"]["id"] if projection["step"] else None,
                None,
                agent_id,
                policy,
                "not_applicable",
            )
            return projection

        step_id, attempt_id = open_attempts[0]
        attempt = state["attempts"].get(attempt_id)
        if not isinstance(attempt, dict) or attempt.get("step_id") != step_id:
            return _diagnose(
                projection,
                "execution_state_invalid",
                f"attempt {attempt_id} is inconsistent with step {step_id}",
                ".agent/step-runs.jsonl",
            )
        if policy == "enforce" and not attempt.get("agent_id"):
            return _diagnose(
                projection,
                "execution_state_invalid",
                f"enforced attempt {attempt_id} has no owner",
                ".agent/step-runs.jsonl",
            )
        expires_at = attempt.get("lease_expires_at")
        if expires_at is not None and attempt_deadline(attempt) is None:
            return _diagnose(
                projection,
                "execution_state_invalid",
                f"attempt {attempt_id} has an invalid lease deadline",
                ".agent/step-runs.jsonl",
            )
        current = now or datetime.now(timezone.utc)
        lease_state = (
            "no_deadline"
            if expires_at is None
            else "expired"
            if attempt_is_expired(attempt, current, grace)
            else "live"
        )
        step_state = state["steps"][step_id]
        projection["step"] = {
            "id": step_id,
            "state": step_state.get("status", "pending"),
            "completed": bool(step_state.get("completed")),
        }
        projection["attempt"] = {
            "id": attempt_id,
            "state": attempt.get("status"),
            "owner": attempt.get("agent_id"),
            "open": bool(attempt.get("open")),
        }
        projection["lease"].update({
            "expires_at": expires_at,
            "state": lease_state,
        })
        projection["receipts"] = _project_receipts(root, step_id, attempt_id)
        projection["gates"] = verify_step(
            root,
            plan,
            step_id,
            attempt_id,
            strict=strict,
            record=False,
            include_gates=True,
        ).get("gates", [])
        projection["recovery_actions"] = _project_recovery_actions(
            step_id,
            attempt,
            agent_id,
            policy,
            lease_state,
        )
        return projection
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return _diagnose(
            projection,
            "execution_state_invalid",
            str(exc),
            "execution ledgers",
        )


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


def next_action(
    root: Path,
    strict: bool = False,
    agent_id: Optional[str] = None,
) -> Action:
    try:
        plan = _load_plan(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _invalid_action(_diagnose(
            _base_resumability(None, agent_id),
            "plan_invalid",
            str(exc),
            ".agent/plan.lock.json",
        ))
    if plan is None:
        projection = _diagnose(
            _base_resumability(None, agent_id),
            "plan_missing",
            "no .agent/plan.lock.json",
            ".agent/plan.lock.json",
        )
        return _attach(
            Action("uninitialized", "no .agent/plan.lock.json",
                   command=_cmd(["init"]), args=["init"]),
            projection,
        )
    if not isinstance(plan, dict):
        return _invalid_action(_diagnose(
            _base_resumability(None, agent_id),
            "plan_invalid",
            ".agent/plan.lock.json top-level value must be a JSON object",
            ".agent/plan.lock.json",
        ))
    if plan.get("locked") is not True:
        projection = _diagnose(
            _base_resumability(plan, agent_id),
            "plan_unlocked",
            "plan exists but is not locked",
            ".agent/plan.lock.json",
        )
        args = ["lock-plan", ".agent/plan.lock.json"]
        return _attach(
            Action("plan_unlocked", "plan exists but is not locked",
                   command=_cmd(args), args=args),
            projection,
        )
    if not (root / ".agent/execution.contract.json").exists():
        projection = _diagnose(
            _base_resumability(plan, agent_id),
            "execution_contract_missing",
            "execution contract missing",
            ".agent/execution.contract.json",
        )
        return _attach(
            Action("execution_uninitialized", "execution contract missing",
                   command=_cmd(["init-execution"]), args=["init-execution"]),
            projection,
        )

    projection = resumability_projection(root, plan, agent_id, strict)
    if projection["diagnostics"]:
        return _invalid_action(projection)
    attempt = projection["attempt"]
    inflight = None
    if attempt is not None and projection["step"] is not None:
        step_id = projection["step"]["id"]
        step = next(
            item
            for item in plan.get("steps", [])
            if isinstance(item, dict) and item.get("id") == step_id
        )
        inflight = (step_id, attempt["id"], step)
    if inflight is None:
        eligible = next_step(root, plan)
        if eligible is not None:
            step_id = eligible["id"]
            args = ["claim-step", step_id, "--agent", "$USER"]
            return _attach(
                Action("step_unclaimed", f"step {step_id} is eligible and unclaimed",
                       command=_cmd(args), args=args, step_id=step_id),
                projection,
            )
        # All steps complete: run/drift/proof states.
        drift = audit_drift(root, plan)
        if drift.get("status") == "fail":
            return _attach(
                Action("drift_failing", "changes fall outside plan scope",
                       command=_cmd(["finish-run"]), args=["finish-run"],
                       diagnostics=[f"out of scope: {p}" for p in
                                    drift.get("out_of_scope_files", [])]),
                projection,
            )
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
            return _attach(
                Action("run_unverified", "run-level verification not satisfied",
                       command=_cmd(["finish-run"]), args=["finish-run"],
                       diagnostics=diagnostics),
                projection,
            )
        proof_path = root / ".agent/proof-pack.json"
        if not proof_path.exists():
            return _attach(
                Action("proof_missing", "proof pack not built",
                       command=_cmd(["finish-run"]), args=["finish-run"]),
                projection,
            )
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
            return _attach(
                Action(state, "proof verification failed",
                       command=_cmd(["finish-run"]), args=["finish-run"],
                       diagnostics=[f["message"] for f in errors]),
                projection,
            )
        return _attach(
            Action("complete", "run complete and proof verified",
                   command=None, args=[], blocking=False),
            projection,
        )

    step_id, attempt_id, step = inflight
    unrecorded = _scoped_unrecorded_paths(root, plan, step_id, attempt_id)
    if unrecorded:
        args = ["record-file-change", "--step", step_id, "--path", unrecorded[0]]
        return _attach(
            Action(
                "file_receipts_missing",
                f"step {step_id} has changed scoped files with no receipt",
                command=_cmd(args), args=args, step_id=step_id,
                diagnostics=[f"unrecorded: {p}" for p in unrecorded],
            ),
            projection,
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
        return _attach(
            Action(
                "validation_missing",
                f"step {step_id} has an unmet validation gate",
                command=_cmd(args), args=args, step_id=step_id, gate=gate,
                diagnostics=[f["message"] for f in findings],
            ),
            projection,
        )
    if not attempt_is_verified(root, step_id, attempt_id):
        args = ["finish-step", step_id]
        return _attach(
            Action("step_unverified",
                   f"step {step_id} validated but not verified",
                   command=_cmd(args), args=args, step_id=step_id),
            projection,
        )
    args = ["finish-step", step_id]
    return _attach(
        Action("step_uncompleted",
               f"step {step_id} verified but not completed",
               command=_cmd(args), args=args, step_id=step_id),
        projection,
    )


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
