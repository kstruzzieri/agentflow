"""Command and file receipt helpers for Agentflow v0.3."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import append_jsonl, read_jsonl, utc_now
from .contracts import (
    COMMAND_RECEIPTS_SCHEMA_VERSION,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DIFF_COMMAND_VERSION,
    EXECUTION_ARTIFACT_PATHS,
    FILE_RECEIPTS_SCHEMA_VERSION,
)
from .hunks import compute_hunks, effective_hunk_policy
from .execution import (
    attempt_deadline,
    lease_grace_seconds,
    lease_policy,
    load_execution_contract,
    read_step_state,
    renew_lease,
    require_writable_attempt,
    resolve_attempt,
)
from .git import changed_file_records, git_blob_for_head
from .locks import file_lock
from .risk import classify_command, evaluate_policy
from .validation import path_in_effective_scope


_NO_TIMEOUT_OVERRIDE = object()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_receipts_path(root: Path) -> Path:
    return root / EXECUTION_ARTIFACT_PATHS["command-receipts"]


def _file_receipts_path(root: Path) -> Path:
    return root / EXECUTION_ARTIFACT_PATHS["file-receipts"]


def command_receipts(root: Path) -> List[Dict[str, Any]]:
    return read_jsonl(_command_receipts_path(root))


def file_receipts(root: Path) -> List[Dict[str, Any]]:
    return read_jsonl(_file_receipts_path(root))


def _next_command_receipt_id(root: Path) -> str:
    return f"CR{len(command_receipts(root)) + 1}"


def _next_file_receipt_id(root: Path) -> str:
    return f"FR{len(file_receipts(root)) + 1}"


def _ledger_lock_path(ledger_path: Path) -> Path:
    """Sidecar lock file guarding atomic id allocation for one ledger.

    The ``.lockfile`` suffix (not ``.lock``) is deliberate: ``audit_drift``
    classifies any changed path ending in ``.lock`` as a dependency lockfile,
    so a ``.lock`` sidecar would emit a false dependency-drift note on every
    record-command/run in projects that track ``.agent/``.
    """
    return ledger_path.parent / f"{ledger_path.name}.lockfile"


def _contract_policy(root: Path) -> Dict[str, Any]:
    contract = load_execution_contract(root) or {}
    policy = contract.get("command_policy", {})
    return policy if isinstance(policy, dict) else {}


def _receipt_output_paths(root: Path, attempt_id: str, receipt_id: str) -> Tuple[Path, Path]:
    base = root / ".agent/receipts" / attempt_id
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{receipt_id}.stdout.txt", base / f"{receipt_id}.stderr.txt"


def _truncate(data: bytes, max_bytes: int) -> Tuple[bytes, bool]:
    if max_bytes and len(data) > max_bytes:
        return data[:max_bytes], True
    return data, False


def _relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


def _write_output(
    root: Path,
    attempt_id: str,
    receipt_id: str,
    stream: str,
    data: bytes,
) -> Tuple[str, str]:
    digest = _sha256_bytes(data)
    policy = _contract_policy(root)
    if policy.get("receipt_store") == "content_addressed":
        path = root / ".agent/receipts/sha256" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return _relative(root, path), digest
    stdout_path, stderr_path = _receipt_output_paths(root, attempt_id, receipt_id)
    path = stdout_path if stream == "stdout" else stderr_path
    path.write_bytes(data)
    return _relative(root, path), digest


def _positive_timeout(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _command_gate_key(gate: Dict[str, Any]) -> str:
    return " ".join(gate["run"])


def _matching_gate_timeout(
    plan: Dict[str, Any],
    step_id: str,
    command: List[str],
    gate: Optional[str],
) -> object:
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or step.get("id") != step_id:
            continue
        validation = [
            item
            for item in step.get("validation", [])
            if isinstance(item, str)
        ]
        gates = step.get("gates", [])
        if not isinstance(gates, list):
            return None

        command_gates = []
        for index, candidate in enumerate(gates):
            if not isinstance(candidate, dict) or candidate.get("kind") != "command":
                continue
            run = candidate.get("run")
            if not isinstance(run, list) or not all(isinstance(item, str) for item in run):
                continue
            aliases = [_command_gate_key(candidate)]
            if index < len(validation):
                aliases.append(validation[index])
            command_gates.append((candidate, run, aliases))

        for candidate, run, _aliases in command_gates:
            if run == command:
                timeout = candidate.get("timeout_seconds")
                if timeout is not None:
                    if not _positive_timeout(timeout):
                        raise ValueError("timeout_seconds must be a positive integer")
                    return int(timeout)
                return _NO_TIMEOUT_OVERRIDE

        if gate is not None:
            for candidate, _run, aliases in command_gates:
                if gate in aliases:
                    timeout = candidate.get("timeout_seconds")
                    if timeout is not None:
                        if not _positive_timeout(timeout):
                            raise ValueError("timeout_seconds must be a positive integer")
                        return int(timeout)
        return None
    return None


def resolve_command_timeout_seconds(
    plan: Dict[str, Any],
    step_id: str,
    command: List[str],
    gate: Optional[str],
    policy: Dict[str, Any],
) -> int:
    if not isinstance(policy, dict):
        policy = {}
    value = policy.get("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)
    if not _positive_timeout(value):
        raise ValueError("command_timeout_seconds must be a positive integer")
    override = _matching_gate_timeout(plan, step_id, command, gate)
    if override is _NO_TIMEOUT_OVERRIDE:
        return int(value)
    if override is not None:
        return int(override)
    return int(value)


def _coerce_output_bytes(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return bytes(value)


def _receipt_output_fields(
    root: Path,
    attempt_id: str,
    receipt_id: str,
    stdout_data: Optional[bytes],
    stderr_data: Optional[bytes],
    *,
    record_outputs: bool,
    capture_stderr: bool,
    max_bytes: int,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "stdout_path": None,
        "stderr_path": None,
        "stdout_sha256": None,
        "stderr_sha256": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "truncated": False,
    }
    if record_outputs and stdout_data is not None:
        stdout, stdout_truncated = _truncate(stdout_data, max_bytes)
        fields["stdout_path"], fields["stdout_sha256"] = _write_output(
            root, attempt_id, receipt_id, "stdout", stdout
        )
        fields["stdout_truncated"] = stdout_truncated
    if record_outputs and capture_stderr and stderr_data is not None:
        stderr, stderr_truncated = _truncate(stderr_data, max_bytes)
        fields["stderr_path"], fields["stderr_sha256"] = _write_output(
            root, attempt_id, receipt_id, "stderr", stderr
        )
        fields["stderr_truncated"] = stderr_truncated
    fields["truncated"] = bool(fields["stdout_truncated"] or fields["stderr_truncated"])
    return fields


def _apply_confirmation_fields(
    receipt: Dict[str, Any],
    risk_policy: Optional[str],
    classification: Dict[str, Any],
    confirmed: bool,
    confirmation_source: Optional[str],
) -> None:
    if (
        risk_policy == "require-confirmation"
        and classification["level"] == "high"
        and confirmed
    ):
        receipt["risk_policy"] = "require-confirmation"
        receipt["confirmed"] = True
        receipt["confirmation_source"] = confirmation_source or "cli"


def run_command(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    attempt_id: Optional[str],
    command: List[str],
    gate: Optional[str] = None,
    confirmed: bool = False,
    confirmation_source: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_attempt = resolve_attempt(root, step_id, attempt_id)
    require_writable_attempt(
        root, step_id, resolved_attempt, new_work=True, agent_id=agent_id
    )
    policy = _contract_policy(root)
    risk_policy = policy.get("risk_policy")
    classification = classify_command(command, plan, step_id, root=root)
    decision = evaluate_policy(classification["level"], risk_policy, confirmed)
    receipts_path = _command_receipts_path(root)

    if decision == "block":
        now = utc_now()
        with file_lock(_ledger_lock_path(receipts_path)):
            receipt = {
                "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
                "id": _next_command_receipt_id(root),
                "step_id": step_id,
                "attempt_id": resolved_attempt,
                "provenance": "observed",
                "command": command,
                "cwd": ".",
                "env_names": [],
                "started_at": now,
                "finished_at": now,
                "exit_code": None,
                "stdout_path": None,
                "stderr_path": None,
                "stdout_sha256": None,
                "stderr_sha256": None,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "truncated": False,
                "risk": classification,
                "decision": "blocked",
                "timed_out": False,
                "risk_policy": risk_policy,
            }
            append_jsonl(receipts_path, receipt)
        return receipt

    max_bytes = int(policy.get("max_output_bytes", 200000))
    record_outputs = bool(policy.get("record_outputs", True))
    capture_stderr = bool(policy.get("capture_stderr", True))
    timeout_seconds = resolve_command_timeout_seconds(plan, step_id, command, gate, policy)
    if lease_policy(root) == "enforce":
        attempt = read_step_state(root)["attempts"].get(resolved_attempt, {})
        deadline = attempt_deadline(attempt)
        grace = lease_grace_seconds(root)
        need = timedelta(seconds=timeout_seconds + grace)
        if deadline is not None and deadline - datetime.now(timezone.utc) < need:
            renew_minutes = max(1, (timeout_seconds + 2 * grace + 59) // 60)
            renew_lease(root, step_id, resolved_attempt, agent_id, minutes=renew_minutes)
    started_at = utc_now()
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE if record_outputs else subprocess.DEVNULL,
            stderr=subprocess.PIPE if record_outputs and capture_stderr else subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
        exit_code = proc.returncode
        stdout_data = proc.stdout
        stderr_data = proc.stderr
        timed_out = False
        receipt_decision = "allowed"
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        partial_stdout = getattr(exc, "stdout", None)
        if partial_stdout is None:
            partial_stdout = exc.output
        stdout_data = _coerce_output_bytes(partial_stdout)
        stderr_data = _coerce_output_bytes(exc.stderr)
        timed_out = True
        receipt_decision = "timeout"
    finished_at = utc_now()
    with file_lock(_ledger_lock_path(receipts_path)):
        receipt_id = _next_command_receipt_id(root)
        output_fields = _receipt_output_fields(
            root,
            resolved_attempt,
            receipt_id,
            _coerce_output_bytes(stdout_data),
            _coerce_output_bytes(stderr_data),
            record_outputs=record_outputs,
            capture_stderr=capture_stderr,
            max_bytes=max_bytes,
        )
        receipt = {
            "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
            "id": receipt_id,
            "step_id": step_id,
            "attempt_id": resolved_attempt,
            "provenance": "observed",
            "command": command,
            "cwd": ".",
            "env_names": [],
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            **output_fields,
            "risk": classification,
            "decision": receipt_decision,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
        }
        _apply_confirmation_fields(
            receipt,
            risk_policy,
            classification,
            confirmed,
            confirmation_source,
        )
        if gate:
            receipt["gate"] = gate
        append_jsonl(receipts_path, receipt)
    return receipt


def record_command(
    root: Path,
    step_id: str,
    attempt_id: Optional[str],
    command: List[str],
    exit_code: int,
    gate: Optional[str] = None,
    provenance: str = "attested",
    plan: Optional[Dict[str, Any]] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_attempt = resolve_attempt(root, step_id, attempt_id)
    require_writable_attempt(
        root, step_id, resolved_attempt,
        new_work=provenance == "attested", agent_id=agent_id,
    )
    receipts_path = _command_receipts_path(root)
    with file_lock(_ledger_lock_path(receipts_path)):
        receipt = {
            "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
            "id": _next_command_receipt_id(root),
            "step_id": step_id,
            "attempt_id": resolved_attempt,
            "provenance": provenance,
            "command": command,
            "cwd": ".",
            "env_names": [],
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "exit_code": exit_code,
            "stdout_path": None,
            "stderr_path": None,
            "stdout_sha256": None,
            "stderr_sha256": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "truncated": False,
        }
        if plan is not None:
            receipt["risk"] = classify_command(command, plan, step_id, root=root)
            receipt["decision"] = "allowed"
        if gate:
            receipt["gate"] = gate
        append_jsonl(receipts_path, receipt)
    return receipt


def _change_record_for_path(root: Path, path: str) -> Dict[str, str]:
    for record in changed_file_records(root):
        if record["path"] == path:
            return record
    if (root / path).exists():
        return {"path": path, "previous_path": "", "status": "", "change_kind": "modified"}
    return {"path": path, "previous_path": "", "status": "", "change_kind": "deleted"}


def record_file_change(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    attempt_id: Optional[str],
    path: str,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not path_in_effective_scope(plan, step_id, path):
        raise ValueError(f"{path} is outside effective file scope for {step_id}")
    resolved_attempt = resolve_attempt(root, step_id, attempt_id)
    require_writable_attempt(
        root, step_id, resolved_attempt, new_work=True, agent_id=agent_id
    )
    record = _change_record_for_path(root, path)
    full_path = root / path
    after_sha256 = None if record["change_kind"] == "deleted" else sha256_path(full_path)
    before_path = record["previous_path"] or path
    receipt = {
        "schema_version": FILE_RECEIPTS_SCHEMA_VERSION,
        "step_id": step_id,
        "attempt_id": resolved_attempt,
        "path": path,
        "previous_path": record["previous_path"] or None,
        "change_kind": record["change_kind"],
        "before_git_blob": git_blob_for_head(root, before_path),
        "after_sha256": after_sha256,
        "recorded_at": utc_now(),
    }
    if effective_hunk_policy(root) == "off":
        attribution, hunks = "disabled", []
    else:
        attribution, hunks = compute_hunks(root, record)
    receipt["diff_engine"] = "git"
    receipt["diff_command_version"] = DIFF_COMMAND_VERSION
    receipt["diff_algorithm"] = "myers"
    receipt["diff_unified"] = 0
    receipt["hunk_attribution"] = attribution
    receipt["hunks"] = hunks
    receipts_path = _file_receipts_path(root)
    with file_lock(_ledger_lock_path(receipts_path)):
        receipt["id"] = _next_file_receipt_id(root)
        append_jsonl(receipts_path, receipt)
    return receipt


def verify_receipt_outputs(root: Path) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    resolved_root = root.resolve()
    for receipt in command_receipts(root):
        for path_key, hash_key in (
            ("stdout_path", "stdout_sha256"),
            ("stderr_path", "stderr_sha256"),
        ):
            receipt_path = receipt.get(path_key)
            expected_hash = receipt.get(hash_key)
            if not receipt_path or not expected_hash:
                continue
            path = root / receipt_path
            resolved_path = path.resolve(strict=False)
            try:
                resolved_path.relative_to(resolved_root)
            except ValueError:
                findings.append(
                    {
                        "severity": "error",
                        "message": f"receipt output path escapes root: {receipt_path}",
                    }
                )
                continue
            if not path.exists():
                findings.append(
                    {"severity": "error", "message": f"missing receipt output {receipt_path}"}
                )
                continue
            actual_hash = sha256_path(resolved_path)
            if actual_hash != expected_hash:
                findings.append(
                    {
                        "severity": "error",
                        "message": f"receipt output hash mismatch for {receipt_path}",
                    }
                )
    return findings


def replay_gates(
    root: Path,
    plan: Dict[str, Any],
    step_id: Optional[str] = None,
    record: bool = False,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "passed", "errors": [], "warnings": [], "replayed": []}
    candidates = [
        receipt
        for receipt in command_receipts(root)
        if receipt.get("gate") and receipt.get("provenance") == "attested"
    ]
    if step_id:
        candidates = [receipt for receipt in candidates if receipt.get("step_id") == step_id]
    for receipt in candidates:
        command = receipt.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            result["errors"].append(
                {"severity": "error", "message": f"{receipt.get('id')} has malformed command"}
            )
            continue
        proc = subprocess.run(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != receipt.get("exit_code"):
            result["errors"].append(
                {
                    "severity": "error",
                    "message": (
                        f"{receipt.get('id')} exit code mismatch: "
                        f"recorded {receipt.get('exit_code')} replayed {proc.returncode}"
                    ),
                }
            )
            continue
        result["replayed"].append(receipt.get("id"))
        if record:
            record_command(
                root,
                str(receipt["step_id"]),
                str(receipt["attempt_id"]),
                command,
                proc.returncode,
                gate=str(receipt["gate"]),
                provenance="reconstructed",
                plan=plan,
            )
    if result["errors"]:
        result["status"] = "failed"
    elif result["warnings"]:
        result["status"] = "warning"
    return result
