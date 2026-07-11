from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentflow.artifacts import append_jsonl
from agentflow.events import filter_events_since, project_events, valid_since


def _write(root: Path, name: str, records: list) -> None:
    path = root / ".agent" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


class ProjectEventsTests(unittest.TestCase):
    def test_projects_chronological_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "step-runs", [
                {"event": "claimed", "step_id": "P1", "attempt_id": "A1",
                 "recorded_at": "2026-06-18T10:00:00+00:00", "agent_id": "me"},
                {"event": "completed", "step_id": "P1", "attempt_id": "A1",
                 "recorded_at": "2026-06-18T10:05:00+00:00"},
            ])
            _write(root, "command-receipts", [
                {"id": "CR1", "step_id": "P1", "attempt_id": "A1",
                 "started_at": "2026-06-18T10:02:00+00:00", "exit_code": 0,
                 "command": ["pytest"]},
            ])
            events = project_events(root)
            self.assertEqual(
                [event["type"] for event in events],
                ["step.claimed", "command.recorded", "step.completed"],
            )

    def test_projects_amendment_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "step-runs", [
                {"event": "amendment_started", "step_id": "P1", "attempt_id": "A2",
                 "amends_attempt": "A1", "amends_completed_at": "2026-06-18T10:05:00+00:00",
                 "reason": "address review", "reason_code": "review_feedback",
                 "recorded_at": "2026-06-18T11:00:00+00:00", "agent_id": "me"},
            ])
            events = project_events(root)
            amend = [e for e in events if e["type"] == "step.amendment_started"]
            self.assertEqual(len(amend), 1)
            self.assertEqual(amend[0]["data"]["amends_attempt"], "A1")
            self.assertEqual(amend[0]["data"]["reason_code"], "review_feedback")
            self.assertEqual(
                amend[0]["data"]["amends_completed_at"], "2026-06-18T10:05:00+00:00"
            )

    def test_projects_abandon_and_fail_audit_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "step-runs", [
                {"event": "abandoned", "step_id": "P1", "attempt_id": "A1",
                 "abandoned_by": "agent-b", "superseded_by": "A2", "reason": "crash",
                 "recorded_at": "2026-06-18T11:00:00+00:00"},
                {"event": "failed", "step_id": "P1", "attempt_id": "A2",
                 "failed_by": "operator", "reason": "unrecoverable",
                 "recorded_at": "2026-06-18T11:05:00+00:00"},
            ])
            events = project_events(root)
            abandoned = [e for e in events if e["type"] == "step.abandoned"][0]
            self.assertEqual(abandoned["data"]["abandoned_by"], "agent-b")
            self.assertEqual(abandoned["data"]["superseded_by"], "A2")
            failed = [e for e in events if e["type"] == "step.failed"][0]
            self.assertEqual(failed["data"]["failed_by"], "operator")

    def test_command_event_exposes_decision_and_risk(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                {
                    "id": "CR1",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "started_at": "2026-06-18T10:00:00+00:00",
                    "finished_at": "2026-06-18T10:00:00+00:00",
                    "exit_code": None,
                    "command": ["rm", "-rf", "fixture.txt"],
                    "provenance": "observed",
                    "decision": "blocked",
                    "risk": {
                        "level": "high",
                        "findings": [
                            {
                                "category": "destructive_delete",
                                "level": "high",
                                "detail": "rm recursive force",
                            }
                        ],
                    },
                }
            ])

            event = project_events(root)[0]

            self.assertEqual(event["type"], "command.recorded")
            self.assertEqual(event["data"]["decision"], "blocked")
            self.assertEqual(event["data"]["risk_level"], "high")
            self.assertEqual(event["data"]["finding_count"], 1)

    def test_command_event_exposes_timeout_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                {
                    "id": "CR1",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "started_at": "2026-06-23T10:00:00+00:00",
                    "finished_at": "2026-06-23T10:00:01+00:00",
                    "exit_code": None,
                    "command": ["python3", "-c", "import time; time.sleep(2)"],
                    "provenance": "observed",
                    "decision": "timeout",
                    "timed_out": True,
                    "timeout_seconds": 1,
                }
            ])

            event = project_events(root)[0]

            self.assertEqual(event["data"]["decision"], "timeout")
            self.assertEqual(event["data"]["timed_out"], True)
            self.assertEqual(event["data"]["timeout_seconds"], 1)

    def test_tie_break_is_deterministic(self) -> None:
        timestamp = "2026-06-18T10:00:00+00:00"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "step-runs", [
                {"event": "verified", "step_id": "P1", "attempt_id": "A1",
                 "recorded_at": timestamp}])
            _write(root, "command-receipts", [
                {"id": "CR1", "step_id": "P1", "attempt_id": "A1",
                 "started_at": timestamp, "exit_code": 0, "command": ["x"]}])
            _write(root, "file-receipts", [
                {"id": "FR1", "step_id": "P1", "attempt_id": "A1",
                 "recorded_at": timestamp, "path": "a.py", "change_kind": "modified"}])
            _write(root, "verification-runs", [
                {"id": "VR1", "scope": "run", "status": "passed",
                 "recorded_at": timestamp}])
            first = project_events(root)
            second = project_events(root)
            self.assertEqual(first, second)
            self.assertEqual(
                [event["source"]["ledger"] for event in first],
                ["step-runs", "command-receipts", "file-receipts", "verification-runs"],
            )

    def test_missing_ledger_is_tolerated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "step-runs", [
                {"event": "claimed", "step_id": "P1", "attempt_id": "A1",
                 "recorded_at": "2026-06-18T10:00:00+00:00"}])
            events = project_events(root)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "step.claimed")

    def test_all_missing_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(project_events(Path(tmp)), [])

    def test_source_pointers(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "step-runs", [
                {"event": "claimed", "step_id": "P1", "attempt_id": "A1",
                 "recorded_at": "2026-06-18T09:59:00+00:00"}])
            _write(root, "command-receipts", [
                {"id": "CR1", "step_id": "P1", "attempt_id": "A1",
                 "started_at": "2026-06-18T10:00:00+00:00", "exit_code": 0,
                 "command": ["x"]},
                {"id": "CR2", "step_id": "P1", "attempt_id": "A1",
                 "started_at": "2026-06-18T10:01:00+00:00", "exit_code": 0,
                 "command": ["y"]},
            ])
            events = project_events(root)
            self.assertEqual(
                events[0]["source"],
                {"ledger": "step-runs", "record_id": None, "index": 0},
            )
            cr2 = [e for e in events if e["source"]["record_id"] == "CR2"][0]
            self.assertEqual(
                cr2["source"],
                {"ledger": "command-receipts", "record_id": "CR2", "index": 1},
            )

    def test_filter_since_is_inclusive(self) -> None:
        events = [
            {"timestamp": "2026-06-18T10:00:00+00:00"},
            {"timestamp": "2026-06-18T11:00:00+00:00"},
        ]
        self.assertEqual(len(filter_events_since(events, "2026-06-18T10:00:00+00:00")), 2)
        self.assertEqual(len(filter_events_since(events, "2026-06-18T10:30:00+00:00")), 1)

    def test_filter_since_compares_offsets_by_absolute_time(self) -> None:
        events = [
            {"timestamp": "2026-06-18T08:59:59+00:00"},
            {"timestamp": "2026-06-18T09:30:00+00:00"},
        ]

        filtered = filter_events_since(events, "2026-06-18T10:00:00+01:00")

        self.assertEqual(filtered, [{"timestamp": "2026-06-18T09:30:00+00:00"}])

    def test_command_event_exposes_cwd_and_output_hashes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                {
                    "id": "CR1",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "started_at": "2026-06-30T10:00:00+00:00",
                    "exit_code": 1,
                    "command": ["pytest", "-k", "foo"],
                    "cwd": ".",
                    "decision": "allowed",
                    "stdout_sha256": "a" * 64,
                    "stderr_sha256": "b" * 64,
                }
            ])
            event = project_events(root)[0]
            self.assertEqual(event["data"]["cwd"], ".")
            self.assertEqual(event["data"]["stdout_sha256"], "a" * 64)
            self.assertEqual(event["data"]["stderr_sha256"], "b" * 64)

    def test_valid_since(self) -> None:
        self.assertTrue(valid_since("2026-06-18T10:00:00+00:00"))
        self.assertTrue(valid_since("2026-06-18T10:00:00Z"))
        self.assertTrue(valid_since("2026-06-18"))
        self.assertFalse(valid_since("not-a-date"))


