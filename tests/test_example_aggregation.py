from __future__ import annotations

import unittest
import os
import subprocess
import sys
from pathlib import Path


class AggregationExampleTests(unittest.TestCase):
    def test_workload_creates_two_writers_and_verifies_canonical_proof(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "examples/aggregation/run.py"], cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("aggregation example passed", result.stdout)

    def test_readme_names_dry_run_and_final_proof_chain(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "examples/aggregation/README.md").read_text()
        for command in ("aggregate-ledgers", "--dry-run", "verify-run", "build-proof", "verify-proof"):
            self.assertIn(command, text)
