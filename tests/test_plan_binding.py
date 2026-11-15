"""#138: the locked plan is bound to the run; an edit needs a recorded amendment."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest import mock

from agentflow import porcelain
from agentflow.aggregate import Source, analyze, parse_sources, write_canonical
from agentflow.artifacts import (
    create_initial_artifacts,
    plan_binding_sha256,
    read_json,
    read_jsonl,
    write_json,
)
from agentflow.contracts import AMENDMENTS_SCHEMA_VERSION, RUN_ANCHORS_SCHEMA_VERSION
from agentflow.execution import claim_step, init_execution_artifacts
from agentflow.execution_coverage import verify_run, verify_step
from agentflow.plan_binding import plan_binding_error
from agentflow.proof import InvalidWorkingState, build_proof, verify_proof
from agentflow.validation import audit_drift

from tests.test_aggregate import _repo_with_worktrees
from tests.test_run_baseline import (
    commit_all,
    edit_line,
    finish_step,
    messages,
    one_step_plan,
    run_cli,
    seeded_repo,
)

ANCHORS = ".agent/run-anchors.jsonl"
AMENDMENTS = ".agent/amendments.jsonl"
PLAN = ".agent/plan.lock.json"
MISMATCH = "plan does not match the accepted plan"


def bound_run(root: Path, task_plan: Optional[dict] = None) -> dict:
    """A run started the documented way: the plan is locked before init-execution."""
    task_plan = task_plan or one_step_plan()
    create_initial_artifacts(root)
    write_json(root / PLAN, task_plan)
    init_execution_artifacts(root)
    return task_plan


def anchor(root: Path) -> dict:
    return read_jsonl(root / ANCHORS)[0]


def hand_edit(root: Path, **changes: object) -> dict:
    """Edit the plan file directly, as an agent loosening its own rules would."""
    data = json.loads((root / PLAN).read_text(encoding="utf-8"))
    data.update(changes)
    (root / PLAN).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def widen(root: Path) -> dict:
    return hand_edit(root, allowed_files=["src/", "other/", ".agent/"])


def relock(root: Path) -> None:
    code, _, err = run_cli("lock-plan", str(root / PLAN))
    assert code == 0, err


def amend(root: Path, amendment_id: str = "AM1") -> tuple:
    return run_cli(
        "amend-plan", "--root", str(root), "--id", amendment_id,
        "--reason", "scope grew", "--changed-scope", "allow other/",
    )


def current(root: Path) -> dict:
    return read_json(root / PLAN)


class RunStartPinTests(unittest.TestCase):
    def test_init_execution_pins_the_locked_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            row = anchor(root)
            self.assertEqual(row["schema_version"], RUN_ANCHORS_SCHEMA_VERSION)
            self.assertEqual(row["plan_sha256"], plan_binding_sha256(task_plan))
            self.assertRegex(row["run_id"], r"^[0-9a-f]{32}$")

    def test_each_run_gets_its_own_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            first = anchor(root)["run_id"]
            init_execution_artifacts(root, force=True)
            self.assertNotEqual(anchor(root)["run_id"], first)

    def test_library_init_with_an_unlocked_plan_pins_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            self.assertIsNone(anchor(root)["plan_sha256"])
            write_json(root / PLAN, one_step_plan())
            self.assertIsNone(plan_binding_error(root, current(root)))

    def test_run_outside_git_is_pinned_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_plan = bound_run(root)
            row = anchor(root)
            self.assertIsNone(row["base_commit"])
            self.assertEqual(row["plan_sha256"], plan_binding_sha256(task_plan))
            widen(root)
            self.assertIn(MISMATCH, plan_binding_error(root, current(root)))
            report = audit_drift(root, current(root))
            self.assertEqual(report["status"], "fail")

    def test_cli_init_execution_refuses_an_unlocked_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            self.assertEqual(run_cli("init", "--root", str(root))[0], 0)
            code, out, err = run_cli("init-execution", "--root", str(root))
            self.assertEqual(code, 2)
            self.assertIn("lock-plan", out + err)
            self.assertEqual(len((out + err).strip().splitlines()), 1)
            self.assertFalse((root / ".agent/step-runs.jsonl").exists())
            self.assertFalse((root / ANCHORS).exists())

    def test_cli_force_refuses_before_replacing_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            before = (root / ".agent/step-runs.jsonl").read_bytes()
            hand_edit(root, locked=False)
            code, _, _ = run_cli("init-execution", "--root", str(root), "--force")
            self.assertEqual(code, 2)
            self.assertEqual((root / ".agent/step-runs.jsonl").read_bytes(), before)

    def test_cli_rerun_on_an_existing_run_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            hand_edit(root, locked=False)
            code, out, _ = run_cli("init-execution", "--root", str(root))
            self.assertEqual(code, 0)
            self.assertIn("skipped .agent/step-runs.jsonl", out)


class HandEditTests(unittest.TestCase):
    def test_hand_edit_fails_every_run_level_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            pinned = plan_binding_sha256(task_plan)
            edited = widen(root)
            found = plan_binding_sha256(edited)

            expected = (
                f"plan does not match the accepted plan (expected {pinned[:12]}, found {found[:12]}) "
                "and no amendment records the change; lock the plan, then record it with amend-plan"
            )
            self.assertEqual(plan_binding_error(root, edited), expected)
            self.assertIn(expected, messages(verify_run(root, edited, record=False)))
            report = audit_drift(root, edited)
            self.assertEqual(report["status"], "fail")
            self.assertIn(expected, report["notes"])
            self.assertIn(expected, messages(verify_step(root, edited, "P1", "A1", record=False)))

            for command in ("verify-run", "audit-drift", "finish-run"):
                code, out, err = run_cli(command, "--root", str(root))
                self.assertEqual(code, 1, command)
                lines = [line for line in (out + err).splitlines() if MISMATCH in line]
                self.assertEqual(len(lines), 1, (command, out, err))

    def test_noop_relock_keeps_the_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            relock(root)
            self.assertIsNone(plan_binding_error(root, current(root)))

    def test_base_ref_does_not_bless_a_plan_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            widen(root)
            code, out, err = run_cli("verify-run", "--root", str(root), "--base-ref", "HEAD")
            self.assertEqual(code, 1)
            self.assertIn(MISMATCH, out + err)

    def test_next_action_names_the_plan_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            widen(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "state_invalid")
            self.assertTrue(any(MISMATCH in line for line in action.diagnostics))

    def test_build_proof_refuses_a_plan_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            edit_line(root, "src/app.txt", 1, "RECORDED")
            finish_step(root, "P1", "A1", ["src/app.txt"])
            edited = hand_edit(root, objective="A different objective.")
            with self.assertRaises(InvalidWorkingState) as raised:
                build_proof(root, root / PLAN)
            self.assertIn(MISMATCH, str(raised.exception))
            self.assertIsNotNone(edited)

    def test_verify_proof_recomputes_the_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            edit_line(root, "src/app.txt", 1, "RECORDED")
            finish_step(root, "P1", "A1", ["src/app.txt"])
            hand_edit(root, objective="A different objective.")
            # A proof built by a client that skipped the check: hashes agree, lineage does not.
            with mock.patch("agentflow.proof.plan_binding_error", return_value=None):
                write_json(root / ".agent/proof-pack.json", build_proof(root, root / PLAN))
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertTrue(
                any(f["severity"] == "error" and MISMATCH in f["message"] for f in findings), findings
            )


class AmendmentTests(unittest.TestCase):
    def test_amend_plan_after_relock_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            edited = widen(root)
            relock(root)
            code, out, err = amend(root)
            self.assertEqual(code, 0, err)
            self.assertIn("recorded amendment AM1", out)

            row = read_jsonl(root / AMENDMENTS)[0]
            self.assertEqual(row["schema_version"], AMENDMENTS_SCHEMA_VERSION)
            self.assertEqual(row["run_id"], anchor(root)["run_id"])
            self.assertEqual(row["previous_plan_sha256"], plan_binding_sha256(task_plan))
            self.assertEqual(row["plan_sha256"], plan_binding_sha256(edited))

            plan_now = current(root)
            self.assertIsNone(plan_binding_error(root, plan_now))
            self.assertFalse([m for m in messages(verify_run(root, plan_now, record=False)) if MISMATCH in m])
            self.assertFalse([n for n in audit_drift(root, plan_now)["notes"] if MISMATCH in n])

    def test_amendment_recorded_before_the_edit_does_not_cover_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            self.assertEqual(amend(root, "AM1")[0], 0)
            widen(root)
            relock(root)
            self.assertIn(MISMATCH, plan_binding_error(root, current(root)))
            self.assertEqual(amend(root, "AM2")[0], 0)
            self.assertIsNone(plan_binding_error(root, current(root)))

    def test_revert_needs_its_own_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            edited = widen(root)
            relock(root)
            self.assertEqual(amend(root, "AM1")[0], 0)
            hand_edit(root, allowed_files=task_plan["allowed_files"])
            error = plan_binding_error(root, current(root))
            self.assertIn(f"expected {plan_binding_sha256(edited)[:12]}", error)
            self.assertIn(f"found {plan_binding_sha256(task_plan)[:12]}", error)
            relock(root)
            self.assertEqual(amend(root, "AM2")[0], 0)
            self.assertIsNone(plan_binding_error(root, current(root)))

    def test_new_run_ignores_the_previous_runs_amendments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            widen(root)
            relock(root)
            self.assertEqual(amend(root, "AM1")[0], 0)
            write_json(root / PLAN, task_plan)
            init_execution_artifacts(root, force=True)
            self.assertIsNone(plan_binding_error(root, current(root)))
            widen(root)
            self.assertIn(MISMATCH, plan_binding_error(root, current(root)))

    def test_amend_plan_refuses_an_unlocked_or_invalid_plan_on_a_bound_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            hand_edit(root, allowed_files=["src/", "other/", ".agent/"], locked=False)
            code, out, err = amend(root)
            self.assertEqual(code, 2)
            self.assertIn("lock-plan", out + err)
            self.assertEqual(read_jsonl(root / AMENDMENTS), [])
            hand_edit(root, locked=True, objective="")
            self.assertEqual(amend(root)[0], 2)
            self.assertEqual(read_jsonl(root / AMENDMENTS), [])

    def test_amend_plan_on_an_unbound_run_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_cli("init", "--root", str(root))[0], 0)
            self.assertEqual(amend(root)[0], 0)
            row = read_jsonl(root / AMENDMENTS)[0]
            for key in ("run_id", "plan_sha256", "previous_plan_sha256"):
                self.assertNotIn(key, row)


class FailClosedAndLegacyTests(unittest.TestCase):
    def _rewrite_anchor(self, root: Path, **changes: object) -> None:
        row = anchor(root)
        for key, value in changes.items():
            if value is ...:
                row.pop(key, None)
            else:
                row[key] = value
        (root / ANCHORS).write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

    def test_anchor_without_a_plan_hash_is_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            self._rewrite_anchor(root, schema_version="1.0.0", plan_sha256=..., run_id=...)
            widen(root)
            self.assertIsNone(plan_binding_error(root, current(root)))
            self.assertFalse([m for m in messages(verify_run(root, current(root), record=False)) if "plan" in m])

    def test_run_without_an_anchor_is_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            (root / ANCHORS).unlink()
            widen(root)
            self.assertIsNone(plan_binding_error(root, current(root)))

    def test_malformed_pin_fails_closed(self) -> None:
        for bad in ("not-a-hash", 7, ""):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                root = seeded_repo(tmp)
                bound_run(root)
                self._rewrite_anchor(root, plan_sha256=bad)
                error = plan_binding_error(root, current(root))
                self.assertIn("run-anchors.jsonl", error)
                self.assertEqual(verify_run(root, current(root), record=False)["status"], "failed")

    def test_pin_without_a_run_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            self._rewrite_anchor(root, run_id=...)
            self.assertIn("run_id", plan_binding_error(root, current(root)))

    def test_broken_amendment_chain_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            bound_run(root)
            widen(root)
            relock(root)
            self.assertEqual(amend(root)[0], 0)
            rows = read_jsonl(root / AMENDMENTS)
            for change in ({"previous_plan_sha256": "0" * 64}, {"plan_sha256": "nope"}):
                with self.subTest(change=change):
                    (root / AMENDMENTS).write_text(
                        json.dumps({**rows[0], **change}) + "\n", encoding="utf-8"
                    )
                    error = plan_binding_error(root, current(root))
                    self.assertIn("amendment AM1", error)

    def test_committed_work_under_an_amended_plan_passes_verify_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = seeded_repo(tmp)
            task_plan = bound_run(root)
            claim_step(root, task_plan, "P1", "agent-a")
            edit_line(root, "src/app.txt", 1, "RECORDED")
            finish_step(root, "P1", "A1", ["src/app.txt"])
            commit_all(root, "recorded work")
            widen(root)
            relock(root)
            self.assertEqual(amend(root)[0], 0)
            result = verify_run(root, current(root), record=False)
            self.assertEqual(messages(result), [])


class AggregationTests(unittest.TestCase):
    def _amended_sources(self, tmp: str, shared: bool = False) -> tuple:
        output, first, second = _repo_with_worktrees(tmp)
        bound_run(first)
        init_execution_artifacts(first, force=True)
        shutil.copytree(first / ".agent", second / ".agent", dirs_exist_ok=True)
        widen(first)
        relock(first)
        shutil.copyfile(first / PLAN, second / PLAN)
        self.assertEqual(amend(first, "AM1")[0], 0)
        if shared:
            shutil.copyfile(first / AMENDMENTS, second / AMENDMENTS)
        else:
            self.assertEqual(amend(second, "AM2")[0], 0)
        sources = [Source(first, "a", "a"), Source(second, "b", "b")]
        for source in sources:
            self.assertIsNone(plan_binding_error(source.root, current(source.root)))
        self.assertEqual(analyze(sources, output, base_ref="HEAD")["status"], "ok")
        return output, sources

    def test_conflicting_merged_amendments_preserve_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, sources = self._amended_sources(tmp)
            (output / ".agent").mkdir()
            sentinel = output / ".agent/keep.txt"
            sentinel.write_bytes(b"existing proof")
            before = {
                path: path.read_bytes()
                for source in sources
                for path in (source.root / ".agent").rglob("*") if path.is_file()
            }
            result = write_canonical(sources, output, base_ref="HEAD")
            self.assertEqual(result["status"], "collision", result)
            self.assertTrue(any(
                collision["kind"] == "merged_plan_binding_failed"
                and "amendment AM2" in collision["error"]
                for collision in result["collisions"]
            ), result)
            self.assertEqual(list((output / ".agent").iterdir()), [sentinel])
            self.assertEqual(sentinel.read_bytes(), b"existing proof")
            self.assertEqual(list(output.glob(".agent.aggregate.*")), [])
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_shared_amendment_merges_once_and_keeps_valid_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, sources = self._amended_sources(tmp, shared=True)
            result = write_canonical(sources, output, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(len(read_jsonl(output / AMENDMENTS)), 1)
            self.assertIsNone(plan_binding_error(output, current(output)))

    def _sources(self, tmp: str, edit_second: bool = False, amend_second: bool = False) -> List:
        roots = []
        for name in ("a", "b"):
            root = Path(tmp) / name
            root.mkdir()
            seeded_repo(str(root))
            bound_run(root)
            roots.append(root)
        if edit_second:
            widen(roots[1])
            relock(roots[1])
            if amend_second:
                assert amend(roots[1])[0] == 0
        sources, errors = parse_sources([str(root) for root in roots], ["a", "b"], None)
        assert not errors, errors
        return sources

    def _kinds(self, tmp: str, sources: List) -> List[str]:
        output = Path(tmp) / "out"
        output.mkdir()
        return [collision["kind"] for collision in analyze(sources, output)["collisions"]]

    def test_sources_with_different_plan_pins_collide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sources = self._sources(tmp)
            row = anchor(sources[1].root)
            row["plan_sha256"] = "1" * 64
            (sources[1].root / ANCHORS).write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertIn("run_anchor_mismatch", self._kinds(tmp, sources))

    def test_source_with_an_unrecorded_plan_edit_collides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sources = self._sources(tmp, edit_second=True)
            self.assertIn("plan_binding_failed", self._kinds(tmp, sources))


if __name__ == "__main__":
    unittest.main()
