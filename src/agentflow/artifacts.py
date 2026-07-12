"""Artifact defaults and filesystem helpers for Agentflow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .contracts import (
    ARTIFACT_COMPATIBILITY_POLICIES,
    ARTIFACT_PATHS,
    ARTIFACT_SCHEMA_VERSIONS,
    ASSUMPTIONS_SCHEMA_VERSION,
    CONTEXT_RECEIPTS_SCHEMA_VERSION,
    DRIFT_REPORT_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    FAILURES_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    PROOF_PACK_SCHEMA_VERSION,
    EXECUTION_ARTIFACT_SCHEMA_VERSIONS,
)
from .versioning import validate_schema_version_policy


SCHEMA_VERSION = PLAN_SCHEMA_VERSION


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# lock-plan restamps locked/locked_at on every re-lock, so a raw file hash
# would invalidate plan-bound evidence even when nothing semantic changed.
_PLAN_LOCK_METADATA_FIELDS = ("locked", "locked_at")


def plan_binding_sha256(plan: Dict[str, Any]) -> str:
    """Canonical content hash used to bind evidence to a plan.

    Excludes lock bookkeeping so a no-op re-lock keeps existing bindings;
    any semantic plan change still invalidates them.
    """
    content = {
        key: value
        for key, value in plan.items()
        if key not in _PLAN_LOCK_METADATA_FIELDS
    }
    payload = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_plan() -> Dict[str, Any]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "",
        "scope": [],
        "non_goals": [],
        "invariants": [],
        "allowed_files": [],
        "blocked_files": [],
        "validation_gates": [],
        "rollback_plan": "",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [],
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def default_assumptions() -> Dict[str, Any]:
    return {
        "schema_version": ASSUMPTIONS_SCHEMA_VERSION,
        "assumptions": [],
    }


def default_drift_report() -> Dict[str, Any]:
    return {
        "schema_version": DRIFT_REPORT_SCHEMA_VERSION,
        "status": "warning",
        "changed_files": [],
        "unmapped_hunks": [],
        "out_of_scope_files": [],
        "blocked_files_changed": [],
        "dependency_changes": [],
        "test_weakening": [],
        "notes": ["No drift audit has been run yet."],
        "generated_at": utc_now(),
    }


def default_proof_pack() -> str:
    return """# Proof Pack

## Objective

TBD.

## Scope

TBD.

## Plan Steps Completed

TBD.

## Files Changed

TBD.

## Evidence

TBD.

## Validation

TBD.

## Drift Audit

TBD.

## Remaining Risk

TBD.

## Follow-Ups

TBD.
"""


def model_profile(provider: str, name: str, cost_tier: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "provider": provider,
        "context_window": 0,
        "supports_tool_calls": False,
        "supports_json_schema": False,
        "supports_vision": False,
        "cost_tier": cost_tier,
        "strengths": [],
    }


def initial_files() -> List[Tuple[str, str]]:
    return [
        (".agent/plan.lock.json", dumps_json(default_plan())),
        (".agent/evidence.jsonl", ""),
        (".agent/assumptions.json", dumps_json(default_assumptions())),
        (".agent/context-receipts.jsonl", ""),
        (".agent/failures.jsonl", ""),
        (".agent/amendments.jsonl", ""),
        (".agent/review-runs.jsonl", ""),
        (".agent/capability-receipts.jsonl", ""),
        (".agent/runtime-snapshots.jsonl", ""),
        (".agent/drift-report.json", dumps_json(default_drift_report())),
        (".agent/proof-pack.md", default_proof_pack()),
        (
            ".agent/model-profiles/openai.example.json",
            dumps_json(model_profile("openai", "model-id", "medium")),
        ),
        (
            ".agent/model-profiles/anthropic.example.json",
            dumps_json(model_profile("anthropic", "model-id", "medium")),
        ),
        (
            ".agent/model-profiles/local.example.json",
            dumps_json(model_profile("local", "model-id", "low")),
        ),
    ]


def _artifact_name(path: Path) -> Optional[str]:
    candidate = path.as_posix()
    paths = {**ARTIFACT_PATHS, "proof-pack": ".agent/proof-pack.json"}
    return next(
        (
            name
            for name, relative in paths.items()
            if candidate == relative or candidate.endswith("/" + relative)
        ),
        None,
    )


def _validate_artifact_version(
    path: Path, data: Any, line_number: Optional[int] = None
) -> None:
    artifact = _artifact_name(path)
    reader_gated = {
        "execution-contract",
        "step-runs",
        "command-receipts",
        "file-receipts",
        "verification-runs",
        "drift-report",
    }
    if (
        artifact not in reader_gated
        or not isinstance(data, dict)
        or "schema_version" not in data
    ):
        return
    supported = EXECUTION_ARTIFACT_SCHEMA_VERSIONS.get(
        artifact, ARTIFACT_SCHEMA_VERSIONS.get(artifact)
    )
    if supported is None:
        return
    errors = validate_schema_version_policy(
        data.get("schema_version"),
        supported,
        artifact,
        ARTIFACT_COMPATIBILITY_POLICIES[artifact],
    )
    if errors:
        location = f"{path}:{line_number}" if line_number is not None else str(path)
        raise ValueError(f"{location}: {errors[0]}")


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    _validate_artifact_version(path, data)
    return data


def try_read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read a JSON object without raising for malformed or non-object JSON."""
    try:
        data = read_json(path)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"malformed JSON in {path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} top-level value must be a JSON object"
    return data, None


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_json(data), encoding="utf-8")


def append_jsonl(path: Path, item: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    items: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
                _validate_artifact_version(path, item, line_number)
                items.append(item)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
    return items


def create_initial_artifacts(root: Path, force: bool = False) -> Tuple[List[str], List[str]]:
    created: List[str] = []
    skipped: List[str] = []

    for relative_path, content in initial_files():
        path = root / relative_path
        if path.exists() and not force:
            skipped.append(relative_path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(relative_path)

    return created, skipped


def require_files(paths: Iterable[Path]) -> List[str]:
    missing = []
    for path in paths:
        if not path.exists():
            missing.append(str(path))
    return missing
