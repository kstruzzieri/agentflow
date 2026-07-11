"""Review-cycle evidence: map review findings into Agentflow proof.

This module is a stdlib-only verifier/indexer. It hashes review artifacts and
indexes a machine projection (``review-manifest.json``) the review system
produces; it never parses ``findings-final.yaml`` and never adjudicates a
finding's verdict.
"""

from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import plan_binding_sha256, read_jsonl, try_read_json, utc_now
from .contracts import (
    REVIEW_DEPTH_POLICY,
    REVIEW_GATE_POLICIES,
    REVIEW_RUNS_SCHEMA_VERSION,
    WORKFLOW_REVIEW_DEPTHS,
    recorded_review_depth,
    review_depth_rank,
    review_depth_satisfies,
    strict_mode,
)

REVIEW_RUN_ID_PATTERN = re.compile(r"^RR-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# Mirrors schemas/review-manifest.schema.json: schema_version pattern. The
# hand-rolled validator is the only enforced gate (stdlib-only, no jsonschema),
# so it must reject versions the published schema would reject.
MANIFEST_SCHEMA_VERSION_PATTERN = re.compile(r"^0\.[0-2]\.[0-9]+$")
GATE_STATUSES = ("pass", "warn", "fail")
SEVERITIES = ("critical", "high", "medium", "low")
STATUSES = ("open", "accepted", "fixed", "rejected", "superseded")

# ratchet-v1 ordering over the review-gate policy membership set
# REVIEW_GATE_POLICIES (contracts.py). Higher index = stricter.
GATE_ORDER = {"ignore": 0, "warn": 1, "block": 2}

# Canonical verification-semantics token written by build_time_review_policy and
# checked at verify time. Centralised here so both sites stay in sync.
REVIEW_VERIFICATION_SEMANTICS = "ratchet-v1"


def join_review_gate(a: str, b: str) -> str:
    """Return the stricter of two review-gate policies over ignore<warn<block.

    Unrecognized values rank below ``ignore`` so a corrupt recorded policy can
    never raise and never win the join.  This guarantees that an unrecognized
    value never beats a *recognized* policy; it does not guarantee a recognized
    return value when both inputs are unrecognized.
    """
    rank_a = GATE_ORDER.get(a, -1)
    rank_b = GATE_ORDER.get(b, -1)
    return a if rank_a >= rank_b else b


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file without importing proof.py."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_finding_ref(value: str) -> Dict[str, str]:
    """Parse a review-run-scoped finding ref ``RR-...#FINDING_ID``."""
    if not isinstance(value, str) or "#" not in value:
        raise ValueError(f"finding ref must be 'RR-...#FINDING_ID': {value!r}")
    review_run_id, _, finding_id = value.partition("#")
    if not REVIEW_RUN_ID_PATTERN.fullmatch(review_run_id):
        raise ValueError(f"invalid review_run_id in finding ref: {value!r}")
    if not finding_id.strip():
        raise ValueError(f"finding ref missing finding id: {value!r}")
    return {"review_run_id": review_run_id, "finding_id": finding_id}


def _require_str(obj: Dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest field {key} must be a non-empty string")
    return value


def validate_manifest(manifest: Any) -> None:
    """Validate the shape and enums of a review-manifest.json object."""
    if not isinstance(manifest, dict):
        raise ValueError("review manifest must be a JSON object")
    schema_version = _require_str(manifest, "schema_version")
    # fullmatch, not match: Python's re.match leaves a trailing '$' matching
    # before a final newline, so "0.1.0\n" would slip through. ECMA-262 '$'
    # (what the JSON schema means) anchors at true end-of-input; fullmatch
    # reproduces that and keeps the validator from drifting from the schema.
    if not MANIFEST_SCHEMA_VERSION_PATTERN.fullmatch(schema_version):
        raise ValueError(
            f"schema_version must match {MANIFEST_SCHEMA_VERSION_PATTERN.pattern}: "
            f"{schema_version!r}"
        )
    review_run_id = _require_str(manifest, "review_run_id")
    if not REVIEW_RUN_ID_PATTERN.fullmatch(review_run_id):
        raise ValueError(
            f"review_run_id must match RR-<YYYYMMDDTHHMMSSZ>-<8hex>: {review_run_id!r}"
        )
    _require_str(manifest, "state_dir")
    gate_status = manifest.get("gate_status")
    if gate_status not in GATE_STATUSES:
        raise ValueError(f"gate_status must be one of {GATE_STATUSES}: {gate_status!r}")
    active_blocking = manifest.get("active_blocking", [])
    if not isinstance(active_blocking, list) or not all(
        isinstance(item, str) for item in active_blocking
    ):
        raise ValueError("active_blocking must be a list of strings")
    findings = manifest.get("findings")
    if not isinstance(findings, dict):
        raise ValueError("findings must be an object")
    index = findings.get("index", [])
    if not isinstance(index, list):
        raise ValueError("findings.index must be a list")
    for row in index:
        if not isinstance(row, dict):
            raise ValueError("findings.index rows must be objects")
        _require_str(row, "finding_id")
        if row.get("severity") not in SEVERITIES:
            raise ValueError(f"finding severity invalid: {row.get('severity')!r}")
        if row.get("status") not in STATUSES:
            raise ValueError(f"finding status invalid: {row.get('status')!r}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("artifacts must be a non-empty list")
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise ValueError("artifact entries must be objects")
        _require_str(entry, "path")
    depth_profile = manifest.get("depth_profile")
    if depth_profile is not None and depth_profile not in WORKFLOW_REVIEW_DEPTHS:
        raise ValueError(f"depth_profile invalid: {depth_profile!r}")


def normalize_artifact_path(root: Path, state_dir: str, relative: str) -> str:
    """Resolve ``state_dir/relative`` to a root-relative path, rejecting escapes.

    Rejects absolute paths, parent traversal, and symlinks that resolve outside
    ``state_dir``/``root``. Returns the normalized path relative to ``root``
    (posix style).
    """
    if not isinstance(relative, str) or not relative:
        raise ValueError("artifact path must be a non-empty string")
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValueError(f"artifact path must be relative: {relative!r}")
    if ".." in relative_path.parts:
        raise ValueError(f"artifact path must not contain parent traversal: {relative!r}")
    resolved_root = root.resolve()
    resolved_state = (root / state_dir).resolve(strict=False)
    try:
        resolved_state.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"state_dir escapes root: {state_dir!r}") from exc
    candidate = (resolved_state / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(resolved_state)
    except ValueError as exc:
        raise ValueError(f"artifact path escapes state_dir: {relative!r}") from exc
    try:
        rel = candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"artifact path escapes root: {relative!r}") from exc
    return rel.as_posix()


def read_review_runs(root: Path) -> List[Dict[str, Any]]:
    """Read the review-runs ledger. Raises ValueError on malformed rows."""
    rows = read_jsonl(root / ".agent/review-runs.jsonl")
    result: List[Dict[str, Any]] = []
    required = (
        "schema_version",
        "review_run_id",
        "recorded_at",
        "state_dir",
        "manifest_path",
        "manifest_sha256",
        "gate_status",
        "artifacts",
    )
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"review-runs line {index} must be a JSON object")
        for key in required:
            if key not in row:
                raise ValueError(f"review-runs line {index} missing {key}")
        review_run_id = row.get("review_run_id")
        if not isinstance(review_run_id, str) or not REVIEW_RUN_ID_PATTERN.fullmatch(review_run_id):
            raise ValueError(f"review-runs line {index} has invalid review_run_id")
        if row.get("gate_status") not in GATE_STATUSES:
            raise ValueError(f"review-runs line {index} has invalid gate_status")
        depth_profile = row.get("depth_profile")
        if depth_profile is not None and depth_profile not in WORKFLOW_REVIEW_DEPTHS:
            raise ValueError(f"review-runs line {index} has invalid depth_profile")
        plan_sha256 = row.get("plan_sha256")
        if plan_sha256 is not None and (
            not isinstance(plan_sha256, str)
            or not SHA256_PATTERN.fullmatch(plan_sha256)
        ):
            raise ValueError(f"review-runs line {index} has invalid plan_sha256")
        artifacts = row.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError(f"review-runs line {index} artifacts must be a list")
        for entry in artifacts:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("sha256"), str)
            ):
                raise ValueError(f"review-runs line {index} has malformed artifact entry")
        result.append(row)
    return result


