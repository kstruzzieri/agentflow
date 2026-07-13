"""Deterministic, stdlib-only producer of ``review-manifest.json``.

This module is the review-cycle *runner*: it projects declared finding state
(from a compact ``findings-final.json`` sidecar) into the manifest schema that
``agentflow record-review`` consumes. It never parses ``findings-final.yaml``
and never adjudicates a finding's verdict — adjudication stays in the review
system; this module only removes the manual transcription/computation steps the
#8 dogfood proved error-prone. It deliberately lives outside ``review.py`` (the
YAML-blind verifier), importing only contract constants from it.
"""

from __future__ import annotations

import fnmatch
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .git import current_branch
from .review import (
    SEVERITIES,
    STATUSES,
    WORKFLOW_REVIEW_DEPTHS,
    validate_manifest,
    validate_manifest_against_plan,
)

MANIFEST_SCHEMA_VERSION = "1.0.0"
MANIFEST_FILENAME = "review-manifest.json"

# status values that make a finding inactive (never block, never warn)
INACTIVE_STATUSES = frozenset({"fixed", "rejected", "superseded"})
ACTIVE_STATUSES = frozenset(STATUSES) - INACTIVE_STATUSES

# Optional per-finding fields carried from the sidecar into the manifest index.
OPTIONAL_INDEX_FIELDS = ("steelman_verdict", "superseded_by", "fix_commit")


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_repair_context(finding: Dict[str, Any], required: bool) -> None:
    fid = finding["id"]
    refs = finding.get("agentflow_refs")
    owner = refs.get("plan_step") if isinstance(refs, dict) else None
    for field, value in (
        ("claim", finding.get("claim")),
        ("suggested_fix", finding.get("suggested_fix")),
        ("agentflow_refs.plan_step", owner),
    ):
        if required and not _non_empty_string(value):
            raise ValueError(f"finding {fid} {field} must be a non-empty string")
        if value is not None and not _non_empty_string(value):
            raise ValueError(f"finding {fid} {field} must be a non-empty string")

    file_value = finding.get("file")
    line = finding.get("line")
    line_end = finding.get("line_end")
    if file_value is not None and not _non_empty_string(file_value):
        raise ValueError(f"finding {fid} file must be a non-empty string")
    if (line is not None or line_end is not None) and file_value is None:
        raise ValueError(f"finding {fid} line requires file")
    for field, value in (("line", line), ("line_end", line_end)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"finding {fid} {field} must be a positive integer")
    if line_end is not None and line is None:
        raise ValueError(f"finding {fid} line_end requires line")
    if line is not None and line_end is not None and line_end < line:
        raise ValueError(f"finding {fid} line_end must be greater than or equal to line")

# Required review artifacts. The runner fails closed when any is absent so a
# manifest cannot be recorded with only the compact JSON sidecar hashed.
REQUIRED_ARTIFACTS = (
    "findings-final.json",
    "findings-final.yaml",
    "synthesis.md",
    "gate.yaml",
)

# Extra required artifacts (beyond the findings sidecar) per declared depth.
# Monotonic superset: each deeper profile requires everything the lighter ones
# do. ``deep`` reproduces the historical four-pass requirement exactly.
DEPTH_REQUIRED_ARTIFACTS = {
    "none": (),
    "light": (),
    "standard": (),
    "spec_quality": ("gate.yaml",),
    "deep": REQUIRED_ARTIFACTS[1:],  # findings-final.yaml, synthesis.md, gate.yaml
}

# Deterministic artifact order. Optional names are listed only when present;
# paths are relative to state_dir (matching review.normalize_artifact_path).
KNOWN_ARTIFACTS = (
    "findings-final.json",
    "findings-final.yaml",
    "findings-bp.yaml",
    "findings-adv.yaml",
    "synthesis.md",
    "gate.yaml",
    "ready-for-pr.md",
)


