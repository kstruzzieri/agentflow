from __future__ import annotations

import contextlib
import copy
import io
import json
import multiprocessing
import os
import shlex
import subprocess
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentflow import cli
from agentflow.artifacts import create_initial_artifacts, plan_binding_sha256, write_json
from agentflow.contracts import PLAN_SCHEMA_VERSION
from agentflow.confirmations import (
    DEFAULT_EXPIRY,
    argv_sha256,
    confirmations_path,
    issue_confirmation,
    match_confirmation,
    parse_expiry,
    read_confirmations,
)

from agentflow.execution import (
    claim_step,
    confirmation_mode,
    contract_schema_version_for,
    default_execution_contract,
    init_execution_artifacts,
    load_execution_contract,
    validate_execution_contract,
)
from agentflow.receipts import command_receipts, record_command, replay_gates, run_command


def _contract(version: str, **policy: str) -> dict:
    contract = default_execution_contract(".")
    contract["schema_version"] = version
    contract["command_policy"].update(policy)
    return contract


def _messages(contract: dict) -> list:
    return [f["message"] for f in validate_execution_contract(contract)]


class ContractVersionTests(unittest.TestCase):
    def test_v1_0_without_confirmation_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root)
            contract = load_execution_contract(root)
            self.assertEqual(contract["schema_version"], "1.0.0")
            self.assertNotIn("confirmation", contract["command_policy"])

    def test_v1_0_with_confirmation_is_rejected(self) -> None:
        contract = _contract("1.0.0", confirmation="bound")
        self.assertTrue(
            any("1.0.0 cannot carry confirmation" in m for m in _messages(contract))
        )
        with self.assertRaises(ValueError):
            confirmation_mode(contract)
        with self.assertRaises(ValueError):
            confirmation_mode(_contract("1.0.0", confirmation="self"))

    def test_v1_1_with_bound_loads(self) -> None:
        contract = _contract("1.1.0", confirmation="bound")
        self.assertEqual(_messages(contract), [])
        self.assertEqual(confirmation_mode(contract), "bound")

    def test_self_places_no_risk_policy_requirement(self) -> None:
        contract = _contract("1.1.0", confirmation="self", risk_policy="warn")
        self.assertEqual(_messages(contract), [])
        self.assertEqual(confirmation_mode(contract), "self")

    def test_bound_with_warn_is_rejected(self) -> None:
        contract = _contract("1.1.0", confirmation="bound", risk_policy="warn")
        self.assertTrue(
            any(
                "confirmation bound requires risk_policy require-confirmation" in m
                for m in _messages(contract)
            )
        )

    def test_unknown_mode_is_rejected(self) -> None:
        contract = _contract("1.1.0", confirmation="bnd")
        self.assertTrue(any("confirmation bnd is invalid" in m for m in _messages(contract)))
        with self.assertRaises(ValueError):
            confirmation_mode(contract)

    def test_v1_2_is_rejected(self) -> None:
        contract = _contract("1.2.0")
        self.assertTrue(any("incompatible" in m for m in _messages(contract)))

    def test_confirmation_mode_defaults_to_self(self) -> None:
        self.assertEqual(confirmation_mode(None), "self")
        self.assertEqual(confirmation_mode({}), "self")
        self.assertEqual(confirmation_mode(default_execution_contract(".")), "self")

    def test_writer_emits_lowest_version(self) -> None:
        contract = default_execution_contract(".")
        self.assertEqual(contract_schema_version_for(contract), "1.0.0")
        with_mode = copy.deepcopy(contract)
        with_mode["command_policy"]["confirmation"] = "bound"
        self.assertEqual(contract_schema_version_for(with_mode), "1.1.0")


HIGH = ["rm", "-rf", "build"]
NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)


def _plan() -> dict:
    return {"schema_version": PLAN_SCHEMA_VERSION, "objective": "o", "allowed_files": ["src/"], "steps": [{"id": "P1", "files": ["src/"]}]}


