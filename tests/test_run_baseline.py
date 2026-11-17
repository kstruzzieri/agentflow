"""#139: drift checks compare against a run baseline, committed or not."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from agentflow import cli
from agentflow.artifacts import create_initial_artifacts, read_jsonl, write_json
from agentflow.contracts import PLAN_SCHEMA_VERSION, RUN_ANCHORS_SCHEMA_VERSION
from agentflow.execution import (
    amend_step,
    claim_step,
    complete_step,
    init_execution_artifacts,
    mark_step_verified,
    reclaim_step,
)
from agentflow.execution_coverage import verify_run
from agentflow.git import BaselineError, comparison_base, comparison_snapshot
from agentflow.receipts import record_command, record_file_change
from agentflow.validation import audit_drift

ANCHORS = ".agent/run-anchors.jsonl"
NOT_A_REPO = "not a git repository; Agentflow needs Git to read changed files"
STEP_RUNS_MARKED = (
    f"{ANCHORS} is missing, but step runs were recorded against a run baseline; "
    "restore it, or check with --base-ref REF"
)
LINES = "".join(f"line {i}\n" for i in range(1, 21))


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@e.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=str(root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc.stdout.strip()


def commit_all(root: Path, message: str = "work") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def plan() -> dict:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "Exercise run baselines.",
        "scope": ["Edit src/."],
        "non_goals": [],
        "invariants": ["Drift compares against the run baseline."],
        "allowed_files": ["src/", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["python3 -c \"print('ok')\""],
        "rollback_plan": "Revert.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [
            {
                "id": step_id,
                "action": f"Edit src/ ({step_id}).",
                "files": ["src/"],
                "preconditions": ["Claimed."],
                "expected_diff": ["src/ edited."],
                "validation": ["python3 -c \"print('ok')\""],
                "evidence_ids": [],
                "gates": [{"kind": "command", "run": ["python3", "-c", "print('ok')"]}],
            }
            for step_id in ("P1", "P2")
        ],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-11-14T00:00:00+00:00",
    }


def one_step_plan() -> dict:
    data = plan()
    data["steps"] = data["steps"][:1]
    return data


def seeded_repo(tmp: str, commit: bool = True) -> Path:
    """A repo with src/app.txt committed and .agent/ ignored, as most users run."""
    root = Path(tmp)
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".agent/\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src/app.txt").write_text(LINES, encoding="utf-8")
    if commit:
        commit_all(root, "seed")
    return root


def start_run(root: Path, task_plan: Optional[dict] = None) -> dict:
    task_plan = task_plan or one_step_plan()
    create_initial_artifacts(root)
    init_execution_artifacts(root)
    write_json(root / ".agent/plan.lock.json", task_plan)
    return task_plan


def finish_step(root: Path, step_id: str, attempt_id: str, paths: List[str]) -> None:
    for path in paths:
        record_file_change(root, plan(), step_id, attempt_id, path)
    record_command(root, step_id, attempt_id, ["python3", "-c", "print('ok')"], 0)
    mark_step_verified(root, step_id, attempt_id, [])
    complete_step(root, step_id, attempt_id)


def edit_line(root: Path, path: str, index: int, text: str) -> None:
    lines = (root / path).read_text(encoding="utf-8").splitlines()
    lines[index] = text
    (root / path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def completed_run(tmp: str) -> tuple:
    """One recorded edit to src/app.txt, completed and then committed."""
    root = seeded_repo(tmp)
    task_plan = start_run(root)
    claim_step(root, task_plan, "P1", "agent-a")
    edit_line(root, "src/app.txt", 1, "RECORDED")
    finish_step(root, "P1", "A1", ["src/app.txt"])
    commit_all(root, "recorded work")
    return root, task_plan


def messages(result: dict) -> List[str]:
    return [finding["message"] for finding in result["errors"]]


def run_cli(*argv: str) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def as_legacy_run(root: Path) -> None:
    """Make the run look like one Agentflow 1.7.0 or older left: claims, no run-start row.

    Those versions wrote no run_id on step events (#152), so the claims say
    nothing about the row either.
    """
    (root / ANCHORS).unlink()
    path = root / ".agent/step-runs.jsonl"
    rows = [{key: value for key, value in row.items() if key != "run_id"} for row in read_jsonl(path)]
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class AnchorLedgerTests(unittest.TestCase):
    def test_init_execution_records_head_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            head = git(root, "rev-parse", "HEAD")
            init_execution_artifacts(root)
            rows = read_jsonl(root / ANCHORS)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["schema_version"], RUN_ANCHORS_SCHEMA_VERSION)
            self.assertEqual(rows[0]["id"], "RA1")
            self.assertEqual(rows[0]["anchor"], "run_start")
            self.assertEqual(rows[0]["base_commit"], head)
            self.assertIn("recorded_at", rows[0])
            self.assertIn("execution_contract_sha256", rows[0])

    def test_init_execution_unborn_repo_records_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp, commit=False)
            init_execution_artifacts(root)
            rows = read_jsonl(root / ANCHORS)
            self.assertEqual([row["base_commit"] for row in rows], [None])

    def test_init_execution_outside_git_records_no_commit(self) -> None:
        # #138: the row is written outside Git too (it pins the plan); with no
        # repository there is still nothing to compare against.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root)
            self.assertEqual([row["base_commit"] for row in read_jsonl(root / ANCHORS)], [None])
            with comparison_snapshot():
                self.assertEqual(comparison_base(root)["source"], "head")

    def test_init_execution_rerun_keeps_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            init_execution_artifacts(root)
            before = (root / ANCHORS).read_bytes()
            (root / "src/new.txt").write_text("x\n", encoding="utf-8")
            commit_all(root)
            _created, skipped = init_execution_artifacts(root)
            self.assertIn(ANCHORS, skipped)
            self.assertEqual((root / ANCHORS).read_bytes(), before)

    def test_init_execution_force_rewrites_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            init_execution_artifacts(root)
            (root / "src/new.txt").write_text("x\n", encoding="utf-8")
            head = commit_all(root)
            init_execution_artifacts(root, force=True)
            self.assertEqual([row["base_commit"] for row in read_jsonl(root / ANCHORS)], [head])


NO_RUN = "no run started; run agentflow init-execution first; nothing was recorded"


class RunStartRequiredTests(unittest.TestCase):
    """#148: init-execution is the one place a run starts."""

    def assert_refused(self, root: Path, task_plan: dict) -> None:
        before = (root / ".agent/step-runs.jsonl").read_bytes() if (root / ".agent/step-runs.jsonl").exists() else None
        with self.assertRaises(ValueError) as caught:
            claim_step(root, task_plan, "P1", "agent-a")
        self.assertEqual(str(caught.exception), NO_RUN)
        after = (root / ".agent/step-runs.jsonl").read_bytes() if (root / ".agent/step-runs.jsonl").exists() else None
        self.assertEqual(after, before)
        self.assertFalse((root / ".agent/attempts/P1.current").exists())

    def test_claim_without_step_runs_ledger_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            create_initial_artifacts(root)
            write_json(root / ".agent/plan.lock.json", one_step_plan())
            self.assert_refused(root, one_step_plan())

    def test_claim_without_contract_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            (root / ".agent/execution.contract.json").unlink()
            self.assert_refused(root, task_plan)

    def test_claim_with_no_anchor_and_no_events_is_refused_until_init_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            (root / ANCHORS).unlink()
            self.assert_refused(root, task_plan)
            head = git(root, "rev-parse", "HEAD")
            created, _skipped = init_execution_artifacts(root)
            self.assertIn(ANCHORS, created)
            self.assertEqual([row["base_commit"] for row in read_jsonl(root / ANCHORS)], [head])
            claim_step(root, task_plan, "P1", "agent-a")
            with comparison_snapshot():
                self.assertEqual(comparison_base(root)["source"], "run-anchor")

    def test_run_with_events_and_no_anchor_keeps_head_comparison(self) -> None:
        # A run an older version started: claims already recorded, no anchor.
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root, plan())
            claim_step(root, task_plan, "P1", "agent-a")
            as_legacy_run(root)
            init_execution_artifacts(root)
            self.assertFalse((root / ANCHORS).exists())
            event = claim_step(root, task_plan, "P2", "agent-a")
            self.assertEqual(event["attempt_id"], "A2")
            self.assertNotIn("run_id", event)
            with comparison_snapshot():
                self.assertEqual(comparison_base(root)["source"], "head")

    def test_outside_git_claims_after_init_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_plan = start_run(root)
            self.assertEqual(claim_step(root, task_plan, "P1", "agent-a")["attempt_id"], "A1")

    def test_outside_git_no_anchor_and_no_events_is_refused(self) -> None:
        # Since #138 the run-start row pins the plan outside Git too, so a run
        # without one is not exempt.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_plan = start_run(root)
            (root / ANCHORS).unlink()
            self.assert_refused(root, task_plan)
            init_execution_artifacts(root)
            self.assertEqual([row["base_commit"] for row in read_jsonl(root / ANCHORS)], [None])
            claim_step(root, task_plan, "P1", "agent-a")

    def test_cli_claim_without_init_execution_is_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            create_initial_artifacts(root)
            write_json(root / ".agent/plan.lock.json", one_step_plan())
            code, out, err = run_cli("claim-step", "P1", "--agent", "agent-a", "--root", str(root))
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertEqual(err, NO_RUN + "\n")
            self.assertFalse((root / ".agent/step-runs.jsonl").exists())

    def test_cli_changed_file_commands_outside_git_are_one_line(self) -> None:
        # #140 item 2: these read changed files from Git and used to end in a traceback.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            (root / "src").mkdir()
            (root / "src/app.txt").write_text("hello\n", encoding="utf-8")
            for argv in (
                ["record-file-change", "--step", "P1", "--path", "src/app.txt"],
                ["verify-run"],
                ["finish-run"],
            ):
                with self.subTest(command=argv[0]):
                    code, out, err = run_cli(*argv, "--root", str(root))
                    self.assertEqual(code, 1)
                    self.assertIn(NOT_A_REPO, out + err)
                    self.assertNotIn("Traceback", out + err)


