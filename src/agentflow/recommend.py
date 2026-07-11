"""Deterministic workflow recommendation from a task brief (#70).

Reads a machine-authored task brief and returns a recommended workflow posture
(one of five archetypes) plus nearest-cheaper / nearest-safer alternatives and a
full workflow-contract candidate. Pure and stdlib-only: no ``.agent`` writes, no
plan locking, no ``load_pack``, no subprocess/eval/import of data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .contracts import RISK_LEVELS, WORKFLOW_CONTRACT_SCHEMA_VERSION
from .versioning import validate_schema_version

BRIEF_SCHEMA_VERSION = "0.1.0"
RECOMMENDATION_SCHEMA_VERSION = "0.1.0"
PACK_ID = "agentflow-default"

TASK_TYPES = ("docs", "bugfix", "feature", "refactor")
BLAST_RADII = ("isolated", "local", "cross_cutting")
DECLARED_SIZES = ("xs", "s", "m", "l", "xl")
DOCS_EXTENSIONS = (".md", ".rst", ".txt", ".adoc")
DOCS_DIR_SEGMENTS = ("docs", "doc")

SMALL_FILE_MAX = 5
LARGE_FILE_MIN = 20
UNKNOWN = "unknown"

BRIEF_FIELDS = {
    "schema_version",
    "task_type",
    "declared_risk",
    "security_sensitive",
    "candidate_files",
    "blast_radius",
    "validation_needs",
    "declared_size",
}
REQUIRED_BRIEF_FIELDS = ("schema_version", "task_type", "declared_risk")


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_non_empty_string(item) for item in value)


def validate_brief(brief: Any) -> List[str]:
    """Return machine-readable diagnostics for a task brief; empty means valid."""
    errors: List[str] = []
    if not isinstance(brief, dict):
        return ["task brief must be a JSON object"]

    for field in sorted(set(brief) - BRIEF_FIELDS):
        errors.append(f"unknown task brief field: {field}")
    for field in REQUIRED_BRIEF_FIELDS:
        if field not in brief:
            errors.append(f"missing required task brief field: {field}")
    if errors:
        return errors

    errors.extend(
        validate_schema_version(brief["schema_version"], BRIEF_SCHEMA_VERSION, "task-brief")
    )
    if brief["task_type"] not in TASK_TYPES:
        errors.append("task_type must be one of: " + ", ".join(TASK_TYPES))
    if brief["declared_risk"] not in RISK_LEVELS:
        errors.append("declared_risk must be one of: " + ", ".join(RISK_LEVELS))

    if "security_sensitive" in brief and not isinstance(brief["security_sensitive"], bool):
        errors.append("security_sensitive must be boolean")
    if "candidate_files" in brief and not _string_list(brief["candidate_files"]):
        errors.append("candidate_files must be an array of non-empty strings")
    if "validation_needs" in brief and not _string_list(brief["validation_needs"]):
        errors.append("validation_needs must be an array of non-empty strings")
    if "blast_radius" in brief and brief["blast_radius"] not in BLAST_RADII:
        errors.append("blast_radius must be one of: " + ", ".join(BLAST_RADII))
    if "declared_size" in brief and brief["declared_size"] not in DECLARED_SIZES:
        errors.append("declared_size must be one of: " + ", ".join(DECLARED_SIZES))

    return errors


@dataclass(frozen=True)
class Archetype:
    profile_id: str
    rank: int
    review_depth: str
    hunk_attribution: str
    require_review_run: bool
    required_capabilities: Tuple[Dict[str, Any], ...]
    required_gates: Tuple[str, ...]
    summary: str


ARCHETYPES: Tuple[Archetype, ...] = (
    Archetype("docs-only", 0, "none", "observe", False, (), (),
              "a documentation-only, low-risk change"),
    Archetype("small-bugfix", 1, "light", "enforce", False, (), ("unit-tests",),
              "a bounded, low-risk fix with few known files"),
    Archetype("medium-feature", 2, "standard", "enforce", False, (), ("unit-tests",),
              "a change that adds bounded new behavior"),
    Archetype("large-feature", 3, "deep", "enforce", True, (), ("unit-tests",),
              "broad or cross-cutting work"),
    Archetype("high-risk", 4, "deep", "enforce", True,
              ({"id": "security-review", "required": True},), ("unit-tests", "security-scan"),
              "high-risk or security-sensitive work"),
)
ARCHETYPES_BY_ID: Dict[str, Archetype] = {a.profile_id: a for a in ARCHETYPES}
# Rank must equal index so "nearest cheaper/safer" is index +/- 1 (see _alternatives).
assert [a.rank for a in ARCHETYPES] == list(range(len(ARCHETYPES)))


def _is_docs_path(path: str) -> bool:
    if path.lower().endswith(DOCS_EXTENSIONS):
        return True
    return any(segment in DOCS_DIR_SEGMENTS for segment in path.split("/"))


def _rationale(profile_id: str) -> str:
    return f"Recommended {profile_id}: {ARCHETYPES_BY_ID[profile_id].summary}."


def classify(brief: Dict[str, Any]) -> Tuple[str, List[str], str]:
    """Map a (validated) brief to ``(profile_id, signals, rationale)``. Pure.

    Ordered rules, first match wins. Missing optional signals are ``unknown``
    and never read as "safe": only an explicitly-present, satisfied signal may
    de-escalate to ``docs-only`` or ``small-bugfix``.
    """
    task_type = brief["task_type"]
    declared_risk = brief["declared_risk"]
    security = bool(brief.get("security_sensitive", False))
    files = brief.get("candidate_files")
    files = list(files) if isinstance(files, list) else None
    blast = brief.get("blast_radius", UNKNOWN)
    size = brief.get("declared_size", UNKNOWN)
    file_count = len(files) if files is not None else None

    broad_signal = (
        blast == "cross_cutting"
        or size in ("l", "xl")
        or (file_count is not None and file_count >= LARGE_FILE_MIN)
    )
    all_docs = files is not None and len(files) > 0 and all(_is_docs_path(f) for f in files)
    bounded_small = (
        files is not None
        and len(files) > 0
        and len(files) <= SMALL_FILE_MAX
        and not broad_signal
        and size in ("xs", "s")
        and blast in ("isolated", "local")
    )

    if declared_risk == "high" or security:
        profile_id = "high-risk"
    elif task_type == "docs" and declared_risk == "low" and not security and all_docs:
        profile_id = "docs-only"
    elif broad_signal:
        profile_id = "large-feature"
    elif task_type == "bugfix" and declared_risk == "low" and not security and bounded_small:
        profile_id = "small-bugfix"
    else:
        profile_id = "medium-feature"

    signals = [
        f"task_type={task_type}",
        f"declared_risk={declared_risk}",
        f"security_sensitive={str(security).lower()}",
        f"candidate_files={file_count if file_count is not None else UNKNOWN}",
        f"blast_radius={blast}",
        f"declared_size={size}",
        f"broad_signal={str(broad_signal).lower()}",
        f"bounded_small={str(bounded_small).lower()}",
    ]
    return profile_id, signals, _rationale(profile_id)


class RecommendError(ValueError):
    """Invalid recommendation argument (unknown profile / missing override reason)."""

    def __init__(self, message: str, code: str = "recommend_error") -> None:
        super().__init__(message)
        self.code = code


def build_contract_candidate(
    profile_id: str, brief: Dict[str, Any], *, selected_by: str, selection_reason: str
) -> Dict[str, Any]:
    """Project an archetype + brief into a full, valid workflow.contract.json object.

    The candidate is never written; it is the report's ready-to-materialize
    payload. ``validation_needs`` from the brief union into the archetype gates.
    """
    arch = ARCHETYPES_BY_ID[profile_id]
    needs = brief.get("validation_needs") or []
    gates = sorted(set(arch.required_gates) | set(needs))
    return {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "workflow_pack": PACK_ID,
        "workflow_profile": arch.profile_id,
        "selected_by": selected_by,
        "selection_reason": selection_reason,
        "required_capabilities": [dict(cap) for cap in arch.required_capabilities],
        "review_depth": arch.review_depth,
        "validation_policy": {"required_gates": gates},
        "proof_policy": {
            "hunk_attribution": arch.hunk_attribution,
            "require_review_run": arch.require_review_run,
        },
    }


def _alternatives(profile_id: str) -> List[Dict[str, str]]:
    rank = ARCHETYPES_BY_ID[profile_id].rank
    out: List[Dict[str, str]] = []
    for relation, target in (("cheaper", rank - 1), ("safer", rank + 1)):
        if 0 <= target < len(ARCHETYPES):
            arch = ARCHETYPES[target]
            out.append({
                "profile": arch.profile_id,
                "relation": relation,
                "reason": f"choose {arch.profile_id} for {arch.summary}",
            })
    return out


def recommend(
    brief: Dict[str, Any],
    *,
    selected_profile: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the full recommendation report. Pure; raises ``RecommendError``.

    ``selected_profile`` lets an operator override the recommendation. An
    override (selected != recommended) requires a non-empty ``reason``.
    """
    recommended, signals, rationale = classify(brief)
    selected = recommended
    override: Optional[Dict[str, str]] = None

    if selected_profile is not None:
        if selected_profile not in ARCHETYPES_BY_ID:
            raise RecommendError(f"unknown profile: {selected_profile}", code="unknown_profile")
        selected = selected_profile
        if selected_profile != recommended:
            if not (reason and reason.strip()):
                raise RecommendError(
                    "override requires --reason", code="override_requires_reason"
                )
            override = {
                "from_profile": recommended,
                "to_profile": selected_profile,
                "reason": reason,
            }

    if override is not None:
        selected_by = "recommend-workflow --selected-profile"
        selection_reason = f"Override: {recommended} -> {selected}. {reason}"
    else:
        selected_by = "recommend-workflow"
        selection_reason = rationale

    return {
        "schema_version": RECOMMENDATION_SCHEMA_VERSION,
        "recommended": {"pack": PACK_ID, "profile": recommended},
        "selected": {"pack": PACK_ID, "profile": selected},
        "signals": signals,
        "rationale": rationale,
        "alternatives": _alternatives(recommended),
        "override": override,
        "workflow_contract_candidate": build_contract_candidate(
            selected, brief, selected_by=selected_by, selection_reason=selection_reason
        ),
    }


def render_text(report: Dict[str, Any]) -> List[str]:
    """Render a recommendation report as human-readable lines."""
    recommended = report["recommended"]
    lines = [f"recommended {recommended['pack']}/{recommended['profile']}"]
    override = report.get("override")
    if override is not None:
        selected = report["selected"]
        lines.append(f"selected {selected['pack']}/{selected['profile']} (override)")
        lines.append(
            f"override: {override['from_profile']} -> {override['to_profile']}: {override['reason']}"
        )
    lines.append("signals: " + ", ".join(report["signals"]))
    lines.append(f"rationale: {report['rationale']}")
    lines.append("alternatives:")
    for alt in report["alternatives"]:
        lines.append(f"  {alt['relation']} {alt['profile']}: {alt['reason']}")
    return lines
