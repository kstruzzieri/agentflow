"""Cross-artifact coverage and referential-integrity checks."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .contracts import recorded_review_depth, review_depth_satisfies


_CRITERION_STATUSES = ("satisfied", "failed", "missing", "unmapped")


def _step_ids(plan: Dict[str, Any]) -> Set[str]:
    return {
        step["id"]
        for step in plan.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }


def build_coverage(
    plan: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    context_receipts: List[Dict[str, Any]],
    runtime_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    steps = _step_ids(plan)
    step_evidence_ids: Set[str] = set()
    step_evidence_by_step: Dict[str, Set[str]] = {}
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        ids = step.get("evidence_ids", [])
        if isinstance(step_id, str):
            step_evidence_by_step[step_id] = set()
        for evidence_id in ids if isinstance(ids, list) else []:
            if isinstance(evidence_id, str):
                step_evidence_ids.add(evidence_id)
                if isinstance(step_id, str):
                    step_evidence_by_step[step_id].add(evidence_id)

    evidence_by_id = {
        item["id"]: item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    plan_evidence_ids = {
        evidence_id
        for evidence_id in plan.get("evidence_ids", [])
        if isinstance(evidence_id, str)
    }

    receipt_steps: Set[str] = set()
    dangling_used_for: List[Dict[str, str]] = []
    for receipt in context_receipts:
        receipt_id = str(receipt.get("id", ""))
        used_for = receipt.get("used_for", [])
        if not isinstance(used_for, list):
            continue
        for step_id in used_for:
            if not isinstance(step_id, str):
                continue
            if step_id in steps:
                receipt_steps.add(step_id)
            else:
                dangling_used_for.append({"receipt_id": receipt_id, "step_id": step_id})

    dangling_supports: List[Dict[str, str]] = []
    supported_by_evidence: Set[str] = set()
    evidence_supporting_steps: Set[str] = set()
    for item in evidence:
        evidence_id = str(item.get("id", ""))
        supports = item.get("supports", [])
        if not isinstance(supports, list):
            continue
        for step_id in supports:
            if not isinstance(step_id, str):
                continue
            if step_id in steps:
                supported_by_evidence.add(step_id)
                evidence_supporting_steps.add(evidence_id)
            else:
                dangling_supports.append({"evidence_id": evidence_id, "step_id": step_id})

    referenced_evidence_ids = plan_evidence_ids | step_evidence_ids
    missing_plan_evidence_ids = sorted(
        evidence_id for evidence_id in referenced_evidence_ids if evidence_id not in evidence_by_id
    )
    used_evidence_ids = step_evidence_ids | plan_evidence_ids | evidence_supporting_steps
    unused_evidence_ids = sorted(
        evidence_id
        for evidence_id, item in evidence_by_id.items()
        if evidence_id not in used_evidence_ids and item.get("kind") != "review"
    )
    steps_with_step_evidence = {
        step_id
        for step_id, evidence_ids in step_evidence_by_step.items()
        if any(evidence_id in evidence_by_id for evidence_id in evidence_ids)
    }
    steps_without_support = sorted(
        step_id
        for step_id in steps
        if step_id not in steps_with_step_evidence
        and step_id not in supported_by_evidence
        and step_id not in receipt_steps
    )

    dangling_route_runtimes: List[Dict[str, str]] = []
    if runtime_config:
        runtimes = runtime_config.get("runtimes", {})
        runtime_ids = set(runtimes.keys()) if isinstance(runtimes, dict) else set()
        routes = runtime_config.get("routes", {})
        default_runtime = runtime_config.get("default_runtime")
        if isinstance(routes, dict):
            for route_name, route in routes.items():
                if not isinstance(route, dict):
                    continue
                primary = route.get("primary")
                primary_field = "primary"
                if primary is None and isinstance(default_runtime, str):
                    primary = default_runtime
                    primary_field = "default_runtime"
                if isinstance(primary, str) and primary not in runtime_ids:
                    dangling_route_runtimes.append(
                        {"route": route_name, "field": primary_field, "runtime": primary}
                    )
                fallbacks = route.get("fallbacks", [])
                if isinstance(fallbacks, list):
                    for fallback in fallbacks:
                        if isinstance(fallback, str) and fallback not in runtime_ids:
                            dangling_route_runtimes.append(
                                {
                                    "route": route_name,
                                    "field": "fallbacks",
                                    "runtime": fallback,
                                }
                            )

    return {
        "steps_without_support": steps_without_support,
        "missing_plan_evidence_ids": missing_plan_evidence_ids,
        "unused_evidence_ids": unused_evidence_ids,
        "dangling_supports": dangling_supports,
        "dangling_used_for": dangling_used_for,
        "dangling_route_runtimes": dangling_route_runtimes,
    }


def _wraps_run(command: Any, run: List[str]) -> bool:
    # env-style wrappers prepend argv (["env", "K=V", *run]); anything that
    # rewrites or extends the gate command is not evidence for that gate.
    return (
        isinstance(command, list)
        and len(command) > len(run)
        and command[-len(run):] == run
    )


def _latest_command_receipt(
    step_id: str,
    gate: Dict[str, Any],
    validation_alias: Optional[str],
    receipts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    run = gate.get("run")
    if not isinstance(run, list):
        return None
    aliases = {item for item in (" ".join(run), validation_alias) if item}
    for receipt in reversed(receipts):
        if receipt.get("step_id") != step_id:
            continue
        command = receipt.get("command")
        if command == run:
            return receipt
        # A gate label alone is not criterion proof: the labeled receipt must
        # still have run the gate command (allowing env-prefix wrapping).
        if receipt.get("gate") in aliases and _wraps_run(command, run):
            return receipt
    return None


def _combined_status(statuses: List[str]) -> str:
    for status in ("failed", "missing", "unmapped"):
        if status in statuses:
            return status
    return "satisfied"


def build_requirement_coverage(
    plan: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    receipts: List[Dict[str, Any]],
    review_runs: List[Dict[str, Any]],
    plan_sha256: str,
) -> Dict[str, Any]:
    """Project optional plan criteria onto explicitly mapped deterministic evidence."""
    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        return {}

    steps_by_criterion: Dict[str, List[str]] = {}
    evidence_by_criterion: Dict[str, List[Dict[str, Any]]] = {}
    recorded_evidence_ids = {
        item["id"]
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            continue
        step_id = step["id"]
        for criterion_id in step.get("criterion_ids", []):
            if isinstance(criterion_id, str):
                steps_by_criterion.setdefault(criterion_id, []).append(step_id)
        validation = step.get("validation", [])
        for gate_index, gate in enumerate(step.get("gates", [])):
            if not isinstance(gate, dict):
                continue
            if gate.get("kind") == "inspection":
                evidence_id = gate.get("evidence_id")
                status = (
                    "satisfied" if evidence_id in recorded_evidence_ids else "missing"
                )
                for criterion_id in gate.get("criterion_ids", []):
                    if isinstance(criterion_id, str):
                        evidence_by_criterion.setdefault(criterion_id, []).append(
                            {
                                "kind": "inspection",
                                "step_id": step_id,
                                "evidence_id": evidence_id,
                                "status": status,
                            }
                        )
                continue
            if gate.get("kind") != "command":
                continue
            run = gate.get("run")
            if not isinstance(run, list):
                continue
            validation_alias = (
                validation[gate_index]
                if isinstance(validation, list)
                and gate_index < len(validation)
                and isinstance(validation[gate_index], str)
                else None
            )
            receipt = _latest_command_receipt(
                step_id, gate, validation_alias, receipts
            )
            status = "missing"
            if receipt is not None:
                failed = (
                    receipt.get("exit_code") != 0
                    or receipt.get("decision") in ("blocked", "timeout")
                    or receipt.get("timed_out") is True
                )
                status = "failed" if failed else "satisfied"
            for criterion_id in gate.get("criterion_ids", []):
                if not isinstance(criterion_id, str):
                    continue
                row = {
                    "kind": "command",
                    "step_id": step_id,
                    "gate": " ".join(run),
                    "status": status,
                }
                if receipt is not None and isinstance(receipt.get("id"), str):
                    row["receipt_id"] = receipt["id"]
                evidence_by_criterion.setdefault(criterion_id, []).append(row)

    projected_requirements: List[Dict[str, Any]] = []
    counts = {status: 0 for status in _CRITERION_STATUSES}
    for requirement in requirements:
        projected_criteria: List[Dict[str, Any]] = []
        for criterion in requirement.get("acceptance_criteria", []):
            criterion_id = criterion["id"]
            criterion_evidence = list(evidence_by_criterion.get(criterion_id, []))
            review = criterion.get("review")
            if isinstance(review, dict):
                minimum_depth = review["minimum_depth"]
                qualifying = [
                    run
                    for run in review_runs
                    if run.get("plan_sha256") == plan_sha256
                    and review_depth_satisfies(
                        recorded_review_depth(run.get("depth_profile")), minimum_depth
                    )
                ]
                review_row: Dict[str, Any] = {
                    "kind": "review",
                    "minimum_depth": minimum_depth,
                    "status": "missing",
                }
                if qualifying:
                    run = qualifying[-1]
                    review_row.update(
                        {
                            "status": (
                                "satisfied"
                                if run.get("gate_status") == "pass"
                                and not run.get("active_blocking")
                                else "failed"
                            ),
                            "review_run_id": run.get("review_run_id"),
                            "plan_sha256": plan_sha256,
                            "depth_profile": recorded_review_depth(
                                run.get("depth_profile")
                            ),
                        }
                    )
                criterion_evidence.append(review_row)
            status = (
                _combined_status([row["status"] for row in criterion_evidence])
                if criterion_evidence
                else "unmapped"
            )
            counts[status] += 1
            projected_criteria.append(
                {
                    "id": criterion_id,
                    "text": criterion["text"],
                    "status": status,
                    "steps": steps_by_criterion.get(criterion_id, []),
                    "evidence": criterion_evidence,
                }
            )
        projected_requirements.append(
            {
                "id": requirement["id"],
                "text": requirement["text"],
                "status": _combined_status(
                    [criterion["status"] for criterion in projected_criteria]
                ),
                "acceptance_criteria": projected_criteria,
            }
        )
    return {
        "requirements": projected_requirements,
        "criterion_status_counts": counts,
    }


def build_design_decision_coverage(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Project optional design decisions in canonical plan order."""
    decisions = plan.get("design_decisions")
    if not isinstance(decisions, list) or not decisions:
        return {}

    steps_by_decision: Dict[str, List[str]] = {}
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            continue
        for decision_id in step.get("design_decision_ids", []):
            if isinstance(decision_id, str):
                steps_by_decision.setdefault(decision_id, []).append(step["id"])

    return {
        "design_decisions": [
            {
                "id": decision["id"],
                "text": decision["text"],
                "references": list(decision.get("references", [])),
                "steps": list(steps_by_decision.get(decision["id"], [])),
            }
            for decision in decisions
        ]
    }


