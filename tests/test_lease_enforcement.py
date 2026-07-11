from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentflow.artifacts import create_initial_artifacts, write_json
from agentflow.execution import (
    attempt_is_expired,
    claim_step,
    complete_step,
    fail_step,
    init_execution_artifacts,
    mark_step_verified,
    read_step_state,
    reclaim_step,
    renew_lease,
    require_writable_attempt,
)

ENFORCE = {"concurrency": {"lease_policy": "enforce", "lease_ttl_minutes": 30,
                           "lease_grace_seconds": 30}}


def plan() -> dict:
    return {
        "schema_version": "0.3.0", "objective": "Lease enforcement fixture.",
        "scope": ["s"], "non_goals": [], "invariants": ["i"],
        "allowed_files": [".agent/", "f.txt"], "blocked_files": [],
        "validation_gates": ["python3 -c \"print('ok')\""],
        "rollback_plan": "r", "risk_level": "low",
        "drift_budget": {"unrelated_edits": 0, "new_dependencies": 0,
                         "formatting_drift": "minimal",
                         "architecture_drift": "requires_approval", "test_weakening": 0},
        "steps": [{"id": "P1", "action": "a", "files": ["f.txt"],
                   "preconditions": ["p"], "expected_diff": ["d"],
                   "validation": ["python3 -c \"print('ok')\""], "evidence_ids": []}],
        "evidence_ids": [], "locked": True, "locked_at": "2026-06-01T00:00:00+00:00",
    }


def _root(tmp: str, contract_overlay: dict | None = None) -> Path:
    root = Path(tmp)
    subprocess.run(["git", "init"], cwd=str(root), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    create_initial_artifacts(root)
    init_execution_artifacts(root)
    write_json(root / ".agent/plan.lock.json", plan())
    if contract_overlay:
        from agentflow.artifacts import read_json
        contract = read_json(root / ".agent/execution.contract.json")
        contract["concurrency"].update(contract_overlay["concurrency"])
        write_json(root / ".agent/execution.contract.json", contract)
    return root


class ProjectionTests(unittest.TestCase):
    def test_projection_tracks_owner_and_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            attempt = read_step_state(root)["attempts"]["A1"]
            self.assertEqual(attempt["agent_id"], "agent-a")
            self.assertIsNotNone(attempt["lease_expires_at"])

    def test_attempt_is_expired_respects_grace(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0)
        attempt = {"lease_expires_at": past.isoformat()}
        now = datetime.now(timezone.utc)
        self.assertTrue(attempt_is_expired(attempt, now, grace_seconds=0))
        self.assertFalse(attempt_is_expired(attempt, now, grace_seconds=120))

    def test_none_deadline_never_expires(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertFalse(attempt_is_expired({"lease_expires_at": None}, now, grace_seconds=0))


class ClaimMutexTests(unittest.TestCase):
    def test_second_claim_on_live_lease_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            with self.assertRaises(ValueError) as ctx:
                claim_step(root, plan(), "P1", "agent-b")
            self.assertIn("leased to agent-a", str(ctx.exception))

    def test_claim_rejects_non_positive_lease_minutes(self) -> None:
        for minutes in (0, -1):
            with self.subTest(minutes=minutes):
                with tempfile.TemporaryDirectory() as tmp:
                    root = _root(tmp, ENFORCE)
                    with self.assertRaises(ValueError) as ctx:
                        claim_step(root, plan(), "P1", "agent-a", lease_minutes=minutes)
                    self.assertIn("positive integer", str(ctx.exception))

    def test_claim_on_expired_lease_prints_reclaim_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            with self.assertRaises(ValueError) as ctx:
                claim_step(root, plan(), "P1", "agent-b", now=future)
            self.assertIn("reclaim-step P1", str(ctx.exception))

    def test_naive_now_does_not_raise_typeerror(self) -> None:
        # A naive injected clock must be coerced tz-aware before comparison,
        # so the expired-lease branch fires (ValueError) instead of a TypeError.
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            with self.assertRaises(ValueError) as ctx:
                claim_step(root, plan(), "P1", "agent-b", now=datetime(2999, 1, 1))
            self.assertIn("reclaim-step", str(ctx.exception))

    def test_reclaim_expired_abandons_and_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            reclaim_step(root, plan(), "P1", "agent-b", reason="a crashed", now=future)
            state = read_step_state(root)
            self.assertFalse(state["attempts"]["A1"]["open"])
            self.assertEqual(state["attempts"]["A1"]["status"], "abandoned")
            self.assertTrue(state["attempts"]["A2"]["open"])
            self.assertEqual(state["attempts"]["A2"]["agent_id"], "agent-b")

    def test_reclaim_live_lease_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            with self.assertRaises(ValueError) as ctx:
                reclaim_step(root, plan(), "P1", "agent-b", reason="impatient")
            self.assertIn("still leased", str(ctx.exception))

    def test_reclaim_rejects_invalid_lease_minutes_before_abandoning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            with self.assertRaises(ValueError) as ctx:
                reclaim_step(
                    root, plan(), "P1", "agent-b",
                    reason="crashed", lease_minutes=0, now=future,
                )
            self.assertIn("positive integer", str(ctx.exception))
            self.assertEqual(read_step_state(root)["attempts"]["A1"]["status"], "claimed")

    def test_advisory_second_claim_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)  # advisory default
            claim_step(root, plan(), "P1", "agent-a")
            claim_step(root, plan(), "P1", "agent-b")  # no raise
            self.assertEqual(len(read_step_state(root)["steps"]["P1"]["open_attempts"]), 2)


class WriteGateTests(unittest.TestCase):
    def test_foreign_writer_rejected_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            with self.assertRaises(ValueError) as ctx:
                require_writable_attempt(root, "P1", "A1", new_work=True, agent_id="agent-b")
            self.assertIn("owned", str(ctx.exception).lower())

    def test_owner_write_allowed_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            require_writable_attempt(root, "P1", "A1", new_work=True, agent_id="agent-a")

    def test_missing_agent_write_rejected_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            with self.assertRaises(ValueError) as ctx:
                require_writable_attempt(root, "P1", "A1", new_work=True)
            self.assertIn("AGENTFLOW_AGENT_ID", str(ctx.exception))

    def test_expired_owner_write_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            with self.assertRaises(ValueError) as ctx:
                require_writable_attempt(root, "P1", "A1", new_work=True,
                                         agent_id="agent-a", now=future)
            self.assertIn("expired", str(ctx.exception).lower())

    def test_missing_owner_rejected_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            path = root / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[-1].pop("agent_id")
            path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                require_writable_attempt(root, "P1", "A1", new_work=True, agent_id="agent-a")
            self.assertIn("has no owner", str(ctx.exception))

    def test_renew_extends_deadline_and_unblocks_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            just_after = datetime.now(timezone.utc) + timedelta(minutes=31)
            renew_lease(root, "P1", "A1", "agent-a", minutes=120)
            require_writable_attempt(root, "P1", "A1", new_work=True,
                                     agent_id="agent-a", now=just_after)

    def test_advisory_foreign_writer_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp)
            claim_step(root, plan(), "P1", "agent-a")
            require_writable_attempt(root, "P1", "A1", new_work=True, agent_id="agent-b")


