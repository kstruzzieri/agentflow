from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentflow.proof import verify_proof

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "proof-bundle"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def commit_fixture_copy(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=ci@agentflow.invalid",
            "-c",
            "user.name=agentflow-ci",
            "commit",
            "-qm",
            "proof bundle under verification",
        ],
        cwd=str(root),
        check=True,
    )


def run_agentflow(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def workflow_step(name: str) -> str:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    marker = f"      - name: {name}"
    start = workflow.index(marker)
    next_step = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if next_step == -1 else workflow[start:next_step]


def workflow_run_script(name: str) -> str:
    step = workflow_step(name)
    lines = step.splitlines()
    run_index = lines.index("        run: |")
    body: list[str] = []
    for line in lines[run_index + 1 :]:
        if line == "":
            body.append("")
            continue
        if not line.startswith("          "):
            break
        body.append(line[10:])
    return "\n".join(body)


class CommittedProofBundleTests(unittest.TestCase):
    def test_workflow_supports_linux_and_macos_on_all_supported_pythons(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('os: ["ubuntu-latest", "macos-latest"]', workflow)
        for version in ('"3.11"', '"3.12"', '"3.13"'):
            self.assertIn(version, workflow)

    def test_fixture_proof_verifies(self) -> None:
        proof_path = FIXTURE_ROOT / ".agent" / "proof-pack.json"
        self.assertTrue(proof_path.exists(), "committed proof bundle is missing")
        findings = verify_proof(FIXTURE_ROOT, proof_path)
        errors = [f for f in findings if f.get("severity") == "error"]
        self.assertEqual(errors, [], f"committed proof bundle failed verification: {errors}")

    def test_fixture_verifies_in_staged_git_tree_without_mutating_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proof-bundle"
            shutil.copytree(FIXTURE_ROOT, root)
            commit_fixture_copy(root)

            proof_path = root / ".agent" / "proof-pack.json"
            initial_findings = verify_proof(root, proof_path)
            self.assertEqual(
                [f for f in initial_findings if f.get("severity") == "error"],
                [],
                f"committed proof bundle failed verification: {initial_findings}",
            )

            result = run_agentflow(root, "verify-run", "--no-record")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("verify-run passed", result.stdout)

            final_findings = verify_proof(root, proof_path)
            self.assertEqual(
                [f for f in final_findings if f.get("severity") == "error"],
                [],
                f"read-only run verification mutated proof inputs: {final_findings}",
            )

    def test_verify_run_fails_when_unreceipted_change_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proof-bundle"
            shutil.copytree(FIXTURE_ROOT, root)
            commit_fixture_copy(root)
            unreceipted_path = root / "src" / "unreceipted.py"
            unreceipted_path.parent.mkdir()
            unreceipted_path.write_text("print('unmapped')\n", encoding="utf-8")

            result = run_agentflow(root, "verify-run", "--no-record")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("src/unreceipted.py", result.stdout)

    def test_workflow_checks_configured_proof_through_smoke_script(self) -> None:
        # #139: --base-ref replaced the shell that rebuilt a Git baseline.
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("Stage configured proof bundle", workflow)
        step = workflow_step("Verify configured proof")
        self.assertIn("AGENTFLOW_PROOF_BASE_REF", step)
        self.assertIn("github.event.pull_request.base.sha", step)
        self.assertIn("examples/ci-proof/smoke.sh", step)

    def run_workflow_step(self, cwd: Path, **env_overrides: str) -> subprocess.CompletedProcess:
        script = workflow_run_script("Verify configured proof").replace(
            "examples/ci-proof/smoke.sh", str(REPO_ROOT / "examples" / "ci-proof" / "smoke.sh")
        )
        env = os.environ.copy()
        env.pop("AGENTFLOW_PROOF_BASE_REF", None)
        env.update({"PYTHON": sys.executable, "PR_BASE_SHA": "", **env_overrides})
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=str(cwd), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_workflow_base_ref_fails_a_committed_unreceipted_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            shutil.copytree(FIXTURE_ROOT, root)
            commit_fixture_copy(root)
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True, stdout=subprocess.PIPE, text=True,
            ).stdout.strip()
            unreceipted_path = root / "src" / "unreceipted.py"
            unreceipted_path.parent.mkdir()
            unreceipted_path.write_text("print('unmapped')\n", encoding="utf-8")
            commit_fixture_copy(root)

            passed = self.run_workflow_step(root, AGENTFLOW_PROOF_ROOT=".", PR_BASE_SHA="HEAD")
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

            # A push has no pull request base: the step checks the ledgers only.
            pushed = self.run_workflow_step(root, AGENTFLOW_PROOF_ROOT=".")
            self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)

            failed = self.run_workflow_step(root, AGENTFLOW_PROOF_ROOT=".", PR_BASE_SHA=base)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("src/unreceipted.py", failed.stdout)

            explicit = self.run_workflow_step(
                root, AGENTFLOW_PROOF_ROOT=".", PR_BASE_SHA="HEAD", AGENTFLOW_PROOF_BASE_REF=base
            )
            self.assertNotEqual(explicit.returncode, 0)

    def test_workflow_refuses_a_base_ref_for_a_subdirectory_root(self) -> None:
        result = self.run_workflow_step(
            REPO_ROOT,
            AGENTFLOW_PROOF_ROOT="tests/fixtures/proof-bundle",
            AGENTFLOW_PROOF_BASE_REF="HEAD",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGENTFLOW_PROOF_ROOT=.", result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
