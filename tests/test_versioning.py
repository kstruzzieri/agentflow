from __future__ import annotations

import unittest

from agentflow.versioning import (
    SchemaVersion,
    is_schema_version_compatible,
    parse_schema_version,
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

    def test_invalid_schema_version_is_error(self) -> None:
        errors = validate_schema_version("bad", "0.2.0", "runtime-config")
        self.assertEqual(
            errors,
            ["runtime-config schema_version must be MAJOR.MINOR.PATCH"],
        )


if __name__ == "__main__":
    unittest.main()
