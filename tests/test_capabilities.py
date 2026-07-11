from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentflow.capabilities import (
    append_capability_receipt,
    build_capability_receipt,
    capability_checks,
    capability_summary,
    read_capability_receipts,
    validate_capability_receipt,
)
from agentflow.contracts import CAPABILITY_RECEIPTS_SCHEMA_VERSION


def used_row(**over):
    row = {
        "schema_version": CAPABILITY_RECEIPTS_SCHEMA_VERSION,
        "id": "CAP1",
        "capability": "tdd",
        "status": "used",
        "provider": "manual",
        "reason": "red-green-refactor",
        "evidence": ["E1"],
        "recorded_at": "2026-06-27T12:00:00+00:00",
    }
    row.update(over)
    return row


class ValidateTests(unittest.TestCase):
    def test_valid_used_row(self) -> None:
        self.assertEqual(validate_capability_receipt(used_row()), [])

    def test_valid_waived_row_without_provider(self) -> None:
        row = used_row(id="CAP2", status="waived", capability="frontend-qa", evidence=[])
        del row["provider"]
        self.assertEqual(validate_capability_receipt(row), [])

    def test_used_requires_provider(self) -> None:
        row = used_row()
        del row["provider"]
        errors = validate_capability_receipt(row)
        self.assertTrue(any("provider" in e for e in errors))

    def test_waived_provider_if_present_must_be_non_empty(self) -> None:
        row = used_row(status="waived", provider="")
        errors = validate_capability_receipt(row)
        self.assertTrue(any("provider" in e for e in errors))

    def test_bad_status(self) -> None:
        errors = validate_capability_receipt(used_row(status="done"))
        self.assertTrue(any("status" in e for e in errors))

    def test_unknown_field(self) -> None:
        errors = validate_capability_receipt(used_row(extra="x"))
        self.assertTrue(any("unknown" in e for e in errors))

    def test_missing_reason(self) -> None:
        row = used_row()
        row["reason"] = ""
        errors = validate_capability_receipt(row)
        self.assertTrue(any("reason" in e for e in errors))

    def test_evidence_must_be_string_list(self) -> None:
        errors = validate_capability_receipt(used_row(evidence=[1]))
        self.assertTrue(any("evidence" in e for e in errors))

    def test_bad_schema_version(self) -> None:
        errors = validate_capability_receipt(used_row(schema_version="9.9.9"))
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_non_object(self) -> None:
        self.assertEqual(
            validate_capability_receipt("nope"),
            ["capability receipt must be a JSON object"],
        )


class BuildTests(unittest.TestCase):
    def test_build_used_receipt(self) -> None:
        row = build_capability_receipt(
            "CAP1", "tdd", "used", "why", provider="manual", evidence=["E1"]
        )
        self.assertEqual(row["schema_version"], CAPABILITY_RECEIPTS_SCHEMA_VERSION)
        self.assertEqual(row["provider"], "manual")
        self.assertEqual(row["status"], "used")
        self.assertEqual(row["evidence"], ["E1"])
        self.assertIn("recorded_at", row)

    def test_build_waived_receipt_omits_provider(self) -> None:
        row = build_capability_receipt("CAP2", "frontend-qa", "waived", "no frontend files")
        self.assertNotIn("provider", row)
        self.assertEqual(row["evidence"], [])

    def test_build_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_capability_receipt("CAP1", "", "used", "why", provider="manual")


class LedgerTests(unittest.TestCase):
    def test_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_capability_receipt(
                root, build_capability_receipt("CAP1", "tdd", "used", "r", provider="manual")
            )
            append_capability_receipt(
                root, build_capability_receipt("CAP2", "frontend-qa", "waived", "r")
            )
            rows = read_capability_receipts(root)
            self.assertEqual([r["id"] for r in rows], ["CAP1", "CAP2"])

    def test_read_missing_ledger_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_capability_receipts(Path(tmp)), [])

    def test_read_rejects_malformed_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".agent/capability-receipts.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"id": "CAP1"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                read_capability_receipts(root)


class SummaryTests(unittest.TestCase):
    def _root(self, tmp):
        root = Path(tmp)
        append_capability_receipt(
            root, build_capability_receipt("CAP1", "tdd", "used", "r", provider="manual")
        )
        append_capability_receipt(
            root, build_capability_receipt("CAP2", "frontend-qa", "waived", "r")
        )
        return root

    def test_required_recorded_waived_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            summary = capability_summary(root, ["tdd", "review-spec"])
            self.assertEqual(summary["required"], ["tdd", "review-spec"])
            self.assertEqual(summary["recorded"], ["tdd"])
            self.assertEqual(summary["waived"], ["frontend-qa"])
            self.assertEqual(summary["missing"], ["review-spec"])

    def test_used_and_waived_both_satisfy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            summary = capability_summary(root, ["tdd", "frontend-qa"])
            self.assertEqual(summary["missing"], [])

    def test_used_wins_over_waived_for_same_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_capability_receipt(
                root, build_capability_receipt("CAP1", "tdd", "used", "r", provider="manual")
            )
            append_capability_receipt(
                root, build_capability_receipt("CAP2", "tdd", "waived", "r")
            )
            summary = capability_summary(root, ["tdd"])
            self.assertEqual(summary["recorded"], ["tdd"])
            self.assertEqual(summary["waived"], ["tdd"])
            self.assertEqual(summary["missing"], [])

    def test_capability_ids_compared_exactly_without_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_capability_receipt(
                root, build_capability_receipt("CAP1", "tdd", "used", "r", provider="manual")
            )
            summary = capability_summary(root, ["TDD"])
            self.assertEqual(summary["missing"], ["TDD"])
            self.assertEqual(summary["recorded"], ["tdd"])

    def test_duplicate_required_id_is_not_duplicated_in_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = capability_summary(root, ["tdd", "tdd"])
            self.assertEqual(summary["missing"], ["tdd"])

    def test_empty_ledger_missing_equals_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = capability_summary(root, ["tdd"])
            self.assertEqual(summary["missing"], ["tdd"])
            self.assertEqual(summary["recorded"], [])


class CheckTests(unittest.TestCase):
    def test_missing_warns(self) -> None:
        checks = capability_checks(
            {"required": ["tdd"], "recorded": [], "waived": [], "missing": ["tdd"]}
        )
        self.assertEqual(checks[0]["id"], "required_capabilities_satisfied")
        self.assertEqual(checks[0]["status"], "warning")
        self.assertIn("tdd", checks[0]["message"])

    def test_satisfied_passes(self) -> None:
        checks = capability_checks(
            {"required": ["tdd"], "recorded": ["tdd"], "waived": [], "missing": []}
        )
        self.assertEqual(checks[0]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
