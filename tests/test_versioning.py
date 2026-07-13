from __future__ import annotations

import unittest

from agentflow.versioning import (
    SchemaVersion,
    is_schema_version_compatible,
    parse_schema_version,
    validate_historical_proof_schema_version,
    validate_schema_version,
)


class SchemaVersionTests(unittest.TestCase):
    def test_parse_schema_version(self) -> None:
        self.assertEqual(parse_schema_version("0.2.0"), SchemaVersion(0, 2, 0))

    def test_accepts_same_major_and_lower_or_equal_minor(self) -> None:
        self.assertTrue(is_schema_version_compatible("0.1.0", "0.2.0"))
        self.assertTrue(is_schema_version_compatible("0.2.0", "0.2.0"))

    def test_rejects_higher_minor_or_major_mismatch(self) -> None:
        self.assertFalse(is_schema_version_compatible("0.4.0", "0.3.0"))
        self.assertFalse(is_schema_version_compatible("1.0.0", "0.3.0"))

    def test_accepts_v02_and_v03_under_v03_toolchain(self) -> None:
        self.assertTrue(is_schema_version_compatible("0.2.0", "0.3.0"))
        self.assertTrue(is_schema_version_compatible("0.3.0", "0.3.0"))

    def test_validate_schema_version_reports_clear_artifact_name(self) -> None:
        errors = validate_schema_version("1.0.0", "0.2.0", "plan-lock")
        self.assertEqual(
            errors,
            ["plan-lock schema_version 1.0.0 is incompatible with supported 0.2.0"],
        )

    def test_exact_policy_rejects_an_older_minor(self) -> None:
        self.assertEqual(
            validate_schema_version(
                "0.2.0", "0.3.0", "execution-contract", policy="exact"
            ),
            [
                "execution-contract schema_version 0.2.0 is incompatible with "
                "supported 0.3.0"
            ],
        )

    def test_none_policy_deliberately_skips_version_validation(self) -> None:
        self.assertEqual(
            validate_schema_version("not-a-version", "0.9.0", "proof-pack", policy="none"),
            [],
        )

    def test_agentflow_1_accepts_historical_proofs_from_0_4_onward(self) -> None:
        self.assertEqual(validate_historical_proof_schema_version("0.4.0", "1.2.0"), [])
        self.assertEqual(validate_historical_proof_schema_version("1.1.0", "1.2.0"), [])
        self.assertNotEqual(
            validate_historical_proof_schema_version("0.3.9", "1.2.0"), []
        )

    def test_parse_rejects_whitespace_leading_zeros_and_suffixes(self) -> None:
        for value in ("01.2.3", "1. 2.3", " 1.2.3", "1.2.3 ", "1.2.3-rc1", "1.2", "1.2.3.4"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "MAJOR.MINOR.PATCH"):
                    parse_schema_version(value)

    def test_invalid_supported_constant_is_a_loud_developer_error(self) -> None:
        # A malformed *supported* version is a broken constant, not bad user
        # data; it must raise instead of blaming the artifact's schema_version.
        with self.assertRaisesRegex(ValueError, "invalid supported schema_version"):
            validate_schema_version("0.2.0", "not-a-version", "plan-lock")

    def test_invalid_schema_version_is_error(self) -> None:
        errors = validate_schema_version("bad", "0.2.0", "runtime-config")
        self.assertEqual(
            errors,
            ["runtime-config schema_version must be MAJOR.MINOR.PATCH"],
        )


if __name__ == "__main__":
    unittest.main()
