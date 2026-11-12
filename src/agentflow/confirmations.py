"""Bound self-confirmation ledger for high-risk commands (#112 phase 1).

``.agent/command-confirmations.jsonl`` holds ``issued`` rows written by
``agentflow confirm-command`` and ``consumed`` rows written when ``run`` spends
one. A confirmation is bound to one step, one exact argv, the locked plan and
the execution contract, and expires.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import append_jsonl, ledger_lock_path, plan_binding_sha256, read_jsonl
from .contracts import COMMAND_CONFIRMATIONS_SCHEMA_VERSION, EXECUTION_ARTIFACT_PATHS
from .execution import _now, confirmation_mode, execution_contract_sha256, load_execution_contract
from .locks import file_lock
from .risk import classify_command

DEFAULT_EXPIRY = timedelta(minutes=15)
MAX_EXPIRY = timedelta(hours=24)
_EXPIRY_RE = re.compile(r"[1-9][0-9]*[mh]")


def confirmations_path(root: Path) -> Path:
    return root / EXECUTION_ARTIFACT_PATHS["command-confirmations"]


def read_confirmations(root: Path) -> List[Dict[str, Any]]:
    return read_jsonl(confirmations_path(root))


def argv_sha256(argv: List[str]) -> str:
    return hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_expiry(text: str) -> timedelta:
    if not _EXPIRY_RE.fullmatch(text):
        raise ValueError("--expires must look like 15m or 2h")
    value = int(text[:-1])
    delta = timedelta(minutes=value) if text[-1] == "m" else timedelta(hours=value)
    if delta > MAX_EXPIRY:
        raise ValueError("--expires is capped at 24h")
    return delta


def issue_confirmation(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    argv: List[str],
    expires: timedelta,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Append an ``issued`` row; raise ValueError (no ledger written) on refusal."""
    if confirmation_mode(load_execution_contract(root)) != "bound":
        raise ValueError("confirm-command needs command_policy.confirmation bound")
    if not any(isinstance(s, dict) and s.get("id") == step_id for s in plan.get("steps", [])):
        raise ValueError(f"unknown step id: {step_id}")
    if classify_command(argv, plan, step_id, root=root)["level"] != "high":
        raise ValueError("command is not high-risk; it needs no confirmation")
    issued = _now(now).replace(microsecond=0)
    path = confirmations_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(ledger_lock_path(path)):
        count = sum(1 for r in read_confirmations(root) if r.get("event") == "issued")
        row = {
            "schema_version": COMMAND_CONFIRMATIONS_SCHEMA_VERSION,
            "id": f"CC{count + 1}",
            "event": "issued",
            "step_id": step_id,
            "argv": list(argv),
            "argv_sha256": argv_sha256(argv),
            "plan_sha256": plan_binding_sha256(plan),
            "execution_contract_sha256": execution_contract_sha256(root),
            "issued_at": issued.isoformat(),
            "expires_at": (issued + expires).isoformat(),
            "source": "self",
        }
        append_jsonl(path, row)
    return row


def _expired(row: Dict[str, Any], now: datetime) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(row.get("expires_at")))
    except ValueError:
        return True
    return _now(expires_at) <= _now(now)


def _number(row: Dict[str, Any]) -> int:
    match = re.fullmatch(r"CC([0-9]+)", str(row.get("id")))
    return int(match.group(1)) if match else 0


def match_confirmation(
    rows: List[Dict[str, Any]],
    step_id: str,
    argv_hash: str,
    plan_hash: str,
    contract_hash: Optional[str],
    now: datetime,
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(id, None)`` for a usable confirmation, else ``(None, reason)``."""
    consumed = {r.get("id") for r in rows if r.get("event") == "consumed"}
    issued = [
        r for r in rows
        if r.get("event") == "issued"
        and r.get("step_id") == step_id
        and r.get("argv_sha256") == argv_hash
    ]
    bound = [
        r for r in issued
        if r.get("plan_sha256") == plan_hash
        and r.get("execution_contract_sha256") == contract_hash
    ]
    usable = [r for r in bound if not _expired(r, now) and r.get("id") not in consumed]
    if usable:
        return min(usable, key=_number)["id"], None
    if any(not _expired(r, now) for r in bound):
        return None, "confirmation_used"
    if bound:
        return None, "confirmation_expired"
    if issued:
        return None, "confirmation_stale"
    return None, "confirmation_missing"


def consume_confirmation(
    root: Path,
    plan: Dict[str, Any],
    step_id: str,
    attempt_id: str,
    argv: List[str],
    contract_hash: Optional[str],
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Burn a matching confirmation: ``(id, None)`` after appending its
    ``consumed`` row, else ``(None, reason)`` with nothing written.

    Match and append happen under one ledger lock, so two runs cannot spend
    the same confirmation. Callers launch only after this returns, with the
    lock released, and must not hold the receipts lock while calling it.
    """
    at = _now(now)
    path = confirmations_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(ledger_lock_path(path)):
        found, reason = match_confirmation(
            read_confirmations(root), step_id, argv_sha256(argv),
            plan_binding_sha256(plan), contract_hash, at,
        )
        if found is not None:
            append_jsonl(path, {
                "schema_version": COMMAND_CONFIRMATIONS_SCHEMA_VERSION,
                "id": found,
                "event": "consumed",
                "attempt_id": attempt_id,
                "consumed_at": at.replace(microsecond=0).isoformat(),
            })
    return found, reason
