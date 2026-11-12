from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
