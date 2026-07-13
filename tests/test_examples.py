from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class CiProofExampleTests(unittest.TestCase):
    def test_copyable_workflow_defines_proof_root(self) -> None:
        workflow = (REPO_ROOT / "examples/ci-proof/workflow.yml").read_text(encoding="utf-8")
        self.assertIn("PROOF_ROOT: tests/fixtures/proof-bundle", workflow)

    def test_smoke_script_default_root_uses_copy_fallback(self) -> None:
        # No-argument invocation is the README command; the fixture root is a
        # repo subdirectory, so this exercises the temp-copy fallback branch.
        result = subprocess.run(
            ["sh", "examples/ci-proof/smoke.sh"],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHON": sys.executable},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ci_proof_smoke_script_verifies_fixture_in_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proof-bundle"
            shutil.copytree(REPO_ROOT / "tests/fixtures/proof-bundle", root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=example@agentflow.invalid", "-c", "user.name=example", "commit", "-qm", "baseline"],
                cwd=root,
                check=True,
            )
            result = subprocess.run(
                ["sh", "examples/ci-proof/smoke.sh", str(root)],
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHON": sys.executable},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=600,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
