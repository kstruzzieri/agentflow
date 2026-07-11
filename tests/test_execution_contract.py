from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentflow.execution import (
    default_execution_contract,
    doctor,
    init_execution_artifacts,
    lease_policy,
    validate_execution_contract,
)
from agentflow.validation import validate_plan


class ExecutionContractTests(unittest.TestCase):
    def test_default_contract_is_single_writer_and_provider_neutral(self) -> None:
        contract = default_execution_contract(".")
        self.assertEqual(contract["schema_version"], "0.3.0")
        self.assertEqual(contract["contract_type"], "agentflow_execution_contract")
        self.assertEqual(contract["concurrency"]["writer_model"], "single_writer")
        self.assertIn("codex_skills", contract["agent_interface"]["forbidden_assumptions"])
        self.assertIn(".agent/", contract["concurrency"]["reconcile_ignore"])

    def test_init_execution_creates_contract_and_empty_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created, skipped = init_execution_artifacts(root, force=False)

            self.assertIn(".agent/execution.contract.json", created)
            self.assertIn(".agent/step-runs.jsonl", created)
            self.assertIn(".agent/command-receipts.jsonl", created)
            self.assertIn(".agent/file-receipts.jsonl", created)
            self.assertIn(".agent/verification-runs.jsonl", created)
            self.assertEqual(skipped, [])
            contract = json.loads(
                (root / ".agent/execution.contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["schema_version"], "0.3.0")

    def test_init_execution_does_not_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root, force=False)
            contract_path = root / ".agent/execution.contract.json"
            contract_path.write_text('{"sentinel": true}\n', encoding="utf-8")

            created, skipped = init_execution_artifacts(root, force=False)

            self.assertNotIn(".agent/execution.contract.json", created)
            self.assertIn(".agent/execution.contract.json", skipped)
            self.assertEqual(
                json.loads(contract_path.read_text(encoding="utf-8")),
                {"sentinel": True},
            )

    def test_init_execution_force_overwrites_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root, force=False)
            contract_path = root / ".agent/execution.contract.json"
            contract_path.write_text('{"sentinel": true}\n', encoding="utf-8")

            created, skipped = init_execution_artifacts(root, force=True)

            self.assertIn(".agent/execution.contract.json", created)
            self.assertEqual(skipped, [])
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(contract["contract_type"], "agentflow_execution_contract")

    def test_default_contract_requires_confirmation(self) -> None:
        contract = default_execution_contract(".")
        self.assertEqual(
            contract["command_policy"]["risk_policy"], "require-confirmation"
        )

    def test_validation_accepts_missing_risk_policy(self) -> None:
        contract = default_execution_contract(".")
        del contract["command_policy"]["risk_policy"]
        findings = validate_execution_contract(contract)
        self.assertFalse(any("risk_policy" in f["message"] for f in findings))

    def test_validation_rejects_invalid_risk_policy(self) -> None:
        contract = default_execution_contract(".")
        contract["command_policy"]["risk_policy"] = "blok"
        findings = validate_execution_contract(contract)
        self.assertTrue(
            any(f["severity"] == "error" and "risk_policy" in f["message"] for f in findings)
        )

    def test_default_contract_has_command_timeout(self) -> None:
        contract = default_execution_contract(".")

        self.assertEqual(contract["command_policy"]["command_timeout_seconds"], 600)
        self.assertEqual(validate_execution_contract(contract), [])

    def test_validation_accepts_missing_command_timeout_for_legacy_contract(self) -> None:
        contract = default_execution_contract(".")
        del contract["command_policy"]["command_timeout_seconds"]

        findings = validate_execution_contract(contract)

        self.assertFalse(
            any("command_timeout_seconds" in finding["message"] for finding in findings)
        )

    def test_validation_rejects_invalid_command_timeout_values(self) -> None:
        for value in (0, -1, True, "600"):
            with self.subTest(value=value):
                contract = default_execution_contract(".")
                contract["command_policy"]["command_timeout_seconds"] = value

                findings = validate_execution_contract(contract)

                self.assertTrue(
                    any(
                        finding["severity"] == "error"
                        and "command_timeout_seconds" in finding["message"]
                        for finding in findings
                    )
                )

    def test_plan_validation_accepts_command_gate_timeout(self) -> None:
        plan = {
            "schema_version": "0.3.0",
            "objective": "Timeout validation.",
            "scope": ["Exercise command gate timeout validation."],
            "non_goals": [],
            "invariants": ["Timeout values are positive integers."],
            "allowed_files": ["fixture.txt", ".agent/"],
            "blocked_files": [],
            "validation_gates": ["python3 -c \"print('ok')\""],
            "rollback_plan": "Delete fixture.txt.",
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
                    "action": "Run validation.",
                    "files": ["fixture.txt"],
                    "preconditions": ["Claimed."],
                    "expected_diff": ["No source change."],
                    "validation": ["python3 -c \"print('ok')\""],
                    "evidence_ids": [],
                    "gates": [
                        {
                            "kind": "command",
                            "run": ["python3", "-c", "print('ok')"],
                            "timeout_seconds": 1200,
                        }
                    ],
                }
            ],
            "evidence_ids": [],
            "locked": True,
            "locked_at": "2026-06-23T00:00:00+00:00",
        }

        self.assertEqual(validate_plan(plan), [])

    def test_plan_validation_rejects_invalid_command_gate_timeout(self) -> None:
        for value in (0, -5, False, True, "1200", None):
            with self.subTest(value=value):
                plan = {
                    "schema_version": "0.3.0",
                    "objective": "Timeout validation.",
                    "scope": ["Exercise command gate timeout validation."],
                    "non_goals": [],
                    "invariants": ["Timeout values are positive integers."],
                    "allowed_files": ["fixture.txt", ".agent/"],
                    "blocked_files": [],
                    "validation_gates": ["python3 -c \"print('ok')\""],
                    "rollback_plan": "Delete fixture.txt.",
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
                            "action": "Run validation.",
                            "files": ["fixture.txt"],
                            "preconditions": ["Claimed."],
                            "expected_diff": ["No source change."],
                            "validation": ["python3 -c \"print('ok')\""],
                            "evidence_ids": [],
                            "gates": [
                                {
                                    "kind": "command",
                                    "run": ["python3", "-c", "print('ok')"],
                                    "timeout_seconds": value,
                                }
                            ],
                        }
                    ],
                    "evidence_ids": [],
                    "locked": True,
                    "locked_at": "2026-06-23T00:00:00+00:00",
                }

                errors = validate_plan(plan)

                self.assertTrue(
                    any("timeout_seconds must be a positive integer" in error for error in errors),
                    errors,
                )

    def test_validation_rejects_multi_writer(self) -> None:
        contract = default_execution_contract(".")
        contract["concurrency"]["writer_model"] = "multi_writer"

        findings = validate_execution_contract(contract)

        self.assertTrue(any(finding["severity"] == "error" for finding in findings))
        self.assertTrue(any("multi_writer" in finding["message"] for finding in findings))

    def test_doctor_reports_missing_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            result = doctor(root)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any(
                    "execution.contract.json" in finding["message"]
                    for finding in result["findings"]
                )
            )

    @unittest.skipIf(os.name == "nt", "chmod write-bit semantics differ on Windows")
    def test_doctor_reports_unwritable_agent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root)
            agent_dir = root / ".agent"
            original_mode = agent_dir.stat().st_mode
            agent_dir.chmod(0o500)
            try:
                result = doctor(root)
            finally:
                agent_dir.chmod(original_mode)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any(".agent directory is not writable" in finding["message"] for finding in result["findings"])
            )

    def test_default_contract_enables_hunk_enforcement(self) -> None:
        contract = default_execution_contract(".")
        self.assertEqual(contract["proof_policy"]["hunk_attribution"], "enforce")
        self.assertEqual(validate_execution_contract(contract), [])

    def test_validator_rejects_unknown_hunk_policy(self) -> None:
        contract = default_execution_contract(".")
        contract["proof_policy"]["hunk_attribution"] = "bogus"
        findings = validate_execution_contract(contract)
        self.assertTrue(any("hunk_attribution" in f["message"] for f in findings))

    def test_lease_policy_enforce_is_valid(self) -> None:
        contract = default_execution_contract(".")
        contract["concurrency"]["lease_policy"] = "enforce"
        self.assertEqual(validate_execution_contract(contract), [])

    def test_lease_policy_unknown_is_rejected(self) -> None:
        contract = default_execution_contract(".")
        contract["concurrency"]["lease_policy"] = "multi"
        messages = [f["message"] for f in validate_execution_contract(contract)]
        self.assertTrue(any("lease_policy" in m for m in messages))

    def test_lease_ttl_minutes_must_be_positive(self) -> None:
        contract = default_execution_contract(".")
        contract["concurrency"]["lease_ttl_minutes"] = 0
        messages = [f["message"] for f in validate_execution_contract(contract)]
        self.assertTrue(any("lease_ttl_minutes" in m for m in messages))

    def test_lease_grace_seconds_must_be_non_negative(self) -> None:
        contract = default_execution_contract(".")
        contract["concurrency"]["lease_grace_seconds"] = -1
        messages = [f["message"] for f in validate_execution_contract(contract)]
        self.assertTrue(any("lease_grace_seconds" in m for m in messages))

    def test_absent_lease_fields_are_backward_compatible(self) -> None:
        contract = default_execution_contract(".")
        contract["concurrency"].pop("lease_policy", None)
        contract["concurrency"].pop("lease_ttl_minutes", None)
        contract["concurrency"].pop("lease_grace_seconds", None)
        self.assertEqual(validate_execution_contract(contract), [])


class ReviewPolicyDefaultTests(unittest.TestCase):
    def test_default_contract_has_review_gate(self) -> None:
        from agentflow.execution import default_execution_contract

        policy = default_execution_contract(".")["proof_policy"]
        self.assertEqual(policy["review_gate"], "warn")
        self.assertEqual(policy["require_review_run"], False)


    def test_lease_policy_reads_advisory_for_non_dict_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root)
            # A structurally-wrong contract (valid JSON list) must degrade to
            # advisory instead of raising AttributeError.
            (root / ".agent/execution.contract.json").write_text(
                json.dumps([1, 2]), encoding="utf-8"
            )
            self.assertEqual(lease_policy(root), "advisory")


if __name__ == "__main__":
    unittest.main()
