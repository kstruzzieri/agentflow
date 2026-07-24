#!/usr/bin/env python3
"""Validate the pre-bump schema-soak candidate and its frozen paths.

The soak clock starts with a successful GitHub Actions CI run on ``main``. Its
server-issued ``created_at`` timestamp cannot be back-dated by a contributor.
Until the clock and the required workloads complete, the freeze set must not
change at all and the load-bearing constants must stay pre-1.0.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib import error, request


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = Path("docs/schema-freeze-soak.json")
MANIFEST_SCHEMA_VERSION = "0.4.0"
CONTRACTS_PATH = "src/agentflow/contracts.py"
SOAK_DURATION = timedelta(days=21)
ONE_ZERO_VERSION = "1.0.0"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SEMVER_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")

SCHEMA_CONSTANTS = frozenset(
    {
        "PLAN_SCHEMA_VERSION",
        "EXECUTION_CONTRACT_SCHEMA_VERSION",
        "PROOF_PACK_SCHEMA_VERSION",
        "STEP_RUNS_SCHEMA_VERSION",
        "COMMAND_RECEIPTS_SCHEMA_VERSION",
        "FILE_RECEIPTS_SCHEMA_VERSION",
        "VERIFICATION_RUNS_SCHEMA_VERSION",
        "DRIFT_REPORT_SCHEMA_VERSION",
    }
)

FREEZE_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        "schemas/command-receipts.schema.json",
        "schemas/drift-report.schema.json",
        "schemas/execution-contract.schema.json",
        "schemas/file-receipts.schema.json",
        "schemas/plan-lock.schema.json",
        "schemas/proof-pack.schema.json",
        "schemas/step-runs.schema.json",
        "schemas/verification-runs.schema.json",
        "scripts/check_schema_soak.py",
        "src/agentflow/aggregate.py",
        "src/agentflow/artifacts.py",
        "src/agentflow/capabilities.py",
        "src/agentflow/cli.py",
        "src/agentflow/contracts.py",
        "src/agentflow/coverage.py",
        "src/agentflow/draft_plan.py",
        "src/agentflow/events.py",
        "src/agentflow/execution.py",
        "src/agentflow/execution_coverage.py",
        "src/agentflow/git.py",
        "src/agentflow/handoff.py",
        "src/agentflow/hunks.py",
        # execution.py, receipts.py, and review.py hold this lock while
        # appending the frozen ledgers; its correctness is what prevents
        # duplicate attempt and receipt ids, so it is load-bearing mutation
        # semantics even though it never names a schema constant.
        "src/agentflow/locks.py",
        "src/agentflow/packs.py",
        "src/agentflow/porcelain.py",
        "src/agentflow/proof.py",
        "src/agentflow/receipts.py",
        "src/agentflow/review.py",
        "src/agentflow/risk.py",
        "src/agentflow/runtime.py",
        "src/agentflow/stuck.py",
        "src/agentflow/validation.py",
        "src/agentflow/versioning.py",
        "src/agentflow/viewer.py",
        "src/agentflow/workflow_contract.py",
        "tests/fixtures/compatibility",
        "tests/fixtures/proof-bundle",
        "tests/test_aggregate.py",
        "tests/test_artifact_versioning.py",
        "tests/test_capabilities.py",
        "tests/test_ci_proof_bundle.py",
        "tests/test_cli.py",
        "tests/test_draft_plan.py",
        "tests/test_events.py",
        "tests/test_execution_contract.py",
        "tests/test_execution_state.py",
        "tests/test_execution_verification.py",
        "tests/test_handoff.py",
        "tests/test_hunks.py",
        "tests/test_lease_enforcement.py",
        "tests/test_lease_locking.py",
        "tests/test_packs.py",
        "tests/test_receipt_concurrency.py",
        "tests/test_porcelain.py",
        "tests/test_proof.py",
        "tests/test_proof_compatibility.py",
        "tests/test_receipts.py",
        "tests/test_review.py",
        "tests/test_risk.py",
        "tests/test_runtime.py",
        "tests/test_schema_contracts.py",
        "tests/test_schema_soak.py",
        "tests/test_stuck.py",
        "tests/test_versioning.py",
        "tests/test_view_proof.py",
        "tests/test_workflow_contract.py",
    }
)

WORKLOAD_IDS = frozenset(
    {
        "ci-proof",
        "mcp-stdio",
        "workflow-pack",
        "aggregation",
        "released-pyz",
    }
)

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_commit",
        "transition_commit",
        "workflow_run_id",
        "schema_versions",
        "freeze_paths",
        "workloads",
    }
)

# Each published schema and the constant whose value its pattern must accept.
SCHEMA_FILE_CONSTANTS = {
    "schemas/plan-lock.schema.json": "PLAN_SCHEMA_VERSION",
    "schemas/execution-contract.schema.json": "EXECUTION_CONTRACT_SCHEMA_VERSION",
    "schemas/proof-pack.schema.json": "PROOF_PACK_SCHEMA_VERSION",
    "schemas/step-runs.schema.json": "STEP_RUNS_SCHEMA_VERSION",
    "schemas/command-receipts.schema.json": "COMMAND_RECEIPTS_SCHEMA_VERSION",
    "schemas/file-receipts.schema.json": "FILE_RECEIPTS_SCHEMA_VERSION",
    "schemas/verification-runs.schema.json": "VERIFICATION_RUNS_SCHEMA_VERSION",
    "schemas/drift-report.schema.json": "DRIFT_REPORT_SCHEMA_VERSION",
}

# The execution contract is exact-version state. The other published
# load-bearing schemas admit the supported major through the supported minor.
EXACT_SCHEMA_PATHS = frozenset({"schemas/execution-contract.schema.json"})

# Immutable fixture trees. The 1.0 transition adds a fixture built by the 1.0
# code; issue #5 requires the existing snapshots to survive untouched, so these
# accept additions and nothing else.
FIXTURE_ROOTS = (
    "tests/fixtures/compatibility",
    "tests/fixtures/proof-bundle",
)


class SoakCheckError(ValueError):
    """The soak manifest or candidate state is invalid."""


class DuplicateJsonKeyError(ValueError):
    """A JSON object contains the same key more than once."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_unique_json_object)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise SoakCheckError(detail)
    return result


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = _load_json(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        raise SoakCheckError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SoakCheckError("manifest must be a JSON object")
    missing = sorted(MANIFEST_FIELDS - set(data))
    unknown = sorted(set(data) - MANIFEST_FIELDS)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        if unknown:
            details.append("unknown fields: " + ", ".join(unknown))
        raise SoakCheckError("; ".join(details))
    if data["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise SoakCheckError(
            f"schema_version must be {MANIFEST_SCHEMA_VERSION}"
        )
    return data


def _utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SoakCheckError(f"{field} must be an ISO-8601 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SoakCheckError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise SoakCheckError(f"{field} must be UTC")
    return parsed


def _validate_ancestor_commit(root: Path, value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise SoakCheckError(
            f"{field} must be an exact 40-character lowercase SHA"
        )
    resolved = _git(root, "rev-parse", "--verify", f"{value}^{{commit}}").stdout.strip()
    if resolved != value:
        raise SoakCheckError(f"{field} does not resolve to the recorded commit")
    ancestor = _git(root, "merge-base", "--is-ancestor", value, "HEAD", check=False)
    if ancestor.returncode != 0:
        raise SoakCheckError(f"{field} must be an ancestor of HEAD")
    return value


def _validate_candidate(root: Path, value: Any) -> str:
    return _validate_ancestor_commit(root, value, "candidate_commit")


def _validate_transition_commit(
    root: Path, value: Any, candidate: str
) -> str | None:
    if value is None:
        return None
    transition = _validate_ancestor_commit(root, value, "transition_commit")
    descendant = _git(
        root,
        "merge-base",
        "--is-ancestor",
        candidate,
        transition,
        check=False,
    )
    if descendant.returncode != 0 or transition == candidate:
        raise SoakCheckError(
            "transition_commit must descend from candidate_commit"
        )
    return transition


def _candidate_recording_commit(root: Path, candidate: str) -> str:
    """Return the first commit in the current uninterrupted candidate sequence."""
    commits = _git(
        root,
        "log",
        "--format=%H",
        "--reverse",
        "--",
        MANIFEST_PATH.as_posix(),
    ).stdout.split()
    recording_commit: str | None = None
    unreadable = False
    for commit in commits:
        snapshot = _git(
            root,
            "show",
            f"{commit}:{MANIFEST_PATH.as_posix()}",
            check=False,
        )
        if snapshot.returncode != 0:
            recording_commit = None
            continue
        try:
            data = _load_json(snapshot.stdout)
        except (json.JSONDecodeError, DuplicateJsonKeyError):
            unreadable = True
            recording_commit = None
            continue
        if isinstance(data, dict) and data.get("candidate_commit") == candidate:
            if recording_commit is None:
                recording_commit = commit
        else:
            recording_commit = None
    if recording_commit is not None:
        return recording_commit
    if unreadable:
        raise SoakCheckError(
            f"cannot determine the candidate recording: {MANIFEST_PATH.as_posix()} "
            "is unreadable in Git history"
        )
    raise SoakCheckError("manifest candidate must be recorded in Git history")


def _workflow_run_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SoakCheckError("workflow_run_id must be a positive GitHub Actions run id or null")
    return value


def _fetch_workflow_run(workflow_run_id: int) -> dict[str, Any]:
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        raise SoakCheckError(
            "cannot resolve trusted workflow run locally: GITHUB_REPOSITORY and GITHUB_TOKEN are required"
        )
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    endpoint = f"{api_url}/repos/{repository}/actions/runs/{workflow_run_id}"
    api_request = request.Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "agentflow-schema-soak-check",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with request.urlopen(api_request, timeout=10) as response:
            data = _load_json(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise SoakCheckError(
            f"cannot resolve trusted workflow run: GitHub API returned HTTP {exc.code}"
        ) from exc
    except (
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as exc:
        raise SoakCheckError(
            "cannot resolve trusted workflow run: GitHub API is unavailable"
        ) from exc
    if not isinstance(data, dict):
        raise SoakCheckError("cannot resolve trusted workflow run: invalid GitHub API response")
    return data


def _trusted_soak_start(
    root: Path, workflow_run: dict[str, Any], recording_commit: str
) -> datetime:
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_repository = workflow_run.get("repository")
    if (
        not isinstance(repository, str)
        or not isinstance(run_repository, dict)
        or run_repository.get("full_name") != repository
        or workflow_run.get("path") != ".github/workflows/ci.yml"
    ):
        raise SoakCheckError("workflow run must belong to this repository's CI workflow")
    if (
        workflow_run.get("event") != "push"
        or workflow_run.get("status") != "completed"
        or workflow_run.get("conclusion") != "success"
        or workflow_run.get("head_branch") != "main"
    ):
        raise SoakCheckError("workflow run must be a completed successful push run on main")
    head_sha = workflow_run.get("head_sha")
    if not isinstance(head_sha, str) or SHA_RE.fullmatch(head_sha) is None:
        raise SoakCheckError("workflow run head_sha must be an exact commit SHA")
    if _git(root, "merge-base", "--is-ancestor", recording_commit, head_sha, check=False).returncode != 0:
        raise SoakCheckError("workflow run head_sha must contain the candidate recording commit")
    return _utc_timestamp(workflow_run.get("created_at"), "workflow run created_at")


def _schema_versions(source: str, label: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SoakCheckError(f"{label} is not valid Python") from exc
    versions: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id in SCHEMA_CONSTANTS
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            versions[target.id] = node.value.value
    if set(versions) != SCHEMA_CONSTANTS:
        raise SoakCheckError(f"{label} is missing load-bearing schema constants")
    return versions


def _version_blind(source: str, label: str) -> str:
    """Dump the AST with the load-bearing constant *values* erased.

    Two ``contracts.py`` revisions compare equal here exactly when they differ
    only in those version strings, which is the one post-soak change issue #5
    allows. Everything else in the file still has to match byte-for-byte after
    AST normalization.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SoakCheckError(f"{label} is not valid Python") from exc
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id in SCHEMA_CONSTANTS
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _schema_version_pattern(data: bytes) -> str | None:
    value = _load_json(data.decode("utf-8", errors="replace"))
    if not isinstance(value, dict):
        return None
    spec = value.get("properties", {})
    if not isinstance(spec, dict):
        return None
    field = spec.get("schema_version")
    if not isinstance(field, dict):
        return None
    pattern = field.get("pattern")
    return pattern if isinstance(pattern, str) else None


def _pattern_blind(data: bytes, path: str) -> str:
    """Canonicalize a published schema with its ``schema_version`` pattern erased.

    Two revisions compare equal here exactly when they differ only in that
    pattern -- the one schema edit the 1.0 transition needs.
    """
    try:
        value = _load_json(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise SoakCheckError(f"frozen JSON path is invalid: {path}") from exc
    if isinstance(value, dict):
        spec = value.get("properties")
        if isinstance(spec, dict) and isinstance(spec.get("schema_version"), dict):
            spec["schema_version"] = {
                key: item
                for key, item in spec["schema_version"].items()
                if key != "pattern"
            }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _semver_blind(source: str, label: str) -> str:
    """Dump a Python AST with every semver-shaped string literal erased.

    Version-pinning tests restate the load-bearing constants verbatim, so the
    1.0 transition has to touch them. Comparing with those literals erased means
    a test may change which versions it pins and nothing else -- no assertion,
    fixture, or control flow can ride along.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SoakCheckError(f"{label} is not valid Python") from exc
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SEMVER_RE.fullmatch(node.value) is not None
        ):
            node.value = ""
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _is_schema_pattern_bump(candidate_data: bytes, current_data: bytes, path: str) -> bool:
    """True when a published schema changed only its ``schema_version`` pattern."""
    return _pattern_blind(candidate_data, path) == _pattern_blind(current_data, path)


def _require_pattern_coherence(root: Path, supported: dict[str, str]) -> None:
    """Every published pattern must match the runtime's 1.0 policy.

    Checked across all eight schemas, not just the edited ones: a schema left
    untouched is byte-identical to the candidate and would otherwise sail past
    the freeze diff while its pattern still rejects the new version. That silent
    half-transition is the incoherence #5's "patterns and validators must agree"
    acceptance criterion exists to prevent.
    """
    incoherent = []
    for path, constant in sorted(SCHEMA_FILE_CONSTANTS.items()):
        version = supported.get(constant)
        if version is None:
            continue
        try:
            pattern = _schema_version_pattern((root / path).read_bytes())
        except (OSError, json.JSONDecodeError, DuplicateJsonKeyError) as exc:
            raise SoakCheckError(f"frozen JSON path is invalid: {path}") from exc
        if pattern is None:
            incoherent.append(f"{path} declares no schema_version pattern")
            continue
        major, minor, _ = _version_tuple(version)
        expected = (
            rf"^{re.escape(version)}$"
            if path in EXACT_SCHEMA_PATHS
            else rf"^{major}\.{minor}\.(?:0|[1-9][0-9]*)$"
        )
        if pattern != expected:
            incoherent.append(
                f"{path} pattern disagrees with the runtime validator: "
                f"{constant} {version} requires {expected}"
            )
    if incoherent:
        raise SoakCheckError(
            "published schema patterns disagree with the declared constants: "
            + "; ".join(incoherent)
        )


def _require_one_zero_transition(current: dict[str, str]) -> None:
    if set(current.values()) != {ONE_ZERO_VERSION}:
        raise SoakCheckError(
            "the post-soak transition must set all eight load-bearing "
            f"schema constants to exactly {ONE_ZERO_VERSION}"
        )


def _require_pre_1_0(versions: dict[str, str], detail: str) -> None:
    # ponytail: the literal 0.x ceiling is issue #5's 1.0 freeze. A later 2.0
    # soak swaps this for the then-current released major.
    if any(
        SEMVER_RE.fullmatch(version) is None or not version.startswith("0.")
        for version in versions.values()
    ):
        raise SoakCheckError(detail)


def _current_schema_versions(root: Path) -> dict[str, str]:
    path = root / CONTRACTS_PATH
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SoakCheckError(f"cannot read {path}: {exc}") from exc
    return _schema_versions(source, str(path))


def _validate_schema_versions(value: Any, blob: bytes) -> None:
    if not isinstance(value, dict) or set(value) != SCHEMA_CONSTANTS:
        raise SoakCheckError("schema_versions must contain exactly the eight load-bearing constants")
    if any(not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None for version in value.values()):
        raise SoakCheckError("schema_versions values must be MAJOR.MINOR.PATCH strings")
    candidate_versions = _schema_versions(
        blob.decode("utf-8", errors="replace"), "candidate contracts.py"
    )
    if value != candidate_versions:
        raise SoakCheckError("schema_versions do not match candidate contracts.py")
    _require_pre_1_0(
        candidate_versions,
        "candidate_commit must record pre-1.0 schema constants; 1.0 is reached "
        "by the post-soak version-only bump",
    )


def _is_version_only_bump(candidate_source: str, current_source: str) -> bool:
    """True when ``contracts.py`` changed only by increasing load-bearing versions."""
    if _version_blind(candidate_source, "candidate contracts.py") != _version_blind(
        current_source, CONTRACTS_PATH
    ):
        return False
    before = _schema_versions(candidate_source, "candidate contracts.py")
    after = _schema_versions(current_source, CONTRACTS_PATH)
    for name, new_version in after.items():
        old_version = before[name]
        if new_version == old_version:
            continue
        if (
            SEMVER_RE.fullmatch(new_version) is None
            or _version_tuple(new_version) <= _version_tuple(old_version)
        ):
            return False
    return True


def _semantic_value(path: str, data: bytes) -> Any:
    if path.endswith(".py"):
        try:
            return ast.dump(
                ast.parse(data.decode("utf-8")),
                annotate_fields=True,
                include_attributes=False,
            )
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise SoakCheckError(f"frozen Python path is invalid: {path}") from exc
    if path.endswith(".json"):
        try:
            value = _load_json(data.decode("utf-8"))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            DuplicateJsonKeyError,
        ) as exc:
            raise SoakCheckError(f"frozen JSON path is invalid: {path}") from exc
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    return data


def _candidate_tree(root: Path, candidate: str, paths: Sequence[str]) -> dict[str, str]:
    """Map every frozen path in the candidate tree to its Git file mode."""
    result = _git(root, "ls-tree", "-r", "-z", candidate, "--", *paths)
    tree: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        tree[path] = metadata.split(" ", 1)[0]
    return tree


def _candidate_blobs(
    root: Path, candidate: str, paths: Sequence[str]
) -> dict[str, bytes]:
    """Read every frozen blob in one `git cat-file --batch`.

    One `git show` per path costs ~150 subprocesses on the real freeze set; this
    is a single pipe, which keeps the guard well under a second on every job in
    the CI matrix.
    """
    if not paths:
        return {}
    # A newline in a frozen path would desynchronize the batch protocol below.
    unsafe = sorted(path for path in paths if "\n" in path)
    if unsafe:
        raise SoakCheckError("frozen path contains a newline: " + ", ".join(unsafe))
    request = "".join(f"{candidate}:{path}\n" for path in paths).encode("utf-8")
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input=request,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SoakCheckError(detail or "cannot read candidate blobs")
    blobs: dict[str, bytes] = {}
    stream = result.stdout
    offset = 0
    for path in paths:
        end = stream.find(b"\n", offset)
        if end < 0:
            raise SoakCheckError(f"cannot read candidate path: {path}")
        header = stream[offset:end].decode("utf-8", errors="replace")
        offset = end + 1
        parts = header.rsplit(" ", 2)
        if len(parts) != 3 or not parts[2].isdigit():
            raise SoakCheckError(f"cannot read candidate path: {path}")
        size = int(parts[2])
        blobs[path] = stream[offset : offset + size]
        offset += size + 1
    return blobs


def _current_mode(root: Path, path: str) -> str | None:
    target = root / path
    if target.is_symlink():
        return "120000"
    if not target.is_file():
        return None
    return "100755" if target.stat().st_mode & 0o111 else "100644"


def _current_tree(root: Path, paths: Sequence[str]) -> dict[str, str]:
    result = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *paths,
    )
    tree: dict[str, str] = {}
    for path in result.stdout.split("\0"):
        if not path:
            continue
        mode = _current_mode(root, path)
        if mode is not None:
            tree[path] = mode
    return tree


def _current_bytes(root: Path, path: str, mode: str) -> bytes:
    target = root / path
    try:
        if mode == "120000":
            return os.readlink(target).encode("utf-8")
        return target.read_bytes()
    except OSError as exc:
        raise SoakCheckError(f"cannot read frozen path: {path}") from exc


def _require_current_tree_matches_commit(
    root: Path, commit: str, paths: Sequence[str]
) -> None:
    expected_tree = _candidate_tree(root, commit, paths)
    current_tree = _current_tree(root, paths)
    differences = set(expected_tree) ^ set(current_tree)
    blobs = _candidate_blobs(root, commit, sorted(expected_tree))
    for path in sorted(set(expected_tree) & set(current_tree)):
        if (
            expected_tree[path] != current_tree[path]
            or blobs[path] != _current_bytes(root, path, current_tree[path])
        ):
            differences.add(path)
    if differences:
        raise SoakCheckError(
            "freeze set changed after transition_commit: "
            + ", ".join(sorted(differences))
        )


def _frozen_paths_present(root: Path) -> None:
    """Catch a stale ``FREEZE_PATHS`` entry before the soak makes it load-bearing.

    The list is hand-maintained; without this a rename would sit undetected
    until the day someone tries to start the clock.
    """
    missing = sorted(path for path in FREEZE_PATHS if not (root / path).exists())
    if missing:
        raise SoakCheckError(
            "freeze path missing from the working tree: " + ", ".join(missing)
        )


def _validate_freeze_paths(
    root: Path,
    value: Any,
    blobs: dict[str, bytes],
    candidate_tree: dict[str, str],
    allow_transition: bool,
) -> None:
    """Diff the declared freeze set between the candidate and the working tree.

    ``allow_transition`` opens issue #5's post-soak window. It permits exactly
    the mechanical parts of the 1.0 transition -- a version-only ``contracts.py``
    bump, a schema whose only edit is a ``schema_version`` pattern that accepts
    the new constant, a test whose only edit is which semver literals it pins,
    and additions under the immutable fixture trees. Everything else still
    fails, in that window as much as outside it.
    """
    if (
        not isinstance(value, list)
        or not all(isinstance(path, str) for path in value)
        or len(value) != len(set(value))
        or set(value) != FREEZE_PATHS
    ):
        raise SoakCheckError("freeze_paths must match the audited freeze set")
    current_tree = _current_tree(root, sorted(FREEZE_PATHS))
    candidate_supported = (
        _schema_versions(
            blobs[CONTRACTS_PATH].decode("utf-8", errors="replace"),
            "candidate contracts.py",
        )
        if CONTRACTS_PATH in blobs
        else {}
    )
    transition_allowed = False
    if allow_transition:
        current_supported = _current_schema_versions(root)
        if current_supported != candidate_supported:
            _require_one_zero_transition(current_supported)
            _require_pattern_coherence(root, current_supported)
            transition_allowed = True

    added = set(current_tree) - set(candidate_tree)
    removed = set(candidate_tree) - set(current_tree)
    differences = set(removed)
    for path in sorted(added):
        # A new fixture is the one addition #5 asks for; anything else appearing
        # inside the freeze set is drift.
        if transition_allowed and any(
            path.startswith(root_path + "/") for root_path in FIXTURE_ROOTS
        ):
            continue
        differences.add(path)

    for path in sorted(set(candidate_tree) & set(current_tree)):
        if candidate_tree[path] != current_tree[path]:
            differences.add(path)
            continue
        candidate_data = blobs[path]
        current_data = _current_bytes(root, path, current_tree[path])
        if candidate_data == current_data:
            continue
        if transition_allowed:
            if path == CONTRACTS_PATH and _is_version_only_bump(
                candidate_data.decode("utf-8", errors="replace"),
                current_data.decode("utf-8", errors="replace"),
            ):
                continue
            if path in SCHEMA_FILE_CONSTANTS and _is_schema_pattern_bump(
                candidate_data, current_data, path
            ):
                continue
            if (
                path.startswith("tests/")
                and path.endswith(".py")
                and _semver_blind(
                    candidate_data.decode("utf-8", errors="replace"), path
                )
                == _semver_blind(current_data.decode("utf-8", errors="replace"), path)
            ):
                continue
        if _semantic_value(path, candidate_data) != _semantic_value(path, current_data):
            differences.add(path)
    if differences:
        raise SoakCheckError(
            "freeze set changed since candidate: " + ", ".join(sorted(differences))
        )


def _validate_workloads(
    value: Any,
    candidate: str,
    start_time: datetime | None,
    now: datetime,
) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SoakCheckError("workloads must be an array of objects")
    ids = [item.get("id") for item in value]
    if not all(isinstance(workload_id, str) for workload_id in ids):
        raise SoakCheckError("workload ids must be strings")
    if len(ids) != len(set(ids)):
        raise SoakCheckError("workloads must not contain duplicate workload ids")
    unknown_ids = sorted(set(ids) - WORKLOAD_IDS)
    if unknown_ids:
        raise SoakCheckError("workloads contain unknown ids: " + ", ".join(unknown_ids))
    if start_time is None:
        if value:
            raise SoakCheckError(
                "workloads require a trusted main-CI observation before recording evidence"
            )
        return frozenset()
    expected_fields = {
        "id",
        "command",
        "commit",
        "outcome",
        "recorded_at_utc",
        "url",
    }
    for item in value:
        workload_id = item["id"]
        if set(item) != expected_fields:
            raise SoakCheckError(f"workload {workload_id} has invalid fields")
        if not isinstance(item["command"], str) or not item["command"].strip():
            raise SoakCheckError(f"workload {workload_id} command must be non-empty")
        if item["commit"] != candidate:
            raise SoakCheckError(f"workload {workload_id} commit must equal candidate_commit")
        if item["outcome"] != "passed":
            raise SoakCheckError(f"workload {workload_id} outcome must be passed")
        recorded_at = _utc_timestamp(
            item["recorded_at_utc"], f"workload {workload_id} recorded_at_utc"
        )
        if recorded_at < start_time:
            raise SoakCheckError(
                f"workload {workload_id} recorded_at_utc must not be earlier than trusted soak start"
            )
        # Issue #5 requires these workloads to be exercised *during* the soak, so
        # the only upper bound is the present.
        if recorded_at > now:
            raise SoakCheckError(
                f"workload {workload_id} recorded_at_utc must not be in the future"
            )
        url = item["url"]
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise SoakCheckError(f"workload {workload_id} url must be null or non-empty")
    return frozenset(ids)


def check_soak(root: Path) -> str:
    now = datetime.now(timezone.utc)
    manifest_path = root / MANIFEST_PATH
    if not manifest_path.exists():
        history = _git(
            root,
            "log",
            "-1",
            "--format=%H",
            "--",
            MANIFEST_PATH.as_posix(),
        )
        if history.stdout.strip():
            raise SoakCheckError("manifest was removed after the soak started")
        _frozen_paths_present(root)
        _require_pre_1_0(
            _current_schema_versions(root),
            "load-bearing schema constants must remain pre-1.0 until the soak starts",
        )
        return f"schema soak not started: {MANIFEST_PATH.as_posix()} is absent"

    manifest = _read_manifest(manifest_path)
    candidate = _validate_candidate(root, manifest["candidate_commit"])
    transition = _validate_transition_commit(
        root, manifest["transition_commit"], candidate
    )
    recording_commit = _candidate_recording_commit(root, candidate)

    paths = sorted(FREEZE_PATHS)
    candidate_tree = _candidate_tree(root, candidate, paths)
    missing_paths = [
        path
        for path in paths
        if path not in candidate_tree
        and not any(file.startswith(path + "/") for file in candidate_tree)
    ]
    if missing_paths:
        raise SoakCheckError(
            "freeze path missing from candidate: " + ", ".join(missing_paths)
        )
    blobs = _candidate_blobs(root, candidate, sorted(candidate_tree))

    _validate_schema_versions(manifest["schema_versions"], blobs[CONTRACTS_PATH])
    workflow_run_id = _workflow_run_id(manifest["workflow_run_id"])
    if workflow_run_id is None:
        if transition is not None:
            raise SoakCheckError(
                "transition_commit requires a completed soak and all workloads"
            )
        _validate_workloads(manifest["workloads"], candidate, None, now)
        _validate_freeze_paths(
            root,
            manifest["freeze_paths"],
            blobs,
            candidate_tree,
            allow_transition=False,
        )
        return f"schema soak awaiting trusted main-CI observation: {candidate}"

    start_time = _trusted_soak_start(
        root, _fetch_workflow_run(workflow_run_id), recording_commit
    )
    minimum_end = start_time + SOAK_DURATION
    elapsed = now >= minimum_end
    recorded_workloads = _validate_workloads(
        manifest["workloads"], candidate, start_time, now
    )
    completed_workloads = recorded_workloads == WORKLOAD_IDS
    current_versions = _current_schema_versions(root)
    transitioning = current_versions != manifest["schema_versions"]
    if transitioning and transition is None:
        raise SoakCheckError(
            "transition_commit must record the coordinated 1.0 transition"
        )
    if transition is not None:
        if not elapsed or not completed_workloads:
            raise SoakCheckError(
                "transition_commit requires a completed soak and all workloads"
            )
        _require_one_zero_transition(current_versions)
    _validate_freeze_paths(
        root,
        manifest["freeze_paths"],
        blobs,
        candidate_tree,
        allow_transition=transition is not None,
    )
    if transition is not None:
        _require_current_tree_matches_commit(root, transition, paths)

    stamp = minimum_end.isoformat().replace("+00:00", "Z")
    if not elapsed:
        remaining = minimum_end - now
        return (
            f"schema soak in progress: {candidate} unchanged, "
            f"{remaining.days}d {remaining.seconds // 3600}h remain "
            f"(minimum end {stamp})"
        )
    if not completed_workloads:
        pending = ", ".join(sorted(WORKLOAD_IDS - recorded_workloads))
        return (
            f"schema soak in progress: {candidate} pending workloads: {pending} "
            f"(minimum end {stamp})"
        )
    if transition is not None:
        return (
            f"schema transition complete: {transition} records "
            f"{ONE_ZERO_VERSION} after soak {candidate}"
        )
    return f"schema soak complete: {candidate} unchanged through {stamp}"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        message = check_soak(args.root.resolve())
    except SoakCheckError as exc:
        sys.stderr.write(f"schema soak check failed: {exc}\n")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
