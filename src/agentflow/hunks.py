"""Hunk-level attribution: shared diff parsing, identity, and coverage.

This module is the only place that shells ``git diff`` and computes hunk
identity hashes. It imports only stdlib plus ``artifacts`` and ``contracts`` to
stay free of import cycles (``receipts``/``validation``/``execution_coverage``
all import this module). It deliberately does not use ``git.run_git`` because
that helper strips stdout, and hunk patches / HEAD blobs need exact output.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import read_jsonl, try_read_json
from .contracts import DIFF_COMMAND_VERSION, EXECUTION_ARTIFACT_PATHS, HUNK_ATTRIBUTION_POLICIES

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Number of lines before each hunk to include in the context anchor window.
_CONTEXT_ANCHOR_LINES = 3


def _git_text(root: Path, args: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git_bytes(root: Path, args: List[str]) -> Tuple[int, bytes, bytes]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _base_flags(no_index: bool) -> List[str]:
    flags = [
        "-c",
        "core.autocrlf=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        "--diff-algorithm=myers",
    ]
    flags.append("--no-index" if no_index else "--no-renames")
    return flags


def hunk_identity(posix_path: str, change_kind: str, changed_lines: List[str]) -> str:
    payload = "\n".join([DIFF_COMMAND_VERSION, posix_path, change_kind, *changed_lines])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _head_lines(root: Path, path: str) -> Optional[List[str]]:
    code, stdout, _ = _git_text(root, ["show", f"HEAD:{path}"])
    if code != 0:
        return None
    # CRLF→LF normalization is deliberate: ensures cross-platform identity stability.
    return stdout.replace("\r\n", "\n").split("\n")


def _context_sha256(head_lines: Optional[List[str]], old_start: int) -> Tuple[Optional[str], str]:
    if not head_lines or old_start <= 0:
        return None, "none"
    start = max(0, old_start - 1 - _CONTEXT_ANCHOR_LINES)
    anchors = head_lines[start : old_start - 1]
    if not anchors:
        return None, "none"
    digest = hashlib.sha256("\n".join(anchors).encode("utf-8")).hexdigest()
    return digest, "head-baseline"


def _parse_patch(text: str, posix_path: str, change_kind: str, head_lines: Optional[List[str]]) -> List[Dict[str, Any]]:
    hunks: List[Dict[str, Any]] = []
    # CRLF→LF normalization is deliberate: ensures cross-platform identity stability.
    lines = text.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        index += 1
        body: List[str] = []
        # Safe under --unified=0: no context lines are emitted, so every
        # added/removed content line carries a '+'/'-' prefix; a literal "@@ "
        # sequence in file content cannot appear without a prefix and cannot be
        # misread as a hunk header.
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith(("+", "-")):
                body.append(line)
            index += 1
        context_hash, context_source = _context_sha256(head_lines, old_start)
        hunks.append(
            {
                "hash": hunk_identity(posix_path, change_kind, body),
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "context_sha256": context_hash,
                "context_source": context_source,
            }
        )
    return hunks


def _is_binary(text: str) -> bool:
    return "Binary files " in text or "GIT binary patch" in text


def compute_hunks(root: Path, record: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """Return ``(attribution, hunks)`` for one changed-file record.

    ``attribution`` is ``"hunked"`` for a parsed text diff or
    ``"whole_file_fallback"`` for binary / unparseable / unreconstructable
    inputs. The ``"disabled"`` state is set by the caller, never here.
    """
    path = record["path"]
    posix_path = _normalize_path(path)
    change_kind = record.get("change_kind", "modified")
    head_lines = _head_lines(root, record.get("previous_path") or path)

    if change_kind == "added":
        code, stdout, _ = _git_text(root, [*_base_flags(no_index=True), "--", "/dev/null", path])
        if code > 1:
            return "whole_file_fallback", []
        if _is_binary(stdout):
            return "whole_file_fallback", []
        return "hunked", _parse_patch(stdout, posix_path, change_kind, None)

    if change_kind == "renamed":
        previous_path = record.get("previous_path") or ""
        if not previous_path:
            return "whole_file_fallback", []
        code, blob, _ = _git_bytes(root, ["show", f"HEAD:{previous_path}"])
        if code != 0:
            return "whole_file_fallback", []
        with NamedTemporaryFile("wb", suffix=".afhunk", delete=True) as handle:
            handle.write(blob)
            handle.flush()
            code, stdout, _ = _git_text(
                root, [*_base_flags(no_index=True), "--", handle.name, path]
            )
        if code > 1 or _is_binary(stdout):
            return "whole_file_fallback", []
        return "hunked", _parse_patch(stdout, posix_path, change_kind, head_lines)

    # modified / deleted: working tree vs HEAD
    code, stdout, _ = _git_text(root, [*_base_flags(no_index=False), "HEAD", "--", path])
    if code != 0:
        return "whole_file_fallback", []
    if _is_binary(stdout):
        return "whole_file_fallback", []
    return "hunked", _parse_patch(stdout, posix_path, change_kind, head_lines)


def effective_hunk_policy(root: Path) -> str:
    contract_path = root / EXECUTION_ARTIFACT_PATHS["execution-contract"]
    if not contract_path.exists():
        return "off"
    # A malformed contract must not crash the proof/CI gate; degrade to the
    # contract-present default ("enforce") rather than raising.
    contract, _ = try_read_json(contract_path)
    if not isinstance(contract, dict):
        return "enforce"
    policy = contract.get("proof_policy", {})
    value = policy.get("hunk_attribution", "enforce") if isinstance(policy, dict) else "enforce"
    return value if value in HUNK_ATTRIBUTION_POLICIES else "enforce"


def _receipts_by_path(root: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    latest: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for receipt in read_jsonl(root / EXECUTION_ARTIFACT_PATHS["file-receipts"]):
        path = receipt.get("path")
        if not isinstance(path, str):
            continue
        latest[path] = receipt  # append-only ledger: last write wins
        grouped.setdefault(path, []).append(receipt)
    return latest, grouped


def unmapped_hunks(root: Path, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest, grouped = _receipts_by_path(root)
    out: List[Dict[str, Any]] = []
    for record in records:
        path = record.get("path")
        latest_receipt = latest.get(path)
        if latest_receipt is None:
            continue  # missing-receipt policy handles this upstream
        path_receipts = grouped.get(path, [])
        if "hunks" not in latest_receipt:
            continue  # latest legacy 0.3.0 receipt -> whole-file coverage
        if latest_receipt.get("hunk_attribution") != "hunked":
            continue  # whole_file_fallback / disabled
        recorded = {
            hunk["hash"]
            for receipt in path_receipts
            if receipt.get("hunk_attribution") == "hunked"
            for hunk in receipt.get("hunks", [])
            if isinstance(hunk.get("hash"), str)
        }
        attribution, current = compute_hunks(root, record)
        if attribution != "hunked":
            continue  # became binary/unparseable -> after_sha256 governs
        for hunk in current:
            if hunk["hash"] not in recorded:
                out.append(
                    {
                        "path": path,
                        "hash": hunk["hash"],
                        "old_start": hunk["old_start"],
                        "old_count": hunk["old_count"],
                        "new_start": hunk["new_start"],
                        "new_count": hunk["new_count"],
                        "reason": "no_matching_hunk",
                    }
                )
    return out