def _bound_root(tmp: str, mode: str = "bound") -> Path:
    root = Path(tmp)
    init_execution_artifacts(root)
    contract = load_execution_contract(root)
    contract["schema_version"] = "1.1.0"
    contract["command_policy"]["risk_policy"] = "require-confirmation"
    contract["command_policy"]["confirmation"] = mode
    write_json(root / ".agent/execution.contract.json", contract)
    write_json(root / ".agent/plan.lock.json", _plan())
    return root


class IssueTests(unittest.TestCase):
    def _refuses(self, root, step, argv, text):
        with self.assertRaises(ValueError) as ctx:
            issue_confirmation(root, _plan(), step, argv, DEFAULT_EXPIRY, now=NOW)
        self.assertIn(text, str(ctx.exception))
        self.assertFalse(confirmations_path(root).exists())

    def test_issue_writes_bound_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_root(tmp)
            row = issue_confirmation(root, _plan(), "P1", HIGH, DEFAULT_EXPIRY, now=NOW)
            self.assertEqual(
                set(row),
                {"schema_version", "id", "event", "step_id", "argv", "argv_sha256", "plan_sha256",
                 "execution_contract_sha256", "issued_at", "expires_at", "source"},
            )
            self.assertEqual(row["id"], "CC1")
            self.assertEqual(row["event"], "issued")
            self.assertEqual(row["argv"], HIGH)
            self.assertEqual(row["argv_sha256"], argv_sha256(HIGH))
            self.assertEqual(row["plan_sha256"], plan_binding_sha256(_plan()))
            self.assertEqual(row["source"], "self")
            delta = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["issued_at"])
            self.assertEqual(delta, timedelta(minutes=15))
            self.assertEqual(read_confirmations(root), [row])
            second = issue_confirmation(root, _plan(), "P1", HIGH, DEFAULT_EXPIRY, now=NOW)
            self.assertEqual(second["id"], "CC2")

    def test_issue_refuses_unknown_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._refuses(_bound_root(tmp), "P9", HIGH, "unknown step")

    def test_issue_refuses_non_high_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._refuses(_bound_root(tmp), "P1", ["echo", "hi"], "not high-risk")

    def test_issue_refuses_self_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._refuses(
                _bound_root(tmp, "self"), "P1", HIGH,
                "confirm-command needs command_policy.confirmation bound",
            )

    def test_issue_refuses_default_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_execution_artifacts(root)
            self._refuses(root, "P1", HIGH, "confirmation bound")

    def test_parse_expiry_cap(self) -> None:
        self.assertEqual(parse_expiry("24h"), timedelta(hours=24))
        self.assertEqual(parse_expiry("15m"), timedelta(minutes=15))
        with self.assertRaisesRegex(ValueError, "capped at 24h"):
            parse_expiry("25h")
        for bad in ("0m", "15", "-1m", "1d", ""):
            with self.assertRaises(ValueError):
                parse_expiry(bad)


def _issued(n, **over):
    row = {
        "schema_version": "1.0.0", "id": f"CC{n}", "event": "issued", "step_id": "P1",
        "argv": HIGH, "argv_sha256": argv_sha256(HIGH), "plan_sha256": "p", "execution_contract_sha256": "c",
        "issued_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=15)).isoformat(), "source": "self",
    }
    row.update(over)
    return row


def _consumed(n):
    return {"schema_version": "1.0.0", "id": f"CC{n}", "event": "consumed", "attempt_id": "A1",
            "consumed_at": NOW.isoformat()}