class CommittedDriftTests(unittest.TestCase):
    def test_committed_recorded_work_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            drift = audit_drift(root, task_plan)
            self.assertEqual(drift["status"], "pass", drift)
            self.assertIn("src/app.txt", drift["changed_files"])
            result = verify_run(root, task_plan, record=False)
            self.assertEqual(result["status"], "passed", result)

    def test_committed_unrecorded_file_fails_verify_run_and_audit_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            (root / "stray.txt").write_text("unrecorded\n", encoding="utf-8")
            (root / "src/extra.txt").write_text("unrecorded\n", encoding="utf-8")
            commit_all(root, "sneak")
            drift = audit_drift(root, task_plan)
            self.assertEqual(drift["status"], "fail")
            self.assertIn("stray.txt", drift["out_of_scope_files"])
            result = verify_run(root, task_plan, record=False)
            self.assertEqual(result["status"], "failed")
            self.assertIn("changed file is not mapped to a step receipt: src/extra.txt", messages(result))
            self.assertIn("out-of-scope changed file: stray.txt", messages(result))

    def test_committed_unrecorded_hunk_in_recorded_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            edit_line(root, "src/app.txt", 15, "SNEAKED")
            # Re-record the whole-file hash only would be a lie; leave the receipt as is
            # and commit: the after_sha256 and the hunk both no longer match.
            commit_all(root, "sneak")
            drift = audit_drift(root, task_plan)
            self.assertEqual(drift["status"], "fail")
            self.assertEqual([hunk["path"] for hunk in drift["unmapped_hunks"]], ["src/app.txt"])
            self.assertEqual(verify_run(root, task_plan, record=False)["status"], "failed")

    def test_index_only_changes_fail_drift_checks(self) -> None:
        for base_ref in (None, "HEAD"):
            for kind in ("added", "modified", "deleted"):
                with self.subTest(base_ref=base_ref, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    root = seeded_repo(tmp)
                    path = root / "stray.txt"
                    if kind != "added":
                        path.write_text("seed\n", encoding="utf-8")
                        commit_all(root, "seed stray")
                    task_plan = start_run(root)
                    claim_step(root, task_plan, "P1", "agent-a")
                    edit_line(root, "src/app.txt", 1, "RECORDED")
                    finish_step(root, "P1", "A1", ["src/app.txt"])
                    commit_all(root, "recorded work")

                    if kind == "deleted":
                        git(root, "rm", "stray.txt")
                    else:
                        path.write_text("staged only\n", encoding="utf-8")
                        git(root, "add", "stray.txt")
                    if kind == "added":
                        path.unlink()
                    else:
                        path.write_text("seed\n", encoding="utf-8")

                    with comparison_snapshot(base_ref=base_ref):
                        drift = audit_drift(root, task_plan)
                        result = verify_run(root, task_plan, record=False)
                    self.assertEqual(drift["status"], "fail", drift)
                    self.assertIn("stray.txt", drift["out_of_scope_files"])
                    self.assertEqual(result["status"], "failed", result)
                    self.assertIn("changed file is not mapped to a step receipt: stray.txt", messages(result))

    def test_two_step_adjacent_committed_edits_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root, plan())
            claim_step(root, task_plan, "P1", "agent-a")
            edit_line(root, "src/app.txt", 4, "STEP ONE")
            finish_step(root, "P1", "A1", ["src/app.txt"])
            commit_all(root, "step one")
            claim_step(root, task_plan, "P2", "agent-a")
            edit_line(root, "src/app.txt", 5, "STEP TWO")
            finish_step(root, "P2", "A2", ["src/app.txt"])
            self.assertEqual(audit_drift(root, task_plan)["status"], "pass")
            self.assertEqual(verify_run(root, task_plan, record=False)["status"], "passed")
            commit_all(root, "step two")
            self.assertEqual(audit_drift(root, task_plan)["status"], "pass")
            self.assertEqual(verify_run(root, task_plan, record=False)["status"], "passed")

    def test_receipt_records_hunk_base_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            head = git(root, "rev-parse", "HEAD")
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            edit_line(root, "src/app.txt", 1, "RECORDED")
            receipt = record_file_change(root, task_plan, "P1", "A1", "src/app.txt")
            self.assertEqual(receipt["hunk_base_commit"], head)

    def test_unborn_repo_run_compares_against_empty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp, commit=False)
            (root / ".gitignore").unlink()  # out of scope; .agent/ is in scope and reconcile-ignored
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            finish_step(root, "P1", "A1", ["src/app.txt"])
            commit_all(root, "first")
            self.assertEqual(verify_run(root, task_plan, record=False)["status"], "passed")
            (root / "src/extra.txt").write_text("unrecorded\n", encoding="utf-8")
            commit_all(root, "sneak")
            self.assertIn(
                "changed file is not mapped to a step receipt: src/extra.txt",
                messages(verify_run(root, task_plan, record=False)),
            )

    def test_committed_move_needs_both_paths_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            git(root, "mv", "src/app.txt", "src/moved.txt")
            commit_all(root, "move")
            record_file_change(root, task_plan, "P1", "A1", "src/moved.txt")
            self.assertIn(
                "changed file is not mapped to a step receipt: src/app.txt",
                messages(verify_run(root, task_plan, record=False)),
            )
            receipt = record_file_change(root, task_plan, "P1", "A1", "src/app.txt")
            self.assertEqual(receipt["change_kind"], "deleted")
            record_command(root, "P1", "A1", ["python3", "-c", "print('ok')"], 0)
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            self.assertEqual(verify_run(root, task_plan, record=False)["status"], "passed")

    def test_legacy_run_without_anchor_keeps_status_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            as_legacy_run(root)  # a run started by 1.6 or older: claims, no anchor
            edit_line(root, "src/app.txt", 1, "RECORDED")
            finish_step(root, "P1", "A1", ["src/app.txt"])
            commit_all(root, "recorded work")
            (root / "stray.txt").write_text("unrecorded\n", encoding="utf-8")
            commit_all(root, "sneak")
            drift = audit_drift(root, task_plan)
            self.assertEqual(drift["status"], "pass")
            self.assertEqual(drift["changed_files"], [])
            self.assertIn("No changed files detected by git status.", drift["notes"])
            self.assertEqual(drift["baseline"], {"source": "head", "commit": None})
            self.assertEqual(verify_run(root, task_plan, record=False)["status"], "passed")


