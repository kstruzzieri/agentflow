"""Execution verification coverage for Agentflow v0.3."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import append_jsonl, read_jsonl, try_read_json, utc_now
from .contracts import TRUSTED_PROVENANCE_VALUES, VERIFICATION_RUNS_SCHEMA_VERSION
from .execution import (
    TERMINAL_EVENTS,
    attempt_is_expired,
    lease_grace_seconds,
    lease_policy,
    read_step_events,
    read_step_state,
    resolve_attempt,
)
from .git import changed_file_records
from .hunks import effective_hunk_policy, unmapped_hunks
from .receipts import (
    command_receipts,
    file_receipts,
    path_in_effective_scope,
    replay_gates,
    sha256_path,
)
from .stuck import detect_stuck
from .validation import audit_drift, matches_path, validate_plan


def _step_by_id(plan: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    for step in plan.get("steps", []):
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise ValueError(f"unknown step id: {step_id}")


def _evidence_ids(root: Path) -> set[str]:
    return {
        item["id"]
        for item in read_jsonl(root / ".agent/evidence.jsonl")
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _command_gate_key(gate: Dict[str, Any]) -> str:
    return " ".join(gate["run"])


def _matching_command_receipt(
    gate: Dict[str, Any],
    receipts: List[Dict[str, Any]],
    receipt_by_gate: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if isinstance(gate.get("run"), list):
        for receipt in receipts:
            if receipt.get("command") == gate["run"]:
                return receipt
    for alias in gate.get("aliases", [gate.get("gate")]):
        receipt = receipt_by_gate.get(alias)
        if receipt is not None:
            return receipt
    return None


def _gates_for_step(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    gates = step.get("gates")
    if isinstance(gates, list) and gates:
        resolved = []
        validation = [
            item
            for item in step.get("validation", [])
            if isinstance(item, str)
        ]
        for index, gate in enumerate(gates):
            if not isinstance(gate, dict):
                continue
            if gate.get("kind") == "command" and isinstance(gate.get("run"), list):
                aliases = [_command_gate_key(gate)]
                if index < len(validation):
                    aliases.append(validation[index])
                resolved.append(
                    {
                        "kind": "command",
                        "gate": aliases[0],
                        "aliases": aliases,
                        "run": gate["run"],
                    }
                )
            elif gate.get("kind") == "inspection":
                resolved.append(
                    {
                        "kind": "inspection",
                        "evidence_id": gate.get("evidence_id"),
                        "describe": gate.get("describe", ""),
                    }
                )
        return resolved
    return [
        {"kind": "legacy", "gate": item}
        for item in step.get("validation", [])
        if isinstance(item, str)
    ]


def _add_finding(target: Dict[str, List[Dict[str, str]]], severity: str, message: str) -> None:
    target["errors" if severity == "error" else "warnings"].append(
        {"severity": severity, "message": message}
    )


def _receipt_timed_out(receipt: Dict[str, Any]) -> bool:
    return receipt.get("decision") == "timeout" or receipt.get("timed_out") is True


def _timeout_message(gate: str, receipt: Dict[str, Any]) -> str:
    timeout_seconds = receipt.get("timeout_seconds")
    return f"gate {gate} timed out after {timeout_seconds} seconds"


def _proof_policy(root: Path) -> Dict[str, Any]:
    contract_path = root / ".agent/execution.contract.json"
    if not contract_path.exists():
        return {}
    try:
        contract, _ = try_read_json(contract_path)
    except OSError:
        return {}
    if contract is None:
        return {}
    policy = contract.get("proof_policy", {})
    return policy if isinstance(policy, dict) else {}


def _verification_run_id(root: Path) -> str:
    path = root / ".agent/verification-runs.jsonl"
    return f"VR{len(read_jsonl(path)) + 1}"


def _append_verification(
    root: Path,
    scope: str,
    step_id: Optional[str],
    attempt_id: Optional[str],
    strict: bool,
    replay: bool,
    result: Dict[str, Any],
) -> None:
    entry: Dict[str, Any] = {
        "schema_version": VERIFICATION_RUNS_SCHEMA_VERSION,
        "id": _verification_run_id(root),
        "scope": scope,
        "strict": strict,
        "replay": replay,
        "status": result["status"],
        "findings": [*result["errors"], *result["warnings"]],
        "recorded_at": utc_now(),
    }
    if step_id is not None:
        entry["step_id"] = step_id
    if attempt_id is not None:
        entry["attempt_id"] = attempt_id
    append_jsonl(root / ".agent/verification-runs.jsonl", entry)


def verify_step(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    attempt_id: Optional[str],
    strict: bool = False,
    replay: bool = False,
    record: bool = True,
    include_gates: bool = False,
) -> Dict[str, Any]:
    step = _step_by_id(plan, step_id)
    resolved_attempt = resolve_attempt(root, step_id, attempt_id)
    result: Dict[str, Any] = {
        "status": "passed",
        "errors": [],
        "warnings": [],
    }
    gate_results: List[Dict[str, Any]] = []
    if include_gates:
        result["gates"] = gate_results
    policy = _proof_policy(root)
    effective_strict = strict or bool(policy.get("strict_by_default"))
    require_command_receipts = effective_strict or bool(
        policy.get("require_command_receipts_for_validation")
    )
    require_inspection_evidence = effective_strict or bool(
        policy.get("require_evidence_for_inspection_gates")
    )
    require_trusted_receipts = effective_strict or bool(
        policy.get("require_managed_receipts_for_validation")
    )
    evidence = _evidence_ids(root)
    receipts = [
        receipt
        for receipt in command_receipts(root)
        if receipt.get("step_id") == step_id and receipt.get("attempt_id") == resolved_attempt
    ]
    files = [
        receipt
        for receipt in file_receipts(root)
        if receipt.get("step_id") == step_id and receipt.get("attempt_id") == resolved_attempt
    ]
    receipt_by_gate = {
        receipt.get("gate"): receipt
        for receipt in receipts
        if isinstance(receipt.get("gate"), str)
    }
    for gate in _gates_for_step(step):
        gate_result = {
            "kind": gate["kind"],
            "label": gate.get("gate") or gate.get("evidence_id") or gate.get("describe", ""),
            "status": "satisfied",
            "receipt_id": None,
            "evidence_id": gate.get("evidence_id"),
        }
        gate_results.append(gate_result)
        if gate["kind"] == "command":
            receipt = _matching_command_receipt(gate, receipts, receipt_by_gate)
            if receipt is None:
                gate_result["status"] = "missing"
                _add_finding(
                    result,
                    "error" if require_command_receipts else "warning",
                    f"missing command receipt for gate: {gate['gate']}",
                )
                continue
            gate_result["receipt_id"] = receipt.get("id")
            if _receipt_timed_out(receipt):
                gate_result["status"] = "failed"
                _add_finding(result, "error", _timeout_message(gate["gate"], receipt))
            elif receipt.get("exit_code") != 0:
                gate_result["status"] = "failed"
                _add_finding(
                    result,
                    "error",
                    f"gate {gate['gate']} recorded exit code {receipt.get('exit_code')}",
                )
            if require_trusted_receipts and receipt.get("provenance") not in TRUSTED_PROVENANCE_VALUES:
                gate_result["status"] = "failed"
                _add_finding(result, "error", f"gate {gate['gate']} uses attested provenance")
        elif gate["kind"] == "inspection":
            evidence_id = gate.get("evidence_id")
            if evidence_id not in evidence:
                gate_result["status"] = "missing"
                _add_finding(result, "error", f"inspection evidence id {evidence_id} is missing")
        else:
            receipt = receipt_by_gate.get(gate["gate"])
            if receipt:
                gate_result["receipt_id"] = receipt.get("id")
                if _receipt_timed_out(receipt):
                    gate_result["status"] = "failed"
                    _add_finding(result, "error", _timeout_message(gate["gate"], receipt))
                elif receipt.get("exit_code") != 0:
                    gate_result["status"] = "failed"
                    _add_finding(
                        result,
                        "error",
                        f"gate {gate['gate']} recorded exit code {receipt.get('exit_code')}",
                    )
                if (
                    require_trusted_receipts
                    and receipt.get("provenance") not in TRUSTED_PROVENANCE_VALUES
                ):
                    gate_result["status"] = "failed"
                    _add_finding(result, "error", f"gate {gate['gate']} uses attested provenance")
            elif not any(evidence_id in evidence for evidence_id in step.get("evidence_ids", [])):
                gate_result["status"] = "missing"
                _add_finding(
                    result,
                    "error"
                    if require_command_receipts or require_inspection_evidence
                    else "warning",
                    f"legacy inspection gate needs evidence or receipt: {gate['gate']}",
                )
    for receipt in files:
        path = receipt.get("path")
        if isinstance(path, str) and not path_in_effective_scope(plan, step_id, path):
            _add_finding(result, "error", f"{path} is outside effective file scope for {step_id}")
    if replay:
        replay_result = replay_gates(root, plan, step_id=step_id, record=False)
        result["errors"].extend(replay_result["errors"])
        result["warnings"].extend(replay_result["warnings"])
    if result["errors"]:
        result["status"] = "failed"
    elif result["warnings"]:
        result["status"] = "warning"
    if record:
        _append_verification(root, "step", step_id, resolved_attempt, effective_strict, replay, result)
    return result


def build_execution_coverage(
    root: Path,
    plan: Dict[str, Any],
    strict: bool = False,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    state = read_step_state(root)
    coverage: Dict[str, Any] = {
        "steps": {},
        "attempts": len(state["attempts"]),
        "completed_steps": [],
        "failed_attempts": [],
    }
    # #20 lease diagnostics. abandoned_attempts is a deterministic ledger fact.
    # expired_leases / no_deadline_open_attempts are evaluated against `now` at
    # build time and may legitimately differ before vs. after an expiry -- they
    # are proof facts at build time, not reproducible-across-time invariants.
    coverage["expired_leases"] = []
    coverage["no_deadline_open_attempts"] = []
    coverage["abandoned_attempts"] = [
        {
            "attempt_id": aid,
            "abandoned_by": attempt["events"][-1].get("abandoned_by"),
            "superseded_by": attempt["events"][-1].get("superseded_by"),
        }
        for aid, attempt in state["attempts"].items()
        if attempt.get("status") == "abandoned" and attempt.get("events")
    ]
    enforce = lease_policy(root) == "enforce"
    grace = lease_grace_seconds(root)
    current = now or datetime.now(timezone.utc)
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            continue
        step_id = step["id"]
        step_state = state["steps"].get(
            step_id,
            {"status": "pending", "completed": False, "open_attempts": []},
        )
        step_result = {"status": step_state["status"], "errors": [], "warnings": []}
        if step_state.get("completed"):
            coverage["completed_steps"].append(step_id)
        if step_state.get("open_attempts"):
            step_result["warnings"].append(
                {"severity": "warning", "message": f"{step_id} has open attempt"}
            )
        for aid in step_state.get("open_attempts", []):
            attempt = state["attempts"].get(aid, {})
            if attempt.get("lease_expires_at") is None:
                if enforce:
                    coverage["no_deadline_open_attempts"].append(aid)
                    step_result["warnings"].append(
                        {
                            "severity": "warning",
                            "message": f"{step_id}/{aid} has no lease deadline; owner renew-lease or fail-step",
                        }
                    )
            elif attempt_is_expired(attempt, current, grace):
                coverage["expired_leases"].append(aid)
                if enforce:
                    step_result["errors"].append(
                        {
                            "severity": "error",
                            "message": f"{step_id}/{aid} lease expired at {attempt['lease_expires_at']}; reclaim-step or fail-step",
                        }
                    )
                else:
                    step_result["warnings"].append(
                        {
                            "severity": "warning",
                            "message": f"{step_id}/{aid} lease expired (advisory)",
                        }
                    )
        if step_state["status"] == "failed":
            coverage["failed_attempts"].extend(step_state.get("attempts", []))
        coverage["steps"][step_id] = step_result
    return coverage


def _ignored(path: str, prefixes: List[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _latest_file_receipts_by_path(root: Path) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for receipt in file_receipts(root):
        path = receipt.get("path")
        if isinstance(path, str):
            latest[path] = receipt
    return latest


def _reconcile_changed_files(
    root: Path,
    plan: Dict[str, Any],
    require_file_receipts: bool = True,
) -> List[Dict[str, str]]:
    contract: Dict[str, Any] = {}
    contract_path = root / ".agent/execution.contract.json"
    if contract_path.exists():
        try:
            contract, _ = try_read_json(contract_path)
        except OSError:
            contract = None
        if contract is None:
            contract = {}
    ignore = contract.get("concurrency", {}).get("reconcile_ignore", [".agent/", ".git/"])
    latest = _latest_file_receipts_by_path(root)
    findings: List[Dict[str, str]] = []
    for record in changed_file_records(root):
        path = record["path"]
        if _ignored(path, ignore):
            continue
        receipt = latest.get(path)
        if receipt is None:
            if not require_file_receipts:
                continue
            findings.append(
                {
                    "severity": "error",
                    "message": f"changed file is not mapped to a step receipt: {path}",
                }
            )
            continue
        if record["change_kind"] == "deleted":
            if receipt.get("change_kind") != "deleted":
                findings.append(
                    {"severity": "error", "message": f"deleted file lacks deleted receipt: {path}"}
                )
            continue
        expected_hash = receipt.get("after_sha256")
        actual_hash = sha256_path(root / path)
        if expected_hash != actual_hash:
            findings.append(
                {"severity": "error", "message": f"out-of-band edit after latest file receipt: {path}"}
            )
    if effective_hunk_policy(root) == "enforce":
        # Scope-filter candidates the same way audit_drift does, so verify-run
        # and audit-drift cannot diverge on which paths are hunk-checked.
        allowed = plan.get("allowed_files", [])
        blocked = plan.get("blocked_files", [])
        candidates = [
            record
            for record in changed_file_records(root)
            if not _ignored(record["path"], ignore)
            and matches_path(record["path"], allowed)
            and not matches_path(record["path"], blocked)
        ]
        for entry in unmapped_hunks(root, candidates):
            findings.append(
                {
                    "severity": "error",
                    "message": (
                        f"unmapped hunk in {entry['path']} "
                        f"(@@ -{entry['old_start']},{entry['old_count']} "
                        f"+{entry['new_start']},{entry['new_count']} @@): no matching file receipt"
                    ),
                }
            )
    return findings


def _parse_iso_timestamp(value: str) -> Optional[datetime]:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(normalized)
        except ValueError:
            return None
        parsed = datetime.combine(parsed_date, datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _terminal_attempt_violations(root: Path) -> List[Dict[str, str]]:
    """Flag receipts whose attempt is unknown/foreign or terminal-at-write.

    Backstop for hand-edited ledgers; the write guard is the primary path.
    Every receipt must name an existing attempt for the same step. New-work
    receipts must additionally predate their attempt's terminal event;
    reconstructed/managed command receipts are exempt from that terminal check
    (gate replay legitimately writes them to completed attempts).
    """
    state = read_step_state(root)
    attempts = state["attempts"]
    terminal_at: Dict[tuple, datetime] = {}
    for event in read_step_events(root):
        if event.get("event") in TERMINAL_EVENTS:
            stamp = event.get("recorded_at")
            if isinstance(stamp, str):
                parsed = _parse_iso_timestamp(stamp)
                if parsed is not None:
                    terminal_at[(event.get("step_id"), event.get("attempt_id"))] = parsed
    exempt = {"reconstructed", "managed"}
    findings: List[Dict[str, str]] = []

    def _check(receipt: Dict[str, Any], stamp_key: str, new_work: bool) -> None:
        step_id = receipt.get("step_id")
        attempt_id = receipt.get("attempt_id")
        receipt_id = receipt.get("id")
        attempt = attempts.get(attempt_id)
        if attempt is None or attempt.get("step_id") != step_id:
            findings.append(
                {
                    "severity": "error",
                    "message": f"receipt references unknown attempt: {receipt_id}",
                }
            )
            return
        if not new_work:
            return
        term = terminal_at.get((step_id, attempt_id))
        stamp = receipt.get(stamp_key)
        parsed_stamp = _parse_iso_timestamp(stamp) if isinstance(stamp, str) else None
        if term and parsed_stamp is not None and parsed_stamp > term:
            findings.append(
                {
                    "severity": "error",
                    "message": f"terminal-attempt receipt violation: {receipt_id}",
                }
            )

    for receipt in file_receipts(root):
        _check(receipt, "recorded_at", new_work=True)
    for receipt in command_receipts(root):
        _check(receipt, "finished_at", new_work=receipt.get("provenance") not in exempt)
    return findings


def _timeout_receipt_violations(root: Path) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    for receipt in command_receipts(root):
        if _receipt_timed_out(receipt):
            findings.append(
                {
                    "severity": "error",
                    "message": (
                        f"command receipt {receipt.get('id')} for {receipt.get('step_id')} "
                        f"timed out after {receipt.get('timeout_seconds')} seconds"
                    ),
                }
            )
    return findings


def _unresolved_finding_refs(root: Path) -> List[Dict[str, str]]:
    """Flag amendment finding refs whose review run is not recorded.

    Backstop for review-cycle integration: an ``amendment_started`` event may
    cite findings from a review run that was never written to
    ``.agent/review-runs.jsonl``. Each such reference is surfaced as a finding.
    """
    recorded = {
        row.get("review_run_id")
        for row in read_jsonl(root / ".agent/review-runs.jsonl")
        if isinstance(row, dict)
    }
    findings: List[Dict[str, str]] = []
    for event in read_step_events(root):
        if event.get("event") != "amendment_started":
            continue
        for ref in event.get("finding_refs", []) or []:
            if not isinstance(ref, dict):
                continue
            if ref.get("review_run_id") not in recorded:
                findings.append(
                    {
                        "severity": "warning",
                        "message": (
                            f"unresolved finding ref {ref.get('review_run_id')}#"
                            f"{ref.get('finding_id')} on {event.get('step_id')}/"
                            f"{event.get('attempt_id')}"
                        ),
                    }
                )
    return findings


def verify_run(
    root: Path,
    plan: Dict[str, Any],
    strict: bool = False,
    record: bool = True,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "passed", "errors": [], "warnings": []}
    policy = _proof_policy(root)
    effective_strict = strict or bool(policy.get("strict_by_default"))
    require_file_receipts = effective_strict or bool(
        policy.get("require_file_receipts_for_changed_files", True)
    )
    for error in validate_plan(plan):
        result["errors"].append({"severity": "error", "message": error})
    drift = audit_drift(root, plan)
    if drift.get("status") == "fail":
        for path in drift.get("out_of_scope_files", []):
            if not path.startswith(".agent/"):
                result["errors"].append(
                    {"severity": "error", "message": f"out-of-scope changed file: {path}"}
                )
        for path in drift.get("blocked_files_changed", []):
            result["errors"].append(
                {"severity": "error", "message": f"blocked file changed: {path}"}
            )
    result["errors"].extend(_reconcile_changed_files(root, plan, require_file_receipts))
    coverage = build_execution_coverage(root, plan, effective_strict, now=now)
    for step_id, step in coverage["steps"].items():
        for warning in step.get("warnings", []):
            result["warnings"].append(warning)
        # #20: an unrecovered expired lease under enforce is a coverage error and
        # must gate the run (advisory downgrades it to a warning above).
        for error in step.get("errors", []):
            result["errors"].append(error)
        if step["status"] == "failed":
            result["errors"].append({"severity": "error", "message": f"{step_id} has failed attempt"})
        if effective_strict and step["status"] != "completed":
            result["errors"].append({"severity": "error", "message": f"{step_id} is not completed"})
    result["errors"].extend(_terminal_attempt_violations(root))
    result["errors"].extend(_timeout_receipt_violations(root))
    for finding in _unresolved_finding_refs(root):
        if effective_strict:
            result["errors"].append(
                {"severity": "error", "message": finding["message"]}
            )
        else:
            result["warnings"].append(finding)
    for finding in detect_stuck(root, plan)["findings"]:
        result["warnings"].append(
            {
                "severity": "warning",
                "message": finding["message"],
                "rule": finding["rule"],
                "step_id": finding["step_id"],
                "attempt_id": finding["attempt_id"],
            }
        )
    if result["errors"]:
        result["status"] = "failed"
    elif result["warnings"]:
        result["status"] = "warning"
    if record:
        _append_verification(root, "run", None, None, effective_strict, False, result)
    return result
