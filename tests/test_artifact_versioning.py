from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentflow import artifacts
from agentflow.artifacts import read_json, read_jsonl, write_json
from agentflow.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    STEP_RUNS_SCHEMA_VERSION,
)
from agentflow.proof import verify_proof


ROOT = Path(__file__).resolve().parents[1]


def _next_major(version: str) -> str:
    """The next-major literal must track the constant: hardcoding "1.0.0"
    would invert these rejection tests on the day the schema freezes at 1.0.0."""
    return f"{int(version.split('.')[0]) + 1}.0.0"


class ArtifactReaderVersionTests(unittest.TestCase):
    def test_version_gated_json_rejects_missing_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/execution.contract.json"
            write_json(path, {"contract_type": "agentflow_execution_contract"})

            with self.assertRaisesRegex(ValueError, "schema_version must be"):
                read_json(path)

    def test_version_gated_jsonl_rejects_missing_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/step-runs.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text('{"event":"claimed"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema_version must be"):
                read_jsonl(path)

    def test_execution_contract_reader_enforces_exact_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/execution.contract.json"
            write_json(path, {"schema_version": "0.2.0"})

            with self.assertRaisesRegex(ValueError, "execution-contract.*incompatible"):
                read_json(path)

    def test_contract_policy_degrades_on_incompatible_contract(self) -> None:
        # Mirrors execution._concurrency: an incompatible contract must not
        # crash the receipt-recording path; it degrades to advisory defaults.
        from agentflow.receipts import _contract_policy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".agent/execution.contract.json"
            write_json(path, {"schema_version": "0.2.0", "command_policy": {"receipt_store": "by_attempt"}})

            self.assertEqual(_contract_policy(root), {})

    def test_try_read_json_reports_version_rejection_not_malformed_json(self) -> None:
        # The file is well-formed JSON; calling a version rejection "malformed
        # JSON" would send users chasing a parse error that does not exist.
        from agentflow.artifacts import try_read_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/execution.contract.json"
            write_json(path, {"schema_version": "0.2.0"})

            data, error = try_read_json(path)

            self.assertIsNone(data)
            self.assertNotIn("malformed JSON", error or "")
            self.assertIn("incompatible", error or "")

    def test_plan_reader_rejects_newer_major(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/plan.lock.json"
            write_json(path, {"schema_version": _next_major(PLAN_SCHEMA_VERSION)})

            with self.assertRaisesRegex(ValueError, "plan-lock.*incompatible"):
                read_json(path)

    def test_step_ledger_reader_accepts_older_minor_in_same_major(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/step-runs.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text('{"schema_version":"0.4.0"}\n', encoding="utf-8")

            self.assertEqual(read_jsonl(path), [{"schema_version": "0.4.0"}])

    def test_step_ledger_reader_rejects_newer_major(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/step-runs.jsonl"
            path.parent.mkdir(parents=True)
            newer = _next_major(STEP_RUNS_SCHEMA_VERSION)
            path.write_text(f'{{"schema_version":"{newer}"}}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "incompatible"):
                read_jsonl(path)

    def test_auxiliary_ledger_reader_enforces_its_declared_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/evidence.jsonl"
            path.parent.mkdir(parents=True)
            newer = _next_major(EVIDENCE_SCHEMA_VERSION)
            path.write_text(f'{{"schema_version":"{newer}"}}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "evidence.*incompatible"):
                read_jsonl(path)

    def test_historical_proof_verification_bypasses_future_working_state_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fixture"
            shutil.copytree(
                ROOT / "tests/fixtures/compatibility/released-v0.4.0", root
            )
            artifact_versions = {
                name: "1.0.0" for name in artifacts.ARTIFACT_SCHEMA_VERSIONS
            }
            execution_versions = {
                name: "1.0.0"
                for name in artifacts.EXECUTION_ARTIFACT_SCHEMA_VERSIONS
            }

            with patch("agentflow.proof.PROOF_PACK_SCHEMA_VERSION", "1.0.0"):
                with patch.dict(
                    artifacts.ARTIFACT_SCHEMA_VERSIONS, artifact_versions
                ):
                    with patch.dict(
                        artifacts.EXECUTION_ARTIFACT_SCHEMA_VERSIONS,
                        execution_versions,
                    ):
                        findings = verify_proof(
                            root, root / ".agent/proof-pack.json"
                        )

            self.assertEqual(
                [finding for finding in findings if finding["severity"] == "error"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