class FailClosedTests(unittest.TestCase):
    def assert_fails_closed(self, root: Path, task_plan: dict, needle: str) -> None:
        drift = audit_drift(root, task_plan)
        self.assertEqual(drift["status"], "fail")
        self.assertTrue(any(needle in note for note in drift["notes"]), drift["notes"])
        self.assertIn("error", drift["baseline"])
        result = verify_run(root, task_plan, record=False)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(needle in m for m in messages(result)), messages(result))

    def test_unreachable_anchor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            anchor = read_jsonl(root / ANCHORS)[0]["base_commit"]
            git(root, "reset", "-q", "--hard", "HEAD~1")
            git(root, "commit", "-q", "--amend", "-m", "rewritten seed")
            self.assert_fails_closed(root, task_plan, anchor[:12])
            self.assert_fails_closed(root, task_plan, "--base-ref")

    def test_missing_anchor_with_anchored_receipts_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            (root / ANCHORS).unlink()
            self.assert_fails_closed(root, task_plan, "run-anchors.jsonl")

    def test_row_deleted_after_claim_fails_closed(self) -> None:
        # #152: no file receipt names a baseline yet, but the claim names the run.
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            base = git(root, "rev-parse", "HEAD")
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            (root / ANCHORS).unlink()
            edit_line(root, "src/app.txt", 1, "EDITED")
            (root / "outside.txt").write_text("unrecorded\n", encoding="utf-8")
            commit_all(root, "sneak")
            self.assert_fails_closed(root, task_plan, STEP_RUNS_MARKED)
            with self.assertRaises(BaselineError) as caught:
                record_file_change(root, task_plan, "P1", "A1", "src/app.txt")
            self.assertIn(STEP_RUNS_MARKED, str(caught.exception))
            with comparison_snapshot(base_ref=base):
                drift = audit_drift(root, task_plan)
                result = verify_run(root, task_plan, record=False)
            self.assertEqual(drift["baseline"], {"source": "base-ref", "commit": base, "ref": base})
            self.assertIn("outside.txt", drift["out_of_scope_files"])
            self.assertIn("out-of-scope changed file: outside.txt", messages(result))
            for argv in (["verify-run", "--no-record"], ["audit-drift"], ["finish-run"], ["build-proof"]):
                with self.subTest(command=argv[0]):
                    code, out, err = run_cli(*argv, "--root", str(root))
                    self.assertNotEqual(code, 0)
                    self.assertIn(STEP_RUNS_MARKED, out + err)
                    self.assertNotIn("Traceback", out + err)

    def test_malformed_or_multi_row_anchor_fails_closed(self) -> None:
        for content in ("", "not json\n", None):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                root, task_plan = completed_run(tmp)
                path = root / ANCHORS
                if content is None:
                    path.write_text(path.read_text(encoding="utf-8") * 2, encoding="utf-8")
                else:
                    path.write_text(content, encoding="utf-8")
                self.assert_fails_closed(root, task_plan, "run-anchors.jsonl")

    def test_baseline_error_is_one_clean_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _task_plan = completed_run(tmp)
            (root / ANCHORS).write_text("not json\n", encoding="utf-8")
            for command in ("audit-drift", "verify-run"):
                with self.subTest(command=command):
                    code, out, err = run_cli(command, "--root", str(root), "--no-record") if command == "verify-run" \
                        else run_cli(command, "--root", str(root))
                    self.assertEqual(code, 1)
                    self.assertNotIn("Traceback", out + err)
                    self.assertIn("run-anchors.jsonl", out + err)