class ReviewEventTests(unittest.TestCase):
    def test_review_recorded_event_projected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_jsonl(
                root / ".agent/review-runs.jsonl",
                {
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "recorded_at": "2026-06-20T18:00:00+00:00",
                    "gate_status": "pass",
                    "active_blocking": [],
                    "policy": "full",
                },
            )
            events = project_events(root)
            review_events = [e for e in events if e["type"] == "review.recorded"]
            self.assertEqual(len(review_events), 1)
            self.assertEqual(review_events[0]["source"]["ledger"], "review-runs")
            self.assertEqual(review_events[0]["data"]["gate_status"], "pass")
            self.assertEqual(review_events[0]["data"]["active_blocking_count"], 0)

    def test_finding_refs_surface_on_amendment_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_jsonl(
                root / ".agent/step-runs.jsonl",
                {
                    "event": "amendment_started",
                    "step_id": "P1",
                    "attempt_id": "A2",
                    "recorded_at": "2026-06-20T18:01:00+00:00",
                    "finding_refs": [
                        {"review_run_id": "RR-20260620T180000Z-ab12cd34", "finding_id": "BP-001"}
                    ],
                },
            )
            events = project_events(root)
            amend = [e for e in events if e["type"] == "step.amendment_started"][0]
            self.assertIn("finding_refs", amend["data"])


if __name__ == "__main__":
    unittest.main()