def mint_review_run_id() -> str:
    """Mint a fresh ``RR-<YYYYMMDDTHHMMSSZ>-<8hex>`` review run id."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"RR-{stamp}-{secrets.token_hex(4)}"


def load_findings(path: Path) -> List[Dict[str, Any]]:
    """Load and validate the compact ``findings-final.json`` sidecar.

    Expects ``{"findings": [ {id, severity, status, ...}, ... ]}``. Fails closed
    with ValueError on a missing/unreadable file, a non-list ``findings``,
    duplicate ids, non-string optional fields, or any row missing a non-empty
    ``id`` or carrying an out-of-enum severity/status.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unreadable findings sidecar: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed findings sidecar JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("findings sidecar must be a JSON object")
    findings = raw.get("findings")
    if not isinstance(findings, list):
        raise ValueError("findings sidecar 'findings' must be a list")
    seen_ids = set()
    for index, row in enumerate(findings, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"finding {index} must be an object")
        fid = row.get("id")
        if not isinstance(fid, str) or not fid.strip():
            raise ValueError(f"finding {index} missing a non-empty id")
        if fid in seen_ids:
            raise ValueError(f"duplicate finding id: {fid}")
        seen_ids.add(fid)
        if row.get("severity") not in SEVERITIES:
            raise ValueError(f"finding {fid} severity invalid: {row.get('severity')!r}")
        if row.get("status") not in STATUSES:
            raise ValueError(f"finding {fid} status invalid: {row.get('status')!r}")
        for field in OPTIONAL_INDEX_FIELDS:
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"finding {fid} {field} must be a string")
        _validate_repair_context(row, row["status"] in ACTIVE_STATUSES)
    return findings


def load_policy_config(path: Path) -> Dict[str, Any]:
    """Load the machine policy source (``docs/ai/config.json``) with stdlib json."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unreadable policy config: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed policy config JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("policy config must be a JSON object")
    if not isinstance(config.get("branch_modifiers"), dict):
        raise ValueError("policy config missing 'branch_modifiers' object")
    if not isinstance(config.get("gate_policy"), dict):
        raise ValueError("policy config missing 'gate_policy' object")
    return config


def resolve_gate_policy(
    config: Dict[str, Any], branch: str
) -> Tuple[str, List[str], List[str]]:
    """Resolve a branch ref to ``(gate_name, blocks_on, warns_on)``.

    Matches ``branch`` against ``branch_modifiers`` globs with fnmatchcase so
    behavior is deterministic across platforms. The most specific match wins:
    any non-``*`` match (longest pattern) is preferred over the ``*`` default.
    Raises ValueError if no ``*`` default exists or the named gate is absent
    from ``gate_policy``.
    """
    modifiers = config["branch_modifiers"]
    normalized_branch = _normalize_branch_ref(branch)
    specific = [
        p for p in modifiers if p != "*" and fnmatch.fnmatchcase(normalized_branch, p)
    ]
    if specific:
        pattern = max(specific, key=len)
    elif "*" in modifiers:
        pattern = "*"
    else:
        raise ValueError("branch_modifiers has no '*' default")
    # Only the ``gate`` key is consulted here. Other modifier keys (e.g.
    # ``require_hotfix_debt``) are intentionally ignored: branch-debt and
    # readiness concerns are owned by the pass-4 ``gate.yaml``, not this
    # deterministic finding-policy projection.
    modifier = modifiers[pattern]
    if not isinstance(modifier, dict):
        raise ValueError(f"branch modifier {pattern!r} must be an object")
    gate_name = modifier.get("gate")
    if not isinstance(gate_name, str) or not gate_name.strip():
        raise ValueError(f"branch modifier {pattern!r} missing non-empty gate")
    policy = config["gate_policy"].get(gate_name)
    if not isinstance(policy, dict):
        raise ValueError(f"no gate_policy for gate {gate_name!r}")
    blocks_on = _policy_severities(gate_name, policy, "blocks_on")
    warns_on = _policy_severities(gate_name, policy, "warns_on")
    return gate_name, blocks_on, warns_on


def _normalize_branch_ref(branch: str) -> str:
    """Normalize common full/remote branch refs before policy glob matching."""
    prefixes = (
        "refs/heads/",
        "refs/remotes/",
        "remotes/",
    )
    normalized = branch.strip()
    for prefix in prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    if normalized.startswith("origin/"):
        normalized = normalized[len("origin/"):]
    return normalized


def _policy_severities(gate_name: str, policy: Dict[str, Any], field: str) -> List[str]:
    value = policy.get(field, [])
    if not isinstance(value, list):
        raise ValueError(f"gate_policy {gate_name!r} {field} must be a list")
    severities: List[str] = []
    for item in value:
        if not isinstance(item, str) or item not in SEVERITIES:
            raise ValueError(
                f"gate_policy {gate_name!r} {field} contains invalid severity: {item!r}"
            )
        severities.append(item)
    return severities


def compute_gate(
    findings: List[Dict[str, Any]],
    blocks_on: List[str],
    warns_on: List[str],
) -> Tuple[str, List[str]]:
    """Compute ``(gate_status, active_blocking)`` over FINAL finding severity.

    Only active findings (status not in ``INACTIVE_STATUSES``) count. A finding
    whose severity is in ``blocks_on`` is blocking; one whose severity is in
    ``warns_on`` contributes a warn. ``gate_status`` is fail > warn > pass.
    ``active_blocking`` preserves input order.
    """
    blocking: List[str] = []
    warned = False
    for finding in findings:
        if finding["status"] in INACTIVE_STATUSES:
            continue
        severity = finding["severity"]
        if severity in blocks_on:
            blocking.append(finding["id"])
        elif severity in warns_on:
            warned = True
    if blocking:
        return "fail", blocking
    if warned:
        return "warn", []
    return "pass", []


def project_findings(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Project findings into ``counts_by_severity``/``counts_by_status``/``index``.

    Counts cover all findings; the index lists every finding with its id,
    severity, and status, plus any non-empty optional fields
    (``steelman_verdict``, ``superseded_by``, ``fix_commit``).
    """
    counts_by_severity: Dict[str, int] = {}
    counts_by_status: Dict[str, int] = {}
    index: List[Dict[str, Any]] = []
    for finding in findings:
        severity = finding["severity"]
        status = finding["status"]
        counts_by_severity[severity] = counts_by_severity.get(severity, 0) + 1
        counts_by_status[status] = counts_by_status.get(status, 0) + 1
        row: Dict[str, Any] = {
            "finding_id": finding["id"],
            "severity": severity,
            "status": status,
        }
        for field in OPTIONAL_INDEX_FIELDS:
            value = finding.get(field)
            if value:
                row[field] = value
        refs = finding.get("agentflow_refs")
        owner = refs.get("plan_step") if isinstance(refs, dict) else None
        if owner:
            row["owning_step"] = owner
        if finding.get("claim"):
            row["claim"] = finding["claim"]
        if finding.get("file"):
            location: Dict[str, Any] = {"path": finding["file"]}
            for field in ("line", "line_end"):
                if finding.get(field) is not None:
                    location[field] = finding[field]
            row["location"] = location
        if finding.get("suggested_fix"):
            row["suggested_fix"] = finding["suggested_fix"]
        index.append(row)
    return {
        "counts_by_severity": counts_by_severity,
        "counts_by_status": counts_by_status,
        "index": index,
    }


