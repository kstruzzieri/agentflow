from __future__ import annotations

import json
import unittest

from agentflow.handoff import export_handoff, lint_handoff_text


def plan() -> dict:
    return {
        "schema_version": "0.3.0",
        "objective": "Portable work packet.",
        "scope": ["Exercise handoff."],
        "non_goals": ["No provider-specific wording."],
        "invariants": ["Use local files and shell commands only."],
        "allowed_files": ["fixture.txt"],
        "blocked_files": ["secrets/"],
        "validation_gates": ["python3 -m unittest discover -s tests"],
        "rollback_plan": "Revert fixture.txt.",
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
                "validation": ["python3 -m unittest discover -s tests"],
                "evidence_ids": [],
            }
        ],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


class HandoffTests(unittest.TestCase):
    def test_export_json_handoff_is_provider_neutral(self) -> None:
        payload = export_handoff(plan(), "P1", "json")

        text = json.dumps(payload, sort_keys=True)
        self.assertEqual(lint_handoff_text(text), [])
        self.assertEqual(payload["step"]["id"], "P1")
        self.assertIn("agentflow claim-step P1 --agent", payload["commands"]["claim"])

    def test_export_markdown_handoff_is_provider_neutral(self) -> None:
        markdown = export_handoff(plan(), "P1", "markdown")

        self.assertEqual(lint_handoff_text(markdown), [])
        self.assertIn("# Agentflow Work Packet", markdown)
        self.assertIn("agentflow verify-step P1", markdown)

    def test_lint_handoff_flags_provider_token(self) -> None:
        findings = lint_handoff_text("Use Codex skills and MCP tools.")

        self.assertEqual(findings[0]["severity"], "error")
        self.assertIn("provider-specific token", findings[0]["message"])


if __name__ == "__main__":
    unittest.main()
