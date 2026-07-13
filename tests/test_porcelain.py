import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentflow import cli, porcelain


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")


def _step():
    return {
        "id": "P1",
        "action": "Exercise porcelain fixture.",
        "files": ["src/feature.py"],
        "preconditions": ["Repository initialized."],
        "expected_diff": ["src/feature.py changes are attributed to P1."],
        "validation": ["python3 -c \"print(1)\""],
        "gates": [
            {"kind": "command", "run": ["python3", "-c", "print(1)"]},
        ],
        "evidence_ids": [],
    }


def _plan(steps):
    return {
        "schema_version": "0.3.0",
        "objective": "porcelain fixture",
        "scope": ["exercise execution porcelain"],
        "non_goals": ["no production behavior outside porcelain"],
        "invariants": ["tests pass"],
        "allowed_files": [".agent/", "src/feature.py"],
        "blocked_files": [],
        "validation_gates": ["python3 -c \"print(1)\""],
        "rollback_plan": "git reset",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": steps,
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def _ready_repo_with_steps(root: Path, steps):
    _init_repo(root)
    cli.main(["init", "--root", str(root)])
    plan = _plan(steps)
    (root / ".agent/plan.lock.json").write_text(json.dumps(plan))
    cli.main(["lock-plan", str(root / ".agent/plan.lock.json")])
    cli.main(["init-execution", "--root", str(root)])
    return plan


def _ready_repo(root: Path):
    return _ready_repo_with_steps(root, [_step()])


def _claimed(root: Path):
    _ready_repo(root)
    cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])


