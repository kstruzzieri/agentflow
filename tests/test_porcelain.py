import contextlib
import io
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentflow.artifacts import append_jsonl, create_initial_artifacts, write_json
from agentflow.cli_contract import JSON_OUTPUTS
from agentflow.execution import (
    amend_step, block_step, claim_step, complete_step, eligible_step, fail_step,
    init_execution_artifacts, mark_step_verified, next_step, read_step_events,
    read_step_state, reclaim_step, renew_lease, step_state_from_events,
)
from agentflow.contracts import (
    COMMAND_RECEIPTS_SCHEMA_VERSION,
    FILE_RECEIPTS_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    STEP_RUNS_SCHEMA_VERSION,
)
from agentflow import cli, execution_coverage, mcp_server, porcelain


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
        "schema_version": PLAN_SCHEMA_VERSION,
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


def _ready_repo_with_steps(root: Path, steps, extra_allowed: tuple = ()):
    _init_repo(root)
    cli.main(["init", "--root", str(root)])
    plan = _plan(steps)
    plan["allowed_files"].extend(extra_allowed)
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


def _time_out_open_attempt(root: Path, agent: str = "tester") -> None:
    path = root / ".agent/execution.contract.json"
    contract = json.loads(path.read_text())
    contract["command_policy"]["command_timeout_seconds"] = 1
    path.write_text(json.dumps(contract))
    rc = cli.main(["run", "--step", "P1", "--agent", agent, "--gate", 'python3 -c "print(1)"',
                   "--root", str(root), "--", "python3", "-c", "import time; time.sleep(5)"])
    assert rc == 124, rc


def _timeout_then_rerun(root: Path) -> None:
    """#70: P1/A1's gate times out, then a passing rerun lands in the same attempt."""
    _claimed(root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/feature.py").write_text("x = 1\n")
    cli.main(["record-file-change", "--step", "P1", "--path", "src/feature.py", "--root", str(root)])
    _time_out_open_attempt(root)
    assert cli.main(["run", "--step", "P1", "--agent", "tester", "--gate", 'python3 -c "print(1)"',
                     "--root", str(root), "--", "python3", "-c", "print(1)"]) == 0


def _legacy_complete(root: Path, attempt: str) -> None:
    """1.0 let a timed-out attempt complete; #70 refuses, so write the row directly."""
    append_jsonl(root / ".agent/step-runs.jsonl", {
        "schema_version": STEP_RUNS_SCHEMA_VERSION, "recorded_at": "2026-10-16T00:00:00+00:00",
        "event": "completed", "step_id": "P1", "attempt_id": attempt,
    })
    (root / ".agent/attempts/P1.current").unlink(missing_ok=True)


def _timed_out_gate(root: Path, retry_in_new_attempt: bool):
    """#60: P1/A1's gate times out. Then either fail A1 and complete P1 in A2,
    or rerun the gate inside A1 and complete A1 (the timeout stays unsuperseded)."""
    _claimed(root)  # A1
    path = root / ".agent/execution.contract.json"
    contract = json.loads(path.read_text())
    contract["command_policy"]["command_timeout_seconds"] = 1
    path.write_text(json.dumps(contract))
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/feature.py").write_text("x = 1\n")
    gate = "python3 -c \"print(1)\""
    record = ["record-file-change", "--step", "P1", "--path", "src/feature.py", "--root", str(root)]
    cli.main(record)
    cli.main(["run", "--step", "P1", "--gate", gate, "--root", str(root),
              "--", "python3", "-c", "import time; time.sleep(5)"])
    if retry_in_new_attempt:
        cli.main(["fail-step", "P1", "--reason", "gate timed out", "--root", str(root)])
        cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])  # A2
        cli.main(record)
    cli.main(["run", "--step", "P1", "--gate", gate, "--root", str(root),
              "--", "python3", "-c", "print(1)"])
    cli.main(["verify-step", "P1", "--root", str(root)])
    if retry_in_new_attempt:
        cli.main(["complete-step", "P1", "--root", str(root)])
    else:
        _legacy_complete(root, "A1")


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


def _enforced_claim(root: Path, agent: str = "worker-7") -> None:
    _ready_repo(root)
    _set_concurrency(root, lease_policy="enforce")
    assert cli.main(["claim-step", "P1", "--agent", agent, "--root", str(root)]) == 0
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/feature.py").write_text("x = 1\n")
    assert cli.main(["record-file-change", "--step", "P1", "--path", "src/feature.py",
                     "--agent", agent, "--root", str(root)]) == 0


def _set_attempt_lease(root: Path, attempt: str, expires_at) -> None:
    """Rewrite every lease row of `attempt` (None = no deadline). `run` renews leases."""
    path = root / ".agent/step-runs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row.get("attempt_id") == attempt and "lease_expires_at" in row:
            if expires_at is None:
                row.pop("lease_expires_at")
            else:
                row["lease_expires_at"] = expires_at
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _closed_amendment(root: Path, close: str, timeout: bool) -> None:
    """P1 completed as A1 (CR1), then amendment A2 (timeout = CR2) closed by `close`."""
    _completed_step(root)
    assert cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "review fix",
                     "--root", str(root)]) == 0
    if timeout:
        _time_out_open_attempt(root)
    assert cli.main([close, "P1", "--reason", "closed", "--agent", "tester", "--root", str(root)]) == 0


def _locked_plan(root: Path):
    return json.loads((root / ".agent/plan.lock.json").read_text())


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

    def test_closing_a_terminal_attempt_is_refused_and_workspace_stays_valid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            cli.main(["claim-step", "P1", "--agent", "agent-a", "--root", str(root)])
            cli.main([
                "fail-step", "P1", "--attempt", "A1", "--reason", "failed",
                "--root", str(root),
            ])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli.main([
                    "block-step", "P1", "--attempt", "A1", "--reason", "late block",
                    "--root", str(root),
                ])

            self.assertEqual(code, 2)
            self.assertIn("is already failed", stderr.getvalue())
            action = porcelain.next_action(root, agent_id="agent-b")
            self.assertNotEqual(action.state, "state_invalid")
            self.assertTrue(_recovery(action.resumability, "claim")["allowed"])

    def test_verify_step_on_a_blocked_attempt_writes_nothing(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)
            cli.main([
                "block-step", "P1", "--attempt", "A1", "--reason", "blocked",
                "--root", str(root),
            ])
            step_runs = root / ".agent/step-runs.jsonl"
            verification_runs = root / ".agent/verification-runs.jsonl"
            before = (step_runs.read_bytes(), verification_runs.read_bytes())
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli.main(["verify-step", "P1", "--attempt", "A1", "--root", str(root)])

            self.assertEqual(code, 2)
            self.assertIn("is already blocked", stderr.getvalue())
            self.assertEqual((step_runs.read_bytes(), verification_runs.read_bytes()), before)

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
                    "schema_version": STEP_RUNS_SCHEMA_VERSION,
                    "event": "claimed",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "agent_id": "agent-a",
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "schema_version": STEP_RUNS_SCHEMA_VERSION,
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
                "schema_version": STEP_RUNS_SCHEMA_VERSION,
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
                "schema_version": STEP_RUNS_SCHEMA_VERSION,
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
                "schema_version": STEP_RUNS_SCHEMA_VERSION,
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
                "schema_version": STEP_RUNS_SCHEMA_VERSION,
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
                "schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION,
                "step_id": "P1",
                "attempt_id": "A1",
                "gate": "python3 -c print(1)",
                "exit_code": 0,
            },
            "file-receipts.jsonl": {
                "schema_version": FILE_RECEIPTS_SCHEMA_VERSION,
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
            "--agent", "$USER", "--", "python3", "-c", "print(1)", "$USER",
        ], user_placeholder=True)
        self.assertIn("'python3 -c \"print(1)\"'", command)
        self.assertIn("--agent $USER", command)
        self.assertIn("'print(1)'", command)
        self.assertTrue(command.endswith("'$USER'"))

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

    def test_finish_run_stops_at_verify_run_on_open_attempt(self):
        # #58: non-strict finish-run used to pass here and write a proof.
        with TemporaryDirectory() as d:
            root = Path(d)
            _validated(root)  # P1/A1 claimed, file recorded, gate passed, never completed
            proof = root / ".agent/proof-pack.json"
            before = proof.read_bytes() if proof.exists() else None

            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")

            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_at"], "verify-run")
            self.assertTrue(any("P1 has open attempt A1" in line for line in result["diagnostics"]))
            self.assertTrue(any("P1 is not completed" in line for line in result["diagnostics"]))
            self.assertEqual(proof.read_bytes() if proof.exists() else None, before)

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


