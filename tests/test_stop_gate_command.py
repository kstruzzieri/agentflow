"""Tests for ``agentflow stop-gate``: the Stop hook answer for an installed package.

``tests/test_stop_gate.py`` covers the POSIX shell hook. This file covers the
Python command that replaces it for installed packages: which root it governs,
the gate chain it runs, and the one JSON object it prints.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentflow import porcelain, stop_gate

from tests.test_porcelain import _completed_step, _ready_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _govern(path: Path) -> Path:
    (path / ".agent").mkdir(parents=True, exist_ok=True)
    (path / ".agent/plan.lock.json").write_text("{}\n", encoding="utf-8")
    return path


class FindRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_explicit_root_wins_over_environment(self):
        a = _govern(self.tmp / "a")
        b = _govern(self.tmp / "b")
        found = stop_gate.find_root(str(a), {"AGENTFLOW_ROOT": str(b)}, self.tmp)
        self.assertEqual(found, a)

    def test_agentflow_root_beats_claude_project_dir(self):
        a = _govern(self.tmp / "a")
        b = _govern(self.tmp / "b")
        env = {"AGENTFLOW_ROOT": str(a), "CLAUDE_PROJECT_DIR": str(b)}
        self.assertEqual(stop_gate.find_root(None, env, self.tmp), a)
        self.assertEqual(stop_gate.find_root(None, {"CLAUDE_PROJECT_DIR": str(b)}, self.tmp), b)

    def test_cwd_walks_up_to_the_nearest_locked_plan(self):
        root = _govern(self.tmp / "repo")
        deep = root / "src" / "pkg"
        deep.mkdir(parents=True)
        self.assertEqual(stop_gate.find_root(None, {}, deep), root)

    def test_environment_root_is_not_walked(self):
        root = _govern(self.tmp / "repo")
        (root / "src").mkdir()
        self.assertIsNone(stop_gate.find_root(None, {"AGENTFLOW_ROOT": str(root / "src")}, self.tmp))

    def test_no_locked_plan_is_ungoverned(self):
        self.assertIsNone(stop_gate.find_root(None, {}, self.tmp))


class DecideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _fake(self, fail=None, output=None, raise_on=None):
        calls = []

        def fake(argv):
            calls.append(list(argv))
            if raise_on is not None and argv[0] == raise_on[0]:
                raise raise_on[1]
            if argv[0] == fail:
                return 1, list(output if output is not None else ["gate failed"])
            return 0, ["next-action from fake"] if argv[0] == "next-action" else []

        return calls, fake

    def test_finished_run_allows_without_recording(self):
        _completed_step(self.root)
        ledger = self.root / ".agent/verification-runs.jsonl"
        before = ledger.read_bytes() if ledger.exists() else b""
        self.assertEqual(stop_gate.decide(self.root), {})
        after = ledger.read_bytes() if ledger.exists() else b""
        self.assertEqual(after, before)

    def test_unfinished_run_blocks_with_gate_next_action_and_operator_line(self):
        _ready_repo(self.root)
        answer = stop_gate.decide(self.root)
        self.assertEqual(answer["decision"], "block")
        self.assertEqual(set(answer), {"decision", "reason"})
        lines = answer["reason"].splitlines()
        self.assertEqual(
            lines[0],
            "agentflow stop-gate: 'verify-run' failed; "
            "blocking completion until the Agentflow proof chain passes.",
        )
        self.assertIn("error: P1 is not completed", lines)
        self.assertTrue(any(line.startswith("next-action step_unclaimed:") for line in lines))
        self.assertEqual(lines[-1], stop_gate.OPERATOR_LINE)

    def test_gates_run_in_order_with_absolute_paths_and_no_record(self):
        _govern(self.root)
        calls, fake = self._fake()
        with mock.patch.object(porcelain, "run_cli_captured", fake):
            self.assertEqual(stop_gate.decide(self.root), {})
        plan = str(self.root / ".agent/plan.lock.json")
        self.assertEqual(calls, [
            ["verify-run", "--no-record", "--root", str(self.root), "--plan", plan],
            ["build-proof", "--root", str(self.root), "--plan", plan],
            ["verify-proof", "--root", str(self.root)],
        ])

    def test_stops_at_first_failing_gate(self):
        _govern(self.root)
        calls, fake = self._fake(fail="build-proof")
        with mock.patch.object(porcelain, "run_cli_captured", fake):
            answer = stop_gate.decide(self.root)
        self.assertEqual([argv[0] for argv in calls], ["verify-run", "build-proof", "next-action"])
        self.assertEqual(answer["decision"], "block")
        self.assertIn("'build-proof' failed", answer["reason"])
        self.assertIn("next-action from fake", answer["reason"])

    def test_gate_diagnostics_are_capped(self):
        _govern(self.root)
        _, fake = self._fake(fail="verify-run", output=[f"line {i}" for i in range(50)])
        with mock.patch.object(porcelain, "run_cli_captured", fake):
            reason = stop_gate.decide(self.root)["reason"]
        lines = reason.splitlines()
        self.assertIn("line 19", lines)
        self.assertNotIn("line 20", lines)
        self.assertIn("30 more lines", reason)

    def test_internal_error_blocks(self):
        _govern(self.root)
        _, fake = self._fake(raise_on=("verify-run", RuntimeError("boom")))
        with mock.patch.object(porcelain, "run_cli_captured", fake):
            answer = stop_gate.decide(self.root)
        self.assertEqual(answer["decision"], "block")
        self.assertIn("internal error", answer["reason"])
        self.assertIn("boom", answer["reason"])
        self.assertEqual(answer["reason"].splitlines()[-1], stop_gate.OPERATOR_LINE)

    def test_gate_exit_blocks(self):
        _govern(self.root)
        _, fake = self._fake(raise_on=("build-proof", SystemExit(2)))
        with mock.patch.object(porcelain, "run_cli_captured", fake):
            self.assertEqual(stop_gate.decide(self.root)["decision"], "block")

    def test_failing_next_action_still_blocks(self):
        _govern(self.root)
        _, fake = self._fake(fail="verify-run", raise_on=("next-action", RuntimeError("na")))
        with mock.patch.object(porcelain, "run_cli_captured", fake):
            answer = stop_gate.decide(self.root)
        self.assertEqual(answer["decision"], "block")
        self.assertEqual(answer["reason"].splitlines()[-1], stop_gate.OPERATOR_LINE)


STRAY_FD1 = """
import os, sys
from agentflow import cli, porcelain

