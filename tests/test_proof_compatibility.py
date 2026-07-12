from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentflow.proof import core_sha256


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


def mutated_current_fixture(tmp: str, schema_version: str, drop_meta: bool = False) -> Path:
    root = Path(tmp) / "fixture"
    shutil.copytree(FIXTURES / "current-full", root)
    proof_path = root / ".agent/proof-pack.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["schema_version"] = schema_version
    if drop_meta:
        del proof["meta"]
    proof["core_sha256"] = core_sha256(proof)
    proof_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


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
        manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))

        self.assertIn(RELEASE_SHA256, provenance)
        self.assertIn("releases/download/v0.4.0/agentflow.pyz", provenance)
        self.assertEqual(manifest["release_asset"]["sha256"], RELEASE_SHA256)
        self.assertEqual(
            manifest["release_asset"]["url"],
            "https://github.com/kstruzzieri/agentflow/releases/download/v0.4.0/agentflow.pyz",
        )
        for path, expected in manifest["artifacts"].items():
            actual = hashlib.sha256((root / path).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, path)
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

    def test_newer_schema_rejection_is_upgrade_not_tamper_or_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_fixture(mutated_current_fixture(tmp, "0.10.0", drop_meta=True))

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newer schema", output)
        self.assertIn("upgrade Agentflow", output)
        self.assertNotIn("tamper", output.lower())
        self.assertNotIn("missing required field meta", output)

    def test_malformed_schema_is_rejected_even_with_recomputed_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_fixture(mutated_current_fixture(tmp, "garbage"))

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MAJOR.MINOR.PATCH", output)


if __name__ == "__main__":
    unittest.main()
