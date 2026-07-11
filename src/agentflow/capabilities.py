"""Capability receipts: record and summarize specialized-practice usage.

Agentflow declares required capabilities (via the workflow contract) and records
evidence that they were used or knowingly waived. It never invokes a provider;
``provider`` is a free string.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import append_jsonl, read_jsonl, utc_now
from .contracts import CAPABILITY_RECEIPTS_SCHEMA_VERSION, CAPABILITY_STATUSES

CAPABILITY_RECEIPTS_PATH = ".agent/capability-receipts.jsonl"

KNOWN_FIELDS = {
    "schema_version",
    "id",
    "capability",
    "status",
    "provider",
    "reason",
    "evidence",
    "recorded_at",
}


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_capability_receipt(row: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(row, dict):
        return ["capability receipt must be a JSON object"]
    for field in sorted(set(row) - KNOWN_FIELDS):
        errors.append(f"unknown capability receipt field: {field}")
    if row.get("schema_version") != CAPABILITY_RECEIPTS_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CAPABILITY_RECEIPTS_SCHEMA_VERSION}")
    for field in ("id", "capability", "reason", "recorded_at"):
        if not _non_empty_string(row.get(field)):
            errors.append(f"{field} must be a non-empty string")
    status = row.get("status")
    if status not in CAPABILITY_STATUSES:
        errors.append("status must be one of: " + ", ".join(CAPABILITY_STATUSES))
    provider = row.get("provider")
    if status == "used":
        if not _non_empty_string(provider):
            errors.append("provider must be a non-empty string when status is used")
    elif provider is not None and not _non_empty_string(provider):
        errors.append("provider must be a non-empty string when present")
    evidence = row.get("evidence", [])
    if not isinstance(evidence, list) or not all(_non_empty_string(item) for item in evidence):
        errors.append("evidence must be a list of non-empty strings")
    return errors


def build_capability_receipt(
    receipt_id: str,
    capability: str,
    status: str,
    reason: str,
    provider: Optional[str] = None,
    evidence: Optional[List[str]] = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "schema_version": CAPABILITY_RECEIPTS_SCHEMA_VERSION,
        "id": receipt_id,
        "capability": capability,
        "status": status,
        "reason": reason,
        "evidence": list(evidence or []),
        "recorded_at": utc_now(),
    }
    if provider is not None:
        row["provider"] = provider
    errors = validate_capability_receipt(row)
    if errors:
        raise ValueError("; ".join(errors))
    return row


def read_capability_receipts(root: Path) -> List[Dict[str, Any]]:
    rows = read_jsonl(root / CAPABILITY_RECEIPTS_PATH)
    for index, row in enumerate(rows, start=1):
        errors = validate_capability_receipt(row)
        if errors:
            raise ValueError(
                f"capability-receipts.jsonl row {index}: " + "; ".join(errors)
            )
    return rows


def append_capability_receipt(root: Path, row: Dict[str, Any]) -> None:
    append_jsonl(root / CAPABILITY_RECEIPTS_PATH, row)


def capability_summary(root: Path, required_ids: List[str]) -> Dict[str, Any]:
    rows = read_capability_receipts(root)
    used = {row["capability"] for row in rows if row.get("status") == "used"}
    waived = {row["capability"] for row in rows if row.get("status") == "waived"}
    satisfied = used | waived
    return {
        "required": list(required_ids),
        "recorded": sorted(used),
        "waived": sorted(waived),
        "missing": sorted({cap for cap in required_ids if cap not in satisfied}),
    }


def capability_checks(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    missing = summary.get("missing", [])
    if missing:
        return [
            {
                "id": "required_capabilities_satisfied",
                "status": "warning",
                "count": len(missing),
                "message": "missing required capabilities: " + ", ".join(missing),
            }
        ]
    return [{"id": "required_capabilities_satisfied", "status": "passed", "count": 0}]