class TestSupersededTimeout(unittest.TestCase):
    """#60: a timeout that a later completed attempt superseded stops blocking the run."""

    def _decisions(self, root: Path):
        lines = (root / ".agent/command-receipts.jsonl").read_text().splitlines()
        return [json.loads(line)["decision"] for line in lines]

    def test_finish_run_passes_after_superseded_timeout(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timed_out_gate(root, retry_in_new_attempt=True)
            self.assertEqual(self._decisions(root), ["timeout", "allowed"])

            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")

            self.assertTrue(result["ok"], result["diagnostics"])
            proof = json.loads((root / ".agent/proof-pack.json").read_text())
            self.assertEqual(proof["execution"]["command_timed_out"], 1)

    def test_strict_verify_run_gate_and_next_action_accept_superseded_timeout(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timed_out_gate(root, retry_in_new_attempt=True)

            result = porcelain.finish_run(root, root / ".agent/plan.lock.json", strict=True)

            self.assertEqual(result["gates"][1], {"name": "verify-run", "status": "ok"})
            # Strict finish-run can still stop at build-proof here: strict mode turns
            # the steps_without_support proof-check warning into a failure. #60
            # concerns verify-run only.
            self.assertNotEqual(result["stopped_at"], "verify-run")
            self.assertNotEqual(porcelain.next_action(root, strict=True).state, "run_unverified")

    def _timeout_current_attempt(self, root):
        path = root / ".agent/execution.contract.json"
        contract = json.loads(path.read_text())
        contract["command_policy"]["command_timeout_seconds"] = 1
        path.write_text(json.dumps(contract))
        self.assertEqual(cli.main([
            "run", "--step", "P1", "--agent", "tester", "--gate", 'python3 -c "print(1)"',
            "--root", str(root), "--", "python3", "-c", "import time; time.sleep(5)",
        ]), 124)

    def _finish_recovered_attempt(self, root):
        self.assertEqual(cli.main([
            "run", "--step", "P1", "--agent", "tester", "--gate", 'python3 -c "print(1)"',
            "--root", str(root), "--", "python3", "-c", "print(1)",
        ]), 0)
        self.assertEqual(cli.main([
            "finish-step", "P1", "--agent", "tester", "--root", str(root),
        ]), 0)
        result = porcelain.finish_run(root, root / ".agent/plan.lock.json")
        self.assertTrue(result["ok"], result["diagnostics"])
        self.assertEqual(porcelain.next_action(root).state, "complete")
        proof = json.loads((root / ".agent/proof-pack.json").read_text())
        self.assertEqual(proof["execution"]["command_timed_out"], 1)

    def test_reclaim_opens_the_timeout_replacement_without_another_claim(self):
        for policy in ("advisory", "enforce"):
            with self.subTest(policy=policy), TemporaryDirectory() as d:
                root = Path(d)
                _ready_repo(root)
                _set_concurrency(root, lease_policy=policy)
                self.assertEqual(cli.main([
                    "claim-step", "P1", "--agent", "tester", "--lease-minutes", "1",
                    "--root", str(root),
                ]), 0)
                self._timeout_current_attempt(root)
                # Expire the latest lease without waiting for the real deadline.
                path = root / ".agent/step-runs.jsonl"
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                rows[-1]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))

                self.assertEqual(cli.main([
                    "reclaim-step", "P1", "--agent", "tester", "--reason", "expired timeout",
                    "--root", str(root),
                ]), 0)
                self._finish_recovered_attempt(root)

                self.assertEqual(self._decisions(root), ["timeout", "allowed"])

    def test_failed_amendment_timeout_recovers_with_another_amendment(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            self.assertEqual(cli.main([
                "amend-step", "P1", "--agent", "tester", "--reason", "review fix",
                "--root", str(root),
            ]), 0)
            self._timeout_current_attempt(root)
            self.assertEqual(cli.main([
                "fail-step", "P1", "--reason", "gate timed out", "--root", str(root),
            ]), 0)

            self.assertEqual(cli.main([
                "amend-step", "P1", "--agent", "tester", "--reason", "retry review fix",
                "--root", str(root),
            ]), 0)
            self._finish_recovered_attempt(root)

            self.assertEqual(self._decisions(root), ["allowed", "timeout", "allowed"])

    def test_timeout_in_the_completed_attempt_still_blocks_the_run(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timed_out_gate(root, retry_in_new_attempt=False)
            self.assertEqual(self._decisions(root), ["timeout", "allowed"])

            result = porcelain.finish_run(root, root / ".agent/plan.lock.json")

            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_at"], "verify-run")
            self.assertTrue(any("timed out" in line for line in result["diagnostics"]))
            self.assertEqual(porcelain.next_action(root, strict=True).state, "run_unverified")


class TestTimedOutAttemptRefusal(unittest.TestCase):
    """#70: an attempt holding a timed-out receipt can never be completed."""

    LEDGERS = ("step-runs.jsonl", "verification-runs.jsonl", "command-receipts.jsonl")

    def _ledgers(self, root):
        return {name: (root / ".agent" / name).read_bytes()
                if (root / ".agent" / name).exists() else b"" for name in self.LEDGERS}

    def _cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_complete_step_refuses_a_verified_timed_out_attempt(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timeout_then_rerun(root)
            self.assertEqual(cli.main(["verify-step", "P1", "--root", str(root)]), 0)
            before = self._ledgers(root)
            rc, out, err = self._cli(["complete-step", "P1", "--root", str(root)])
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("attempt A1 for step P1 holds timed-out command receipt CR1 "
                          "and cannot complete", err)
            self.assertIn("nothing was recorded", err)
            self.assertEqual(self._ledgers(root), before)

    def test_complete_step_names_the_timeout_before_verification(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timeout_then_rerun(root)
            rc, _out, err = self._cli(["complete-step", "P1", "--root", str(root)])
            self.assertEqual(rc, 1)
            self.assertIn("holds timed-out command receipt CR1", err)
            self.assertNotIn("verify-step must pass", err)

    def test_finish_step_refuses_without_writing_or_replaying(self):
        for extra in ([], ["--json"], ["--replay"]):
            with self.subTest(extra=extra), TemporaryDirectory() as d:
                root = Path(d)
                _timeout_then_rerun(root)
                before = self._ledgers(root)
                for _ in range(2):  # a repeated refusal is stable
                    rc, out, err = self._cli(["finish-step", "P1", "--root", str(root), *extra])
                    self.assertEqual((rc, out), (1, ""))
                    self.assertNotIn("Traceback", err)
                    self.assertIn("holds timed-out command receipt CR1", err)
                self.assertEqual(self._ledgers(root), before)

    def test_mcp_completion_tools_report_the_refusal_envelope(self):
        for tool in ("complete_step", "finish_step"):
            with self.subTest(tool=tool), TemporaryDirectory() as d:
                root = Path(d)
                _timeout_then_rerun(root)
                before = self._ledgers(root)
                result = mcp_server.handle_message({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool, "arguments": {"step": "P1", "root": str(root)}},
                })["result"]
                structured = result["structuredContent"]
                self.assertTrue(result["isError"])
                self.assertEqual((structured["exit_code"], structured["stdout"]), (1, ""))
                self.assertNotIn("data", structured)
                self.assertIn("holds timed-out command receipt CR1", structured["stderr"])
                self.assertEqual(self._ledgers(root), before)

    def test_mcp_block_step_closes_the_timed_out_attempt(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timeout_then_rerun(root)
            result = mcp_server.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "block_step", "arguments": {
                    "step": "P1", "attempt": "A1", "reason": "gate timed out", "root": str(root)}},
            })["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["data"]["event"], "blocked")
            self.assertEqual(porcelain.next_action(root, agent_id="tester").state, "step_unclaimed")

    def test_mcp_block_step_refuses_a_foreign_agent_under_enforce(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _enforced_claim(root)
            _time_out_open_attempt(root, "worker-7")
            result = mcp_server.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "block_step", "arguments": {
                    "step": "P1", "reason": "r", "agent": "worker-8", "root": str(root)}},
            })["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["exit_code"], 2)
            self.assertIn("owned by worker-7", result["structuredContent"]["stderr"])


class TestRecoverySelector(unittest.TestCase):
    """#70: one selector drives the projection's step and recovery actions."""

    def test_timed_out_attempt_close_follows_policy_lease_and_caller(self):
        past = "2000-01-01T00:00:00+00:00"
        inside_grace = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        cases = [  # (policy, lease, agent, close)
            ("advisory", None, None, "block"),
            ("enforce", "live", "worker-7", "block"),
            ("enforce", "no_deadline", "worker-7", "block"),
            ("enforce", "grace", "worker-7", "block"),
            ("enforce", "expired", "worker-7", "reclaim"),
            ("enforce", "expired", "worker-8", "reclaim"),
            ("enforce", "expired", None, None),
            ("enforce", "live", "worker-8", None),
            ("enforce", "live", None, None),
            ("enforce", "no_deadline", "worker-8", None),
        ]
        for policy, lease, agent, close in cases:
            with self.subTest(policy=policy, lease=lease, agent=agent), TemporaryDirectory() as d:
                root = Path(d)
                if policy == "advisory":
                    _claimed(root)
                    _time_out_open_attempt(root)
                else:
                    _enforced_claim(root)
                    _time_out_open_attempt(root, "worker-7")
                    if lease != "live":
                        _set_attempt_lease(root, "A1", {"no_deadline": None, "grace": inside_grace,
                                                        "expired": past}[lease])
                projection, selection = porcelain._resumability(root, _locked_plan(root), agent_id=agent)
                self.assertEqual((selection.kind, selection.close), ("timed_out", close))
                self.assertEqual(_recovery(projection, "block")["allowed"], close == "block")
                self.assertEqual(_recovery(projection, "reclaim")["allowed"], close == "reclaim")
                self.assertFalse(_recovery(projection, "continue")["allowed"])
                self.assertTrue(_recovery(projection, "fail")["break_glass"])
                for name in ("block", "amend"):
                    self.assertTrue(_recovery(projection, name)["automatic"])
                    self.assertFalse(_recovery(projection, name)["break_glass"])
                if close == "block":
                    self.assertEqual(_recovery(projection, "block")["reason"],
                                     "attempt holds a timed-out receipt and cannot complete")
                if (lease, agent) == ("live", "worker-8"):
                    self.assertIn("only the owner can block it", _recovery(projection, "block")["reason"])

    def test_completed_step_needing_replacement_projects_amend_not_claim(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _closed_amendment(root, "fail-step", timeout=False)
            projection, selection = porcelain._resumability(root, _locked_plan(root), agent_id="tester")
            self.assertEqual((selection.kind, selection.step_id), ("amend", "P1"))
            self.assertEqual(projection["step"], {"id": "P1", "state": "failed", "completed": True})
            self.assertIsNone(projection["attempt"])
            self.assertTrue(_recovery(projection, "amend")["allowed"])
            self.assertFalse(_recovery(projection, "claim")["allowed"])
            self.assertEqual(_recovery(projection, "claim")["reason"], "step has completed work; use amend")
            anonymous, _ = porcelain._resumability(root, _locked_plan(root))
            self.assertEqual(_recovery(anonymous, "amend")["reason"], "amend requires an agent identity")

    def test_claimable_step_beats_a_replacement_and_amend_denial_says_so(self):
        p2 = {**_step(), "id": "P2", "files": ["src/other.py"],
              "expected_diff": ["src/other.py changes are attributed to P2."]}
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo_with_steps(root, [_step(), p2], extra_allowed=("src/other.py",))
            (root / "src").mkdir()
            cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])
            (root / "src/feature.py").write_text("x = 1\n")
            cli.main(["record-file-change", "--step", "P1", "--path", "src/feature.py", "--root", str(root)])
            cli.main(["run", "--step", "P1", "--gate", 'python3 -c "print(1)"', "--root", str(root),
                      "--", "python3", "-c", "print(1)"])
            cli.main(["finish-step", "P1", "--root", str(root)])
            self.assertEqual(cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "r",
                                       "--root", str(root)]), 0)
            self.assertEqual(cli.main(["fail-step", "P1", "--reason", "x", "--root", str(root)]), 0)
            projection, selection = porcelain._resumability(root, _locked_plan(root), agent_id="tester")
            self.assertEqual((selection.kind, selection.step_id), ("claim", "P2"))
            self.assertTrue(_recovery(projection, "claim")["allowed"])
            self.assertFalse(_recovery(projection, "amend")["allowed"])
            self.assertEqual(_recovery(projection, "amend")["reason"], "a step is eligible to claim first")

    def test_older_recoverable_timeout_selects_amend(self):  # A2 timeout blocked, A3 clean blocked
        with TemporaryDirectory() as d:
            root = Path(d)
            _closed_amendment(root, "block-step", timeout=True)
            cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "again", "--root", str(root)])
            cli.main(["block-step", "P1", "--reason", "x", "--agent", "tester", "--root", str(root)])
            _projection, selection = porcelain._resumability(root, _locked_plan(root), agent_id="tester")
            self.assertEqual(selection.kind, "amend")
            self.assertEqual(selection.needs[0]["causes"][0]["receipt_id"], "CR2")

    def test_open_attempt_wins_over_an_older_recoverable_timeout(self):  # review focus 3
        with TemporaryDirectory() as d:
            root = Path(d)
            _closed_amendment(root, "block-step", timeout=True)
            cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "again", "--root", str(root)])
            projection, selection = porcelain._resumability(root, _locked_plan(root), agent_id="tester")
            self.assertEqual((selection.kind, selection.attempt_id), ("inflight", "A3"))
            self.assertFalse(_recovery(projection, "amend")["allowed"])
            self.assertFalse(_recovery(projection, "block")["allowed"])
            # Finishing A3 supersedes the older timeout: verify-run passes with a warning.
            for argv in (
                ["record-file-change", "--step", "P1", "--path", "src/feature.py", "--agent", "tester"],
                ["run", "--step", "P1", "--gate", 'python3 -c "print(1)"', "--agent", "tester",
                 "--", "python3", "-c", "print(1)"],
                ["finish-step", "P1", "--agent", "tester"],
            ):
                cut = argv.index("--") if "--" in argv else len(argv)
                argv[cut:cut] = ["--root", str(root)]
                self.assertEqual(cli.main(argv), 0, argv)
            result = execution_coverage.verify_run(root, _locked_plan(root), record=False)
            self.assertEqual(result["errors"], [])
            self.assertTrue(any("CR2" in w["message"] and "superseded by completed attempt A3" in w["message"]
                                for w in result["warnings"]), result["warnings"])
            self.assertEqual(cli.main(["verify-run", "--root", str(root)]), 0)

    def test_unrecoverable_timeout_suppresses_amend_run_wide(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timed_out_gate(root, retry_in_new_attempt=False)  # legacy: A1 completed with CR1
            self.assertEqual(cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "r",
                                       "--root", str(root)]), 0)
            self.assertEqual(cli.main(["fail-step", "P1", "--reason", "x", "--root", str(root)]), 0)
            projection, selection = porcelain._resumability(root, _locked_plan(root), agent_id="tester")
            self.assertTrue(selection.needs)  # a replacement is needed; only the guard withholds it
            self.assertIsNone(selection.kind)
            self.assertEqual([r["id"] for r in selection.unrecoverable], ["CR1"])
            self.assertFalse(any(item["allowed"] for item in projection["recovery_actions"]))


