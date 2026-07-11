"""Smoke tests for the zipapp build script (scripts/build_zipapp.py)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from agentflow import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_zipapp.py"
ISOLATED_PYTHON = [sys.executable, "-I"]

# The artifacts refuse to run below the supported floor (that refusal is
# itself under test elsewhere), so execution smoke tests need a >= 3.11
# interpreter; the CI matrix (3.11-3.13) always runs them.
RUNS_PYZ = sys.version_info >= (3, 11)

# Execution subprocesses must prove the archive stands alone: no PYTHONPATH
# fallback to the checkout, and a cwd outside the repo.
PYZ_ENV = os.environ.copy()
PYZ_ENV.pop("PYTHONPATH", None)


class BuildZipappTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls._tmp.name)
        build = subprocess.run(
            [*ISOLATED_PYTHON, str(BUILD_SCRIPT), "--output-dir", str(cls.output_dir)],
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            raise AssertionError(f"build failed: {build.stderr}")
        cls.cli_pyz = cls.output_dir / "agentflow.pyz"
        cls.mcp_pyz = cls.output_dir / "agentflow-mcp.pyz"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_build_produces_both_artifacts(self):
        self.assertTrue(self.cli_pyz.is_file())
        self.assertTrue(self.mcp_pyz.is_file())

    @unittest.skipUnless(RUNS_PYZ, "pyz execution requires Python 3.11+")
    def test_cli_pyz_reports_version(self):
        result = subprocess.run(
            [*ISOLATED_PYTHON, str(self.cli_pyz), "--version"],
            capture_output=True,
            text=True,
            env=PYZ_ENV,
            cwd=self.output_dir,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"agentflow {__version__}")

    @unittest.skipUnless(RUNS_PYZ, "pyz execution requires Python 3.11+")
    def test_cli_pyz_help_exits_zero(self):
        result = subprocess.run(
            [*ISOLATED_PYTHON, str(self.cli_pyz), "--help"],
            capture_output=True,
            text=True,
            env=PYZ_ENV,
            cwd=self.output_dir,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(RUNS_PYZ, "pyz execution requires Python 3.11+")
    def test_mcp_pyz_answers_initialize(self):
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        result = subprocess.run(
            [*ISOLATED_PYTHON, str(self.mcp_pyz)],
            input=request + "\n",
            capture_output=True,
            text=True,
            timeout=60,
            env=PYZ_ENV,
            cwd=self.output_dir,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.strip().splitlines()[0])
        self.assertEqual(response["result"]["serverInfo"]["name"], "agentflow")
        self.assertEqual(response["result"]["serverInfo"]["version"], __version__)

    @unittest.skipIf(RUNS_PYZ, "the refusal is only observable below Python 3.11")
    def test_pyz_refuses_old_python(self):
        result = subprocess.run(
            [*ISOLATED_PYTHON, str(self.cli_pyz), "--version"],
            capture_output=True,
            text=True,
            env=PYZ_ENV,
            cwd=self.output_dir,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires Python 3.11", result.stderr)

    def test_archive_hygiene(self):
        for pyz in (self.cli_pyz, self.mcp_pyz):
            with zipfile.ZipFile(pyz) as archive:
                names = archive.namelist()
            self.assertIn("agentflow/cli.py", names)
            self.assertIn("agentflow/mcp_server.py", names)
            self.assertIn("__main__.py", names)
            self.assertEqual([name for name in names if "__pycache__" in name], [])
            with open(pyz, "rb") as handle:
                self.assertEqual(handle.read(2), b"#!")

    def test_main_guard_and_dispatch(self):
        targets = (
            (self.cli_pyz, "agentflow.cli"),
            (self.mcp_pyz, "agentflow.mcp_server"),
        )
        for pyz, module in targets:
            with zipfile.ZipFile(pyz) as archive:
                main_src = archive.read("__main__.py").decode("utf-8")
            self.assertIn("(3, 11)", main_src)
            self.assertIn(module, main_src)

    def test_only_rejects_unknown_target(self):
        result = subprocess.run(
            [
                *ISOLATED_PYTHON,
                str(BUILD_SCRIPT),
                "--only",
                "bogus",
                "--output-dir",
                str(self.output_dir),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