class RunMarkerTests(unittest.TestCase):
    """#152: events that open an attempt name the run they belong to."""

    def test_opening_events_carry_the_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            run_id = read_jsonl(root / ANCHORS)[0]["run_id"]
            claim_step(root, task_plan, "P1", "agent-a", lease_minutes=1,
                       now=datetime(2020, 1, 1, tzinfo=timezone.utc))
            reclaim_step(root, task_plan, "P1", "agent-b", "lease expired")
            edit_line(root, "src/app.txt", 1, "RECORDED")
            finish_step(root, "P1", "A2", ["src/app.txt"])
            amend_step(root, task_plan, "P1", "agent-a", "follow-up")
            events = read_jsonl(root / ".agent/step-runs.jsonl")
            marked = [(event["event"], event["run_id"]) for event in events if "run_id" in event]
            self.assertEqual(
                marked, [("claimed", run_id), ("claimed", run_id), ("amendment_started", run_id)],
            )

    def test_claim_after_row_deleted_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root, plan())
            claim_step(root, task_plan, "P1", "agent-a")
            (root / ANCHORS).unlink()
            before = (root / ".agent/step-runs.jsonl").read_bytes()
            with self.assertRaises(BaselineError) as caught:
                claim_step(root, task_plan, "P2", "agent-a")
            self.assertIn(STEP_RUNS_MARKED, str(caught.exception))
            self.assertEqual((root / ".agent/step-runs.jsonl").read_bytes(), before)
            # init-execution does not restore it: the ledger holds claims.
            init_execution_artifacts(root)
            self.assertFalse((root / ANCHORS).exists())

    def test_force_restart_marks_events_with_the_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            first = claim_step(root, task_plan, "P1", "agent-a")["run_id"]
            init_execution_artifacts(root, force=True)
            second = read_jsonl(root / ANCHORS)[0]["run_id"]
            self.assertNotEqual(second, first)
            self.assertEqual(claim_step(root, task_plan, "P1", "agent-a")["run_id"], second)
            self.assertEqual(
                [event.get("run_id") for event in read_jsonl(root / ".agent/step-runs.jsonl")], [second],
            )

    def test_unmarked_ledger_without_row_keeps_head_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            as_legacy_run(root)
            with comparison_snapshot():
                self.assertEqual(comparison_base(root)["source"], "head")
            (root / "outside.txt").write_text("unrecorded\n", encoding="utf-8")
            commit_all(root, "sneak")
            self.assertEqual(audit_drift(root, task_plan)["baseline"], {"source": "head", "commit": None})


