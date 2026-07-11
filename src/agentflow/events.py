"""Read-only event-stream projection over Agentflow's .agent/ ledgers.

The five append-only ledgers remain authoritative. This module derives a single
chronological view and never writes to disk.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

from .artifacts import read_jsonl
from .contracts import ARTIFACT_PATHS

# Ledger projection order. Rank only breaks ties at identical timestamps; it
# never reorders events that already differ in time.
_LEDGER_ORDER = (
    "step-runs",
    "command-receipts",
    "file-receipts",
    "verification-runs",
    "review-runs",
)
_LEDGER_RANK = {name: index for index, name in enumerate(_LEDGER_ORDER)}


def _collect(record: Dict[str, Any], keys: tuple) -> Dict[str, Any]:
    return {key: record[key] for key in keys if record.get(key) is not None}


def _step_event(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    kind = record.get("event")
    return {
        "type": f"step.{kind}" if kind else "step.unknown",
        "timestamp": record.get("recorded_at", ""),
        "step_id": record.get("step_id"),
        "attempt_id": record.get("attempt_id"),
        "source": {"ledger": "step-runs", "record_id": None, "index": index},
        "data": _collect(
            record,
            (
                "agent_id",
                "lease_expires_at",
                "reason",
                "reason_code",
                "amends_attempt",
                "amends_completed_at",
                "findings",
                "finding_refs",
                "abandoned_by",
                "superseded_by",
                "failed_by",
            ),
        ),
    }


def _command_event(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    risk = record.get("risk") or {}
    data = _collect(
        record,
        (
            "command",
            "exit_code",
            "gate",
            "cwd",
            "provenance",
            "decision",
            "finished_at",
            "timed_out",
            "timeout_seconds",
            "stdout_sha256",
            "stderr_sha256",
        ),
    )
    data["risk_level"] = risk.get("level")
    data["finding_count"] = len(risk.get("findings", []))
    return {
        "type": "command.recorded",
        "timestamp": record.get("started_at", ""),
        "step_id": record.get("step_id"),
        "attempt_id": record.get("attempt_id"),
        "source": {"ledger": "command-receipts", "record_id": record.get("id"), "index": index},
        "data": data,
    }


def _file_event(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "type": "file.changed",
        "timestamp": record.get("recorded_at", ""),
        "step_id": record.get("step_id"),
        "attempt_id": record.get("attempt_id"),
        "source": {"ledger": "file-receipts", "record_id": record.get("id"), "index": index},
        "data": _collect(record, ("path", "previous_path", "change_kind", "after_sha256")),
    }


def _verification_event(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    data = _collect(record, ("scope", "status", "strict", "replay"))
    findings = record.get("findings")
    if isinstance(findings, list):
        data["finding_count"] = len(findings)
    return {
        "type": "verification.run",
        "timestamp": record.get("recorded_at", ""),
        "step_id": record.get("step_id"),
        "attempt_id": record.get("attempt_id"),
        "source": {"ledger": "verification-runs", "record_id": record.get("id"), "index": index},
        "data": data,
    }


def _review_event(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    data = _collect(record, ("review_run_id", "gate_status", "policy"))
    data["active_blocking_count"] = len(record.get("active_blocking", []) or [])
    return {
        "type": "review.recorded",
        "timestamp": record.get("recorded_at", ""),
        "step_id": None,
        "attempt_id": None,
        "source": {
            "ledger": "review-runs",
            "record_id": record.get("review_run_id"),
            "index": index,
        },
        "data": data,
    }


_PROJECTORS: Dict[str, Callable[[Dict[str, Any], int], Dict[str, Any]]] = {
    "step-runs": _step_event,
    "command-receipts": _command_event,
    "file-receipts": _file_event,
    "verification-runs": _verification_event,
    "review-runs": _review_event,
}


def project_events(root: Path) -> List[Dict[str, Any]]:
    """Project the five ledgers into one deterministically ordered event list."""
    events: List[Dict[str, Any]] = []
    for ledger in _LEDGER_ORDER:
        path = root / ARTIFACT_PATHS[ledger]
        projector = _PROJECTORS[ledger]
        for index, record in enumerate(read_jsonl(path)):
            if isinstance(record, dict):
                events.append(projector(record, index))
    events.sort(
        key=lambda event: (
            event["timestamp"],
            _LEDGER_RANK[event["source"]["ledger"]],
            event["source"]["index"],
        )
    )
    return events


def _normalize_utc_suffix(value: str) -> str:
    if value.endswith("Z"):
        return f"{value[:-1]}+00:00"
    return value


def _parse_iso_timestamp(value: str) -> datetime:
    normalized = _normalize_utc_suffix(value)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed_date = date.fromisoformat(normalized)
        parsed = datetime.combine(parsed_date, datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_events_since(events: List[Dict[str, Any]], since: str) -> List[Dict[str, Any]]:
    """Return events whose timestamp is at or after ``since`` (inclusive)."""
    threshold = _parse_iso_timestamp(since)
    filtered = []
    for event in events:
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str):
            continue
        try:
            event_time = _parse_iso_timestamp(timestamp)
        except ValueError:
            continue
        if event_time >= threshold:
            filtered.append(event)
    return filtered


def valid_since(value: str) -> bool:
    """True when ``value`` parses as an ISO-8601 date or datetime."""
    try:
        _parse_iso_timestamp(value)
    except ValueError:
        return False
    return True
