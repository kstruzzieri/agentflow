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


def find_root(explicit: Optional[str], environ: Mapping[str, str], cwd: Path) -> Optional[Path]:
    """The governed root, or None when no locked plan is found.

    The first source wins: ``--root``, ``$AGENTFLOW_ROOT``,
    ``$CLAUDE_PROJECT_DIR``, then the current directory. Only the current
    directory is walked upward: Codex and Copilot run hooks in the session's
    working directory, which may be a subdirectory of the repository.
    """
    for given in (explicit, environ.get("AGENTFLOW_ROOT"), environ.get("CLAUDE_PROJECT_DIR")):
        if given:
            candidates = [Path(given).resolve()]
            break
    else:
        start = cwd.resolve()
        candidates = [start, *start.parents]
    for directory in candidates:
        if (directory / PLAN).is_file():
            return directory
    return None


def _block(lines: List[str]) -> Dict[str, str]:
    return {"decision": "block", "reason": "\n".join([*lines, OPERATOR_LINE])}


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
        return _block([f"agentflow stop-gate: internal error: {type(exc).__name__}: {exc}"])
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
