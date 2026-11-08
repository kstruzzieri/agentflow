"""#110: the proof records, gate by gate, how each validation gate was satisfied."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.proof import core_sha256
from agentflow.viewer import collect_view_model, render_html


ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
OBSERVED_GATE = [PY, "-c", "print('observed')"]
SUMMARY = (
    "validation gates: 1 run by agentflow, 1 replayed by agentflow, "
    "1 self-reported, 2 inspection evidence, 1 missing"
)


def agentflow(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [PY, "-m", "agentflow", *args],
        cwd=str(root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _step(step_id: str, validation: list, **extra) -> dict:
    return {
        "id": step_id,
        "action": f"Fixture step {step_id}.",
        "files": ["fixture.txt"],
        "preconditions": ["Workspace initialized."],
        "expected_diff": ["None."],
        "validation": validation,
        "evidence_ids": [],
        **extra,
    }


def _plan() -> dict:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "Show how each validation gate was satisfied.",
        "scope": ["Gate provenance fixture."],
        "non_goals": [],
        "invariants": ["Fixture only."],
        "allowed_files": ["fixture.txt", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["fixture gates"],
        "rollback_plan": "Delete the fixture.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "evidence_ids": ["E1", "E2"],
        "steps": [
            # A command gate Agentflow runs, and an inspection gate met by evidence.
            _step(
                "P1",
                ["observed gate", "inspection gate"],
                gates=[
                    {"kind": "command", "run": OBSERVED_GATE},
                    {"kind": "inspection", "evidence_id": "E1", "describe": "Read the output."},
                ],
            ),
            # The agent's own report of a command it ran elsewhere.
            _step("P2", ["self-reported gate"]),
            # An attested gate that Agentflow re-ran with the same exit code.
            _step("P3", ["replayed gate"]),
            # A legacy gate with no receipt, met by the step's evidence.
            _step("P4", ["evidence gate"], evidence_ids=["E2"]),
            # A legacy gate with neither: a warning outside strict mode.
            _step("P5", ["unmet gate"]),
        ],
        "locked": False,
        "locked_at": None,
    }


def build_fixture(root: Path) -> None:
    def ok(*args: str) -> None:
        result = agentflow(root, *args)
        if result.returncode != 0:
            raise AssertionError(f"{args}: {result.stdout}{result.stderr}")

    ok("init")
    (root / ".agent/plan.lock.json").write_text(json.dumps(_plan(), indent=2), encoding="utf-8")
    ok("lock-plan", ".agent/plan.lock.json")
    ok("init-execution")
    # A contract that requires neither gate receipts nor inspection evidence
    # lets P5 complete with a gate nothing satisfied (a warning), so the proof
    # must say so.
    contract_path = root / ".agent/execution.contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["proof_policy"]["require_command_receipts_for_validation"] = False
    contract["proof_policy"]["require_evidence_for_inspection_gates"] = False
    contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    for evidence_id in ("E1", "E2"):
        ok("record-evidence", "--id", evidence_id, "--claim", "Checked by hand.", "--source", "fixture")

    def finish(step_id: str) -> None:
        ok("verify-step", step_id)
        ok("complete-step", step_id)

    ok("claim-step", "P1", "--agent", "agent-a")
    ok("run", "--step", "P1", "--", *OBSERVED_GATE)
    finish("P1")

    ok("claim-step", "P2", "--agent", "agent-a")
    ok(
        "record-command", "--step", "P2", "--exit-code", "0", "--gate", "self-reported gate",
        "--", PY, "-c", "print('attested')",
    )
    finish("P2")

    ok("claim-step", "P3", "--agent", "agent-a")
    ok(
        "record-command", "--step", "P3", "--exit-code", "0", "--gate", "replayed gate",
        "--", PY, "-c", "print('replayed')",
    )
    ok("replay-gates", "--step", "P3", "--record")
    finish("P3")

    for step_id in ("P4", "P5"):
        ok("claim-step", step_id, "--agent", "agent-a")
        finish(step_id)
    ok("build-proof")


def _load(root: Path) -> dict:
    return json.loads((root / ".agent/proof-pack.json").read_text(encoding="utf-8"))


def _save(root: Path, proof: dict, rehash: bool = True) -> None:
    if rehash:
        proof["core_sha256"] = core_sha256(proof)
    (root / ".agent/proof-pack.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")


class ValidationGateProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "fixture"
        cls.root.mkdir()
        build_fixture(cls.root)
        cls.proof = _load(cls.root)
        cls.receipts = [
            json.loads(line)
            for line in (cls.root / ".agent/command-receipts.jsonl").read_text().splitlines()
            if line.strip()
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def copy(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return Path(shutil.copytree(self.root, Path(tmp) / "fixture"))

    def entries(self, step_id: str) -> list:
        return [
            entry
            for entry in self.proof["coverage"]["validation_gates"]
            if entry["step_id"] == step_id
        ]

    def receipt_id(self, step_id: str, provenance: str) -> str:
        (receipt,) = [
            r for r in self.receipts if r["step_id"] == step_id and r["provenance"] == provenance
        ]
        return receipt["id"]

    def test_command_gate_run_by_agentflow_names_its_observed_receipt(self) -> None:
        command, _inspection = self.entries("P1")
        self.assertEqual(
            command,
            {
                "step_id": "P1",
                "attempt_id": command["attempt_id"],
                "gate": " ".join(OBSERVED_GATE),
                "kind": "command",
                "status": "satisfied",
                "receipt_id": self.receipt_id("P1", "observed"),
                "provenance": "observed",
                "evidence_id": None,
            },
        )
        self.assertTrue(command["attempt_id"])

    def test_inspection_gate_names_its_evidence(self) -> None:
        _command, inspection = self.entries("P1")
        self.assertEqual(inspection["kind"], "inspection")
        self.assertEqual(inspection["evidence_id"], "E1")
        self.assertIsNone(inspection["receipt_id"])
        self.assertIsNone(inspection["provenance"])

    def test_self_reported_gate_keeps_attested_provenance(self) -> None:
        (entry,) = self.entries("P2")
        self.assertEqual(entry["gate"], "self-reported gate")
        self.assertEqual(entry["provenance"], "attested")
        self.assertEqual(entry["receipt_id"], self.receipt_id("P2", "attested"))

    def test_replayed_gate_names_the_reconstructed_receipt(self) -> None:
        (entry,) = self.entries("P3")
        self.assertEqual(entry["provenance"], "reconstructed")
        self.assertEqual(entry["receipt_id"], self.receipt_id("P3", "reconstructed"))

    def test_legacy_gate_met_by_step_evidence_names_it(self) -> None:
        (entry,) = self.entries("P4")
        self.assertEqual((entry["kind"], entry["status"]), ("legacy", "satisfied"))
        self.assertEqual(entry["evidence_id"], "E2")
        self.assertIsNone(entry["receipt_id"])

    def test_unmet_gate_is_recorded_as_missing(self) -> None:
        (entry,) = self.entries("P5")
        self.assertEqual(entry["status"], "missing")
        self.assertIsNone(entry["receipt_id"])
        self.assertIsNone(entry["evidence_id"])

    def test_verify_proof_prints_the_summary_before_proof_verified(self) -> None:
        result = agentflow(self.root, "verify-proof")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [SUMMARY, "proof verified"])

    def test_edited_entry_fails_verify_proof(self) -> None:
        for rehash in (True, False):
            with self.subTest(rehash=rehash):
                root = self.copy()
                proof = _load(root)
                (entry,) = [e for e in proof["coverage"]["validation_gates"] if e["step_id"] == "P2"]
                entry["provenance"] = "observed"
                _save(root, proof, rehash=rehash)

                result = agentflow(root, "verify-proof")

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertNotIn("proof verified", result.stdout)
                if rehash:
                    self.assertIn("validation gate coverage is stale or tampered", result.stdout)

    def test_removed_entries_fail_verify_proof(self) -> None:
        root = self.copy()
        proof = _load(root)
        del proof["coverage"]["validation_gates"]
        _save(root, proof)

        result = agentflow(root, "verify-proof")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("validation gate coverage", result.stdout)

    def test_missing_plan_lock_is_reported_once_as_a_missing_source(self) -> None:
        root = self.copy()
        (root / ".agent/plan.lock.json").unlink()

        result = agentflow(root, "verify-proof")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("missing source .agent/plan.lock.json", result.stdout)
        self.assertNotIn("validation gate coverage", result.stdout)

    def test_view_proof_html_survives_a_hand_edited_provenance(self) -> None:
        # view-proof renders proofs it has not verified.
        root = self.copy()
        proof = _load(root)
        proof["coverage"]["validation_gates"][0]["provenance"] = ["observed"]
        _save(root, proof, rehash=False)

        html = render_html(collect_view_model(root, root / ".agent/proof-pack.json", root / "p.html"))

        self.assertIn("validation gates: ", html)

    def test_view_proof_html_shows_the_summary_and_each_gate(self) -> None:
        output = self.root / "proof.html"
        html = render_html(collect_view_model(self.root, self.root / ".agent/proof-pack.json", output))
        self.assertIn(SUMMARY, html)
        self.assertIn(self.receipt_id("P3", "reconstructed"), html)
        self.assertIn("self-reported gate", html)


class EvidenceOnlyProofTests(unittest.TestCase):
    def test_proof_without_execution_contract_has_no_gate_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(agentflow(root, "init").returncode, 0)
            plan = _plan()
            plan["steps"] = [_step("P1", ["manual check"])]
            plan["evidence_ids"] = []
            (root / ".agent/plan.lock.json").write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(agentflow(root, "lock-plan", ".agent/plan.lock.json").returncode, 0)
            build = agentflow(root, "build-proof")
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)

            result = agentflow(root, "verify-proof")

            self.assertNotIn("validation_gates", _load(root)["coverage"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("validation gates:", result.stdout)


if __name__ == "__main__":
    unittest.main()
