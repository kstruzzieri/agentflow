from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_release.py"


@unittest.skipIf(
    sys.version_info < (3, 11), "release guard requires Python 3.11+"
)
class ReleaseGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src" / "agentflow").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_fixture(
        self,
        *,
        project_version: str = "0.4.0",
        package_version: str = "0.4.0",
        project_name: str = "agentflow",
        changelog: str | None = None,
    ) -> None:
        (self.root / "pyproject.toml").write_text(
            f'[project]\nname = "{project_name}"\nversion = "{project_version}"\n',
            encoding="utf-8",
        )
        (self.root / "src" / "agentflow" / "__init__.py").write_text(
            f'__version__ = "{package_version}"\n', encoding="utf-8"
        )
        (self.root / "CHANGELOG.md").write_text(
            changelog
            or (
                "# Changelog\n\n"
                "## [Unreleased]\n\n"
                "## [0.4.0] - 2026-07-10\n\n"
                "### Added\n\n"
                "- Initial public release.\n\n"
                "## [0.3.0] - 2026-07-03\n\n"
                "### Added\n\n"
                "- First tagged release.\n"
            ),
            encoding="utf-8",
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
        )

    def test_version_check_accepts_matching_versions_with_fallback_name(self) -> None:
        self._write_fixture(project_name="agentflow-proof")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "version declarations agree: 0.4.0")

    def test_version_check_rejects_pyproject_mismatch(self) -> None:
        self._write_fixture(project_version="0.4.1")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("pyproject.toml has 0.4.1", result.stderr)

    def test_version_check_rejects_package_mismatch(self) -> None:
        self._write_fixture(package_version="0.4.1")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("agentflow.__version__ has 0.4.1", result.stderr)

    def test_release_check_rejects_malformed_tag(self) -> None:
        self._write_fixture()

        result = self._run("--tag", "release-0.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("expected vMAJOR.MINOR.PATCH", result.stderr)

    def test_release_check_rejects_leading_zero_tag(self) -> None:
        self._write_fixture()

        result = self._run("--tag", "v00.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("expected vMAJOR.MINOR.PATCH", result.stderr)

    def test_release_check_rejects_tag_version_mismatch(self) -> None:
        self._write_fixture()

        result = self._run("--tag", "v0.4.1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("tag has 0.4.1", result.stderr)

    def test_release_check_rejects_missing_changelog_heading(self) -> None:
        self._write_fixture(
            changelog="# Changelog\n\n## [Unreleased]\n\n- Pending.\n"
        )

        result = self._run("--tag", "v0.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("found 0 release headings for 0.4.0", result.stderr)

    def test_release_check_rejects_duplicate_changelog_heading(self) -> None:
        section = "## [0.4.0] - 2026-07-10\n\n- Released.\n\n"
        self._write_fixture(changelog="# Changelog\n\n" + section + section)

        result = self._run("--tag", "v0.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("found 2 release headings for 0.4.0", result.stderr)

    def test_release_check_rejects_empty_changelog_section(self) -> None:
        self._write_fixture(
            changelog=(
                "# Changelog\n\n"
                "## [0.4.0] - 2026-07-10\n\n"
                "## [0.3.0] - 2026-07-03\n\n- Older.\n"
            )
        )

        result = self._run("--tag", "v0.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("CHANGELOG release 0.4.0 has no notes", result.stderr)

    def test_release_check_writes_exact_changelog_notes(self) -> None:
        self._write_fixture()
        notes = self.root / "notes.md"

        result = self._run(
            "--tag", "v0.4.0", "--notes-file", str(notes)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            notes.read_text(encoding="utf-8"),
            "### Added\n\n- Initial public release.\n",
        )

    def test_notes_file_requires_tag(self) -> None:
        self._write_fixture()

        result = self._run("--notes-file", str(self.root / "notes.md"))

        self.assertEqual(result.returncode, 2)
        self.assertIn("--notes-file requires --tag", result.stderr)


if __name__ == "__main__":
    unittest.main()