def build_manifest(
    review_run_id: str,
    state_dir: str,
    policy_name: str,
    gate_status: str,
    active_blocking: List[str],
    projection: Dict[str, Any],
    artifacts: List[Dict[str, str]],
    depth_profile: str = "deep",
) -> Dict[str, Any]:
    """Assemble a ``review-manifest.json`` object and self-validate the contract.

    Raises ValueError (via ``validate_manifest``) if the assembled object does
    not satisfy the manifest contract the verifier enforces.
    """
    manifest: Dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "review_run_id": review_run_id,
        "state_dir": state_dir,
        "policy": policy_name,
        "gate_status": gate_status,
        "active_blocking": list(active_blocking),
        "depth_profile": depth_profile,
        "amendment_ready": True,
        "findings": projection,
        "artifacts": artifacts,
    }
    validate_manifest(manifest)
    return manifest


def build_artifacts(
    state_dir_abs: Path,
    findings_artifact: str = "findings-final.json",
    required_extra: Tuple[str, ...] = REQUIRED_ARTIFACTS[1:],
) -> List[Dict[str, str]]:
    """List review artifacts in ``state_dir_abs``, fixed order.

    Raises ValueError if any required artifact is missing. ``required_extra``
    is the non-sidecar artifact set the declared depth demands (default: the
    deep four-pass set). Returns ``[{"path": name}, ...]`` for each name in
    ``KNOWN_ARTIFACTS`` that exists as a file. Paths are relative to state_dir,
    which is what ``review.normalize_artifact_path`` resolves against
    ``root/state_dir``.
    """
    required = (findings_artifact, *required_extra)
    for name in required:
        if not (state_dir_abs / name).is_file():
            raise ValueError(f"required review artifact missing: {name}")
    artifacts: List[Dict[str, str]] = []
    seen = set()
    for name in (findings_artifact, *KNOWN_ARTIFACTS):
        if name in seen:
            continue
        seen.add(name)
        if (state_dir_abs / name).is_file():
            artifacts.append({"path": name})
    return artifacts


def _resolve_state_dir(root: Path, state_dir: str) -> Tuple[Path, str]:
    """Return ``(abs_path, root_relative_posix)`` for ``state_dir``; reject escapes."""
    resolved_root = root.resolve()
    state_abs = (resolved_root / state_dir).resolve(strict=False)
    try:
        rel = state_abs.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"state_dir escapes root: {state_dir!r}") from exc
    return state_abs, rel


