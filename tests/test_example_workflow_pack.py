from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class WorkflowPackExampleTests(unittest.TestCase):
    def test_workload_executes_draft_plan_and_proof(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "examples/workflow-pack/run.py"], cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("workflow-pack example passed", result.stdout)

    def test_readme_uses_shipped_pack_and_required_commands(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "examples/workflow-pack/README.md").read_text()
        self.assertTrue((root / "examples/packs/agentflow-draft-demo/.agentflow-pack/pack.json").is_file())
        for command in ("recommend-workflow", "draft-plan", "lock-plan", "init-execution", "verify-proof"):
            self.assertIn(command, readme)
