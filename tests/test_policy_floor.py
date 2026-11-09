"""#111: verify-proof --require holds a proof to a verifier-chosen minimum policy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentflow import proof as proof_module
from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.proof import core_sha256


ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
GATE = [PY, "-c", "print('gate')"]
CONTRACT = ".agent/execution.contract.json"

# Each weak setting the fixture's contract carries, and the floor it misses.
WEAK = {
    "hunk_attribution": ("off", "enforce"),
    "risk_policy": ("warn", "require-confirmation"),
    "review_gate": ("ignore", "warn"),
    "lease_policy": ("advisory", "enforce"),
    "require_managed_receipts_for_validation": (False, "true"),
}
SECTIONS = {
    "hunk_attribution": "proof_policy",
    "risk_policy": "command_policy",
    "review_gate": "proof_policy",
    "lease_policy": "concurrency",
    "require_managed_receipts_for_validation": "proof_policy",
}


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


def _plan(with_gate: bool = True) -> dict:
    step = {
        "id": "P1",
        "action": "Fixture step.",
        "files": ["fixture.txt"],
        "preconditions": ["Workspace initialized."],
        "expected_diff": ["None."],
        "validation": ["fixture gate"],
        "evidence_ids": [],
    }
    if with_gate:
        step["gates"] = [{"kind": "command", "run": GATE}]
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "Hold a proof to a policy floor.",
        "scope": ["Policy floor fixture."],
        "non_goals": [],
        "invariants": ["Fixture only."],
        "allowed_files": ["fixture.txt", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["fixture gate"],
        "rollback_plan": "Delete the fixture.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "evidence_ids": [],
        "steps": [step],
        "locked": False,
        "locked_at": None,
    }


def _ok(root: Path, *args: str) -> None:
    result = agentflow(root, *args)
    if result.returncode != 0:
        raise AssertionError(f"{args}: {result.stdout}{result.stderr}")


def _edit_contract(root: Path, edit) -> None:
    path = root / CONTRACT
    contract = json.loads(path.read_text(encoding="utf-8"))
    edit(contract)
    path.write_text(json.dumps(contract, indent=2), encoding="utf-8")


def _weaken(contract: dict) -> None:
    for key, (weak, _floor) in WEAK.items():
        contract[SECTIONS[key]][key] = weak


def build_fixture(root: Path) -> None:
    _ok(root, "init")
    (root / ".agent/plan.lock.json").write_text(json.dumps(_plan(), indent=2), encoding="utf-8")
    _ok(root, "lock-plan", ".agent/plan.lock.json")
    _ok(root, "init-execution")
    _edit_contract(root, _weaken)
    _ok(root, "claim-step", "P1", "--agent", "agent-a")
    _ok(root, "run", "--step", "P1", "--", *GATE)
    _ok(root, "verify-step", "P1")
    _ok(root, "complete-step", "P1")
    _ok(root, "build-proof")


def _rehash_contract(root: Path) -> None:
    """Re-seal the bundle around an edited contract, as a hand-made bundle would."""
    proof_path = root / ".agent/proof-pack.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256((root / CONTRACT).read_bytes()).hexdigest()
    for item in proof["files"]:
        if item["path"] == CONTRACT:
            item["sha256"] = digest
    proof["core_sha256"] = core_sha256(proof)
    proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")


class PolicyFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "fixture"
        cls.root.mkdir()
        build_fixture(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def copy(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return Path(shutil.copytree(self.root, Path(tmp) / "fixture"))

    def verify(self, root: Path, *requirements: str) -> subprocess.CompletedProcess:
        args = ["verify-proof"]
        for requirement in requirements:
            args += ["--require", requirement]
        return agentflow(root, *args)

    def test_proof_with_weak_policy_verifies_without_a_floor(self) -> None:
        result = self.verify(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("proof verified", result.stdout)

    def test_each_weak_setting_fails_its_matching_floor(self) -> None:
        for key, (weak, floor) in WEAK.items():
            with self.subTest(key=key):
                result = self.verify(self.root, f"{key}={floor}")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(
                    f"error: policy_below_floor: {key} is {str(weak).lower()}; "
                    f"the floor requires {key}={floor}",
                    result.stdout,
                )
                self.assertNotIn("proof verified", result.stdout)

    def test_floor_at_or_below_the_contract_setting_passes(self) -> None:
        result = self.verify(
            self.root,
            "hunk_attribution=off",
            "risk_policy=warn",
            "review_gate=ignore",
            "lease_policy=advisory",
            "require_managed_receipts_for_validation=false",
            # Set by init-execution and left alone by the fixture.
            "require_command_receipts_for_validation=true",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("proof verified", result.stdout)

    def test_every_unmet_requirement_is_reported(self) -> None:
        result = self.verify(self.root, "hunk_attribution=observe", "review_gate=block")
        self.assertEqual(result.returncode, 1)
        self.assertIn("policy_below_floor: hunk_attribution is off", result.stdout)
        self.assertIn("policy_below_floor: review_gate is ignore", result.stdout)

    def test_missing_key_counts_as_its_default(self) -> None:
        root = self.copy()

        def drop(contract: dict) -> None:
            # The optional keys; build-proof refuses a contract missing the others.
            del contract["proof_policy"]["hunk_attribution"]
            del contract["command_policy"]["risk_policy"]
            del contract["proof_policy"]["require_review_run"]

        _edit_contract(root, drop)
        _ok(root, "build-proof")

        passed = self.verify(root, "hunk_attribution=enforce", "require_review_run=false")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        failed = self.verify(root, "risk_policy=require-confirmation", "require_review_run=true")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("policy_below_floor: risk_policy is warn", failed.stdout)
        self.assertIn("policy_below_floor: require_review_run is false", failed.stdout)

    def test_contract_edited_after_build_fails_as_tampering_not_below_floor(self) -> None:
        # Raised to meet the floor, and lowered further (built as "off") below it.
        for value in ("enforce", "observe"):
            with self.subTest(value=value):
                root = self.copy()
                _edit_contract(
                    root, lambda c: c["proof_policy"].__setitem__("hunk_attribution", value)
                )
                result = self.verify(root, "hunk_attribution=enforce")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"hash mismatch for {CONTRACT}", result.stdout)
                self.assertNotIn("policy_below_floor", result.stdout)

    def test_unrecognized_contract_value_fails_closed(self) -> None:
        root = self.copy()
        _edit_contract(root, lambda c: c["proof_policy"].__setitem__("hunk_attribution", "bogus"))
        _rehash_contract(root)
        self.assertEqual(self.verify(root).returncode, 0)

        result = self.verify(root, "hunk_attribution=off")

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            'policy_below_floor: hunk_attribution has unrecognized value "bogus"', result.stdout
        )

    def test_core_checksum_failure_suppresses_the_floor(self) -> None:
        root = self.copy()
        proof_path = root / ".agent/proof-pack.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["core_sha256"] = "0" * 64
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")

        result = self.verify(root, "hunk_attribution=enforce")

        self.assertEqual(result.returncode, 1)
        self.assertIn("proof canonical core checksum mismatch", result.stdout)
        self.assertNotIn("policy_below_floor", result.stdout)

    def test_floor_judges_the_bytes_that_were_hashed(self) -> None:
        # A producer that can still write the root swaps in a strong contract
        # after verify-proof hashed the weak one. The floor must not see it.
        root = self.copy()
        real_core = proof_module.core_sha256

        def swap_then_hash(proof: dict) -> str:
            _edit_contract(
                root, lambda c: c["proof_policy"].__setitem__("hunk_attribution", "enforce")
            )
            return real_core(proof)

        with mock.patch.object(proof_module, "core_sha256", side_effect=swap_then_hash):
            findings = proof_module.verify_proof(
                root, root / ".agent/proof-pack.json", floor=[("hunk_attribution", "enforce")]
            )

        self.assertIn(
            "policy_below_floor: hunk_attribution is off; "
            "the floor requires hunk_attribution=enforce",
            [finding["message"] for finding in findings],
        )

    def test_section_that_is_not_an_object_fails_closed(self) -> None:
        root = self.copy()
        _edit_contract(root, lambda c: c.__setitem__("concurrency", []))
        _rehash_contract(root)

        result = self.verify(root, "lease_policy=advisory")

        self.assertEqual(result.returncode, 1)
        self.assertIn("policy_below_floor: lease_policy has unrecognized value null", result.stdout)

    def test_unreadable_contract_fails_closed(self) -> None:
        root = self.copy()
        (root / CONTRACT).write_text("{not json", encoding="utf-8")
        _rehash_contract(root)

        result = self.verify(root, "review_gate=ignore")

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "policy_below_floor: the proof has no readable execution contract", result.stdout
        )

    def test_contract_listed_under_another_spelling_counts_as_missing(self) -> None:
        root = self.copy()
        proof_path = root / ".agent/proof-pack.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        respelled = "./" + CONTRACT
        proof["generated_from"] = [respelled if p == CONTRACT else p for p in proof["generated_from"]]
        for item in proof["files"]:
            if item["path"] == CONTRACT:
                item["path"] = respelled
        proof["core_sha256"] = core_sha256(proof)
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")

        result = self.verify(root, "hunk_attribution=off")

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "policy_below_floor: the proof has no readable execution contract", result.stdout
        )

    def test_boolean_setting_must_be_a_real_boolean(self) -> None:
        root = self.copy()
        _edit_contract(
            root, lambda c: c["proof_policy"].__setitem__("require_review_run", 1)
        )
        _rehash_contract(root)

        result = self.verify(root, "require_review_run=true")

        self.assertEqual(result.returncode, 1)
        self.assertIn("policy_below_floor: require_review_run has unrecognized value 1", result.stdout)

    def test_invalid_requirement_is_a_usage_error(self) -> None:
        for requirement in (
            "hunk_attribution",
            "unknown_key=on",
            "hunk_attribution=strict",
            "require_review_run=yes",
        ):
            with self.subTest(requirement=requirement):
                result = self.verify(self.root, requirement)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("--require", result.stderr)


class EvidenceOnlyFloorTests(unittest.TestCase):
    def test_floor_fails_a_proof_without_an_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ok(root, "init")
            (root / ".agent/plan.lock.json").write_text(
                json.dumps(_plan(with_gate=False)), encoding="utf-8"
            )
            _ok(root, "lock-plan", ".agent/plan.lock.json")
            _ok(root, "build-proof")
            self.assertEqual(agentflow(root, "verify-proof").returncode, 0)

            result = agentflow(root, "verify-proof", "--require", "review_gate=ignore")

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "policy_below_floor: the proof has no readable execution contract; "
                "the floor requires review_gate=ignore",
                result.stdout,
            )


if __name__ == "__main__":
    unittest.main()
