from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentflow.artifacts import read_json, read_jsonl, write_json


class ArtifactReaderVersionTests(unittest.TestCase):
    def test_execution_contract_reader_enforces_exact_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agent/execution.contract.json"
            write_json(path, {"schema_version": "0.2.0"})

            with self.assertRaisesRegex(ValueError, "execution-contract.*incompatible"):
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


if __name__ == "__main__":
    unittest.main()
