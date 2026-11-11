from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentflow.artifacts import append_jsonl, create_initial_artifacts, write_json
from agentflow.cli import main as cli_main
from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.execution import claim_step, init_execution_artifacts
from agentflow.receipts import command_receipts, record_command, run_command
from agentflow.simulate import simulate_risk

LOW = ["python3", "-c", "print('ok')"]
MEDIUM = ["cp", "fixture.txt", "copy.txt"]  # write_outside_scope=medium
HIGH = ["sh", "-c", "echo x > outside.txt"]  # write_outside_scope=high
READS_NOTES = ["cat", "notes.txt"]  # low until notes.txt is blocked


def plan(blocked_files=None) -> dict:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "Simulate risk fixture.",
        "scope": ["Exercise simulate-risk."],
        "non_goals": [],
        "invariants": ["Read-only."],
        "allowed_files": ["fixture.txt", ".agent/"],
        "blocked_files": blocked_files or [],
        "validation_gates": ["python3 -c \"print('ok')\""],
        "rollback_plan": "Delete the root.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [
            {
                "id": "P1",
                "action": "Edit fixture.",
                "files": ["fixture.txt"],
                "preconditions": ["Claimed."],
                "expected_diff": ["fixture.txt"],
                "validation": ["python3 -c \"print('ok')\""],
                "evidence_ids": [],
            }
        ],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


def set_risk_policy(root: Path, value) -> None:
    path = root / ".agent/execution.contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        contract["command_policy"].pop("risk_policy", None)
    else:
        contract["command_policy"]["risk_policy"] = value
    path.write_text(json.dumps(contract), encoding="utf-8")


def agent_snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / ".agent").rglob("*"))
        if path.is_file()
    }


def ids(entries) -> list:
    return [entry["receipt_id"] for entry in entries]


def make_root(tmp: str) -> Path:
    """Run LOW, MEDIUM, HIGH and READS_NOTES under warn as CR1-CR4."""
    root = Path(tmp)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    create_initial_artifacts(root)
    init_execution_artifacts(root)
    write_json(root / ".agent/plan.lock.json", plan())
    set_risk_policy(root, "warn")
    claim_step(root, plan(), "P1", "agent-a")
    (root / "fixture.txt").write_text("x\n", encoding="utf-8")
    (root / "notes.txt").write_text("n\n", encoding="utf-8")
    for command in (LOW, MEDIUM, HIGH, READS_NOTES):
        run_command(root, plan(), "P1", None, command)
    return root