class RenewLeaseTests(unittest.TestCase):
    def test_owner_can_renew_expired_lease(self) -> None:
        # Self-recovery: the owning agent renews even after the finite lease
        # has expired (addendum item 3).
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            recorded = renew_lease(root, "P1", "A1", "agent-a", minutes=60, now=future)
            self.assertEqual(recorded["event"], "lease_renewed")
            # After renewal, the owner can write again at that same future clock.
            require_writable_attempt(root, "P1", "A1", new_work=True,
                                     agent_id="agent-a", now=future)

    def test_foreign_agent_cannot_renew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            with self.assertRaises(ValueError) as ctx:
                renew_lease(root, "P1", "A1", "agent-b", minutes=60, now=future)
            self.assertIn("owned", str(ctx.exception).lower())

    def test_renew_rejects_unknown_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)

            with self.assertRaises(ValueError) as ctx:
                renew_lease(root, "P1", "A404", "agent-a", minutes=60)

            self.assertIn("was never opened", str(ctx.exception))
            self.assertNotIn("A404", read_step_state(root)["attempts"])

    def test_renew_rejects_non_positive_minutes(self) -> None:
        for minutes in (0, -1):
            with self.subTest(minutes=minutes):
                with tempfile.TemporaryDirectory() as tmp:
                    root = _root(tmp, ENFORCE)
                    claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
                    with self.assertRaises(ValueError) as ctx:
                        renew_lease(root, "P1", "A1", "agent-a", minutes=minutes)
                    self.assertIn("positive integer", str(ctx.exception))

    def test_renew_does_not_mutate_prior_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claimed = claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            original_deadline = claimed["lease_expires_at"]
            renew_lease(root, "P1", "A1", "agent-a", minutes=120)
            events = read_step_state(root)["attempts"]["A1"]["events"]
            self.assertEqual(events[0]["event"], "claimed")
            self.assertEqual(events[0]["lease_expires_at"], original_deadline)
            self.assertEqual(events[-1]["event"], "lease_renewed")
            # Projection surfaces the newest deadline, not the claimed one.
            self.assertNotEqual(
                read_step_state(root)["attempts"]["A1"]["lease_expires_at"],
                original_deadline,
            )


class LifecycleGateTests(unittest.TestCase):
    def test_foreign_verify_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            with self.assertRaises(ValueError):
                mark_step_verified(root, "P1", "A1", [], agent_id="agent-b")

    def test_owner_verify_complete_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            mark_step_verified(root, "P1", "A1", [], agent_id="agent-a")
            complete_step(root, "P1", "A1", agent_id="agent-a")
            self.assertTrue(read_step_state(root)["steps"]["P1"]["completed"])

    def test_expired_owner_complete_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            mark_step_verified(root, "P1", "A1", [], agent_id="agent-a")
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            with self.assertRaises(ValueError) as ctx:
                complete_step(root, "P1", "A1", agent_id="agent-a", now=future)
            self.assertIn("expired", str(ctx.exception).lower())

    def test_fail_step_is_break_glass_and_records_failed_by(self) -> None:
        # fail-step never enforces ownership; a foreign agent may break-glass.
        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=30)
            recorded = fail_step(root, "P1", "A1", "wedged", agent_id="agent-b")
            self.assertEqual(recorded["failed_by"], "agent-b")


class AutoRenewTests(unittest.TestCase):
    def test_run_autorenews_short_lease_before_long_command(self) -> None:
        from agentflow.receipts import run_command

        with tempfile.TemporaryDirectory() as tmp:
            root = _root(tmp, ENFORCE)
            # Claim with a 1-minute lease; command timeout dwarfs it => renew fires.
            claim_step(root, plan(), "P1", "agent-a", lease_minutes=1)
            receipt = run_command(root, plan(), "P1", "A1", ["true"], agent_id="agent-a")
            self.assertEqual(receipt["exit_code"], 0)
            events = [e["event"] for e in read_step_state(root)["attempts"]["A1"]["events"]]
            self.assertIn("lease_renewed", events)


if __name__ == "__main__":
    unittest.main()
