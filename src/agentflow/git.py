"""Git helpers used by drift auditing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def run_git(root: Path, args: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout.rstrip(), proc.stderr.rstrip()


def is_git_repo(root: Path) -> bool:
    code, _, _ = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return code == 0


def changed_files(root: Path) -> List[str]:
    code, stdout, stderr = run_git(root, ["status", "--porcelain=v1", "-uall"])
    if code != 0:
        raise RuntimeError(stderr or "git status failed")

    files = []
    for line in stdout.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return sorted(set(files))


def changed_file_records(root: Path) -> List[Dict[str, str]]:
    code, stdout, stderr = run_git(root, ["status", "--porcelain=v1", "-uall"])
    if code != 0:
        raise RuntimeError(stderr or "git status failed")

    records: List[Dict[str, str]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        raw_path = line[3:].strip()
        previous_path = ""
        path = raw_path
        change_kind = "modified"
        if " -> " in raw_path:
            previous_path, path = raw_path.split(" -> ", 1)
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