def fake(argv):
    os.write(1, b"stray\\n")
    return 1, ["x"]

porcelain.run_cli_captured = fake
raise SystemExit(cli.main(["stop-gate", "--root", sys.argv[1]]))
"""


class StopGateCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv, cwd=None, input=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENTFLOW_ROOT", "CLAUDE_PROJECT_DIR")}
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        return subprocess.run(
            argv, cwd=cwd or self.root, env=env, capture_output=True, text=True,
            timeout=120, input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
        )

    def _stop_gate(self, *args, cwd=None, input=None):
        return self._run([sys.executable, "-m", "agentflow", "stop-gate", *args],
                         cwd=cwd, input=input)

    def _assert_block(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("\n"), 1, result.stdout)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_ungoverned_prints_empty_object(self):
        result = self._stop_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "{}\n")
        self.assertIn("no locked plan", result.stderr)

    def test_unfinished_run_prints_one_block_object(self):
        _ready_repo(self.root)
        self._assert_block(self._stop_gate("--root", str(self.root), cwd=REPO_ROOT))

    def test_finished_run_prints_empty_object(self):
        _completed_step(self.root)
        result = self._stop_gate("--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "{}\n")

    def test_subdirectory_cwd_finds_the_plan(self):
        _ready_repo(self.root)
        deep = self.root / "src" / "deep"
        deep.mkdir(parents=True)
        self._assert_block(self._stop_gate(cwd=deep))

    def test_hook_payload_on_stdin_does_not_change_the_answer(self):
        _ready_repo(self.root)
        payload = '{"hook_event_name": "Stop", "stop_hook_active": true}'
        self._assert_block(self._stop_gate(input=payload))

    def test_stray_fd1_output_goes_to_stderr(self):
        _govern(self.root)
        result = self._run([sys.executable, "-c", STRAY_FD1, str(self.root)])
        self._assert_block(result)
        self.assertIn("stray", result.stderr)


if __name__ == "__main__":
    unittest.main()