def _state_dir_hint(findings_json: Optional[str], state_dir: str) -> Optional[str]:
    """Advice for a not-found/escaping relative ``--findings-json``.

    Returns ``None`` for the default (``None``) or an absolute path — those are
    never re-rooted, so the state-dir advice would mislead. Otherwise returns a
    hint that the path is state-dir-relative, plus a "did you mean" suggestion
    when the path's parts start with the ``--state-dir`` parts (the doubling
    case). Compares path parts, not raw string prefixes, so ``.../main2/...`` is
    not treated as living under ``.../main``.
    """
    if not findings_json:
        return None
    candidate = Path(findings_json)
    if candidate.is_absolute():
        return None
    hint = " (--findings-json is resolved relative to --state-dir)"
    state_parts = Path(state_dir).parts
    cand_parts = candidate.parts
    if (
        len(cand_parts) > len(state_parts)
        and cand_parts[: len(state_parts)] == state_parts
    ):
        stripped = Path(*cand_parts[len(state_parts):]).as_posix()
        hint += f"; did you mean --findings-json {stripped}?"
    return hint


def _resolve_findings_json(state_abs: Path, findings_json: Optional[str]) -> Tuple[Path, str]:
    """Return ``(abs_path, state_relative_posix)`` for the sidecar; reject escapes."""
    if findings_json is None:
        return state_abs / "findings-final.json", "findings-final.json"
    if not findings_json.strip():
        raise ValueError("findings_json must be a non-empty path")
    sidecar = Path(findings_json)
    if not sidecar.is_absolute() and ".." in sidecar.parts:
        raise ValueError(
            f"findings_json escapes state_dir: {findings_json!r}"
            " (--findings-json is resolved relative to --state-dir)"
        )
    sidecar_abs = sidecar.resolve(strict=False) if sidecar.is_absolute() else (
        state_abs / sidecar
    ).resolve(strict=False)
    try:
        sidecar_rel = sidecar_abs.relative_to(state_abs).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"findings_json escapes state_dir: {findings_json!r}"
            " (--findings-json is resolved relative to --state-dir)"
        ) from exc
    return sidecar_abs, sidecar_rel


def produce_manifest(
    root: Path,
    state_dir: str,
    branch: Optional[str],
    findings_json: Optional[str],
    config_path: Path,
    depth_profile: str = "deep",
) -> Dict[str, Any]:
    """Produce a validated ``review-manifest.json`` object (does not write it).

    ``depth_profile`` (default ``deep``) declares the review depth this recorded
    run satisfies and selects the required artifact set: lighter depths require
    fewer artifacts. Raises ValueError on any malformed input or contract
    violation.
    """
    if depth_profile not in WORKFLOW_REVIEW_DEPTHS:
        raise ValueError(f"depth_profile invalid: {depth_profile!r}")
    state_abs, state_rel = _resolve_state_dir(root, state_dir)

    resolved_branch = branch or current_branch(root)
    if not resolved_branch:
        raise ValueError("could not resolve branch; pass --branch")

    sidecar, sidecar_rel = _resolve_findings_json(state_abs, findings_json)
    if not sidecar.is_file():
        hint = _state_dir_hint(findings_json, state_dir) or ""
        raise ValueError(f"findings sidecar not found: {sidecar}{hint}")
    findings = load_findings(sidecar)

    config = load_policy_config(config_path)
    policy_name, blocks_on, warns_on = resolve_gate_policy(config, resolved_branch)
    gate_status, active_blocking = compute_gate(findings, blocks_on, warns_on)
    projection = project_findings(findings)
    artifacts = build_artifacts(
        state_abs,
        findings_artifact=sidecar_rel,
        required_extra=DEPTH_REQUIRED_ARTIFACTS[depth_profile],
    )

    manifest = build_manifest(
        review_run_id=mint_review_run_id(),
        state_dir=state_rel,
        policy_name=policy_name,
        gate_status=gate_status,
        active_blocking=active_blocking,
        projection=projection,
        artifacts=artifacts,
        depth_profile=depth_profile,
    )
    plan_path = root / ".agent/plan.lock.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"locked plan unavailable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"locked plan is malformed JSON: {exc}") from exc
    validate_manifest_against_plan(manifest, plan)
    return manifest


def exit_code_for(
    manifest: Dict[str, Any], fail_on_block: bool, strict_exit: bool
) -> int:
    """Process exit status for a *valid* manifest. Never mutates ``manifest``.

    Default 0 (so evidence is still written even on warn/fail). ``fail_on_block``
    returns 1 when ``active_blocking`` is non-empty or ``gate_status == 'fail'``.
    ``strict_exit`` additionally returns 1 on ``gate_status == 'warn'``.
    """
    if fail_on_block and (
        manifest.get("active_blocking") or manifest.get("gate_status") == "fail"
    ):
        return 1
    if strict_exit and manifest.get("gate_status") in ("warn", "fail"):
        return 1
    return 0
