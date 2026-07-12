from __future__ import annotations

import unittest
from pathlib import Path


class AggregationExampleTests(unittest.TestCase):
    def test_readme_names_dry_run_and_final_proof_chain(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "examples/aggregation/README.md").read_text()
        for command in ("aggregate-ledgers", "--dry-run", "verify-run", "build-proof", "verify-proof"):
            self.assertIn(command, text)