_COMMAND_ACTION = {
    "claim-step": "claim", "amend-step": "amend", "block-step": "block", "reclaim-step": "reclaim",
    "record-file-change": "continue", "run": "continue", "finish-step": "continue",
}


class TestNextActionRecovery(unittest.TestCase):
    """#70: next-action guidance a non-break-glass consumer can follow to a proof."""

    def _drive(self, root, agent, strict=False):
        """Follow next-action using only allowed, automatic, non-break-glass actions."""
        for _ in range(25):
            action = porcelain.next_action(root, strict=strict, agent_id=agent)
            if not action.args or action.args[0] not in _COMMAND_ACTION:
                return action
            rec = _recovery(action.resumability, _COMMAND_ACTION[action.args[0]])
            self.assertTrue(rec["allowed"] and rec["automatic"] and not rec["break_glass"],
                            (action.state, rec))
            argv = list(action.args)
            cut = argv.index("--") if "--" in argv else len(argv)
            argv[cut:cut] = ["--root", str(root)]
            self.assertEqual(cli.main(argv), 0, (action.state, argv))
        self.fail("next-action did not converge")

    def _assert_proven(self, root, agent, strict=False):
        action = self._drive(root, agent, strict)
        self.assertIn(action.state, {"run_unverified", "proof_missing"})
        self.assertEqual(cli.main(["verify-run", "--root", str(root), *(["--strict"] if strict else [])]), 0)
        if not strict:
            self.assertTrue(porcelain.finish_run(root, root / ".agent/plan.lock.json")["ok"])
            self.assertEqual(porcelain.next_action(root, agent_id=agent).state, "complete")

    def test_consumer_loop_recovers_every_fixture_without_break_glass(self):
        def advisory_initial(root):
            _timeout_then_rerun(root)

        def enforce_owner(root):
            _enforced_claim(root)
            _time_out_open_attempt(root, "worker-7")

        def reclaim_initial(root):
            enforce_owner(root)
            _set_attempt_lease(root, "A1", "2000-01-01T00:00:00+00:00")

        def timed_out_amendment(root):
            _completed_step(root)
            cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "r", "--root", str(root)])
            _time_out_open_attempt(root)

        def reclaim_amendment(root):
            _completed_step(root)
            _set_concurrency(root, lease_policy="enforce")
            cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "r", "--root", str(root)])
            _time_out_open_attempt(root)
            _set_attempt_lease(root, "A2", "2000-01-01T00:00:00+00:00")

        fixtures = [  # (setup, driving agent, strict)
            (advisory_initial, "tester", False),
            (enforce_owner, "worker-7", False),
            (reclaim_initial, "worker-8", False),
            (timed_out_amendment, "tester", False),
            (reclaim_amendment, "worker-8", False),
            (lambda root: _closed_amendment(root, "fail-step", timeout=False), "tester", False),
            (lambda root: _closed_amendment(root, "block-step", timeout=False), "tester", True),
        ]
        for setup, agent, strict in fixtures:
            with self.subTest(setup=getattr(setup, "__name__", "closed_amendment"), strict=strict), \
                    TemporaryDirectory() as d:
                root = Path(d)
                setup(root)
                self._assert_proven(root, agent, strict)

    def test_timed_out_attempt_states_before_and_after_rerun_and_verify(self):  # review focus 1
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            _time_out_open_attempt(root)
            want = ["block-step", "P1", "--attempt", "A1", "--reason", "gate timed out"]
            self.assertEqual(porcelain.next_action(root).args, want)
            cli.main(["run", "--step", "P1", "--gate", 'python3 -c "print(1)"', "--root", str(root),
                      "--", "python3", "-c", "print(1)"])
            self.assertEqual(porcelain.next_action(root).args, want)
            cli.main(["verify-step", "P1", "--root", str(root)])
            action = porcelain.next_action(root, agent_id="tester")
            self.assertEqual(action.state, "attempt_timed_out")
            self.assertEqual(action.args, [*want, "--agent", "tester"])
            self.assertEqual(action.diagnostics, ["command receipt CR1 timed out after 1 seconds"])
            # The emitted close works even after a standalone verify-step, and frees the step.
            self.assertEqual(cli.main([*action.args, "--root", str(root)]), 0)
            self.assertEqual(porcelain.next_action(root, agent_id="tester").state, "step_unclaimed")

    def test_structured_gate_whose_own_run_timed_out(self):
        # The receipt's command equals the gate's run, so verify-step credits it first-match.
        sleepy = ["python3", "-c", "import os, time; time.sleep(int(os.environ.get('AF70_SLEEP', '0')))"]
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo_with_steps(root, [{**_step(), "gates": [{"kind": "command", "run": sleepy}]}])
            cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)])
            path = root / ".agent/execution.contract.json"
            contract = json.loads(path.read_text())
            contract["command_policy"]["command_timeout_seconds"] = 1
            path.write_text(json.dumps(contract))
            with mock.patch.dict("os.environ", {"AF70_SLEEP": "5"}):
                self.assertEqual(cli.main(["run", "--step", "P1", "--gate", 'python3 -c "print(1)"',
                                           "--root", str(root), "--", *sleepy]), 124)
            cli.main(["run", "--step", "P1", "--gate", 'python3 -c "print(1)"', "--root", str(root),
                      "--", *sleepy])  # passing rerun, same argv
            self.assertEqual(porcelain.next_action(root).state, "attempt_timed_out")

    def test_step_amendment_needed_for_each_cause_and_strictness_source(self):
        cases = [  # (close, timeout, strict source, expected --reason)
            ("block-step", True, None, "replace attempt A2: timed-out receipt CR2"),
            ("fail-step", False, None, "replace failed attempt A2"),
            ("block-step", False, "flag", "replace blocked attempt A2 (strict)"),
            ("block-step", False, "env", "replace blocked attempt A2 (strict)"),
            ("block-step", False, "policy", "replace blocked attempt A2 (strict)"),
        ]
        for close, timeout, source, reason in cases:
            with self.subTest(close=close, timeout=timeout, source=source), TemporaryDirectory() as d:
                root = Path(d)
                _closed_amendment(root, close, timeout)
                if source == "policy":
                    path = root / ".agent/execution.contract.json"
                    contract = json.loads(path.read_text())
                    contract.setdefault("proof_policy", {})["strict_by_default"] = True
                    path.write_text(json.dumps(contract))
                if source == "env":
                    out = io.StringIO()
                    with mock.patch.dict("os.environ", {"AGENTFLOW_STRICT": "1"}), \
                            contextlib.redirect_stdout(out):
                        cli.main(["next-action", "--json", "--agent", "tester", "--root", str(root)])
                    payload = json.loads(out.getvalue())
                else:
                    payload = porcelain.next_action(root, strict=source == "flag", agent_id="tester").to_dict()
                self.assertEqual(payload["state"], "step_amendment_needed")
                self.assertEqual(payload["args"], ["amend-step", "P1", "--agent", "tester", "--reason", reason])
                self.assertEqual((payload["step_id"], payload["resumability"]["step"]["id"]), ("P1", "P1"))

    def test_blocked_amendment_without_timeout_still_passes_in_default_mode(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _closed_amendment(root, "block-step", timeout=False)
            self.assertEqual(porcelain.next_action(root, agent_id="tester").state, "run_unverified")
            self.assertTrue(porcelain.finish_run(root, root / ".agent/plan.lock.json")["ok"])
            self.assertEqual(porcelain.next_action(root).state, "complete")

    def test_two_repairable_steps_pick_plan_order_and_name_the_other(self):
        p2 = {**_step(), "id": "P2", "files": ["src/other.py"],
              "expected_diff": ["src/other.py changes are attributed to P2."]}
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo_with_steps(root, [_step(), p2], extra_allowed=("src/other.py",))
            (root / "src").mkdir()
            for sid, path in (("P1", "src/feature.py"), ("P2", "src/other.py")):
                cli.main(["claim-step", sid, "--agent", "tester", "--root", str(root)])
                (root / path).write_text("x = 1\n")
                cli.main(["record-file-change", "--step", sid, "--path", path, "--root", str(root)])
                cli.main(["run", "--step", sid, "--gate", 'python3 -c "print(1)"', "--root", str(root),
                          "--", "python3", "-c", "print(1)"])
                cli.main(["finish-step", sid, "--root", str(root)])
            for sid in ("P2", "P1"):
                cli.main(["amend-step", sid, "--agent", "tester", "--reason", "r", "--root", str(root)])
                cli.main(["fail-step", sid, "--reason", "x", "--root", str(root)])
            action = porcelain.next_action(root, agent_id="tester")
            self.assertEqual((action.state, action.step_id), ("step_amendment_needed", "P1"))
            self.assertIn("also needs a replacement attempt: P2", action.diagnostics)

    def test_legacy_stranded_run_never_advertises_amend(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _timed_out_gate(root, retry_in_new_attempt=False)
            # A failed amendment makes a replacement needed; only the run-wide guard withholds it.
            self.assertEqual(cli.main(["amend-step", "P1", "--agent", "tester", "--reason", "r",
                                       "--root", str(root)]), 0)
            self.assertEqual(cli.main(["fail-step", "P1", "--reason", "x", "--root", str(root)]), 0)
            for strict in (False, True):
                action = porcelain.next_action(root, strict=strict, agent_id="tester")
                self.assertEqual(action.state, "run_unverified")
                self.assertTrue(any("CR1 for P1 cannot be superseded" in line for line in action.diagnostics))

    def test_agent_identity_is_used_verbatim(self):  # review focus 2 and 5
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            with mock.patch.dict("os.environ", {"USER": "someone-else"}):
                action = porcelain.next_action(root, agent_id="worker 7")
            self.assertEqual(action.args, ["claim-step", "P1", "--agent", "worker 7"])
            self.assertIn("--agent 'worker 7'", action.command)
            self.assertEqual(porcelain.next_action(root).args, ["claim-step", "P1", "--agent", "$USER"])
            out = io.StringIO()
            with mock.patch.dict("os.environ", {"AGENTFLOW_AGENT_ID": "worker-9"}), \
                    contextlib.redirect_stdout(out):
                cli.main(["next-action", "--json", "--root", str(root)])
            self.assertEqual(json.loads(out.getvalue())["args"][-1], "worker-9")

    def test_printed_commands_preserve_literal_user_identity(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            _set_concurrency(root, lease_policy="enforce")
            for state in ("step_unclaimed", "file_receipts_missing", "validation_missing",
                          "step_unverified", "step_amendment_needed"):
                action = porcelain.next_action(root, agent_id="$USER")
                self.assertEqual(action.state, state)
                rendered = subprocess.run(
                    ["sh", "-c", 'agentflow() { printf "%s\\0" "$@"; }; ' + action.command],
                    env={"USER": "different-agent"}, capture_output=True, check=True,
                )
                argv = rendered.stdout.decode().split("\0")[:-1]
                self.assertEqual(argv, action.args)
                cut = argv.index("--") if "--" in argv else len(argv)
                argv[cut:cut] = ["--root", str(root)]
                self.assertEqual(cli.main(argv), 0)
                if state == "step_unclaimed":
                    (root / "src").mkdir()
                    (root / "src/feature.py").write_text("x = 1\n")
                elif state == "step_unverified":
                    cli.main(["amend-step", "P1", "--agent", "$USER", "--reason", "test",
                              "--root", str(root)])
                    cli.main(["fail-step", "P1", "--reason", "test", "--root", str(root)])

    def test_printed_commands_execute_leading_hyphen_identities(self):
        for agent in ("-worker", "--strict"):
            with self.subTest(agent=agent), TemporaryDirectory() as d:
                root = Path(d)
                _ready_repo(root)
                _set_concurrency(root, lease_policy="enforce")
                for state in ("step_unclaimed", "file_receipts_missing", "validation_missing",
                              "step_unverified", "step_amendment_needed"):
                    action = porcelain.next_action(root, agent_id=agent)
                    self.assertEqual(action.state, state)
                    rendered = subprocess.run(
                        ["sh", "-c", 'agentflow() { printf "%s\\0" "$@"; }; ' + action.command],
                        capture_output=True, check=True,
                    )
                    argv = rendered.stdout.decode().split("\0")[:-1]
                    self.assertEqual(argv, action.args)
                    cut = argv.index("--") if "--" in argv else len(argv)
                    argv[cut:cut] = ["--root", str(root)]
                    self.assertEqual(cli.main(argv), 0)
                    if state == "step_unclaimed":
                        (root / "src").mkdir()
                        (root / "src/feature.py").write_text("x = 1\n")
                    elif state == "step_unverified":
                        self.assertEqual(cli.main(["amend-step", "P1", f"--agent={agent}",
                                                   "--reason", "test", "--root", str(root)]), 0)
                        self.assertEqual(cli.main(["fail-step", "P1", "--reason", "test",
                                                   "--root", str(root)]), 0)

    def test_timeout_recovery_executes_leading_hyphen_identities(self):
        for close in ("block-step", "reclaim-step"):
            with self.subTest(close=close), TemporaryDirectory() as d:
                root = Path(d)
                _ready_repo(root)
                _set_concurrency(root, lease_policy="enforce")
                self.assertEqual(cli.main(["claim-step", "P1", "--agent=-worker",
                                           "--root", str(root)]), 0)
                path = root / ".agent/execution.contract.json"
                contract = json.loads(path.read_text())
                contract["command_policy"]["command_timeout_seconds"] = 1
                path.write_text(json.dumps(contract))
                self.assertEqual(cli.main(["run", "--step", "P1", "--agent=-worker",
                                           "--root", str(root), "--", "python3", "-c",
                                           "import time; time.sleep(5)"]), 124)
                if close == "reclaim-step":
                    _set_attempt_lease(root, "A1", "2000-01-01T00:00:00+00:00")
                action = porcelain.next_action(root, agent_id="-worker")
                self.assertEqual(action.state, "attempt_timed_out")
                self.assertEqual(action.args[0], close)
                self.assertEqual(cli.main([*action.args, "--root", str(root)]), 0)

    def test_default_user_placeholder_still_expands_for_claim_and_amend(self):
        for setup, state in ((_ready_repo, "step_unclaimed"),
                             (lambda root: _closed_amendment(root, "fail-step", False),
                              "step_amendment_needed")):
            with self.subTest(state=state), TemporaryDirectory() as d:
                root = Path(d)
                setup(root)
                action = porcelain.next_action(root)
                self.assertEqual(action.state, state)
                rendered = subprocess.run(
                    ["sh", "-c", 'agentflow() { printf "%s\\0" "$@"; }; ' + action.command],
                    env={"USER": "default-agent"}, capture_output=True, check=True,
                )
                argv = rendered.stdout.decode().split("\0")[:-1]
                self.assertEqual(argv[argv.index("--agent") + 1], "default-agent")

    def test_inflight_commands_carry_agent_and_finish_commands_carry_strict(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            (root / "src").mkdir()
            (root / "src/feature.py").write_text("x = 1\n")
            action = porcelain.next_action(root, agent_id="tester")
            self.assertEqual(action.args[-2:], ["--agent", "tester"])  # record-file-change
            cli.main(["record-file-change", "--step", "P1", "--path", "src/feature.py", "--root", str(root)])
            run_args = porcelain.next_action(root, agent_id="tester").args
            self.assertEqual(run_args[run_args.index("--") - 2:run_args.index("--")], ["--agent", "tester"])
            cli.main(["run", "--step", "P1", "--gate", 'python3 -c "print(1)"', "--root", str(root),
                      "--", "python3", "-c", "print(1)"])
            self.assertEqual(porcelain.next_action(root, strict=True).args, ["finish-step", "P1", "--strict"])
            self.assertEqual(porcelain.next_action(root).args, ["finish-step", "P1"])
            cli.main(["finish-step", "P1", "--root", str(root)])
            self.assertEqual(porcelain.next_action(root, strict=True).args, ["finish-run", "--strict"])

    def test_inflight_command_is_withheld_when_continue_is_denied(self):  # ruling R6
        with TemporaryDirectory() as d:
            root = Path(d)
            _enforced_claim(root)  # worker-7 owns A1 under enforce, live lease
            owner = porcelain.next_action(root, agent_id="worker-7")
            self.assertEqual(owner.state, "validation_missing")
            self.assertEqual(owner.args[:2], ["run", "--step"])
            self.assertEqual(owner.args[owner.args.index("--") - 2:owner.args.index("--")],
                             ["--agent", "worker-7"])
            cases = [  # (agent, lease rewrite, continue denial reason)
                ("worker-8", None, "attempt is owned by worker-7"),
                ("worker-7", "2000-01-01T00:00:00+00:00",
                 "expired enforced lease must be renewed or reclaimed"),
            ]
            for agent, expires_at, reason in cases:
                with self.subTest(agent=agent, expired=expires_at is not None):
                    if expires_at is not None:
                        _set_attempt_lease(root, "A1", expires_at)
                    action = porcelain.next_action(root, agent_id=agent)
                    self.assertEqual(action.state, "validation_missing")
                    self.assertEqual((action.command, action.args), (None, []))
                    self.assertEqual(_recovery(action.resumability, "continue")["reason"], reason)
                    self.assertIn(reason, action.diagnostics)
                    self.assertNotIn("args", action.to_dict())
            # Without a supplied agent the command is unchanged.
            self.assertEqual(porcelain.next_action(root).args[:2], ["run", "--step"])

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(ValueError):
            porcelain.Action("made_up", "x")
        self.assertEqual(len(porcelain.NEXT_ACTION_STATES), 17)


class TestPolicyFloorRatchet(unittest.TestCase):
    """#66: a non-strict finish-run keeps the floor of the proof it replaces."""

    NOTE = ("review policy floor kept from existing proof: "
            "proof_strict_effective=true, review_gate_effective=block")

    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGENTFLOW_STRICT": ""})
        env.start()
        self.addCleanup(env.stop)

    def _mcp(self, root: Path, tool: str) -> dict:
        from agentflow import mcp_server
        return mcp_server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {"root": str(root)}},
        })["result"]["structuredContent"]

    def test_non_strict_finish_run_stops_at_build_proof_with_the_floor_note(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            plan = root / ".agent/plan.lock.json"
            self.assertEqual(porcelain.finish_run(root, plan, strict=True)["stopped_at"], "build-proof")
            result = porcelain.finish_run(root, plan)
            self.assertEqual((result["ok"], result["stopped_at"]), (False, "build-proof"))
            for line in (self.NOTE, "steps_without_support: warning", "created .agent/proof-pack.json"):
                self.assertIn(line, result["diagnostics"])

    def test_next_action_reports_stale_then_failing(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            plan = root / ".agent/plan.lock.json"
            porcelain.finish_run(root, plan, strict=True)
            append_jsonl(root / ".agent/evidence.jsonl",
                         {"id": "E9", "kind": "command", "claim": "late", "supports": []})
            self.assertEqual(porcelain.next_action(root).state, "proof_stale")
            porcelain.finish_run(root, plan)
            action = porcelain.next_action(root)
            self.assertEqual(action.state, "proof_failing")
            self.assertIn("steps_without_support: warning", action.diagnostics)

    def test_publication_failure_is_a_normal_stop_for_finish_run_and_mcp(self):
        from agentflow import proof as proof_module
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            with mock.patch.object(proof_module.os, "replace",
                                   side_effect=OSError(5, "Input/output error")):
                result = porcelain.finish_run(root, root / ".agent/plan.lock.json")
                content = self._mcp(root, "build_proof")
            self.assertEqual((result["ok"], result["stopped_at"]), (False, "build-proof"))
            self.assertTrue(any(line.startswith("could not publish proof: ")
                                for line in result["diagnostics"]))
            self.assertEqual(content["exit_code"], 1)
            self.assertIn("could not publish proof: ", content["stderr"])
            self.assertFalse((root / ".agent/proof-pack.json").exists())

    def test_mcp_finish_run_without_strict_keeps_the_floor(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _completed_step(root)
            porcelain.finish_run(root, root / ".agent/plan.lock.json", strict=True)
            content = self._mcp(root, "finish_run")
            self.assertEqual(content["exit_code"], 1)
            self.assertEqual(content["data"]["stopped_at"], "build-proof")
            proof = json.loads((root / ".agent/proof-pack.json").read_text(encoding="utf-8"))
            self.assertTrue(proof["review"]["policy"]["proof_strict_effective"])


class TestRecordedBlockers(unittest.TestCase):
    """#72: next-action reports attempts closed by block-step / fail-step."""

    KEYS = {"step_id", "attempt_id", "event", "reason", "recorded_at", "attempt_owner", "superseded_by"}

    @staticmethod
    def _row(event, attempt, step="P1", **extra):
        return {"event": event, "step_id": step, "attempt_id": attempt, "recorded_at": "t", **extra}

    def _blockers(self, root, **kwargs):
        return porcelain.next_action(root, **kwargs).to_dict()["recorded_blockers"]

    def _cli(self, root, *argv):
        self.assertEqual(cli.main([*argv, "--root", str(root)]), 0)

    def _last_time(self, root):
        return json.loads((root / ".agent/step-runs.jsonl").read_text().splitlines()[-1])["recorded_at"]

    def test_every_validated_state_carries_the_key(self):
        def unrecorded_edit(root):
            _claimed(root)
            (root / "src").mkdir(parents=True, exist_ok=True)
            (root / "src/feature.py").write_text("x = 1\n")

        def verified(root):
            _validated(root)
            self._cli(root, "verify-step", "P1")

        def drifting(root):
            _completed_step(root)
            (root / "rogue.txt").write_text("out of scope\n")

        def run_verified(root):
            _completed_step(root)
            self._cli(root, "verify-run")

        def stale_proof(root):
            _completed_step(root)
            _build_verified_proof(root)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text())
            plan["objective"] = "changed after proof"
            plan_path.write_text(json.dumps(plan, indent=2))

        def corrupt_proof(root):
            _completed_step(root)
            _build_verified_proof(root)
            (root / ".agent/proof-pack.json").write_text("{}\n")

        cases = [
            (_ready_repo, "step_unclaimed", 0),
            (_claimed, "validation_missing", 0),
            (_timeout_then_rerun, "attempt_timed_out", 0),
            (lambda r: _closed_amendment(r, "fail-step", timeout=False), "step_amendment_needed", 1),
            (_completed_step, "run_unverified", 0),
            (lambda r: (_completed_step(r), _build_verified_proof(r)), "complete", 0),
            (unrecorded_edit, "file_receipts_missing", 0),
            (_validated, "step_unverified", 0),
            (verified, "step_uncompleted", 0),
            (drifting, "drift_failing", 0),
            (run_verified, "proof_missing", 0),
            (stale_proof, "proof_stale", 0),
            (corrupt_proof, "proof_failing", 0),
        ]
        covered = set()
        for setup, state, count in cases:
            with self.subTest(state=state), TemporaryDirectory() as d:
                root = Path(d)
                setup(root)
                payload = porcelain.next_action(root).to_dict()
                self.assertEqual(payload["state"], state)
                self.assertIsInstance(payload["recorded_blockers"], list)
                self.assertEqual(len(payload["recorded_blockers"]), count)
                covered.add(payload["state"])
        self.assertEqual(
            covered,
            set(porcelain.NEXT_ACTION_STATES)
            - {"uninitialized", "plan_unlocked", "execution_uninitialized", "state_invalid"},
        )

    def test_block_on_never_completed_step_is_a_current_blocker(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            self._cli(root, "block-step", "P1", "--reason", "waiting on upstream API key", "--agent", "tester")
            payload = porcelain.next_action(root).to_dict()
            self.assertEqual((payload["state"], payload["blocking"]), ("step_unclaimed", True))
            self.assertEqual(payload["recorded_blockers"], [{
                "step_id": "P1", "attempt_id": "A1", "event": "blocked",
                "reason": "waiting on upstream API key", "recorded_at": self._last_time(root),
                "attempt_owner": "tester", "superseded_by": None,
            }])

    def test_claim_after_block_supersedes_the_blocker(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            self._cli(root, "block-step", "P1", "--reason", "waiting on upstream API key", "--agent", "tester")
            self._cli(root, "claim-step", "P1", "--agent", "tester")
            (entry,) = self._blockers(root)
            self.assertEqual((entry["attempt_id"], entry["superseded_by"]), ("A1", "A2"))

    def test_break_glass_fail_records_the_opener_not_the_operator(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            self._cli(root, "fail-step", "P1", "--reason", "break glass", "--agent", "operator")
            (entry,) = self._blockers(root)
            self.assertEqual(set(entry), self.KEYS)
            self.assertEqual((entry["event"], entry["attempt_owner"]), ("failed", "tester"))

    def test_standalone_fail_without_opener_has_no_owner(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _ready_repo(root)
            self._cli(root, "fail-step", "P1", "--attempt", "A9", "--reason", "no opener")
            (entry,) = self._blockers(root)
            self.assertEqual(
                (entry["attempt_id"], entry["event"], entry["attempt_owner"], entry["superseded_by"]),
                ("A9", "failed", None, None),
            )
            self._cli(root, "claim-step", "P1", "--agent", "tester")
            (entry,) = self._blockers(root)
            self.assertEqual(entry["superseded_by"], "A1")

    def test_blocked_amendment_is_current_and_leaves_state_alone(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _closed_amendment(root, "block-step", timeout=False)
            payload = porcelain.next_action(root).to_dict()
            self.assertEqual(payload["state"], "run_unverified")
            self.assertEqual(
                [(e["attempt_id"], e["event"], e["reason"], e["attempt_owner"], e["superseded_by"])
                 for e in payload["recorded_blockers"]],
                [("A2", "blocked", "closed", "tester", None)],
            )

    def test_reclaimed_lease_is_not_a_blocker(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _enforced_claim(root)
            _set_attempt_lease(root, "A1", "2000-01-01T00:00:00+00:00")
            self._cli(root, "reclaim-step", "P1", "--agent", "worker-8", "--reason", "expired")
            rows = [json.loads(line) for line in (root / ".agent/step-runs.jsonl").read_text().splitlines()]
            self.assertIn("abandoned", [row["event"] for row in rows])
            self.assertEqual(self._blockers(root, agent_id="worker-8"), [])

    def test_superseded_by_follows_ledger_order_not_id_order(self):
        events = [
            self._row("claimed", "WT9-A1", agent_id="w9"),
            self._row("lease_renewed", "WT9-A1", agent_id="w9"),
            self._row("blocked", "WT9-A1", reason="r9"),
            self._row("claimed", "WT5-A1", step="P2", agent_id="w5"),
            self._row("claimed", "WT1-A1", agent_id="w1"),
            self._row("blocked", "WT1-A1", reason="r1"),
            self._row("claimed", "WT0-A1", agent_id="w0"),
        ]
        self.assertEqual(
            [(e["attempt_id"], e["attempt_owner"], e["superseded_by"])
             for e in porcelain._recorded_blockers(events)],
            [("WT9-A1", "w9", "WT1-A1"), ("WT1-A1", "w1", "WT0-A1")],
        )

    def test_non_string_reason_and_time_become_null(self):
        events = [
            self._row("claimed", "A1", agent_id="a"),
            {"event": "failed", "step_id": "P1", "attempt_id": "A1", "reason": 7},
        ]
        (entry,) = porcelain._recorded_blockers(events)
        self.assertIsNone(entry["reason"])
        self.assertIsNone(entry["recorded_at"])

    def test_key_absent_without_a_validated_ledger(self):
        def init_only(root):
            self._cli(root, "init")

        def unlocked_contract(root):
            cli.main(["init", "--root", str(root)])
            plan_path = root / ".agent/plan.lock.json"
            plan_path.write_text(json.dumps(_plan([_step()])))
            cli.main(["lock-plan", str(plan_path)])

        def ambiguous_after_block(root):
            _ready_repo(root)
            self._cli(root, "claim-step", "P1", "--agent", "agent-a")
            self._cli(root, "block-step", "P1", "--reason", "r", "--agent", "agent-a")
            self._cli(root, "claim-step", "P1", "--agent", "agent-a")
            self._cli(root, "claim-step", "P1", "--agent", "agent-b")

        def malformed_ledger(root):
            _claimed(root)
            self._cli(root, "block-step", "P1", "--reason", "r", "--agent", "tester")
            with (root / ".agent/step-runs.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('"not an object"\n')

        cases = [
            (lambda root: None, "uninitialized"),
            (init_only, "plan_unlocked"),
            (unlocked_contract, "execution_uninitialized"),
            (ambiguous_after_block, "state_invalid"),
            (malformed_ledger, "state_invalid"),
        ]
        for setup, state in cases:
            with self.subTest(state=state, setup=getattr(setup, "__name__", "empty")), TemporaryDirectory() as d:
                root = Path(d)
                setup(root)
                payload = porcelain.next_action(root).to_dict()
                self.assertEqual(payload["state"], state)
                self.assertNotIn("recorded_blockers", payload)

    def test_json_keeps_a_raw_reason(self):
        raw = "line\nnext\x1b[31m" + "x" * 300
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            self._cli(root, "block-step", "P1", "--reason", raw, "--agent", "tester")
            (entry,) = self._blockers(root)
            self.assertEqual(entry["reason"], raw)
            self.assertIn("\\u001b", json.dumps(porcelain.next_action(root).to_dict()))

    def test_mcp_next_action_carries_recorded_blockers(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _claimed(root)
            self._cli(root, "block-step", "P1", "--reason", "waiting on upstream API key", "--agent", "tester")
            result = mcp_server.handle_message({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "next_action", "arguments": {"root": str(root)}},
            })
            blockers = result["result"]["structuredContent"]["data"]["recorded_blockers"]
            self.assertEqual(blockers, self._blockers(root))
            self.assertEqual(len(blockers), 1)


class TestRecordedBlockerHumanOutput(unittest.TestCase):
    """#72: human next-action prints one sanitized line per current blocker."""

    def _human(self, root):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["next-action", "--root", str(root)]), 0)
        return out.getvalue().splitlines()

    def _blocked(self, root, reason):
        _claimed(root)
        self.assertEqual(cli.main(["block-step", "P1", "--reason", reason, "--agent", "tester",
                                   "--root", str(root)]), 0)
        return json.loads((root / ".agent/step-runs.jsonl").read_text().splitlines()[-1])["recorded_at"]

    def test_current_blocker_prints_one_line(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            at = self._blocked(root, "waiting on upstream API key")
            self.assertEqual(
                self._human(root)[-1],
                f"  recorded blocker: P1/A1 blocked at {at}: waiting on upstream API key",
            )

    def test_blocker_lines_follow_diagnostics(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _closed_amendment(root, "fail-step", timeout=False)
            diags = porcelain.next_action(root).diagnostics
            self.assertTrue(diags)
            lines = self._human(root)
            self.assertEqual(lines[-2], f"  {diags[-1]}")
            self.assertTrue(lines[-1].startswith("  recorded blocker: P1/A2 failed at "), lines[-1])

    def test_history_is_not_printed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._blocked(root, "first")
            self.assertEqual(cli.main(["claim-step", "P1", "--agent", "tester", "--root", str(root)]), 0)
            self.assertEqual(cli.main(["block-step", "P1", "--reason", "second", "--agent", "tester",
                                       "--root", str(root)]), 0)
            blocker_lines = [line for line in self._human(root) if "recorded blocker:" in line]
            self.assertEqual(len(blocker_lines), 1, blocker_lines)
            self.assertTrue(blocker_lines[0].startswith("  recorded blocker: P1/A2 blocked at "))
            self.assertTrue(blocker_lines[0].endswith(": second"))

    def test_reason_is_sanitized_and_capped(self):
        cases = {
            "controls": (
                "a\nb\rc\x1bd\x9be\x7ff\u202eg\u2028h" + "x" * 300,
                ("a?b?c?d?e?f?g?h" + "x" * 300)[:200] + "...",
            ),
            "exactly_200": ("y" * 200, "y" * 200),
        }
        for name, (reason, shown) in cases.items():
            with self.subTest(case=name), TemporaryDirectory() as d:
                root = Path(d)
                at = self._blocked(root, reason)
                self.assertEqual(
                    self._human(root)[-1],
                    f"  recorded blocker: P1/A1 blocked at {at}: {shown}",
                )

    def test_null_reason_ends_at_the_timestamp(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            at = self._blocked(root, "to be replaced")
            path = root / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1]["reason"] = 7
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertEqual(self._human(root)[-1], f"  recorded blocker: P1/A1 blocked at {at}")
            rows[-1].pop("recorded_at")
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertEqual(self._human(root)[-1], "  recorded blocker: P1/A1 blocked at ?")


def _explain_plan(depends: bool = True, steps: bool = True) -> dict:
    plan = {
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
    if not depends:
        del plan["steps"][1]["depends_on"]
    if not steps:
        plan["steps"] = []
    return plan


def _explain_root(root: Path, plan: dict, enforce: bool = False) -> dict:
    create_initial_artifacts(root)
    init_execution_artifacts(root)
    write_json(root / ".agent/plan.lock.json", plan)
    if enforce:
        _set_concurrency(root, lease_policy="enforce")
    return plan


def _step_row(root: Path, **row) -> None:
    append_jsonl(root / ".agent/step-runs.jsonl", {
        "schema_version": STEP_RUNS_SCHEMA_VERSION,
        "recorded_at": "2026-10-27T00:00:00+00:00",
        **row,
    })


def _done(root: Path, plan: dict, step_id: str, agent: str = "agent-a", now=None) -> str:
    attempt = claim_step(root, plan, step_id, agent, now=now)["attempt_id"]
    mark_step_verified(root, step_id, attempt, [], agent_id=agent, now=now)
    complete_step(root, step_id, attempt, agent_id=agent, now=now)
    return attempt


_PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _plan_of(root: Path) -> dict:
    return json.loads((root / ".agent/plan.lock.json").read_text())


def _ledger_text(root: Path, name: str, text: str) -> None:
    with (root / ".agent" / name).open("a", encoding="utf-8") as handle:
        handle.write(text)


def _r1(root):
    _explain_root(root, _explain_plan())
    return root


def _r2(root):
    plan = _explain_root(root, _explain_plan())
    claim_step(root, plan, "P1", "agent-a")
    fail_step(root, "P1", None, "x")
    return root


def _r3(root):
    plan = _explain_root(root, _explain_plan())
    claim_step(root, plan, "P1", "agent-a")
    return root


def _r4(root):
    plan = _explain_root(root, _explain_plan())
    _done(root, plan, "P1")
    claim_step(root, plan, "P2", "agent-b")
    return root


def _r5(root):
    plan = _explain_root(root, _explain_plan(depends=False))
    claim_step(root, plan, "P1", "agent-a")
    claim_step(root, plan, "P2", "agent-b")
    return root


def _r6(root):
    plan = _explain_root(root, _explain_plan())
    _done(root, plan, "P1")
    _done(root, plan, "P2")
    amend_step(root, plan, "P1", "agent-b", "fix")
    return root


def _r7(root):
    plan = _explain_root(root, _explain_plan(), enforce=True)
    _done(root, plan, "P1")
    claim_step(root, plan, "P2", "agent-b", now=_PAST)
    return root


def _r7b(root):
    plan = _explain_root(root, _explain_plan(), enforce=True)
    claim_step(root, plan, "P1", "agent-a", now=_PAST)
    return root


def _r8(root):
    plan = _explain_root(root, _explain_plan())
    _done(root, plan, "P1")
    _done(root, plan, "P2")
    return root


def _r9(root):
    _r8(root)
    amend_step(root, _plan_of(root), "P1", "agent-b", "fix")
    fail_step(root, "P1", None, "x")
    return root


def _r10(root):
    plan = _explain_root(root, _explain_plan())
    _done(root, plan, "P1")
    claim_step(root, plan, "P2", "agent-b")
    claim_step(root, plan, "P2", "agent-c")
    return root


def _r11(root):
    _explain_root(root, _explain_plan(steps=False))
    return root


def _r12(root):
    _explain_root(root, _explain_plan())
    fail_step(root, "P1", "A1", "x")
    return root


def _r13(root):
    plan = _explain_root(root, _explain_plan(depends=False))
    claim_step(root, plan, "P1", "agent-a")
    return root


def _r14(root):
    plan = _explain_root(root, _explain_plan())
    _done(root, plan, "P1")
    amend_step(root, plan, "P1", "agent-b", "fix")
    return root


def _r15(root):
    plan = _explain_root(root, _explain_plan(), enforce=True)
    _done(root, plan, "P1")
    claim_step(root, plan, "P2", "agent-b", now=_PAST)
    reclaim_step(root, plan, "P2", "agent-c", "expired")
    return root


def _r16(root):
    _r3(root)
    renew_lease(root, "P1", None, "agent-a")
    return root


def _r17(root):
    _explain_root(root, _explain_plan())
    _step_row(root, event="claimed", step_id="P1", attempt_id="A1", agent_id=None)
    return root


def _r18(root):
    plan = _explain_root(root, _explain_plan(depends=False))
    claim_step(root, plan, "P1", "agent-a")
    claim_step(root, plan, "P1", "agent-b")
    mark_step_verified(root, "P1", "A2", [], agent_id="agent-b")
    complete_step(root, "P1", "A2", agent_id="agent-b")
    fail_step(root, "P1", "A1", "late")
    _done(root, plan, "P2")
    return root


def _r19(root):
    _explain_root(root, _explain_plan())
    _step_row(root, event="claimed", step_id="P1", attempt_id="WTw2-A1", agent_id="w2")
    _step_row(root, event="claimed", step_id="P1", attempt_id="WTw1-A1", agent_id="w1")
    return root


def _r20(root):
    _r6(root)
    mark_step_verified(root, "P1", "A3", [])
    return root


def _r21(root):
    plan = _explain_plan(depends=False)
    plan["steps"].append({
        **json.loads(json.dumps(plan["steps"][0])),
        "id": "P3",
        "depends_on": ["P1", "P2"],
    })
    _explain_root(root, plan)
    claim_step(root, plan, "P1", "agent-a")
    _done(root, plan, "P2")
    return root


def _r22(root):
    _explain_root(root, _explain_plan())
    _step_row(root, event="claimed", step_id="P1", attempt_id="A1", agent_id="")
    return root


def _r23(root):
    _r8(root)
    amend_step(root, _plan_of(root), "P1", "agent-b", "fix")
    block_step(root, "P1", None, "x", agent_id="agent-b")
    return root


def _r24(root):
    _r8(root)
    _step_row(root, event="amendment_started", step_id="P1", attempt_id="A3", agent_id="agent-b")
    _step_row(root, event="abandoned", step_id="P1", attempt_id="A3")
    return root


_R3_LINES = (
    ("step P1 has open attempt A1", "agent-a"),
    ("step P2 waits on unmet dependencies: P1",),
)

# name: (build, reason_code, step id, one needle tuple per diagnostic, absent needles)
_EXPLAIN_CASES = {
    "R1": (_r1, "eligible", "P1", (), ()),
    "R2": (_r2, "eligible", "P1", (), ()),
    "R3": (_r3, "dependencies_unmet", None, _R3_LINES, ()),
    "R4": (_r4, "open_attempts", None, (("P2", "A2", "agent-b"),), ()),
    "R5": (_r5, "open_attempts", None, (("P1", "A1"), ("P2", "A2")), ()),
    "R6": (_r6, "open_attempts", None,
           (("completed work and open attempt A3", "agent-b"),), ()),
    "R7": (_r7, "open_attempts", None, (("P2", "A2"),), ()),
    "R7b": (_r7b, "dependencies_unmet", None, (("P1", "A1"), ("P2 waits",)), ()),
    "R8": (_r8, "steps_complete", None, (), ()),
    "R9": (_r9, "steps_complete", None, (("P1", "A3", "failed", "next-action"),), ()),
    "R10": (_r10, "open_attempts", None, (("P2", "A2"), ("P2", "A3")), ()),
    "R11": (_r11, "steps_complete", None, (("plan has no steps",),), ()),
    "R12": (_r12, "eligible", "P1", (), ()),
    "R13": (_r13, "eligible", "P2", (), ()),
    "R14": (_r14, "eligible", "P2", (), ()),
    "R15": (_r15, "open_attempts", None, (("P2", "A3", "agent-c"),), ("A2",)),
    "R16": (_r16, "dependencies_unmet", None, _R3_LINES, ()),
    "R17": (_r17, "dependencies_unmet", None, (("A1", "(no owner)"), ("P2 waits",)), ()),
    "R18": (_r18, "steps_complete", None, (("P1", "A1", "failed"),), ("A2",)),
    "R19": (_r19, "dependencies_unmet", None,
            (("WTw2-A1",), ("WTw1-A1",), ("P2 waits",)), ()),
    "R20": (_r20, "open_attempts", None, (("completed work and open attempt A3",),), ()),
    "R21": (_r21, "dependencies_unmet", None,
            (("P1", "A1"), ("step P3 waits on unmet dependencies: P1",)), ("P2",)),
    "R22": (_r22, "dependencies_unmet", None, (("A1", "(no owner)"), ("P2 waits",)), ()),
    "R23": (_r23, "steps_complete", None, (("P1", "A3", "blocked", "next-action"),), ()),
    "R24": (_r24, "steps_complete", None, (("P1", "A3", "abandoned", "next-action"),), ()),
}


def _planned(change):
    """D with `change(plan)` applied to the plan dict before it is written."""
    def build(root):
        plan = _explain_plan()
        change(plan)
        _explain_root(root, plan)
        return root
    return build


def _with_row(base, **row):
    def build(root):
        base(root)
        _step_row(root, **row)
        return root
    return build


def _overwrite(relative, text):
    def build(root):
        _r1(root)
        (root / relative).write_text(text, encoding="utf-8")
        return root
    return build


def _inv_malformed_jsonl(root):
    _r1(root)
    _ledger_text(root, "step-runs.jsonl", "{not json\n")
    return root


def _inv_receipt_missing_field(root):
    _r1(root)
    append_jsonl(root / ".agent/command-receipts.jsonl",
                 {"schema_version": COMMAND_RECEIPTS_SCHEMA_VERSION, "id": "CR1"})
    return root


def _inv_missing_ledger(root):
    _r1(root)
    (root / ".agent/file-receipts.jsonl").unlink()
    return root


def _inv_missing_plan(root):
    return root / "missing"


def _inv_missing_contract(root):
    _r1(root)
    (root / ".agent/execution.contract.json").unlink()
    return root


def _inv_invalid_contract(root):
    _r1(root)
    path = root / ".agent/execution.contract.json"
    contract = json.loads(path.read_text())
    del contract["shell"]
    path.write_text(json.dumps(contract), encoding="utf-8")
    return root


def _inv_non_object_ledger_row(root):
    _r1(root)
    _ledger_text(root, "step-runs.jsonl", "[]\n")
    return root


def _inv_wrong_owner_renewal(root):
    plan = _explain_root(root, _explain_plan(), enforce=True)
    claim_step(root, plan, "P1", "agent-a")
    _step_row(root, event="lease_renewed", step_id="P1", attempt_id="A1", agent_id="agent-b")
    return root


def _inv_plan_before_ledger(root):
    _planned(lambda plan: plan.update(locked_at=5))(root)
    _step_row(root, event="bogus", step_id="P1", attempt_id="A1")
    return root


def _set_cycle(plan):
    plan["steps"][0]["depends_on"] = ["P2"]


def _set_unknown_dependency(plan):
    plan["steps"][1]["depends_on"] = ["P9"]


# name: (build, message needle, next-action reports the same message as state_invalid)
_EXPLAIN_INVALID = {
    "bogus-event": (_with_row(_r3, event="bogus", step_id="P1", attempt_id="A1"),
                    "invalid event 'bogus'", True),
    "attempt-in-two-steps": (_with_row(_r3, event="in_progress", step_id="P2", attempt_id="A1"),
                             "belongs to multiple steps", True),
    "after-terminal": (_with_row(_r8, event="verified", step_id="P1", attempt_id="A1"),
                       "events after a terminal event", True),
    "bogus-after-open-amendment": (_with_row(_r6, event="bogus", step_id="P1", attempt_id="A3"),
                                   "invalid event 'bogus'", True),
    "malformed-jsonl": (_inv_malformed_jsonl, "invalid JSONL", True),
    "receipt-missing-field": (_inv_receipt_missing_field,
                              "receipt step_id must be a non-empty string", True),
    "unknown-step": (_with_row(_r1, event="claimed", step_id="P9", attempt_id="A1", agent_id="x"),
                     "execution ledger references unknown steps: P9", True),
    "missing-ledger": (_inv_missing_ledger, ".agent/file-receipts.jsonl is missing", True),
    "plan-cycle": (_planned(_set_cycle), "depends_on cycle detected", True),
    "non-object-plan": (_overwrite(".agent/plan.lock.json", "[]"),
                        "top-level value must be a JSON object", True),
    "missing-plan": (_inv_missing_plan, "no .agent/plan.lock.json; run init", False),
    "unlocked-plan": (_planned(lambda plan: plan.update(locked=False)),
                      "plan exists but is not locked; run lock-plan", False),
    "missing-contract": (_inv_missing_contract,
                         ".agent/execution.contract.json is missing", False),
    "null-plan": (_overwrite(".agent/plan.lock.json", "null"),
                  ".agent/plan.lock.json top-level value must be a JSON object", False),
    "bad-locked-at": (_planned(lambda plan: plan.update(locked_at=5)),
                      "locked_at must be a string or null", True),
    "unknown-dependency": (_planned(_set_unknown_dependency),
                           "depends_on references unknown step id: P9", True),
    "non-object-contract": (_overwrite(".agent/execution.contract.json", "[]"),
                            "execution contract must be a JSON object", True),
    "contract-version": (_overwrite(".agent/execution.contract.json", "{}"),
                         "execution-contract schema_version must be MAJOR.MINOR.PATCH", True),
    "invalid-contract": (_inv_invalid_contract, "missing required field: shell", True),
    "non-object-ledger-row": (_inv_non_object_ledger_row,
                              ".agent/step-runs.jsonl:1: ledger entry must be a JSON object",
                              True),
    "empty-attempt-id": (_with_row(_r3, event="claimed", step_id="P2", attempt_id="", agent_id="x"),
                         "attempt_id must be a non-empty string", True),
    "owner-not-string": (_with_row(_r1, event="claimed", step_id="P1", attempt_id="A1", agent_id=7),
                         "agent_id must be a string", True),
    "deadline-not-string": (_with_row(_r1, event="claimed", step_id="P1", attempt_id="A1",
                                      agent_id="a", lease_expires_at=5),
                            "lease_expires_at must be a string or null", True),
    "duplicate-opener": (_with_row(_r3, event="claimed", step_id="P1", attempt_id="A1", agent_id="x"),
                         "attempt A1 has multiple opening events", True),
    "missing-opener": (_with_row(_r1, event="verified", step_id="P1", attempt_id="A1"),
                       "attempt A1 has no opening event before verified", True),
    "wrong-owner-renewal": (_inv_wrong_owner_renewal,
                            "attempt A1 renewed by agent-b instead of agent-a", True),
    "plan-before-ledger": (_inv_plan_before_ledger,
                           "locked_at must be a string or null", True),
}


class TestNextStepExplain(unittest.TestCase):
    """#71: next-step --explain classifies exactly the rows it validated."""

    def test_reason_code_matrix(self):
        seen = set()
        for name, (build, code, step_id, lines, absent) in _EXPLAIN_CASES.items():
            with self.subTest(name), TemporaryDirectory() as d:
                root = build(Path(d))
                result = porcelain.explain_next_step(root)
                seen.add(result["reason_code"])
                self.assertEqual(result["reason_code"], code)
                self.assertEqual((result["step"] or {}).get("id"), step_id)
                self.assertIsInstance(result["diagnostics"], list)
                self.assertEqual(len(result["diagnostics"]), len(lines), result["diagnostics"])
                for line, needles in zip(result["diagnostics"], lines):
                    for needle in needles:
                        self.assertIn(needle, line)
                for needle in absent:
                    self.assertNotIn(needle, "\n".join(result["diagnostics"]))
                plan = json.loads((root / ".agent/plan.lock.json").read_text())
                self.assertEqual(result["step"], next_step(root, plan))
                # E5: the wrappers equal the pure reducers on every R ledger
                self.assertEqual(read_step_state(root), step_state_from_events(read_step_events(root)))
                self.assertEqual(next_step(root, plan), eligible_step(read_step_state(root), plan))
        self.assertEqual(seen, set(porcelain.NEXT_STEP_REASON_CODES))  # C2

    def test_parallel_open_attempts_are_what_next_action_refuses(self):
        with TemporaryDirectory() as d:
            root = _EXPLAIN_CASES["R5"][0](Path(d))
            self.assertEqual(porcelain.next_action(root).state, "state_invalid")
            self.assertEqual(porcelain.explain_next_step(root)["reason_code"], "open_attempts")

    def test_invalid_state_raises_with_next_action_parity(self):
        for name, (build, needle, parity) in _EXPLAIN_INVALID.items():
            with self.subTest(name), TemporaryDirectory() as d:
                root = build(Path(d))
                with self.assertRaises(ValueError) as ctx:
                    porcelain.explain_next_step(root)
                self.assertIn(needle, str(ctx.exception))
                if parity:
                    action = porcelain.next_action(root)
                    self.assertEqual((action.state, action.diagnostics),
                                     ("state_invalid", [str(ctx.exception)]))

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode-000 file")
    def test_unreadable_ledger_raises_with_next_action_parity(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            _explain_root(root, _explain_plan())
            ledger = root / ".agent/command-receipts.jsonl"
            ledger.chmod(0)
            try:
                with self.assertRaises(ValueError) as ctx:
                    porcelain.explain_next_step(root)
                self.assertIn("Permission denied", str(ctx.exception))
                self.assertEqual(porcelain.next_action(root).diagnostics, [str(ctx.exception)])
            finally:
                ledger.chmod(0o644)

    def test_classifies_the_validated_rows_not_a_later_append(self):
        """Review Focus 1: a row landing after validation is never classified."""
        with TemporaryDirectory() as d:
            root = _EXPLAIN_CASES["R6"][0](Path(d))
            real = porcelain._projection_ledgers

            def read_then_append(r):
                ledgers = real(r)
                _step_row(r, event="bogus", step_id="P1", attempt_id="A3")
                return ledgers

            with mock.patch.object(porcelain, "_projection_ledgers", side_effect=read_then_append):
                result = porcelain.explain_next_step(root)
            self.assertEqual(result["reason_code"], "open_attempts")
            self.assertIn("A3", result["diagnostics"][0])


def _run_next_step(root: Path, *flags: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["next-step", *flags, "--root", str(root)])
    return rc, out.getvalue(), err.getvalue()


class TestNextStepExplainCli(unittest.TestCase):
    def _case(self, d, name):
        return _EXPLAIN_CASES[name][0](Path(d))

    def test_plain_output_is_unchanged(self):  # D1
        with TemporaryDirectory() as d:
            root = self._case(d, "R1")
            p1 = json.dumps(_explain_plan()["steps"][0], indent=2, sort_keys=True) + "\n"
            self.assertEqual(_run_next_step(root), (0, "P1\n", ""))
            self.assertEqual(_run_next_step(root, "--json"), (0, p1, ""))
        with TemporaryDirectory() as d:
            root = self._case(d, "R8")
            self.assertEqual(_run_next_step(root), (0, "no eligible step\n", ""))
            self.assertEqual(_run_next_step(root, "--json"), (0, "null\n", ""))

    def test_bogus_row_still_prints_null_without_explain(self):  # D2
        with TemporaryDirectory() as d:
            root = self._case(d, "R8")
            _step_row(root, event="bogus", step_id="P1", attempt_id="A1")
            self.assertEqual(_run_next_step(root, "--json"), (0, "null\n", ""))
            rc, out, err = _run_next_step(root, "--explain", "--json")
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("invalid event 'bogus'", err)

    def test_explain_json_wraps_the_plain_payload(self):  # D3, C1
        variant = JSON_OUTPUTS["next-step"][2]
        for name in _EXPLAIN_CASES:
            with self.subTest(name), TemporaryDirectory() as d:
                root = self._case(d, name)
                rc, out, err = _run_next_step(root, "--explain", "--json")
                self.assertEqual((rc, err), (0, ""))
                payload = json.loads(out)
                self.assertEqual(out, json.dumps(payload, indent=2, sort_keys=True) + "\n")
                self.assertEqual(set(payload), set(variant["keys"]))
                self.assertIn(payload["reason_code"], porcelain.NEXT_STEP_REASON_CODES)
                self.assertIsInstance(payload["diagnostics"], list)
                self.assertTrue(all(isinstance(line, str) for line in payload["diagnostics"]))
                self.assertIsInstance(payload["step"], (dict, type(None)))
                self.assertEqual(payload["step"], json.loads(_run_next_step(root, "--json")[1]))

    def test_human_explain_output(self):  # H1
        with TemporaryDirectory() as d:
            self.assertEqual(_run_next_step(self._case(d, "R1"), "--explain"),
                             (0, "P1\nreason: eligible\n", ""))
        with TemporaryDirectory() as d:
            rc, out, _ = _run_next_step(self._case(d, "R3"), "--explain")
            lines = out.splitlines()
            self.assertEqual(lines[:2], ["no eligible step", "reason: dependencies_unmet"])
            self.assertEqual(len(lines), 4)
            self.assertTrue(all(line.startswith("  step ") for line in lines[2:]))
            self.assertIn("A1", lines[2])
        with TemporaryDirectory() as d:
            self.assertEqual(_run_next_step(self._case(d, "R8"), "--explain"),
                             (0, "no eligible step\nreason: steps_complete\n", ""))

    def test_invalid_state_exits_1_with_the_message_on_stderr(self):  # E1, E2, Review Focus 3
        for name, (build, needle, _parity) in _EXPLAIN_INVALID.items():
            with self.subTest(name), TemporaryDirectory() as d:
                root = build(Path(d))
                with self.assertRaises(ValueError) as ctx:
                    # the CLI resolves --root (/var -> /private/var on macOS); path-bearing
                    # messages such as invalid JSONL must be compared on the same path
                    porcelain.explain_next_step(root.resolve())
                for flags in (["--explain"], ["--explain", "--json"]):
                    self.assertEqual(_run_next_step(root, *flags), (1, "", f"{ctx.exception}\n"))

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode-000 file")
    def test_unreadable_ledger_exits_1_in_both_modes(self):  # E1
        with TemporaryDirectory() as d:
            root = Path(d)
            _explain_root(root, _explain_plan())
            ledger = root / ".agent/command-receipts.jsonl"
            ledger.chmod(0)
            try:
                for flags in (["--explain"], ["--explain", "--json"]):
                    rc, out, err = _run_next_step(root, *flags)
                    self.assertEqual((rc, out), (1, ""))
                    self.assertIn("Permission denied", err)
            finally:
                ledger.chmod(0o644)

    def test_explain_flag_defaults_to_false(self):  # D4
        self.assertIs(cli.build_parser().parse_args(["next-step"]).explain, False)

    def _mcp_next_step(self, root):
        return mcp_server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "next_step", "arguments": {"root": str(root), "explain": True}},
        })["result"]

    def test_mcp_next_step_explain(self):  # C4
        with TemporaryDirectory() as d:
            result = self._mcp_next_step(self._case(d, "R3"))
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["data"]["reason_code"], "dependencies_unmet")
        with TemporaryDirectory() as d:
            result = self._mcp_next_step(_EXPLAIN_INVALID["bogus-event"][0](Path(d)))
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["exit_code"], 1)
            self.assertNotIn("data", result["structuredContent"])
            self.assertIn("invalid event 'bogus'", result["structuredContent"]["stderr"])


if __name__ == "__main__":
    unittest.main()