def _validated(root: Path):
    _claimed(root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/feature.py").write_text("x = 1\n")
    cli.main(["record-file-change", "--step", "P1", "--path",
              "src/feature.py", "--root", str(root)])
    cli.main(["run", "--step", "P1", "--gate", "python3 -c \"print(1)\"",
              "--root", str(root), "--", "python3", "-c", "print(1)"])


def _completed_step(root: Path):
    _validated(root)
    cli.main(["verify-step", "P1", "--root", str(root)])
    cli.main(["complete-step", "P1", "--root", str(root)])


def _build_verified_proof(root: Path):
    cli.main(["audit-drift", "--root", str(root)])
    cli.main(["verify-run", "--root", str(root)])
    cli.main(["build-proof", "--root", str(root)])
    cli.main(["verify-proof", "--root", str(root)])


class TestNextActionEarlyStates(unittest.TestCase):
    def test_uninitialized_when_no_plan(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "uninitialized")
            self.assertEqual(action.args, ["init"])
            self.assertTrue(action.blocking)

    def test_uninitialized_when_plan_schema_is_newer_major(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cli.main(["init", "--root", str(root)])
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text())
            plan["schema_version"] = "1.0.0"
            plan_path.write_text(json.dumps(plan))

            action = porcelain.next_action(root)

            self.assertEqual(action.state, "uninitialized")
            self.assertEqual(action.args, ["init"])

    def test_plan_unlocked_when_lock_false(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cli.main(["init", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "plan_unlocked")
            self.assertIn("lock-plan", action.args)

    def test_execution_uninitialized_after_lock(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cli.main(["init", "--root", str(root)])
            plan_path = root / ".agent/plan.lock.json"
            plan = _plan([_step()])
            plan_path.write_text(json.dumps(plan))
            cli.main(["lock-plan", str(plan_path)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "execution_uninitialized")
            self.assertEqual(action.args, ["init-execution"])


class TestNextActionStepUnclaimed(unittest.TestCase):
    def test_step_unclaimed_offers_claim(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_unclaimed")
            self.assertEqual(action.step_id, "P1")
            self.assertEqual(action.args[:2], ["claim-step", "P1"])
            self.assertIn("--agent", action.args)


class TestNextActionInflightWork(unittest.TestCase):
    def test_command_rendering_shell_quotes_gate_but_expands_user(self):
        command = porcelain._cmd([
            "run", "--gate", 'python3 -c "print(1)"',
            "--agent", "$USER", "--", "python3", "-c", "print(1)",
        ])
        self.assertIn("'python3 -c \"print(1)\"'", command)
        self.assertIn("--agent $USER", command)
        self.assertIn("'print(1)'", command)

    def test_file_receipts_missing_when_scoped_edit_unrecorded(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")  # scoped, unrecorded
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "file_receipts_missing")
            self.assertEqual(action.args[:3], ["record-file-change", "--step", "P1"])
            self.assertIn("src/feature.py", action.args)

    def test_validation_missing_when_files_recorded_but_gate_unmet(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")
            cli.main(["record-file-change", "--step", "P1", "--path",
                      "src/feature.py", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "validation_missing")
            self.assertEqual(action.args[:2], ["run", "--step"])
            self.assertEqual(action.step_id, "P1")

    def test_validation_missing_points_to_unmet_later_gate(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            step = _step()
            step["validation"] = [
                "python3 -c \"print(1)\"",
                "python3 -c \"print(2)\"",
            ]
            step["gates"] = [
                {"kind": "command", "run": ["python3", "-c", "print(1)"]},
                {"kind": "command", "run": ["python3", "-c", "print(2)"]},
            ]
            _ready_repo_with_steps(root, [step])
            cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")
            cli.main(["record-file-change", "--step", "P1", "--path",
                      "src/feature.py", "--root", str(root)])
            cli.main(["run", "--step", "P1", "--gate", "python3 -c \"print(1)\"",
                      "--root", str(root), "--", "python3", "-c", "print(1)"])

            action = porcelain.next_action(root)

            self.assertEqual(action.state, "validation_missing")
            self.assertEqual(action.gate, "python3 -c print(2)")
            self.assertEqual(action.args[-3:], ["python3", "-c", "print(2)"])

    def test_non_strict_step_warning_continues_to_finish_step(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            warning = {"severity": "warning", "message": "non-blocking warning"}
            with mock.patch(
                "agentflow.porcelain.verify_step",
                return_value={"status": "warning", "errors": [], "warnings": [warning]},
            ):
                action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_unverified")
            self.assertEqual(action.args, ["finish-step", "P1"])

    def test_strict_step_warning_reports_validation_missing(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            warning = {"severity": "warning", "message": "strict warning"}
            with mock.patch(
                "agentflow.porcelain.verify_step",
                return_value={"status": "warning", "errors": [], "warnings": [warning]},
            ):
                action = porcelain.next_action(root, strict=True)
            self.assertEqual(action.state, "validation_missing")
            self.assertIn("strict warning", action.diagnostics)


class TestNextActionVerifyComplete(unittest.TestCase):
    def test_step_unverified_offers_finish_step(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_unverified")
            self.assertEqual(action.args, ["finish-step", "P1"])

    def test_step_uncompleted_after_verify(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            cli.main(["verify-step", "P1", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_uncompleted")
            self.assertEqual(action.args, ["finish-step", "P1"])


class TestNextActionRunStates(unittest.TestCase):
    def test_run_unverified_after_all_steps_complete(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "run_unverified")
            self.assertEqual(action.args, ["finish-run"])

    def test_strict_next_action_ignores_stuck_advisory_run_warning(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            (root / ".agent/proof-pack.json").write_text("{}\n")
            warning = {
                "severity": "warning",
                "message": "stuck advisory",
                "rule": "repeated_command_failure",
            }
            with mock.patch(
                "agentflow.porcelain.verify_run",
                return_value={"status": "warning", "errors": [], "warnings": [warning]},
            ), mock.patch(
                "agentflow.porcelain._latest_run_verification",
                return_value={"status": "warning"},
            ), mock.patch(
                "agentflow.porcelain.verify_proof",
                return_value=[],
            ):
                action = porcelain.next_action(root, strict=True)
            self.assertEqual(action.state, "complete")

    def test_drift_failing_precedes_run_unverified(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            (root / "rogue.txt").write_text("out of scope\n")
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "drift_failing")
            self.assertEqual(action.args, ["finish-run"])

    def test_proof_missing_before_build(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            cli.main(["verify-run", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_missing")

    def test_proof_stale_after_source_changes(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            _build_verified_proof(root)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text())
            plan["objective"] = "changed after proof"
            plan_path.write_text(json.dumps(plan, indent=2))
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_stale")

    def test_proof_failing_for_invalid_metadata(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            _build_verified_proof(root)
            (root / ".agent/proof-pack.json").write_text("{}\n")  # was valid, now corrupt metadata
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_failing")

    def test_complete_when_proof_verified(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            _build_verified_proof(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "complete")
            self.assertFalse(action.blocking)
            self.assertIsNone(action.command)


class TestFinishStep(unittest.TestCase):
    def test_finish_step_verifies_and_completes(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            plan = json.loads((root / ".agent/plan.lock.json").read_text())
            result = porcelain.finish_step(root, plan, "P1", None)
            self.assertTrue(result["verified"])
            self.assertTrue(result["completed"])
            self.assertEqual(result["verification_status"], "passed")

    def test_finish_step_does_not_complete_on_failure(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])
            plan = json.loads((root / ".agent/plan.lock.json").read_text())
            result = porcelain.finish_step(root, plan, "P1", None, strict=True)
            self.assertFalse(result["completed"])
            self.assertFalse(result["verified"])
            self.assertTrue(result["diagnostics"])


class TestFinishRun(unittest.TestCase):
    def test_finish_run_all_green(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")
            self.assertTrue(result["ok"])
            self.assertIsNone(result["stopped_at"])
            self.assertEqual([g["name"] for g in result["gates"]],
                             ["audit-drift", "verify-run", "build-proof", "verify-proof"])

    def test_finish_run_stops_at_first_failing_gate(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            # Introduce an out-of-scope change so audit-drift fails.
            (root / "rogue.txt").write_text("x\n")
            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")
            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_at"], "audit-drift")
            self.assertEqual(len(result["gates"]), 1)
            # Diagnostics carry the failing gate's real output, not a swallowed
            # "see command output" pointer.
            self.assertTrue(result["diagnostics"])
            self.assertTrue(any("rogue.txt" in line for line in result["diagnostics"]))

    def test_finish_run_passes_plan_to_verify_run(self):
        calls = []

        def fake_main(argv):
            calls.append(argv)
            return 0

        root = Path("/tmp/agentflow-fixture")
        plan_path = root / "custom-plan.json"
        with mock.patch("agentflow.cli.main", side_effect=fake_main):
            result = porcelain.finish_run(root, plan_path)

        self.assertTrue(result["ok"])
        self.assertEqual(
            calls[1],
            ["verify-run", "--root", str(root), "--plan", str(plan_path)],
        )

    def test_finish_run_stops_at_each_gate(self):
        gate_order = ["audit-drift", "verify-run", "build-proof", "verify-proof"]
        for idx, failing in enumerate(gate_order):
            calls = {"n": 0}
            def fake_main(argv, _idx=idx, calls=calls):
                rc = 1 if calls["n"] == _idx else 0
                calls["n"] += 1
                return rc
            with mock.patch("agentflow.cli.main", side_effect=fake_main):
                result = porcelain.finish_run(Path("/tmp"), Path("/tmp/p.json"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_at"], failing)
            self.assertEqual([g["name"] for g in result["gates"]], gate_order[:idx + 1])


if __name__ == "__main__":
    unittest.main()
