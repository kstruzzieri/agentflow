from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentflow.contracts import (
    PLAN_SCHEMA_VERSION,
    STEP_RUNS_SCHEMA_VERSION,
)
from agentflow.artifacts import append_jsonl, create_initial_artifacts, read_json, write_json
from agentflow.execution import (
    _close_step,
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
    renew_lease,
    require_lifecycle_owner,
    resolve_attempt,
)


def plan() -> dict:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
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

    def test_writes_to_a_terminal_attempt_are_refused_and_append_nothing(self) -> None:
        def abandon(root: Path, attempt: str) -> None:
            append_jsonl(root / ".agent/step-runs.jsonl", {
                "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": "abandoned",
                "step_id": "P1", "attempt_id": attempt,
                "recorded_at": "2026-10-13T00:00:00+00:00", "reason": "test",
            })

        closers = {
            "completed": lambda root, a: (
                mark_step_verified(root, "P1", a, []), complete_step(root, "P1", a)),
            "blocked": lambda root, a: block_step(root, "P1", a, "first close"),
            "failed": lambda root, a: fail_step(root, "P1", a, "first close"),
            "abandoned": abandon,
        }
        writers = {
            "block": lambda root, a: block_step(root, "P1", a, "second close"),
            "fail": lambda root, a: fail_step(root, "P1", a, "second close"),
            # complete_step's unlocked pre-check fires first; this is the in-lock race path.
            "complete": lambda root, a: _close_step(root, "P1", a, "completed"),
            "verify": lambda root, a: mark_step_verified(root, "P1", a, []),
            "renew": lambda root, a: renew_lease(root, "P1", a, "agent-a", 5),
        }
        for terminal, close in closers.items():
            for name, write in writers.items():
                with self.subTest(terminal=terminal, write=name), \
                        tempfile.TemporaryDirectory() as tmp:
                    root = self._root(tmp)
                    attempt = claim_step(root, plan(), "P1", "agent-a")["attempt_id"]
                    close(root, attempt)
                    ledger = root / ".agent/step-runs.jsonl"
                    before = ledger.read_bytes()

                    with self.assertRaises(ValueError) as ctx:
                        write(root, attempt)

                    self.assertIn(f"is already {terminal}", str(ctx.exception))
                    self.assertEqual(ledger.read_bytes(), before)

    def test_claim_after_standalone_fail_gets_a_fresh_attempt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            fail_step(root, "P1", "A1", "failed before any claim")

            claimed = claim_step(root, plan(), "P1", "agent-a")

            self.assertEqual(claimed["attempt_id"], "A2")
            self.assertEqual(read_step_state(root)["steps"]["P1"]["open_attempts"], ["A2"])

    def test_stale_close_does_not_clear_the_new_attempt_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            first = claim_step(root, plan(), "P1", "agent-a")["attempt_id"]
            block_step(root, "P1", first, "retry")
            second = claim_step(root, plan(), "P1", "agent-a")["attempt_id"]

            with self.assertRaises(ValueError):
                block_step(root, "P1", first, "stale")

            self.assertEqual(current_step_attempt(root, "P1"), second)

    def test_verify_on_terminal_attempt_is_refused_before_any_ledger_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            attempt = claim_step(root, plan(), "P1", "agent-a")["attempt_id"]
            block_step(root, "P1", attempt, "blocked")

            with self.assertRaises(ValueError) as ctx:
                require_lifecycle_owner(root, "P1", attempt, None, action="verify")

            self.assertIn("is already blocked; nothing was recorded", str(ctx.exception))

    def test_second_standalone_fail_on_a_never_opened_attempt_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            fail_step(root, "P1", "A1", "failed before any claim")
            ledger = root / ".agent/step-runs.jsonl"
            before = ledger.read_bytes()

            with self.assertRaises(ValueError) as ctx:
                fail_step(root, "P1", "A1", "again")

            self.assertIn("is already failed", str(ctx.exception))
            self.assertEqual(ledger.read_bytes(), before)

    def test_complete_on_a_completed_attempt_names_the_closed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            attempt = claim_step(root, plan(), "P1", "agent-a")["attempt_id"]
            mark_step_verified(root, "P1", attempt, [])
            complete_step(root, "P1", attempt)

            # RuntimeError keeps complete-step's existing exit code 1 for this case.
            with self.assertRaises(RuntimeError) as ctx:
                complete_step(root, "P1", attempt)

            self.assertIn("is already completed", str(ctx.exception))

    def test_block_on_a_closed_attempt_names_the_closed_state_before_lease_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = read_json(contract_path)
            contract["concurrency"]["lease_policy"] = "enforce"
            write_json(contract_path, contract)
            an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            attempt = claim_step(
                root, plan(), "P1", "agent-a", lease_minutes=1, now=an_hour_ago
            )["attempt_id"]
            fail_step(root, "P1", attempt, "forced closed")

            with self.assertRaises(ValueError) as ctx:
                block_step(root, "P1", attempt, "late block", agent_id="agent-a")

            self.assertIn("is already failed", str(ctx.exception))

    def test_claim_skips_rows_with_a_malformed_attempt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            append_jsonl(root / ".agent/step-runs.jsonl", {
                "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": "failed",
                "step_id": "P1", "attempt_id": [],
                "recorded_at": "2026-10-13T00:00:00+00:00",
            })

            claimed = claim_step(root, plan(), "P1", "agent-a")

            self.assertEqual(claimed["attempt_id"], "A1")

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
                    "schema_version": STEP_RUNS_SCHEMA_VERSION,
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
