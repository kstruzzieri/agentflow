#!/usr/bin/env python3
"""Check every runtime config under examples/integrations/ from an installed Agentflow.

Run with the Python of a virtual environment that has the built wheel
installed, with that environment's bin directory first on PATH:

    python scripts/check_integrations.py examples/integrations

For each runtime folder it starts the MCP server exactly as the config names
it (``initialize`` then ``tools/list``), and runs the Stop hook line as written
in scratch Git repositories: no locked plan (allow), an unfinished run (block),
a finished run (allow), and an unfinished run with Agentflow missing from PATH
(block). Prints one ``ok``/``FAIL`` line per check; exits 1 on any failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Dict, List, Optional

import agentflow
from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.mcp_server import TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = 'python3 -c "print(1)"'
TIMEOUT = 120


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _toml(path: Path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _mcp_entry(server: Dict) -> List[str]:
    return [server["command"], *server.get("args", [])]


MCP: Dict[str, Callable[[Path], List[str]]] = {
    "claude-code": lambda d: _mcp_entry(_json(d / ".mcp.json")["mcpServers"]["agentflow"]),
    "codex": lambda d: _mcp_entry(_toml(d / ".codex/config.toml")["mcp_servers"]["agentflow"]),
    "copilot": lambda d: _mcp_entry(_json(d / ".mcp.json")["mcpServers"]["agentflow"]),
}
HOOK: Dict[str, Callable[[Path], str]] = {
    "claude-code": lambda d: _json(d / ".claude/settings.json")["hooks"]["Stop"][0]["hooks"][0]["command"],
    "codex": lambda d: _toml(d / ".codex/config.toml")["hooks"]["Stop"][0]["hooks"][0]["command"],
    "copilot": lambda d: _json(d / ".github/hooks/agentflow.json")["hooks"]["agentStop"][0]["bash"],
}


def _env(path: Optional[str] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTFLOW_ROOT", "CLAUDE_PROJECT_DIR")}
    if path is not None:
        env["PATH"] = path
    return env


def _run(argv: List[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, **kwargs)


def _af(repo: Path, *args: str) -> None:
    result = _run(["agentflow", *args, *(() if args[0] == "lock-plan" else ("--root", str(repo)))],
                  repo, env=_env())
    if result.returncode != 0:
        raise RuntimeError(f"agentflow {' '.join(args)} failed: {result.stdout}{result.stderr}")


def _plan() -> Dict:
    """The one-step plan from tests/test_porcelain.py (_plan, _step)."""
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "integration check fixture",
        "scope": ["exercise the Stop hook"],
        "non_goals": ["none"],
        "invariants": ["tests pass"],
        "allowed_files": [".agent/", "src/feature.py"],
        "blocked_files": [],
        "validation_gates": [GATE],
        "rollback_plan": "git reset",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0, "new_dependencies": 0, "formatting_drift": "minimal",
            "architecture_drift": "requires_approval", "test_weakening": 0,
        },
        "steps": [{
            "id": "P1",
            "action": "Exercise the fixture.",
            "files": ["src/feature.py"],
            "preconditions": ["Repository initialized."],
            "expected_diff": ["src/feature.py changes are attributed to P1."],
            "validation": [GATE],
            "gates": [{"kind": "command", "run": ["python3", "-c", "print(1)"]}],
            "evidence_ids": [],
        }],
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def make_repo(path: Path, runtime_dir: Path, state: str) -> Path:
    """A Git repo holding the runtime's files: state is none, unfinished or finished."""
    shutil.copytree(runtime_dir, path)
    for args in (["init", "-q"], ["config", "user.email", "ci@example.invalid"],
                 ["config", "user.name", "ci"], ["add", "-A"], ["commit", "-qm", "configs"]):
        _run(["git", *args], path).check_returncode()
    if state == "none":
        return path
    _af(path, "init")
    plan_path = path / ".agent/plan.lock.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    _af(path, "lock-plan", str(plan_path))
    _af(path, "init-execution")
    if state == "finished":
        _af(path, "claim-step", "P1", "--agent", "ci")
        (path / "src").mkdir()
        (path / "src/feature.py").write_text("x = 1\n", encoding="utf-8")
        _af(path, "record-file-change", "--step", "P1", "--path", "src/feature.py")
        result = _run(["agentflow", "run", "--step", "P1", "--gate", GATE, "--root", str(path),
                       "--", "python3", "-c", "print(1)"], path, env=_env())
        result.check_returncode()
        _af(path, "verify-step", "P1")
        _af(path, "complete-step", "P1")
    return path


def check_mcp(argv: List[str], cwd: Path) -> Optional[str]:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    try:
        result = _run(argv, cwd, env=_env(), stdin=None,
                      input="".join(json.dumps(m) + "\n" for m in messages))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not start {argv}: {exc}"
    replies = {}
    for line in result.stdout.splitlines():
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            return f"non-JSON line on stdout: {line[:200]}"
        replies[reply.get("id")] = reply
    if "serverInfo" not in replies.get(1, {}).get("result", {}):
        return f"no initialize result (exit {result.returncode}): {result.stderr[-500:]}"
    names = {tool["name"] for tool in replies.get(2, {}).get("result", {}).get("tools", [])}
    expected = {tool.name for tool in TOOLS}
    if names != expected:
        return f"tools/list mismatch: missing {sorted(expected - names)}, extra {sorted(names - expected)}"
    return None


def check_hook(line: str, cwd: Path, expect_block: bool, path: Optional[str] = None) -> Optional[str]:
    result = _run(["sh", "-c", line], cwd, env=_env(path))
    if result.returncode != 0:
        return f"hook exited {result.returncode}: {result.stderr[-500:]}"
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"stdout is not one JSON object (exit {result.returncode}): {result.stdout[:200]!r}"
    if result.stdout.count("\n") != 1:
        return f"stdout holds more than one line: {result.stdout[:200]!r}"
    if expect_block:
        if not isinstance(answer, dict) or answer.get("decision") != "block":
            return f"expected a block, got {answer}"
        reason = answer.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return f"expected a block reason, got {answer}"
    elif answer != {}:
        return f"expected an empty allow response, got {answer}"
    return None


def main(argv: List[str]) -> int:
    if len(argv) != 1:
        print("usage: check_integrations.py <examples/integrations>", file=sys.stderr)
        return 2
    installed = Path(agentflow.__file__).resolve()
    if REPO_ROOT / "src" in installed.parents:
        print(f"FAIL setup: agentflow imported from the source tree ({installed}), "
              "not an installed wheel", file=sys.stderr)
        return 1
    examples = Path(argv[0])
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for runtime in sorted(MCP):
            folder = examples / runtime
            base = Path(tmp) / runtime
            repos = {state: make_repo(base / state, folder, state)
                     for state in ("none", "unfinished", "finished")}
            line = HOOK[runtime](folder)
            checks = [
                ("mcp", lambda: check_mcp(MCP[runtime](folder), repos["none"])),
                ("hook-no-plan", lambda: check_hook(line, repos["none"], False)),
                ("hook-unfinished", lambda: check_hook(line, repos["unfinished"], True)),
                ("hook-finished", lambda: check_hook(line, repos["finished"], False)),
                ("hook-missing-cli", lambda: check_hook(line, repos["unfinished"], True,
                                                        path="/usr/bin:/bin")),
            ]
            for name, check in checks:
                error = check()
                if error is None:
                    print(f"ok {runtime} {name}")
                else:
                    failures += 1
                    print(f"FAIL {runtime} {name}: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
