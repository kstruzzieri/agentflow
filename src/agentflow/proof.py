"""Proof-pack metadata and verification helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .artifacts import (
    plan_binding_sha256,
    historical_proof_reads,
    read_json,
    read_jsonl,
    try_read_json,
    utc_now,
    write_json,
)
from .contracts import (
    AGGREGATION_SCHEMA_VERSION,
    ARTIFACT_PATHS,
    EXECUTION_ARTIFACT_PATHS,
    MCP_SERVER_STATUSES,
    PROOF_PACK_SCHEMA_VERSION,
)
from .coverage import build_coverage, build_requirement_coverage, evaluate_context_budget
from .execution import read_step_events, read_step_state
from .execution_coverage import build_execution_coverage
from .receipts import command_receipts, file_receipts, replay_gates, verify_receipt_outputs
from .review import (
    build_time_review_policy,
    review_checks,
    review_summary,
    verify_review_integrity,
)
from .workflow_contract import (
    WORKFLOW_CONTRACT_PATH,
    validate_workflow_contract,
    workflow_contract_summary,
)
from .capabilities import capability_checks, capability_summary
from .stuck import stuck_block
from .validation import validate_requirement_traceability
from .versioning import (
    parse_schema_version,
    validate_historical_proof_schema_version,
    validate_schema_version,
)


PROOF_METADATA_FIELDS = {
    "schema_version": str,
    "bundle_version": str,
    "meta": dict,
    "generated_from": list,
    "files": list,
    "checks": list,
    "coverage": dict,
    "core_sha256": str,
}

_AGGREGATION_SOURCE_ID_RE = re.compile(r"^[a-z0-9]{1,16}$")
_AGGREGATION_PREFIX_RE = re.compile(r"^WT[a-z0-9]{1,16}-$")
# A total cap of 640 keeps every numeric component below Python's minimum
# configurable decimal-conversion threshold. Both published schemas mirror it.
_AGGREGATION_SCHEMA_VERSION_MAX_LENGTH = 640


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_files(root: Path) -> List[str]:
    return [path for path in sorted(ARTIFACT_PATHS.values()) if (root / path).exists()]


def _aggregation_manifest_errors(manifest: Dict[str, Any]) -> List[str]:
    """Structural validation for an embedded aggregation manifest.

    Runtime twin of schemas/aggregation.schema.json (a test-time contract that
    is never loaded at runtime); extra keys are allowed (additive).
    """
    schema_version = manifest.get("schema_version")
    errors: List[str] = []
    if (
        isinstance(schema_version, str)
        and len(schema_version) > _AGGREGATION_SCHEMA_VERSION_MAX_LENGTH
    ):
        errors.append(
            "aggregation schema_version must be at most "
            f"{_AGGREGATION_SCHEMA_VERSION_MAX_LENGTH} characters"
        )
    else:
        errors.extend(
            validate_schema_version(
                schema_version, AGGREGATION_SCHEMA_VERSION, "aggregation"
            )
        )
    if manifest.get("mode") != "cross_worktree":
        errors.append("mode must be cross_worktree")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must be a non-empty list")
    else:
        for index, entry in enumerate(sources):
            if not isinstance(entry, dict):
                errors.append(f"sources[{index}] is not an object")
                continue
            source_id = entry.get("source_id")
            if not isinstance(source_id, str):
                errors.append(f"sources[{index}].source_id missing or not a string")
            elif not _AGGREGATION_SOURCE_ID_RE.fullmatch(source_id):
                errors.append(f"sources[{index}].source_id does not match schema pattern")
            root_label = entry.get("root_label")
            if not isinstance(root_label, str):
                errors.append(f"sources[{index}].root_label missing or not a string")
            namespaced_prefix = entry.get("namespaced_prefix")
            if not isinstance(namespaced_prefix, str):
                errors.append(
                    f"sources[{index}].namespaced_prefix missing or not a string"
                )
            elif not _AGGREGATION_PREFIX_RE.fullmatch(namespaced_prefix):
                errors.append(
                    f"sources[{index}].namespaced_prefix does not match schema pattern"
                )
            for key in ("base_commit", "head_commit"):
                if key not in entry or not isinstance(entry[key], (str, type(None))):
                    errors.append(f"sources[{index}].{key} missing or not a string/null")
    source_count = manifest.get("source_count")
    if isinstance(source_count, bool) or not isinstance(source_count, int) or source_count < 1:
        errors.append("source_count must be a positive integer")
    elif isinstance(sources, list) and source_count != len(sources):
        errors.append("source_count does not match len(sources)")
    return errors


def _normalized_execution_hash(root: Path) -> str:
    payload: Dict[str, Any] = {}
    volatile = {"started_at", "finished_at", "recorded_at", "lease_expires_at"}
    for name, relative_path in EXECUTION_ARTIFACT_PATHS.items():
        path = root / relative_path
        if not path.exists():
            continue
        if path.suffix == ".jsonl":
            payload[name] = [
                {key: value for key, value in row.items() if key not in volatile}
                for row in read_jsonl(path)
            ]
        else:
            payload[name] = read_json(path)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _status_counts(entries: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("status"), str):
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def runtime_block(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Summarize the latest recorded runtime snapshot for the proof core.

    Read-only (#19): never probes, never calls build_runtime_status. Returns
    (block, None) on success, (None, None) when no ledger/snapshot exists, and
    (None, reason) when the ledger is unreadable or malformed (P2b) -- the
    caller emits a runtime_snapshot_readable warning instead of crashing.
    """
    path = root / ".agent/runtime-snapshots.jsonl"
    if not path.exists():
        return None, None
    try:
        snapshots = read_jsonl(path)
    except ValueError as exc:
        return None, str(exc)
    if not snapshots:
        return None, None
    latest = snapshots[-1]
    if not isinstance(latest, dict) or not isinstance(latest.get("runtimes"), list):
        return None, "latest runtime snapshot is not a valid snapshot object"
    runtime_config_path = root / ".agent/runtime.config.json"
    if runtime_config_path.exists():
        snapshot_hash = latest.get("runtime_config_sha256")
        if not isinstance(snapshot_hash, str) or snapshot_hash != sha256_file(runtime_config_path):
            return None, "stale runtime snapshot: runtime config hash mismatch"
    mcp_entries = latest.get("mcp_servers", [])
    if not isinstance(mcp_entries, list):
        mcp_entries = []
    mcp_rows = []
    for entry in mcp_entries:
        if not isinstance(entry, dict):
            continue
        row_id = entry.get("id", "unknown")
        row_status = entry.get("status", "unavailable")
        if not isinstance(row_id, str) or row_status not in MCP_SERVER_STATUSES:
            return None, "latest runtime snapshot has malformed mcp server rows"
        count = entry.get("declared_tool_count", 0)
        mcp_rows.append(
            {
                "id": row_id,
                "status": row_status,
                "declared_tool_count": count if isinstance(count, int) else 0,
            }
        )
    return (
        {
            "latest_snapshot_id": latest.get("id", "unknown"),
            "runtime_counts": _status_counts(latest.get("runtimes")),
            "mcp_server_counts": _status_counts(mcp_entries),
            "mcp_servers": sorted(mcp_rows, key=lambda row: row["id"]),
        },
        None,
    )


