import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentflow import cli, porcelain


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")


def _step():
    return {
        "id": "P1",
        "action": "Exercise porcelain fixture.",
        "files": ["src/feature.py"],
        "preconditions": ["Repository initialized."],
        "expected_diff": ["src/feature.py changes are attributed to P1."],
        "validation": ["python3 -c \"print(1)\""],
        "gates": [
            {"kind": "command", "run": ["python3", "-c", "print(1)"]},
        ],
        "evidence_ids": [],
    }


def _plan(steps):
    return {
        "schema_version": "0.3.0",
        "objective": "porcelain fixture",
        "scope": ["exercise execution porcelain"],
        "non_goals": ["no production behavior outside porcelain"],
        "invariants": ["tests pass"],
        "allowed_files": [".agent/", "src/feature.py"],
        "blocked_files": [],
        "validation_gates": ["python3 -c \"print(1)\""],
        "rollback_plan": "git reset",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": steps,
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def _ready_repo_with_steps(root: Path, steps):
    _init_repo(root)
    cli.main(["init", "--root", str(root)])
    plan = _plan(steps)
    (root / ".agent/plan.lock.json").write_text(json.dumps(plan))
    cli.main(["lock-plan", str(root / ".agent/plan.lock.json")])
    cli.main(["init-execution", "--root", str(root)])
    return plan


def _ready_repo(root: Path):
    return _ready_repo_with_steps(root, [_step()])


def _claimed(root: Path):
    _ready_repo(root)
    cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])


def _validated(root: Path):
    _claimed(root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/feature.py").write_text("x = 1\n")
    cli.main(["record-file-change", "--step", "P1", "--path",
              "src/feature.py", "--root", str(root)])
    cli.main(["run", "--step", "P1", "--gate", "python3 -c \"print(1)\"",
              "--root", str(root), "--", "python3", "-c", "print(1)"])


def _completed_step(root: Path):
    _validated(root)
    cli.main(["verify-step", "P1", "--root", str(root)])
    cli.main(["complete-step", "P1", "--root", str(root)])


def _build_verified_proof(root: Path):
    cli.main(["audit-drift", "--root", str(root)])
    cli.main(["verify-run", "--root", str(root)])
    cli.main(["build-proof", "--root", str(root)])
    cli.main(["verify-proof", "--root", str(root)])


def _set_concurrency(root: Path, **values):
    path = root / ".agent/execution.contract.json"
    contract = json.loads(path.read_text())
    contract["concurrency"].update(values)
    path.write_text(json.dumps(contract))


def _recovery(projection, name):
    return next(item for item in projection["recovery_actions"] if item["action"] == name)


def _agent_snapshot(root: Path):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in (root / ".agent").rglob("*")
        if path.is_file()
    }


