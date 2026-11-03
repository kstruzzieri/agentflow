"""Git helpers used by drift auditing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def changed_files(root: Path) -> List[str]:
    return sorted({record["path"] for record in changed_file_records(root)})


def changed_file_records(root: Path) -> List[Dict[str, str]]:
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
