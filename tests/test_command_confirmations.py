from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentflow import cli
from agentflow.artifacts import plan_binding_sha256, write_json
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
    confirmation_mode,
    contract_schema_version_for,
    default_execution_contract,
    init_execution_artifacts,
    load_execution_contract,
    validate_execution_contract,
)


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


if __name__ == "__main__":
    unittest.main()
