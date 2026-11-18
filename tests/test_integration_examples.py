"""Tests for the copy-ready runtime configs under examples/integrations/.

Each runtime folder is copied into a user's repository as is, so these tests
pin what the files promise: the installed MCP server, one shared Stop hook
line that never fails open when Agentflow is missing, and timeouts long enough
that no runtime skips the hook.
"""

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples/integrations"
CLAUDE = EXAMPLES / "claude-code"
CODEX = EXAMPLES / "codex"
COPILOT = EXAMPLES / "copilot"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _claude_hook():
    return _json(CLAUDE / ".claude/settings.json")["hooks"]["Stop"][0]["hooks"][0]


def _codex():
    return tomllib.loads((CODEX / ".codex/config.toml").read_text(encoding="utf-8"))


def _codex_hook():
    return _codex()["hooks"]["Stop"][0]["hooks"][0]


def _copilot_hook():
    return _json(COPILOT / ".github/hooks/agentflow.json")["hooks"]["agentStop"][0]


def hook_line() -> str:
    return _claude_hook()["command"]


class IntegrationExampleTests(unittest.TestCase):
    def test_every_runtime_starts_the_installed_mcp_server(self):
        self.assertEqual(
            _json(CLAUDE / ".mcp.json"),
            {"mcpServers": {"agentflow": {"type": "stdio", "command": "agentflow-mcp", "args": []}}},
        )
        self.assertEqual((COPILOT / ".mcp.json").read_bytes(), (CLAUDE / ".mcp.json").read_bytes())
        self.assertEqual(_codex()["mcp_servers"]["agentflow"],
                         {"command": "agentflow-mcp", "args": []})

    def test_every_runtime_runs_the_same_stop_gate_line(self):
        lines = {_claude_hook()["command"], _codex_hook()["command"], _copilot_hook()["bash"]}
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(hook_line().startswith("agentflow stop-gate || "))
        self.assertEqual(_claude_hook()["type"], "command")
        self.assertEqual(_codex_hook()["type"], "command")
        self.assertEqual(_copilot_hook()["type"], "command")
        self.assertEqual(_copilot_hook()["cwd"], ".")

    def test_windows_lines(self):
        self.assertTrue(_copilot_hook()["powershell"].startswith("agentflow stop-gate; if (-not $?)"))
        self.assertEqual(_codex_hook()["command_windows"], "agentflow stop-gate")

    def test_hook_timeouts_are_600_seconds(self):
        self.assertEqual(_claude_hook()["timeout"], 600)
        self.assertEqual(_codex_hook()["timeout"], 600)
        self.assertEqual(_copilot_hook()["timeoutSec"], 600)

    def test_copilot_setup_steps_installs_agentflow(self):
        text = (COPILOT / ".github/workflows/copilot-setup-steps.yml").read_text(encoding="utf-8")
        self.assertIn("  copilot-setup-steps:\n", text)
        self.assertIn('pip install "agentflow-proof>=1.9.0"', text)

    def test_no_example_depends_on_a_source_checkout(self):
        files = [path for path in EXAMPLES.rglob("*") if path.is_file()]
        self.assertEqual(len(files), 6)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for marker in ("PYTHONPATH", "src/agentflow", "hooks/stop-gate.sh"):
                self.assertNotIn(marker, text, f"{path.relative_to(REPO_ROOT)} names {marker}")


@unittest.skipIf(os.name == "nt", "the hook line is POSIX sh")
class HookLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.bin = tmp / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _govern(self):
        (self.repo / ".agent").mkdir()
        (self.repo / ".agent/plan.lock.json").write_text("{}\n", encoding="utf-8")

    def _fake_agentflow(self, body: str):
        fake = self.bin / "agentflow"
        fake.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        fake.chmod(0o755)

    def _run(self, cwd=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.repo)}
        return subprocess.run(["sh", "-c", hook_line()], cwd=cwd or self.repo, env=env,
                              capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def _assert_block(self, result):
        self.assertEqual(result.stdout.count("\n"), 1, result.stdout)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_hook_line_blocks_a_governed_repo_when_agentflow_is_missing(self):
        self._govern()
        self._assert_block(self._run())

    def test_hook_line_blocks_a_governed_repo_when_agentflow_is_too_old(self):
        self._govern()
        self._fake_agentflow("echo 'invalid choice' >&2\nexit 2\n")
        self._assert_block(self._run())

    def test_hook_line_allows_an_ungoverned_repo_when_agentflow_is_missing(self):
        self.assertEqual(self._run().stdout, "{}\n")

    def test_hook_line_passes_a_working_answer_through(self):
        self._govern()
        answer = '{"decision": "block", "reason": "r"}'
        self._fake_agentflow(f"printf '%s\\n' '{answer}'\n")
        self.assertEqual(self._run().stdout, answer + "\n")

    def test_hook_line_finds_the_plan_from_a_subdirectory(self):
        self._govern()
        sub = self.repo / "sub"
        sub.mkdir()
        self._assert_block(self._run(cwd=sub))


if __name__ == "__main__":
    unittest.main()
