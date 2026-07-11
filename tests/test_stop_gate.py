"""Tests for the runtime-agnostic Agentflow stop-gate hook.

The hook (``hooks/stop-gate.sh``) is a thin orchestrator: it runs the Agentflow
proof chain (``verify-run`` -> ``build-proof`` -> ``verify-proof``) and maps the
outcome onto a universal exit-code contract (0 = proof satisfied / allow stop,
non-zero = blocked). The Agentflow CLI itself is exercised by the rest of the
suite, so here we inject a fake ``agentflow`` via ``$AGENTFLOW_CMD`` and assert
only the hook's own behaviour: pass-through when ungoverned, fail-fast ordering,
and a clean exit when every gate passes.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "stop-gate.sh"

# A fake `agentflow` that records the gate it was asked to run, writes a stdout
# marker for gate subcommands, and fails for any subcommand named in $FAKE_FAIL
# (space-delimited). It tolerates the global `--root` option and non-gate probes
# like `--version` (which carry no gate, so they are neither logged nor failed —
# they always succeed).
FAKE_AGENTFLOW = """#!/bin/sh
sub=""
for arg in "$@"; do
  case "$arg" in
    verify-run|build-proof|verify-proof) sub="$arg" ;;
  esac
done
if [ -n "$sub" ]; then
  printf 'stdout from %s\\n' "$sub"
  printf '%s\\n' "$sub" >> "$FAKE_LOG"
  case " ${FAKE_FAIL:-} " in
    *" $sub "*) exit 1 ;;
  esac
fi
exit 0
"""


class StopGateHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.log = self.tmp / "gate.log"
        self.fake = self.tmp / "fake-agentflow"
        self.fake.write_text(FAKE_AGENTFLOW)
        self.fake.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def _lock_plan(self):
        agent_dir = self.root / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "plan.lock.json").write_text("{}\n")

    def _run(self, *, fail="", root_env="AGENTFLOW_ROOT"):
        env = dict(os.environ)
        # Exercise exactly one root-resolution source per run.
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.pop("AGENTFLOW_ROOT", None)
        env[root_env] = str(self.root)
        env["AGENTFLOW_CMD"] = str(self.fake)
        env["FAKE_LOG"] = str(self.log)
        env["FAKE_FAIL"] = fail
        return subprocess.run(
            ["sh", str(HOOK)],
            env=env,
            capture_output=True,
            text=True,
        )

    def _gates_run(self):
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line]

    def test_passthrough_when_no_locked_plan(self):
        # Ungoverned repo: no plan.lock.json -> allow stop, never touch agentflow.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._gates_run(), [])
        # Notes go to stderr; stdout must stay clean for stdout-parsing runtimes.
        self.assertEqual(result.stdout, "")

    def test_runs_full_chain_in_order_when_all_pass(self):
        self._lock_plan()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._gates_run(), ["verify-run", "build-proof", "verify-proof"]
        )
        # Critical: Claude Code parses stdout JSON on exit 0. A leak here would
        # corrupt the allow decision, so the hook must emit nothing on stdout.
        self.assertEqual(result.stdout, "")
        self.assertIn("stdout from verify-run", result.stderr)

    def test_resolves_root_from_claude_project_dir(self):
        # Claude Code sets $CLAUDE_PROJECT_DIR rather than $AGENTFLOW_ROOT.
        self._lock_plan()
        result = self._run(root_env="CLAUDE_PROJECT_DIR")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._gates_run(), ["verify-run", "build-proof", "verify-proof"]
        )

    def test_resolves_root_from_current_directory_without_pwd_env(self):
        # Some runtimes do not export $PWD; the hook should ask the shell for cwd.
        self._lock_plan()
        env = dict(os.environ)
        env.pop("AGENTFLOW_ROOT", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.pop("PWD", None)
        env["AGENTFLOW_CMD"] = str(self.fake)
        env["FAKE_LOG"] = str(self.log)
        env["FAKE_FAIL"] = ""
        result = subprocess.run(
            ["sh", str(HOOK)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._gates_run(), ["verify-run", "build-proof", "verify-proof"]
        )
        self.assertEqual(result.stdout, "")

    def test_blocks_and_stops_at_first_failing_gate(self):
        self._lock_plan()
        result = self._run(fail="verify-run")
        self.assertNotEqual(result.returncode, 0)
        # Fail-fast: build-proof and verify-proof must not run after verify-run.
        self.assertEqual(self._gates_run(), ["verify-run"])

    def test_blocks_when_final_gate_fails(self):
        self._lock_plan()
        result = self._run(fail="verify-proof")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self._gates_run(), ["verify-run", "build-proof", "verify-proof"]
        )

    def test_blocks_with_clear_message_when_agentflow_not_runnable(self):
        # A missing/broken CLI must fail closed with a distinct message, not be
        # misreported as a proof failure or silently allowed.
        self._lock_plan()
        env = dict(os.environ)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["AGENTFLOW_ROOT"] = str(self.root)
        env["AGENTFLOW_CMD"] = str(self.tmp / "does-not-exist-agentflow")
        env["FAKE_LOG"] = str(self.log)
        env["FAKE_FAIL"] = ""
        result = subprocess.run(
            ["sh", str(HOOK)], env=env, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not runnable", result.stderr)
        self.assertEqual(self._gates_run(), [])

    def test_idempotent_when_chain_passes(self):
        # Safe to run repeatedly: re-running with a passing chain stays green.
        self._lock_plan()
        first = self._run()
        second = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)


if __name__ == "__main__":
    unittest.main()