def evaluate_context_budget(
    plan: Dict[str, Any],
    context_receipts: List[Dict[str, Any]],
    failures: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    budget = plan.get("context_budget", {})
    if not isinstance(budget, dict):
        return []

    findings: List[Dict[str, Any]] = []
    total_bytes = sum(
        item.get("bytes", 0)
        for item in context_receipts
        if isinstance(item.get("bytes", 0), int)
    )
    distinct_sources = {
        item.get("source")
        for item in context_receipts
        if isinstance(item.get("source"), str)
    }

    max_total_bytes = budget.get("max_total_bytes")
    if isinstance(max_total_bytes, int) and total_bytes > max_total_bytes:
        findings.append(
            {
                "id": "context_max_total_bytes_exceeded",
                "status": "warning",
                "severity": "warning",
                "message": (
                    f"context receipts total {total_bytes} bytes exceeds "
                    f"max_total_bytes {max_total_bytes}"
                ),
            }
        )

    max_files = budget.get("max_files")
    if isinstance(max_files, int) and len(distinct_sources) > max_files:
        findings.append(
            {
                "id": "context_max_files_exceeded",
                "status": "warning",
                "severity": "warning",
                "message": (
                    f"context receipts use {len(distinct_sources)} sources "
                    f"exceeding max_files {max_files}"
                ),
            }
        )

    max_log_lines = budget.get("max_log_lines_per_failure")
    if isinstance(max_log_lines, int):
        for failure in failures:
            relevant_lines = failure.get("relevant_lines", [])
            if isinstance(relevant_lines, list) and len(relevant_lines) > max_log_lines:
                findings.append(
                    {
                        "id": "failure_log_lines_exceeded",
                        "status": "warning",
                        "severity": "info",
                        "message": (
                            f"failure for {failure.get('command', 'unknown')} records "
                            f"{len(relevant_lines)} relevant lines exceeding "
                            f"max_log_lines_per_failure {max_log_lines}"
                        ),
                    }
                )

    return findings
