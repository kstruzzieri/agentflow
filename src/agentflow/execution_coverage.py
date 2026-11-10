"""Execution verification coverage for Agentflow v0.3."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .artifacts import append_jsonl, read_jsonl, utc_now
from .contracts import TRUSTED_PROVENANCE_VALUES, VERIFICATION_RUNS_SCHEMA_VERSION
from .execution import (
    ATTEMPT_OPENING_EVENTS,
    LEASE_EVENTS,
    OPEN_EVENTS,
    TERMINAL_EVENTS,
    attempt_is_expired,
    execution_contract_sha256,
    lease_grace_seconds,
    latest_completed_attempt,
    lease_policy,
    read_step_events,
    read_step_state,
    receipt_timed_out,
    resolve_attempt,
    try_load_execution_contract,
)
from .git import changed_file_records, is_git_repo
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
        # Latest wins, like receipt_by_gate and coverage._latest_command_receipt.
        for receipt in reversed(receipts):
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


def _timeout_message(gate: str, receipt: Dict[str, Any]) -> str:
    timeout_seconds = receipt.get("timeout_seconds")
    return f"gate {gate} timed out after {timeout_seconds} seconds"


def _proof_policy(root: Path) -> Dict[str, Any]:
    contract = try_load_execution_contract(root) or {}
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
        "execution_contract_sha256": execution_contract_sha256(root),
    }
    if step_id is not None:
        entry["step_id"] = step_id
    if attempt_id is not None:
        entry["attempt_id"] = attempt_id
    append_jsonl(root / ".agent/verification-runs.jsonl", entry)


def _check_gates(
    root: Path,
    step: Dict[str, Any],
    attempt_id: str,
    effective_strict: bool,
    policy: Dict[str, Any],
    result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Judge step's gates on attempt_id's receipts; findings go into result.

    verify_step and the proof's validation gate coverage (#110) share this, so
    the proof names exactly the receipt or evidence verify-step accepted.
    """
    gate_results: List[Dict[str, Any]] = []
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
        if receipt.get("step_id") == step["id"] and receipt.get("attempt_id") == attempt_id
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
            if receipt_timed_out(receipt):
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
                if receipt_timed_out(receipt):
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
    return gate_results


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
    policy = _proof_policy(root)
    effective_strict = strict or bool(policy.get("strict_by_default"))
    gate_results = _check_gates(root, step, resolved_attempt, effective_strict, policy, result)
    if include_gates:
        result["gates"] = gate_results
    files = [
        receipt
        for receipt in file_receipts(root)
        if receipt.get("step_id") == step_id and receipt.get("attempt_id") == resolved_attempt
    ]
    for receipt in files:
        path = receipt.get("path")
        if isinstance(path, str) and not path_in_effective_scope(plan, step_id, path):
            _add_finding(result, "error", f"{path} is outside effective file scope for {step_id}")
    # An error in every mode: a warning would still let finish-step complete.
    for message in _inherited_receipt_errors(root, plan, step_id, resolved_attempt):
        _add_finding(result, "error", message)
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