class MatchTests(unittest.TestCase):
    def _match(self, rows, argv=HIGH, plan="p", contract="c", now=NOW):
        return match_confirmation(rows, "P1", argv_sha256(argv), plan, contract, now)

    def test_usable_lowest_id(self) -> None:
        self.assertEqual(self._match([_issued(2), _issued(1)]), ("CC1", None))

    def test_consumed_skipped_for_next(self) -> None:
        self.assertEqual(self._match([_issued(1), _consumed(1), _issued(2)]), ("CC2", None))

    def test_used(self) -> None:
        self.assertEqual(self._match([_issued(1), _consumed(1)]), (None, "confirmation_used"))

    def test_expired(self) -> None:
        later = NOW + timedelta(minutes=16)
        self.assertEqual(self._match([_issued(1)], now=later), (None, "confirmation_expired"))

    def test_expiry_equal_to_now_is_expired(self) -> None:
        at = NOW + timedelta(minutes=15)
        self.assertEqual(self._match([_issued(1)], now=at), (None, "confirmation_expired"))

    def test_expiry_compared_as_instants(self) -> None:
        row = _issued(1, expires_at="2026-11-12T13:30:00+01:00")  # 12:30Z
        self.assertEqual(self._match([row]), ("CC1", None))

    def test_stale_on_plan_or_contract_change(self) -> None:
        self.assertEqual(self._match([_issued(1)], plan="other"), (None, "confirmation_stale"))
        self.assertEqual(self._match([_issued(1)], contract="other"), (None, "confirmation_stale"))

    def test_missing(self) -> None:
        self.assertEqual(self._match([]), (None, "confirmation_missing"))
        self.assertEqual(self._match([_issued(1)], argv=["rm", "-rf", "dist"]), (None, "confirmation_missing"))

    def test_sudo_prefix_does_not_match(self) -> None:
        self.assertEqual(self._match([_issued(1)], argv=["sudo"] + HIGH), (None, "confirmation_missing"))