class SimulateRiskTests(unittest.TestCase):

    def test_each_proposed_policy_reports_only_what_it_would_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)

            warn = simulate_risk(root, plan(), risk_policy="warn")
            confirm = simulate_risk(root, plan(), risk_policy="require-confirmation")
            block = simulate_risk(root, plan(), risk_policy="block")

            self.assertEqual(warn["stopped"], [])
            self.assertEqual(ids(confirm["stopped"]), ["CR3"])
            self.assertEqual(confirm["stopped"][0]["outcome"], "needs_confirmation")
            self.assertEqual(ids(block["stopped"]), ["CR3"])
            entry = block["stopped"][0]
            self.assertEqual(entry["outcome"], "blocked")
            self.assertEqual(entry["step_id"], "P1")
            self.assertEqual(entry["command"], HIGH)
            self.assertEqual(entry["level"], "high")
            self.assertEqual(entry["categories"], ["write_outside_scope"])
            self.assertFalse(entry["advisory"])
            self.assertEqual(block["by_category"], {"write_outside_scope": ["CR3"]})
            self.assertEqual(block["current_risk_policy"], "warn")
            self.assertEqual(block["proposed_risk_policy"], "block")
            self.assertEqual(block["receipts"], 4)
            self.assertEqual(block["skipped"], 0)
            self.assertEqual(block["analyzer_drift"], [])

    def test_a_proposed_blocked_path_flags_an_earlier_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)

            report = simulate_risk(
                root, plan(), proposed_plan=plan(["notes.txt"]), risk_policy="block"
            )

            self.assertEqual(ids(report["stopped"]), ["CR3", "CR4"])
            self.assertEqual(report["stopped"][1]["categories"], ["blocked_path"])
            self.assertEqual(report["by_category"]["blocked_path"], ["CR4"])
            # Drift compares against the current plan, so a plan change is not drift.
            self.assertEqual(report["analyzer_drift"], [])

    def test_invalid_plans_are_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            before = agent_snapshot(root)
            for field, value in (
                ("blocked_files", "notes.txt"),
                ("allowed_files", "fixture.txt"),
                ("steps", None),
                ("schema_version", "2.0.0"),
            ):
                invalid = dict(plan(), **{field: value})
                for source in ("current", "proposed"):
                    with self.subTest(field=field, source=source):
                        with self.assertRaises(ValueError):
                            simulate_risk(
                                root, invalid if source == "current" else plan(),
                                proposed_plan=invalid if source == "proposed" else plan(),
                                risk_policy="block",
                            )
            self.assertEqual(agent_snapshot(root), before)

    def test_commands_agentflow_did_not_run_are_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            record_command(root, "P1", None, ["rm", "-rf", "build"], 0, plan=plan())

            report = simulate_risk(root, plan(), risk_policy="block")

            self.assertEqual(ids(report["stopped"]), ["CR3", "CR5"])
            attested = report["stopped"][1]
            self.assertTrue(attested["advisory"])
            self.assertEqual(attested["provenance"], "attested")
            self.assertEqual(attested["categories"], ["destructive_delete"])
            self.assertFalse(report["stopped"][0]["advisory"])

    def test_blocked_and_confirmed_receipts_are_not_new_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            set_risk_policy(root, "require-confirmation")
            blocked = run_command(root, plan(), "P1", None, ["sh", "-c", "echo y > a.txt"])
            confirmed = run_command(
                root, plan(), "P1", None, ["sh", "-c", "echo z > b.txt"],
                confirmed=True, confirmation_source="cli",
            )
            self.assertEqual(blocked["decision"], "blocked")
            self.assertTrue(confirmed["confirmed"])

            confirm = simulate_risk(root, plan(), risk_policy="require-confirmation")
            block = simulate_risk(root, plan(), risk_policy="block")

            # CR5 never ran; CR6 was confirmed, which require-confirmation accepts.
            self.assertEqual(ids(confirm["stopped"]), ["CR3"])
            # block ignores confirmation, so the confirmed command is a new stop.
            self.assertEqual(ids(block["stopped"]), ["CR3", "CR6"])

    def test_analyzer_drift_is_reported_apart_from_policy_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            stale = dict(
                command_receipts(root)[0],
                id="CR5",
                command=["rm", "-rf", "build"],
                risk={"level": "low", "findings": []},
            )
            append_jsonl(root / ".agent/command-receipts.jsonl", stale)

            report = simulate_risk(root, plan(), risk_policy="warn")

            self.assertEqual(report["stopped"], [])
            self.assertEqual(
                report["analyzer_drift"],
                [
                    {
                        "receipt_id": "CR5",
                        "step_id": "P1",
                        "recorded_level": "low",
                        "current_level": "high",
                        "recorded_categories": [],
                        "current_categories": ["destructive_delete"],
                    }
                ],
            )

    def test_default_proposed_policy_is_the_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            set_risk_policy(root, "require-confirmation")
            report = simulate_risk(root, plan())
            self.assertEqual(report["current_risk_policy"], "require-confirmation")
            self.assertEqual(report["proposed_risk_policy"], "require-confirmation")
            self.assertEqual(ids(report["stopped"]), ["CR3"])

            set_risk_policy(root, None)  # a contract without risk_policy acts as warn
            report = simulate_risk(root, plan())
            self.assertEqual(report["current_risk_policy"], "warn")
            self.assertEqual(report["stopped"], [])

    def test_invalid_contract_policy_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            set_risk_policy(root, "loose")
            with self.assertRaisesRegex(ValueError, "unknown risk_policy"):
                simulate_risk(root, plan(), risk_policy="block")

    def test_malformed_receipts_are_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            receipt = dict(command_receipts(root)[2], id="CR5")
            ledger = root / ".agent/command-receipts.jsonl"
            original = ledger.read_text(encoding="utf-8")
            for bad in (
                None, [], "not a receipt", 42,
                dict(receipt, command="rm -rf build"),
                dict(receipt, command=[]),
                dict(receipt, step_id=None),
                dict(receipt, id=[]),
                dict(receipt, provenance=[]),
                dict(receipt, risk={"level": [], "findings": []}),
            ):
                with self.subTest(receipt=bad):
                    ledger.write_text(json.dumps(bad) + "\n" + original, encoding="utf-8")
                    before = agent_snapshot(root)
                    report = simulate_risk(root, plan(), risk_policy="block")
                    self.assertEqual(report["receipts"], 5)
                    self.assertEqual(report["skipped"], 1)
                    self.assertEqual(ids(report["stopped"]), ["CR3"])
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = cli_main([
                            "simulate-risk", "--root", str(root), "--risk-policy", "block", "--json"
                        ])
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(out.getvalue()), report)
                    self.assertEqual(agent_snapshot(root), before)

    def test_missing_receipt_metadata_remains_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            receipt = dict(command_receipts(root)[2])
            for field in ("id", "provenance", "risk"):
                receipt.pop(field)
            append_jsonl(root / ".agent/command-receipts.jsonl", receipt)
            report = simulate_risk(root, plan(), risk_policy="block")
            self.assertEqual(report["skipped"], 0)
            self.assertEqual(ids(report["stopped"]), ["CR3", None])
            self.assertIsNone(report["stopped"][1]["provenance"])
            self.assertTrue(report["stopped"][1]["advisory"])

    def test_simulate_risk_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "proposed.json").write_text(json.dumps(plan(["notes.txt"])), encoding="utf-8")
            before = agent_snapshot(root)

            simulate_risk(root, plan(), proposed_plan=plan(["notes.txt"]), risk_policy="block")
            with contextlib.redirect_stdout(io.StringIO()):
                cli_main(["simulate-risk", "--root", str(root), "--plan", "proposed.json",
                          "--risk-policy", "block"])
                cli_main(["simulate-risk", "--root", str(root), "--json"])

            self.assertEqual(agent_snapshot(root), before)


