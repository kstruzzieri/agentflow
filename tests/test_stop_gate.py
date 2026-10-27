"""Tests for the runtime-agnostic Agentflow stop-gate hook.

The hook (``hooks/stop-gate.sh``) is a thin orchestrator: it runs the Agentflow
proof chain (``verify-run`` -> ``build-proof`` -> ``verify-proof``) and maps the
outcome onto a universal exit-code contract (0 = proof satisfied / allow stop,
non-zero = blocked). The Agentflow CLI itself is exercised by the rest of the
suite, so here we inject a fake ``agentflow`` via ``$AGENTFLOW_CMD`` and assert
only the hook's own behaviour: pass-through when ungoverned, fail-fast ordering,
a clean exit when every gate passes, and the refusal guidance (the next-action
output, then the fixed operator line, printed only when a gate fails).
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
# like `--version` (which carry no gate, so they are neither logged nor failed,
# and always succeed). `next-action` is also a non-gate probe: it is never
# logged, prints a "next-action from fake" marker line, and fails only when
# $FAKE_FAIL names it.
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
if [ "${1:-}" = "next-action" ]; then
  printf 'next-action from fake: %s\\n' "$*"
  case " ${FAKE_FAIL:-} " in
    *" next-action "*) exit 1 ;;
  esac
fi
exit 0
"""

# The fixed line the hook prints last on every refusal (kept in sync with
# hooks/stop-gate.sh and docs/stop-hook.md).
OPERATOR_LINE = (
    "agentflow stop-gate: a recorded blocker does not satisfy this gate. "
    "If the work is genuinely blocked, an operator must interrupt the session "
    "(see docs/stop-hook.md)."
)


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

    def test_refusal_prints_next_action_then_operator_line(self):
        for gate in ("verify-run", "build-proof", "verify-proof"):
            with self.subTest(gate=gate):
                self.log.unlink(missing_ok=True)
                self._lock_plan()
                result = self._run(fail=gate)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                failed = result.stderr.index(
                    f"'{gate}' failed; blocking completion"
                )
                next_action = result.stderr.index(
                    f"next-action from fake: next-action --root {self.root}"
                )
                operator = result.stderr.index(OPERATOR_LINE)
                self.assertLess(failed, next_action)
                self.assertLess(next_action, operator)
                self.assertEqual(self._gates_run()[-1], gate)

    def test_refusal_survives_a_failing_next_action(self):
        # next-action is guidance only: if it fails, the hook still refuses
        # (exit 1) and still prints the failed-gate line and the operator line.
        self._lock_plan()
        result = self._run(fail="verify-run next-action")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("'verify-run' failed; blocking completion", result.stderr)
        self.assertIn("next-action from fake", result.stderr)
        self.assertIn(OPERATOR_LINE, result.stderr)

    def test_passing_and_ungoverned_stderr_unchanged(self):
        # The guidance is refusal-only: ungoverned and passing runs print
        # exactly what they printed before.
        ungoverned = self._run()
        self.assertEqual(ungoverned.returncode, 0, ungoverned.stderr)
        self.assertEqual(
            ungoverned.stderr,
            f"agentflow stop-gate: no locked plan at "
            f"{self.root}/.agent/plan.lock.json; nothing to enforce.\n",
        )
        self._lock_plan()
        passing = self._run()
        self.assertEqual(passing.returncode, 0, passing.stderr)
        self.assertEqual(
            passing.stderr,
            "stdout from verify-run\nstdout from build-proof\n"
            "stdout from verify-proof\n"
            "agentflow stop-gate: proof chain verified; safe to stop.\n",
        )


if __name__ == "__main__":
    unittest.main()
