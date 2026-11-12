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
from agentflow.artifacts import default_drift_report, read_jsonl, write_json
from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.execution import execution_contract_sha256, execution_contract_snapshot
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


def _start(root: Path, edit=None) -> None:
    """Lock the fixture plan and initialize execution, editing the contract first."""
    _ok(root, "init")
    (root / ".agent/plan.lock.json").write_text(json.dumps(_plan(), indent=2), encoding="utf-8")
    _ok(root, "lock-plan", ".agent/plan.lock.json")
    _ok(root, "init-execution")
    if edit is not None:
        _edit_contract(root, edit)


def _finish(root: Path) -> None:
    """Satisfy the gate, close P1 and seal the bundle under the current contract."""
    _ok(root, "run", "--step", "P1", "--", *GATE)
    _ok(root, "verify-step", "P1")
    _ok(root, "complete-step", "P1")
    # #117: a floor on a runtime policy needs a drift report audit-drift wrote.
    _ok(root, "audit-drift")
    _ok(root, "build-proof")


def build_fixture(root: Path, edit=_weaken) -> None:
    _start(root, edit)
    _ok(root, "claim-step", "P1", "--agent", "agent-a")
    _finish(root)


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
        def drop(contract: dict) -> None:
            _weaken(contract)
            # The optional keys; build-proof refuses a contract missing the others.
            del contract["proof_policy"]["hunk_attribution"]
            del contract["command_policy"]["risk_policy"]
            del contract["proof_policy"]["require_review_run"]

        # Dropped before the work starts: a contract edited mid-run fails a
        # runtime-policy floor on its own (#117).
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = Path(tmp)
        build_fixture(root, drop)

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


