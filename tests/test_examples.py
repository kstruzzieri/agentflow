from __future__ import annotations

import os
import re
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

    def test_ci_proof_gate_guide_keeps_the_gate_contract(self) -> None:
        text = (REPO_ROOT / "docs/ci-proof-gate.md").read_text(encoding="utf-8")
        for required in (
            "verify-run --no-record",
            "--require hunk_attribution=enforce",
            "--require review_gate=block",
            "policy_below_floor",
            "AGENTFLOW_PROOF_ROOT",
            "AGENTFLOW_PROOF_BASE_REF",
            "security-model.md",
            "../examples/ci-proof",
        ):
            self.assertIn(required, text)
        example = (REPO_ROOT / "examples/ci-proof/README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("docs/ci-proof-gate.md", example)


@unittest.skipIf(
    os.name == "nt" or shutil.which("git") is None,
    "the quickstart needs a POSIX shell and git",
)
class QuickstartGuideTests(unittest.TestCase):
    def test_quickstart_runs_verbatim_to_a_verified_proof(self) -> None:
        text = (REPO_ROOT / "docs/quickstart.md").read_text(encoding="utf-8")
        self.assertIn("POSIX", text)
        # Every bash block, in page order, is the script a reader pastes.
        script = "\n".join(re.findall(r"```bash\n(.*?)```", text, flags=re.S))
        # The page is executed below: it must never install from the network.
        self.assertNotIn("pip", script)
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp, "bin")
            shim.mkdir()
            command = shim / "agentflow"
            command.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" -m agentflow "$@"\n',
                encoding="utf-8",
            )
            command.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{shim}{os.pathsep}{os.environ['PATH']}",
                PYTHONPATH=str(REPO_ROOT / "src"),
                # A fresh machine: no Git identity and no personal Git settings.
                GIT_CONFIG_GLOBAL=os.devnull,
                GIT_CONFIG_NOSYSTEM="1",
                # Stop Git guessing an identity from the login and host name.
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="user.useConfigOnly",
                GIT_CONFIG_VALUE_0="true",
            )
            for name in (
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
                "EMAIL",
            ):
                env.pop(name, None)
            work = Path(tmp, "work")
            work.mkdir()
            result = subprocess.run(
                ["sh", "-e", "-c", script], cwd=work, env=env,
                stdin=subprocess.DEVNULL, timeout=120,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout)
        samples = "\n".join(re.findall(r"```text\n(.*?)```", text, flags=re.S))
        self.assertIn("verify-proof ok", samples)
        self.assertIn("proof verified", samples)
        for line in samples.splitlines():
            self.assertIn(line, result.stdout)


if __name__ == "__main__":
    unittest.main()