def build_review_run_record(root: Path, manifest_path: Path) -> Dict[str, Any]:
    """Validate a manifest, hash it and its artifacts, and return a ledger record.

    Performs all filesystem reads; the caller appends the returned record. Raises
    ValueError on any validation, containment, missing-artifact, state_dir
    disagreement, or duplicate-id problem so nothing is appended on failure.
    """
    try:
        manifest, read_error = try_read_json(manifest_path)
    except OSError as exc:
        raise ValueError(f"unreadable review manifest: {exc}") from exc
    if manifest is None:
        raise ValueError(f"unreadable review manifest: {read_error}")
    validate_manifest(manifest)

    state_dir = manifest["state_dir"]
    declared = (root / state_dir).resolve(strict=False)
    actual_dir = manifest_path.resolve(strict=False).parent
    if declared != actual_dir:
        raise ValueError(
            f"manifest state_dir {state_dir!r} does not match manifest location"
        )

    review_run_id = manifest["review_run_id"]
    if any(row.get("review_run_id") == review_run_id for row in read_review_runs(root)):
        raise ValueError(f"duplicate review_run_id already recorded: {review_run_id}")

    plan_path = root / ".agent/plan.lock.json"
    if not plan_path.is_file():
        raise ValueError("plan lock missing: .agent/plan.lock.json")
    plan, plan_error = try_read_json(plan_path)
    if not isinstance(plan, dict):
        raise ValueError(plan_error or "plan lock must be a JSON object")

    artifacts: List[Dict[str, str]] = []
    for entry in manifest["artifacts"]:
        rel = normalize_artifact_path(root, state_dir, entry["path"])
        path = root / rel
        if not path.exists():
            raise ValueError(f"review artifact missing on disk: {rel}")
        if not path.is_file():
            raise ValueError(f"review artifact is not a regular file: {rel}")
        artifacts.append({"path": rel, "sha256": sha256_file(path)})

    manifest_rel = manifest_path.resolve(strict=False).relative_to(root.resolve()).as_posix()
    record: Dict[str, Any] = {
        "schema_version": REVIEW_RUNS_SCHEMA_VERSION,
        "review_run_id": review_run_id,
        "recorded_at": utc_now(),
        "state_dir": state_dir,
        "manifest_path": manifest_rel,
        "manifest_sha256": sha256_file(manifest_path),
        "plan_sha256": plan_binding_sha256(plan),
        "policy": manifest.get("policy"),
        "gate_status": manifest["gate_status"],
        "active_blocking": list(manifest.get("active_blocking", [])),
        "depth_profile": recorded_review_depth(manifest.get("depth_profile")),
        "findings": manifest.get("findings", {}),
        "artifacts": artifacts,
    }
    return record


