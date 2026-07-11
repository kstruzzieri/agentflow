from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentflow.artifacts import append_jsonl, create_initial_artifacts, write_json
from agentflow.execution import (
    amend_step,
    attempt_for_step,
    attempt_is_terminal,
    block_step,
    claim_step,
    complete_step,
    current_step_attempt,
    fail_step,
    init_execution_artifacts,
    latest_completed_attempt,
    mark_step_verified,
    next_step,
    read_step_state,
    resolve_attempt,
)


def plan() -> dict:
    return {
        "schema_version": "0.3.0",
        "objective": "Exercise state machine.",
        "scope": ["State machine fixture."],
        "non_goals": [],
        "invariants": ["Ledgers are append-only."],
        "allowed_files": ["fixture.txt", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["python3 -c \"print('ok')\""],
        "rollback_plan": "Delete fixture.txt.",
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
                "id": "P1",
                "action": "Create fixture.",
                "files": ["fixture.txt"],
                "preconditions": ["Plan locked."],
                "expected_diff": ["fixture.txt exists."],
                "validation": ["python3 -c \"print('ok')\""],
                "evidence_ids": [],
            },
            {
                "id": "P2",
                "action": "Inspect fixture.",
                "files": ["fixture.txt"],
                "preconditions": ["P1 complete."],
                "expected_diff": ["No additional changes."],
                "validation": ["manual inspection"],
                "evidence_ids": [],
                "depends_on": ["P1"],
            },
        ],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


class ExecutionStateTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", plan())
        return root

    def test_next_step_returns_first_pending_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            step = next_step(root, plan())

            self.assertEqual(step["id"], "P1")

    def test_claim_step_uses_global_deterministic_attempt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            first = claim_step(root, plan(), "P1", "agent-a")
            block_step(root, "P1", None, "retry later")
            second = claim_step(root, plan(), "P1", "agent-a")

            self.assertEqual(first["attempt_id"], "A1")
            self.assertEqual(second["attempt_id"], "A2")
            self.assertEqual(current_step_attempt(root, "P1"), "A2")

    def test_resolve_attempt_errors_without_open_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            with self.assertRaises(ValueError) as ctx:
                resolve_attempt(root, "P1", None)

            self.assertIn("claim-step first", str(ctx.exception))

    def test_complete_block_and_fail_close_attempt_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])
            self.assertIsNone(current_step_attempt(root, "P1"))

            second = claim_step(root, plan(), "P2", "agent-a")
            fail_step(root, "P2", second["attempt_id"], "validation failed")
            self.assertIsNone(current_step_attempt(root, "P2"))

    def test_next_step_honors_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")

            self.assertIsNone(next_step(root, plan()))

            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])
            step = next_step(root, plan())

            self.assertEqual(step["id"], "P2")

    def test_amendment_helpers_track_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])

            self.assertTrue(attempt_is_terminal(root, "P1", "A1"))
            attempt_id, completed_event = latest_completed_attempt(root, "P1")
            self.assertEqual(attempt_id, "A1")
            self.assertEqual(completed_event["event"], "completed")
            self.assertIsNotNone(attempt_for_step(root, "P1", "A1"))
            self.assertIsNone(attempt_for_step(root, "P1", "A9"))
            self.assertIsNone(attempt_for_step(root, "P2", "A1"))

    def test_verified_and_completed_events_require_opened_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            with self.assertRaises(ValueError) as verify_ctx:
                mark_step_verified(root, "P1", "A2", [])
            self.assertIn("never opened", str(verify_ctx.exception))

            append_jsonl(
                root / ".agent/step-runs.jsonl",
                {
                    "schema_version": "0.3.0",
                    "event": "verified",
                    "step_id": "P1",
                    "attempt_id": "A2",
                    "recorded_at": "2026-06-19T00:00:00+00:00",
                    "findings": [],
                },
            )
            with self.assertRaises(ValueError) as complete_ctx:
                complete_step(root, "P1", "A2")
            self.assertIn("never opened", str(complete_ctx.exception))

    def test_claim_step_rejects_completed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])

            with self.assertRaises(ValueError) as ctx:
                claim_step(root, plan(), "P1", "agent-a")
            self.assertIn("amend-step", str(ctx.exception))

    def test_claim_step_still_allows_retry_of_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            fail_step(root, "P1", first["attempt_id"], "gate failed")
            second = claim_step(root, plan(), "P1", "agent-a")
            self.assertEqual(second["attempt_id"], "A2")

    def test_amend_step_opens_linked_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])

            event = amend_step(
                root, plan(), "P1", "agent-a", "address review", reason_code="review_feedback"
            )
            self.assertEqual(event["event"], "amendment_started")
            self.assertEqual(event["attempt_id"], "A2")
            self.assertEqual(event["amends_attempt"], "A1")
            self.assertEqual(event["reason"], "address review")
            self.assertEqual(event["reason_code"], "review_feedback")
            self.assertIsNotNone(event["amends_completed_at"])
            self.assertEqual(current_step_attempt(root, "P1"), "A2")

    def test_amend_step_requires_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            with self.assertRaises(ValueError) as ctx:
                amend_step(root, plan(), "P1", "agent-a", "too early")
            self.assertIn("no completed attempt", str(ctx.exception))

    def test_amend_step_rejects_open_amendment_and_bad_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])
            amend_step(root, plan(), "P1", "agent-a", "first amendment")

            with self.assertRaises(ValueError) as open_ctx:
                amend_step(root, plan(), "P1", "agent-a", "second amendment")
            self.assertIn("open attempt", str(open_ctx.exception))

            with self.assertRaises(ValueError):
                amend_step(root, plan(), "P1", "agent-a", "  ")
            with self.assertRaises(ValueError):
                amend_step(root, plan(), "P1", "", "missing agent")
            with self.assertRaises(ValueError):
                amend_step(root, plan(), "P1", "agent-a", "ok", reason_code="bogus")

    def test_amend_step_completed_guard_precedes_input_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            # Never-completed step with an empty reason: the completed-attempt
            # guard takes priority over input validation, per spec.
            with self.assertRaises(ValueError) as ctx:
                amend_step(root, plan(), "P1", "agent-a", "  ")
            self.assertIn("no completed attempt", str(ctx.exception))

    def test_claim_step_rejected_even_after_failed_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])
            amend = amend_step(root, plan(), "P1", "agent-a", "amend")
            fail_step(root, "P1", amend["attempt_id"], "amendment gate failed")

            with self.assertRaises(ValueError) as ctx:
                claim_step(root, plan(), "P1", "agent-a")
            self.assertIn("amend-step", str(ctx.exception))

    def test_amend_step_chains_to_latest_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])
            amend1 = amend_step(root, plan(), "P1", "agent-a", "first")
            mark_step_verified(root, "P1", amend1["attempt_id"], [])
            complete_step(root, "P1", amend1["attempt_id"])
            amend2 = amend_step(root, plan(), "P1", "agent-a", "second")

            self.assertEqual(amend2["attempt_id"], "A3")
            self.assertEqual(amend2["amends_attempt"], "A2")

    def test_amend_step_records_finding_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")
            mark_step_verified(root, "P1", first["attempt_id"], [])
            complete_step(root, "P1", first["attempt_id"])
            event = amend_step(
                root,
                plan(),
                "P1",
                "agent-a",
                "address review finding BP-001",
                reason_code="review_feedback",
                finding_refs=[
                    {"review_run_id": "RR-20260620T180000Z-ab12cd34", "finding_id": "BP-001"}
                ],
            )
            self.assertEqual(
                event["finding_refs"],
                [{"review_run_id": "RR-20260620T180000Z-ab12cd34", "finding_id": "BP-001"}],
            )

    def test_read_step_state_marks_failed_attempt_terminal_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            attempt = claim_step(root, plan(), "P1", "agent-a")
            fail_step(root, "P1", attempt["attempt_id"], "failed gate")

            state = read_step_state(root)

            self.assertEqual(state["steps"]["P1"]["status"], "failed")
            self.assertEqual(state["steps"]["P1"]["completed"], False)


if __name__ == "__main__":
    unittest.main()
