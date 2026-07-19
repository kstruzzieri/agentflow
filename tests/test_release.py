from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_release.py"


def _workflow_job(text: str, name: str) -> str:
    header = f"\n  {name}:\n"
    start = text.index(header)
    body_start = start + len(header)
    next_job = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[body_start:])
    end = body_start + next_job.start() if next_job else len(text)
    return text[start:end]


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

    def test_version_check_allows_pending_unreleased_notes(self) -> None:
        self._write_fixture(
            changelog="# Changelog\n\n## [Unreleased]\n\n- Pending.\n"
        )

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_release_check_rejects_non_ascii_digit_tag(self) -> None:
        self._write_fixture()

        result = self._run("--tag", "v1.2\u0662.3")

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

    def test_release_check_stops_notes_before_footer_links(self) -> None:
        self._write_fixture(
            changelog=(
                "# Changelog\n\n"
                "## [0.4.0] - 2026-07-10\n\n"
                "- Released.\n\n"
                "[Unreleased]: https://example.test/compare\n"
                "[0.4.0]: https://example.test/release\n"
            )
        )
        notes = self.root / "notes.md"

        result = self._run(
            "--tag", "v0.4.0", "--notes-file", str(notes)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(notes.read_text(encoding="utf-8"), "- Released.\n")

    def test_release_check_keeps_notes_after_fenced_hash_heading(self) -> None:
        # A ``## `` line inside a fenced code block must not be mistaken for a
        # section boundary and silently truncate the extracted notes.
        self._write_fixture(
            changelog=(
                "# Changelog\n\n"
                "## [Unreleased]\n\n"
                "## [0.4.0] - 2026-07-10\n\n"
                "### Added\n\n"
                "- Config example:\n\n"
                "```ini\n"
                "## legacy header\n"
                "key = value\n"
                "```\n\n"
                "- Second real note.\n\n"
                "## [0.3.0] - 2026-07-03\n\n- Older.\n"
            )
        )
        notes = self.root / "notes.md"

        result = self._run("--tag", "v0.4.0", "--notes-file", str(notes))

        self.assertEqual(result.returncode, 0, result.stderr)
        body = notes.read_text(encoding="utf-8")
        self.assertIn("## legacy header", body)
        self.assertIn("- Second real note.", body)
        self.assertNotIn("Older.", body)

    def test_release_check_rejects_notes_left_in_unreleased(self) -> None:
        self._write_fixture(
            changelog=(
                "# Changelog\n\n"
                "## [Unreleased]\n\n"
                "### Added\n\n- Still pending here.\n\n"
                "## [0.4.0] - 2026-07-10\n\n"
                "### Added\n\n- Released note.\n"
            )
        )

        result = self._run("--tag", "v0.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Unreleased", result.stderr)
        self.assertIn("must be empty", result.stderr)

    def test_version_check_rejects_missing_package_version(self) -> None:
        self._write_fixture()
        (self.root / "src" / "agentflow" / "__init__.py").write_text(
            "__all__ = ['__version__']\n", encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("exactly one literal", result.stderr)

    def test_version_check_rejects_multiple_package_versions(self) -> None:
        self._write_fixture()
        (self.root / "src" / "agentflow" / "__init__.py").write_text(
            '__version__ = "0.4.0"\n__version__ = "0.4.0"\n', encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("exactly one literal", result.stderr)

    def test_version_check_rejects_missing_project_table(self) -> None:
        self._write_fixture()
        (self.root / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n', encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("no [project] version declaration", result.stderr)

    def test_release_check_reports_missing_changelog_file(self) -> None:
        self._write_fixture()
        (self.root / "CHANGELOG.md").unlink()

        result = self._run("--tag", "v0.4.0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read", result.stderr)
        self.assertIn("CHANGELOG.md", result.stderr)

    def test_notes_file_write_failure_is_concise(self) -> None:
        self._write_fixture()

        result = self._run(
            "--tag", "v0.4.0", "--notes-file", str(self.root)
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("release check failed: cannot write", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_notes_file_requires_tag(self) -> None:
        self._write_fixture()

        result = self._run("--notes-file", str(self.root / "notes.md"))

        self.assertEqual(result.returncode, 2)
        self.assertIn("--notes-file requires --tag", result.stderr)


@unittest.skipIf(
    sys.version_info < (3, 11), "release guard requires Python 3.11+"
)
class RepositoryReleaseDisciplineTests(unittest.TestCase):
    def test_repository_version_guard_accepts_declared_version(self) -> None:
        import tomllib

        declared = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), f"version declarations agree: {declared}"
        )

    def test_changelog_contains_unreleased_and_backfilled_releases(self) -> None:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("## [Unreleased]", text)
        self.assertIn("## [0.4.0] - 2026-07-10", text)
        self.assertIn("## [0.3.0] - 2026-07-03", text)

    def test_ci_checks_versions_before_unit_tests(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        check = text.index("run: python3 scripts/check_release.py\n")
        tests = text.index("PYTHONPATH=src python3 -m unittest discover")
        self.assertLess(check, tests)

    def test_release_workflow_guards_before_build(self) -> None:
        text = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        guard = text.index("\n  guard:")
        build = text.index("\n  build:")
        self.assertLess(guard, build)
        self.assertIn("permissions:\n  contents: read", text[:guard])
        guard_job = _workflow_job(text, "guard")
        self.assertIn(
            'run: python3 scripts/check_release.py --tag "$GITHUB_REF_NAME"\n',
            guard_job,
        )
        self.assertIn("needs: guard", _workflow_job(text, "build"))
        self.assertIn(
            '--notes-file "$RUNNER_TEMP/release-notes.md"',
            _workflow_job(text, "github-release"),
        )
        self.assertNotIn("|| true", text)
        self.assertNotIn("--generate-notes", text)

    def test_release_workflow_builds_and_uploads_one_artifact_bundle(self) -> None:
        text = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        build = _workflow_job(text, "build")

        self.assertEqual(text.count("scripts/build_zipapp.py"), 1)
        self.assertEqual(
            text.count("python -m build --sdist --wheel --outdir dist"), 1
        )
        self.assertIn("build==1.5.0", build)
        self.assertIn("twine==6.2.0", build)
        self.assertIn(
            'SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)', build
        )
        self.assertIn("PYTHONPATH=src python -m unittest discover -s tests", build)
        self.assertIn("python scripts/check_distribution.py --dist-dir dist", build)
        self.assertIn("python -m twine check dist/*.whl dist/*.tar.gz", build)
        self.assertIn("python dist/agentflow.pyz --version", build)
        self.assertIn("python dist/agentflow-mcp.pyz", build)
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            build,
        )
        self.assertIn("name: release-distributions", build)
        for path in (
            "dist/agentflow.pyz",
            "dist/agentflow-mcp.pyz",
            "dist/*.whl",
            "dist/*.tar.gz",
        ):
            self.assertIn(path, build)
        self.assertIn("if-no-files-found: error", build)

    def test_release_workflow_clean_installs_exact_artifacts(self) -> None:
        text = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        wheel = _workflow_job(text, "clean-install-wheel")
        sdist = _workflow_job(text, "clean-install-sdist")
        download = (
            "actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        )

        self.assertIn('python-version: ["3.11", "3.12", "3.13"]', wheel)
        self.assertIn("needs: build", wheel)
        self.assertIn(download, wheel)
        self.assertIn("name: release-distributions", wheel)
        self.assertIn("python -m venv", wheel)
        self.assertIn("wheels=(dist/*.whl)", wheel)
        self.assertIn('test "${#wheels[@]}" -eq 1', wheel)
        self.assertIn('pip install --no-index "${wheels[0]}"', wheel)
        self.assertIn("agentflow --version", wheel)
        self.assertIn("agentflow --help", wheel)
        self.assertIn("agentflow-mcp", wheel)
        self.assertIn('grep -q \'"serverInfo"\'', wheel)
        self.assertIn("agentflow verify-proof --root", wheel)
        self.assertIn(
            "tests/fixtures/compatibility/released-v0.4.0", wheel
        )

        self.assertIn("needs: build", sdist)
        self.assertIn(download, sdist)
        self.assertIn("python -m venv", sdist)
        self.assertIn("setuptools==83.0.0", sdist)
        self.assertIn("sdists=(dist/*.tar.gz)", sdist)
        self.assertIn('test "${#sdists[@]}" -eq 1', sdist)
        self.assertIn(
            'pip install --no-index --no-build-isolation "${sdists[0]}"',
            sdist,
        )
        self.assertIn("agentflow --version", sdist)

    def test_release_workflow_limits_publication_permissions_and_payload(self) -> None:
        text = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        release = _workflow_job(text, "github-release")
        publish = _workflow_job(text, "publish-pypi")
        download = (
            "actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        )

        self.assertEqual(text.count("contents: write"), 1)
        self.assertIn("contents: write", release)
        for dependency in ("guard", "clean-install-wheel", "clean-install-sdist"):
            self.assertIn(f"- {dependency}", release)
        self.assertIn(download, release)
        self.assertIn('--notes-file "$RUNNER_TEMP/release-notes.md"', release)
        for artifact in (
            "agentflow.pyz",
            "agentflow-mcp.pyz",
            "agentflow_proof-*.whl",
            "agentflow_proof-*.tar.gz",
            "SHA256SUMS",
        ):
            self.assertIn(artifact, release)

        self.assertEqual(text.count("id-token: write"), 1)
        self.assertIn("if: false # Issue #5 compatibility freeze", publish)
        self.assertIn("environment: pypi", publish)
        self.assertIn("id-token: write", publish)
        for dependency in (
            "guard",
            "clean-install-wheel",
            "clean-install-sdist",
            "github-release",
        ):
            self.assertIn(f"- {dependency}", publish)
        self.assertIn(download, publish)
        stage_start = publish.index("- name: Stage PyPI distributions")
        action_start = publish.index("- uses: pypa/gh-action-pypi-publish@")
        stage = publish[stage_start:action_start]
        self.assertIn("dist/*.whl", stage)
        self.assertIn("dist/*.tar.gz", stage)
        self.assertNotIn(".pyz", stage)
        self.assertIn(
            "pypa/gh-action-pypi-publish@"
            "2834a314042ef964da07689278dd1e9d773e8afd",
            publish,
        )
        self.assertIn("packages-dir: pypi-dist/", publish)
        self.assertNotRegex(publish, r"(?m)^\s+(?:password|token):")

    def test_packaging_docs_name_release_order_and_python_floor(self) -> None:
        text = (REPO_ROOT / "docs" / "packaging.md").read_text(
            encoding="utf-8"
        )

        ordered = [
            "pyproject.toml",
            "src/agentflow/__init__.py",
            "Unreleased",
            "python3 scripts/check_release.py --tag vX.Y.Z",
            "git tag vX.Y.Z",
            "git push origin vX.Y.Z",
        ]
        positions = [text.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Python 3.11 or newer", text)
        compact = text.replace("\n> ", " ")
        self.assertIn(
            "Ordinary CI checks only that the version declarations agree",
            compact,
        )
        self.assertIn("tag-triggered `Release` workflow", compact)


if __name__ == "__main__":
    unittest.main()
