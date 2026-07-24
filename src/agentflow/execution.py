"""Execution contract and step-run state for Agentflow v0.3."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import append_jsonl, dumps_json, read_json, read_jsonl, utc_now
from .contracts import (
    ARTIFACT_COMPATIBILITY_POLICIES,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_LEASE_GRACE_SECONDS,
    DEFAULT_LEASE_TTL_MINUTES,
    EXECUTION_ARTIFACT_PATHS,
    EXECUTION_CONTRACT_SCHEMA_VERSION,
    HUNK_ATTRIBUTION_POLICIES,
    LEASE_POLICIES,
    REVIEW_GATE_POLICIES,
    RISK_POLICIES,
    STEP_RUNS_SCHEMA_VERSION,
)
from .git import is_git_repo
from .locks import file_lock
from .versioning import validate_schema_version_policy


ATTEMPT_OPENING_EVENTS = {"claimed", "amendment_started"}
OPEN_EVENTS = {*ATTEMPT_OPENING_EVENTS, "in_progress", "verified"}
TERMINAL_EVENTS = {"completed", "blocked", "failed", "abandoned"}
LEASE_EVENTS = {"lease_renewed"}
AMENDMENT_REASON_CODES = {
    "review_feedback",
    "validation_followup",
    "operator_correction",
    "other",
}


def _now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _utc_plus_minutes(minutes: int, now: Optional[datetime] = None) -> str:
    return (_now(now).replace(microsecond=0) + timedelta(minutes=minutes)).isoformat()


def _concurrency(root: Path) -> Dict[str, Any]:
    # A malformed/unreadable contract degrades to advisory defaults (the
    # backward-compat guarantee) instead of crashing lease readers; audit_drift
    # and verify_run already tolerate a malformed contract elsewhere.
    try:
        contract = load_execution_contract(root) or {}
    except (ValueError, OSError):
        return {}
    if not isinstance(contract, dict):
        return {}  # structurally-wrong contract (list/scalar) reads as advisory
    block = contract.get("concurrency", {})
    return block if isinstance(block, dict) else {}


def lease_policy(root: Path) -> str:
    value = _concurrency(root).get("lease_policy", "advisory")
    return value if value in LEASE_POLICIES else "advisory"


def lease_ttl_minutes(root: Path) -> int:
    value = _concurrency(root).get("lease_ttl_minutes", DEFAULT_LEASE_TTL_MINUTES)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_LEASE_TTL_MINUTES


def lease_grace_seconds(root: Path) -> int:
    value = _concurrency(root).get("lease_grace_seconds", DEFAULT_LEASE_GRACE_SECONDS)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return DEFAULT_LEASE_GRACE_SECONDS


def _parse_deadline(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def attempt_deadline(attempt: Dict[str, Any]) -> Optional[datetime]:
    return _parse_deadline(attempt.get("lease_expires_at"))


def attempt_is_expired(attempt: Dict[str, Any], now: datetime, grace_seconds: int) -> bool:
    deadline = attempt_deadline(attempt)
    if deadline is None:
        return False
    return now > deadline + timedelta(seconds=grace_seconds)


def default_execution_contract(root: str = ".") -> Dict[str, Any]:
    return {
        "schema_version": EXECUTION_CONTRACT_SCHEMA_VERSION,
        "contract_type": "agentflow_execution_contract",
        "root": root,
        "shell": {"default": "sh", "requires_posix": True},
        "agent_interface": {
            "minimum_capabilities": [
                "read_files",
                "edit_files",
                "run_shell",
                "report_exit_code",
            ],
            "forbidden_assumptions": [
                "codex_skills",
                "mcp_tools",
                "provider_function_calls",
            ],
        },
        "concurrency": {
            "writer_model": "single_writer",
            "reconcile_ignore": [".agent/", ".git/"],
            "lease_policy": "advisory",
            "lease_ttl_minutes": DEFAULT_LEASE_TTL_MINUTES,
            "lease_grace_seconds": DEFAULT_LEASE_GRACE_SECONDS,
        },
        "command_policy": {
            "record_outputs": True,
            "max_output_bytes": 200000,
            "capture_stderr": True,
            "receipt_store": "by_attempt",
            "risk_policy": "require-confirmation",
            "command_timeout_seconds": DEFAULT_COMMAND_TIMEOUT_SECONDS,
        },
        "proof_policy": {
            "strict_by_default": False,
            "require_command_receipts_for_validation": True,
            "require_file_receipts_for_changed_files": True,
            "require_managed_receipts_for_validation": False,
            "require_evidence_for_inspection_gates": True,
            "review_gate": "warn",
            "require_review_run": False,
            "hunk_attribution": "enforce",
        },
    }


def init_execution_artifacts(root: Path, force: bool = False) -> Tuple[List[str], List[str]]:
    files = {
        ".agent/execution.contract.json": dumps_json(default_execution_contract(".")),
        ".agent/step-runs.jsonl": "",
        ".agent/command-receipts.jsonl": "",
        ".agent/file-receipts.jsonl": "",
        ".agent/verification-runs.jsonl": "",
    }
    created: List[str] = []
    skipped: List[str] = []
    for relative_path, content in files.items():
        path = root / relative_path
        if path.exists() and not force:
            skipped.append(relative_path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(relative_path)
    (root / ".agent/handoffs").mkdir(parents=True, exist_ok=True)
    (root / ".agent/attempts").mkdir(parents=True, exist_ok=True)
    (root / ".agent/receipts").mkdir(parents=True, exist_ok=True)
    return created, skipped


def load_execution_contract(root: Path) -> Optional[Dict[str, Any]]:
    path = root / EXECUTION_ARTIFACT_PATHS["execution-contract"]
    if not path.exists():
        return None
    return read_json(path)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _non_empty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
    )


def _lease_minutes_or_default(value: Optional[int], default: Optional[int]) -> Optional[int]:
    if value is not None:
        if not _positive_int(value):
            raise ValueError("lease minutes must be a positive integer")
        return value
    return default


def validate_execution_contract(contract: Any) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []

    def error(message: str) -> None:
        findings.append({"severity": "error", "message": message})

    if not isinstance(contract, dict):
        error("execution contract must be an object")
        return findings

    required = (
        "schema_version",
        "contract_type",
        "root",
        "shell",
        "agent_interface",
        "concurrency",
        "command_policy",
        "proof_policy",
    )
    for field in required:
        if field not in contract:
            error(f"missing required field: {field}")

    if "schema_version" in contract:
        for message in validate_schema_version_policy(
            contract["schema_version"],
            EXECUTION_CONTRACT_SCHEMA_VERSION,
            "execution-contract",
            ARTIFACT_COMPATIBILITY_POLICIES["execution-contract"],
        ):
            error(message)
    if (
        "contract_type" in contract
        and contract["contract_type"] != "agentflow_execution_contract"
    ):
        error("contract_type must be agentflow_execution_contract")
    if "root" in contract and not isinstance(contract["root"], str):
        error("root must be a string")

    shell = contract.get("shell")
    if "shell" in contract and not isinstance(shell, dict):
        error("shell must be an object")
    elif isinstance(shell, dict):
        for field in ("default", "requires_posix"):
            if field not in shell:
                error(f"shell missing required field: {field}")
        if "default" in shell and (
            not isinstance(shell["default"], str) or not shell["default"]
        ):
            error("shell.default must be a non-empty string")
        if "requires_posix" in shell and not isinstance(shell["requires_posix"], bool):
            error("shell.requires_posix must be boolean")

    agent_interface = contract.get("agent_interface")
    if "agent_interface" in contract and not isinstance(agent_interface, dict):
        error("agent_interface must be an object")
    elif isinstance(agent_interface, dict):
        for field in ("minimum_capabilities", "forbidden_assumptions"):
            if field not in agent_interface:
                error(f"agent_interface missing required field: {field}")
            elif not _non_empty_string_list(agent_interface[field]):
                error(
                    f"agent_interface.{field} must contain only non-empty strings"
                )

    concurrency = contract.get("concurrency")
    if "concurrency" in contract and not isinstance(concurrency, dict):
        error("concurrency must be an object")
    elif isinstance(concurrency, dict):
        for field in ("writer_model", "reconcile_ignore"):
            if field not in concurrency:
                error(f"concurrency missing required field: {field}")
        if "writer_model" in concurrency and concurrency["writer_model"] != "single_writer":
            error(
                f"writer_model {concurrency['writer_model']} is not supported in v0.3"
            )
        if "reconcile_ignore" in concurrency and not _non_empty_string_list(
            concurrency["reconcile_ignore"]
        ):
            error("concurrency.reconcile_ignore must contain only non-empty strings")
        if "lease_policy" in concurrency and concurrency["lease_policy"] not in LEASE_POLICIES:
            error(f"lease_policy {concurrency['lease_policy']} is invalid")
        if "lease_ttl_minutes" in concurrency and not _positive_int(concurrency["lease_ttl_minutes"]):
            error("lease_ttl_minutes must be a positive integer")
        if "lease_grace_seconds" in concurrency and not _non_negative_int(
            concurrency["lease_grace_seconds"]
        ):
            error("lease_grace_seconds must be a non-negative integer")

    command_policy = contract.get("command_policy")
    if "command_policy" in contract and not isinstance(command_policy, dict):
        error("command_policy must be an object")
    elif isinstance(command_policy, dict):
        for field in (
            "record_outputs",
            "max_output_bytes",
            "capture_stderr",
            "receipt_store",
        ):
            if field not in command_policy:
                error(f"command_policy missing required field: {field}")
        for field in ("record_outputs", "capture_stderr"):
            if field in command_policy and not isinstance(command_policy[field], bool):
                error(f"command_policy.{field} must be boolean")
        if "max_output_bytes" in command_policy and not _non_negative_int(
            command_policy["max_output_bytes"]
        ):
            error("command_policy.max_output_bytes must be a non-negative integer")
        if (
            "receipt_store" in command_policy
            and command_policy["receipt_store"]
            not in ("by_attempt", "content_addressed")
        ):
            error(f"receipt_store {command_policy['receipt_store']} is invalid")
        if "command_timeout_seconds" in command_policy and not _positive_int(
            command_policy["command_timeout_seconds"]
        ):
            error("command_timeout_seconds must be a positive integer")
        if (
            "risk_policy" in command_policy
            and command_policy["risk_policy"] not in RISK_POLICIES
        ):
            error(f"risk_policy {command_policy['risk_policy']} is invalid")

    proof_policy = contract.get("proof_policy")
    if "proof_policy" in contract and not isinstance(proof_policy, dict):
        error("proof_policy must be an object")
    elif isinstance(proof_policy, dict):
        boolean_fields = (
            "strict_by_default",
            "require_command_receipts_for_validation",
            "require_file_receipts_for_changed_files",
            "require_managed_receipts_for_validation",
            "require_evidence_for_inspection_gates",
        )
        for field in boolean_fields:
            if field not in proof_policy:
                error(f"proof_policy missing required field: {field}")
            elif not isinstance(proof_policy[field], bool):
                error(f"proof_policy.{field} must be boolean")
        if "review_gate" in proof_policy and proof_policy["review_gate"] not in REVIEW_GATE_POLICIES:
            error(f"review_gate {proof_policy['review_gate']} is invalid")
        if "require_review_run" in proof_policy and not isinstance(
            proof_policy["require_review_run"], bool
        ):
            error("proof_policy.require_review_run must be boolean")
        if (
            "hunk_attribution" in proof_policy
            and proof_policy["hunk_attribution"] not in HUNK_ATTRIBUTION_POLICIES
        ):
            error(
                f"hunk_attribution {proof_policy['hunk_attribution']} is invalid"
            )
    return findings


def doctor(root: Path) -> Dict[str, Any]:
    findings: List[Dict[str, str]] = []
    # An incompatible or unreadable contract is exactly what doctor exists to
    # diagnose: report it as a finding instead of letting read_json's version
    # gate escape as a traceback (same degrade rule as _concurrency above).
    try:
        contract = load_execution_contract(root)
    except (ValueError, OSError) as exc:
        findings.append({"severity": "error", "message": str(exc)})
        return {"status": "failed", "contract": None, "findings": findings}
    if contract is None:
        findings.append(
            {"severity": "error", "message": ".agent/execution.contract.json is missing"}
        )
        return {"status": "failed", "contract": None, "findings": findings}
    findings.extend(validate_execution_contract(contract))
    if any(finding["severity"] == "error" for finding in findings):
        return {"status": "failed", "contract": contract, "findings": findings}
    if contract.get("shell", {}).get("requires_posix") and shutil.which("sh") is None:
        findings.append({"severity": "error", "message": "required POSIX sh is not available"})
    if shutil.which("git") is None:
        findings.append({"severity": "error", "message": "required git command is not available"})
    if not (root / ".agent").exists():
        findings.append({"severity": "error", "message": ".agent directory is missing"})
    elif not (root / ".agent").is_dir():
        findings.append({"severity": "error", "message": ".agent exists but is not a directory"})
    elif not os.access(root / ".agent", os.W_OK):
        findings.append({"severity": "error", "message": ".agent directory is not writable"})
    if not is_git_repo(root):
        findings.append({"severity": "warning", "message": "root is not a git repository"})
    status = "failed" if any(finding["severity"] == "error" for finding in findings) else "passed"
    return {"status": status, "contract": contract, "findings": findings}


def _step_runs_path(root: Path) -> Path:
    return root / EXECUTION_ARTIFACT_PATHS["step-runs"]


def _step_runs_lock_path(root: Path) -> Path:
    # `.lockfile` (not `.lock`) so audit_drift does not treat it as a dependency
    # lockfile — mirrors receipts._ledger_lock_path.
    return _step_runs_path(root).parent / "step-runs.jsonl.lockfile"


def _attempt_pointer(root: Path, step_id: str) -> Path:
    return root / ".agent/attempts" / f"{step_id}.current"


def _next_attempt_id(events: List[Dict[str, Any]]) -> str:
    opens = [
        event
        for event in events
        if event.get("event") in ATTEMPT_OPENING_EVENTS
    ]
    return f"A{len(opens) + 1}"


def _append_step_event(root: Path, event: Dict[str, Any]) -> Dict[str, Any]:
    event = {"schema_version": STEP_RUNS_SCHEMA_VERSION, "recorded_at": utc_now(), **event}
    append_jsonl(_step_runs_path(root), event)
    return event


def read_step_events(root: Path) -> List[Dict[str, Any]]:
    return read_jsonl(_step_runs_path(root))


def read_step_state(root: Path) -> Dict[str, Any]:
    state: Dict[str, Any] = {"steps": {}, "attempts": {}}
    for event in read_step_events(root):
        step_id = event.get("step_id")
        attempt_id = event.get("attempt_id")
        event_kind = event.get("event")
        if not isinstance(step_id, str) or not isinstance(attempt_id, str):
            continue
        attempt = state["attempts"].setdefault(
            attempt_id,
            {"step_id": step_id, "status": "pending", "open": False, "events": []},
        )
        attempt.setdefault("agent_id", None)
        attempt.setdefault("lease_expires_at", None)
        attempt["events"].append(event)
        if event_kind in ATTEMPT_OPENING_EVENTS:
            attempt["agent_id"] = event.get("agent_id")
        if event.get("lease_expires_at") is not None:
            attempt["lease_expires_at"] = event.get("lease_expires_at")
        if event_kind in LEASE_EVENTS:
            continue  # metadata only: never changes status/open/step aggregates
        attempt["status"] = event_kind
        attempt["open"] = event_kind in OPEN_EVENTS
        step = state["steps"].setdefault(
            step_id,
            {
                "status": "pending",
                "completed": False,
                "open_attempts": [],
                "attempts": [],
            },
        )
        if attempt_id not in step["attempts"]:
            step["attempts"].append(attempt_id)
        step["status"] = event_kind
        step["completed"] = event_kind == "completed" or step["completed"]
    for attempt_id, attempt in state["attempts"].items():
        step = state["steps"][attempt["step_id"]]
        if attempt["open"] and attempt_id not in step["open_attempts"]:
            step["open_attempts"].append(attempt_id)
    return state


def _find_step(plan: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    for step in plan.get("steps", []):
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise ValueError(f"unknown step id: {step_id}")


def next_step(root: Path, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    state = read_step_state(root)
    steps = plan.get("steps", [])
    completed = {
        step_id
        for step_id, item in state["steps"].items()
        if item.get("completed")
    }
    if any(isinstance(step, dict) and step.get("depends_on") for step in steps):
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            if not isinstance(step_id, str):
                continue
            current = state["steps"].get(step_id, {})
            if current.get("completed") or current.get("open_attempts"):
                continue
            dependencies = step.get("depends_on", [])
            if all(dependency in completed for dependency in dependencies):
                return step
        return None
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        current = state["steps"].get(step_id, {})
        if not current.get("completed") and not current.get("open_attempts"):
            return step
    return None


def current_step_attempt(root: Path, step_id: str) -> Optional[str]:
    pointer = _attempt_pointer(root, step_id)
    if pointer.exists():
        value = pointer.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def resolve_attempt(root: Path, step_id: str, attempt_id: Optional[str]) -> str:
    if attempt_id:
        return attempt_id
    pointer_attempt = current_step_attempt(root, step_id)
    if pointer_attempt:
        return pointer_attempt
    state = read_step_state(root)
    open_attempts = state["steps"].get(step_id, {}).get("open_attempts", [])
    if len(open_attempts) == 1:
        return open_attempts[0]
    if len(open_attempts) > 1:
        raise ValueError(f"multiple open attempts for {step_id}; pass --attempt")
    raise ValueError(f"{step_id} has no open attempt; run claim-step first")


def claim_step(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    agent_id: str,
    lease_minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    _find_step(plan, step_id)
    enforce = lease_policy(root) == "enforce"
    with file_lock(_step_runs_lock_path(root)):
        state = read_step_state(root)
        if state["steps"].get(step_id, {}).get("completed"):
            raise ValueError(f"step {step_id} has completed work; use amend-step")
        if enforce:
            open_ids = state["steps"].get(step_id, {}).get("open_attempts", [])
            if open_ids:
                existing = state["attempts"][open_ids[0]]
                deadline = existing.get("lease_expires_at")
                if deadline is None:
                    raise ValueError(
                        f"step {step_id} has an open no-deadline attempt {open_ids[0]} "
                        f"owned by {existing.get('agent_id')}; owner must renew-lease "
                        "or run fail-step"
                    )
                if not attempt_is_expired(existing, _now(now), lease_grace_seconds(root)):
                    raise ValueError(
                        f"step {step_id} is leased to {existing.get('agent_id')} "
                        f"until {deadline}"
                    )
                raise ValueError(
                    f"step {step_id} attempt {open_ids[0]} lease expired at {deadline}; "
                    f"recover with: agentflow reclaim-step {step_id} "
                    "--agent <you> --reason <text>"
                )
        events = read_step_events(root)
        attempt_id = _next_attempt_id(events)
        minutes = _lease_minutes_or_default(
            lease_minutes, lease_ttl_minutes(root) if enforce else None
        )
        event = {
            "event": "claimed",
            "step_id": step_id,
            "attempt_id": attempt_id,
            "agent_id": agent_id,
            "lease_expires_at": _utc_plus_minutes(minutes, now) if minutes is not None else None,
        }
        recorded = _append_step_event(root, event)
        pointer = _attempt_pointer(root, step_id)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(f"{attempt_id}\n", encoding="utf-8")
    return recorded


def reclaim_step(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    agent_id: str,
    reason: str,
    lease_minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Abandon an expired attempt and open a fresh claim atomically."""
    _find_step(plan, step_id)
    if not agent_id or not agent_id.strip():
        raise ValueError("reclaim-step requires --agent")
    if not reason or not reason.strip():
        raise ValueError("reclaim-step requires --reason")
    with file_lock(_step_runs_lock_path(root)):
        state = read_step_state(root)
        open_ids = state["steps"].get(step_id, {}).get("open_attempts", [])
        if not open_ids:
            raise ValueError(f"step {step_id} has no open attempt; use claim-step")
        if len(open_ids) > 1:
            raise ValueError(
                f"step {step_id} has multiple open attempts; "
                "pass --attempt to fail-step first"
            )
        old_id = open_ids[0]
        old = state["attempts"][old_id]
        if old.get("lease_expires_at") is None:
            raise ValueError(
                f"attempt {old_id} has no deadline; owner may renew-lease or run fail-step"
            )
        if not attempt_is_expired(old, _now(now), lease_grace_seconds(root)):
            raise ValueError(
                f"attempt {old_id} still leased to {old.get('agent_id')} "
                f"until {old.get('lease_expires_at')}; wait or coordinate"
            )
        minutes = _lease_minutes_or_default(lease_minutes, lease_ttl_minutes(root))
        new_id = _next_attempt_id(read_step_events(root))
        _append_step_event(root, {
            "event": "abandoned", "step_id": step_id, "attempt_id": old_id,
            "abandoned_by": agent_id, "reason": reason, "superseded_by": new_id,
        })
        recorded = _append_step_event(root, {
            "event": "claimed", "step_id": step_id, "attempt_id": new_id,
            "agent_id": agent_id, "lease_expires_at": _utc_plus_minutes(minutes, now),
        })
        pointer = _attempt_pointer(root, step_id)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(f"{new_id}\n", encoding="utf-8")
    return recorded


