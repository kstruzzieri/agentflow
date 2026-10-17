from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentflow.contracts import (
    COMMAND_RECEIPTS_SCHEMA_VERSION,
    FILE_RECEIPTS_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    STEP_RUNS_SCHEMA_VERSION,
)
from agentflow.artifacts import append_jsonl, create_initial_artifacts, read_json, write_json
from agentflow.execution import (
    amend_step,
    block_step,
    claim_step,
    complete_step,
    fail_step,
    init_execution_artifacts,
    mark_step_verified,
    reclaim_step,
)
from agentflow.execution_coverage import (
    build_execution_coverage,
    replacement_needs,
    timeout_receipt_states,
    verify_run,
    verify_step,
)
from agentflow.receipts import record_command, record_file_change, run_command
from agentflow.validation import audit_drift


def plan() -> dict:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "Verify fixture.",
        "scope": ["Exercise verification."],
        "non_goals": [],
        "invariants": ["Verification is derived from ledgers."],
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
                "preconditions": ["Claimed."],
                "expected_diff": ["fixture.txt exists."],
                "validation": ["python3 -c \"print('ok')\""],
                "evidence_ids": ["E1"],
                "gates": [
                    {"kind": "command", "run": ["python3", "-c", "print('ok')"]},
                    {
                        "kind": "inspection",
                        "evidence_id": "E1",
                        "describe": "Fixture inspected.",
                    },
                ],
            }
        ],
        "evidence_ids": ["E1"],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


