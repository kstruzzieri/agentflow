"""Workflow contract artifact validation and summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .artifacts import read_json, write_json
from .contracts import (
    HUNK_ATTRIBUTION_POLICIES,
    WORKFLOW_CONTRACT_SCHEMA_VERSION,
    WORKFLOW_REVIEW_DEPTHS,
)
from .versioning import validate_schema_version


WORKFLOW_CONTRACT_PATH = ".agent/workflow.contract.json"

TOP_LEVEL_FIELDS = {
    "schema_version",
    "workflow_pack",
    "workflow_profile",
    "selected_by",
    "selection_reason",
    "required_capabilities",
    "review_depth",
    "validation_policy",
    "proof_policy",
}
REQUIRED_FIELDS = {
    "schema_version": str,
    "workflow_pack": str,
    "workflow_profile": str,
    "selected_by": str,
    "selection_reason": str,
    "required_capabilities": list,
    "review_depth": str,
    "validation_policy": dict,
    "proof_policy": dict,
}
CAPABILITY_FIELDS = {"id", "required"}
VALIDATION_POLICY_FIELDS = {"required_gates"}
PROOF_POLICY_FIELDS = {"hunk_attribution", "require_review_run"}


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_empty_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_non_empty_string(item) for item in value)


def validate_workflow_contract(contract: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(contract, dict):
        return ["workflow contract must be a JSON object"]

    for field in sorted(set(contract) - TOP_LEVEL_FIELDS):
        errors.append(f"unknown workflow contract field: {field}")

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in contract:
            errors.append(f"missing required workflow contract field: {field}")
            continue
        if not isinstance(contract[field], expected_type):
            errors.append(f"{field} must be {expected_type.__name__}")

    if errors:
        return errors

    errors.extend(
        validate_schema_version(
            contract["schema_version"],
            WORKFLOW_CONTRACT_SCHEMA_VERSION,
            "workflow-contract",
        )
    )

    for field in ("workflow_pack", "workflow_profile", "selected_by", "selection_reason"):
        if not _non_empty_string(contract[field]):
            errors.append(f"{field} must be a non-empty string")

    for index, capability in enumerate(contract["required_capabilities"], start=1):
        prefix = f"required_capabilities[{index}]"
        if not isinstance(capability, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for field in sorted(set(capability) - CAPABILITY_FIELDS):
            errors.append(f"{prefix} unknown field: {field}")
        if not _non_empty_string(capability.get("id")):
            errors.append(f"{prefix}.id must be a non-empty string")
        if not isinstance(capability.get("required"), bool):
            errors.append(f"{prefix}.required must be boolean")

    review_depth = contract["review_depth"]
    if review_depth not in WORKFLOW_REVIEW_DEPTHS:
        errors.append(
            "review_depth must be one of: " + ", ".join(WORKFLOW_REVIEW_DEPTHS)
        )

    validation_policy = contract["validation_policy"]
    for field in sorted(set(validation_policy) - VALIDATION_POLICY_FIELDS):
        errors.append(f"validation_policy unknown field: {field}")
    required_gates = validation_policy.get("required_gates")
    if not _non_empty_string_list(required_gates):
        errors.append("validation_policy.required_gates must contain non-empty strings")

    proof_policy = contract["proof_policy"]
    for field in sorted(set(proof_policy) - PROOF_POLICY_FIELDS):
        errors.append(f"proof_policy unknown field: {field}")
    hunk_attribution = proof_policy.get("hunk_attribution")
    if hunk_attribution not in HUNK_ATTRIBUTION_POLICIES:
        errors.append(
            "proof_policy.hunk_attribution must be one of: "
            + ", ".join(HUNK_ATTRIBUTION_POLICIES)
        )
    if not isinstance(proof_policy.get("require_review_run"), bool):
        errors.append("proof_policy.require_review_run must be boolean")

    return errors


def read_workflow_contract(root: Path) -> Dict[str, Any]:
    return read_json(root / WORKFLOW_CONTRACT_PATH)


def write_workflow_contract(root: Path, contract: Dict[str, Any]) -> Path:
    errors = validate_workflow_contract(contract)
    if errors:
        raise ValueError("; ".join(errors))
    path = root / WORKFLOW_CONTRACT_PATH
    write_json(path, contract)
    return path


def workflow_contract_summary(contract: Dict[str, Any]) -> Dict[str, Any]:
    required_capabilities = [
        item["id"]
        for item in contract.get("required_capabilities", [])
        if isinstance(item, dict)
        and item.get("required") is True
        and isinstance(item.get("id"), str)
    ]
    validation_policy = contract.get("validation_policy", {})
    proof_policy = contract.get("proof_policy", {})
    return {
        "workflow_pack": contract.get("workflow_pack"),
        "workflow_profile": contract.get("workflow_profile"),
        "selected_by": contract.get("selected_by"),
        "review_depth": contract.get("review_depth"),
        "required_capabilities": required_capabilities,
        "required_gates": list(validation_policy.get("required_gates", []))
        if isinstance(validation_policy, dict)
        else [],
        "proof_policy": {
            "hunk_attribution": proof_policy.get("hunk_attribution")
            if isinstance(proof_policy, dict)
            else None,
            "require_review_run": proof_policy.get("require_review_run")
            if isinstance(proof_policy, dict)
            else None,
        },
    }