def _close_step(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    event_kind: str,
    reason: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with file_lock(_step_runs_lock_path(root)):
        resolved_attempt = resolve_attempt(root, step_id, attempt_id)
        event: Dict[str, Any] = {
            "event": event_kind,
            "step_id": step_id,
            "attempt_id": resolved_attempt,
        }
        if reason:
            event["reason"] = reason
        if extra:
            event.update(extra)
        recorded = _append_step_event(root, event)
        pointer = _attempt_pointer(root, step_id)
        if pointer.exists():
            pointer.unlink()
    return recorded


def latest_attempt_event(root: Path, step_id: str, attempt_id: str) -> Optional[Dict[str, Any]]:
    latest = None
    for event in read_step_events(root):
        if event.get("step_id") == step_id and event.get("attempt_id") == attempt_id:
            latest = event
    return latest


def attempt_is_verified(root: Path, step_id: str, attempt_id: str) -> bool:
    latest = latest_attempt_event(root, step_id, attempt_id)
    return bool(latest and latest.get("event") == "verified")


def latest_completed_attempt(
    root: Path, step_id: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return (attempt_id, completed_event) for the step's latest completion."""
    result: Optional[Tuple[str, Dict[str, Any]]] = None
    for event in read_step_events(root):
        if (
            event.get("step_id") == step_id
            and event.get("event") == "completed"
            and isinstance(event.get("attempt_id"), str)
        ):
            result = (event["attempt_id"], event)
    return result


def attempt_for_step(
    root: Path, step_id: str, attempt_id: str
) -> Optional[Dict[str, Any]]:
    """Return the projected attempt only if it exists and belongs to step_id."""
    attempt = read_step_state(root)["attempts"].get(attempt_id)
    if attempt is None or attempt.get("step_id") != step_id:
        return None
    return attempt


def attempt_is_terminal(root: Path, step_id: str, attempt_id: str) -> bool:
    latest = latest_attempt_event(root, step_id, attempt_id)
    return bool(latest and latest.get("event") in TERMINAL_EVENTS)


def attempt_has_opener(root: Path, step_id: str, attempt_id: str) -> bool:
    attempt = attempt_for_step(root, step_id, attempt_id)
    if attempt is None:
        return False
    return any(
        event.get("event") in ATTEMPT_OPENING_EVENTS
        for event in attempt.get("events", [])
        if isinstance(event, dict)
    )


def _require_opened_attempt(root: Path, step_id: str, attempt_id: str) -> None:
    if not attempt_has_opener(root, step_id, attempt_id):
        raise ValueError(
            f"attempt {attempt_id} for step {step_id} was never opened with "
            "claim-step or amend-step"
        )


def complete_step(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    agent_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    resolved_attempt = resolve_attempt(root, step_id, attempt_id)
    _require_opened_attempt(root, step_id, resolved_attempt)
    if not attempt_is_verified(root, step_id, resolved_attempt):
        raise RuntimeError("verify-step must pass before complete-step")
    _require_owner(root, step_id, resolved_attempt, agent_id, now, action="complete")
    return _close_step(root, step_id, resolved_attempt, "completed")


def block_step(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    reason: str,
    agent_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    resolved_attempt = resolve_attempt(root, step_id, attempt_id)
    _require_opened_attempt(root, step_id, resolved_attempt)
    _require_owner(root, step_id, resolved_attempt, agent_id, now, action="block")
    return _close_step(root, step_id, resolved_attempt, "blocked", reason)


def fail_step(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    reason: str,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    # Break-glass path: no owner enforcement. Records who forced the failure.
    extra = {"failed_by": agent_id} if agent_id else None
    return _close_step(root, step_id, attempt_id, "failed", reason, extra=extra)


def mark_step_verified(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    findings: List[Dict[str, Any]],
    agent_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    with file_lock(_step_runs_lock_path(root)):
        resolved_attempt = resolve_attempt(root, step_id, attempt_id)
        _require_opened_attempt(root, step_id, resolved_attempt)
        _require_owner(root, step_id, resolved_attempt, agent_id, now, action="verify")
        return _append_step_event(
            root,
            {
                "event": "verified",
                "step_id": step_id,
                "attempt_id": resolved_attempt,
                "findings": findings,
            },
        )


def amend_step(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    agent_id: str,
    reason: str,
    reason_code: Optional[str] = None,
    finding_refs: Optional[List[Dict[str, str]]] = None,
    lease_minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Open an auditable amendment attempt on a completed step.

    The amendment is a new attempt linked to the latest completed attempt; the
    prior attempt is never mutated.
    """
    _find_step(plan, step_id)
    with file_lock(_step_runs_lock_path(root)):
        completed = latest_completed_attempt(root, step_id)
        if completed is None:
            raise ValueError(f"step {step_id} has no completed attempt; use claim-step")
        if read_step_state(root)["steps"].get(step_id, {}).get("open_attempts"):
            raise ValueError(f"step {step_id} has an open attempt; finish it first")
        if not reason or not reason.strip():
            raise ValueError("amend-step requires a non-empty --reason")
        if not agent_id or not agent_id.strip():
            raise ValueError("amend-step requires --agent")
        if reason_code is not None and reason_code not in AMENDMENT_REASON_CODES:
            raise ValueError(
                f"invalid reason_code {reason_code}; expected one of "
                f"{', '.join(sorted(AMENDMENT_REASON_CODES))}"
            )
        amends_attempt, completed_event = completed
        attempt_id = _next_attempt_id(read_step_events(root))
        event: Dict[str, Any] = {
            "event": "amendment_started",
            "step_id": step_id,
            "attempt_id": attempt_id,
            "amends_attempt": amends_attempt,
            "amends_completed_at": completed_event.get("recorded_at"),
            "agent_id": agent_id,
            "reason": reason,
        }
        if reason_code is not None:
            event["reason_code"] = reason_code
        if finding_refs:
            event["finding_refs"] = finding_refs
        minutes = _lease_minutes_or_default(
            lease_minutes, lease_ttl_minutes(root) if lease_policy(root) == "enforce" else None
        )
        if minutes is not None:
            event["lease_expires_at"] = _utc_plus_minutes(minutes, now)
        recorded = _append_step_event(root, event)
        pointer = _attempt_pointer(root, step_id)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(f"{attempt_id}\n", encoding="utf-8")
    return recorded


def _require_owner(
    root: Path,
    step_id: str,
    attempt_id: str,
    agent_id: Optional[str],
    now: Optional[datetime],
    *,
    action: str,
    allow_expired: bool = False,
) -> None:
    """Enforce-only owner + expiry gate on an attempt.

    Under ``lease_policy != "enforce"`` this is a no-op (advisory behavior is
    unchanged). Under enforce the caller's ``agent_id`` must match the attempt
    opener, and a finite lease must not have expired -- unless ``allow_expired``
    (the ``renew-lease`` self-recovery path, where the owner may extend an
    already-expired finite lease). A foreign agent is always rejected.
    """
    if lease_policy(root) != "enforce":
        return
    attempt = read_step_state(root)["attempts"].get(attempt_id)
    if attempt is None:
        return  # existence handled by callers
    if not agent_id:
        raise ValueError(
            f"{action} on {step_id}/{attempt_id} requires --agent or "
            "AGENTFLOW_AGENT_ID under enforce"
        )
    owner = attempt.get("agent_id")
    if owner is not None and agent_id != owner:
        raise ValueError(
            f"{action} rejected: {attempt_id} is owned by {owner}, not {agent_id}"
        )
    if owner is None:
        raise ValueError(
            f"{action} rejected: {attempt_id} has no owner; use fail-step "
            "or repair the attempt ledger"
        )
    if not allow_expired and attempt_is_expired(attempt, _now(now), lease_grace_seconds(root)):
        raise ValueError(
            f"{action} rejected: {attempt_id} lease expired at "
            f"{attempt.get('lease_expires_at')}; renew-lease or reclaim-step"
        )


def require_lifecycle_owner(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    agent_id: Optional[str],
    now: Optional[datetime] = None,
    *,
    action: str,
) -> str:
    with file_lock(_step_runs_lock_path(root)):
        resolved = resolve_attempt(root, step_id, attempt_id)
        _require_opened_attempt(root, step_id, resolved)
        _require_owner(root, step_id, resolved, agent_id, now, action=action)
        return resolved


def renew_lease(
    root: Path,
    step_id: str,
    attempt_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Extend an attempt's lease by appending a metadata-only lease_renewed row.

    Owner self-recovery: the owning agent may renew even an expired finite
    lease (``allow_expired=True``); foreign recovery still goes through
    reclaim-step. Prior rows are never mutated -- projection surfaces the
    newest deadline.
    """
    with file_lock(_step_runs_lock_path(root)):
        resolved = resolve_attempt(root, step_id, attempt_id)
        _require_opened_attempt(root, step_id, resolved)
        _require_owner(root, step_id, resolved, agent_id, now, action="renew", allow_expired=True)
        resolved_minutes = _lease_minutes_or_default(minutes, lease_ttl_minutes(root))
        recorded = _append_step_event(
            root,
            {
                "event": "lease_renewed",
                "step_id": step_id,
                "attempt_id": resolved,
                "agent_id": agent_id,
                "lease_expires_at": _utc_plus_minutes(resolved_minutes, now),
            },
        )
    return recorded


def require_writable_attempt(
    root: Path,
    step_id: str,
    attempt_id: str,
    *,
    new_work: bool,
    agent_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Validate a receipt's target attempt before it is written.

    All receipts must name an existing attempt for the same step. New-work
    receipts additionally require the attempt to be open (not terminal), so
    post-completion edits must go through amend-step. Under lease_policy=enforce
    a new-work write also requires the caller to be the (non-expired) owner.
    """
    state = read_step_state(root)
    attempt = state["attempts"].get(attempt_id)
    if attempt is not None and attempt.get("step_id") != step_id:
        attempt = None
    if attempt is None:
        raise ValueError(f"attempt {attempt_id} does not belong to step {step_id}")
    if not new_work:
        return
    if not attempt_has_opener(root, step_id, attempt_id):
        raise ValueError(
            f"attempt {attempt_id} for step {step_id} was never opened with "
            "claim-step or amend-step"
        )
    if not attempt.get("open"):
        guidance = (
            "open an amendment with amend-step"
            if state["steps"].get(step_id, {}).get("completed")
            else "retry with claim-step"
        )
        raise ValueError(
            f"attempt {attempt_id} for step {step_id} is not open; {guidance}"
        )
    _require_owner(root, step_id, attempt_id, agent_id, now, action="write")
