"""Validation and drift-audit logic for Agentflow artifacts."""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .artifacts import try_read_json, utc_now
from .contracts import (
    AUTHORITIES,
    DRIFT_REPORT_SCHEMA_VERSION,
    EXECUTION_MODES,
    PLAN_SCHEMA_VERSION,
    RISK_LEVELS,
)
from .execution import (
    attempt_is_expired,
    lease_grace_seconds,
    lease_policy,
    read_step_state,
)
from .git import changed_file_records, changed_files, is_git_repo
from .hunks import effective_hunk_policy, unmapped_hunks
from .versioning import validate_schema_version


REQUIRED_PLAN_FIELDS = {
    "schema_version": str,
    "objective": str,
    "scope": list,
    "non_goals": list,
    "invariants": list,
    "allowed_files": list,
    "blocked_files": list,
    "validation_gates": list,
    "rollback_plan": str,
    "risk_level": str,
    "drift_budget": dict,
    "steps": list,
    "evidence_ids": list,
}

_TRACE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")


def _is_non_empty_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _validate_criterion_refs(
    prefix: str,
    value: Any,
    criterion_ids: set[str],
    errors: List[str],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{prefix} must be a list")
        return
    if not value:
        errors.append(f"{prefix} must contain at least one criterion id")
        return
    if not _is_non_empty_string_list(value):
        errors.append(f"{prefix} must contain only non-empty strings")
        return
    seen = set()
    for criterion_id in value:
        if criterion_id in seen:
            errors.append(f"{prefix} contains duplicate id: {criterion_id}")
        seen.add(criterion_id)
        if criterion_id not in criterion_ids:
            errors.append(
                f"{prefix} references unknown acceptance criterion id: {criterion_id}"
            )


def _validate_requirements(plan: Dict[str, Any], errors: List[str]) -> set[str]:
    if "requirements" not in plan:
        return set()
    requirements = plan["requirements"]
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements must contain at least one requirement")
        return set()

    requirement_ids: set[str] = set()
    criterion_ids: set[str] = set()
    for requirement_index, requirement in enumerate(requirements, start=1):
        prefix = f"requirements[{requirement_index}]"
        if not isinstance(requirement, dict):
            errors.append(f"{prefix} must be an object")
            continue
        requirement_id = requirement.get("id")
        if not isinstance(requirement_id, str) or not _TRACE_ID_RE.fullmatch(requirement_id):
            errors.append(f"{prefix}.id has invalid stable id: {requirement_id}")
        elif requirement_id in requirement_ids:
            errors.append(f"duplicate requirement id: {requirement_id}")
        else:
            requirement_ids.add(requirement_id)
        if not isinstance(requirement.get("text"), str) or not requirement["text"].strip():
            errors.append(f"{prefix}.text must be a non-empty string")

        criteria = requirement.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria:
            errors.append(f"{prefix}.acceptance_criteria must contain at least one criterion")
            continue
        for criterion_index, criterion in enumerate(criteria, start=1):
            criterion_prefix = f"{prefix}.acceptance_criteria[{criterion_index}]"
            if not isinstance(criterion, dict):
                errors.append(f"{criterion_prefix} must be an object")
                continue
            criterion_id = criterion.get("id")
            if not isinstance(criterion_id, str) or not _TRACE_ID_RE.fullmatch(criterion_id):
                errors.append(f"{criterion_prefix}.id has invalid stable id: {criterion_id}")
            elif criterion_id in criterion_ids:
                errors.append(f"duplicate acceptance criterion id: {criterion_id}")
            else:
                criterion_ids.add(criterion_id)
            if not isinstance(criterion.get("text"), str) or not criterion["text"].strip():
                errors.append(f"{criterion_prefix}.text must be a non-empty string")
            review = criterion.get("review")
            if review is not None:
                if not isinstance(review, dict):
                    errors.append(f"{criterion_prefix}.review must be an object")
                elif review.get("minimum_depth") not in ("spec_quality", "deep"):
                    errors.append(
                        f"{criterion_prefix}.review.minimum_depth must be one of: "
                        "spec_quality, deep"
                    )
    return criterion_ids


def validate_requirement_traceability(plan: Dict[str, Any]) -> List[str]:
    """Validate the optional requirement/criterion extension in isolation."""
    errors: List[str] = []
    criterion_ids = _validate_requirements(plan, errors)
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        if "requirements" in plan:
            errors.append("steps must be a list for requirement traceability")
        return errors

    mapped_criterion_ids: set[str] = set()
    for step_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        prefix = f"steps[{step_index}]"
        step_criterion_ids: set[str] = set()
        if "criterion_ids" in step:
            refs = step["criterion_ids"]
            _validate_criterion_refs(
                f"{prefix}.criterion_ids", refs, criterion_ids, errors
            )
            if isinstance(refs, list):
                step_criterion_ids = {
                    item for item in refs if isinstance(item, str)
                }
                mapped_criterion_ids.update(step_criterion_ids)
        gates = step.get("gates", [])
        if not isinstance(gates, list):
            continue
        for gate_index, gate in enumerate(gates, start=1):
            if isinstance(gate, dict) and "criterion_ids" in gate:
                gate_prefix = f"{prefix}.gates[{gate_index}].criterion_ids"
                gate_refs = gate["criterion_ids"]
                _validate_criterion_refs(
                    gate_prefix,
                    gate_refs,
                    criterion_ids,
                    errors,
                )
                for criterion_id in gate_refs if isinstance(gate_refs, list) else []:
                    if (
                        isinstance(criterion_id, str)
                        and criterion_id in criterion_ids
                        and criterion_id not in step_criterion_ids
                    ):
                        errors.append(
                            f"{gate_prefix} must be a subset of "
                            f"{prefix}.criterion_ids; invalid id: {criterion_id}"
                        )

    for criterion_id in sorted(criterion_ids):
        if criterion_id not in mapped_criterion_ids:
            errors.append(
                f"acceptance criterion {criterion_id} is not mapped to any step"
            )
    return errors


def _validate_gate(
    prefix: str,
    gate: Any,
    evidence_ids: set[str],
    errors: List[str],
) -> None:
    if not isinstance(gate, dict):
        errors.append(f"{prefix} must be an object")
        return
    kind = gate.get("kind")
    if kind not in ("command", "inspection"):
        errors.append(f"{prefix}.kind must be one of: command, inspection")
        return
    if kind == "command":
        run = gate.get("run")
        if not _is_non_empty_string_list(run):
            errors.append(f"{prefix}.run must contain at least one command argument")
        if "timeout_seconds" in gate:
            timeout = gate["timeout_seconds"]
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
                errors.append(f"{prefix}.timeout_seconds must be a positive integer")
    if kind == "inspection":
        evidence_id = gate.get("evidence_id")
        describe = gate.get("describe")
        if not isinstance(evidence_id, str) or not evidence_id:
            errors.append(f"{prefix}.evidence_id must be a non-empty string")
        elif evidence_id not in evidence_ids:
            errors.append(f"{prefix}.evidence_id references unknown evidence id: {evidence_id}")
        if not isinstance(describe, str) or not describe.strip():
            errors.append(f"{prefix}.describe must be a non-empty string")


def _detect_depends_on_errors(steps: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    ids = [step.get("id") for step in steps if isinstance(step, dict)]
    known_ids = {step_id for step_id in ids if isinstance(step_id, str)}
    graph: Dict[str, List[str]] = {}
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str):
            continue
        depends_on = step.get("depends_on", [])
        if depends_on is None:
            depends_on = []
        if not isinstance(depends_on, list):
            errors.append(f"steps[{index}].depends_on must be a list")
            continue
        graph[step_id] = []
        for dependency in depends_on:
            if not isinstance(dependency, str) or not dependency:
                errors.append(f"steps[{index}].depends_on must contain only non-empty strings")
                continue
            if dependency not in known_ids:
                errors.append(f"steps[{index}].depends_on references unknown step id: {dependency}")
            graph[step_id].append(dependency)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str, chain: List[str]) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            cycle = " -> ".join([*chain, step_id])
            errors.append(f"depends_on cycle detected: {cycle}")
            return
        visiting.add(step_id)
        for dependency in graph.get(step_id, []):
            if dependency in graph:
                visit(dependency, [*chain, step_id])
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in sorted(graph):
        visit(step_id, [])
    return errors


