from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentflow.artifacts import append_jsonl, create_initial_artifacts, read_json, write_json
from agentflow.execution import (
    amend_step,
    claim_step,
    complete_step,
    init_execution_artifacts,
    mark_step_verified,
    reclaim_step,
)
from agentflow.execution_coverage import build_execution_coverage, verify_run, verify_step
from agentflow.receipts import record_command, record_file_change, run_command
from agentflow.validation import audit_drift


def plan() -> dict:
    return {
        "schema_version": "0.3.0",
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
    def _root(self, tmp: str) -> Path:
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
        claim_step(root, plan(), "P1", "agent-a")
        return root

    def _append_failed_commands(self, root: Path, count: int) -> None:
        for index in range(count):
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
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
            "schema_version": "0.3.0",
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

    def test_verify_step_rejects_non_timeout_null_exit_code_generically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
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

    def test_verify_run_surfaces_open_amendment_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")

            result = verify_run(root, plan(), record=False)
            messages = [w["message"] for w in result["warnings"]]
            self.assertTrue(any("has open attempt" in m for m in messages))

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
                    "schema_version": "0.3.0",
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
                    "schema_version": "0.3.0",
                    "event": "completed",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "recorded_at": "2026-06-01T00:00:00+00:00",
                },
            )
            append_jsonl(
                root / ".agent/file-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
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
                    "schema_version": "0.3.0",
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

            # P1 is claimed-but-open in this fixture, so verify-run now surfaces
            # the open-attempt warning at the top level; the point of this test is
            # that record=False leaves the ledger untouched.
            self.assertEqual(result["status"], "warning")
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