def validation_gate_coverage(root: Path, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """#110: how each gate of each step's latest completed attempt was satisfied.

    Judged by verify-step's rules under the execution contract's own policy,
    never a --strict flag, so verify-proof can recompute it from the hashed
    ledgers alone.
    """
    policy = _proof_policy(root)
    effective_strict = bool(policy.get("strict_by_default"))
    provenance = {receipt.get("id"): receipt.get("provenance") for receipt in command_receipts(root)}
    evidence = _evidence_ids(root)
    entries: List[Dict[str, Any]] = []
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            continue
        completed = latest_completed_attempt(root, step["id"])
        if completed is None:
            continue
        attempt_id = completed[0]
        findings: Dict[str, Any] = {"errors": [], "warnings": []}
        for gate in _check_gates(root, step, attempt_id, effective_strict, policy, findings):
            evidence_id = gate["evidence_id"]
            if gate["kind"] == "legacy" and gate["receipt_id"] is None and gate["status"] == "satisfied":
                # verify-step accepts any of the step's evidence ids; name the first present.
                evidence_id = next(item for item in step.get("evidence_ids", []) if item in evidence)
            entries.append(
                {
                    "step_id": step["id"],
                    "attempt_id": attempt_id,
                    "gate": gate["label"],
                    "kind": gate["kind"],
                    "status": gate["status"],
                    "receipt_id": gate["receipt_id"],
                    "provenance": provenance.get(gate["receipt_id"]) if gate["receipt_id"] else None,
                    "evidence_id": evidence_id,
                }
            )
    return entries


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


def latest_file_receipts_by_path(root: Path) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for receipt in file_receipts(root):
        path = receipt.get("path")
        if isinstance(path, str):
            latest[path] = receipt
    return latest


def receipt_matches_worktree(root: Path, record: Dict[str, str], receipt: Dict[str, Any]) -> bool:
    """True when receipt records record's changed path as it is now.

    verify-run and next-action (#97) share this check.
    """
    if record["change_kind"] == "deleted":
        return receipt.get("change_kind") == "deleted"
    return receipt.get("after_sha256") == sha256_path(root / record["path"])


def receipt_counts_for_attempt(
    receipt: Dict[str, Any], attempt_id: Optional[str], attempts: Dict[str, Any]
) -> bool:
    """True when attempt_id or a completed attempt wrote receipt.

    An open, blocked, failed or abandoned attempt's receipt does not cover
    another attempt. next-action and verify-step share this rule.
    """
    producer = receipt.get("attempt_id")
    return producer == attempt_id or attempts.get(producer, {}).get("status") == "completed"


def _inherited_receipt_errors(
    root: Path, plan: Dict[str, Any], step_id: str, attempt_id: Optional[str]
) -> List[str]:
    """Changed in-scope files whose latest receipt attempt_id may not rely on.

    A missing or stale receipt stays verify-run's finding. Non-git roots have
    no changed-file list to inspect.
    """
    if not is_git_repo(root):
        return []
    try:
        records = changed_file_records(root)
    except RuntimeError as exc:
        return [f"cannot inspect changed files: {exc}"]
    attempts = read_step_state(root)["attempts"]
    latest = latest_file_receipts_by_path(root)
    errors: List[str] = []
    for record in records:
        path = record["path"]
        if path.startswith((".agent/", ".git/")) or not path_in_effective_scope(plan, step_id, path):
            continue
        receipt = latest.get(path)
        if receipt is None or receipt_counts_for_attempt(receipt, attempt_id, attempts):
            continue
        producer = receipt.get("attempt_id")
        status = attempts.get(producer, {}).get("status", "unknown")
        errors.append(
            f"{path} was last recorded by attempt {producer} ({status}); "
            f"record it in attempt {attempt_id}"
        )
    return errors


def _reconcile_changed_files(
    root: Path,
    plan: Dict[str, Any],
    require_file_receipts: bool = True,
) -> List[Dict[str, str]]:
    return [finding for _path, finding, _stopped in _changed_file_pairs(root, plan, require_file_receipts)]


def _changed_file_pairs(
    root: Path,
    plan: Dict[str, Any],
    require_file_receipts: bool = True,
) -> List[Tuple[str, Dict[str, str], Optional[Dict[str, Any]]]]:
    """(path, finding, stopped) for each changed file verify-run flags (#73 needs the path).

    #103: stopped is set when the latest receipt matches the tree but a blocked,
    failed or abandoned attempt wrote it and the path's latest completed receipt
    does not match. It names that attempt for next-action; the finding stays
    severity and message. An open attempt and an unknown producer each have
    their own verify-run error.
    """
    contract = try_load_execution_contract(root) or {}
    ignore = contract.get("concurrency", {}).get("reconcile_ignore", [".agent/", ".git/"])
    latest = latest_file_receipts_by_path(root)
    attempts = read_step_state(root)["attempts"]
    latest_completed = {
        receipt["path"]: receipt
        for receipt in file_receipts(root)
        if isinstance(receipt.get("path"), str)
        and attempts.get(receipt.get("attempt_id"), {}).get("status") == "completed"
    }
    findings: List[Tuple[str, Dict[str, str], Optional[Dict[str, Any]]]] = []
    for record in changed_file_records(root):
        path = record["path"]
        if _ignored(path, ignore):
            continue
        receipt = latest.get(path)
        if receipt is None:
            if not require_file_receipts:
                continue
            findings.append((path, {
                "severity": "error",
                "message": f"changed file is not mapped to a step receipt: {path}",
            }, None))
            continue
        if receipt_matches_worktree(root, record, receipt):
            attempt_id = receipt.get("attempt_id")
            status = attempts.get(attempt_id, {}).get("status")
            completed = latest_completed.get(path)
            if status in _SUPERSEDABLE_STATUSES and not (
                completed and receipt_matches_worktree(root, record, completed)
            ):
                findings.append((path, {
                    "severity": "error",
                    "message": f"unverified edit from {status} attempt {attempt_id}: {path}",
                }, {
                    "cause": "unverified_edit",
                    "step_id": receipt.get("step_id"),
                    "attempt_id": attempt_id,
                    "status": status,
                }))
            continue
        message = (
            f"deleted file lacks deleted receipt: {path}"
            if record["change_kind"] == "deleted"
            else f"out-of-band edit after latest file receipt: {path}"
        )
        findings.append((path, {"severity": "error", "message": message}, None))
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
            findings.append((entry["path"], {
                "severity": "error",
                "message": (
                    f"unmapped hunk in {entry['path']} "
                    f"(@@ -{entry['old_start']},{entry['old_count']} "
                    f"+{entry['new_start']},{entry['new_count']} @@): no matching file receipt"
                ),
            }, None))
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


_SUPERSEDABLE_STATUSES = {"blocked", "failed", "abandoned"}


class _LedgerAttempts(NamedTuple):
    owner: Dict[str, str]  # attempt id -> step id, by first appearance
    inconsistent: set  # attempt ids with mixed steps or duplicate terminals
    opened_at: Dict[str, int]  # insertion order == first-opened ledger order
    last_index: Dict[str, int]  # latest non-lease event per attempt
    last_kind: Dict[str, Any]


def _ledger_attempts(events: List[Dict[str, Any]]) -> _LedgerAttempts:
    """One ledger scan shared by supersession and receipt classification."""
    owner: Dict[str, str] = {}
    inconsistent = set()
    closed_attempts = set()
    opened_at: Dict[str, int] = {}
    last_index: Dict[str, int] = {}
    last_kind: Dict[str, Any] = {}
    for index, event in enumerate(events):
        aid, sid, kind = event.get("attempt_id"), event.get("step_id"), event.get("event")
        if not isinstance(aid, str) or not isinstance(sid, str):
            continue
        if owner.setdefault(aid, sid) != sid:
            inconsistent.add(aid)
        if kind in TERMINAL_EVENTS:
            if aid in closed_attempts:
                inconsistent.add(aid)
            closed_attempts.add(aid)
        if kind in ATTEMPT_OPENING_EVENTS:
            opened_at.setdefault(aid, index)
        if kind not in LEASE_EVENTS:
            last_index[aid], last_kind[aid] = index, kind
    return _LedgerAttempts(owner, inconsistent, opened_at, last_index, last_kind)


def _superseding_attempt(
    attempts: _LedgerAttempts, receipt: Dict[str, Any]
) -> Optional[str]:
    """#60: the completed attempt that replaced a timed-out receipt's attempt.

    Judged on ledger order alone. Attempt ids are compared only for equality,
    never ordered, because aggregation renames them (WT<sid>- prefixes).
    """
    step_id, attempt_id = receipt.get("step_id"), receipt.get("attempt_id")
    if not isinstance(step_id, str) or not isinstance(attempt_id, str):
        return None
    if (
        attempt_id in attempts.inconsistent
        or attempts.owner.get(attempt_id) != step_id
        or attempts.last_kind.get(attempt_id) not in _SUPERSEDABLE_STATUSES
    ):
        return None
    closed_at = attempts.last_index[attempt_id]
    for aid, index in attempts.opened_at.items():
        if (
            aid != attempt_id
            and aid not in attempts.inconsistent
            and attempts.owner[aid] == step_id
            and index > closed_at
            and attempts.last_kind.get(aid) == "completed"
        ):
            return aid
    return None


def _classify_timeout_receipt(
    attempts: _LedgerAttempts, receipt: Dict[str, Any], step_ids: set
) -> Tuple[str, Optional[str]]:
    step_id, attempt_id = receipt.get("step_id"), receipt.get("attempt_id")
    if not isinstance(step_id, str) or step_id not in step_ids:
        return "unrecoverable", None
    superseded_by = _superseding_attempt(attempts, receipt)
    if superseded_by is not None:
        return "superseded", superseded_by
    if (
        not isinstance(attempt_id, str)
        or attempts.owner.get(attempt_id) != step_id
        or attempt_id in attempts.inconsistent
    ):
        return "unrecoverable", None
    kind = attempts.last_kind.get(attempt_id)
    if kind in OPEN_EVENTS:
        return "open", None
    if kind in _SUPERSEDABLE_STATUSES:
        return "recoverable", None
    return "unrecoverable", None


def timeout_receipt_states(
    root: Path, plan: Dict[str, Any],
) -> List[Tuple[Dict[str, Any], str, Optional[str]]]:
    """Every timed-out receipt in ledger order as (receipt, state, superseded_by).

    state is superseded (#60), open (attempt still in flight), recoverable (the
    attempt was closed blocked/failed/abandoned and a replacement can supersede
    it) or unrecoverable (completed, unknown, mismatched, inconsistent or an
    undeclared step). superseded_by is set only for superseded.
    """
    timed_out = [receipt for receipt in command_receipts(root) if receipt_timed_out(receipt)]
    if not timed_out:
        return []
    attempts = _ledger_attempts(read_step_events(root))
    step_ids = {
        step["id"] for step in plan.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }
    return [
        (receipt, *_classify_timeout_receipt(attempts, receipt, step_ids))
        for receipt in timed_out
    ]


def _timeout_receipt_findings(
    root: Path, plan: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    for receipt, state, superseded_by in timeout_receipt_states(root, plan):
        message = (
            f"command receipt {receipt.get('id')} for {receipt.get('step_id')} "
            f"timed out after {receipt.get('timeout_seconds')} seconds"
        )
        if state != "superseded":
            errors.append({"severity": "error", "message": message})
            continue
        warnings.append({
            "severity": "warning",
            "rule": "superseded_timeout",
            "message": (
                f"{message} in attempt {receipt['attempt_id']}; "
                f"superseded by completed attempt {superseded_by}"
            ),
            "step_id": receipt["step_id"],
            "attempt_id": receipt["attempt_id"],
            "receipt_id": receipt.get("id"),
            "superseded_by": superseded_by,
        })
    return errors, warnings


def replacement_needs(
    root: Path,
    plan: Dict[str, Any],
    strict: bool = False,
    edits: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Plan-order completed steps that need a replacement attempt, with causes.

    Mirrors the verify_run errors a replacement attempt would clear: a
    recoverable timed-out receipt, a failed latest attempt, and (effective
    strict) a latest attempt that is not completed. attempt_id/status are the
    step's latest non-lease ledger event. #73: when post_completion_edits
    offers them, each flagged change is a post_completion_edit cause (#103:
    unverified_edit for a stopped attempt's edit) on its one candidate step,
    after that step's other causes.
    """
    edit_causes: Dict[str, List[Dict[str, Any]]] = {}
    if edits and edits.get("offered"):
        for pair in edits["pairs"]:
            [owner] = edits["candidates"][pair["path"]]
            edit_causes.setdefault(owner, []).append({"cause": "post_completion_edit", **pair})
    effective_strict = strict or bool(_proof_policy(root).get("strict_by_default"))
    state = read_step_state(root)
    latest: Dict[str, Dict[str, Any]] = {}
    for event in read_step_events(root):
        if (
            isinstance(event.get("step_id"), str)
            and isinstance(event.get("attempt_id"), str)
            and event.get("event") not in LEASE_EVENTS
        ):
            latest[event["step_id"]] = event
    recoverable: Dict[str, List[Dict[str, Any]]] = {}
    for receipt, receipt_state, _ in timeout_receipt_states(root, plan):
        if receipt_state == "recoverable":
            recoverable.setdefault(receipt["step_id"], []).append(receipt)
    needs: List[Dict[str, Any]] = []
    for step in plan.get("steps", []):
        step_id = step.get("id") if isinstance(step, dict) else None
        step_state = state["steps"].get(step_id) if isinstance(step_id, str) else None
        if not step_state or not step_state["completed"] or step_state["open_attempts"]:
            continue
        status = step_state["status"]
        causes: List[Dict[str, Any]] = [
            {
                "cause": "timeout",
                "receipt_id": receipt.get("id"),
                "attempt_id": receipt["attempt_id"],
                "timeout_seconds": receipt.get("timeout_seconds"),
                "status": state["attempts"][receipt["attempt_id"]]["status"],
            }
            for receipt in recoverable.get(step_id, [])
        ]
        if status == "failed":
            causes.append({"cause": "failed_attempt"})
        if effective_strict and status != "completed":
            causes.append({"cause": "strict_incomplete"})
        causes.extend(edit_causes.get(step_id, []))
        if causes:
            needs.append({
                "step_id": step_id,
                "attempt_id": latest[step_id]["attempt_id"],
                "status": latest[step_id]["event"],
                "causes": causes,
            })
    return needs


def _requires_file_receipts(root: Path, strict: bool) -> bool:
    policy = _proof_policy(root)
    return (
        strict
        or bool(policy.get("strict_by_default"))
        or bool(policy.get("require_file_receipts_for_changed_files", True))
    )


def post_completion_edits(
    root: Path, plan: Dict[str, Any], strict: bool = False
) -> Dict[str, Any]:
    """#73: changed files verify-run flags, and whether one step can absorb them.

    For the selector's terminal branch only (no open attempt, nothing
    eligible). Read-only. A path's candidates are the plan steps whose
    effective scope holds it, the check record-file-change applies; that
    permits recording, it does not prove who edited. #103: a stopped attempt's
    edit has one candidate, the step on its receipt, while that step still
    holds the path. offered is True only when every path has exactly one
    candidate, drift has no out-of-scope or blocked file, and nothing makes the
    run permanently unprovable. Git and file errors propagate: the caller fails
    closed.
    """
    edits: Dict[str, Any] = {
        "pairs": [], "candidates": {}, "drift": None,
        "permanent": False, "scope_violation": False, "offered": False,
    }
    if not is_git_repo(root):
        return edits
    pairs = _changed_file_pairs(root, plan, _requires_file_receipts(root, strict))
    if not pairs:
        return edits
    step_ids = [
        step["id"] for step in plan.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    ]
    edits["pairs"] = [
        {"path": path, "message": finding["message"], **(stopped or {})}
        for path, finding, stopped in pairs
    ]
    for path, _finding, stopped in pairs:
        if path not in edits["candidates"]:
            owner = stopped["step_id"] if stopped else None
            edits["candidates"][path] = (
                [owner]
                if owner in step_ids and path_in_effective_scope(plan, owner, path)
                else [step_id for step_id in step_ids if path_in_effective_scope(plan, step_id, path)]
            )
    edits["permanent"] = bool(_terminal_attempt_violations(root)) or any(
        receipt_state == "unrecoverable"
        for _receipt, receipt_state, _by in timeout_receipt_states(root, plan)
    )
    if edits["permanent"]:
        return edits
    drift = audit_drift(root, plan)
    edits["drift"] = drift
    edits["scope_violation"] = bool(
        drift.get("out_of_scope_files") or drift.get("blocked_files_changed")
    )
    edits["offered"] = not edits["scope_violation"] and all(
        len(candidates) == 1 for candidates in edits["candidates"].values()
    )
    return edits


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


def incomplete_execution_errors(root: Path, plan: Dict[str, Any]) -> List[Dict[str, str]]:
    """#58: an open attempt or a never-completed step leaves the run unfinished.

    verify_run (both modes) and build_proof share this, so the two can never
    disagree about whether a run is finished.
    """
    steps = read_step_state(root)["steps"]
    errors: List[Dict[str, str]] = []
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            continue
        state = steps.get(step["id"], {})
        if state.get("open_attempts"):
            errors.append({
                "severity": "error",
                "message": f"{step['id']} has open attempt {', '.join(state['open_attempts'])}",
            })
        if not state.get("completed"):
            errors.append({"severity": "error", "message": f"{step['id']} is not completed"})
    return errors


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
    require_file_receipts = _requires_file_receipts(root, strict)
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
    result["errors"].extend(incomplete_execution_errors(root, plan))
    for step_id, step in coverage["steps"].items():
        for warning in step.get("warnings", []):
            # #58: an open attempt is reported once, as the error above.
            if warning.get("message") != f"{step_id} has open attempt":
                result["warnings"].append(warning)
        # #20: an unrecovered expired lease under enforce is a coverage error and
        # must gate the run (advisory downgrades it to a warning above).
        for error in step.get("errors", []):
            result["errors"].append(error)
        if step["status"] == "failed":
            result["errors"].append({"severity": "error", "message": f"{step_id} has failed attempt"})
        # Strict also needs the latest attempt completed. A never-completed step
        # already has its "is not completed" error from the helper.
        if (
            effective_strict
            and step["status"] != "completed"
            and step_id in coverage["completed_steps"]
        ):
            result["errors"].append({"severity": "error", "message": f"{step_id} is not completed"})
    result["errors"].extend(_terminal_attempt_violations(root))
    timeout_errors, timeout_warnings = _timeout_receipt_findings(root, plan)
    result["errors"].extend(timeout_errors)
    result["warnings"].extend(timeout_warnings)
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