class TestNextActionEarlyStates(unittest.TestCase):
    def test_uninitialized_when_no_plan(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "uninitialized")
            self.assertEqual(action.args, ["init"])
            self.assertTrue(action.blocking)

    def test_incompatible_plan_schema_reports_structured_diagnostic(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cli.main(["init", "--root", str(root)])
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text())
            plan["schema_version"] = "9.0.0"
            plan_path.write_text(json.dumps(plan))

            action = porcelain.next_action(root)

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertEqual(
                action.resumability["diagnostics"][0]["code"],
                "plan_invalid",
            )

    def test_malformed_locked_plan_members_report_structured_diagnostic(self):
        cases = {
            "unhashable evidence id": ("evidence_ids", [{}]),
            "non-string lock timestamp": ("locked_at", {}),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label), TemporaryDirectory() as d:
                root = Path(d)
                _ready_repo(root)
                plan_path = root / ".agent/plan.lock.json"
                plan = json.loads(plan_path.read_text())
                plan[field] = value
                plan_path.write_text(json.dumps(plan))

                try:
                    action = porcelain.next_action(root, agent_id="worker")
                except Exception as exc:
                    self.fail(
                        f"next_action raised instead of reporting invalid plan: {exc}"
                    )

                self.assertEqual(action.state, "state_invalid")
                self.assertEqual(
                    action.resumability["diagnostics"][0]["code"],
                    "plan_invalid",
                )
                self.assertFalse(any(
                    item["allowed"]
                    for item in action.resumability["recovery_actions"]
                ))

    def test_plan_unlocked_when_lock_false(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cli.main(["init", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "plan_unlocked")
            self.assertIn("lock-plan", action.args)

    def test_non_boolean_lock_is_not_treated_as_locked(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text())
            plan["locked"] = "yes"
            plan_path.write_text(json.dumps(plan))

            action = porcelain.next_action(root, agent_id="worker")

            self.assertEqual(action.state, "plan_unlocked")
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_execution_uninitialized_after_lock(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cli.main(["init", "--root", str(root)])
            plan_path = root / ".agent/plan.lock.json"
            plan = _plan([_step()])
            plan_path.write_text(json.dumps(plan))
            cli.main(["lock-plan", str(plan_path)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "execution_uninitialized")
            self.assertEqual(action.args, ["init-execution"])


class TestNextActionStepUnclaimed(unittest.TestCase):
    def test_step_unclaimed_offers_claim(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_unclaimed")
            self.assertEqual(action.step_id, "P1")
            self.assertEqual(action.args[:2], ["claim-step", "P1"])
            self.assertIn("--agent", action.args)


class TestResumabilityProjection(unittest.TestCase):
    def test_enforced_live_lease_is_actor_specific(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            _set_concurrency(
                root,
                lease_policy="enforce",
                lease_ttl_minutes=30,
                lease_grace_seconds=0,
            )
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])

            owner = porcelain.next_action(root, agent_id="agent-a").resumability
            foreign = porcelain.next_action(root, agent_id="agent-b").resumability
            unknown = porcelain.next_action(root).resumability

            self.assertEqual(owner["attempt"]["owner"], "agent-a")
            self.assertEqual(owner["lease"]["state"], "live")
            self.assertTrue(owner["lease"]["exclusive"])
            self.assertTrue(_recovery(owner, "continue")["allowed"])
            self.assertTrue(_recovery(owner, "renew")["allowed"])
            self.assertFalse(_recovery(owner, "reclaim")["allowed"])
            self.assertFalse(_recovery(foreign, "continue")["allowed"])
            self.assertFalse(_recovery(foreign, "renew")["allowed"])
            self.assertFalse(_recovery(foreign, "reclaim")["allowed"])
            self.assertFalse(_recovery(unknown, "continue")["allowed"])
            self.assertFalse(_recovery(unknown, "renew")["allowed"])

    def test_expired_enforced_lease_permits_owner_renew_and_identified_reclaim(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            _set_concurrency(
                root,
                lease_policy="enforce",
                lease_ttl_minutes=30,
                lease_grace_seconds=0,
            )
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])
            path = root / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            owner = porcelain.next_action(root, agent_id="agent-a").resumability
            foreign = porcelain.next_action(root, agent_id="agent-b").resumability
            unknown = porcelain.next_action(root).resumability

            self.assertEqual(owner["lease"]["state"], "expired")
            self.assertFalse(_recovery(owner, "continue")["allowed"])
            self.assertTrue(_recovery(owner, "renew")["allowed"])
            self.assertTrue(_recovery(owner, "reclaim")["allowed"])
            self.assertFalse(_recovery(foreign, "renew")["allowed"])
            self.assertTrue(_recovery(foreign, "reclaim")["allowed"])
            self.assertFalse(_recovery(unknown, "reclaim")["allowed"])

    def test_advisory_no_deadline_does_not_claim_exclusivity_or_reclaimability(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])

            projection = porcelain.next_action(root, agent_id="agent-b").resumability

            self.assertEqual(projection["lease"]["policy"], "advisory")
            self.assertEqual(projection["lease"]["state"], "no_deadline")
            self.assertFalse(projection["lease"]["exclusive"])
            self.assertTrue(_recovery(projection, "continue")["allowed"])
            self.assertTrue(_recovery(projection, "renew")["allowed"])
            self.assertFalse(_recovery(projection, "reclaim")["allowed"])

    def test_receipts_and_gates_are_scoped_to_the_active_attempt(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])
            (root / "src").mkdir()
            (root / "src/feature.py").write_text("x = 1\n")
            cli.main([
                "record-file-change", "--step", "P1", "--path", "src/feature.py",
                "--root", str(root),
            ])
            cli.main([
                "run", "--step", "P1", "--gate", "python3 -c \"print(1)\"",
                "--root", str(root), "--", "python3", "-c", "print(1)",
            ])
            cli.main([
                "fail-step", "P1", "--attempt", "A1", "--reason", "abandoned",
                "--root", str(root),
            ])
            cli.main(["claim-step", "P1", "--agent", "agent-b", "--root", str(root)])

            projection = porcelain.next_action(root, agent_id="agent-b").resumability

            self.assertEqual(projection["attempt"]["id"], "A2")
            self.assertEqual(projection["receipts"], {"commands": [], "files": []})
            self.assertEqual(projection["gates"][0]["status"], "missing")
            self.assertIsNone(projection["gates"][0]["receipt_id"])

    def test_terminal_attempt_is_not_resumed_renewed_or_reclaimed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])
            cli.main([
                "fail-step", "P1", "--attempt", "A1", "--reason", "failed",
                "--root", str(root),
            ])

            projection = porcelain.next_action(root, agent_id="agent-b").resumability

            self.assertIsNone(projection["attempt"])
            self.assertTrue(_recovery(projection, "claim")["allowed"])
            self.assertFalse(_recovery(projection, "continue")["allowed"])
            self.assertFalse(_recovery(projection, "renew")["allowed"])
            self.assertFalse(_recovery(projection, "reclaim")["allowed"])

    def test_multiple_open_attempts_are_ambiguous_and_offer_no_allowed_action(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])
            cli.main(["claim-step", "P1", "--agent", "agent-b", "--root", str(root)])

            action = porcelain.next_action(root, agent_id="agent-b")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertIsNone(action.resumability["attempt"])
            self.assertEqual(
                action.resumability["diagnostics"][0]["code"],
                "ambiguous_open_attempts",
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_malformed_execution_contract_has_diagnostic_and_no_recovery(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            (root / ".agent/execution.contract.json").write_text("{")

            action = porcelain.next_action(root, agent_id="agent-a")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertEqual(
                action.resumability["diagnostics"][0]["code"],
                "execution_contract_invalid",
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_missing_execution_ledger_has_diagnostic_and_no_recovery(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            (root / ".agent/step-runs.jsonl").unlink()

            action = porcelain.next_action(root, agent_id="worker")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertEqual(
                action.resumability["diagnostics"][0]["code"],
                "execution_state_invalid",
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_non_object_execution_ledger_row_has_diagnostic_and_no_recovery(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            (root / ".agent/step-runs.jsonl").write_text("[]\n")

            try:
                action = porcelain.next_action(root, agent_id="worker")
            except Exception as exc:  # Regression guard: diagnostics, never a traceback.
                self.fail(f"next_action raised instead of reporting invalid state: {exc}")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_attempt_id_reused_across_steps_is_invalid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            first = _step()
            second = _step()
            second["id"] = "P2"
            _ready_repo_with_steps(root, [first, second])
            rows = [
                {
                    "schema_version": "0.5.0",
                    "event": "claimed",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "agent_id": "agent-a",
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "schema_version": "0.5.0",
                    "event": "claimed",
                    "step_id": "P2",
                    "attempt_id": "A1",
                    "agent_id": "agent-b",
                    "recorded_at": "2026-01-01T00:01:00+00:00",
                },
            ]
            (root / ".agent/step-runs.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )

            action = porcelain.next_action(root, agent_id="agent-b")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertIn(
                "belongs to multiple steps",
                action.resumability["diagnostics"][0]["message"],
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_resumable_event_without_opener_is_invalid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            event = {
                "schema_version": "0.5.0",
                "event": "verified",
                "step_id": "P1",
                "attempt_id": "A1",
                "recorded_at": "2026-01-01T00:00:00+00:00",
            }
            (root / ".agent/step-runs.jsonl").write_text(
                json.dumps(event) + "\n"
            )

            action = porcelain.next_action(root, agent_id="worker")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertIn(
                "has no opening event",
                action.resumability["diagnostics"][0]["message"],
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_populated_receipts_and_gates_project_declared_fields(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)

            projection = porcelain.next_action(root, agent_id="tester").resumability

            self.assertEqual(len(projection["receipts"]["commands"]), 1)
            command = projection["receipts"]["commands"][0]
            self.assertEqual(
                set(command),
                {"id", "gate", "command", "exit_code", "decision",
                 "timed_out", "provenance", "finished_at"},
            )
            self.assertEqual(command["id"], "CR1")
            self.assertEqual(command["exit_code"], 0)
            self.assertEqual(command["decision"], "allowed")
            self.assertFalse(command["timed_out"])
            self.assertEqual(len(projection["receipts"]["files"]), 1)
            file_receipt = projection["receipts"]["files"][0]
            self.assertEqual(
                set(file_receipt),
                {"id", "path", "change_kind", "recorded_at"},
            )
            self.assertEqual(file_receipt["path"], "src/feature.py")
            self.assertEqual(projection["gates"][0]["status"], "satisfied")
            self.assertEqual(projection["gates"][0]["receipt_id"], "CR1")

    def test_ledger_referencing_unknown_step_is_invalid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            path = root / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            ghost = dict(rows[0])
            ghost.update(step_id="P9", attempt_id="A9")
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in [*rows, ghost])
            )

            action = porcelain.next_action(root, agent_id="tester")

            self.assertEqual(action.state, "state_invalid")
            self.assertIn(
                "unknown steps: P9",
                action.resumability["diagnostics"][0]["message"],
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_enforced_attempt_without_owner_is_invalid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            _set_concurrency(root, lease_policy="enforce")
            event = {
                "schema_version": "0.5.0",
                "event": "claimed",
                "step_id": "P1",
                "attempt_id": "A1",
                "recorded_at": "2026-01-01T00:00:00+00:00",
            }
            (root / ".agent/step-runs.jsonl").write_text(json.dumps(event) + "\n")

            action = porcelain.next_action(root, agent_id="tester")

            self.assertEqual(action.state, "state_invalid")
            self.assertIn(
                "has no owner",
                action.resumability["diagnostics"][0]["message"],
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_direct_projection_of_non_dict_plan_is_diagnosed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)

            projection = porcelain.resumability_projection(root, ["not", "a", "plan"])

            self.assertEqual(projection["diagnostics"][0]["code"], "plan_invalid")
            self.assertFalse(any(
                item["allowed"] for item in projection["recovery_actions"]
            ))

    def test_contract_hash_race_is_diagnosed_not_raised(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            with mock.patch.object(
                porcelain, "sha256_path", side_effect=OSError("contract unlinked")
            ):
                action = porcelain.next_action(root, agent_id="tester")

            self.assertEqual(action.state, "state_invalid")
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_completed_event_without_opener_cannot_unlock_dependency(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            first = _step()
            second = _step()
            second["id"] = "P2"
            second["depends_on"] = ["P1"]
            _ready_repo_with_steps(root, [first, second])
            event = {
                "schema_version": "0.5.0",
                "event": "completed",
                "step_id": "P1",
                "attempt_id": "A1",
                "recorded_at": "2026-01-01T00:00:00+00:00",
            }
            (root / ".agent/step-runs.jsonl").write_text(
                json.dumps(event) + "\n"
            )

            action = porcelain.next_action(root, agent_id="worker")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertIn(
                "has no opening event",
                action.resumability["diagnostics"][0]["message"],
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_foreign_enforced_lease_renewal_is_invalid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            _set_concurrency(root, lease_policy="enforce", lease_grace_seconds=0)
            cli.main([
                "claim-step", "P1", "--agent", "owner", "--root", str(root),
            ])
            path = root / ".agent/step-runs.jsonl"
            renewal = {
                "schema_version": "0.5.0",
                "event": "lease_renewed",
                "step_id": "P1",
                "attempt_id": "A1",
                "agent_id": "foreign",
                "lease_expires_at": "2099-01-01T00:00:00+00:00",
                "recorded_at": "2026-01-01T00:01:00+00:00",
            }
            path.write_text(path.read_text() + json.dumps(renewal) + "\n")

            action = porcelain.next_action(root, agent_id="owner")

            self.assertEqual(action.state, "state_invalid")
            self.assertIsNone(action.command)
            self.assertIn(
                "renewed by foreign instead of owner",
                action.resumability["diagnostics"][0]["message"],
            )
            self.assertFalse(any(
                item["allowed"] for item in action.resumability["recovery_actions"]
            ))

    def test_incomplete_receipt_rows_are_invalid(self):
        cases = {
            "command-receipts.jsonl": {
                "schema_version": "0.4.0",
                "step_id": "P1",
                "attempt_id": "A1",
                "gate": "python3 -c print(1)",
                "exit_code": 0,
            },
            "file-receipts.jsonl": {
                "schema_version": "0.4.0",
                "step_id": "P1",
                "attempt_id": "A1",
                "path": "src/feature.py",
                "change_kind": "modified",
                "recorded_at": "2026-01-01T00:00:00+00:00",
            },
        }
        for ledger, receipt in cases.items():
            with self.subTest(ledger=ledger), TemporaryDirectory() as d:
                root = Path(d)
                _ready_repo(root)
                cli.main([
                    "claim-step", "P1", "--agent", "worker", "--root", str(root),
                ])
                (root / ".agent" / ledger).write_text(
                    json.dumps(receipt) + "\n"
                )

                action = porcelain.next_action(root, agent_id="worker")

                self.assertEqual(action.state, "state_invalid")
                self.assertIsNone(action.command)
                self.assertIn(
                    "receipt",
                    action.resumability["diagnostics"][0]["message"],
                )
                self.assertFalse(any(
                    item["allowed"]
                    for item in action.resumability["recovery_actions"]
                ))

    def test_projection_is_read_only(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])
            before = _agent_snapshot(root)

            action = porcelain.next_action(root, agent_id="agent-a")

            self.assertIn("plan_sha256", action.resumability["contract"])
            self.assertIn("execution_contract_sha256", action.resumability["contract"])
            self.assertEqual(_agent_snapshot(root), before)


class TestNextActionInflightWork(unittest.TestCase):
    def test_command_rendering_shell_quotes_gate_but_expands_user(self):
        command = porcelain._cmd([
            "run", "--gate", 'python3 -c "print(1)"',
            "--agent", "$USER", "--", "python3", "-c", "print(1)",
        ])
        self.assertIn("'python3 -c \"print(1)\"'", command)
        self.assertIn("--agent $USER", command)
        self.assertIn("'print(1)'", command)

    def test_file_receipts_missing_when_scoped_edit_unrecorded(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")  # scoped, unrecorded
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "file_receipts_missing")
            self.assertEqual(action.args[:3], ["record-file-change", "--step", "P1"])
            self.assertIn("src/feature.py", action.args)

    def test_validation_missing_when_files_recorded_but_gate_unmet(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")
            cli.main(["record-file-change", "--step", "P1", "--path",
                      "src/feature.py", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "validation_missing")
            self.assertEqual(action.args[:2], ["run", "--step"])
            self.assertEqual(action.step_id, "P1")

    def test_validation_missing_points_to_unmet_later_gate(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            step = _step()
            step["validation"] = [
                "python3 -c \"print(1)\"",
                "python3 -c \"print(2)\"",
            ]
            step["gates"] = [
                {"kind": "command", "run": ["python3", "-c", "print(1)"]},
                {"kind": "command", "run": ["python3", "-c", "print(2)"]},
            ]
            _ready_repo_with_steps(root, [step])
            cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")
            cli.main(["record-file-change", "--step", "P1", "--path",
                      "src/feature.py", "--root", str(root)])
            cli.main(["run", "--step", "P1", "--gate", "python3 -c \"print(1)\"",
                      "--root", str(root), "--", "python3", "-c", "print(1)"])

            action = porcelain.next_action(root)

            self.assertEqual(action.state, "validation_missing")
            self.assertEqual(action.gate, "python3 -c print(2)")
            self.assertEqual(action.args[-3:], ["python3", "-c", "print(2)"])

    def test_non_strict_step_warning_continues_to_finish_step(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            warning = {"severity": "warning", "message": "non-blocking warning"}
            with mock.patch(
                "agentflow.porcelain.verify_step",
                return_value={"status": "warning", "errors": [], "warnings": [warning]},
            ):
                action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_unverified")
            self.assertEqual(action.args, ["finish-step", "P1"])

    def test_strict_step_warning_reports_validation_missing(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            warning = {"severity": "warning", "message": "strict warning"}
            with mock.patch(
                "agentflow.porcelain.verify_step",
                return_value={"status": "warning", "errors": [], "warnings": [warning]},
            ):
                action = porcelain.next_action(root, strict=True)
            self.assertEqual(action.state, "validation_missing")
            self.assertIn("strict warning", action.diagnostics)


class TestNextActionVerifyComplete(unittest.TestCase):
    def test_step_unverified_offers_finish_step(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_unverified")
            self.assertEqual(action.args, ["finish-step", "P1"])

    def test_step_uncompleted_after_verify(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            cli.main(["verify-step", "P1", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "step_uncompleted")
            self.assertEqual(action.args, ["finish-step", "P1"])


class TestNextActionRunStates(unittest.TestCase):
    def test_run_unverified_after_all_steps_complete(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "run_unverified")
            self.assertEqual(action.args, ["finish-run"])

    def test_strict_next_action_ignores_stuck_advisory_run_warning(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            (root / ".agent/proof-pack.json").write_text("{}\n")
            warning = {
                "severity": "warning",
                "message": "stuck advisory",
                "rule": "repeated_command_failure",
            }
            with mock.patch(
                "agentflow.porcelain.verify_run",
                return_value={"status": "warning", "errors": [], "warnings": [warning]},
            ), mock.patch(
                "agentflow.porcelain._latest_run_verification",
                return_value={"status": "warning"},
            ), mock.patch(
                "agentflow.porcelain.verify_proof",
                return_value=[],
            ):
                action = porcelain.next_action(root, strict=True)
            self.assertEqual(action.state, "complete")

    def test_drift_failing_precedes_run_unverified(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            (root / "rogue.txt").write_text("out of scope\n")
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "drift_failing")
            self.assertEqual(action.args, ["finish-run"])

    def test_proof_missing_before_build(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            cli.main(["verify-run", "--root", str(root)])
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_missing")

    def test_proof_stale_after_source_changes(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            _build_verified_proof(root)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text())
            plan["objective"] = "changed after proof"
            plan_path.write_text(json.dumps(plan, indent=2))
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_stale")

    def test_proof_failing_for_invalid_metadata(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            _build_verified_proof(root)
            (root / ".agent/proof-pack.json").write_text("{}\n")  # was valid, now corrupt metadata
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_failing")

    def test_complete_when_proof_verified(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            _build_verified_proof(root)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "complete")
            self.assertFalse(action.blocking)
            self.assertIsNone(action.command)


class TestFinishStep(unittest.TestCase):
    def test_finish_step_verifies_and_completes(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            plan = json.loads((root / ".agent/plan.lock.json").read_text())
            result = porcelain.finish_step(root, plan, "P1", None)
            self.assertTrue(result["verified"])
            self.assertTrue(result["completed"])
            self.assertEqual(result["verification_status"], "passed")

    def test_finish_step_does_not_complete_on_failure(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])
            plan = json.loads((root / ".agent/plan.lock.json").read_text())
            result = porcelain.finish_step(root, plan, "P1", None, strict=True)
            self.assertFalse(result["completed"])
            self.assertFalse(result["verified"])
            self.assertTrue(result["diagnostics"])


class TestFinishRun(unittest.TestCase):
    def test_finish_run_all_green(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")
            self.assertTrue(result["ok"])
            self.assertIsNone(result["stopped_at"])
            self.assertEqual([g["name"] for g in result["gates"]],
                             ["audit-drift", "verify-run", "build-proof", "verify-proof"])

    def test_finish_run_stops_at_first_failing_gate(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            # Introduce an out-of-scope change so audit-drift fails.
            (root / "rogue.txt").write_text("x\n")
            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")
            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_at"], "audit-drift")
            self.assertEqual(len(result["gates"]), 1)
            # Diagnostics carry the failing gate's real output, not a swallowed
            # "see command output" pointer.
            self.assertTrue(result["diagnostics"])
            self.assertTrue(any("rogue.txt" in line for line in result["diagnostics"]))

    def test_finish_run_passes_plan_to_verify_run(self):
        calls = []

        def fake_main(argv):
            calls.append(argv)
            return 0

        root = Path("/tmp/agentflow-fixture")
        plan_path = root / "custom-plan.json"
        with mock.patch("agentflow.cli.main", side_effect=fake_main):
            result = porcelain.finish_run(root, plan_path)

        self.assertTrue(result["ok"])
        self.assertEqual(
            calls[1],
            ["verify-run", "--root", str(root), "--plan", str(plan_path)],
        )

    def test_finish_run_stops_at_each_gate(self):
        gate_order = ["audit-drift", "verify-run", "build-proof", "verify-proof"]
        for idx, failing in enumerate(gate_order):
            calls = {"n": 0}
            def fake_main(argv, _idx=idx, calls=calls):
                rc = 1 if calls["n"] == _idx else 0
                calls["n"] += 1
                return rc
            with mock.patch("agentflow.cli.main", side_effect=fake_main):
                result = porcelain.finish_run(Path("/tmp"), Path("/tmp/p.json"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_at"], failing)
            self.assertEqual([g["name"] for g in result["gates"]], gate_order[:idx + 1])


if __name__ == "__main__":
    unittest.main()