class ProvenanceTests(unittest.TestCase):
    def test_drift_report_records_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            anchor = read_jsonl(root / ANCHORS)[0]["base_commit"]
            self.assertEqual(audit_drift(root, task_plan)["baseline"], {"source": "run-anchor", "commit": anchor})

    def test_verify_run_records_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, task_plan = completed_run(tmp)
            anchor = read_jsonl(root / ANCHORS)[0]["base_commit"]
            result = verify_run(root, task_plan, record=True)
            self.assertEqual(result["baseline"], {"source": "run-anchor", "commit": anchor})
            row = read_jsonl(root / ".agent/verification-runs.jsonl")[-1]
            self.assertEqual(row["baseline"], {"source": "run-anchor", "commit": anchor})


class RegistrationTests(unittest.TestCase):
    def test_next_action_works_without_anchor_ledger(self) -> None:
        from agentflow.porcelain import next_action

        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = start_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            as_legacy_run(root)  # a run started by an older version
            action = next_action(root)
            self.assertNotIn("run-anchors", " ".join([action.reason, *action.diagnostics]))
            self.assertEqual(action.step_id, "P1")


class BaseRefTests(unittest.TestCase):
    def legacy_repo_with_committed_edits(self, tmp: str) -> tuple:
        """A legacy run (no anchor) whose edits are committed, then cloned clean."""
        work = Path(tmp) / "work"
        work.mkdir()
        root = seeded_repo(str(work))
        base = git(root, "rev-parse", "HEAD")
        task_plan = start_run(root)
        claim_step(root, task_plan, "P1", "agent-a")
        as_legacy_run(root)  # a run started by 1.6 or older: claims, no anchor
        edit_line(root, "src/app.txt", 1, "RECORDED")
        finish_step(root, "P1", "A1", ["src/app.txt"])
        (root / "stray.txt").write_text("unrecorded\n", encoding="utf-8")
        edit_line(root, "src/app.txt", 15, "SNEAKED")
        commit_all(root, "work")
        clone = Path(tmp) / "clone"
        git(Path(tmp), "clone", "-q", str(root), str(clone))
        subprocess.run(["cp", "-R", str(root / ".agent"), str(clone / ".agent")], check=True)
        return clone, task_plan, base

    def test_base_ref_in_clean_clone_fails_on_committed_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone, task_plan, base = self.legacy_repo_with_committed_edits(tmp)
            self.assertEqual(verify_run(clone, task_plan, record=False)["status"], "passed")
            with comparison_snapshot(base_ref=base):
                drift = audit_drift(clone, task_plan)
                result = verify_run(clone, task_plan, record=False)
            self.assertEqual(drift["status"], "fail")
            self.assertEqual(drift["baseline"], {"source": "base-ref", "commit": base, "ref": base})
            self.assertIn("stray.txt", drift["out_of_scope_files"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("out-of-scope changed file: stray.txt", messages(result))
            code, out, _err = run_cli("verify-run", "--root", str(clone), "--no-record", "--base-ref", base)
            self.assertEqual(code, 1)
            self.assertIn("stray.txt", out)
            code, out, _err = run_cli("audit-drift", "--root", str(clone), "--base-ref", base)
            self.assertEqual(code, 1)

    def test_base_ref_unknown_ref_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone, _task_plan, _base = self.legacy_repo_with_committed_edits(tmp)
            code, out, err = run_cli("verify-run", "--root", str(clone), "--no-record", "--base-ref", "no-such-ref")
            self.assertEqual(code, 1)
            self.assertIn("no-such-ref", out + err)
            self.assertNotIn("Traceback", out + err)

    def test_base_ref_multiple_merge_bases_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            git(root, "checkout", "-qb", "a")
            (root / "src/a.txt").write_text("a\n", encoding="utf-8")
            commit_all(root, "a")
            git(root, "checkout", "-qb", "b", "HEAD~1")
            (root / "src/b.txt").write_text("b\n", encoding="utf-8")
            commit_all(root, "b")
            git(root, "merge", "-q", "--no-edit", "a")  # B2 = B1 + A1
            git(root, "checkout", "-q", "a")
            git(root, "merge", "-q", "--no-edit", "b~1")  # A2 = A1 + B1: criss-cross
            git(root, "checkout", "-q", "b")
            start_run(root)
            code, out, err = run_cli("audit-drift", "--root", str(root), "--base-ref", "a")
            self.assertEqual(code, 1, out + err)
            self.assertIn("merge base", out + err)

    def test_finish_run_reports_a_dash_ref_instead_of_exiting_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone, _task_plan, _base = self.legacy_repo_with_committed_edits(tmp)
            code, out, _err = run_cli("finish-run", "--root", str(clone), "--base-ref=-x", "--json")
            self.assertEqual(code, 1)
            result = json.loads(out)
            self.assertEqual(result["stopped_at"], "audit-drift")
            self.assertTrue(any("-x" in line for line in result["diagnostics"]), result)

    def test_finish_run_passes_base_ref_to_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone, _task_plan, base = self.legacy_repo_with_committed_edits(tmp)
            code, out, _err = run_cli("finish-run", "--root", str(clone), "--base-ref", base, "--json")
            result = json.loads(out)
            self.assertEqual(code, 1)
            self.assertEqual(result["stopped_at"], "audit-drift")


if __name__ == "__main__":
    unittest.main()