class ConfirmCommandCliTests(unittest.TestCase):
    def _run(self, root, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["confirm-command", "--root", str(root), "--step", "P1", *extra, "--", *HIGH])
        return code, out.getvalue(), err.getvalue()

    def test_text_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_root(tmp)
            code, out, _ = self._run(root)
            self.assertEqual(code, 0)
            self.assertIn("CC1", out)
            self.assertIn("rm -rf build", out)
            self.assertIn("expires", out)
            code, out, _ = self._run(root, "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["id"], "CC2")

    def test_refusal_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_root(tmp, "self")
            code, out, err = self._run(root)
            self.assertEqual(code, 2)
            self.assertIn("confirmation bound", err)
            self.assertFalse(confirmations_path(root).exists())

    def test_expiry_cap_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self._run(_bound_root(tmp), "--expires", "25h")
            self.assertEqual(code, 2)
            self.assertIn("capped at 24h", err)



RUN = ["sh", "-c", "echo x >> runs.txt; rm -rf build"]
FAIL = ["sh", "-c", "echo x >> runs.txt; rm -rf build; exit 3"]
# exec: the timeout kill must reach sleep itself, or its open pipe stalls run.
SLOW = ["sh", "-c", "echo x >> runs.txt; rm -rf build; exec sleep 5"]
CONTRACT = ".agent/execution.contract.json"
BARRIER_TIMEOUT = 30


def _run_plan() -> dict:
    step = {
        "action": "Run high-risk commands.",
        "files": ["*.txt"],
        "preconditions": [],
        "expected_diff": [],
        "validation": ["slow"],
        "gates": [{"kind": "command", "run": SLOW, "timeout_seconds": 1}],
        "evidence_ids": [],
    }
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objective": "Exercise bound confirmations.",
        "scope": ["Run high-risk commands under bound."],
        "non_goals": [],
        "invariants": ["Each confirmation authorizes one run."],
        "allowed_files": ["*.txt", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["slow"],
        "rollback_plan": "Discard the temp worktree.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [{"id": "P1", **step}, {"id": "P2", **step}],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


def _bound_run_root(tmp: str, version: str = "1.1.0", **policy) -> Path:
    """A git root with P1 claimed; confirmation bound unless overridden (None drops it)."""
    root = Path(tmp)
    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    create_initial_artifacts(root)
    init_execution_artifacts(root)
    contract = load_execution_contract(root)
    contract["schema_version"] = version
    command_policy = contract["command_policy"]
    command_policy.update({"risk_policy": "require-confirmation", "confirmation": "bound"})
    command_policy.update(policy)
    for key in [k for k, v in command_policy.items() if v is None]:
        del command_policy[key]
    write_json(root / CONTRACT, contract)
    write_json(root / ".agent/plan.lock.json", _run_plan())
    claim_step(root, _run_plan(), "P1", "agent-a")
    return root


def _runs(root: Path) -> int:
    path = root / "runs.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _concurrent_run_worker(args) -> dict:
    root_str, barrier = args
    barrier.wait(timeout=BARRIER_TIMEOUT)
    return run_command(Path(root_str), _run_plan(), "P1", None, RUN)


class BoundRunTests(unittest.TestCase):
    def _issue(self, root, argv=RUN, step="P1", now=None):
        return issue_confirmation(root, _run_plan(), step, argv, DEFAULT_EXPIRY, now=now)

    def _blocked(self, root, reason, argv=RUN, plan=None):
        before, runs = read_confirmations(root), _runs(root)
        receipt = run_command(root, plan or _run_plan(), "P1", None, argv)
        self.assertEqual(receipt["decision"], "blocked")
        self.assertEqual(receipt["confirmation_block_reason"], reason)
        self.assertNotIn("confirmation_id", receipt)
        self.assertNotIn("confirmed", receipt)
        self.assertEqual(read_confirmations(root), before)
        self.assertEqual(_runs(root), runs)
        return receipt

    def _consumed(self, root):
        return [r for r in read_confirmations(root) if r.get("event") == "consumed"]

    def test_block_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._blocked(_bound_run_root(tmp), "confirmation_missing")

    def test_block_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root, now=datetime.now(timezone.utc) - timedelta(hours=1))
            self._blocked(root, "confirmation_expired")

    def test_block_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root)
            self.assertEqual(run_command(root, _run_plan(), "P1", None, RUN)["decision"], "allowed")
            self._blocked(root, "confirmation_used")

    def test_block_stale_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root)
            relocked = _run_plan()
            relocked["objective"] = "Changed after the confirmation."
            self._blocked(root, "confirmation_stale", plan=relocked)

    def test_block_stale_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root)
            contract = load_execution_contract(root)
            contract["command_policy"]["max_output_bytes"] = 1234
            write_json(root / CONTRACT, contract)
            self._blocked(root, "confirmation_stale")

    def test_block_wrong_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root, step="P2")
            self._blocked(root, "confirmation_missing")

    def test_block_argv_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root, argv=["sh", "-c", "echo y >> runs.txt; rm -rf build"])
            # sudo plus the same argv is a different confirmation. Issued for the
            # sudo form so a regression would run the plain command, not sudo.
            self._issue(root, argv=["sudo"] + RUN)
            self._blocked(root, "confirmation_missing")

    def test_bound_run_consumes_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root)
            # Under bound the old switches are ignored, not recorded.
            receipt = run_command(
                root, _run_plan(), "P1", None, RUN, confirmed=True, confirmation_source="env"
            )
            self.assertEqual(receipt["schema_version"], "1.2.0")
            self.assertEqual(receipt["decision"], "allowed")
            self.assertEqual(receipt["exit_code"], 0)
            self.assertIs(receipt["confirmed"], True)
            self.assertEqual(receipt["confirmation_id"], "CC1")
            self.assertEqual(receipt["risk_policy"], "require-confirmation")
            self.assertNotIn("confirmation_source", receipt)
            self.assertNotIn("confirmation_block_reason", receipt)
            consumed = self._consumed(root)
            self.assertEqual(len(consumed), 1)
            self.assertEqual(consumed[0]["id"], "CC1")
            self.assertEqual(consumed[0]["attempt_id"], receipt["attempt_id"])
            self.assertEqual(consumed[0]["schema_version"], "1.0.0")
            datetime.fromisoformat(consumed[0]["consumed_at"])
            self.assertEqual(_runs(root), 1)
            self.assertEqual(command_receipts(root), [receipt])

    def test_consumed_even_when_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root, argv=FAIL)
            receipt = run_command(root, _run_plan(), "P1", None, FAIL)
            self.assertEqual((receipt["decision"], receipt["exit_code"]), ("allowed", 3))
            self.assertEqual(receipt["confirmation_id"], "CC1")
            self.assertEqual([r["id"] for r in self._consumed(root)], ["CC1"])
            self._blocked(root, "confirmation_used", argv=FAIL)

    def test_consumed_even_when_command_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root, argv=SLOW)
            receipt = run_command(root, _run_plan(), "P1", None, SLOW)
            self.assertEqual(receipt["decision"], "timeout")
            self.assertEqual(receipt["timeout_seconds"], 1)
            self.assertEqual(receipt["confirmation_id"], "CC1")
            self.assertEqual([r["id"] for r in self._consumed(root)], ["CC1"])
            self._blocked(root, "confirmation_used", argv=SLOW)

    def _cli_run(self, root, *flags, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["run", "--root", str(root), "--step", "P1", *flags, "--", *RUN])
        return code, err.getvalue()

    def test_bound_ignores_confirm_risk_and_env(self) -> None:
        hint = (
            " Confirmation is bound: run `agentflow confirm-command --step P1 -- "
            + shlex.join(RUN)
            + "` first; --confirm-risk and AGENTFLOW_CONFIRM_RISK=1 are not accepted."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            for flags, env in ((["--confirm-risk"], None), ([], {"AGENTFLOW_CONFIRM_RISK": "1"})):
                with self.subTest(flags=flags, env=env):
                    code, err = self._cli_run(root, *flags, env=env)
                    self.assertEqual(code, 2)
                    self.assertIn("confirm-command", err)
                    self.assertTrue(err.rstrip("\n").endswith(hint), err)
                    self.assertNotIn("Re-run with --confirm-risk", err)
                    receipt = command_receipts(root)[-1]
                    self.assertEqual(receipt["decision"], "blocked")
                    self.assertEqual(receipt["confirmation_block_reason"], "confirmation_missing")
                    self.assertNotIn("confirmation_source", receipt)
            self.assertEqual(_runs(root), 0)
            self.assertFalse(confirmations_path(root).exists())

    @unittest.skipIf(os.name == "nt", "fcntl-backed POSIX locking; Windows covered by locks.py tests")
    def test_concurrent_double_use_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root)
            with multiprocessing.Manager() as manager:
                barrier = manager.Barrier(2)
                with ProcessPoolExecutor(max_workers=2) as pool:
                    receipts = list(pool.map(_concurrent_run_worker, [(str(root), barrier)] * 2))
            decisions = sorted(r["decision"] for r in receipts)
            self.assertEqual(decisions, ["allowed", "blocked"])
            blocked = next(r for r in receipts if r["decision"] == "blocked")
            self.assertEqual(blocked["confirmation_block_reason"], "confirmation_used")
            self.assertEqual(_runs(root), 1)
            self.assertEqual([r["id"] for r in self._consumed(root)], ["CC1"])

    def test_replay_stays_blocked_under_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            record_command(root, "P1", None, RUN, 0, gate="g", provenance="attested", plan=_run_plan())
            issued = self._issue(root)
            result = replay_gates(root, _run_plan(), step_id="P1", record=True)
            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "CR1 replay blocked by risk_policy require-confirmation",
                result["errors"][0]["message"],
            )
            self.assertEqual(read_confirmations(root), [issued])
            self.assertEqual(_runs(root), 0)

    def test_block_policy_never_consumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp, risk_policy="block")
            issued = self._issue(root)
            receipt = run_command(root, _run_plan(), "P1", None, RUN)
            self.assertEqual(receipt["decision"], "blocked")
            self.assertNotIn("confirmation_block_reason", receipt)
            self.assertEqual(read_confirmations(root), [issued])
            self.assertEqual(_runs(root), 0)

    def test_non_high_command_never_touches_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            receipt = run_command(root, _run_plan(), "P1", None, ["echo", "hi"])
            self.assertEqual(receipt["decision"], "allowed")
            self.assertNotIn("confirmation_id", receipt)
            self.assertFalse(confirmations_path(root).exists())
            self.assertFalse(Path(str(confirmations_path(root)) + ".lockfile").exists())

    def test_self_mode_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp, version="1.0.0", confirmation=None)
            blocked = run_command(root, _run_plan(), "P1", None, RUN)
            self.assertEqual(blocked["decision"], "blocked")
            self.assertNotIn("confirmation_block_reason", blocked)
            code, err = self._cli_run(root)
            self.assertEqual(code, 2)
            self.assertIn("Re-run with --confirm-risk to override.", err)
            self.assertNotIn("confirm-command", err)
            for flags, env, source in (
                (["--confirm-risk"], None, "cli"),
                ([], {"AGENTFLOW_CONFIRM_RISK": "1"}, "env"),
            ):
                with self.subTest(source=source):
                    code, _ = self._cli_run(root, *flags, env=env)
                    self.assertEqual(code, 0)
                    receipt = command_receipts(root)[-1]
                    self.assertEqual(receipt["decision"], "allowed")
                    self.assertIs(receipt["confirmed"], True)
                    self.assertEqual(receipt["confirmation_source"], source)
                    self.assertNotIn("confirmation_id", receipt)
            self.assertEqual(_runs(root), 2)
            self.assertFalse(confirmations_path(root).exists())

    def test_invalid_confirmation_contract_fails_closed(self) -> None:
        # A typo'd mode, a 1.0.0 contract carrying the field, or an unreadable
        # contract must not degrade bound to self: run exits 2 and launches nothing.
        cases = {
            "typo": lambda root: _set_policy(root, confirmation="bnd"),
            "1.0.0 with field": lambda root: _set_version(root, "1.0.0"),
            "malformed json": lambda root: (root / CONTRACT).write_text("{", encoding="utf-8"),
        }
        for name, corrupt in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = _bound_run_root(tmp)
                corrupt(root)
                for flags in ([], ["--confirm-risk"]):
                    code, err = self._cli_run(root, *flags)
                    self.assertEqual(code, 2, err)
                self.assertEqual(_runs(root), 0)
                self.assertEqual(command_receipts(root), [])
                self.assertFalse(confirmations_path(root).exists())

    def test_missing_contract_keeps_advisory_degrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            (root / CONTRACT).unlink()
            receipt = run_command(root, _run_plan(), "P1", None, RUN)
            self.assertEqual(receipt["decision"], "allowed")
            self.assertNotIn("confirmation_id", receipt)
            self.assertEqual(_runs(root), 1)


