"""Compile a task brief plus a workflow pack into an unlocked draft plan (#71).

This is the bridge from adaptive workflow *policy* (a #70 recommendation over a
#61 task brief, and a #17 workflow pack) to an executable Agentflow *plan*. It is
deterministic, provider-agnostic, and read-only at the module level: it never
locks a plan and never writes to disk (the CLI layer owns I/O).

The design treats the recommendation as a **lower-bound posture** and the pack as
the **executable catalog**. ``recommend`` says how careful the workflow must be;
the pack profile is the real, representable workflow whose plan template provides
the step topology. ``draft-plan`` selects the least-strict pack profile that
satisfies the recommended posture, hydrates that profile's template with the
brief, and emits an unlocked ``plan.lock.json`` draft that passes
``validate_plan``. ``lock-plan`` remains the only authority for validation and
locking.
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import packs
from .contracts import REVIEW_DEPTH_ORDER, REVIEW_DEPTH_POLICY
from .packs import _safe_relative_path
from .recommend import ARCHETYPES_BY_ID, LARGE_FILE_MIN, recommend, validate_brief
from .validation import validate_plan
from .workflow_contract import WORKFLOW_CONTRACT_PATH

DRAFT_PLAN_SCHEMA_VERSION = "0.2.0"  # 0.2.0: output payload always carries a `warnings` array (#89)
SELECTION_MODE = "least_strict_satisfying_profile"
WORKFLOW_BLOCK_KEY = "workflow"

# #90: a profile that requires a review run will, at proof time, write review
# artifacts under this path; scope it into allowed_files so audit-drift does not
# flag them.
# ponytail: keep this constant until state_root actually varies per pack.
REVIEW_STATE_PATH = "docs/ai/state/"

# Strictness ladders. A profile satisfies a lower bound only when it is equal or
# stricter on every axis; "least strict" minimizes this tuple lexicographically.
# No recommend archetype emits `spec_quality`; it is ranked between `standard`
# and `deep` so a pack-only profile that uses it sorts sensibly during selection.
# Review depth ranking lives in contracts so plan selection and proof checks stay aligned.
HUNK_ORDER = {"off": 0, "observe": 1, "enforce": 2}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Task types whose candidate files are edits to existing code and must pre-exist.
FILE_BACKED_TASK_TYPES = ("bugfix", "refactor", "docs")


class DraftPlanError(ValueError):
    """A fail-closed compile diagnostic carrying a machine-readable ``code``."""

    def __init__(self, message: str, code: str = "draft_plan_error") -> None:
        super().__init__(message)
        self.code = code


def _required_cap_ids(profile_like: Dict[str, Any]) -> set:
    return {
        cap["id"]
        for cap in profile_like.get("required_capabilities", [])
        if cap.get("required")
    }


def _profile_requires_review_run(profile: Dict[str, Any]) -> bool:
    """Return True when the profile mandates a recorded review run at proof time.

    Mirrors the proof-time condition exactly: either the profile sets
    ``proof_policy.require_review_run`` or its ``review_depth`` carries a
    requires-run obligation in :data:`REVIEW_DEPTH_POLICY` (``spec_quality`` /
    ``deep``). Keeping this in lockstep with the policy means a deep profile that
    leaves the explicit flag False still gets its review-state path scoped in.
    """
    if profile["proof_policy"].get("require_review_run"):
        return True
    _, requires_run = REVIEW_DEPTH_POLICY.get(profile["review_depth"], ("warn", False))
    return requires_run


def lower_bound_from_recommendation(report: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the minimum-safety *posture* from a ``recommend`` report.

    The floor is the recommended archetype's intrinsic posture, NOT the report's
    ``workflow_contract_candidate`` (which also folds in the brief's ad-hoc
    ``validation_needs``). Those needs are operator gate requests added to the
    plan at hydration; they must not force every pack profile to pre-declare them.
    """
    archetype = ARCHETYPES_BY_ID[report["recommended"]["profile"]]
    return {
        "required_gates": set(archetype.required_gates),
        "required_capabilities": {
            cap["id"] for cap in archetype.required_capabilities if cap.get("required")
        },
        "review_depth": archetype.review_depth,
        "hunk_attribution": archetype.hunk_attribution,
        "require_review_run": archetype.require_review_run,
    }


def profile_satisfies(
    profile: Dict[str, Any], manifest: Dict[str, Any], lower_bound: Dict[str, Any]
) -> bool:
    """Return True when a pack profile meets or exceeds the lower-bound posture."""
    gates = set(profile["validation_policy"]["required_gates"])
    if not lower_bound["required_gates"] <= gates:
        return False
    if not lower_bound["required_capabilities"] <= _required_cap_ids(profile):
        return False
    if REVIEW_DEPTH_ORDER[profile["review_depth"]] < REVIEW_DEPTH_ORDER[lower_bound["review_depth"]]:
        return False
    if (
        HUNK_ORDER[profile["proof_policy"]["hunk_attribution"]]
        < HUNK_ORDER[lower_bound["hunk_attribution"]]
    ):
        return False
    if lower_bound["require_review_run"] and not _profile_requires_review_run(profile):
        return False
    # The profile's plan template must actually carry the required gates, else the
    # compiled plan could not run them.
    template = manifest.get("plan_templates", {}).get(profile["plan_template"], {})
    if not lower_bound["required_gates"] <= set(template.get("validation_gates", [])):
        return False
    return True


