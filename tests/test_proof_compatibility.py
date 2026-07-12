from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/compatibility"
RELEASE_SHA256 = "6617b33de632e174fffb7f3e869ab0793fff4df62c324a0cb017c9d5c5ed671c"


def verify_fixture(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agentflow", "verify-proof", "--root", str(root)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class ProofCompatibilityMatrixTests(unittest.TestCase):
    def test_promised_verify_proof_matrix(self) -> None:
        roots = {
            "preserved-legacy": ROOT / "tests/fixtures/proof-bundle",
            "released-v0.4.0": FIXTURES / "released-v0.4.0",
            "current-full": FIXTURES / "current-full",
            "current-aggregated": FIXTURES / "current-aggregated",
        }
        for name, root in roots.items():
            with self.subTest(name=name):
                result = verify_fixture(root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("proof verified", result.stdout)

    def test_released_fixture_is_checksum_pinned_and_genuine(self) -> None:
        root = FIXTURES / "released-v0.4.0"
        provenance = (root / "PROVENANCE.md").read_text(encoding="utf-8")
        proof = json.loads((root / ".agent/proof-pack.json").read_text(encoding="utf-8"))

        self.assertIn(RELEASE_SHA256, provenance)
        self.assertIn("releases/download/v0.4.0/agentflow.pyz", provenance)
        self.assertEqual(proof["meta"]["tool_version"], "0.4.0")

    def test_current_full_fixture_exercises_load_bearing_optional_blocks(self) -> None:
        root = FIXTURES / "current-full"
        proof = json.loads((root / ".agent/proof-pack.json").read_text(encoding="utf-8"))

        self.assertIn("runtime", proof)
        self.assertIn("capabilities", proof)
        self.assertIn("review", proof)
        self.assertIn("requirements", proof["coverage"])
        self.assertTrue(proof["execution"]["amendments"])
        receipts = (root / ".agent/file-receipts.jsonl").read_text(encoding="utf-8")
        self.assertIn("hunks", receipts)

    def test_aggregated_fixture_carries_namespaced_provenance(self) -> None:
        root = FIXTURES / "current-aggregated"
        proof = json.loads((root / ".agent/proof-pack.json").read_text(encoding="utf-8"))
        aggregation = json.loads((root / ".agent/aggregation.json").read_text(encoding="utf-8"))

        self.assertEqual(proof["aggregation"], aggregation)
        self.assertGreaterEqual(aggregation["source_count"], 2)
        self.assertTrue(
            all(source["namespaced_prefix"].startswith("WT") for source in aggregation["sources"])
        )


if __name__ == "__main__":
    unittest.main()
