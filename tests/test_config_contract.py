from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_JSON = ROOT / "docs/ai/config.json"

SEVERITIES = {"critical", "high", "medium", "low"}


class ConfigJsonContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))

    def test_top_level_keys(self) -> None:
        self.assertIn("branch_modifiers", self.config)
        self.assertIn("gate_policy", self.config)

    def test_default_branch_modifier_present(self) -> None:
        self.assertIn("*", self.config["branch_modifiers"])

    def test_every_modifier_gate_resolves_to_a_policy(self) -> None:
        policies = self.config["gate_policy"]
        for pattern, modifier in self.config["branch_modifiers"].items():
            self.assertIn("gate", modifier, f"{pattern} missing 'gate'")
            self.assertIn(
                modifier["gate"], policies,
                f"{pattern} gate {modifier['gate']!r} has no gate_policy",
            )

    def test_gate_policies_use_valid_severities(self) -> None:
        for name, policy in self.config["gate_policy"].items():
            for field in ("blocks_on", "warns_on"):
                self.assertIsInstance(policy.get(field), list, f"{name}.{field}")
                for sev in policy[field]:
                    self.assertIn(sev, SEVERITIES, f"{name}.{field}: {sev!r}")


if __name__ == "__main__":
    unittest.main()
