from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import multiprocessing
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentflow import cli
from agentflow import proof as proof_module
from agentflow.artifacts import (
    append_jsonl,
    create_initial_artifacts,
    ledger_lock_path,
    plan_binding_sha256,
    write_json,
)
from agentflow.contracts import CONFIRMATION_MODES, PLAN_SCHEMA_VERSION
from agentflow.confirmations import (
    DEFAULT_EXPIRY,
    argv_sha256,
    confirmations_path,
    consume_confirmation,
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
from agentflow.locks import file_lock
from agentflow.receipts import command_receipts, record_command, replay_gates, run_command
from agentflow.versioning import validate_schema_version


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

    def test_confirmation_mode_refuses_what_run_refuses(self) -> None:
        # One rule for the validator, run, confirm-command and verify-proof.
        with self.assertRaisesRegex(
            ValueError, "^confirmation bound requires risk_policy require-confirmation$"
        ):
            confirmation_mode(_contract("1.1.0", confirmation="bound", risk_policy="warn"))
        for contract in ([], [1], "x", 3):
            with self.subTest(contract=contract):
                with self.assertRaisesRegex(ValueError, "^execution contract must be an object$"):
                    confirmation_mode(contract)

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

    def test_issue_refuses_bound_without_require_confirmation(self) -> None:
        # run refuses this contract, so confirm-command must not issue for it.
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_root(tmp)
            contract = load_execution_contract(root)
            contract["command_policy"]["risk_policy"] = "warn"
            write_json(root / ".agent/execution.contract.json", contract)
            self._refuses(root, "P1", HIGH, "confirmation bound requires risk_policy require-confirmation")

    def test_read_confirmations_rejects_malformed_rows(self) -> None:
        v = {"schema_version": "1.0.0"}
        rows = ([], "x", {**v, "event": "issued"}, {**v, "id": ["CC1"], "event": "issued"},
                {**v, "id": "CC1", "event": []})
        for row in rows:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                root = _bound_root(tmp)
                confirmations_path(root).write_text(
                    json.dumps(_issued(1)) + "\n" + json.dumps(row) + "\n", encoding="utf-8"
                )
                # read_jsonl rejects a non-object row before read_confirmations sees it.
                expected = (
                    r"/\.agent/command-confirmations\.jsonl:2: ledger entry must be a JSON object$"
                    if not isinstance(row, dict)
                    else "^.agent/command-confirmations.jsonl row 2 must be an object with a string id and event$"
                )
                with self.assertRaisesRegex(ValueError, expected):
                    read_confirmations(root)

    def test_parse_expiry_cap(self) -> None:
        self.assertEqual(parse_expiry("24h"), timedelta(hours=24))
        self.assertEqual(parse_expiry("15m"), timedelta(minutes=15))
        for over in ("25h", "1441m", "99999999999999999999h", "99999999999999999999m"):
            with self.subTest(expires=over), self.assertRaisesRegex(ValueError, "^--expires is capped at 24h$"):
                parse_expiry(over)
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

    def test_argv_hash_keeps_non_ascii_as_utf8(self) -> None:
        argv = ["rm", "-rf", "caf\u00e9"]
        expected = hashlib.sha256(
            json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        escaped = hashlib.sha256(
            json.dumps(argv, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(argv_sha256(argv), expected)
        self.assertNotEqual(argv_sha256(argv), escaped)

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
        for expires in ("25h", "99999999999999999999h"):
            with self.subTest(expires=expires), tempfile.TemporaryDirectory() as tmp:
                code, _, err = self._run(_bound_root(tmp), "--expires", expires)
                self.assertEqual(code, 2)
                self.assertIn("capped at 24h", err)

    def test_unusable_contract_or_ledger_exits_2(self) -> None:
        def array_contract(root):
            (root / ".agent/execution.contract.json").write_text(json.dumps([1]), encoding="utf-8")

        def warn_policy(root):
            contract = load_execution_contract(root)
            contract["command_policy"]["risk_policy"] = "warn"
            write_json(root / ".agent/execution.contract.json", contract)

        def bad_row(row):
            return lambda root: confirmations_path(root).write_text(row + "\n", encoding="utf-8")

        cases = {
            "JSON array contract": (array_contract, "execution contract must be an object"),
            "bound with warn": (warn_policy, "confirmation bound requires risk_policy require-confirmation"),
            "non-object row": (
                bad_row("[]"), ".agent/command-confirmations.jsonl:1: ledger entry must be a JSON object"
            ),
            "list event": (
                bad_row('{"schema_version": "1.0.0", "id": "CC1", "event": []}'), "row 1 must be an object"
            ),
        }
        for name, (corrupt, text) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = _bound_root(tmp)
                corrupt(root)
                before = confirmations_path(root).read_bytes() if confirmations_path(root).exists() else None
                code, out, err = self._run(root)
                self.assertEqual(code, 2, err)
                self.assertIn(text, err)
                after = confirmations_path(root).read_bytes() if confirmations_path(root).exists() else None
                self.assertEqual(after, before)



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


def _contended_consume_worker(args):
    root_str, contract_hash, started = args
    started.set()
    return consume_confirmation(Path(root_str), _run_plan(), "P1", "A1", RUN, contract_hash)


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

    def test_confirmation_expiring_while_waiting_for_lock_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root, now=NOW)
            clock = [NOW]

            @contextlib.contextmanager
            def delayed_lock(path):
                with file_lock(path):
                    clock[0] = NOW + DEFAULT_EXPIRY
                    yield

            with mock.patch("agentflow.confirmations.file_lock", delayed_lock), \
                    mock.patch("agentflow.confirmations._now", side_effect=lambda value: clock[0] if value is None else value):
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

    def test_block_argv_with_trailing_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            self._issue(root)
            self._blocked(root, "confirmation_missing", argv=RUN[:-1] + [RUN[-1] + " "])

    def test_block_plain_confirmation_offered_to_wrapped_argv(self) -> None:
        # normalize_command strips env, so risk sees the same command; the
        # confirmation is bound to the exact argv and must not carry over.
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            (root / "build").mkdir()
            self._issue(root, argv=["rm", "-rf", "build"])
            self._blocked(root, "confirmation_missing", argv=["env", "rm", "-rf", "build"])
            self.assertTrue((root / "build").is_dir())

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

    @unittest.skipIf(os.name == "nt", "fcntl-backed POSIX locking; Windows covered by locks.py tests")
    def test_consume_waits_for_the_ledger_lock_and_rereads_under_it(self) -> None:
        # The attempt lock does not apply here: only the confirmations lock
        # stands between the child's match and a row the parent writes.
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            issued = self._issue(root)
            spent = {"schema_version": "1.0.0", "id": "CC1", "event": "consumed",
                     "attempt_id": "A9", "consumed_at": issued["issued_at"]}
            with multiprocessing.Manager() as manager, \
                    ProcessPoolExecutor(max_workers=1) as pool:
                started = manager.Event()
                pool.submit(os.getpid).result()  # start the worker before locking
                with file_lock(ledger_lock_path(confirmations_path(root))):
                    future = pool.submit(
                        _contended_consume_worker,
                        (str(root), issued["execution_contract_sha256"], started),
                    )
                    self.assertTrue(started.wait(BARRIER_TIMEOUT))
                    # ponytail: a 1s bound proves the child is held at the lock;
                    # a lock-free consume returns in milliseconds.
                    with self.assertRaises(FutureTimeout):
                        future.result(timeout=1)
                    append_jsonl(confirmations_path(root), spent)
                result = future.result(timeout=BARRIER_TIMEOUT)
            self.assertEqual(result, (None, "confirmation_used"))
            self.assertEqual(read_confirmations(root), [issued, spent])

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

    def test_bound_without_require_confirmation_fails_closed(self) -> None:
        # The validator rejects bound with any other risk_policy; run refuses it
        # too, so warn (or no policy) cannot run high-risk commands unconfirmed.
        for risk_policy in ("block", "warn", None):
            with self.subTest(risk_policy=risk_policy), tempfile.TemporaryDirectory() as tmp:
                # confirm-command refuses these contracts too, so issue first.
                root = _bound_run_root(tmp)
                self._issue(root)
                contract = load_execution_contract(root)
                contract["command_policy"].pop("risk_policy")
                if risk_policy is not None:
                    contract["command_policy"]["risk_policy"] = risk_policy
                write_json(root / CONTRACT, contract)
                ledger = confirmations_path(root).read_bytes()
                for flags in ([], ["--confirm-risk"]):
                    code, err = self._cli_run(root, *flags)
                    self.assertEqual(code, 2, err)
                    self.assertIn(
                        "confirmation bound requires risk_policy require-confirmation", err
                    )
                self.assertEqual(confirmations_path(root).read_bytes(), ledger)
                self.assertEqual(command_receipts(root), [])
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

    def test_malformed_ledger_row_exits_2(self) -> None:
        for row, text in (
            ("[]", ".agent/command-confirmations.jsonl:1: ledger entry must be a JSON object"),
            ('{"schema_version": "1.0.0", "id": "CC1", "event": []}',
             "row 1 must be an object with a string id and event"),
        ):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                root = _bound_run_root(tmp)
                confirmations_path(root).write_text(row + "\n", encoding="utf-8")
                code, err = self._cli_run(root)
                self.assertEqual(code, 2, err)
                self.assertIn(text, err)
                self.assertEqual(command_receipts(root), [])
                self.assertEqual(_runs(root), 0)

    def test_missing_contract_keeps_advisory_degrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_run_root(tmp)
            (root / CONTRACT).unlink()
            receipt = run_command(root, _run_plan(), "P1", None, RUN)
            self.assertEqual(receipt["decision"], "allowed")
            self.assertNotIn("confirmation_id", receipt)
            self.assertEqual(_runs(root), 1)


class ReceiptSchemaTests(unittest.TestCase):
    def test_confirmation_schema_and_reader_accept_the_same_versions(self) -> None:
        schema = json.loads((REPO / "schemas/command-confirmations.schema.json").read_text())
        pattern = schema["properties"]["schema_version"]["pattern"]
        for version, accepted in (
            ("1.0.0", True), ("1.0.1", True), ("1.1.0", False),
            ("1.9.0", False), ("2.0.0", False), ("1.01.0", False),
        ):
            with self.subTest(version=version):
                self.assertEqual(bool(re.fullmatch(pattern, version)), accepted)
                self.assertEqual(
                    not validate_schema_version(version, "1.0.0", "command-confirmations"), accepted
                )

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

    def test_ledger_and_receipt_agree_on_confirmation_ids(self) -> None:
        schemas = REPO / "schemas"
        ledger = json.loads((schemas / "command-confirmations.schema.json").read_text(encoding="utf-8"))
        receipt = json.loads((schemas / "command-receipts.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ledger["properties"]["id"]["pattern"], receipt["properties"]["confirmation_id"]["pattern"]
        )

    def test_proof_schema_declares_the_confirmations_block(self) -> None:
        schema = json.loads(
            (REPO / "schemas/proof-pack.schema.json").read_text(encoding="utf-8")
        )
        block = schema["properties"]["confirmations"]
        self.assertEqual(
            sorted(block["required"]), ["bound", "high_risk_executed", "mode", "self", "unbound"]
        )
        self.assertEqual(block["properties"]["mode"], {"enum": list(CONFIRMATION_MODES)})
        self.assertNotIn("confirmations", schema["required"])


REPO = Path(__file__).resolve().parents[1]
GATE = [sys.executable, "-c", "print('gate')"]
RISKY = ["rm", "-rf", "nothing-here"]
CONFIRMATIONS = ".agent/command-confirmations.jsonl"
RECEIPTS = ".agent/command-receipts.jsonl"
SUMMARY_LINE = (
    "command confirmations: bound, 1 high-risk run(s), 1 bound, 0 self, 0 unbound; "
    "self-confirmation proves binding, not who confirmed"
)
# canonical_core before #112; a run that never used confirmations keeps it.
CORE_KEYS = {"generated_from", "files", "checks", "coverage", "review", "capabilities", "stuck"}


def _agentflow(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AGENTFLOW_CONFIRM_RISK"}
    env["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=str(root), env=env, text=True, capture_output=True, check=False,
    )


def _ok(root: Path, *args: str) -> None:
    result = _agentflow(root, *args)
    if result.returncode != 0:
        raise AssertionError(f"{args}: {result.stdout}{result.stderr}")


def _proof_plan() -> dict:
    plan = _run_plan()
    step = plan["steps"][0]
    step.update(validation=["gate"], gates=[{"kind": "command", "run": GATE}])
    plan.update(steps=[step], validation_gates=["gate"], locked=False, locked_at=None)
    return plan


def _build_proof_root(root: Path, mode: str) -> None:
    """One finished step with one high-risk run, sealed by build-proof."""
    _ok(root, "init")
    write_json(root / ".agent/plan.lock.json", _proof_plan())
    _ok(root, "lock-plan", ".agent/plan.lock.json")
    _ok(root, "init-execution")
    confirm = ["--confirm-risk"]
    if mode == "bound":
        _set_version(root, "1.1.0")
        _set_policy(root, confirmation="bound")
        confirm = []
    _ok(root, "claim-step", "P1", "--agent", "agent-a")
    if mode == "bound":
        _ok(root, "confirm-command", "--step", "P1", "--", *RISKY)
    _ok(root, "run", "--step", "P1", *confirm, "--", *RISKY)
    _ok(root, "run", "--step", "P1", "--", *GATE)
    _ok(root, "verify-step", "P1")
    _ok(root, "complete-step", "P1")
    _ok(root, "audit-drift")
    _ok(root, "build-proof")


def _edit_rows(root: Path, ledger: str, edit) -> None:
    path = root / ledger
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    path.write_text("".join(json.dumps(row) + "\n" for row in edit(rows)), encoding="utf-8")


def _errors(result: subprocess.CompletedProcess) -> list:
    return [line for line in result.stdout.splitlines() if line.startswith("error: ")]


class ProofConfirmationTests(unittest.TestCase):
    """#112: the proof records how each high-risk run was confirmed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.roots = {}
        for mode in ("bound", "self"):
            root = Path(cls._tmp.name) / mode
            root.mkdir()
            _build_proof_root(root, mode)
            cls.roots[mode] = root

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _proof(self, root: Path) -> dict:
        return json.loads((root / ".agent/proof-pack.json").read_text(encoding="utf-8"))

    def _tampered(self, edit) -> list:
        """Edit the bound run, reseal it with build-proof, return verify-proof's errors."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = Path(shutil.copytree(self.roots["bound"], Path(tmp) / "bound"))
        edit(root)
        _ok(root, "build-proof")
        result = _agentflow(root, "verify-proof")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        return _errors(result)

    def test_clean_bound_run_verifies(self) -> None:
        root = self.roots["bound"]
        proof = self._proof(root)
        self.assertEqual(proof["schema_version"], "1.2.0")
        self.assertEqual(
            proof["confirmations"],
            {"mode": "bound", "high_risk_executed": 1, "bound": 1, "self": 0, "unbound": []},
        )
        self.assertIn(CONFIRMATIONS, proof["generated_from"])
        result = _agentflow(root, "verify-proof", "--require", "confirmation=bound")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-2:], [SUMMARY_LINE, "proof verified"])
        self.assertTrue(lines[-3].startswith("validation gates: "), lines)
        markdown = (root / ".agent/proof-pack.md").read_text(encoding="utf-8")
        self.assertIn("## Command Confirmations", markdown)
        for text in ("mode: bound", "who confirmed", "timestamps", "left out of the bundle"):
            self.assertIn(text, markdown)

    def test_edited_or_dropped_block_is_stale(self) -> None:
        edits = {
            "edited": lambda proof: proof["confirmations"].update(bound=0, self=1),
            "dropped": lambda proof: proof.pop("confirmations"),
        }
        for name, edit in edits.items():
            with self.subTest(edit=name):
                tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, tmp)
                root = Path(shutil.copytree(self.roots["bound"], Path(tmp) / "bound"))
                path = root / ".agent/proof-pack.json"
                proof = json.loads(path.read_text(encoding="utf-8"))
                edit(proof)
                proof["core_sha256"] = proof_module.core_sha256(proof)
                path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
                result = _agentflow(root, "verify-proof")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(
                    _errors(result), ["error: proof confirmations block is stale or tampered"]
                )

    def test_hand_edited_confirmation_id_fails(self) -> None:
        def unissued(rows):
            return [dict(r, confirmation_id="CC2") if "confirmation_id" in r else r for r in rows]

        def mismatched(rows):
            # A self-consistent row for another command, so only the receipt mismatch fires.
            other = ["rm", "-rf", "elsewhere"]
            return [
                dict(r, argv=other, argv_sha256=argv_sha256(other)) if r["event"] == "issued" else r
                for r in rows
            ]

        def listed_id(rows):
            return [dict(r, confirmation_id=["CC1"]) if "confirmation_id" in r else r for r in rows]

        cases = {
            "unissued id": (RECEIPTS, unissued, "CR1 names confirmation CC2, which was never issued"),
            # A non-string id is an error finding, not a traceback. One on a
            # ledger row: test_malformed_ledger_row_is_a_finding.
            "list id on the receipt": (
                RECEIPTS, listed_id, "CR1 names confirmation ['CC1'], which was never issued"
            ),
            "argv differs": (
                CONFIRMATIONS, mismatched, "CR1 does not match confirmation CC1 (step, argv or contract)"
            ),
        }
        for name, (ledger, edit, message) in cases.items():
            with self.subTest(case=name):
                errors = self._tampered(lambda root: _edit_rows(root, ledger, edit))
                self.assertEqual(errors, [f"error: {message}"])

    def test_doubled_consumed_row_fails(self) -> None:
        def double(rows):
            return rows + [r for r in rows if r["event"] == "consumed"]

        errors = self._tampered(lambda root: _edit_rows(root, CONFIRMATIONS, double))
        self.assertEqual(errors, ["error: confirmation CC1 was consumed 2 times"])

    def test_confirmation_plan_binding_must_match_consumption(self) -> None:
        for event in ("issued", "consumed"):
            with self.subTest(event=event):
                errors = self._tampered(lambda root: _edit_rows(
                    root, CONFIRMATIONS,
                    lambda rows: [dict(r, plan_sha256="0" * 64) if r["event"] == event else r for r in rows],
                ))
                self.assertEqual(errors, ["error: confirmation CC1 does not match its consumed plan binding"])

    def test_missing_or_invalid_plan_bindings_fail(self) -> None:
        for digest in (None, "", "bad", 7, ["0" * 64]):
            with self.subTest(digest=digest):
                errors = self._tampered(lambda root: _edit_rows(
                    root, CONFIRMATIONS,
                    lambda rows: [dict(r, plan_sha256=digest) for r in rows],
                ))
                self.assertEqual(errors, ["error: confirmation CC1 does not match its consumed plan binding"])

    def test_plan_amendment_after_consumption_keeps_the_original_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(shutil.copytree(self.roots["bound"], Path(tmp) / "bound"))
            path = root / ".agent/plan.lock.json"
            plan = json.loads(path.read_text())
            plan["objective"] = "Amended after the confirmed command ran."
            write_json(path, plan)
            _ok(root, "lock-plan", ".agent/plan.lock.json")
            _ok(root, "audit-drift")
            _ok(root, "build-proof")
            result = _agentflow(root, "verify-proof", "--require", "confirmation=bound")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(SUMMARY_LINE, result.stdout)

    def test_orphan_consumption_fails(self) -> None:
        errors = self._tampered(lambda root: _edit_rows(
            root, CONFIRMATIONS,
            lambda rows: rows + [{"schema_version": "1.0.0", "id": "CC999", "event": "consumed",
                                  "attempt_id": "A1", "consumed_at": "not-a-timestamp"}],
        ))
        self.assertEqual(errors, ["error: confirmation CC999 was consumed but never issued"])

    def test_one_confirmation_named_by_two_receipts_fails(self) -> None:
        # R9: one consumed row cannot vouch for two runs.
        def replayed(rows):
            confirmed = next(r for r in rows if "confirmation_id" in r)
            return rows + [dict(confirmed, id=f"CR{len(rows) + 1}")]

        errors = self._tampered(lambda root: _edit_rows(root, RECEIPTS, replayed))
        self.assertEqual(errors, ["error: confirmation CC1 is named by 2 receipts"])

    def test_consumed_by_another_attempt_fails(self) -> None:
        receipt = next(
            r for r in (
                json.loads(line) for line in
                (self.roots["bound"] / RECEIPTS).read_text(encoding="utf-8").splitlines()
            )
            if "confirmation_id" in r
        )

        def elsewhere(rows):
            return [dict(r, attempt_id="A99") if r["event"] == "consumed" else r for r in rows]

        errors = self._tampered(lambda root: _edit_rows(root, CONFIRMATIONS, elsewhere))
        self.assertEqual(
            errors,
            [f"error: confirmation CC1 was consumed by attempt A99 but {receipt['id']} "
             f"ran in attempt {receipt['attempt_id']}"],
        )

    def test_consumed_after_expiry_fails(self) -> None:
        def late(rows):
            expires = next(r["expires_at"] for r in rows if r["event"] == "issued")
            after = (datetime.fromisoformat(expires) + timedelta(seconds=1)).isoformat()
            return [dict(r, consumed_at=after) if r["event"] == "consumed" else r for r in rows]

        errors = self._tampered(lambda root: _edit_rows(root, CONFIRMATIONS, late))
        self.assertEqual(errors, ["error: confirmation CC1 was consumed after it expired"])

    def test_cli_confirmation_under_bound_fails(self) -> None:
        def self_confirmed(rows):
            for row in rows:
                if row.pop("confirmation_id", None):
                    row["confirmation_source"] = "cli"
            return rows

        errors = self._tampered(lambda root: _edit_rows(root, RECEIPTS, self_confirmed))
        self.assertEqual(errors, ["error: CR1 was confirmed by cli under confirmation bound"])

    def test_unconfirmed_run_under_bound_fails(self) -> None:
        # What Agentflow 1.5's run writes when it degrades a bound contract to
        # advisory: a high-risk receipt pinned to the contract, with no confirmation.
        def unconfirmed(rows):
            for row in rows:
                if row.pop("confirmation_id", None):
                    row.pop("confirmed", None)
            return rows

        errors = self._tampered(lambda root: _edit_rows(root, RECEIPTS, unconfirmed))
        self.assertEqual(
            errors, ["error: CR1 ran with no bound confirmation under confirmation bound"]
        )

    def test_tightening_self_to_bound_mid_run_still_verifies(self) -> None:
        # A receipt written under the earlier self contract stays a self count;
        # only a confirmation=bound floor rejects it, through the #117 pins.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = Path(tmp)
        _ok(root, "init")
        write_json(root / ".agent/plan.lock.json", _proof_plan())
        _ok(root, "lock-plan", ".agent/plan.lock.json")
        _ok(root, "init-execution")
        _ok(root, "claim-step", "P1", "--agent", "agent-a")
        _ok(root, "run", "--step", "P1", "--confirm-risk", "--", *RISKY)
        _set_version(root, "1.1.0")
        _set_policy(root, confirmation="bound")
        _ok(root, "confirm-command", "--step", "P1", "--", *RISKY)
        _ok(root, "run", "--step", "P1", "--", *RISKY)
        _ok(root, "run", "--step", "P1", "--", *GATE)
        for args in (("verify-step", "P1"), ("complete-step", "P1"), ("audit-drift",), ("build-proof",)):
            _ok(root, *args)
        result = _agentflow(root, "verify-proof")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "command confirmations: bound, 2 high-risk run(s), 1 bound, 1 self, 0 unbound; "
            "self-confirmation proves binding, not who confirmed",
            result.stdout.splitlines(),
        )
        floor = _agentflow(root, "verify-proof", "--require", "confirmation=bound")
        self.assertEqual(floor.returncode, 1, floor.stdout + floor.stderr)
        self.assertIn(
            "error: policy_below_floor: .agent/command-receipts.jsonl has 1 record(s) not "
            "pinned to the verified execution contract (1 under another contract, 0 unpinned; "
            "first at line 1)",
            _errors(floor),
        )

    def test_issued_argv_must_match_its_hash(self) -> None:
        # The readable argv is what a reviewer reads; it must be the hashed one.
        for name, argv in {"argv swapped": ["echo", "harmless"], "argv not a list": 7}.items():
            with self.subTest(case=name):
                errors = self._tampered(lambda root: _edit_rows(
                    root, CONFIRMATIONS,
                    lambda rows: [dict(r, argv=argv) if r["event"] == "issued" else r for r in rows],
                ))
                self.assertEqual(
                    errors, ["error: confirmation CC1 argv does not match its argv_sha256"]
                )

    def test_malformed_ledger_row_is_a_finding(self) -> None:
        # build-proof refuses these rows, so seal the bundle by hand, as a forger would.
        cases = {
            "list event": lambda rows: [dict(r, event=[]) if r["event"] == "issued" else r for r in rows],
            "list id on the issued row": lambda rows: [
                dict(r, id=["CC1"]) if r["event"] == "issued" else r for r in rows
            ],
            "non-object row": lambda rows: [[]] + rows,
        }
        for name, edit in cases.items():
            with self.subTest(case=name):
                tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, tmp)
                root = Path(shutil.copytree(self.roots["bound"], Path(tmp) / "bound"))
                _edit_rows(root, CONFIRMATIONS, edit)
                path = root / ".agent/proof-pack.json"
                proof = json.loads(path.read_text(encoding="utf-8"))
                for item in proof["files"]:
                    if item["path"] == CONFIRMATIONS:
                        item["sha256"] = hashlib.sha256((root / CONFIRMATIONS).read_bytes()).hexdigest()
                proof["core_sha256"] = proof_module.core_sha256(proof)
                path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
                result = _agentflow(root, "verify-proof")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                detail = (
                    f"{root.resolve() / CONFIRMATIONS}:1: ledger entry must be a JSON object"
                    if name == "non-object row"
                    else f"{CONFIRMATIONS} row 1 must be an object with a string id and event"
                )
                self.assertEqual(
                    _errors(result), [f"error: command confirmations could not be checked: {detail}"]
                )

    def test_build_proof_refuses_a_malformed_ledger_row(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = Path(shutil.copytree(self.roots["bound"], Path(tmp) / "bound"))
        _edit_rows(root, CONFIRMATIONS, lambda rows: [[]] + rows)
        result = _agentflow(root, "build-proof")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            result.stderr.strip(),
            f"invalid ledger: {root.resolve() / CONFIRMATIONS}:1: ledger entry must be a JSON object",
        )

    def test_expiry_after_the_run_never_fails(self) -> None:
        def long_ago(rows):
            return [
                dict(r, issued_at="2020-01-01T00:00:00+00:00", expires_at="2020-01-01T00:15:00+00:00")
                if r["event"] == "issued"
                else dict(r, consumed_at="2020-01-01T00:05:00+00:00")
                for r in rows
            ]

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = Path(shutil.copytree(self.roots["bound"], Path(tmp) / "bound"))
        _edit_rows(root, CONFIRMATIONS, long_ago)
        _ok(root, "build-proof")
        result = _agentflow(root, "verify-proof")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_floor_bound_fails_on_self_contract(self) -> None:
        result = _agentflow(self.roots["self"], "verify-proof", "--require", "confirmation=bound")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            _errors(result),
            ["error: policy_below_floor: confirmation is self; "
             "the floor requires confirmation=bound"],
        )

    def test_no_ledger_self_mode_keeps_core_hash(self) -> None:
        root = self.roots["self"]
        proof = self._proof(root)
        self.assertFalse((root / CONFIRMATIONS).exists())
        self.assertNotIn("confirmations", proof)
        self.assertIsNone(proof_module.confirmation_summary(root))
        core = proof_module.canonical_core(proof)
        self.assertEqual(set(core), CORE_KEYS)
        payload = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(proof["core_sha256"], hashlib.sha256(payload).hexdigest())
        result = _agentflow(root, "verify-proof")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("command confirmations", result.stdout)

    def test_summary_partitions_executed_high_risk_receipts(self) -> None:
        high = {"level": "high", "findings": []}
        receipts = [
            {"id": "CR1", "provenance": "observed", "decision": "allowed", "risk": high,
             "confirmation_id": "CC1"},
            {"id": "CR2", "provenance": "reconstructed", "decision": "timeout", "risk": high,
             "confirmation_source": "env"},
            {"id": "CR3", "provenance": "observed", "decision": "allowed", "risk": high},
            {"id": "CR4", "provenance": "observed", "decision": "blocked", "risk": high},
            {"id": "CR5", "provenance": "attested", "decision": "allowed", "risk": high},
            {"id": "CR6", "provenance": "managed", "decision": "allowed", "risk": high},
            {"id": "CR7", "provenance": "observed", "decision": "allowed", "risk": {"level": "low"}},
            {"id": "CR8", "provenance": "observed", "decision": "timeout", "risk": high},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_root(tmp)
            for receipt in receipts:
                append_jsonl(root / RECEIPTS, receipt)
            self.assertEqual(
                proof_module.confirmation_summary(root),
                {"mode": "bound", "high_risk_executed": 4, "bound": 1, "self": 1,
                 "unbound": ["CR3", "CR8"]},
            )

    def test_summary_is_absent_only_under_self_without_a_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _bound_root(tmp, "self")
            self.assertIsNone(proof_module.confirmation_summary(root))
            confirmations_path(root).write_text("", encoding="utf-8")
            self.assertEqual(
                proof_module.confirmation_summary(root),
                {"mode": "self", "high_risk_executed": 0, "bound": 0, "self": 0, "unbound": []},
            )
            _set_policy(root, confirmation="bound", risk_policy="warn")
            with self.assertRaisesRegex(ValueError, "requires risk_policy require-confirmation"):
                proof_module.confirmation_summary(root)


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