class ReceiptSchemaTests(unittest.TestCase):
    def test_receipt_schema_declares_bound_fields(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schemas/command-receipts.schema.json")
            .read_text(encoding="utf-8")
        )
        props = schema["properties"]
        self.assertEqual(props["confirmation_id"], {"type": "string", "pattern": "^CC[1-9][0-9]*$"})
        self.assertEqual(
            props["confirmation_block_reason"],
            {"enum": ["confirmation_expired", "confirmation_missing",
                      "confirmation_stale", "confirmation_used"]},
        )
        self.assertEqual(props["confirmation_source"], {"enum": ["cli", "env"]})
        self.assertNotIn("confirmation_id", schema["required"])
        self.assertNotIn("confirmation_block_reason", schema["required"])
        for version in ("1.0.0", "1.1.0", "1.2.0"):
            self.assertRegex(version, props["schema_version"]["pattern"])
        self.assertNotRegex("1.3.0", props["schema_version"]["pattern"])


def _set_policy(root: Path, **policy) -> None:
    contract = json.loads((root / CONTRACT).read_text(encoding="utf-8"))
    contract["command_policy"].update(policy)
    write_json(root / CONTRACT, contract)


def _set_version(root: Path, version: str) -> None:
    contract = json.loads((root / CONTRACT).read_text(encoding="utf-8"))
    contract["schema_version"] = version
    write_json(root / CONTRACT, contract)


if __name__ == "__main__":
    unittest.main()