def review_evidence_entries(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deterministic ``kind: review`` attestation entries for a review run."""
    from .contracts import EVIDENCE_SCHEMA_VERSION

    rrid = record["review_run_id"]
    base = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": "review",
        "source": record.get("manifest_path", ""),
        "confidence": "high",
        "last_verified": record["recorded_at"],
    }
    return [
        {
            **base,
            "id": f"E-review-{rrid}",
            "claim": f"review run {rrid} recorded ({len(record['artifacts'])} artifacts hashed)",
        },
        {
            **base,
            "id": f"E-review-gate-{rrid}",
            "claim": f"review run {rrid} gate_status={record['gate_status']}, "
            f"active_blocking={len(record['active_blocking'])}",
        },
    ]


def _workflow_review_policy(root: Path):
    """The selected workflow profile's contribution to the review policy.

    Returns ``(required_review_depth, depth_gate_floor, requires_run)`` from
    ``.agent/workflow.contract.json`` (#74). Tolerant: an absent or malformed
    contract, or an unrecognized ``review_depth``, contributes nothing (an
    ``ignore`` floor and no required run), so the workflow contract can only
    raise strictness, never lower it.
    """
    depth: Optional[str] = None
    wf_requires_run = False
    contract_path = root / ".agent/workflow.contract.json"
    if contract_path.exists():
        data, _ = try_read_json(contract_path)
        if isinstance(data, dict):
            raw_depth = data.get("review_depth")
            if isinstance(raw_depth, str) and raw_depth in REVIEW_DEPTH_POLICY:
                depth = raw_depth
            proof_policy = data.get("proof_policy")
            if isinstance(proof_policy, dict):
                wf_requires_run = bool(proof_policy.get("require_review_run", False))
    depth_gate, depth_requires_run = REVIEW_DEPTH_POLICY.get(depth or "", ("ignore", False))
    return depth, depth_gate, bool(depth_requires_run or wf_requires_run)


def effective_review_policy(
    root: Path, strict: bool = False, recorded: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Resolve review gate policy from the execution + workflow contracts.

    Defaults: review_gate='warn', require_review_run=False. ``strict`` (CLI),
    ``AGENTFLOW_STRICT=1`` (env), and ``proof_policy.strict_by_default`` each
    promote 'warn' to 'block'.

    #74: the selected workflow profile's ``review_depth`` contributes a gate
    floor and a required-run bit (``REVIEW_DEPTH_POLICY``), and its
    ``proof_policy.require_review_run`` is OR'd in. The depth gate is joined over
    ignore<warn<block, so it can only raise strictness, never lower it.

    ratchet-v1: when ``recorded`` (a persisted build-time ``review.policy``
    block) is supplied, the resolved caller policy is joined with the recorded
    floor over ignore<warn<block, and a recorded ``require_review_run`` floors
    the requirement. A recorded build can only raise strictness at verify time,
    never lower it -- deleting the workflow contract before verify cannot drop a
    recorded run requirement.
    """
    policy: Dict[str, Any] = {}
    contract_path = root / ".agent/execution.contract.json"
    if contract_path.exists():
        data, _ = try_read_json(contract_path)
        if data and isinstance(data.get("proof_policy"), dict):
            policy = data["proof_policy"]
    gate = policy.get("review_gate", "warn")
    if gate not in REVIEW_GATE_POLICIES:
        gate = "warn"
    require_review_run = bool(policy.get("require_review_run", False))
    effective_strict = strict_mode(strict) or bool(policy.get("strict_by_default"))

    if recorded is None:
        required_review_depth, depth_gate, depth_requires_run = _workflow_review_policy(root)
    else:
        recorded_depth = recorded.get("required_review_depth")
        required_review_depth = (
            recorded_depth
            if isinstance(recorded_depth, str) and recorded_depth in REVIEW_DEPTH_POLICY
            else None
        )
        depth_gate, depth_requires_run = ("ignore", False)
    gate = join_review_gate(gate, depth_gate)
    require_review_run = require_review_run or depth_requires_run

    if gate == "warn" and effective_strict:
        gate = "block"
    if recorded:
        recorded_gate = recorded.get("review_gate_effective")
        if isinstance(recorded_gate, str) and recorded_gate in REVIEW_GATE_POLICIES:
            gate = join_review_gate(gate, recorded_gate)
        if recorded.get("proof_strict_effective"):
            effective_strict = True
        if recorded.get("require_review_run"):
            require_review_run = True
    return {
        "review_gate": gate,
        "require_review_run": require_review_run,
        "strict_effective": effective_strict,
        "required_review_depth": required_review_depth,
    }


def _amendment_receipts(root: Path, step_id: Any, attempt_id: Any) -> Dict[str, List[str]]:
    from .receipts import command_receipts, file_receipts

    commands = [
        r.get("id")
        for r in command_receipts(root)
        if r.get("step_id") == step_id and r.get("attempt_id") == attempt_id
    ]
    files = [
        r.get("id")
        for r in file_receipts(root)
        if r.get("step_id") == step_id and r.get("attempt_id") == attempt_id
    ]
    return {"command": [c for c in commands if c], "file": [f for f in files if f]}


def _run_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    findings = record.get("findings", {}) if isinstance(record.get("findings"), dict) else {}
    return {
        "review_run_id": record.get("review_run_id"),
        "plan_sha256": record.get("plan_sha256"),
        "gate_status": record.get("gate_status"),
        "active_blocking": list(record.get("active_blocking", []) or []),
        "counts_by_severity": findings.get("counts_by_severity", {}),
        "counts_by_status": findings.get("counts_by_status", {}),
        "artifacts": record.get("artifacts", []),
        "depth_profile": recorded_review_depth(record.get("depth_profile")),
    }


def review_summary(root: Path) -> Dict[str, Any]:
    """Build the proof ``review`` block from the ledger and amendment events."""
    from .execution import read_step_events

    runs = read_review_runs(root)
    by_id = {
        r["review_run_id"]: r
        for r in runs
        if isinstance(r.get("review_run_id"), str)
    }
    correlations: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for event in read_step_events(root):
        if event.get("event") != "amendment_started":
            continue
        for ref in event.get("finding_refs", []) or []:
            if not isinstance(ref, dict):
                continue
            rrid = ref.get("review_run_id")
            fid = ref.get("finding_id")
            amendment = {"step_id": event.get("step_id"), "attempt": event.get("attempt_id")}
            run = by_id.get(rrid)
            if run is None:
                unresolved.append({"review_run_id": rrid, "finding_id": fid, "amendment": amendment})
                continue
            status = None
            index = run.get("findings", {}).get("index", []) if isinstance(run.get("findings"), dict) else []
            for row in index:
                if isinstance(row, dict) and row.get("finding_id") == fid:
                    status = row.get("status")
                    break
            correlations.append(
                {
                    "review_run_id": rrid,
                    "finding_id": fid,
                    "amendment": amendment,
                    "receipts": _amendment_receipts(root, event.get("step_id"), event.get("attempt_id")),
                    "finding_final_status": status,
                }
            )
    return {
        "review_runs": [_run_summary(r) for r in runs],
        "latest_review_run_id": runs[-1].get("review_run_id") if runs else None,
        "correlations": correlations,
        "unresolved_finding_refs": unresolved,
    }


def build_time_review_policy(root: Path, strict: bool = False) -> Dict[str, Any]:
    """The effective review policy recorded into the proof at build time.

    This is the hash-bound floor honored at verify time (ratchet-v1).
    """
    policy = effective_review_policy(root, strict)
    return {
        "review_gate_effective": policy["review_gate"],
        "proof_strict_effective": policy["strict_effective"],
        "require_review_run": policy["require_review_run"],
        "required_review_depth": policy["required_review_depth"],
        "verification_semantics": REVIEW_VERIFICATION_SEMANTICS,
    }


def review_checks(
    root: Path,
    summary: Dict[str, Any],
    strict: bool = False,
    recorded: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Policy + integrity checks for the proof ``checks`` list."""
    policy = effective_review_policy(root, strict, recorded)
    gate = policy["review_gate"]
    runs = summary["review_runs"]
    checks: List[Dict[str, Any]] = []

    def _bad_status() -> str:
        if gate == "block":
            return "failed"
        if gate == "warn":
            return "warning"
        return "passed"

    # #74/#92: report required-vs-recorded review evidence by depth. A recorded
    # run satisfies the requirement when its declared depth is >= the required
    # depth; required_review_depth None means "a run is required, no depth floor".
    required_depth = policy.get("required_review_depth")
    if not policy["require_review_run"]:
        required_status = "passed"
        satisfied_by_depth = None
        required_message = f"no required review run (review_depth={required_depth})"
    else:
        recorded_depths = [recorded_review_depth(r.get("depth_profile")) for r in runs]
        satisfying = [d for d in recorded_depths if review_depth_satisfies(d, required_depth)]
        if satisfying:
            satisfied_by_depth = max(satisfying, key=review_depth_rank)
            required_status = "passed"
            required_message = (
                f"required review run satisfied by depth={satisfied_by_depth} "
                f"(review_depth={required_depth})"
            )
        else:
            satisfied_by_depth = (
                max(recorded_depths, key=review_depth_rank) if recorded_depths else None
            )
            required_status = {"block": "failed", "warn": "warning"}.get(gate, "not_run")
            if recorded_depths:
                required_message = (
                    f"review_depth={required_depth} requires a run at >= {required_depth}; "
                    f"deepest recorded run is {satisfied_by_depth}"
                )
            elif required_depth is None:
                required_message = "a review run is required; none recorded"
            else:
                required_message = (
                    f"review_depth={required_depth} requires a review run; none recorded"
                )
    checks.append(
        {
            "id": "required_review_satisfied",
            "status": required_status,
            "required_review_depth": required_depth,
            "satisfied_by_depth": satisfied_by_depth,
            "message": required_message,
        }
    )

    if not runs:
        if policy["require_review_run"]:
            status = "not_run" if gate == "ignore" else _bad_status()
            checks.append({"id": "review_gate", "status": status, "message": "no review run recorded"})
        else:
            checks.append({"id": "review_gate", "status": "not_run", "message": "no review run recorded"})
        return checks

    latest = runs[-1]
    blocking = latest.get("gate_status") != "pass" or bool(latest.get("active_blocking"))
    checks.append(
        {
            "id": "review_gate",
            "status": _bad_status() if blocking else "passed",
            "message": f"latest gate_status={latest.get('gate_status')}, "
            f"active_blocking={len(latest.get('active_blocking', []))}",
        }
    )

    missing_artifacts = 0
    for run in runs:
        for entry in run.get("artifacts", []):
            path = root / entry.get("path", "")
            if not path.exists():
                missing_artifacts += 1
    if missing_artifacts:
        checks.append(
            {
                "id": "review_artifacts_present",
                "status": "failed" if gate == "block" else "warning",
                "count": missing_artifacts,
                "message": "review source artifacts unavailable for rehash",
            }
        )
    else:
        checks.append({"id": "review_artifacts_present", "status": "passed", "count": 0})

    unresolved = summary["unresolved_finding_refs"]
    checks.append(
        {
            "id": "review_finding_refs_resolved",
            "status": ("failed" if gate == "block" else "warning") if unresolved else "passed",
            "count": len(unresolved),
        }
    )
    return checks


def verify_review_integrity(
    root: Path, strict: bool = False, recorded: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Verify review-run artifact hashes and emit policy findings.

    Always emits a hard integrity error on a present-but-mismatched artifact or a
    malformed ledger. Absent artifacts and gate/ref policy outcomes follow the
    effective review policy (warning by default, error under block/strict).
    """
    findings: List[Dict[str, Any]] = []
    try:
        runs = read_review_runs(root)
    except ValueError as exc:
        return [{"severity": "error", "message": f"malformed review-runs ledger: {exc}"}]

    policy = effective_review_policy(root, strict, recorded)

    # Item #1: surface an unrecognized recorded review_gate_effective so a
    # silently-disabled floor becomes visible (advisory, not a hard error).
    if isinstance(recorded, dict):
        recorded_gate = recorded.get("review_gate_effective")
        if isinstance(recorded_gate, str) and recorded_gate not in REVIEW_GATE_POLICIES:
            findings.append(
                {
                    "severity": "warning",
                    "message": (
                        f"review policy: unrecognized recorded review_gate_effective"
                        f" {recorded_gate!r}; ratchet floor not applied"
                    ),
                }
            )

    # Item #2: surface an unknown verification_semantics for forward-compat.
    # Absent (None) or current v1 value → no finding.
    if isinstance(recorded, dict):
        recorded_semantics = recorded.get("verification_semantics")
        if recorded_semantics is not None and recorded_semantics != REVIEW_VERIFICATION_SEMANTICS:
            findings.append(
                {
                    "severity": "warning",
                    "message": (
                        f"review policy: unknown verification_semantics"
                        f" {recorded_semantics!r}; interpreted as ratchet-v1"
                    ),
                }
            )

    missing_source_severity = "error" if policy["review_gate"] == "block" else "warning"

    for record in runs:
        manifest_rel = record.get("manifest_path")
        manifest_expected = record.get("manifest_sha256")
        if not isinstance(manifest_rel, str) or not isinstance(manifest_expected, str):
            findings.append({"severity": "error", "message": "review manifest entry is malformed"})
        else:
            manifest_path = root / manifest_rel
            if not manifest_path.exists():
                findings.append(
                    {
                        "severity": missing_source_severity,
                        "message": f"review manifest unavailable for rehash: {manifest_rel}",
                    }
                )
            elif sha256_file(manifest_path) != manifest_expected:
                findings.append(
                    {"severity": "error", "message": f"review manifest hash mismatch: {manifest_rel}"}
                )

        for entry in record.get("artifacts", []):
            rel = entry.get("path")
            expected = entry.get("sha256")
            if not isinstance(rel, str) or not isinstance(expected, str):
                findings.append({"severity": "error", "message": "review artifact entry is malformed"})
                continue
            path = root / rel
            if not path.exists():
                continue  # absence handled by policy check below
            if sha256_file(path) != expected:
                findings.append({"severity": "error", "message": f"review artifact hash mismatch: {rel}"})

    summary = review_summary(root)
    for check in review_checks(root, summary, strict, recorded):
        if check["status"] == "failed":
            findings.append({"severity": "error", "message": f"{check['id']}: {check.get('message', check['status'])}"})
        elif check["status"] == "warning":
            findings.append({"severity": "warning", "message": f"{check['id']}: {check.get('message', check['status'])}"})
    return findings