def validate_plan(plan: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    for field, expected_type in REQUIRED_PLAN_FIELDS.items():
        if field not in plan:
            errors.append(f"missing required field: {field}")
            continue
        if not isinstance(plan[field], expected_type):
            errors.append(f"{field} must be {expected_type.__name__}")

    if errors:
        return errors

    errors.extend(
        validate_schema_version(plan["schema_version"], PLAN_SCHEMA_VERSION, "plan-lock")
    )
    if not plan["objective"].strip():
        errors.append("objective must not be empty")
    if plan["risk_level"] not in RISK_LEVELS:
        errors.append(f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if not _is_non_empty_string_list(plan["scope"]):
        errors.append("scope must contain at least one non-empty string")
    if not _is_non_empty_string_list(plan["invariants"]):
        errors.append("invariants must contain at least one non-empty string")
    if not _is_non_empty_string_list(plan["allowed_files"]):
        errors.append("allowed_files must contain at least one non-empty string")
    if not _is_non_empty_string_list(plan["validation_gates"]):
        errors.append("validation_gates must contain at least one non-empty string")
    if not plan["rollback_plan"].strip():
        errors.append("rollback_plan must not be empty")

    drift_budget = plan["drift_budget"]
    for field in ("unrelated_edits", "new_dependencies", "formatting_drift", "architecture_drift"):
        if field not in drift_budget:
            errors.append(f"drift_budget missing field: {field}")

    context_budget = plan.get("context_budget")
    if context_budget is not None:
        if not isinstance(context_budget, dict):
            errors.append("context_budget must be an object")
        else:
            for field in ("max_files", "max_total_bytes", "max_log_lines_per_failure"):
                if field in context_budget and (
                    not isinstance(context_budget[field], int) or context_budget[field] < 0
                ):
                    errors.append(f"context_budget.{field} must be a non-negative integer")
            if "receipts_required" in context_budget and not isinstance(
                context_budget["receipts_required"], bool
            ):
                errors.append("context_budget.receipts_required must be boolean")

    if "quality_gates" in plan and not _is_non_empty_string_list(plan["quality_gates"]):
        errors.append("quality_gates must contain only non-empty strings")

    if "runtime_routes" in plan and not isinstance(plan["runtime_routes"], dict):
        errors.append("runtime_routes must be an object")

    errors.extend(validate_requirement_traceability(plan))

    evidence_ids = set(plan["evidence_ids"])
    if any(not isinstance(item, str) or not item for item in evidence_ids):
        errors.append("evidence_ids must contain only non-empty strings")

    step_ids = set()
    for index, step in enumerate(plan["steps"], start=1):
        prefix = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{prefix} must be an object")
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            errors.append(f"{prefix}.id must be a non-empty string")
        elif step_id in step_ids:
            errors.append(f"duplicate step id: {step_id}")
        else:
            step_ids.add(step_id)
        for field in ("action", "preconditions", "expected_diff", "validation", "evidence_ids"):
            if field not in step:
                errors.append(f"{prefix} missing field: {field}")
        if not isinstance(step.get("action"), str) or not step.get("action", "").strip():
            errors.append(f"{prefix}.action must be a non-empty string")
        if not _is_non_empty_string_list(step.get("files")):
            errors.append(f"{prefix}.files must contain at least one file")
        if not _is_non_empty_string_list(step.get("validation")):
            errors.append(f"{prefix}.validation must contain at least one command or inspection")
        execution_mode = step.get("execution_mode")
        if execution_mode is not None and execution_mode not in EXECUTION_MODES:
            errors.append(
                f"{prefix}.execution_mode must be one of: {', '.join(EXECUTION_MODES)}"
            )
        authority = step.get("authority")
        if authority is not None and authority not in AUTHORITIES:
            errors.append(f"{prefix}.authority must be one of: {', '.join(AUTHORITIES)}")
        runtime_role = step.get("runtime_role")
        if runtime_role is not None and (
            not isinstance(runtime_role, str) or not runtime_role.strip()
        ):
            errors.append(f"{prefix}.runtime_role must be a non-empty string")
        for evidence_id in step.get("evidence_ids", []):
            if evidence_id not in evidence_ids:
                errors.append(f"{prefix}.evidence_ids references unknown evidence id: {evidence_id}")
        gates = step.get("gates")
        if gates is not None:
            if not isinstance(gates, list) or not gates:
                errors.append(f"{prefix}.gates must contain at least one gate")
            else:
                for gate_index, gate in enumerate(gates, start=1):
                    _validate_gate(
                        f"{prefix}.gates[{gate_index}]",
                        gate,
                        evidence_ids,
                        errors,
                    )

    errors.extend(
        _detect_depends_on_errors(
            [step for step in plan["steps"] if isinstance(step, dict)]
        )
    )
    return errors


def matches_path(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.strip()
        if not normalized:
            continue
        if normalized.endswith("/"):
            if path.startswith(normalized):
                return True
        if path == normalized or fnmatch.fnmatch(path, normalized):
            return True
    return False


def effective_scope(plan: Dict[str, Any], step_id: str) -> Tuple[List[str], List[str]]:
    """Return ``(effective_allowed, blocked)`` for a step.

    ``effective_allowed`` is ``step.files`` intersected with top-level
    ``allowed_files``; ``blocked`` is the top-level ``blocked_files``. Mirrors
    the file-receipt scope so risk screening and file receipts agree.
    """
    step_files: List[str] = []
    for step in plan.get("steps", []):
        if isinstance(step, dict) and step.get("id") == step_id:
            step_files = [item for item in step.get("files", []) if isinstance(item, str)]
            break
    allowed = [item for item in plan.get("allowed_files", []) if isinstance(item, str)]
    blocked = [item for item in plan.get("blocked_files", []) if isinstance(item, str)]
    effective_allowed = [
        item
        for item in step_files
        if any(matches_path(item, [allowed_item]) for allowed_item in allowed)
    ]
    return effective_allowed, blocked


def path_in_effective_scope(plan: Dict[str, Any], step_id: str, path: str) -> bool:
    allowed, blocked = effective_scope(plan, step_id)
    return matches_path(path, allowed) and not matches_path(path, blocked)


def audit_drift(root: Path, plan: Dict[str, Any]) -> Dict[str, Any]:
    report = {
        "schema_version": DRIFT_REPORT_SCHEMA_VERSION,
        "status": "pass",
        "changed_files": [],
        "unmapped_hunks": [],
        "out_of_scope_files": [],
        "blocked_files_changed": [],
        "dependency_changes": [],
        "test_weakening": [],
        "notes": [],
        "generated_at": utc_now(),
    }

    if not is_git_repo(root):
        report["status"] = "warning"
        report["notes"].append("Not a git repository; drift audit could not inspect changed files.")
        return report

    files = changed_files(root)
    report["changed_files"] = files

    allowed = plan.get("allowed_files", [])
    blocked = plan.get("blocked_files", [])
    if not allowed:
        report["status"] = "warning"
        report["notes"].append("Plan has no allowed_files entries; every changed file is unaudited.")
        report["out_of_scope_files"] = files
    else:
        report["out_of_scope_files"] = [path for path in files if not matches_path(path, allowed)]

    report["blocked_files_changed"] = [path for path in files if matches_path(path, blocked)]
    report["dependency_changes"] = [
        path
        for path in files
        if path in {"pyproject.toml", "package.json", "package-lock.json", "pnpm-lock.yaml", "uv.lock"}
        or path.endswith((".lock", "requirements.txt"))
    ]

    if report["dependency_changes"]:
        report["notes"].append("Dependency-related files changed; verify dependency budget manually.")

    if report["out_of_scope_files"] or report["blocked_files_changed"]:
        report["status"] = "fail"

    hunk_policy = effective_hunk_policy(root)
    ignore = [".agent/", ".git/"]
    contract_path = root / ".agent/execution.contract.json"
    if contract_path.exists():
        try:
            contract, _ = try_read_json(contract_path)
        except OSError:
            contract = None
        if contract:
            ignore = contract.get("concurrency", {}).get("reconcile_ignore", ignore)
    if hunk_policy in ("enforce", "observe") and allowed:
        candidates = [
            record
            for record in changed_file_records(root)
            if not any(record["path"].startswith(prefix) for prefix in ignore)
            and matches_path(record["path"], allowed)
            and not matches_path(record["path"], blocked)
        ]
        unmapped = unmapped_hunks(root, candidates)
        if unmapped:
            report["unmapped_hunks"] = unmapped
            if hunk_policy == "enforce":
                report["status"] = "fail"
                report["notes"].append(
                    f"{len(unmapped)} unmapped hunk(s) not covered by any file receipt."
                )
            else:
                if report["status"] != "fail":
                    report["status"] = "warning"
                report["notes"].append(
                    f"{len(unmapped)} unmapped hunk(s) detected (observe mode; non-blocking)."
                )

    if not files:
        report["notes"].append("No changed files detected by git status.")

    # #20: enforce-mode stale-attempt note. Purely informational -- it never
    # changes the pass/fail verdict; reclaim-step/fail-step is the operator resolution.
    report["stale_attempts"] = []
    if lease_policy(root) == "enforce":
        state = read_step_state(root)
        now = datetime.now(timezone.utc)
        grace = lease_grace_seconds(root)
        for step_id, step in state["steps"].items():
            for aid in step.get("open_attempts", []):
                attempt = state["attempts"].get(aid, {})
                if attempt.get("lease_expires_at") and attempt_is_expired(attempt, now, grace):
                    report["stale_attempts"].append(
                        {
                            "step_id": step_id,
                            "attempt_id": aid,
                            "owner": attempt.get("agent_id"),
                            "expired_at": attempt.get("lease_expires_at"),
                            "note": "reclaim-step or fail-step",
                        }
                    )
                    report["notes"].append(
                        f"stale attempt {aid} owned by {attempt.get('agent_id')} "
                        f"expired at {attempt.get('lease_expires_at')}; reclaim or fail"
                    )

    return report