class SimulateRiskCliTests(unittest.TestCase):
    def _run(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            code, out, _ = self._run(
                "simulate-risk", "--root", str(root), "--risk-policy", "block", "--json"
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(ids(payload["stopped"]), ["CR3"])
            self.assertEqual(payload["plan"], ".agent/plan.lock.json")

    def test_text_output_with_a_proposed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "proposed.json").write_text(json.dumps(plan(["notes.txt"])), encoding="utf-8")
            code, out, _ = self._run(
                "simulate-risk", "--root", str(root), "--plan", "proposed.json",
                "--risk-policy", "block",
            )
            self.assertEqual(code, 0)
            self.assertIn(
                "simulate-risk: risk_policy warn -> block under proposed.json; 4 receipt(s)", out
            )
            self.assertIn("would block 2 command(s)", out)
            self.assertIn("blocked_path: CR4", out)
            self.assertIn("write_outside_scope: CR3", out)
            self.assertIn("CR4 [P1] high: cat notes.txt", out)

    def test_invalid_policy_and_unreadable_plan_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli_main(["simulate-risk", "--root", str(root), "--risk-policy", "loose"])
            self.assertEqual(ctx.exception.code, 2)

            code, _, err = self._run("simulate-risk", "--root", str(root), "--plan", "missing.json")
            self.assertEqual(code, 1)
            self.assertIn("error: cannot read plan missing.json", err)

    def test_invalid_proposed_plan_reports_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            before = agent_snapshot(root)
            for field, value in (("blocked_files", "notes.txt"), ("schema_version", "2.0.0")):
                with self.subTest(field=field):
                    (root / "proposed.json").write_text(
                        json.dumps(dict(plan(), **{field: value})), encoding="utf-8"
                    )
                    code, out, err = self._run(
                        "simulate-risk", "--root", str(root), "--plan", "proposed.json",
                        "--risk-policy", "block", "--json",
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("error:", err)
            self.assertEqual(agent_snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