PIN = "execution_contract_sha256"
LEDGERS = (
    ".agent/step-runs.jsonl",
    ".agent/command-receipts.jsonl",
    ".agent/file-receipts.jsonl",
    ".agent/verification-runs.jsonl",
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _records(root: Path, ledger: str) -> list:
    lines = (root / ledger).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class ContractPinWriterTests(unittest.TestCase):
    """#117: a record names the contract its runtime policy was read from."""

    def test_an_unreadable_snapshot_stays_unreadable_after_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _start(root)
            path = root / CONTRACT
            strong = path.read_bytes()
            path.unlink()
            path.mkdir()
            with execution_contract_snapshot():
                with self.assertRaises(IsADirectoryError):
                    execution_contract_sha256(root)
                path.rmdir()
                path.write_bytes(strong)
                with self.assertRaises(IsADirectoryError):
                    execution_contract_sha256(root)
            self.assertEqual(execution_contract_sha256(root), _digest(strong))

    def test_run_refuses_an_unreadable_contract_before_launching_the_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _start(root)
            path = root / CONTRACT
            (root / "strong.json").write_bytes(path.read_bytes())
            _ok(root, "claim-step", "P1", "--agent", "agent-a")
            path.unlink()
            (root / "unreadable").mkdir()
            path.symlink_to("../unreadable", target_is_directory=True)
            result = agentflow(root, "run", "--step", "P1", "--", PY, "-c",
                "from pathlib import Path; Path('child-ran').touch(); "
                "p = Path('.agent/execution.contract.json'); p.unlink(); "
                "p.write_bytes(Path('strong.json').read_bytes())")

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / "child-ran").exists())
            self.assertEqual(_records(root, ".agent/command-receipts.jsonl"), [])

    def test_every_runtime_record_carries_the_contract_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            _start(root, lambda c: c["command_policy"].__setitem__("command_timeout_seconds", 1))
            digest = _digest((root / CONTRACT).read_bytes())
            _ok(root, "claim-step", "P1", "--agent", "agent-a")
            _ok(root, "renew-lease", "P1", "--agent", "agent-a")
            # Blocked: high risk, unconfirmed, under the default require-confirmation.
            agentflow(root, "run", "--step", "P1", "--", "rm", "-rf", "nothing-here")
            agentflow(root, "run", "--step", "P1", "--", PY, "-c", "import time; time.sleep(5)")
            # A timed-out receipt keeps its attempt from completing.
            _ok(root, "fail-step", "P1", "--reason", "timed out", "--agent", "agent-a")
            _ok(root, "claim-step", "P1", "--agent", "agent-a")
            _ok(root, "run", "--step", "P1", "--", *GATE)
            _ok(root, "record-command", "--step", "P1", "--gate", "fixture gate",
                "--exit-code", "0", "--", *GATE)
            _ok(root, "replay-gates", "--record")
            (root / "fixture.txt").write_text("changed\n", encoding="utf-8")
            _ok(root, "record-file-change", "--step", "P1", "--path", "fixture.txt")
            _ok(root, "verify-step", "P1")
            _ok(root, "complete-step", "P1")
            agentflow(root, "verify-run")
            agentflow(root, "audit-drift")

            receipts = _records(root, ".agent/command-receipts.jsonl")
            self.assertEqual(
                {(r["provenance"], r.get("decision")) for r in receipts},
                {
                    ("observed", "blocked"),
                    ("observed", "timeout"),
                    ("observed", "allowed"),
                    ("attested", "allowed"),
                    ("reconstructed", "allowed"),
                },
            )
            events = {e["event"] for e in _records(root, ".agent/step-runs.jsonl")}
            self.assertEqual(
                events, {"claimed", "lease_renewed", "failed", "verified", "completed"}
            )
            scopes = {v["scope"] for v in _records(root, ".agent/verification-runs.jsonl")}
            self.assertEqual(scopes, {"step", "run"})
            for ledger in LEDGERS:
                for record in _records(root, ledger):
                    with self.subTest(ledger=ledger, record=record.get("id", record.get("event"))):
                        self.assertEqual(record.get(PIN), digest)
                        self.assertEqual(record["schema_version"], "1.1.0")
            drift = json.loads((root / ".agent/drift-report.json").read_text(encoding="utf-8"))
            self.assertEqual(drift.get(PIN), digest)
            self.assertEqual(drift["schema_version"], "1.1.0")

    def test_a_command_that_rewrites_the_contract_is_pinned_to_the_one_it_ran_under(self) -> None:
        # The child restores a strong contract before exit. The receipt must
        # name the weak bytes risk_policy was read from, not the restored ones.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _start(root, _weaken)
            weak = (root / CONTRACT).read_bytes()
            strong = json.loads(weak)
            strong["command_policy"]["risk_policy"] = "block"
            (root / "strong.json").write_text(json.dumps(strong), encoding="utf-8")
            _ok(root, "claim-step", "P1", "--agent", "agent-a")

            _ok(root, "run", "--step", "P1", "--", PY, "-c",
                "import shutil; shutil.copy('strong.json', '.agent/execution.contract.json')")

            self.assertEqual((root / CONTRACT).read_text(encoding="utf-8"), json.dumps(strong))
            receipt = _records(root, ".agent/command-receipts.jsonl")[-1]
            self.assertEqual(receipt[PIN], _digest(weak))

    def test_replayed_gates_are_pinned_to_the_contract_from_before_they_ran(self) -> None:
        # replay-gates runs a gate before anything else reads the contract, so
        # the snapshot must be taken when the command starts.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _start(root, _weaken)
            weak = (root / CONTRACT).read_bytes()
            strong = json.loads(weak)
            strong["command_policy"]["risk_policy"] = "block"
            (root / "strong.json").write_text(json.dumps(strong), encoding="utf-8")
            restore = [PY, "-c",
                       "import shutil; shutil.copy('strong.json', '.agent/execution.contract.json')"]
            _ok(root, "claim-step", "P1", "--agent", "agent-a")
            _ok(root, "record-command", "--step", "P1", "--gate", "fixture gate",
                "--exit-code", "0", "--", *restore)

            _ok(root, "replay-gates", "--record")

            receipt = _records(root, ".agent/command-receipts.jsonl")[-1]
            self.assertEqual(receipt["provenance"], "reconstructed")
            self.assertEqual(receipt[PIN], _digest(weak))

    def test_ledgers_mixing_1_0_and_1_1_records_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/step-runs.jsonl"
            path.parent.mkdir()
            base = {"event": "claimed", "step_id": "P1", "attempt_id": "A1", "recorded_at": "t"}
            path.write_text(
                json.dumps({**base, "schema_version": "1.0.0"}) + "\n"
                + json.dumps({**base, "schema_version": "1.1.0", PIN: "0" * 64}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(read_jsonl(path)), 2)


RUNTIME_FLOOR = (
    "risk_policy=require-confirmation",
    "lease_policy=advisory",
    "hunk_attribution=enforce",
)


def _pin_errors(result: subprocess.CompletedProcess) -> list:
    return [line for line in result.stdout.splitlines() if "pinned to the verified" in line]


def _rewrite(root: Path, ledger: str, records: list) -> None:
    (root / ledger).write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class ContractPinFloorTests(unittest.TestCase):
    """#117: a runtime-policy floor holds the run to the contract it ran under."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        # The contract never changes, so every record names the hashed one.
        cls.steady = Path(cls._tmp.name) / "steady"
        cls.steady.mkdir()
        build_fixture(cls.steady, edit=None)
        # The issue's scenario: work and audit under a weak contract, raise it, build.
        cls.raised = Path(cls._tmp.name) / "raised"
        cls.raised.mkdir()
        _start(cls.raised, _weaken)
        _ok(cls.raised, "claim-step", "P1", "--agent", "agent-a")
        _ok(cls.raised, "run", "--step", "P1", "--", *GATE)
        _ok(cls.raised, "verify-step", "P1")
        _ok(cls.raised, "complete-step", "P1")
        _ok(cls.raised, "audit-drift")

        def raise_both(contract: dict) -> None:
            contract["command_policy"]["risk_policy"] = "block"
            contract["proof_policy"]["hunk_attribution"] = "enforce"

        _edit_contract(cls.raised, raise_both)
        _ok(cls.raised, "build-proof")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def copy(self, root: Path) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return Path(shutil.copytree(root, Path(tmp) / root.name))

    def fresh(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return Path(tmp)

    def verify(self, root: Path, *requirements: str) -> subprocess.CompletedProcess:
        args = ["verify-proof"]
        for requirement in requirements:
            args += ["--require", requirement]
        return agentflow(root, *args)

    def test_a_run_whose_contract_never_changed_passes(self) -> None:
        result = self.verify(self.steady, *RUNTIME_FLOOR)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("proof verified", result.stdout)

    def test_raising_the_contract_before_build_fails_the_floor(self) -> None:
        self.assertEqual(self.verify(self.raised).returncode, 0)

        result = self.verify(self.raised, "risk_policy=block", "hunk_attribution=enforce")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("risk_policy is", result.stdout)
        self.assertNotIn("hunk_attribution is", result.stdout)
        errors = "\n".join(_pin_errors(result))
        for source in (*LEDGERS[:2], LEDGERS[3], ".agent/drift-report.json"):
            self.assertIn(source, errors)
        self.assertNotIn("proof verified", result.stdout)

    def test_a_build_time_floor_does_not_need_the_pin(self) -> None:
        # review_gate is judged again under the hashed contract at verify time.
        result = self.verify(self.raised, "review_gate=ignore")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dropping_and_restoring_the_contract_between_events_fails(self) -> None:
        root = self.fresh()
        _start(root)
        strong = (root / CONTRACT).read_bytes()
        _ok(root, "claim-step", "P1", "--agent", "agent-a")
        _edit_contract(root, lambda c: c["command_policy"].__setitem__("risk_policy", "warn"))
        _ok(root, "run", "--step", "P1", "--", *GATE)
        (root / CONTRACT).write_bytes(strong)
        _ok(root, "verify-step", "P1")
        _ok(root, "complete-step", "P1")
        _ok(root, "audit-drift")
        _ok(root, "build-proof")

        result = self.verify(root, "risk_policy=require-confirmation")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            _pin_errors(result),
            [
                "error: policy_below_floor: .agent/command-receipts.jsonl has 1 record(s) "
                "not pinned to the verified execution contract "
                "(1 under another contract, 0 unpinned; first at line 1)"
            ],
        )

    def test_a_command_that_restores_the_contract_while_it_runs_fails(self) -> None:
        root = self.fresh()
        _start(root)
        (root / "strong.json").write_bytes((root / CONTRACT).read_bytes())
        _ok(root, "claim-step", "P1", "--agent", "agent-a")
        _edit_contract(root, lambda c: c["command_policy"].__setitem__("risk_policy", "warn"))
        _ok(root, "run", "--step", "P1", "--", PY, "-c",
            "import shutil; shutil.copy('strong.json', '.agent/execution.contract.json')")
        _finish(root)

        result = self.verify(root, "risk_policy=require-confirmation")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(".agent/command-receipts.jsonl has 1 record(s) not pinned", result.stdout)

    def test_retargeting_contract_symlinks_cannot_restore_a_runtime_floor(self) -> None:
        for replacement in ("file", "symlink", "agent_directory"):
            with self.subTest(replacement=replacement):
                root = self.fresh()
                _start(root)
                path = root / CONTRACT
                (root / "strong.json").write_bytes(path.read_bytes())
                _ok(root, "claim-step", "P1", "--agent", "agent-a")
                _edit_contract(root, lambda c: c["command_policy"].__setitem__("risk_policy", "warn"))
                weak = path.read_bytes()
                if replacement == "agent_directory":
                    (root / ".agent").rename(root / "weak-agent")
                    (root / ".agent").symlink_to("weak-agent", target_is_directory=True)
                    restore = (
                        "import shutil; shutil.copytree('.agent', 'strong-agent'); "
                        "Path('strong-agent/execution.contract.json').write_bytes(Path('strong.json').read_bytes()); "
                        "Path('.agent').unlink(); Path('.agent').symlink_to('strong-agent', target_is_directory=True)"
                    )
                else:
                    path.rename(root / "weak.json")
                    path.symlink_to("../weak.json")
                    restore = "p = Path('.agent/execution.contract.json'); p.unlink(); "
                    restore += (
                        "p.write_bytes(Path('strong.json').read_bytes())"
                        if replacement == "file" else "p.symlink_to('../strong.json')"
                    )
                _ok(root, "run", "--step", "P1", "--", PY, "-c",
                    "from pathlib import Path; " + restore)
                receipt = _records(root, ".agent/command-receipts.jsonl")[-1]
                _finish(root)

                result = self.verify(root, "risk_policy=require-confirmation")

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(".agent/command-receipts.jsonl has 1 record(s) not pinned", result.stdout)
                self.assertEqual(receipt[PIN], _digest(weak))

    def test_an_unchanged_symlinked_contract_passes(self) -> None:
        root = self.fresh()
        _start(root)
        path = root / CONTRACT
        path.rename(root / "strong.json")
        path.symlink_to("../strong.json")
        _ok(root, "claim-step", "P1", "--agent", "agent-a")
        _finish(root)

        result = self.verify(root, *RUNTIME_FLOOR)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("proof verified", result.stdout)

    def test_pins_are_judged_on_the_bytes_that_were_hashed(self) -> None:
        # A producer repins the ledgers after verify-proof hashed them.
        root = self.copy(self.raised)
        digest = _digest((root / CONTRACT).read_bytes())
        real_core = proof_module.core_sha256

        def repin_then_hash(proof: dict) -> str:
            for ledger in LEDGERS:
                _rewrite(root, ledger, [{**r, PIN: digest} for r in _records(root, ledger)])
            return real_core(proof)

        with mock.patch.object(proof_module, "core_sha256", side_effect=repin_then_hash):
            findings = proof_module.verify_proof(
                root, root / ".agent/proof-pack.json", floor=[("risk_policy", "block")]
            )

        messages = "\n".join(finding["message"] for finding in findings)
        self.assertIn(".agent/command-receipts.jsonl has 1 record(s) not pinned", messages)

    def test_an_untouched_or_missing_drift_report_fails(self) -> None:
        untouched = self.copy(self.steady)
        write_json(untouched / ".agent/drift-report.json", default_drift_report())
        _ok(untouched, "build-proof")
        missing = self.copy(self.steady)
        (missing / ".agent/drift-report.json").unlink()
        _ok(missing, "build-proof")

        for root, error in (
            (untouched, "is not pinned to the verified execution contract; "
                        "run audit-drift under it, then build-proof"),
            (missing, "is missing; run audit-drift, then build-proof"),
        ):
            with self.subTest(error=error):
                result = self.verify(root, "lease_policy=advisory")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(
                    f"policy_below_floor: .agent/drift-report.json {error}", result.stdout
                )

    def test_a_ledger_missing_from_the_proof_fails(self) -> None:
        # Deleting a ledger of weak-pinned records before build-proof must not
        # drop them from the check. build-proof only counts verification runs.
        root = self.copy(self.raised)
        (root / LEDGERS[3]).unlink()
        _ok(root, "build-proof")

        result = self.verify(root, "risk_policy=block")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            f"policy_below_floor: {LEDGERS[3]} is missing from the proof", result.stdout
        )

    def test_a_ledger_listed_under_another_spelling_counts_as_missing(self) -> None:
        root = self.copy(self.steady)
        proof_path = root / ".agent/proof-pack.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        respelled = "./" + LEDGERS[0]
        proof["generated_from"] = [respelled if p == LEDGERS[0] else p for p in proof["generated_from"]]
        for item in proof["files"]:
            if item["path"] == LEDGERS[0]:
                item["path"] = respelled
        proof["core_sha256"] = core_sha256(proof)
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")

        result = self.verify(root, "lease_policy=advisory")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            f"policy_below_floor: {LEDGERS[0]} is missing from the proof", result.stdout
        )

    def test_records_without_a_pin_fail(self) -> None:
        # Records written before #117, or stripped of their pin, cannot show
        # which contract they ran under.
        root = self.copy(self.steady)
        records = _records(root, LEDGERS[0])
        _rewrite(root, LEDGERS[0], [{k: v for k, v in r.items() if k != PIN} for r in records])
        _ok(root, "build-proof")

        result = self.verify(root, "lease_policy=advisory")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            f".agent/step-runs.jsonl has {len(records)} record(s) not pinned to the "
            f"verified execution contract (0 under another contract, {len(records)} "
            "unpinned; first at line 1)",
            result.stdout,
        )

    def test_redoing_the_step_in_place_does_not_recover(self) -> None:
        # Amending keeps the weak attempt's records in the ledgers.
        root = self.copy(self.raised)
        _ok(root, "amend-step", "P1", "--agent", "agent-a", "--reason", "redo under block",
            "--reason-code", "operator_correction")
        _finish(root)

        result = self.verify(root, "risk_policy=block")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(".agent/command-receipts.jsonl has 1 record(s) not pinned", result.stdout)


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


class VerifiedSourceReadTests(unittest.TestCase):
    """#126: every check after the hash check judges the bytes that were hashed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "fixture"
        cls.root.mkdir()
        build_fixture(cls.root, edit=None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def copy(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return Path(shutil.copytree(self.root, Path(tmp) / "fixture"))

    def verify(self, root: Path, swap=None) -> list:
        """verify-proof --replay, running ``swap`` right after the hash check."""
        real_core = proof_module.core_sha256

        def swap_then_hash(proof: dict) -> str:
            if swap is not None:
                swap()
            return real_core(proof)

        with mock.patch.object(proof_module, "core_sha256", side_effect=swap_then_hash):
            return proof_module.verify_proof(root, root / ".agent/proof-pack.json", replay=True)

    def respell(self, root: Path, source: str, spelling: str) -> None:
        proof_path = root / ".agent/proof-pack.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["generated_from"] = [
            spelling if path == source else path for path in proof["generated_from"]
        ]
        for item in proof["files"]:
            if item["path"] == source:
                item["path"] = spelling
        proof["core_sha256"] = core_sha256(proof)
        write_json(proof_path, proof)

    def test_untouched_bundle_still_verifies(self) -> None:
        findings = self.verify(self.copy())

        self.assertEqual([f for f in findings if f["severity"] == "error"], [])

    def test_an_unhashed_symlink_loop_does_not_affect_verification(self) -> None:
        root = self.copy()
        baseline = self.verify(root)
        (root / ".agent/runtime.config.json").symlink_to("runtime.config.json")

        self.assertEqual(self.verify(root), baseline)

    def test_swapping_any_hashed_source_after_its_hash_check_changes_nothing(self) -> None:
        baseline = self.verify(self.copy())
        proof = json.loads((self.root / ".agent/proof-pack.json").read_text(encoding="utf-8"))
        swaps = {
            "rewritten": lambda path: path.write_bytes(b"{not json\n"),
            "deleted": Path.unlink,
        }
        for item in proof["files"]:
            for name, swap in swaps.items():
                with self.subTest(path=item["path"], swap=name):
                    root = self.copy()
                    self.assertEqual(self.verify(root, lambda: swap(root / item["path"])), baseline)

    def test_receipt_outputs_are_judged_against_the_hashed_ledger(self) -> None:
        # The issue's example: forge a receipt output, then swap in a ledger that
        # vouches for the forgery once verify-proof has hashed the real one.
        for alias in ("canonical", "parent", "symlink", "hardlink", "reader_symlink"):
            with self.subTest(alias=alias):
                root = self.copy()
                source = ".agent/command-receipts.jsonl"
                ledger = root / source
                spelling = source
                if alias == "parent":
                    spelling = ".agent/../.agent/command-receipts.jsonl"
                elif alias == "symlink":
                    spelling = "ledger-link.jsonl"
                    (root / spelling).symlink_to(source)
                elif alias == "hardlink":
                    spelling = "ledger-hardlink.jsonl"
                    (root / spelling).hardlink_to(ledger)
                elif alias == "reader_symlink":
                    spelling = "original-ledger.jsonl"
                    ledger.rename(root / spelling)
                    ledger.symlink_to("../" + spelling)
                proof_path = root / ".agent/proof-pack.json"
                self.respell(root, source, spelling)
                sealed = proof_path.read_bytes()
                self.assertEqual(
                    [f for f in self.verify(root) if f["severity"] == "error"], []
                )
                receipts = read_jsonl(ledger)
                stdout_path = receipts[0]["stdout_path"]
                (root / stdout_path).write_bytes(b"forged\n")
                receipts[0]["stdout_sha256"] = hashlib.sha256(b"forged\n").hexdigest()
                forged = "".join(json.dumps(receipt, sort_keys=True) + "\n" for receipt in receipts)

                def swap() -> None:
                    if alias == "reader_symlink":
                        (root / "forged-ledger.jsonl").write_text(forged, encoding="utf-8")
                        ledger.unlink()
                        ledger.symlink_to("../forged-ledger.jsonl")
                    else:
                        ledger.write_text(forged, encoding="utf-8")

                findings = self.verify(root, swap)

                self.assertIn(
                    f"receipt output hash mismatch for {stdout_path}",
                    [finding["message"] for finding in findings],
                )
                self.assertEqual(proof_path.read_bytes(), sealed)

    def test_symlink_parent_path_does_not_shadow_a_different_source(self) -> None:
        root = self.copy()
        (root / "other/child").mkdir(parents=True)
        (root / ".agent/link").symlink_to("../other/child", target_is_directory=True)
        spelling = ".agent/link/../command-receipts.jsonl"
        (root / "other/command-receipts.jsonl").write_bytes(b"different source\n")
        proof_path = root / ".agent/proof-pack.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["generated_from"].append(spelling)
        proof["files"].append({"path": spelling, "sha256": _digest(b"different source\n")})
        proof["core_sha256"] = core_sha256(proof)
        write_json(proof_path, proof)

        self.assertEqual([f for f in self.verify(root) if f["severity"] == "error"], [])

    def test_replacing_a_reader_during_hashing_cannot_drop_its_alias(self) -> None:
        root = self.copy()
        source = ".agent/command-receipts.jsonl"
        spelling = ".agent/../.agent/command-receipts.jsonl"
        ledger = root / source
        self.respell(root, source, spelling)
        receipts = read_jsonl(ledger)
        stdout_path = receipts[0]["stdout_path"]
        (root / stdout_path).write_bytes(b"forged\n")
        receipts[0]["stdout_sha256"] = _digest(b"forged\n")
        replacement = root / "replacement.jsonl"
        replacement.write_text("".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
        real_open = Path.open
        resolved_ledger = ledger.resolve()

        def replace_after_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path == resolved_ledger and args == ("rb",) and replacement.exists():
                replacement.replace(ledger)
            return handle

        with mock.patch.object(Path, "open", replace_after_open):
            findings = self.verify(root)

        self.assertFalse(replacement.exists())
        self.assertIn(
            f"receipt output hash mismatch for {stdout_path}",
            [f["message"] for f in findings],
        )

    def test_alias_binding_survives_directory_replacement_during_hashing(self) -> None:
        root = self.copy()
        source = ".agent/command-receipts.jsonl"
        self.respell(root, source, ".agent/../.agent/command-receipts.jsonl")
        ledger = root / source
        receipts = read_jsonl(ledger)
        stdout_path = receipts[0]["stdout_path"]
        (root / stdout_path).write_bytes(b"forged\n")
        receipts[0]["stdout_sha256"] = _digest(b"forged\n")
        forged = "".join(json.dumps(r) + "\n" for r in receipts)
        real_sha256 = hashlib.sha256
        replaced = False

        def replace_before_ledger_hash(data=b""):
            nonlocal replaced
            digest = real_sha256(data)
            if not replaced and data == b"":
                replaced = True
                (root / ".agent").rename(root / "old-agent")
                shutil.copytree(root / "old-agent", root / "new-agent")
                (root / ".agent").symlink_to("new-agent", target_is_directory=True)
            return digest

        with mock.patch.object(proof_module.hashlib, "sha256", side_effect=replace_before_ledger_hash):
            findings = self.verify(root, lambda: ledger.write_text(forged, encoding="utf-8"))

        self.assertTrue(replaced)
        self.assertIn(
            f"receipt output hash mismatch for {stdout_path}",
            [f["message"] for f in findings],
        )

    def test_conflicting_alias_snapshots_are_rejected(self) -> None:
        root = self.copy()
        ledger = root / ".agent/command-receipts.jsonl"
        original = ledger.read_bytes()
        changed = original + b"\n"
        spelling = ".agent/../.agent/command-receipts.jsonl"
        proof_path = root / ".agent/proof-pack.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["generated_from"].append(spelling)
        proof["files"].append({"path": spelling, "sha256": _digest(changed)})
        proof["core_sha256"] = core_sha256(proof)
        write_json(proof_path, proof)
        real_sha256 = hashlib.sha256

        def swap_after_first_hash(data=b""):
            digest = real_sha256(data)
            if data == original:
                ledger.write_bytes(changed)
            return digest

        with mock.patch.object(proof_module.hashlib, "sha256", side_effect=swap_after_first_hash):
            findings = self.verify(root)

        self.assertTrue(any(
            f["severity"] == "error" and "conflicting proof sources" in f["message"]
            for f in findings
        ), findings)

    def test_a_source_that_fails_its_hash_is_not_reread(self) -> None:
        root = self.copy()
        (root / ".agent/command-receipts.jsonl").write_bytes(b"{not json\n")

        messages = [finding["message"] for finding in self.verify(root)]

        self.assertIn("hash mismatch for .agent/command-receipts.jsonl", messages)
        self.assertEqual([m for m in messages if "invalid JSONL" in m], [])

    def test_contract_read_before_the_hash_check_does_not_decide(self) -> None:
        # The CLI reads the contract once before any work (#117). If it changed
        # between that read and the hash check, the hashed bytes still decide.
        root = self.copy()
        baseline = self.verify(root)
        honest = (root / CONTRACT).read_bytes()
        with execution_contract_snapshot():
            _edit_contract(
                root, lambda c: c["proof_policy"].update(review_gate="block", require_review_run=True)
            )
            execution_contract_sha256(root)
            (root / CONTRACT).write_bytes(honest)

            self.assertEqual(self.verify(root), baseline)


if __name__ == "__main__":
    unittest.main()
