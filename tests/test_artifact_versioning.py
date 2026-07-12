from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentflow import artifacts
from agentflow.artifacts import read_json, read_jsonl, write_json
from agentflow.proof import verify_proof


ROOT = Path(__file__).resolve().parents[1]


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

    def test_plan_reader_rejects_newer_major(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/plan.lock.json"
            write_json(path, {"schema_version": "1.0.0"})

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
            path.write_text('{"schema_version":"1.0.0"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "incompatible"):
                read_jsonl(path)

    def test_auxiliary_ledger_reader_enforces_its_declared_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/evidence.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text('{"schema_version":"1.0.0"}\n', encoding="utf-8")

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
