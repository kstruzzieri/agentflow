from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowPackExampleTests(unittest.TestCase):
    def test_readme_uses_shipped_pack_and_required_commands(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "examples/workflow-pack/README.md").read_text()
        self.assertTrue((root / "examples/packs/agentflow-draft-demo/.agentflow-pack/pack.json").is_file())
        for command in ("recommend-workflow", "draft-plan", "lock-plan", "init-execution", "verify-proof"):
            self.assertIn(command, readme)
