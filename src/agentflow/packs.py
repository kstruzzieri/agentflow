"""Workflow pack manifest loading, validation, and projection.

A workflow pack is an inert, local data file (``.agentflow-pack/pack.json``)
that projects into the existing plan-lock and workflow-contract validators.
Loading reads exactly one file (the manifest), never reads, stats, or executes
any declared README or hook file, and never uses eval, import, or subprocess.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .contracts import (
    HUNK_ATTRIBUTION_POLICIES,
    WORKFLOW_CONTRACT_SCHEMA_VERSION,
    WORKFLOW_PACK_SCHEMA_VERSION,
    WORKFLOW_REVIEW_DEPTHS,
)
from .validation import validate_plan
from .versioning import validate_schema_version


MANIFEST_DIRNAME = ".agentflow-pack"
MANIFEST_FILENAME = "pack.json"


class PackError(ValueError):
    """Raised when a pack path cannot be resolved or its manifest is invalid."""


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_relative_path(value: Any) -> bool:
    """True only for a non-empty, contained, separator-safe relative path.

    Rejects empty strings, absolute POSIX paths, Windows/drive paths,
    backslashes, "." or ".." segments, and embedded NUL. Operates on the
    string only; the referenced file is never read or stat-ed.
    """
    if not isinstance(value, str) or value == "":
        return False
    if "\x00" in value or "\\" in value:
        return False
    if value.startswith("/"):
        return False
    if len(value) >= 2 and value[1] == ":":  # Windows drive, e.g. C:
        return False
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            return False
    return True


def resolve_manifest_path(path: Path) -> Path:
    """Resolve an accepted pack path form to the pack.json manifest.

    Accepts a project root containing ``.agentflow-pack/pack.json``, the
    ``.agentflow-pack`` directory itself, or the ``pack.json`` file.

    Symlinks in ``path`` are followed by the OS (``is_file``/``is_dir`` resolve
    them); the manifest is the one file this module reads. Callers needing strict
    containment should ``path.resolve()`` before passing.
    """
    if path.is_file() and path.name == MANIFEST_FILENAME:
        return path
    if path.is_dir():
        direct = path / MANIFEST_FILENAME
        if direct.is_file():
            return direct
        nested = path / MANIFEST_DIRNAME / MANIFEST_FILENAME
        if nested.is_file():
            return nested
    raise PackError(f"no pack manifest found at {path}")


PACK_FIELDS = {
    "schema_version",
    "id",
    "name",
    "description",
    "plan_templates",
    "profiles",
    "hook_templates",
    "readme",
}
REQUIRED_PACK_FIELDS = (
    "schema_version",
    "id",
    "name",
    "description",
    "plan_templates",
    "profiles",
)
PROFILE_FIELDS = {
    "id",
    "review_depth",
    "required_capabilities",
    "validation_policy",
    "proof_policy",
    "plan_template",
}
CAPABILITY_FIELDS = {"id", "required"}
VALIDATION_POLICY_FIELDS = {"required_gates"}
PROOF_POLICY_FIELDS = {"hunk_attribution", "require_review_run"}
HOOK_FIELDS = {"id", "path", "describe"}


def validate_pack_manifest(manifest: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(manifest, dict):
        return ["pack manifest must be a JSON object"]

    for field in sorted(set(manifest) - PACK_FIELDS):
        errors.append(f"unknown pack field: {field}")
    for field in REQUIRED_PACK_FIELDS:
        if field not in manifest:
            errors.append(f"missing required pack field: {field}")
    if errors:
        return errors

    errors.extend(
        validate_schema_version(
            manifest["schema_version"], WORKFLOW_PACK_SCHEMA_VERSION, "workflow-pack"
        )
    )
    for field in ("id", "name", "description"):
        if not _non_empty_string(manifest[field]):
            errors.append(f"{field} must be a non-empty string")

    templates = manifest["plan_templates"]
    template_gates: Dict[str, set] = {}
    if not isinstance(templates, dict) or not templates:
        errors.append("plan_templates must be a non-empty object")
        templates = {}
    else:
        for template_id, template in templates.items():
            errors.extend(_validate_template(template_id, template, template_gates))

    profiles = manifest["profiles"]
    if not isinstance(profiles, list) or not profiles:
        errors.append("profiles must be a non-empty array")
    else:
        for index, profile in enumerate(profiles, start=1):
            errors.extend(_validate_profile(profile, index, templates, template_gates))

    if "hook_templates" in manifest:
        hooks = manifest["hook_templates"]
        if not isinstance(hooks, list):
            errors.append("hook_templates must be an array")
        else:
            for index, hook in enumerate(hooks, start=1):
                errors.extend(_validate_hook(hook, index))

    if "readme" in manifest and not _safe_relative_path(manifest["readme"]):
        errors.append("readme must be a safe relative path")

    return errors


def _validate_template(template_id: Any, template: Any, template_gates: Dict[str, set]) -> List[str]:
    prefix = f"plan_templates[{template_id}]"
    if not isinstance(template, dict):
        return [f"{prefix} must be an object"]
    errors: List[str] = []
    if template.get("locked") is True:
        errors.append(f"{prefix} must be unlocked (locked must not be true)")
    if template.get("locked_at") is not None:
        errors.append(f"{prefix} must be unlocked (locked_at must be null)")
    plan_errors = validate_plan(template)
    for plan_error in plan_errors:
        errors.append(f"{prefix}: {plan_error}")
    # Only track gates for a structurally valid template; a malformed template
    # already reports its own errors, and tracking its gates would surface a
    # confusing secondary "not a subset" error against the broken template.
    if not plan_errors:
        gates = template.get("validation_gates")
        if isinstance(gates, list):
            template_gates[template_id] = {g for g in gates if isinstance(g, str)}
    return errors


def _validate_profile(
    profile: Any, index: int, templates: Dict[str, Any], template_gates: Dict[str, set]
) -> List[str]:
    prefix = f"profiles[{index}]"
    if not isinstance(profile, dict):
        return [f"{prefix} must be an object"]
    errors: List[str] = []
    for field in sorted(set(profile) - PROFILE_FIELDS):
        errors.append(f"{prefix} unknown field: {field}")
    for field in sorted(PROFILE_FIELDS):
        if field not in profile:
            errors.append(f"{prefix} missing field: {field}")
    if errors:
        return errors

    if not _non_empty_string(profile["id"]):
        errors.append(f"{prefix}.id must be a non-empty string")

    if profile["review_depth"] not in WORKFLOW_REVIEW_DEPTHS:
        errors.append(
            f"{prefix}.review_depth must be one of: " + ", ".join(WORKFLOW_REVIEW_DEPTHS)
        )

    capabilities = profile["required_capabilities"]
    if not isinstance(capabilities, list):
        errors.append(f"{prefix}.required_capabilities must be an array")
    else:
        for cap_index, capability in enumerate(capabilities, start=1):
            cap_prefix = f"{prefix}.required_capabilities[{cap_index}]"
            if not isinstance(capability, dict):
                errors.append(f"{cap_prefix} must be an object")
                continue
            for field in sorted(set(capability) - CAPABILITY_FIELDS):
                errors.append(f"{cap_prefix} unknown field: {field}")
            if not _non_empty_string(capability.get("id")):
                errors.append(f"{cap_prefix}.id must be a non-empty string")
            if not isinstance(capability.get("required"), bool):
                errors.append(f"{cap_prefix}.required must be boolean")

    validation_policy = profile["validation_policy"]
    required_gates: List[str] = []
    if not isinstance(validation_policy, dict):
        errors.append(f"{prefix}.validation_policy must be an object")
    else:
        for field in sorted(set(validation_policy) - VALIDATION_POLICY_FIELDS):
            errors.append(f"{prefix}.validation_policy unknown field: {field}")
        gates = validation_policy.get("required_gates")
        if not isinstance(gates, list) or not all(_non_empty_string(g) for g in gates):
            errors.append(
                f"{prefix}.validation_policy.required_gates must contain non-empty strings"
            )
        else:
            required_gates = gates

    proof_policy = profile["proof_policy"]
    if not isinstance(proof_policy, dict):
        errors.append(f"{prefix}.proof_policy must be an object")
    else:
        for field in sorted(set(proof_policy) - PROOF_POLICY_FIELDS):
            errors.append(f"{prefix}.proof_policy unknown field: {field}")
        if proof_policy.get("hunk_attribution") not in HUNK_ATTRIBUTION_POLICIES:
            errors.append(
                f"{prefix}.proof_policy.hunk_attribution must be one of: "
                + ", ".join(HUNK_ATTRIBUTION_POLICIES)
            )
        if not isinstance(proof_policy.get("require_review_run"), bool):
            errors.append(f"{prefix}.proof_policy.require_review_run must be boolean")

    template_id = profile["plan_template"]
    if not _non_empty_string(template_id):
        errors.append(f"{prefix}.plan_template must be a non-empty string")
    elif template_id not in templates:
        errors.append(f"{prefix} plan_template references unknown template: {template_id}")
    elif required_gates and template_id in template_gates:
        # Only checked when the template's gates were successfully tracked (the
        # template is structurally valid); a malformed template reports its own
        # error and must not also trigger a spurious subset failure here.
        available = template_gates[template_id]
        missing = [g for g in required_gates if g not in available]
        if missing:
            errors.append(
                f"{prefix} validation_policy.required_gates not a subset of template "
                f"{template_id} validation_gates: " + ", ".join(missing)
            )

    return errors


def _validate_hook(hook: Any, index: int) -> List[str]:
    prefix = f"hook_templates[{index}]"
    if not isinstance(hook, dict):
        return [f"{prefix} must be an object"]
    errors: List[str] = []
    for field in sorted(set(hook) - HOOK_FIELDS):
        errors.append(f"{prefix} unknown field: {field}")
    if not _non_empty_string(hook.get("id")):
        errors.append(f"{prefix}.id must be a non-empty string")
    if not _safe_relative_path(hook.get("path")):
        errors.append(f"{prefix}.path must be a safe relative path")
    if "describe" in hook and not _non_empty_string(hook["describe"]):
        errors.append(f"{prefix}.describe must be a non-empty string")
    return errors


@dataclass(frozen=True)
class Pack:
    manifest: Dict[str, Any]
    manifest_sha256: str
    manifest_path: Path


def load_pack(path: Path) -> Pack:
    manifest_path = resolve_manifest_path(path)
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackError(f"pack manifest is not valid JSON: {exc}") from exc
    errors = validate_pack_manifest(manifest)
    if errors:
        raise PackError("; ".join(errors))
    return Pack(manifest=manifest, manifest_sha256=digest, manifest_path=manifest_path)


def find_profile(manifest: Dict[str, Any], profile_id: str) -> Dict[str, Any]:
    for profile in manifest.get("profiles", []):
        if isinstance(profile, dict) and profile.get("id") == profile_id:
            return profile
    raise PackError(f"unknown profile: {profile_id}")


def profile_to_contract(
    manifest: Dict[str, Any],
    profile_id: str,
    selected_by: str,
    selection_reason: str,
) -> Dict[str, Any]:
    profile = find_profile(manifest, profile_id)
    return {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "workflow_pack": manifest["id"],
        "workflow_profile": profile["id"],
        "selected_by": selected_by,
        "selection_reason": selection_reason,
        "required_capabilities": deepcopy(profile["required_capabilities"]),
        "review_depth": profile["review_depth"],
        "validation_policy": deepcopy(profile["validation_policy"]),
        "proof_policy": deepcopy(profile["proof_policy"]),
    }


def template_to_plan(manifest: Dict[str, Any], template_id: str) -> Dict[str, Any]:
    templates = manifest.get("plan_templates", {})
    if not isinstance(templates, dict) or template_id not in templates:
        raise PackError(f"unknown plan template: {template_id}")
    plan = deepcopy(templates[template_id])
    plan["locked"] = False
    plan["locked_at"] = None
    return plan


def inspect_summary(pack: Pack) -> Dict[str, Any]:
    manifest = pack.manifest
    profiles = []
    for profile in manifest.get("profiles", []):
        profiles.append(
            {
                "id": profile.get("id"),
                "review_depth": profile.get("review_depth"),
                "plan_template": profile.get("plan_template"),
                "required_gates": list(
                    profile.get("validation_policy", {}).get("required_gates", [])
                ),
                "required_capabilities": [
                    cap.get("id")
                    for cap in profile.get("required_capabilities", [])
                    if isinstance(cap, dict)
                ],
                "proof_policy": dict(profile.get("proof_policy", {})),
            }
        )
    return {
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "description": manifest.get("description"),
        "manifest_sha256": pack.manifest_sha256,
        "plan_templates": sorted(manifest.get("plan_templates", {}).keys()),
        "profiles": profiles,
        "hook_templates": [
            {"id": hook.get("id"), "path": hook.get("path")}
            for hook in manifest.get("hook_templates", [])
            if isinstance(hook, dict)
        ],
    }


def render_inspect_summary(summary: Dict[str, Any]) -> List[str]:
    lines = [f"pack {summary['id']} - {summary['name']}"]
    for profile in summary["profiles"]:
        lines.append(
            f"  profile {profile['id']}: review={profile['review_depth']} "
            f"template={profile['plan_template']} "
            f"gates={','.join(profile['required_gates']) or '-'} "
            f"caps={','.join(profile['required_capabilities']) or '-'} "
            f"hunk={profile['proof_policy'].get('hunk_attribution')}"
        )
    for hook in summary["hook_templates"]:
        lines.append(f"  hook {hook['id']}: {hook['path']}")
    return lines
