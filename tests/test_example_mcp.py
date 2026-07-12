from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class McpExampleTests(unittest.TestCase):
    def test_initialize_smoke(self) -> None:
        result = subprocess.run(
            [sys.executable, "examples/mcp-clients/initialize_smoke.py"], cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["serverInfo"]["name"], "agentflow")