class ExecutionVerificationTests(unittest.TestCase):
    def _root(self, tmp: str, claim: bool = True) -> Path:
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", plan())
        append_jsonl(
            root / ".agent/evidence.jsonl",
            {
                "schema_version": "0.2.0",
                "id": "E1",
                "claim": "Inspection completed.",
                "source": "manual",
                "confidence": "high",
                "last_verified": "2026-06-01T00:00:00+00:00",
            },
        )
        if claim:
            claim_step(root, plan(), "P1", "agent-a")
        return root

    def _append_failed_commands(self, root: Path, count: int) -> None:
        for index in range(count):
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
                    "id": f"CRX{index}",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["pytest", "-k", "foo"],
                    "cwd": ".",
                    "started_at": f"2026-06-30T10:0{index}:00+00:00",
                    "finished_at": f"2026-06-30T10:0{index}:01+00:00",
                    "exit_code": 1,
                    "decision": "allowed",
                },
            )

    def test_verify_run_surfaces_stuck_warning_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._append_failed_commands(root, 3)

            result = verify_run(root, plan(), strict=False, record=False)

            stuck_warnings = [
                w for w in result["warnings"]
                if w.get("rule") == "repeated_command_failure"
            ]
            self.assertEqual(len(stuck_warnings), 1)
            stuck_errors = [
                e for e in result["errors"]
                if e.get("rule") == "repeated_command_failure"
            ]
            self.assertEqual(stuck_errors, [])

    def test_verify_run_never_turns_stuck_into_error_under_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._append_failed_commands(root, 3)

            result = verify_run(root, plan(), strict=True, record=False)

            stuck_errors = [
                e for e in result["errors"]
                if e.get("rule") == "repeated_command_failure"
            ]
            self.assertEqual(stuck_errors, [])

    def _append_timeout_receipt(
        self,
        root: Path,
        *,
        remove_fields: tuple[str, ...] = (),
        **overrides: object,
    ) -> None:
        receipt = {
            "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
            "id": "CR-timeout",
            "step_id": "P1",
            "attempt_id": "A1",
            "provenance": "observed",
            "command": ["python3", "-c", "import time; time.sleep(2)"],
            "cwd": ".",
            "env_names": [],
            "started_at": "2026-06-23T10:00:00+00:00",
            "finished_at": "2026-06-23T10:00:01+00:00",
            "exit_code": None,
            "stdout_path": None,
            "stderr_path": None,
            "stdout_sha256": None,
            "stderr_sha256": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "truncated": False,
            "decision": "timeout",
            "timed_out": True,
            "timeout_seconds": 1,
            "gate": "python3 -c \"print('ok')\"",
        }
        receipt.update(overrides)
        for field in remove_fields:
            receipt.pop(field, None)
        append_jsonl(
            root / ".agent/command-receipts.jsonl",
            receipt,
        )

    def test_verify_step_reports_timeout_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._append_timeout_receipt(root)

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "gate python3 -c print('ok') timed out after 1 seconds",
                [finding["message"] for finding in result["errors"]],
            )

    def test_verify_step_reports_timeout_when_either_timeout_marker_is_present(self) -> None:
        cases = [
            ("decision_timeout_timed_out_false", {"decision": "timeout", "timed_out": False}, ()),
            ("decision_timeout_timed_out_missing", {"decision": "timeout"}, ("timed_out",)),
            ("timed_out_true_decision_allowed", {"decision": "allowed", "timed_out": True}, ()),
            ("timed_out_true_decision_missing", {"timed_out": True}, ("decision",)),
        ]
        expected_message = "gate python3 -c print('ok') timed out after 1 seconds"
        for name, overrides, remove_fields in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = self._root(tmp)
                    self._append_timeout_receipt(
                        root,
                        remove_fields=remove_fields,
                        **overrides,
                    )

                    result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

                    messages = [finding["message"] for finding in result["errors"]]
                    self.assertEqual(result["status"], "failed")
                    self.assertIn(expected_message, messages)
                    self.assertNotIn(
                        "gate python3 -c print('ok') recorded exit code None",
                        messages,
                    )

    def test_verify_run_reports_timeout_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._append_timeout_receipt(root)

            result = verify_run(root, plan(), record=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any(
                    "CR-timeout" in finding["message"]
                    and "P1" in finding["message"]
                    and "1 seconds" in finding["message"]
                    for finding in result["errors"]
                ),
                result["errors"],
            )

    def _timeout_findings(self, result: dict) -> tuple:
        errors = [f for f in result["errors"] if "timed out" in f["message"]]
        warnings = [f for f in result["warnings"] if f.get("rule") == "superseded_timeout"]
        return errors, warnings

    def _abandon(self, root: Path, attempt: str) -> None:
        append_jsonl(root / ".agent/step-runs.jsonl", {
            "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": "abandoned",
            "step_id": "P1", "attempt_id": attempt,
            "recorded_at": "2026-10-14T00:00:00+00:00", "reason": "lease expired",
        })

    def _complete(self, root: Path, attempt: str) -> None:
        mark_step_verified(root, "P1", attempt, [])
        complete_step(root, "P1", attempt)

    def _legacy_complete(self, root: Path, attempt: str) -> None:
        """1.0 let a timed-out attempt complete; #70 refuses, so write the rows."""
        for event in ("verified", "completed"):
            append_jsonl(root / ".agent/step-runs.jsonl", {
                "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": event,
                "step_id": "P1", "attempt_id": attempt,
                "recorded_at": "2026-10-16T00:00:00+00:00",
            })

    def test_complete_step_refuses_any_timed_out_receipt_on_the_attempt(self) -> None:
        cases = {
            "decision_with_false_flag": ({"timed_out": False}, ()),
            "decision_without_flag": ({}, ("timed_out",)),
            "flag_only": ({"decision": "allowed"}, ()),
            "non_gate_command": ({"command": ["sleep", "9"], "gate": "unrelated"}, ()),
        }
        for name, (overrides, removed) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)  # P1/A1 open
                self._append_timeout_receipt(root, remove_fields=removed, **overrides)
                mark_step_verified(root, "P1", "A1", [])
                with self.assertRaisesRegex(RuntimeError, "holds timed-out command receipt CR-timeout "):
                    complete_step(root, "P1", "A1")

    def test_refusal_lists_every_timed_out_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._append_timeout_receipt(root)
            self._append_timeout_receipt(root, id="CR-t2")
            mark_step_verified(root, "P1", "A1", [])
            with self.assertRaisesRegex(RuntimeError, "receipt CR-timeout, CR-t2 and cannot complete"):
                complete_step(root, "P1", "A1")

    def test_timeouts_on_other_attempts_or_steps_do_not_block_completion(self) -> None:
        for overrides in ({"attempt_id": "A9"}, {"step_id": "P2"}):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)
                self._append_timeout_receipt(root, **overrides)
                mark_step_verified(root, "P1", "A1", [])
                self.assertEqual(complete_step(root, "P1", "A1")["event"], "completed")

    def _retried(self, root: Path) -> None:
        fail_step(root, "P1", "A1", "gate timed out")
        self._complete(root, claim_step(root, plan(), "P1", "agent-a")["attempt_id"])

    def test_superseded_timeout_is_a_warning_in_both_modes(self) -> None:
        closers = {
            "failed": lambda root: fail_step(root, "P1", "A1", "gate timed out"),
            "blocked": lambda root: block_step(root, "P1", "A1", "gate timed out"),
            "abandoned": lambda root: self._abandon(root, "A1"),
        }
        for closed, close in closers.items():
            for strict in (False, True):
                with self.subTest(closed=closed, strict=strict), \
                        tempfile.TemporaryDirectory() as tmp:
                    root = self._root(tmp)  # P1/A1 open
                    self._append_timeout_receipt(root)
                    close(root)
                    self._complete(root, claim_step(root, plan(), "P1", "agent-a")["attempt_id"])

                    result = verify_run(root, plan(), strict=strict, record=False)
                    errors, warnings = self._timeout_findings(result)

                    self.assertEqual(result["status"], "warning", result["errors"])
                    self.assertEqual(errors, [])
                    self.assertEqual(warnings, [{
                        "severity": "warning",
                        "rule": "superseded_timeout",
                        "message": "command receipt CR-timeout for P1 timed out after 1 seconds "
                                   "in attempt A1; superseded by completed attempt A2",
                        "step_id": "P1",
                        "attempt_id": "A1",
                        "receipt_id": "CR-timeout",
                        "superseded_by": "A2",
                    }])

    def test_timeout_stays_an_error_unless_superseded(self) -> None:
        def later_attempt_open(root: Path) -> None:
            fail_step(root, "P1", "A1", "gate timed out")
            claim_step(root, plan(), "P1", "agent-a")

        def later_attempt_failed(root: Path) -> None:
            fail_step(root, "P1", "A1", "gate timed out")
            fail_step(root, "P1", claim_step(root, plan(), "P1", "agent-a")["attempt_id"], "again")

        def completed_before_close(root: Path) -> None:
            # Advisory leases let A2 open, and complete, while A1 is still open.
            self._complete(root, claim_step(root, plan(), "P1", "agent-b")["attempt_id"])
            fail_step(root, "P1", "A1", "gate timed out")

        def opened_before_close(root: Path) -> None:
            # A2 opens while A1 is open and completes after A1 closes: its opening,
            # not its completion, must come after the close.
            later = claim_step(root, plan(), "P1", "agent-b")["attempt_id"]
            fail_step(root, "P1", "A1", "gate timed out")
            self._complete(root, later)

        def receipt_step_completed(root: Path) -> None:
            # The receipt claims P2, but A1 belongs to P1; a completed P2 attempt
            # must not supersede it.
            fail_step(root, "P1", "A1", "gate timed out")
            for event in ("claimed", "completed"):
                append_jsonl(root / ".agent/step-runs.jsonl", {
                    "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": event,
                    "step_id": "P2", "attempt_id": "A2",
                    "recorded_at": "2026-10-14T00:00:00+00:00",
                })

        def other_step_completed(root: Path) -> None:
            fail_step(root, "P1", "A1", "gate timed out")
            for event in ("claimed", "completed"):
                append_jsonl(root / ".agent/step-runs.jsonl", {
                    "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": event,
                    "step_id": "P9", "attempt_id": "A2",
                    "recorded_at": "2026-10-14T00:00:00+00:00",
                })

        cases = {
            "still_open": (lambda root: None, {}, ()),
            # A passing rerun inside A1 does not clear A1's own timeout.
            "completed_same_attempt": (lambda root: self._legacy_complete(root, "A1"), {}, ()),
            "later_attempt_open": (later_attempt_open, {}, ()),
            "later_attempt_failed": (later_attempt_failed, {}, ()),
            "completed_before_close": (completed_before_close, {}, ()),
            "opened_before_close": (opened_before_close, {}, ()),
            "other_step_completed": (other_step_completed, {}, ()),
            "step_mismatch_with_completed_receipt_step": (
                receipt_step_completed, {"step_id": "P2"}, ()),
            "unknown_attempt": (self._retried, {"attempt_id": "A9"}, ()),
            "missing_attempt": (self._retried, {}, ("attempt_id",)),
            "missing_step": (self._retried, {}, ("step_id",)),
            "step_mismatch": (self._retried, {"step_id": "P2"}, ()),
        }
        for name, (setup, overrides, removed) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)  # P1/A1 open
                self._append_timeout_receipt(root, remove_fields=removed, **overrides)
                setup(root)

                errors, warnings = self._timeout_findings(
                    verify_run(root, plan(), strict=False, record=False)
                )

                self.assertEqual(warnings, [])
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("timed out after 1 seconds", errors[0]["message"])

    def test_timeout_for_undeclared_step_is_not_superseded(self) -> None:
        for strict in (False, True):
            with self.subTest(strict=strict), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)
                self._complete(root, "A1")  # The only declared step, P1, is complete.
                for event, attempt in (("claimed", "A2"), ("failed", "A2"),
                                       ("claimed", "A3"), ("completed", "A3")):
                    append_jsonl(root / ".agent/step-runs.jsonl", {
                        "event": event, "step_id": "P9", "attempt_id": attempt,
                        "recorded_at": "2026-10-14T00:00:00+00:00",
                    })
                self._append_timeout_receipt(root, step_id="P9", attempt_id="A2")

                result = verify_run(root, plan(), strict=strict, record=False)
                errors, warnings = self._timeout_findings(result)

                self.assertEqual(result["status"], "failed")
                self.assertEqual(warnings, [])
                self.assertEqual(errors, [{
                    "severity": "error",
                    "message": "command receipt CR-timeout for P9 timed out after 1 seconds",
                }])

    def test_mixed_step_attempt_history_cannot_supersede_timeout(self) -> None:
        for conflicting_attempt, conflicting_event in (("A1", "failed"), ("A2", "completed")):
            for strict in (False, True):
                with self.subTest(attempt=conflicting_attempt, strict=strict), \
                        tempfile.TemporaryDirectory() as tmp:
                    root = self._root(tmp)
                    self._append_timeout_receipt(root)
                    self._retried(root)  # P1/A1 failed, then P1/A2 completed.
                    ledger = root / ".agent/step-runs.jsonl"
                    events = [json.loads(line) for line in ledger.read_text().splitlines()]
                    for event in events:
                        if (event.get("attempt_id") == conflicting_attempt
                                and event.get("event") == conflicting_event):
                            event["step_id"] = "P2"
                    ledger.write_text("".join(json.dumps(event) + "\n" for event in events))

                    result = verify_run(root, plan(), strict=strict, record=False)
                    errors, warnings = self._timeout_findings(result)

                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(warnings, [])
                    self.assertEqual(errors, [{
                        "severity": "error",
                        "message": "command receipt CR-timeout for P1 timed out after 1 seconds",
                    }])

    def test_contradictory_terminal_history_cannot_supersede_timeout(self) -> None:
        for attempt, earlier_terminal in (("A1", "completed"), ("A2", "failed")):
            for strict in (False, True):
                with self.subTest(attempt=attempt, strict=strict), \
                        tempfile.TemporaryDirectory() as tmp:
                    root = self._root(tmp)
                    self._append_timeout_receipt(root)
                    self._retried(root)  # P1/A1 failed, then P1/A2 completed.
                    ledger = root / ".agent/step-runs.jsonl"
                    events = [json.loads(line) for line in ledger.read_text().splitlines()]
                    terminal = "failed" if attempt == "A1" else "completed"
                    index = next(i for i, event in enumerate(events)
                                 if event.get("attempt_id") == attempt
                                 and event.get("event") == terminal)
                    events.insert(index, {**events[index], "event": earlier_terminal})
                    ledger.write_text("".join(json.dumps(event) + "\n" for event in events))

                    result = verify_run(root, plan(), strict=strict, record=False)
                    errors, warnings = self._timeout_findings(result)

                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(warnings, [])
                    self.assertEqual(errors, [{
                        "severity": "error",
                        "message": "command receipt CR-timeout for P1 timed out after 1 seconds",
                    }])

    def test_amendment_chain_supersedes_failed_amendment_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._complete(root, "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")  # A2
            self._append_timeout_receipt(root, attempt_id="A2")
            fail_step(root, "P1", "A2", "gate timed out")
            amend_step(root, plan(), "P1", "agent-a", "retry review fix")  # A3
            self._complete(root, "A3")

            errors, warnings = self._timeout_findings(verify_run(root, plan(), record=False))

            self.assertEqual(errors, [])
            self.assertEqual([w["superseded_by"] for w in warnings], ["A3"])

    def test_failed_amendment_timeout_without_later_completion_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._complete(root, "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")  # A2
            self._append_timeout_receipt(root, attempt_id="A2")
            fail_step(root, "P1", "A2", "gate timed out")

            errors, warnings = self._timeout_findings(verify_run(root, plan(), record=False))

            self.assertEqual(warnings, [])
            self.assertEqual(len(errors), 1, errors)

    def test_superseded_by_names_the_first_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._append_timeout_receipt(root)
            self._retried(root)  # A1 failed, A2 completed
            amend_step(root, plan(), "P1", "agent-a", "review fix")  # A3
            self._complete(root, "A3")

            _, warnings = self._timeout_findings(verify_run(root, plan(), record=False))

            self.assertEqual([w["superseded_by"] for w in warnings], ["A2"])

    def test_aggregated_ids_supersede_by_ledger_order_not_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            # The completing attempt's id sorts first but comes later in the ledger.
            for event, attempt in (("claimed", "WTw2-A1"), ("failed", "WTw2-A1"),
                                   ("claimed", "WTw1-A1"), ("completed", "WTw1-A1")):
                append_jsonl(root / ".agent/step-runs.jsonl", {
                    "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": event,
                    "step_id": "P1", "attempt_id": attempt,
                    "recorded_at": "2026-10-14T00:00:00+00:00",
                })
            self._append_timeout_receipt(root, id="WTw2-CR1", attempt_id="WTw2-A1")

            errors, warnings = self._timeout_findings(verify_run(root, plan(), record=False))

            self.assertEqual(errors, [])
            self.assertEqual([w["superseded_by"] for w in warnings], ["WTw1-A1"])

    def _amended(self, root: Path) -> str:
        """P1 completed as A1; returns the open amendment attempt id (A2)."""
        self._complete(root, "A1")
        return amend_step(root, plan(), "P1", "agent-a", "review fix")["attempt_id"]

    def test_timeout_receipt_states_classify_each_history(self) -> None:
        cases = {
            "open": (lambda root: None, {}, "open"),
            "recoverable_failed": (lambda root: fail_step(root, "P1", "A1", "t"), {}, "recoverable"),
            "recoverable_blocked": (lambda root: block_step(root, "P1", "A1", "t"), {}, "recoverable"),
            "recoverable_abandoned": (lambda root: self._abandon(root, "A1"), {}, "recoverable"),
            "superseded": (self._retried, {}, "superseded"),
            "completed_same_attempt": (lambda root: self._legacy_complete(root, "A1"), {}, "unrecoverable"),
            "unknown_attempt": (lambda root: None, {"attempt_id": "A9"}, "unrecoverable"),
            "undeclared_step": (lambda root: None, {"step_id": "P9"}, "unrecoverable"),
        }
        for name, (setup, overrides, expected) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)
                self._append_timeout_receipt(root, **overrides)
                setup(root)
                [(receipt, state, by)] = timeout_receipt_states(root, plan())
                self.assertEqual((receipt["id"], state), ("CR-timeout", expected))
                self.assertEqual(by, "A2" if expected == "superseded" else None)

    def test_replacement_needs_agree_with_verify_run(self) -> None:
        def timeout_blocked(root):
            aid = self._amended(root)
            self._append_timeout_receipt(root, attempt_id=aid)
            block_step(root, "P1", aid, "t")

        def failed(root):
            fail_step(root, "P1", self._amended(root), "gave up")

        def blocked(root):
            block_step(root, "P1", self._amended(root), "paused")

        def older_timeout(root):  # A2 timeout blocked, then A3 clean blocked
            timeout_blocked(root)
            block_step(root, "P1", amend_step(root, plan(), "P1", "agent-a", "again")["attempt_id"], "x")

        def superseded_then_failed(root):  # review focus 4
            timeout_blocked(root)
            self._complete(root, amend_step(root, plan(), "P1", "agent-a", "retry")["attempt_id"])
            fail_step(root, "P1", amend_step(root, plan(), "P1", "agent-a", "more")["attempt_id"], "x")

        def never_completed(root):
            self._append_timeout_receipt(root)
            block_step(root, "P1", "A1", "t")

        def open_amendment(root):
            self._append_timeout_receipt(root, attempt_id=self._amended(root))

        cases = {  # name: (setup, {strict: expected cause names})
            "timeout_blocked": (timeout_blocked, {False: ["timeout"], True: ["timeout", "strict_incomplete"]}),
            "failed": (failed, {False: ["failed_attempt"], True: ["failed_attempt", "strict_incomplete"]}),
            "blocked": (blocked, {False: [], True: ["strict_incomplete"]}),
            "older_timeout": (older_timeout, {False: ["timeout"], True: ["timeout", "strict_incomplete"]}),
            "superseded_then_failed": (superseded_then_failed, {False: ["failed_attempt"], True: ["failed_attempt", "strict_incomplete"]}),
            "never_completed": (never_completed, {False: [], True: []}),
            "open_amendment": (open_amendment, {False: [], True: []}),
        }
        messages = {"failed_attempt": "P1 has failed attempt", "strict_incomplete": "P1 is not completed"}
        for name, (setup, expected) in cases.items():
            for strict, causes in expected.items():
                with self.subTest(case=name, strict=strict), tempfile.TemporaryDirectory() as tmp:
                    root = self._root(tmp)
                    setup(root)
                    needs = replacement_needs(root, plan(), strict=strict)
                    got = [c["cause"] for c in needs[0]["causes"]] if needs else []
                    self.assertEqual(got, causes)
                    errors = {f["message"] for f in verify_run(root, plan(), strict=strict, record=False)["errors"]}
                    for cause in got:
                        if cause in messages:
                            self.assertIn(messages[cause], errors)
                    if "timeout" in got:
                        self.assertTrue(any("timed out after 1 seconds" in m for m in errors))

    def test_timeout_receipt_states_follow_ledger_order_for_aggregated_ids(self) -> None:
        def rows(*pairs):
            for event, attempt in pairs:
                append_jsonl(root / ".agent/step-runs.jsonl", {
                    "schema_version": STEP_RUNS_SCHEMA_VERSION, "event": event, "step_id": "P1",
                    "attempt_id": attempt, "recorded_at": "2026-10-17T00:00:00+00:00",
                })
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            rows(("claimed", "WTw2-A1"), ("failed", "WTw2-A1"))
            self._append_timeout_receipt(root, id="WTw2-CR1", attempt_id="WTw2-A1")
            [(_receipt, state, _by)] = timeout_receipt_states(root, plan())
            self.assertEqual(state, "recoverable")
            rows(("claimed", "WTw1-A1"), ("completed", "WTw1-A1"))  # sorts first, comes later
            [(_receipt, state, by)] = timeout_receipt_states(root, plan())
            self.assertEqual((state, by), ("superseded", "WTw1-A1"))

    def test_strict_by_default_counts_as_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            block_step(root, "P1", self._amended(root), "paused")
            path = root / ".agent/execution.contract.json"
            contract = read_json(path)
            contract.setdefault("proof_policy", {})["strict_by_default"] = True
            write_json(path, contract)
            [need] = replacement_needs(root, plan())
            self.assertEqual([c["cause"] for c in need["causes"]], ["strict_incomplete"])
            self.assertEqual((need["attempt_id"], need["status"]), ("A2", "blocked"))

    def test_verify_step_rejects_non_timeout_null_exit_code_generically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
                    "id": "CR-malformed",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["python3", "-c", "print('ok')"],
                    "cwd": ".",
                    "env_names": [],
                    "started_at": "2026-06-23T10:00:00+00:00",
                    "finished_at": "2026-06-23T10:00:01+00:00",
                    "exit_code": None,
                    "stdout_path": None,
                    "stderr_path": None,
                    "stdout_sha256": None,
                    "stderr_sha256": None,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "truncated": False,
                    "decision": "allowed",
                    "timed_out": False,
                    "gate": "python3 -c \"print('ok')\"",
                },
            )

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "failed")
            messages = [finding["message"] for finding in result["errors"]]
            self.assertTrue(any("recorded exit code None" in message for message in messages))
            self.assertFalse(any("timed out" in message for message in messages))

    def test_verify_step_passes_with_command_receipt_file_receipt_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("hello\n", encoding="utf-8")
            run_command(
                root,
                plan(),
                "P1",
                None,
                ["python3", "-c", "print('ok')"],
                gate="python3 -c \"print('ok')\"",
            )
            record_file_change(root, plan(), "P1", None, "fixture.txt")

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["errors"], [])

    def test_verify_run_fails_open_amendment_on_completed_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")

            result = verify_run(root, plan(), record=False)

            # #58: an open attempt fails the run in both modes (it used to be a warning).
            self.assertEqual(result["status"], "failed")
            self.assertIn("P1 has open attempt A2", [e["message"] for e in result["errors"]])
            self.assertFalse(any("has open attempt" in w["message"] for w in result["warnings"]))

    def test_verify_run_fails_open_attempt_in_both_modes(self) -> None:
        for strict in (False, True):
            with self.subTest(strict=strict), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)  # P1/A1 open

                result = verify_run(root, plan(), strict=strict, record=False)

                self.assertEqual(result["status"], "failed")
                errors = [f["message"] for f in result["errors"]]
                self.assertIn("P1 has open attempt A1", errors)
                # Strict's latest-status rule must not duplicate the helper's error.
                self.assertEqual(errors.count("P1 is not completed"), 1)
                every = errors + [f["message"] for f in result["warnings"]]
                self.assertEqual(
                    [m for m in every if "has open attempt" in m], ["P1 has open attempt A1"]
                )

    def test_verify_run_names_every_open_attempt_in_ledger_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open; advisory leases allow a second claim
            claim_step(root, plan(), "P1", "agent-b")

            result = verify_run(root, plan(), strict=False, record=False)

            self.assertIn("P1 has open attempt A1, A2", [e["message"] for e in result["errors"]])

    def test_verify_run_fails_never_claimed_step_without_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, claim=False)

            result = verify_run(root, plan(), strict=False, record=False)

            self.assertEqual(result["status"], "failed")
            self.assertIn("P1 is not completed", [e["message"] for e in result["errors"]])

    def test_verify_run_fails_blocked_only_step_without_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            block_step(root, "P1", "A1", "waiting on input")

            result = verify_run(root, plan(), strict=False, record=False)

            errors = [e["message"] for e in result["errors"]]
            self.assertIn("P1 is not completed", errors)
            self.assertFalse(any("has open attempt" in m for m in errors))

    def test_verify_run_without_execution_contract_fails_incomplete_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
            create_initial_artifacts(root)
            write_json(root / ".agent/plan.lock.json", plan())

            result = verify_run(root, plan(), strict=False, record=False)

            self.assertIn("P1 is not completed", [e["message"] for e in result["errors"]])

    def test_blocked_amendment_passes_lax_and_fails_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")
            block_step(root, "P1", "A2", "deferred")

            lax = verify_run(root, plan(), strict=False, record=False)
            strict = verify_run(root, plan(), strict=True, record=False)

            self.assertNotIn("P1 is not completed", [e["message"] for e in lax["errors"]])
            self.assertIn("P1 is not completed", [e["message"] for e in strict["errors"]])

    def test_verify_run_completed_step_has_no_incomplete_findings(self) -> None:
        for strict in (False, True):
            with self.subTest(strict=strict), tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)
                mark_step_verified(root, "P1", "A1", [])
                complete_step(root, "P1", "A1")

                result = verify_run(root, plan(), strict=strict, record=False)

                messages = [f["message"] for f in result["errors"] + result["warnings"]]
                self.assertFalse(
                    any("has open attempt" in m or "is not completed" in m for m in messages),
                    messages,
                )

    def test_verify_run_flags_terminal_attempt_receipt_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            # Hand-edited ledger: a file receipt recorded after the attempt's
            # completed event (write guard bypassed).
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {
                    "schema_version": FILE_RECEIPTS_SCHEMA_VERSION,
                    "id": "FR99",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "path": "fixture.txt",
                    "change_kind": "modified",
                    "after_sha256": "deadbeef",
                    "recorded_at": "2999-01-01T00:00:00+00:00",
                },
            )
            result = verify_run(root, plan(), record=False)
            messages = [e["message"] for e in result["errors"]]
            self.assertTrue(
                any("terminal-attempt receipt violation" in m for m in messages)
            )

    def test_verify_run_compares_terminal_receipt_timestamps_as_datetimes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            append_jsonl(
                root / ".agent/step-runs.jsonl",
                {
                    "schema_version": STEP_RUNS_SCHEMA_VERSION,
                    "event": "completed",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "recorded_at": "2026-06-01T00:00:00+00:00",
                },
            )
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {
                    "schema_version": FILE_RECEIPTS_SCHEMA_VERSION,
                    "id": "FR100",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "path": "fixture.txt",
                    "change_kind": "modified",
                    "after_sha256": "deadbeef",
                    "recorded_at": "2026-05-31T20:30:00-04:00",
                },
            )

            result = verify_run(root, plan(), record=False)
            messages = [e["message"] for e in result["errors"]]

            self.assertTrue(
                any("terminal-attempt receipt violation" in m for m in messages)
            )

    def test_verify_run_flags_receipt_referencing_unknown_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open
            # Hand-edited ledger: a file receipt naming an attempt that was never
            # opened (write guard bypassed); the backstop must catch it.
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {
                    "schema_version": FILE_RECEIPTS_SCHEMA_VERSION,
                    "id": "FR42",
                    "step_id": "P1",
                    "attempt_id": "A7",
                    "path": "fixture.txt",
                    "change_kind": "modified",
                    "after_sha256": "deadbeef",
                    "recorded_at": "2026-06-18T10:00:00+00:00",
                },
            )
            result = verify_run(root, plan(), record=False)
            messages = [e["message"] for e in result["errors"]]
            self.assertTrue(
                any("unknown attempt" in m for m in messages), messages
            )

    def test_verify_step_strict_rejects_attested_gate_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "print('ok')"],
                exit_code=0,
                gate="python3 -c \"print('ok')\"",
            )

            result = verify_step(root, plan(), "P1", None, strict=True, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("attested" in finding["message"] for finding in result["errors"]))

    def test_verify_step_contract_rejects_attested_gate_receipt_without_cli_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["proof_policy"]["require_managed_receipts_for_validation"] = True
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "print('ok')"],
                exit_code=0,
                gate="python3 -c \"print('ok')\"",
            )

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("attested" in finding["message"] for finding in result["errors"]))

    def test_verify_step_contract_strict_by_default_promotes_missing_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["proof_policy"]["strict_by_default"] = True
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("missing command receipt" in finding["message"] for finding in result["errors"])
            )

    def test_verify_step_default_contract_requires_missing_command_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("missing command receipt" in finding["message"] for finding in result["errors"])
            )

    def test_verify_step_strict_rejects_legacy_attested_gate_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            legacy_plan = plan()
            legacy_plan["steps"][0].pop("gates")
            record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "print('ok')"],
                exit_code=0,
                gate="python3 -c \"print('ok')\"",
            )

            result = verify_step(root, legacy_plan, "P1", None, strict=True, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("attested" in finding["message"] for finding in result["errors"]))

    def test_legacy_inspection_gate_uses_step_evidence_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            legacy_plan = plan()
            legacy_plan["steps"][0].pop("gates")
            legacy_plan["steps"][0]["validation"] = ["manual inspection"]

            result = verify_step(root, legacy_plan, "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "passed")

    def test_failed_command_receipt_makes_verify_step_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "raise SystemExit(3)"],
                exit_code=3,
                gate="python3 -c \"print('ok')\"",
            )

            result = verify_step(root, plan(), "P1", None, strict=False, replay=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(any("exit code 3" in finding["message"] for finding in result["errors"]))

    def test_build_execution_coverage_collects_warnings_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            coverage = build_execution_coverage(root, plan(), strict=False)

            self.assertIn("steps", coverage)
            self.assertIn("P1", coverage["steps"])
            self.assertTrue(coverage["steps"]["P1"]["warnings"])

    def test_verify_run_fails_unmapped_changed_file_and_ignores_agent_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("hello\n", encoding="utf-8")
            (root / ".agent/extra-ledger.jsonl").write_text("{}\n", encoding="utf-8")

            result = verify_run(root, plan(), strict=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("fixture.txt" in finding["message"] for finding in result["errors"])
            )
            self.assertFalse(
                any("extra-ledger" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_handles_tracked_file_modification_with_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "fixture.txt"], cwd=str(root), check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Agentflow Tests",
                    "-c",
                    "user.email=agentflow-tests@example.com",
                    "commit",
                    "-m",
                    "seed fixture",
                ],
                cwd=str(root),
                check=True,
                stdout=subprocess.PIPE,
            )

            (root / "fixture.txt").write_text("updated\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")

            result = verify_run(root, plan(), strict=False)

            self.assertFalse(
                any("fixture.txt" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_honors_file_receipt_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["proof_policy"]["require_file_receipts_for_changed_files"] = False
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            (root / "fixture.txt").write_text("hello\n", encoding="utf-8")

            result = verify_run(root, plan(), strict=False)

            self.assertFalse(
                any("changed file is not mapped" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_strict_requires_file_receipts_despite_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["proof_policy"]["require_file_receipts_for_changed_files"] = False
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            (root / "fixture.txt").write_text("hello\n", encoding="utf-8")

            result = verify_run(root, plan(), strict=True)

            self.assertTrue(
                any("changed file is not mapped" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_opt_out_still_reconciles_existing_file_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["proof_policy"]["require_file_receipts_for_changed_files"] = False
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            (root / "fixture.txt").write_text("first\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            (root / "fixture.txt").write_text("changed later\n", encoding="utf-8")

            result = verify_run(root, plan(), strict=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("out-of-band edit" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_reconciles_latest_file_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("first\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            (root / "fixture.txt").write_text("second\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")

            result = verify_run(root, plan(), strict=False)

            self.assertFalse(
                any("out-of-band edit" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_detects_post_receipt_out_of_band_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("first\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            (root / "fixture.txt").write_text("changed later\n", encoding="utf-8")

            result = verify_run(root, plan(), strict=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("out-of-band edit" in finding["message"] for finding in result["errors"])
            )

    def test_verify_run_omits_step_and_attempt_ids_for_run_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            verify_run(root, plan(), strict=False)

            entries = [
                json.loads(line)
                for line in (root / ".agent/verification-runs.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(entries[-1]["scope"], "run")
            self.assertNotIn("step_id", entries[-1])
            self.assertNotIn("attempt_id", entries[-1])

    def test_verify_run_can_skip_recording_for_read_only_ci(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            ledger = root / ".agent/verification-runs.jsonl"
            before = ledger.read_text(encoding="utf-8")

            result = verify_run(root, plan(), strict=False, record=False)

            # P1 is claimed-but-open in this fixture, so verify-run fails (#58);
            # the point of this test is that record=False leaves the ledger untouched.
            self.assertEqual(result["status"], "failed")
            self.assertEqual(ledger.read_text(encoding="utf-8"), before)

    def _seed_commit(self, root, name, content):
        (root / name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=str(root), check=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "seed"],
            cwd=str(root), check=True, stdout=subprocess.PIPE,
        )

    def _set_hunk_policy(self, root, value):
        contract_path = root / ".agent/execution.contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["proof_policy"]["hunk_attribution"] = value
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

    def test_audit_drift_flags_unmapped_hunk_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._seed_commit(root, "fixture.txt", "\n".join(f"l{i}" for i in range(1, 21)) + "\n")
            lines = (root / "fixture.txt").read_text().splitlines()
            lines[1] = "RECORDED"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            lines[18] = "STRAY"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = audit_drift(root, plan())
            self.assertEqual(report["status"], "fail")
            self.assertEqual(len(report["unmapped_hunks"]), 1)
            self.assertEqual(report["unmapped_hunks"][0]["reason"], "no_matching_hunk")

    def test_audit_drift_observe_is_warning_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_hunk_policy(root, "observe")
            self._seed_commit(root, "fixture.txt", "\n".join(f"l{i}" for i in range(1, 21)) + "\n")
            lines = (root / "fixture.txt").read_text().splitlines()
            lines[1] = "RECORDED"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            lines[18] = "STRAY"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = audit_drift(root, plan())
            self.assertEqual(report["status"], "warning")
            self.assertEqual(len(report["unmapped_hunks"]), 1)

    def test_audit_drift_falls_back_on_malformed_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".agent/execution.contract.json").write_text("{not json", encoding="utf-8")
            (root / "fixture.txt").write_text("changed\n", encoding="utf-8")

            report = audit_drift(root, plan())

            self.assertIn("fixture.txt", report["changed_files"])
            self.assertEqual(report["unmapped_hunks"], [])

    def test_verify_run_falls_back_on_malformed_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".agent/execution.contract.json").write_text("{not json", encoding="utf-8")
            (root / "fixture.txt").write_text("changed\n", encoding="utf-8")

            result = verify_run(root, plan(), record=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("changed file is not mapped to a step receipt" in f["message"] for f in result["errors"])
            )

    def test_verify_run_fails_on_unmapped_hunk_with_no_scope_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._seed_commit(root, "fixture.txt", "\n".join(f"l{i}" for i in range(1, 21)) + "\n")
            lines = (root / "fixture.txt").read_text().splitlines()
            lines[1] = "RECORDED"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            lines[18] = "STRAY"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = verify_run(root, plan(), strict=False)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("unmapped hunk" in f["message"] for f in result["errors"])
            )

    def test_verify_run_observe_does_not_fail_on_unmapped_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_hunk_policy(root, "observe")
            self._seed_commit(root, "fixture.txt", "\n".join(f"l{i}" for i in range(1, 21)) + "\n")
            lines = (root / "fixture.txt").read_text().splitlines()
            lines[1] = "RECORDED"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            lines[18] = "STRAY"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = verify_run(root, plan(), strict=False)
            self.assertFalse(
                any("unmapped hunk" in f["message"] for f in result["errors"])
            )

    def test_multi_step_union_covers_all_recorded_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._seed_commit(root, "fixture.txt", "\n".join(f"l{i}" for i in range(1, 31)) + "\n")
            lines = (root / "fixture.txt").read_text().splitlines()
            lines[2] = "TOP"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            lines[27] = "BOTTOM"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")  # second receipt snapshots both
            report = audit_drift(root, plan())
            self.assertEqual(report["unmapped_hunks"], [])
            self.assertNotEqual(report["status"], "fail")

    def test_duplicate_identical_hunk_is_over_covered(self) -> None:
        # Documented P1 limitation: two byte-identical changed blocks share one
        # identity, so recording one region also "covers" the second, identical
        # edit. This asserts the limitation rather than treating it as a bug.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            seed = "a\nTARGET\nb\n" + "".join(f"pad{i}\n" for i in range(10)) + "a\nTARGET\nb\n"
            self._seed_commit(root, "fixture.txt", seed)
            text = (root / "fixture.txt").read_text()
            once = text.replace("TARGET", "CHANGED", 1)  # edit only the FIRST block
            (root / "fixture.txt").write_text(once, encoding="utf-8")
            record_file_change(root, plan(), "P1", None, "fixture.txt")
            both = once.replace("TARGET", "CHANGED")  # now edit the SECOND, identical block
            (root / "fixture.txt").write_text(both, encoding="utf-8")
            report = audit_drift(root, plan())
            self.assertEqual(report["unmapped_hunks"], [])  # over-covered, not flagged

    def test_normalized_execution_hash_reflects_recorded_hunks(self) -> None:
        # File receipts (with their hunk fields) are execution artifacts, so the
        # tamper-evident normalized hash changes when recorded hunks change.
        from agentflow.proof import execution_summary

        def _hash_after_edit(replacement: str) -> str:
            with tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp)
                self._seed_commit(root, "fixture.txt", "a\nb\nc\n")
                (root / "fixture.txt").write_text(f"a\n{replacement}\nc\n", encoding="utf-8")
                record_file_change(root, plan(), "P1", None, "fixture.txt")
                return execution_summary(root, plan())["normalized_execution_sha256"]

        self.assertNotEqual(_hash_after_edit("ONE"), _hash_after_edit("TWO"))


class ReviewBackstopTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        return ExecutionVerificationTests._root(self, tmp)

    def test_unresolved_finding_ref_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            append_jsonl(
                root / ".agent/step-runs.jsonl",
                {
                    "event": "amendment_started",
                    "step_id": "P1",
                    "attempt_id": "A2",
                    "recorded_at": "2026-06-20T18:00:00+00:00",
                    "finding_refs": [
                        {
                            "review_run_id": "RR-20260620T180000Z-deadbeef",
                            "finding_id": "BP-001",
                        }
                    ],
                },
            )
            result = verify_run(root, plan(), record=False)
            self.assertTrue(
                any(
                    "unresolved finding ref" in w["message"]
                    for w in result["warnings"]
                )
            )


class LeaseCoverageTests(unittest.TestCase):
    def _enforce_root(self, tmp: str) -> Path:
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", plan())
        contract = read_json(root / ".agent/execution.contract.json")
        contract["concurrency"].update(
            {"lease_policy": "enforce", "lease_ttl_minutes": 30, "lease_grace_seconds": 30}
        )
        write_json(root / ".agent/execution.contract.json", contract)
        claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
        return root

    def test_expired_lease_errors_verify_run_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._enforce_root(tmp)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            coverage = build_execution_coverage(root, plan(), now=future)
            messages = [
                e["message"] for s in coverage["steps"].values() for e in s["errors"]
            ]
            self.assertTrue(any("expired" in m.lower() for m in messages))
            self.assertIn("A1", coverage["expired_leases"])
            result = verify_run(root, plan(), record=False, now=future)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("expired" in e["message"].lower() for e in result["errors"])
            )

    def test_expired_lease_only_warns_under_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            write_json(root / ".agent/plan.lock.json", plan())
            # advisory default; stamp an explicit past deadline so expiry is deterministic.
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            coverage = build_execution_coverage(root, plan(), now=future)
            self.assertIn("A1", coverage["expired_leases"])
            errors = [e for s in coverage["steps"].values() for e in s["errors"]]
            self.assertEqual(errors, [])
            warnings = [
                w["message"] for s in coverage["steps"].values() for w in s["warnings"]
            ]
            self.assertTrue(any("expired" in m.lower() for m in warnings))

    def test_no_deadline_open_attempt_warns_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._enforce_root(tmp)
            # Rewrite A1's claim as a legacy no-deadline lease.
            path = root / ".agent/step-runs.jsonl"
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            rows[-1]["lease_expires_at"] = None
            path.write_text(
                "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                encoding="utf-8",
            )
            coverage = build_execution_coverage(root, plan())
            self.assertIn("A1", coverage["no_deadline_open_attempts"])
            warnings = [
                w["message"] for s in coverage["steps"].values() for w in s["warnings"]
            ]
            self.assertTrue(any("no lease deadline" in m.lower() for m in warnings))
            errors = [e for s in coverage["steps"].values() for e in s["errors"]]
            self.assertEqual(errors, [])

    def test_abandoned_attempt_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._enforce_root(tmp)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            reclaim_step(root, plan(), "P1", "agent-b", reason="a crashed", now=future)
            coverage = build_execution_coverage(root, plan())
            abandoned_ids = [a["attempt_id"] for a in coverage["abandoned_attempts"]]
            self.assertIn("A1", abandoned_ids)
            entry = next(a for a in coverage["abandoned_attempts"] if a["attempt_id"] == "A1")
            self.assertEqual(entry["abandoned_by"], "agent-b")
            self.assertEqual(entry["superseded_by"], "A2")
            # Abandoned alone never errors verify-run (the fresh A2 lease is live).
            result = verify_run(root, plan(), record=False)
            self.assertFalse(
                any("abandon" in e["message"].lower() for e in result["errors"])
            )


if __name__ == "__main__":
    unittest.main()