def execution_summary(root: Path, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not (root / EXECUTION_ARTIFACT_PATHS["execution-contract"]).exists():
        return None
    state = read_step_state(root)
    commands = command_receipts(root)
    files = file_receipts(root)
    provenance_counts: Dict[str, int] = {}
    decision_counts: Dict[str, int] = {}
    timeout_seconds_counts: Dict[str, int] = {}
    risk_counts: Dict[str, int] = {}
    finding_categories: Dict[str, int] = {}
    confirmed_high_risk = 0
    timed_out = 0
    for receipt in commands:
        provenance = str(receipt.get("provenance", "unknown"))
        provenance_counts[provenance] = provenance_counts.get(provenance, 0) + 1
        decision = receipt.get("decision")
        if decision:
            decision_counts[decision] = decision_counts.get(decision, 0) + 1
        if receipt.get("decision") == "timeout" or receipt.get("timed_out") is True:
            timed_out += 1
            timeout_seconds = receipt.get("timeout_seconds")
            if isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool):
                key = str(timeout_seconds)
                timeout_seconds_counts[key] = timeout_seconds_counts.get(key, 0) + 1
        risk = receipt.get("risk") or {}
        risk_level = risk.get("level")
        if risk_level:
            risk_counts[risk_level] = risk_counts.get(risk_level, 0) + 1
        if receipt.get("confirmed") and risk_level == "high":
            confirmed_high_risk += 1
        for finding in risk.get("findings", []):
            category = finding.get("category")
            if category:
                finding_categories[category] = finding_categories.get(category, 0) + 1
    verification_runs = read_jsonl(root / EXECUTION_ARTIFACT_PATHS["verification-runs"])
    amendments = [
        {
            "step_id": event.get("step_id"),
            "attempt": event.get("attempt_id"),
            "amends_attempt": event.get("amends_attempt"),
            "reason": event.get("reason"),
            "reason_code": event.get("reason_code"),
            "amends_completed_at": event.get("amends_completed_at"),
        }
        for event in read_step_events(root)
        if event.get("event") == "amendment_started"
    ]
    drift_report_path = root / ".agent/drift-report.json"
    unmapped_changed_files: List[str] = []
    if drift_report_path.exists():
        drift_report = read_json(drift_report_path)
        unmapped_paths = {
            entry["path"]
            for entry in drift_report.get("unmapped_hunks", [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        unmapped_changed_files = sorted(unmapped_paths)
    return {
        "steps_total": len(plan.get("steps", [])),
        "amendments": amendments,
        "steps_completed": len(
            [step for step in state["steps"].values() if step.get("completed")]
        ),
        "attempts": len(state["attempts"]),
        "command_receipts": len(commands),
        "command_receipts_by_provenance": provenance_counts,
        "command_decision_counts": decision_counts,
        "command_timed_out": timed_out,
        "command_timeout_seconds": timeout_seconds_counts,
        "command_risk_counts": risk_counts,
        "command_confirmed_high_risk": confirmed_high_risk,
        "command_finding_categories": finding_categories,
        "file_receipts": len(files),
        "verification_runs": len(verification_runs),
        "normalized_execution_sha256": _normalized_execution_hash(root),
        "unmapped_changed_files": unmapped_changed_files,
        "failed_attempts": [
            attempt_id
            for attempt_id, attempt in state["attempts"].items()
            if attempt.get("status") == "failed"
        ],
    }


def criterion_check_from_coverage(
    coverage: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    criterion_counts = coverage.get("criterion_status_counts")
    if not isinstance(criterion_counts, dict):
        return None
    unsatisfied = sum(
        criterion_counts.get(status, 0)
        for status in ("failed", "missing", "unmapped")
    )
    return {
        "id": "criteria_satisfied",
        "status": "failed" if unsatisfied else "passed",
        "count": unsatisfied,
        "message": (
            f"{unsatisfied} acceptance criteria are not satisfied"
            if unsatisfied
            else "all acceptance criteria are satisfied"
        ),
    }


def check_from_coverage(
    coverage: Dict[str, Any],
    receipts_required: bool = False,
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    mapping = [
        ("missing_plan_evidence_ids", "warning"),
        ("unused_evidence_ids", "warning"),
        ("steps_without_support", "failed" if receipts_required else "warning"),
        ("dangling_supports", "failed"),
        ("dangling_used_for", "failed"),
        ("dangling_route_runtimes", "failed"),
    ]
    for key, bad_status in mapping:
        values = coverage.get(key, [])
        checks.append(
            {
                "id": key,
                "status": bad_status if values else "passed",
                "count": len(values),
            }
        )
    criterion_check = criterion_check_from_coverage(coverage)
    if criterion_check is not None:
        checks.append(criterion_check)
    return checks


def canonical_core(proof: Dict[str, Any]) -> Dict[str, Any]:
    core = {
        "generated_from": proof.get("generated_from", []),
        "files": proof.get("files", []),
        "checks": proof.get("checks", []),
        "coverage": proof.get("coverage", {}),
        "review": proof.get("review", {}),
        "capabilities": proof.get("capabilities", {}),
        "stuck": proof.get("stuck", {}),
    }
    if "workflow_contract" in proof:
        core["workflow_contract"] = proof.get("workflow_contract")
    # #19: conditional like workflow_contract -- pre-0.6.0 proofs without the
    # block keep their core hash and continue to verify (#82 growth path).
    if "runtime" in proof:
        core["runtime"] = proof.get("runtime")
    # #112: conditional like runtime -- single-tree proofs without the block
    # keep their core hash byte-identical (#82 growth path).
    if "aggregation" in proof:
        core["aggregation"] = proof.get("aggregation")
    return core


def core_sha256(proof: Dict[str, Any]) -> str:
    payload = json.dumps(canonical_core(proof), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recorded_schema_is_older(recorded: Any, current: str) -> bool:
    # #82: only a strictly-older, well-formed MAJOR.MINOR.PATCH version proves
    # schema growth. Anything unparseable falls through to the generic (tamper)
    # message -- an unreadable version cannot vouch for the bundle.
    if not isinstance(recorded, str):
        return False
    try:
        recorded_v = parse_schema_version(recorded)
        current_v = parse_schema_version(current)
    except ValueError:
        return False
    return (recorded_v.major, recorded_v.minor, recorded_v.patch) < (
        current_v.major,
        current_v.minor,
        current_v.patch,
    )


def verify_proof_checks(proof: Dict[str, Any], strict: bool = False) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for check in proof.get("checks", []):
        if not isinstance(check, dict):
            findings.append({"severity": "error", "message": "proof check entry is malformed"})
            continue
        status = check.get("status")
        check_id = check.get("id", "unknown")
        message = check.get("message", status)
        if status == "failed":
            findings.append({"severity": "error", "message": f"{check_id}: {message}"})
        elif status == "warning":
            severity = "error" if strict else "warning"
            findings.append({"severity": severity, "message": f"{check_id}: {message}"})
    return findings


def _verify_requirement_coverage(
    root: Path, proof: Dict[str, Any]
) -> List[Dict[str, Any]]:
    keys = ("requirements", "criterion_status_counts")
    recorded_coverage = proof.get("coverage", {})
    recorded = {
        key: recorded_coverage[key]
        for key in keys
        if isinstance(recorded_coverage, dict) and key in recorded_coverage
    }
    plan_path = root / ".agent/plan.lock.json"
    if not plan_path.exists():
        return []
    try:
        plan = read_json(plan_path)
        traceability_errors = validate_requirement_traceability(plan)
        if traceability_errors:
            raise ValueError(
                "invalid requirement traceability: "
                + "; ".join(traceability_errors)
            )
        if not plan.get("requirements"):
            expected = {}
        else:
            evidence = read_jsonl(root / ".agent/evidence.jsonl")
            review = review_summary(root, plan)
            expected = build_requirement_coverage(
                plan,
                evidence,
                command_receipts(root),
                review["review_runs"],
                plan_binding_sha256(plan),
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            {
                "severity": "error",
                "message": f"criterion coverage could not be recomputed: {exc}",
            }
        ]
    # #82 growth path: a proof built before criterion coverage existed cannot
    # record it, which is indistinguishable from a tampered removal. When the
    # recorded schema_version is older than the current one, point the holder
    # at schema growth instead of implying the proof was altered.
    growth_hint = ""
    if _recorded_schema_is_older(proof.get("schema_version"), PROOF_PACK_SCHEMA_VERSION):
        growth_hint = (
            ": proof was built by an older schema version "
            f"({proof.get('schema_version')} < {PROOF_PACK_SCHEMA_VERSION}); "
            "rebuild with current Agentflow to re-verify"
        )
    if recorded != expected:
        return [
            {
                "severity": "error",
                "message": "proof criterion coverage is stale or tampered" + growth_hint,
            }
        ]
    recorded_checks = [
        check
        for check in proof.get("checks", [])
        if isinstance(check, dict) and check.get("id") == "criteria_satisfied"
    ]
    expected_check = criterion_check_from_coverage(expected)
    if recorded_checks != ([expected_check] if expected_check is not None else []):
        return [
            {
                "severity": "error",
                "message": "proof criteria_satisfied check is stale or tampered" + growth_hint,
            }
        ]
    return []


def build_proof(root: Path, plan_path: Path, strict: bool = False) -> Dict[str, Any]:
    resolved_root = root.resolve()
    resolved_plan_path = plan_path.resolve()
    try:
        plan_source = resolved_plan_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError("plan path escapes root") from exc
    plan = read_json(resolved_plan_path)
    traceability_errors = validate_requirement_traceability(plan)
    if traceability_errors:
        raise ValueError(
            "invalid requirement traceability: " + "; ".join(traceability_errors)
        )
    evidence = read_jsonl(root / ".agent/evidence.jsonl")
    context_receipts = read_jsonl(root / ".agent/context-receipts.jsonl")
    failures = read_jsonl(root / ".agent/failures.jsonl")
    review = review_summary(root, plan)
    runtime_config_path = root / ".agent/runtime.config.json"
    runtime_config: Optional[Dict[str, Any]]
    runtime_config_warning = None
    if runtime_config_path.exists():
        runtime_config, runtime_config_warning = try_read_json(runtime_config_path)
    else:
        runtime_config = None
    coverage = build_coverage(plan, evidence, context_receipts, runtime_config)
    coverage.update(
        build_requirement_coverage(
            plan,
            evidence,
            command_receipts(root),
            review["review_runs"],
            plan_binding_sha256(plan),
        )
    )
    # #20: merge lease diagnostics into the coverage block so they land inside
    # canonical_core (proof["execution"] is NOT hash-bound). Lease-expiry status
    # is evaluated at build time and may legitimately differ before vs. after an
    # expiry; the raw timestamp fields stay volatile-excluded from
    # normalized_execution_sha256, so wall-clock churn never affects that hash.
    if (root / EXECUTION_ARTIFACT_PATHS["execution-contract"]).exists():
        execution_coverage = build_execution_coverage(root, plan)
        for key in ("expired_leases", "no_deadline_open_attempts", "abandoned_attempts"):
            coverage[key] = execution_coverage.get(key, [])
    budget = plan.get("context_budget", {})
    receipts_required = bool(budget.get("receipts_required")) if isinstance(budget, dict) else False
    checks = check_from_coverage(coverage, receipts_required)
    if runtime_config_warning:
        checks.append(
            {
                "id": "runtime_config_readable",
                "status": "warning",
                "message": runtime_config_warning,
            }
        )
    checks.extend(evaluate_context_budget(plan, context_receipts, failures))

    drift_path = root / ".agent/drift-report.json"
    if drift_path.exists():
        drift = read_json(drift_path)
        checks.append(
            {
                "id": "drift_audit",
                "status": "failed" if drift.get("status") == "fail" else "passed",
                "drift_status": drift.get("status", "missing"),
            }
        )
    else:
        checks.append({"id": "drift_audit", "status": "not_run", "drift_status": "missing"})

    workflow_summary: Optional[Dict[str, Any]] = None
    workflow_path = root / WORKFLOW_CONTRACT_PATH
    if workflow_path.exists():
        workflow_contract, workflow_read_error = try_read_json(workflow_path)
        if workflow_contract is None:
            checks.append(
                {
                    "id": "workflow_contract_valid",
                    "status": "failed",
                    "message": workflow_read_error,
                }
            )
        else:
            workflow_errors = validate_workflow_contract(workflow_contract)
            if workflow_errors:
                checks.append(
                    {
                        "id": "workflow_contract_valid",
                        "status": "failed",
                        "message": "; ".join(workflow_errors),
                    }
                )
            else:
                workflow_summary = workflow_contract_summary(workflow_contract)
                checks.append({"id": "workflow_contract_valid", "status": "passed"})

    generated_from = sorted(set(artifact_files(root) + [plan_source]))
    files = [
        {"path": relative_path, "sha256": sha256_file(root / relative_path)}
        for relative_path in generated_from
    ]
    proof = {
        "schema_version": PROOF_PACK_SCHEMA_VERSION,
        "bundle_version": PROOF_PACK_SCHEMA_VERSION,
        "bundle_type": "agentflow_proof_pack",
        "meta": {"created_at": utc_now(), "tool_version": __version__},
        "generated_from": generated_from,
        "files": files,
        "checks": checks,
        "coverage": coverage,
    }
    if workflow_summary is not None:
        proof["workflow_contract"] = workflow_summary
    summary = execution_summary(root, plan)
    if summary is not None:
        proof["execution"] = summary
    review["policy"] = build_time_review_policy(root, strict)
    proof["review"] = review
    proof["checks"].extend(review_checks(root, review, strict))

    required_caps = (
        list(workflow_summary.get("required_capabilities", []))
        if workflow_summary is not None
        else []
    )
    try:
        cap_summary = capability_summary(root, required_caps)
        proof["capabilities"] = cap_summary
        proof["checks"].extend(capability_checks(cap_summary))
    except ValueError as exc:
        proof["capabilities"] = {
            "required": required_caps,
            "recorded": [],
            "waived": [],
            "missing": sorted(set(required_caps)),
        }
        proof["checks"].append(
            {
                "id": "capability_receipts_valid",
                "status": "failed",
                "message": str(exc),
            }
        )

    runtime_summary, runtime_warning = runtime_block(root)
    if runtime_summary is not None:
        proof["runtime"] = runtime_summary
    elif runtime_warning is not None:
        proof["checks"].append(
            {
                "id": "runtime_snapshot_readable",
                "status": "warning",
                "message": runtime_warning,
            }
        )

    # #112: aggregation.json is emitted by aggregate-ledgers (cross-worktree
    # merge), never by this build -- provenance is declarative, embedded
    # as-is. The file's hash is already covered by generated_from/files via
    # artifact_files() (it walks ARTIFACT_PATHS), so verify-proof re-checks
    # the manifest for tamper generically; no extra hashing here.
    # _aggregation_manifest_errors is the runtime twin of the schema contract
    # (schemas/aggregation.schema.json), which is never loaded at runtime.
    aggregation_path = root / ARTIFACT_PATHS["aggregation"]
    if aggregation_path.exists():
        aggregation, aggregation_error = try_read_json(aggregation_path)
        if aggregation is None:
            proof["checks"].append(
                {
                    "id": "aggregation_valid",
                    "status": "failed",
                    "message": aggregation_error,
                }
            )
        else:
            manifest_errors = _aggregation_manifest_errors(aggregation)
            if manifest_errors:
                proof["checks"].append(
                    {
                        "id": "aggregation_valid",
                        "status": "failed",
                        "message": "; ".join(manifest_errors),
                    }
                )
            else:
                proof["aggregation"] = aggregation

    proof["stuck"] = stuck_block(root, plan)
    proof["core_sha256"] = core_sha256(proof)
    return proof


def write_proof_metadata(root: Path, proof: Dict[str, Any]) -> Path:
    output = root / ".agent/proof-pack.json"
    write_json(output, proof)
    return output


def render_markdown(
    plan: Dict[str, Any],
    proof: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    drift: Dict[str, Any],
) -> str:
    def markdown_list(items: List[Any], empty: str = "None.") -> str:
        if not items:
            return empty
        return "\n".join(f"- {item}" for item in items)

    steps = [
        f"{step.get('id', 'unknown')}: {step.get('action', '')}"
        for step in plan.get("steps", [])
    ]
    validation = list(plan.get("validation_gates", []))
    for step in plan.get("steps", []):
        validation.extend(step.get("validation", []))

    # #74: surface the effective adaptive review policy (display-only; the JSON
    # checks are authoritative).
    review_block = proof.get("review")
    review_policy_lines: List[Any] = []
    if isinstance(review_block, dict) and isinstance(review_block.get("policy"), dict):
        rp = review_block["policy"]
        recorded = "yes" if review_block.get("latest_review_run_id") else "no"
        review_policy_lines = [
            f"required_review_depth: {rp.get('required_review_depth')}",
            f"review_gate_effective: {rp.get('review_gate_effective')}",
            f"require_review_run: {rp.get('require_review_run')}",
            f"review_run_recorded: {recorded}",
        ]

    requirement_lines: List[str] = []
    for requirement in proof.get("coverage", {}).get("requirements", []):
        requirement_lines.append(
            f"{requirement.get('id')} [{requirement.get('status')}]: "
            f"{requirement.get('text')}"
        )
        for criterion in requirement.get("acceptance_criteria", []):
            evidence_summary = ", ".join(
                f"{item.get('kind')}={item.get('status')}"
                for item in criterion.get("evidence", [])
            ) or "none"
            steps_summary = ", ".join(criterion.get("steps", [])) or "none"
            requirement_lines.append(
                f"{criterion.get('id')} [{criterion.get('status')}]: "
                f"{criterion.get('text')} "
                f"(steps: {steps_summary}; evidence: {evidence_summary})"
            )
    requirement_section = (
        ["", "## Requirement Coverage", "", markdown_list(requirement_lines)]
        if requirement_lines
        else []
    )

    return "\n".join(
        [
            "# Proof Pack",
            "",
            "## Objective",
            "",
            plan.get("objective") or "No objective recorded.",
            "",
            "## Scope",
            "",
            markdown_list(plan.get("scope", [])),
            "",
            "## Workflow Contract",
            "",
            markdown_list(
                [
                    f"workflow_pack: {proof['workflow_contract'].get('workflow_pack')}",
                    f"workflow_profile: {proof['workflow_contract'].get('workflow_profile')}",
                    f"review_depth: {proof['workflow_contract'].get('review_depth')}",
                    (
                        "required_capabilities: "
                        + ", ".join(proof["workflow_contract"].get("required_capabilities", []))
                    ),
                ]
                if isinstance(proof.get("workflow_contract"), dict)
                else []
            ),
            "",
            "## Review Policy",
            "",
            markdown_list(review_policy_lines),
            "",
            "## Plan Steps Completed",
            "",
            markdown_list(steps),
            "",
            "## Evidence",
            "",
            markdown_list(
                [f"{item.get('id')}: {item.get('claim')} ({item.get('source')})" for item in evidence]
            ),
            "",
            "## Coverage",
            "",
            markdown_list(
                [
                    f"{key}: {len(value) if isinstance(value, list) else value}"
                    for key, value in proof.get("coverage", {}).items()
                ]
            ),
            *requirement_section,
            "",
            "## Validation",
            "",
            markdown_list(validation),
            "",
            "## Drift Audit",
            "",
            f"Status: {drift.get('status', 'missing')}",
            "",
            markdown_list(drift.get("notes", [])),
            "",
        ]
    )


def _verify_proof(
    root: Path, proof_path: Path, replay: bool = False, strict: bool = False
) -> List[Dict[str, Any]]:
    proof, read_error = try_read_json(proof_path)
    if proof is None:
        return [{"severity": "error", "message": read_error}]
    if "schema_version" in proof:
        schema_errors = validate_historical_proof_schema_version(
            proof.get("schema_version"), PROOF_PACK_SCHEMA_VERSION
        )
        if schema_errors:
            # Intentional precedence (#82): a newer-schema proof early-returns
            # the "upgrade Agentflow" diagnostic without running the shape or
            # core_sha256 checks, whose composition this verifier cannot know
            # for a future schema. This never weakens integrity: the finding is
            # severity=error, so the proof is rejected either way, and a
            # supported-schema proof always reaches the core tamper check.
            return [{"severity": "error", "message": error} for error in schema_errors]
    findings: List[Dict[str, Any]] = []
    for field, expected_type in PROOF_METADATA_FIELDS.items():
        value = proof.get(field)
        if value is None:
            findings.append(
                {"severity": "error", "message": f"proof metadata missing required field {field}"}
            )
        elif not isinstance(value, expected_type):
            findings.append(
                {"severity": "error", "message": f"proof metadata field {field} has invalid type"}
            )
    if findings:
        return findings
    resolved_root = root.resolve()
    generated_paths = set()
    for path in proof["generated_from"]:
        if isinstance(path, str):
            generated_paths.add(path)
        else:
            findings.append({"severity": "error", "message": "proof generated_from entry is malformed"})
    file_paths = set()
    for item in proof["files"]:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            file_paths.add(item["path"])
    missing_hashes = sorted(generated_paths - file_paths)
    unexpected_hashes = sorted(file_paths - generated_paths)
    if missing_hashes or unexpected_hashes:
        details = []
        if missing_hashes:
            details.append(f"missing hashes for: {', '.join(missing_hashes)}")
        if unexpected_hashes:
            details.append(f"unexpected hashes for: {', '.join(unexpected_hashes)}")
        findings.append(
            {
                "severity": "error",
                "message": "proof generated_from/files path mismatch: " + "; ".join(details),
            }
        )
    for item in proof.get("files", []):
        if not isinstance(item, dict):
            findings.append({"severity": "error", "message": "proof file entry is malformed"})
            continue
        relative_path = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
            findings.append({"severity": "error", "message": "proof file entry is malformed"})
            continue
        path = root / relative_path
        try:
            resolved_path = path.resolve(strict=False)
            resolved_path.relative_to(resolved_root)
        except ValueError:
            findings.append({"severity": "error", "message": f"proof path escapes root: {relative_path}"})
            continue
        if not path.exists():
            findings.append({"severity": "error", "message": f"missing source {relative_path}"})
            continue
        actual_hash = sha256_file(resolved_path)
        if actual_hash != expected_hash:
            findings.append({"severity": "error", "message": f"hash mismatch for {relative_path}"})
    # ratchet-v1: honor the build-time policy floor baked into the proof (tamper-evident via core_sha256)
    recorded_review_policy = None
    review_block = proof.get("review")
    if isinstance(review_block, dict) and isinstance(review_block.get("policy"), dict):
        recorded_review_policy = review_block["policy"]
    recorded_strict = bool(
        isinstance(recorded_review_policy, dict)
        and recorded_review_policy.get("proof_strict_effective")
    )
    effective_proof_strict = strict or recorded_strict

    expected_core = proof.get("core_sha256")
    if isinstance(expected_core, str) and core_sha256(proof) != expected_core:
        # #82: canonical_core grows over releases (workflow_contract, capabilities,
        # ...), so a bundle built by an older Agentflow re-hashes to a different
        # core today. The mismatch is indistinguishable from tampering, so this
        # stays a hard error -- but when the recorded schema_version is older than
        # the current one, point the holder at schema growth instead of implying
        # the proof was altered.
        message = "proof canonical core checksum mismatch"
        if _recorded_schema_is_older(proof.get("schema_version"), PROOF_PACK_SCHEMA_VERSION):
            message = (
                "proof canonical core checksum mismatch: proof was built by an "
                f"older schema version ({proof.get('schema_version')} < "
                f"{PROOF_PACK_SCHEMA_VERSION}); rebuild with current Agentflow to re-verify"
            )
        findings.append({"severity": "error", "message": message})
    findings.extend(_verify_requirement_coverage(root, proof))
    findings.extend(verify_proof_checks(proof, effective_proof_strict))
    findings.extend(verify_receipt_outputs(root))
    findings.extend(verify_review_integrity(root, strict, recorded_review_policy))
    if replay and (root / EXECUTION_ARTIFACT_PATHS["execution-contract"]).exists():
        plan_path = root / ".agent/plan.lock.json"
        if plan_path.exists():
            replay_result = replay_gates(root, read_json(plan_path), record=False)
            findings.extend(replay_result["errors"])
    return findings


def verify_proof(
    root: Path, proof_path: Path, replay: bool = False, strict: bool = False
) -> List[Dict[str, Any]]:
    with historical_proof_reads():
        return _verify_proof(root, proof_path, replay=replay, strict=strict)
