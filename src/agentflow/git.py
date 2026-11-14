"""Git helpers used by drift auditing."""

from __future__ import annotations

import os
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .artifacts import read_jsonl
from .contracts import (
    ARTIFACT_COMPATIBILITY_POLICIES,
    EXECUTION_ARTIFACT_PATHS,
    RUN_ANCHORS_SCHEMA_VERSION,
)
from .versioning import validate_schema_version_policy


def run_git(root: Path, args: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Text mode would translate CR/LF inside NUL-delimited filenames.
    return proc.returncode, os.fsdecode(proc.stdout).rstrip(), os.fsdecode(proc.stderr).rstrip()


def is_git_repo(root: Path) -> bool:
    code, _, _ = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return code == 0


def head_commit(root: Path) -> Optional[str]:
    """HEAD's commit, or None on an unborn branch. Other Git failures raise."""
    code, stdout, _ = run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    if code == 0 and stdout.strip():
        return stdout.strip()
    code, _, stderr = run_git(root, ["rev-parse", "--git-dir"])
    if code != 0:
        raise RuntimeError(stderr or "git rev-parse failed")
    return None


# --- #139: the comparison base for changed-file detection -----------------

RUN_ANCHORS_PATH = EXECUTION_ARTIFACT_PATHS["run-anchors"]
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class BaselineError(RuntimeError, ValueError):
    """The comparison base cannot be trusted; every drift check fails closed.

    A RuntimeError so callers that already treat a Git failure as fail-closed
    do so here; a ValueError so ``cli.main`` prints one clean line.
    """

    def __init__(self, message: str, source: str) -> None:
        super().__init__(message)
        self.source = source


# One resolution per command, like execution_contract_snapshot: every check a
# command runs compares against the same base, even if the ledger or history
# moves under it. Outside a snapshot (direct library calls) each read is fresh.
_COMPARISON: ContextVar[Optional[Dict[str, Any]]] = ContextVar("agentflow_comparison", default=None)


@contextmanager
def comparison_snapshot(base_ref: Optional[str] = None) -> Iterator[None]:
    token = _COMPARISON.set({"base_ref": base_ref, "cache": {}})
    try:
        yield
    finally:
        _COMPARISON.reset(token)


def comparison_base(root: Path) -> Dict[str, Any]:
    """``{"source", "commit", "tree", ["ref"]}`` changed files are measured from.

    ``source`` is ``base-ref`` (``--base-ref``), ``run-anchor`` (the row
    ``init-execution`` recorded) or ``head`` (no anchor: a run recorded by an
    older version keeps the ``git status`` behaviour). ``tree`` is the diff
    target: the commit, or the empty tree for a run started before any commit.
    Raises BaselineError rather than fall back to a weaker comparison.
    """
    snapshot = _COMPARISON.get()
    if snapshot is None:
        return _resolve_comparison(root, None)
    key = str(Path(root).absolute())
    cache = snapshot["cache"]
    if key not in cache:
        try:
            cache[key] = _resolve_comparison(root, snapshot["base_ref"])
        except BaselineError as exc:
            cache[key] = exc
    value = cache[key]
    if isinstance(value, BaselineError):
        raise value
    return value


def baseline_summary(base: Dict[str, Any]) -> Dict[str, Any]:
    """The comparison base as drift reports and verification runs record it."""
    return {key: base[key] for key in ("source", "commit", "ref") if key in base}


def _resolve_comparison(root: Path, base_ref: Optional[str]) -> Dict[str, Any]:
    if base_ref is not None:
        return _resolve_base_ref(root, base_ref)
    path = root / RUN_ANCHORS_PATH
    if not path.exists():
        if _receipts_name_a_baseline(root):
            raise BaselineError(
                f"{RUN_ANCHORS_PATH} is missing, but file receipts were recorded against a run "
                "baseline; restore it, or check with --base-ref REF",
                "run-anchor",
            )
        return {"source": "head", "commit": None, "tree": None}
    commit = _read_anchor(path)
    if commit is None:
        return {"source": "run-anchor", "commit": None, "tree": _empty_tree(root)}
    code, _, _ = run_git(root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    if code != 0:
        raise BaselineError(
            f"run baseline {commit[:12]} is not in this clone; fetch the full history "
            "(for example fetch-depth: 0), or check with --base-ref REF",
            "run-anchor",
        )
    code, _, stderr = run_git(root, ["merge-base", "--is-ancestor", commit, "HEAD"])
    if code == 1:
        raise BaselineError(
            f"run baseline {commit[:12]} is not an ancestor of HEAD: history was rewritten "
            "(rebase or amend). Check with --base-ref REF naming the commit the work now "
            "starts from, or start a new run with init-execution --force",
            "run-anchor",
        )
    if code != 0:
        raise BaselineError(f"cannot check run baseline {commit[:12]}: {stderr}", "run-anchor")
    return {"source": "run-anchor", "commit": commit, "tree": commit}


def _read_anchor(path: Path) -> Optional[str]:
    def bad(reason: str) -> BaselineError:
        return BaselineError(f"{RUN_ANCHORS_PATH} {reason}; drift checks cannot trust it", "run-anchor")

    try:
        rows = read_jsonl(path)
    except (OSError, ValueError) as exc:
        raise bad(f"is unreadable ({exc})") from None
    if len(rows) != 1:
        raise bad(f"must hold exactly one row, found {len(rows)}")
    row = rows[0]
    problems = validate_schema_version_policy(
        row.get("schema_version"),
        RUN_ANCHORS_SCHEMA_VERSION,
        "run-anchors",
        ARTIFACT_COMPATIBILITY_POLICIES["run-anchors"],
    )
    if problems:
        raise bad("; ".join(problems))
    commit = row.get("base_commit", "missing")
    if commit is not None and not (isinstance(commit, str) and _COMMIT.match(commit)):
        raise bad("base_commit must be a commit id or null")
    return commit


def _receipts_name_a_baseline(root: Path) -> bool:
    receipts = root / EXECUTION_ARTIFACT_PATHS["file-receipts"]
    return any("hunk_base_commit" in row for row in read_jsonl(receipts, objects_only=False) if isinstance(row, dict))


def _empty_tree(root: Path) -> str:
    # Computed, not hard-coded: a SHA-256 repository has a different empty tree.
    proc = subprocess.run(
        ["git", "-C", str(root), "hash-object", "-t", "tree", "--stdin"],
        input=b"", check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise BaselineError(f"cannot compute the empty tree: {os.fsdecode(proc.stderr).strip()}", "run-anchor")
    return os.fsdecode(proc.stdout).strip()


def _resolve_base_ref(root: Path, ref: str) -> Dict[str, Any]:
    if not ref or ref.startswith("-"):
        raise BaselineError(f"--base-ref {ref!r} is not a commit", "base-ref")
    code, stdout, _ = run_git(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if code != 0 or not stdout.strip():
        raise BaselineError(
            f"--base-ref {ref} is not a commit in this repository; fetch it "
            "(for example fetch-depth: 0)",
            "base-ref",
        )
    code, stdout, stderr = run_git(root, ["merge-base", "--all", "HEAD", stdout.strip()])
    bases = stdout.split()
    if code != 0 or not bases:
        raise BaselineError(f"--base-ref {ref} has no merge base with HEAD {stderr}".rstrip(), "base-ref")
    if len(bases) > 1:
        raise BaselineError(
            f"--base-ref {ref} has {len(bases)} merge bases with HEAD "
            f"({', '.join(base[:12] for base in bases)}); pass one of them",
            "base-ref",
        )
    return {"source": "base-ref", "commit": bases[0], "tree": bases[0], "ref": ref}


def changed_files(root: Path) -> List[str]:
    return sorted({record["path"] for record in changed_file_records(root)})


def changed_file_records(root: Path) -> List[Dict[str, str]]:
    base = comparison_base(root)
    if base["source"] == "head":
        return _status_records(root)
    return _records_since(root, base["tree"])


def _records_since(root: Path, tree: str) -> List[Dict[str, str]]:
    """Everything that differs from ``tree``, committed or not, plus untracked files.

    No rename detection: a move is a deletion and an addition, so it classifies
    the same whether or not it is committed, and both paths face scope checks.
    Options that configuration could use to hide a change are pinned.
    """
    code, stdout, stderr = run_git(
        root,
        [
            "diff", "--no-ext-diff", "--no-relative", "--no-renames",
            "--ignore-submodules=none", "--name-status", "-z", tree, "--",
        ],
    )
    if code != 0:
        raise RuntimeError(stderr or "git diff failed")
    records: Dict[str, Dict[str, str]] = {}
    fields = stdout.split("\0")
    for status, path in zip(fields[0::2], fields[1::2]):
        if not status:
            continue
        kind = {"A": "added", "D": "deleted"}.get(status[0], "modified")
        records[path] = {"path": path, "previous_path": "", "status": status, "change_kind": kind}
    code, stdout, stderr = run_git(
        root, ["ls-files", "--others", "--exclude-standard", "--full-name", "-z", "--", ":/"]
    )
    if code != 0:
        raise RuntimeError(stderr or "git ls-files failed")
    for path in stdout.split("\0"):
        if not path:
            continue
        # Removed from the index but still on disk: it differs from the base as content.
        kind = "modified" if path in records else "added"
        records[path] = {"path": path, "previous_path": "", "status": "??", "change_kind": kind}
    return [records[path] for path in sorted(records)]


def _status_records(root: Path) -> List[Dict[str, str]]:
    code, stdout, stderr = run_git(root, ["status", "--porcelain=v1", "-z", "-uall"])
    if code != 0:
        raise RuntimeError(stderr or "git status failed")

    records: List[Dict[str, str]] = []
    entries = iter(stdout.split("\0"))
    for entry in entries:
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        previous_path = ""
        change_kind = "modified"
        if "R" in status or "C" in status:
            # With -z, renames and copies emit the destination before the source.
            previous_path = next(entries, "")
            if not previous_path:
                raise RuntimeError("git status omitted rename/copy source path")
            change_kind = "renamed"
        elif status == "??" or "A" in status:
            change_kind = "added"
        elif "D" in status:
            change_kind = "deleted"
        records.append(
            {
                "path": path,
                "previous_path": previous_path,
                "status": status,
                "change_kind": change_kind,
            }
        )
    return sorted(records, key=lambda item: item["path"])


def git_blob_for_head(root: Path, path: str) -> Optional[str]:
    code, stdout, _ = run_git(root, ["rev-parse", f"HEAD:{path}"])
    if code != 0:
        return None
    return stdout.strip()


def current_branch(root: Path) -> Optional[str]:
    """Return the current branch name, or None outside a repo / on detached HEAD."""
    code, stdout, _ = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0:
        return None
    branch = stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch
