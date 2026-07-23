#!/usr/bin/env python3
"""Validate the pre-bump schema-soak candidate and its frozen paths.

The eventual 1.0 work must add its narrowly tested version-only transition;
this guard deliberately grants no exemption while the 21-day soak is active.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = Path("docs/schema-freeze-soak.json")
MANIFEST_SCHEMA_VERSION = "0.1.0"
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
        "tests/test_schema_contracts.py",
        "tests/test_schema_soak.py",
        "tests/test_stuck.py",
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
        "released-v0.4.0",
    }
)

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_commit",
        "start_time_utc",
        "minimum_end_time_utc",
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


def _candidate_time(root: Path, candidate: str) -> datetime:
    value = _git(root, "show", "-s", "--format=%cI", candidate).stdout.strip()
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError as exc:
        raise SoakCheckError("candidate commit has an invalid commit timestamp") from exc


def _manifest_recorded_time(root: Path, candidate: str) -> datetime:
    commits = _git(
        root,
        "log",
        "--format=%H",
        "--reverse",
        "--",
        MANIFEST_PATH.as_posix(),
    ).stdout.splitlines()
    recorded_at: datetime | None = None
    for commit in commits:
        snapshot = _git(
            root,
            "show",
            f"{commit}:{MANIFEST_PATH.as_posix()}",
            check=False,
        )
        if snapshot.returncode != 0:
            recorded_at = None
            continue
        try:
            data = json.loads(snapshot.stdout)
        except json.JSONDecodeError:
            recorded_at = None
            continue
        if isinstance(data, dict) and data.get("candidate_commit") == candidate:
            if recorded_at is None:
                recorded_at = _candidate_time(root, commit)
        else:
            recorded_at = None
    if recorded_at is None:
        raise SoakCheckError("manifest candidate must be recorded in Git history")
    return recorded_at


def _candidate_schema_versions(root: Path, candidate: str) -> dict[str, str]:
    source = _git(
        root, "show", f"{candidate}:src/agentflow/contracts.py"
    ).stdout
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SoakCheckError("candidate contracts.py is not valid Python") from exc
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
        raise SoakCheckError("candidate contracts.py is missing load-bearing schema constants")
    return versions


def _validate_schema_versions(root: Path, candidate: str, value: Any) -> None:
    if not isinstance(value, dict) or set(value) != SCHEMA_CONSTANTS:
        raise SoakCheckError("schema_versions must contain exactly the eight load-bearing constants")
    if any(not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None for version in value.values()):
        raise SoakCheckError("schema_versions values must be MAJOR.MINOR.PATCH strings")
    if value != _candidate_schema_versions(root, candidate):
        raise SoakCheckError("schema_versions do not match candidate contracts.py")


def _git_blob(root: Path, candidate: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{candidate}:{path}"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SoakCheckError(detail or f"cannot read candidate path: {path}")
    return result.stdout


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


def _candidate_files(root: Path, candidate: str, paths: list[str]) -> dict[str, str]:
    result = _git(
        root, "ls-tree", "-r", candidate, "--", *paths
    )
    files: dict[str, str] = {}
    for line in result.stdout.splitlines():
        metadata, path = line.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        files[path] = mode
    return files


def _current_files(root: Path, paths: list[str]) -> dict[str, str]:
    result = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *paths,
    )
    files: dict[str, str] = {}
    for path in result.stdout.splitlines():
        target = root / path
        if target.is_symlink():
            files[path] = "120000"
        elif target.is_file():
            files[path] = "100755" if target.stat().st_mode & 0o111 else "100644"
    return files


def _validate_freeze_paths(root: Path, candidate: str, value: Any) -> None:
    if (
        not isinstance(value, list)
        or not all(isinstance(path, str) for path in value)
        or len(value) != len(set(value))
        or set(value) != FREEZE_PATHS
    ):
        raise SoakCheckError("freeze_paths must match the audited freeze set")
    paths = sorted(FREEZE_PATHS)
    candidate_files = _candidate_files(root, candidate, paths)
    missing_paths = [
        path
        for path in paths
        if path not in candidate_files
        and not any(file.startswith(path + "/") for file in candidate_files)
    ]
    if missing_paths:
        raise SoakCheckError(
            "freeze path missing from candidate: " + ", ".join(missing_paths)
        )
    current_files = _current_files(root, paths)
    differences = set(candidate_files) ^ set(current_files)
    for path in set(candidate_files) & set(current_files):
        if candidate_files[path] != current_files[path] or current_files[path] == "120000":
            differences.add(path)
            continue
        candidate_value = _semantic_value(path, _git_blob(root, candidate, path))
        try:
            current_data = (root / path).read_bytes()
        except OSError as exc:
            raise SoakCheckError(f"cannot read frozen path: {path}") from exc
        if candidate_value != _semantic_value(path, current_data):
            differences.add(path)
    if differences:
        raise SoakCheckError(
            "freeze set changed since candidate: " + ", ".join(sorted(differences))
        )


def _validate_workloads(
    value: Any,
    candidate: str,
    candidate_time: datetime,
    start_time: datetime,
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
        if recorded_at > start_time:
            raise SoakCheckError(
                f"workload {workload_id} recorded_at_utc must not be later than start_time_utc"
            )
        url = item["url"]
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise SoakCheckError(f"workload {workload_id} url must be null or non-empty")


def check_soak(root: Path) -> str:
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
        return f"schema soak not started: {MANIFEST_PATH.as_posix()} is absent"
    manifest = _read_manifest(manifest_path)
    candidate = _validate_candidate(root, manifest["candidate_commit"])
    candidate_time = _candidate_time(root, candidate)
    manifest_recorded_time = _manifest_recorded_time(root, candidate)
    start_time = _utc_timestamp(manifest["start_time_utc"], "start_time_utc")
    if start_time < candidate_time:
        raise SoakCheckError("start_time_utc must not be earlier than candidate_commit")
    if start_time < manifest_recorded_time:
        raise SoakCheckError("start_time_utc must not predate manifest record")
    minimum_end = _utc_timestamp(
        manifest["minimum_end_time_utc"], "minimum_end_time_utc"
    )
    if minimum_end - start_time != timedelta(days=21):
        raise SoakCheckError(
            "minimum_end_time_utc must be exactly 21 days after start_time_utc"
        )
    _validate_schema_versions(root, candidate, manifest["schema_versions"])
    _validate_workloads(
        manifest["workloads"], candidate, candidate_time, start_time
    )
    _validate_freeze_paths(root, candidate, manifest["freeze_paths"])
    return (
        f"schema soak guard passed: {candidate} unchanged through "
        f"{manifest['minimum_end_time_utc']}"
    )


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
