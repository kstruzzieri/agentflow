#!/usr/bin/env python3
"""Validate the pre-bump schema-soak candidate and its frozen paths.

The soak clock is a Git fact. It starts at the commit that first records the
candidate in ``docs/schema-freeze-soak.json`` and runs for 21 days; the manifest
never declares it, so it cannot be back-dated. Until the clock elapses the
freeze set must not change at all and the load-bearing constants must stay
pre-1.0. Once it elapses the guard grants exactly one carve-out -- issue #5's
version-only bump of those constants -- and nothing else.
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


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = Path("docs/schema-freeze-soak.json")
MANIFEST_SCHEMA_VERSION = "0.2.0"
CONTRACTS_PATH = "src/agentflow/contracts.py"
SOAK_DURATION = timedelta(days=21)
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
        "tests/test_packs.py",
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
        "schema_versions",
        "freeze_paths",
        "workloads",
    }
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


def _validate_candidate(root: Path, value: Any) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise SoakCheckError("candidate_commit must be an exact 40-character lowercase SHA")
    resolved = _git(root, "rev-parse", "--verify", f"{value}^{{commit}}").stdout.strip()
    if resolved != value:
        raise SoakCheckError("candidate_commit does not resolve to the recorded commit")
    ancestor = _git(root, "merge-base", "--is-ancestor", value, "HEAD", check=False)
    if ancestor.returncode != 0:
        raise SoakCheckError("candidate_commit must be an ancestor of HEAD")
    return value


def _commit_time(root: Path, commit: str) -> datetime:
    value = _git(root, "show", "-s", "--format=%cI", commit).stdout.strip()
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError as exc:
        raise SoakCheckError(f"commit {commit} has an invalid commit timestamp") from exc


def _soak_start_time(root: Path, candidate: str) -> datetime:
    """Return the commit time of the commit that first recorded this candidate.

    Deriving the start from Git rather than a declared manifest field is what
    makes the clock un-back-datable: shortening the soak would require rewriting
    published history rather than editing a string.
    """
    commits = _git(
        root,
        "log",
        "--format=%H",
        "--reverse",
        "--",
        MANIFEST_PATH.as_posix(),
    ).stdout.split()
    started_at: datetime | None = None
    unreadable = False
    for commit in commits:
        snapshot = _git(
            root,
            "show",
            f"{commit}:{MANIFEST_PATH.as_posix()}",
            check=False,
        )
        if snapshot.returncode != 0:
            started_at = None
            continue
        try:
            data = _load_json(snapshot.stdout)
        except (json.JSONDecodeError, DuplicateJsonKeyError):
            unreadable = True
            started_at = None
            continue
        if isinstance(data, dict) and data.get("candidate_commit") == candidate:
            if started_at is None:
                started_at = _commit_time(root, commit)
        else:
            started_at = None
    if started_at is None:
        if unreadable:
            raise SoakCheckError(
                f"cannot determine the soak start: {MANIFEST_PATH.as_posix()} is "
                "unreadable in Git history"
            )
        raise SoakCheckError("manifest candidate must be recorded in Git history")
    return started_at


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
    allow_version_only: bool,
) -> None:
    if (
        not isinstance(value, list)
        or not all(isinstance(path, str) for path in value)
        or len(value) != len(set(value))
        or set(value) != FREEZE_PATHS
    ):
        raise SoakCheckError("freeze_paths must match the audited freeze set")
    current_tree = _current_tree(root, sorted(FREEZE_PATHS))
    differences = set(candidate_tree) ^ set(current_tree)
    for path in sorted(set(candidate_tree) & set(current_tree)):
        if candidate_tree[path] != current_tree[path]:
            differences.add(path)
            continue
        candidate_data = blobs[path]
        current_data = _current_bytes(root, path, current_tree[path])
        if candidate_data == current_data:
            continue
        if path == CONTRACTS_PATH and allow_version_only:
            if _is_version_only_bump(
                candidate_data.decode("utf-8", errors="replace"),
                current_data.decode("utf-8", errors="replace"),
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
    candidate_time: datetime,
    now: datetime,
) -> None:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SoakCheckError("workloads must be an array of objects")
    ids = [item.get("id") for item in value]
    if not all(isinstance(workload_id, str) for workload_id in ids):
        raise SoakCheckError("workload ids must be strings")
    if len(ids) != len(set(ids)) or set(ids) != WORKLOAD_IDS:
        raise SoakCheckError("workloads must record every required soak workload once")
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
        if recorded_at < candidate_time:
            raise SoakCheckError(
                f"workload {workload_id} recorded_at_utc must not be earlier than candidate_commit"
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
    candidate_time = _commit_time(root, candidate)
    start_time = _soak_start_time(root, candidate)
    if start_time < candidate_time:
        raise SoakCheckError("the recording commit must not predate candidate_commit")
    minimum_end = start_time + SOAK_DURATION
    elapsed = now >= minimum_end

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
    _validate_workloads(manifest["workloads"], candidate, candidate_time, now)
    _validate_freeze_paths(
        root,
        manifest["freeze_paths"],
        blobs,
        candidate_tree,
        allow_version_only=elapsed,
    )

    stamp = minimum_end.isoformat().replace("+00:00", "Z")
    if not elapsed:
        remaining = minimum_end - now
        return (
            f"schema soak in progress: {candidate} unchanged, "
            f"{remaining.days}d {remaining.seconds // 3600}h remain "
            f"(minimum end {stamp})"
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
