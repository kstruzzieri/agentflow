"""The Stop hook answer for an installed Agentflow: ``agentflow stop-gate``.

Claude Code, Codex CLI and GitHub Copilot all read the same answer from a Stop
hook that exits 0: ``{}`` lets the agent stop, ``{"decision": "block",
"reason": ...}`` sends ``reason`` back to the agent as its next prompt. This
module decides that answer by running the proof chain that
``hooks/stop-gate.sh`` runs (``verify-run --no-record`` -> ``build-proof`` ->
``verify-proof``), in process. It never raises: a non-zero exit or a crash
fails open in most runtimes, so an internal error while governed is a block.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from . import porcelain

OPERATOR_LINE = (
    "agentflow stop-gate: a recorded blocker does not satisfy this gate. "
    "If the work is genuinely blocked, an operator must interrupt the session "
    "(see docs/stop-hook.md)."
)
MAX_GATE_LINES = 20
PLAN = Path(".agent/plan.lock.json")


def find_root(
    explicit: Optional[str], environ: Mapping[str, str], cwd: Optional[Path]
) -> Optional[Path]:
    """The governed root, or None when no locked plan is found.

    The first source wins: ``--root``, ``$AGENTFLOW_ROOT``,
    ``$CLAUDE_PROJECT_DIR``, then the current directory (None when it cannot
    be read, for example after it was deleted). Only the current directory is
    walked upward, because Codex and Copilot run hooks in the session's working
    directory, which may be a subdirectory. The walk stops at the repository
    boundary (the first directory holding ``.git``, a directory or a worktree
    file), so a repository nested inside a governed one is never blocked by
    the outer plan.
    """
    for given in (explicit, environ.get("AGENTFLOW_ROOT"), environ.get("CLAUDE_PROJECT_DIR")):
        if given:
            directory = Path(given).resolve()
            return directory if _has_plan(directory) else None
    if cwd is None:
        return None
    for directory in [cwd.resolve(), *cwd.resolve().parents]:
        if _has_plan(directory):
            return directory
        if os.path.lexists(directory / ".git"):
            return None
    return None


def _has_plan(directory: Path) -> bool:
    try:
        return (directory / PLAN).is_file()
    except OSError:  # unreadable: not evidence of governance
        return False


def _block(lines: List[str]) -> Dict[str, str]:
    return {"decision": "block", "reason": "\n".join([*lines, OPERATOR_LINE])}


def internal_error(exc: BaseException) -> Dict[str, str]:
    """The block answer for an error while a governed root is known."""
    return _block([f"agentflow stop-gate: internal error: {type(exc).__name__}: {exc}"])


def decide(root: Path) -> Dict[str, str]:
    """``{}`` to allow, or a block answer. Never raises."""
    plan = str(root / PLAN)
    gates = [
        ("verify-run", ["verify-run", "--no-record", "--root", str(root), "--plan", plan]),
        ("build-proof", ["build-proof", "--root", str(root), "--plan", plan]),
        ("verify-proof", ["verify-proof", "--root", str(root)]),
    ]
    try:
        result = porcelain.run_gate_chain(gates)
    except (Exception, SystemExit) as exc:  # fail closed: a crash would let the agent stop
        return internal_error(exc)
    if result["ok"]:
        return {}
    gate = result["stopped_at"]
    diagnostics = result["diagnostics"]
    lines = [
        f"agentflow stop-gate: '{gate}' failed; "
        "blocking completion until the Agentflow proof chain passes.",
        *diagnostics[:MAX_GATE_LINES],
    ]
    if len(diagnostics) > MAX_GATE_LINES:
        lines.append(
            f"... ({len(diagnostics) - MAX_GATE_LINES} more lines; "
            "run agentflow finish-run to see all)"
        )
    try:
        _, next_lines = porcelain.run_cli_captured(["next-action", "--root", str(root)])
        lines.extend(next_lines[:MAX_GATE_LINES])
    except (Exception, SystemExit):
        pass  # the refusal stands without the suggestion, as in the shell hook
    return _block(lines)
