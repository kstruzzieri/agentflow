from __future__ import annotations

import json
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
    def test_copyable_workflow_checks_source_edits(self) -> None:
        # #139: the copyable job compares the pull request with its base commit.
        workflow = (REPO_ROOT / "examples/ci-proof/workflow.yml").read_text(encoding="utf-8")
        self.assertIn("PROOF_ROOT: .", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("--base-ref", workflow)

    def test_copyable_workflow_checks_ledgers_only_on_push(self) -> None:
        # After a squash or rebase merge the recorded baseline may be gone.
        workflow = (REPO_ROOT / "examples/ci-proof/workflow.yml").read_text(encoding="utf-8")
        self.assertIn("github.event.pull_request.base.sha || 'HEAD'", workflow)

    def test_smoke_script_checks_a_copied_anchored_bundle(self) -> None:
        # #139: a bundle whose run recorded a baseline is copied into a fresh
        # repository that lacks that commit; the copy compares with its one commit.
        gate = "python3 -c \"print('ok')\""
        with tempfile.TemporaryDirectory() as tmp:
            work, outer = Path(tmp) / "work", Path(tmp) / "outer"
            work.mkdir()
            outer.mkdir()
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}

            def git(cwd: Path, *args: str) -> None:
                subprocess.run(
                    ["git", "-c", "user.name=T", "-c", "user.email=t@e.invalid",
                     "-c", "commit.gpgsign=false", *args],
                    cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            def af(*args: str) -> None:
                result = subprocess.run(
                    [sys.executable, "-m", "agentflow", *args], cwd=work, env=env,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            git(work, "init", "-q")
            (work / "seed.txt").write_text("seed\n", encoding="utf-8")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "seed")
            af("init")
            plan_path = work / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan.update({
                "objective": "o", "scope": ["notes.txt"], "invariants": ["i"],
                "allowed_files": ["notes.txt", ".agent/"], "rollback_plan": "r",
                "validation_gates": [gate],
                "steps": [{
                    "id": "P1", "action": "a", "files": ["notes.txt", ".agent/"],
                    "expected_diff": ["d"], "preconditions": [], "evidence_ids": [],
                    "validation": [gate],
                    "gates": [{"kind": "command", "run": ["python3", "-c", "print('ok')"]}],
                }],
            })
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            af("lock-plan", ".agent/plan.lock.json")
            af("init-execution")
            self.assertTrue((work / ".agent/run-anchors.jsonl").exists())
            af("claim-step", "P1", "--agent", "t")
            (work / "notes.txt").write_text("hello\n", encoding="utf-8")
            af("record-file-change", "--step", "P1", "--path", "notes.txt")
            af("run", "--step", "P1", "--gate", gate, "--", "python3", "-c", "print('ok')")
            af("verify-step", "P1")
            af("complete-step", "P1")
            af("finish-run")
            git(outer, "init", "-q")
            shutil.copytree(work, outer / "bundle", ignore=shutil.ignore_patterns(".git"))
            git(outer, "add", "-A")
            git(outer, "commit", "-qm", "bundle")

            result = subprocess.run(
                ["sh", "examples/ci-proof/smoke.sh", str(outer / "bundle")],
                cwd=REPO_ROOT,
                env={**env, "PYTHON": sys.executable},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertIn("verify-run passed", result.stdout, result.stdout + result.stderr)
            self.assertNotIn("not in this clone", result.stdout + result.stderr)

    def test_smoke_script_refuses_a_base_ref_it_cannot_honor(self) -> None:
        # The fixture root is a subdirectory, checked as a copy with no history.
        result = subprocess.run(
            ["sh", "examples/ci-proof/smoke.sh"],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHON": sys.executable,
                 "AGENTFLOW_BASE_REF": "HEAD"},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("AGENTFLOW_BASE_REF", result.stderr)

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
        # The fence info string says what each block is (the comment at the
        # top of the page says the same): `bash` runs in page order,
        # `bash expect-fail` runs and must exit non-zero, `sh` is shown but
        # never run, and `text` is output the run must print.
        blocks = re.findall(r"```([^\n`]*)\n(.*?)```", text, flags=re.S)
        self.assertLessEqual(
            {info for info, _ in blocks}, {"bash", "bash expect-fail", "sh", "text"}
        )
        parts = []
        for info, body in blocks:
            if info == "bash":
                parts.append(body)
            elif info == "bash expect-fail":
                # One command, so its own exit status is the one tested.
                self.assertEqual(len(body.strip().splitlines()), 1, body)
                parts.append(
                    f"if {body.strip()}; then\n  echo 'unexpected success'; exit 1\nfi\n"
                )
        self.assertIn("bash expect-fail", {info for info, _ in blocks})
        script = "\n".join(parts)
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
        self.assertIn("error: out-of-scope changed file: extra.txt", samples)
        for line in samples.splitlines():
            self.assertIn(line, result.stdout)


if __name__ == "__main__":
    unittest.main()
