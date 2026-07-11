from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentflow.artifacts import append_jsonl, create_initial_artifacts, write_json
from agentflow.contracts import DEFAULT_COMMAND_TIMEOUT_SECONDS
from agentflow.execution import (
    amend_step,
    claim_step,
    complete_step,
    fail_step,
    init_execution_artifacts,
    mark_step_verified,
)
from agentflow.receipts import (
    command_receipts,
    record_command,
    record_file_change,
    replay_gates,
    resolve_command_timeout_seconds,
    run_command,
    verify_receipt_outputs,
)


def plan() -> dict:
    return {
        "schema_version": "0.3.0",
        "objective": "Receipt fixture.",
        "scope": ["Exercise receipts."],
        "non_goals": [],
        "invariants": ["Receipts are append-only."],
        "allowed_files": ["fixture.txt", ".agent/"],
        "blocked_files": ["blocked.txt"],
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
                "evidence_ids": [],
            }
        ],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


class ReceiptTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", plan())
        claim_step(root, plan(), "P1", "agent-a")
        return root

    def _commit_seed(self, root: Path, name: str, content: str) -> None:
        (root / name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=str(root), check=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "seed"],
            cwd=str(root), check=True, stdout=subprocess.PIPE,
        )

    def _set_hunk_policy(self, root: Path, value) -> None:
        contract_path = root / ".agent/execution.contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["proof_policy"]["hunk_attribution"] = value
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

    def _set_risk_policy(self, root: Path, value) -> None:
        path = root / ".agent/execution.contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        if value is None:
            contract["command_policy"].pop("risk_policy", None)
        else:
            contract["command_policy"]["risk_policy"] = value
        path.write_text(json.dumps(contract), encoding="utf-8")

    def _set_command_timeout(self, root: Path, value: int) -> None:
        path = root / ".agent/execution.contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["command_policy"]["command_timeout_seconds"] = value
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

    def test_allowed_command_records_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "warn")
            receipt = run_command(
                root, plan(), "P1", None, ["python3", "-c", "print('ok')"]
            )
            self.assertEqual(receipt["decision"], "allowed")
            self.assertEqual(receipt["risk"]["level"], "low")
            self.assertEqual(receipt["timed_out"], False)
            self.assertEqual(receipt["timeout_seconds"], DEFAULT_COMMAND_TIMEOUT_SECONDS)

    def test_resolve_command_timeout_falls_back_to_default_when_policy_missing(self) -> None:
        self.assertEqual(
            resolve_command_timeout_seconds(
                plan(),
                "P1",
                ["python3", "-c", "print('ok')"],
                None,
                {},
            ),
            DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )

    def test_resolve_command_timeout_rejects_invalid_policy_timeout(self) -> None:
        for value in (0, -1, True, "600"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "command_timeout_seconds"):
                    resolve_command_timeout_seconds(
                        plan(),
                        "P1",
                        ["python3", "-c", "print('ok')"],
                        None,
                        {"command_timeout_seconds": value},
                    )

    def test_resolve_command_timeout_rejects_invalid_policy_before_override(self) -> None:
        structured_plan = plan()
        structured_plan["steps"][0]["gates"] = [
            {
                "kind": "command",
                "run": ["python3", "-c", "print('ok')"],
                "timeout_seconds": 3,
            }
        ]

        with self.assertRaisesRegex(ValueError, "command_timeout_seconds"):
            resolve_command_timeout_seconds(
                structured_plan,
                "P1",
                ["python3", "-c", "print('ok')"],
                "python3 -c \"print('ok')\"",
                {"command_timeout_seconds": 0},
            )

    def test_resolve_command_timeout_rejects_invalid_structured_gate_timeout(self) -> None:
        structured_plan = plan()
        structured_plan["steps"][0]["gates"] = [
            {
                "kind": "command",
                "run": ["python3", "-c", "print('ok')"],
                "timeout_seconds": False,
            }
        ]

        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            resolve_command_timeout_seconds(
                structured_plan,
                "P1",
                ["python3", "-c", "print('ok')"],
                "python3 -c \"print('ok')\"",
                {"command_timeout_seconds": DEFAULT_COMMAND_TIMEOUT_SECONDS},
            )

    def test_resolve_command_timeout_uses_legacy_validation_alias(self) -> None:
        structured_plan = plan()
        structured_plan["steps"][0]["validation"] = ["legacy validation alias"]
        structured_plan["steps"][0]["gates"] = [
            {
                "kind": "command",
                "run": ["python3", "-c", "print('declared gate')"],
                "timeout_seconds": 3,
            }
        ]

        # Per the plan, naming the legacy validation string for the same
        # structured gate index is enough to select the gate override; it does
        # not depend on command equality.
        self.assertEqual(
            resolve_command_timeout_seconds(
                structured_plan,
                "P1",
                ["python3", "-c", "print('actual command')"],
                "legacy validation alias",
                {"command_timeout_seconds": 1},
            ),
            3,
        )

    def test_resolve_command_timeout_prefers_exact_run_over_earlier_alias(self) -> None:
        structured_plan = plan()
        structured_plan["steps"][0]["validation"] = [
            "gate one legacy alias",
            "gate two legacy alias",
        ]
        structured_plan["steps"][0]["gates"] = [
            {
                "kind": "command",
                "run": ["python3", "-c", "print('first gate')"],
                "timeout_seconds": 2,
            },
            {
                "kind": "command",
                "run": ["python3", "-c", "print('second gate')"],
                "timeout_seconds": 7,
            },
        ]

        self.assertEqual(
            resolve_command_timeout_seconds(
                structured_plan,
                "P1",
                ["python3", "-c", "print('second gate')"],
                "gate one legacy alias",
                {"command_timeout_seconds": 1},
            ),
            7,
        )

    def test_resolve_command_timeout_exact_run_without_override_uses_policy_default(self) -> None:
        structured_plan = plan()
        structured_plan["steps"][0]["validation"] = [
            "gate one legacy alias",
            "gate two legacy alias",
        ]
        structured_plan["steps"][0]["gates"] = [
            {
                "kind": "command",
                "run": ["python3", "-c", "print('first gate')"],
                "timeout_seconds": 2,
            },
            {
                "kind": "command",
                "run": ["python3", "-c", "print('second gate')"],
            },
        ]

        self.assertEqual(
            resolve_command_timeout_seconds(
                structured_plan,
                "P1",
                ["python3", "-c", "print('second gate')"],
                "gate one legacy alias",
                {"command_timeout_seconds": 5},
            ),
            5,
        )

    def test_block_policy_blocks_high_and_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "block")
            # outside.txt is outside the step's effective scope (only fixture.txt
            # is allowed), so the redirect classifies as write_outside_scope=high.
            sentinel = root / "outside.txt"
            receipt = run_command(
                root, plan(), "P1", None,
                ["sh", "-c", "echo written > outside.txt"],
            )
            self.assertEqual(receipt["decision"], "blocked")
            self.assertIsNone(receipt["exit_code"])
            self.assertNotIn("gate", receipt)
            self.assertFalse(sentinel.exists())  # side effect did not happen

    def test_require_confirmation_blocks_without_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "require-confirmation")
            receipt = run_command(root, plan(), "P1", None, ["rm", "-rf", "fixture.txt"])
            self.assertEqual(receipt["decision"], "blocked")

    def test_require_confirmation_allows_with_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "require-confirmation")
            # high-risk (rm -rf) but harmless: target path does not exist, so
            # execution is a no-op with exit 0. Avoids depending on sudo in CI.
            receipt = run_command(
                root, plan(), "P1", None,
                ["sh", "-c", "rm -rf nonexistent-agentflow-test-xyz"],
                confirmed=True, confirmation_source="cli",
            )
            self.assertEqual(receipt["decision"], "allowed")
            self.assertEqual(receipt["confirmed"], True)
            self.assertEqual(receipt["confirmation_source"], "cli")
            self.assertEqual(receipt["risk_policy"], "require-confirmation")

    def test_blocked_receipt_drops_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "block")
            receipt = run_command(
                root, plan(), "P1", None, ["rm", "-rf", "fixture.txt"], gate="g",
            )
            self.assertEqual(receipt["decision"], "blocked")
            self.assertEqual(receipt["timed_out"], False)
            self.assertNotIn("timeout_seconds", receipt)
            self.assertNotIn("gate", receipt)

    def test_record_command_attaches_advisory_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            receipt = record_command(
                root, "P1", None, ["rm", "-rf", "fixture.txt"], 0, plan=plan()
            )
            self.assertEqual(receipt["risk"]["level"], "high")
            self.assertEqual(receipt["decision"], "allowed")  # advisory, never blocks

    def test_run_command_captures_output_hashes_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            receipt = run_command(
                root,
                plan(),
                "P1",
                None,
                ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
                gate="python3 -c \"print('ok')\"",
            )

            self.assertEqual(receipt["id"], "CR1")
            self.assertIn(receipt["provenance"], {"observed", "managed"})
            self.assertEqual(receipt["exit_code"], 0)
            self.assertTrue((root / receipt["stdout_path"]).exists())
            self.assertTrue((root / receipt["stderr_path"]).exists())
            self.assertEqual(verify_receipt_outputs(root), [])

    def test_run_command_times_out_and_records_timeout_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "warn")
            self._set_command_timeout(root, 1)
            command = ["python3", "-c", "print('slow')"]

            with patch(
                "agentflow.receipts.subprocess.run",
                side_effect=subprocess.TimeoutExpired(command, 1),
            ) as run:
                receipt = run_command(
                    root,
                    plan(),
                    "P1",
                    None,
                    command,
                    gate="python3 -c \"print('ok')\"",
                )

            self.assertIsNone(receipt["exit_code"])
            self.assertEqual(receipt["decision"], "timeout")
            self.assertEqual(receipt["timed_out"], True)
            self.assertEqual(receipt["timeout_seconds"], 1)
            self.assertEqual(receipt["risk"]["level"], "low")
            self.assertEqual(receipt["gate"], "python3 -c \"print('ok')\"")
            self.assertEqual(run.call_args.kwargs["timeout"], 1)
            self.assertEqual(command_receipts(root)[0]["decision"], "timeout")

    def test_run_command_timeout_stops_process_before_late_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "warn")
            self._set_command_timeout(root, 1)
            side_effect = root / "late-timeout-side-effect.txt"
            command = [
                "python3",
                "-c",
                (
                    "import pathlib, time; "
                    "time.sleep(2); "
                    "pathlib.Path('late-timeout-side-effect.txt').write_text("
                    "'late', encoding='utf-8')"
                ),
            ]

            receipt = run_command(
                root,
                plan(),
                "P1",
                None,
                command,
            )

            self.assertIsNone(receipt["exit_code"])
            self.assertEqual(receipt["decision"], "timeout")
            self.assertEqual(receipt["timed_out"], True)
            self.assertEqual(receipt["timeout_seconds"], 1)
            self.assertFalse(side_effect.exists())

    def test_run_command_uses_structured_gate_timeout_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "warn")
            self._set_command_timeout(root, 1)
            command = ["python3", "-c", "print('ok')"]
            structured_plan = plan()
            structured_plan["steps"][0]["gates"] = [
                {
                    "kind": "command",
                    "run": command,
                    "timeout_seconds": 3,
                }
            ]

            with patch(
                "agentflow.receipts.subprocess.run",
                return_value=subprocess.CompletedProcess(command, 0, stdout=b"ok\n", stderr=b""),
            ) as run:
                receipt = run_command(
                    root,
                    structured_plan,
                    "P1",
                    None,
                    command,
                    gate="python3 -c \"print('ok')\"",
                )

            self.assertEqual(receipt["decision"], "allowed")
            self.assertEqual(receipt["exit_code"], 0)
            self.assertEqual(receipt["timeout_seconds"], 3)
            self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_run_command_timeout_records_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_risk_policy(root, "warn")
            self._set_command_timeout(root, 1)
            command = ["python3", "-c", "print('partial')"]

            with patch(
                "agentflow.receipts.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    command,
                    1,
                    output=b"partial out\n",
                    stderr=b"partial err\n",
                ),
            ):
                receipt = run_command(
                    root,
                    plan(),
                    "P1",
                    None,
                    command,
                )

            self.assertEqual(receipt["decision"], "timeout")
            self.assertEqual(
                (root / receipt["stdout_path"]).read_text(encoding="utf-8"),
                "partial out\n",
            )
            self.assertEqual(
                (root / receipt["stderr_path"]).read_text(encoding="utf-8"),
                "partial err\n",
            )
            self.assertEqual(verify_receipt_outputs(root), [])

    def test_run_command_honors_record_outputs_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["command_policy"]["record_outputs"] = False
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

            receipt = run_command(
                root,
                plan(),
                "P1",
                None,
                ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            )

            self.assertIsNone(receipt["stdout_path"])
            self.assertIsNone(receipt["stderr_path"])
            self.assertIsNone(receipt["stdout_sha256"])
            self.assertIsNone(receipt["stderr_sha256"])
            self.assertFalse((root / ".agent/receipts/A1/CR1.stdout.txt").exists())
            self.assertFalse((root / ".agent/receipts/A1/CR1.stderr.txt").exists())
            self.assertEqual(verify_receipt_outputs(root), [])

    def test_run_command_honors_capture_stderr_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["command_policy"]["capture_stderr"] = False
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

            receipt = run_command(
                root,
                plan(),
                "P1",
                None,
                ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            )

            self.assertTrue((root / receipt["stdout_path"]).exists())
            self.assertIsNone(receipt["stderr_path"])
            self.assertIsNotNone(receipt["stdout_sha256"])
            self.assertIsNone(receipt["stderr_sha256"])
            self.assertFalse((root / ".agent/receipts/A1/CR1.stderr.txt").exists())
            self.assertEqual(verify_receipt_outputs(root), [])

    def test_failed_run_command_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            receipt = run_command(root, plan(), "P1", None, ["python3", "-c", "raise SystemExit(7)"])

            self.assertEqual(receipt["exit_code"], 7)
            entries = [
                json.loads(line)
                for line in (root / ".agent/command-receipts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(entries[0]["exit_code"], 7)

    def test_record_command_marks_attested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)

            receipt = record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "print('external')"],
                exit_code=0,
                gate="python3 -c \"print('ok')\"",
            )

            self.assertEqual(receipt["provenance"], "attested")
            self.assertIsNone(receipt["stdout_path"])

    def test_verify_receipt_outputs_rejects_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            try:
                receipt = {
                    "schema_version": "0.3.0",
                    "id": "CR1",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["python3", "-c", "print('outside')"],
                    "cwd": ".",
                    "env_names": [],
                    "started_at": "2026-06-01T00:00:00+00:00",
                    "finished_at": "2026-06-01T00:00:00+00:00",
                    "exit_code": 0,
                    "stdout_path": f"../{outside.name}",
                    "stderr_path": None,
                    "stdout_sha256": hashlib.sha256(b"outside\n").hexdigest(),
                    "stderr_sha256": None,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "truncated": False,
                }
                with (root / ".agent/command-receipts.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(receipt) + "\n")

                findings = verify_receipt_outputs(root)
            finally:
                outside.unlink(missing_ok=True)

            self.assertTrue(any("escapes root" in finding["message"] for finding in findings))

    def test_content_addressed_receipt_store_dedupes_identical_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["command_policy"]["receipt_store"] = "content_addressed"
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

            first = run_command(root, plan(), "P1", None, ["python3", "-c", "print('same')"])
            second = run_command(root, plan(), "P1", None, ["python3", "-c", "print('same')"])

            self.assertEqual(first["stdout_path"], second["stdout_path"])
            self.assertTrue(first["stdout_path"].startswith(".agent/receipts/sha256/"))
            self.assertEqual(verify_receipt_outputs(root), [])

    def test_record_file_change_maps_allowed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("hello\n", encoding="utf-8")

            receipt = record_file_change(root, plan(), "P1", None, "fixture.txt")

            self.assertEqual(receipt["id"], "FR1")
            self.assertEqual(receipt["change_kind"], "added")
            self.assertEqual(len(receipt["after_sha256"]), 64)

    def test_record_file_change_rejects_outside_step_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "blocked.txt").write_text("secret\n", encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                record_file_change(root, plan(), "P1", None, "blocked.txt")

            self.assertIn("outside effective file scope", str(ctx.exception))

    def test_record_file_change_rejected_on_terminal_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 already open
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            (root / "fixture.txt").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                record_file_change(root, plan(), "P1", "A1", "fixture.txt")
            self.assertIn("amend-step", str(ctx.exception))

    def test_record_file_change_rejected_on_failed_never_completed_attempt_mentions_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 already open
            fail_step(root, "P1", "A1", "gate failed")
            (root / "fixture.txt").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                record_file_change(root, plan(), "P1", "A1", "fixture.txt")
            self.assertIn("claim-step", str(ctx.exception))

    def test_record_file_change_rejects_attempt_without_opener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 already open
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
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
            (root / "fixture.txt").write_text("changed", encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                record_file_change(root, plan(), "P1", "A2", "fixture.txt")

            self.assertIn("never opened", str(ctx.exception))

    def test_record_file_change_rejects_unknown_or_cross_step_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / "fixture.txt").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                record_file_change(root, plan(), "P1", "A9", "fixture.txt")
            with self.assertRaises(ValueError):
                record_file_change(root, plan(), "P2", "A1", "fixture.txt")

    def test_record_file_change_allowed_on_amendment_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")
            (root / "fixture.txt").write_text("amended", encoding="utf-8")
            receipt = record_file_change(root, plan(), "P1", None, "fixture.txt")
            self.assertEqual(receipt["attempt_id"], "A2")

    def test_amendment_edit_outside_scope_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            amend_step(root, plan(), "P1", "agent-a", "review fix")
            (root / "blocked.txt").write_text("secret\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                record_file_change(root, plan(), "P1", None, "blocked.txt")

    def test_replay_gates_record_allowed_on_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # P1/A1 open
            record_command(
                root, "P1", "A1", ["python3", "-c", "print('ok')"], 0,
                gate="python3 -c \"print('ok')\"", provenance="attested", plan=plan(),
            )
            mark_step_verified(root, "P1", "A1", [])
            complete_step(root, "P1", "A1")
            result = replay_gates(root, plan(), step_id="P1", record=True)
            self.assertEqual(result["status"], "passed")
            self.assertIn(
                "reconstructed", [r.get("provenance") for r in command_receipts(root)]
            )

    def test_replay_matching_attested_gate_records_reconstructed_receipt(self) -> None:
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

            from agentflow.receipts import replay_gates

            result = replay_gates(root, plan(), step_id="P1", record=True)

            self.assertEqual(result["status"], "passed")
            receipts = [
                json.loads(line)
                for line in (root / ".agent/command-receipts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(receipts[-1]["provenance"], "reconstructed")

    def test_replay_mismatch_reports_error_without_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "raise SystemExit(5)"],
                exit_code=0,
                gate="python3 -c \"print('ok')\"",
            )

            from agentflow.receipts import replay_gates

            result = replay_gates(root, plan(), step_id="P1", record=False)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(
                any("exit code mismatch" in finding["message"] for finding in result["errors"])
            )
            receipts = [
                json.loads(line)
                for line in (root / ".agent/command-receipts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(receipts), 1)


    def test_record_file_change_captures_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._commit_seed(root, "fixture.txt", "a\nb\nc\n")
            (root / "fixture.txt").write_text("a\nCHANGED\nc\n", encoding="utf-8")
            receipt = record_file_change(root, plan(), "P1", None, "fixture.txt")
            self.assertEqual(receipt["hunk_attribution"], "hunked")
            self.assertEqual(receipt["diff_engine"], "git")
            self.assertEqual(receipt["diff_command_version"], "afhunk-v1")
            self.assertEqual(len(receipt["hunks"]), 1)
            self.assertEqual(len(receipt["hunks"][0]["hash"]), 64)

    def test_record_file_change_disabled_when_policy_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            self._set_hunk_policy(root, "off")
            self._commit_seed(root, "fixture.txt", "a\nb\nc\n")
            (root / "fixture.txt").write_text("a\nX\nc\n", encoding="utf-8")
            receipt = record_file_change(root, plan(), "P1", None, "fixture.txt")
            self.assertEqual(receipt["hunk_attribution"], "disabled")
            self.assertEqual(receipt["hunks"], [])


if __name__ == "__main__":
    unittest.main()