def profile_strictness(profile: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """A lexicographic strictness key; lower means less strict."""
    return (
        REVIEW_DEPTH_ORDER[profile["review_depth"]],
        HUNK_ORDER[profile["proof_policy"]["hunk_attribution"]],
        int(profile["proof_policy"]["require_review_run"]),
        len(profile["validation_policy"]["required_gates"]),
        len(_required_cap_ids(profile)),
    )


def select_profile(
    manifest: Dict[str, Any],
    lower_bound: Dict[str, Any],
    *,
    profile_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """Select the pack profile that becomes the plan's final posture.

    With ``profile_id`` the operator chooses explicitly; a profile weaker than the
    recommendation is refused unless a ``reason`` is supplied. Without it, the
    least-strict profile that satisfies the lower bound wins; zero matches or a tie
    at the minimum fails closed.
    """
    if profile_id is not None:
        try:
            profile = packs.find_profile(manifest, profile_id)
        except packs.PackError as exc:
            raise DraftPlanError(str(exc), code="unknown_profile") from exc
        if profile_satisfies(profile, manifest, lower_bound):
            return profile, "operator_selected"
        if not (reason and reason.strip()):
            raise DraftPlanError(
                f"profile {profile_id!r} is weaker than the recommended posture; "
                "pass --reason to override",
                code="profile_weaker_than_recommended",
            )
        return profile, "operator_override"

    candidates = [
        p for p in manifest["profiles"] if profile_satisfies(p, manifest, lower_bound)
    ]
    if not candidates:
        available = ", ".join(p["id"] for p in manifest["profiles"])
        raise DraftPlanError(
            "no workflow pack profile satisfies the recommended posture "
            f"(available: {available})",
            code="no_satisfying_profile",
        )
    best_key = min(profile_strictness(p) for p in candidates)
    minimal = [p for p in candidates if profile_strictness(p) == best_key]
    if len(minimal) > 1:
        tied = ", ".join(p["id"] for p in minimal)
        raise DraftPlanError(
            f"multiple pack profiles are equally least-strict ({tied}); "
            "pass --profile to choose one",
            code="ambiguous_profile",
        )
    return minimal[0], SELECTION_MODE


def is_broad(report: Dict[str, Any]) -> bool:
    """True when the recommendation reflects broad / cross-cutting work."""
    profile = report["recommended"]["profile"]
    broad_signal = "broad_signal=true" in report["signals"]
    return profile == "large-feature" or (profile == "high-risk" and broad_signal)


def _max_risk(*levels: str) -> str:
    return max(levels, key=lambda level: RISK_ORDER.get(level, 0))


def _missing_candidate_files(brief: Dict[str, Any], root: Path) -> list:
    """Return candidate files that must pre-exist but do not (else an empty list).

    Only file-backed task types (edits to existing code) require pre-existence;
    greenfield task types name files they are about to create.
    """
    if brief["task_type"] not in FILE_BACKED_TASK_TYPES:
        return []
    return [f for f in (brief.get("candidate_files") or []) if not (root / f).exists()]


def _candidate_escapes_root(root: Path, relative: str) -> bool:
    try:
        resolved_root = root.resolve(strict=False)
        (resolved_root / relative).resolve(strict=False).relative_to(resolved_root)
        return False
    except (OSError, RuntimeError, ValueError):
        return True


def _check_adequacy(report: Dict[str, Any], brief: Dict[str, Any], template: Dict[str, Any]) -> None:
    n_steps = len([s for s in template.get("steps", []) if isinstance(s, dict)])
    if n_steps >= 2:
        return
    files = brief.get("candidate_files") or []
    if brief.get("declared_size") == "xl" or len(files) >= LARGE_FILE_MIN:
        raise DraftPlanError(
            "brief is too large for a single-step template; provide a multi-step "
            "bounded template or split the brief",
            code="task_too_large",
        )
    if is_broad(report):
        raise DraftPlanError(
            "recommended posture is broad but the selected template has fewer than "
            "two steps; decompose the brief or choose a multi-step template",
            code="decomposition_required",
        )


def _hydrate(
    plan: Dict[str, Any],
    brief: Dict[str, Any],
    objective: str,
    profile: Dict[str, Any],
    manifest: Dict[str, Any],
    report: Dict[str, Any],
    selection_mode: str,
) -> Dict[str, Any]:
    plan = deepcopy(plan)
    plan["objective"] = objective.strip()
    needs = brief.get("validation_needs") or []
    plan["validation_gates"] = sorted(
        set(plan.get("validation_gates", []))
        | set(profile["validation_policy"]["required_gates"])
        | set(needs)
    )
    candidate_files = brief.get("candidate_files") or []
    # Preserve order, dedupe, and guarantee .agent/ is writable for the loop.
    # When the profile requires a review run (#90), scope the review-state path
    # too so recorded review artifacts are not flagged as out-of-scope drift.
    extra_scope = [".agent/"]
    if _profile_requires_review_run(profile):
        extra_scope.append(REVIEW_STATE_PATH)
    plan["allowed_files"] = list(
        dict.fromkeys([*plan.get("allowed_files", []), *candidate_files, *extra_scope])
    )
    plan["risk_level"] = _max_risk(plan.get("risk_level", "low"), brief["declared_risk"])
    # Pointer/provenance only; the authoritative policy lives in the linked
    # workflow contract, not duplicated into the plan body.
    plan[WORKFLOW_BLOCK_KEY] = {
        "contract_path": WORKFLOW_CONTRACT_PATH,
        "workflow_pack": manifest["id"],
        "workflow_profile": profile["id"],
        "recommended_profile": report["recommended"]["profile"],
        "selection_mode": selection_mode,
    }
    plan["locked"] = False
    plan["locked_at"] = None
    return plan


def selection_reason(
    report: Dict[str, Any],
    profile: Dict[str, Any],
    selection_mode: str,
    reason: Optional[str],
) -> str:
    """Human-readable rationale recorded in the linked workflow contract."""
    recommended = report["recommended"]["profile"]
    if selection_mode == "operator_override":
        return (
            f"Operator override to profile {profile['id']} (weaker than recommended "
            f"{recommended}). {reason}"
        )
    if selection_mode == "operator_selected":
        return (
            f"Operator selected profile {profile['id']} (satisfies recommended {recommended})."
        )
    return (
        f"Selected profile {profile['id']} as the least-strict pack profile satisfying "
        f"recommended {recommended}."
    )


def compile_draft_plan(
    brief: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    objective: str,
    root: Path,
    profile_id: Optional[str] = None,
    reason: Optional[str] = None,
    allow_missing_candidates: bool = False,
) -> Dict[str, Any]:
    """Compile a brief + pack into an unlocked draft plan.

    ``manifest`` must be a ``load_pack``-validated pack manifest; the selection
    and hydration logic trusts its schema (profiles, ``validation_policy``,
    ``proof_policy``, enum-constrained ``review_depth``/``hunk_attribution``).
    Returns ``{"plan", "profile", "report", "selection_mode", "template_id",
    "warnings"}``. Raises :class:`DraftPlanError` (with a machine-readable
    ``code``) on any fail-closed condition. The returned plan passes
    ``validate_plan`` and is unlocked.

    ``allow_missing_candidates`` is the #89 greenfield switch: a file-backed
    brief whose ``candidate_files`` do not exist yet normally fails closed with
    ``candidate_file_missing``; with this flag the absence is downgraded to a
    returned warning so a plan can be drafted for files about to be created.
    Path-escape rejection (``candidate_file_unsafe``) is never downgraded.
    """
    errors = validate_brief(brief)
    if errors:
        raise DraftPlanError("invalid brief: " + "; ".join(errors), code="invalid_brief")

    # Candidate files are brief-controlled and flow into the existence probe and
    # the draft's allowed_files; reject anything that could escape the repo root.
    unsafe = [
        f
        for f in (brief.get("candidate_files") or [])
        if not _safe_relative_path(f) or _candidate_escapes_root(root, f)
    ]
    if unsafe:
        raise DraftPlanError(
            "candidate files must be contained relative paths: " + ", ".join(unsafe),
            code="candidate_file_unsafe",
        )

    if not (objective and objective.strip()):
        raise DraftPlanError(
            "an objective is required to compile a plan", code="brief_too_vague"
        )

    report = recommend(brief)
    lower_bound = lower_bound_from_recommendation(report)

    if is_broad(report) and not (brief.get("candidate_files") or []):
        raise DraftPlanError(
            "recommended posture is broad but the brief lists no candidate_files to "
            "bound the change",
            code="brief_too_vague",
        )

    warnings: list = []
    missing = _missing_candidate_files(brief, root)
    if missing:
        message = "candidate files do not exist under the repo root: " + ", ".join(missing)
        if allow_missing_candidates:
            warnings.append({"code": "candidate_file_missing", "message": message})
        else:
            raise DraftPlanError(message, code="candidate_file_missing")

    profile, selection_mode = select_profile(
        manifest, lower_bound, profile_id=profile_id, reason=reason
    )
    template_id = profile["plan_template"]
    if template_id not in manifest.get("plan_templates", {}):
        raise DraftPlanError(
            f"selected profile references unknown plan template: {template_id}",
            code="workflow_inputs_unavailable",
        )
    template = manifest["plan_templates"][template_id]

    _check_adequacy(report, brief, template)

    plan = packs.template_to_plan(manifest, template_id)
    plan = _hydrate(plan, brief, objective, profile, manifest, report, selection_mode)

    plan_errors = validate_plan(plan)
    if plan_errors:
        raise DraftPlanError(
            "compiled plan failed validation: " + "; ".join(plan_errors),
            code="plan_invalid",
        )

    return {
        "plan": plan,
        "profile": profile,
        "report": report,
        "selection_mode": selection_mode,
        "template_id": template_id,
        "warnings": warnings,
    }
