# Release Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver PR1 for Issue #3: a CHANGELOG-backed pre-build release guard, PR-time version consistency check, and exact release procedure.

**Architecture:** A standard-library script reads the two literal version declarations without importing Agentflow, validates exact release tags, parses dated CHANGELOG sections, and optionally writes the selected section as GitHub release notes. Unit tests first exercise temporary repositories; repository-level tests then lock the real CHANGELOG, workflow dependency order, permissions, and documentation into the contract.

**Tech Stack:** Python 3.11+ standard library (`argparse`, `pathlib`, `re`, `tomllib`), `unittest`, GitHub Actions YAML, Markdown, Agentflow task/proof artifacts.

## Global Constraints

- This plan implements PR1 / Issue #3 only. Do not add wheel, sdist, PyPI, OIDC, distribution-name, or README/CONTRIBUTING changes.
- Preserve literal `project.version` and `agentflow.__version__` declarations and require them to agree.
- The no-tag PR check compares version declarations only and never validates `project.name`.
- The tag check accepts only exact `vMAJOR.MINOR.PATCH` tags with no leading zeroes except the number zero.
- The release guard must run before tests or artifact builds in the tag workflow.
- The release workflow uses CHANGELOG notes, not generated GitHub notes.
- Runtime code and tests add no dependency; tests use `unittest`.
- GitHub Actions remain pinned to immutable SHAs.
- The guard and its documented local invocation require Python 3.11 or newer.
- Root `.agent/` remains task-local and is not committed.

---

## File structure

- Create `scripts/check_release.py`: pure-stdlib version, tag, CHANGELOG, and notes validation.
- Create `tests/test_release.py`: temporary-repository unit tests and real-repository workflow/documentation contract tests.
- Create `CHANGELOG.md`: Keep a Changelog history for Unreleased, 0.4.0, and 0.3.0.
- Modify `.github/workflows/ci.yml`: run the version-only check before unit tests.
- Modify `.github/workflows/release.yml`: add the early guard job, least-privilege permissions, and CHANGELOG notes.
- Modify `docs/packaging.md`: document every version-bearing file and the ordered release process.
- Create task-local `.agent/`: locked execution contract, receipts, drift report, and proof.

## Execution setup: isolate PR1 and lock the Agentflow plan

- [ ] **Step 1: Rename the current branch for PR1 and update it from main**

Run:

~~~bash
git branch -m codex/v1-release-discipline
git fetch origin
git rebase origin/main
~~~

Expected: the worktree remains isolated on `codex/v1-release-discipline`; the two approved design commits and this plan commit are based on current `origin/main`.

- [ ] **Step 2: Initialize fresh Agentflow state**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow init
~~~

Replace `.agent/plan.lock.json` with this exact unlocked contract:

~~~json
{
  "schema_version": "0.3.0",
  "objective": "Enforce Agentflow release discipline before tests or artifact builds.",
  "scope": [
    "Add and test a reusable version, tag, and CHANGELOG release guard.",
    "Backfill the 0.3.0 and 0.4.0 CHANGELOG history.",
    "Wire PR-time version checks and tag-time pre-build enforcement.",
    "Document the exact release procedure."
  ],
  "non_goals": [
    "Do not build wheel or sdist artifacts.",
    "Do not change the Python distribution name.",
    "Do not add PyPI trusted publishing or OIDC permissions.",
    "Do not edit README.md or CONTRIBUTING.md.",
    "Do not publish a release or contact any external owner."
  ],
  "invariants": [
    "The release guard uses only the Python 3.11 standard library.",
    "The two literal version declarations remain present and equal.",
    "The PR-time check ignores the distribution name.",
    "Release tags are exact vMAJOR.MINOR.PATCH values.",
    "Tag validation completes before tests or artifact builds.",
    "GitHub release notes come from the matching CHANGELOG section."
  ],
  "allowed_files": [
    ".agent/",
    "scripts/check_release.py",
    "tests/test_release.py",
    "CHANGELOG.md",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "docs/packaging.md"
  ],
  "blocked_files": [
    "pyproject.toml",
    "src/agentflow/__init__.py",
    "README.md",
    "CONTRIBUTING.md"
  ],
  "validation_gates": [
    "python3.11 -m unittest tests.test_release.ReleaseGuardTests -v",
    "python3.11 -m unittest tests.test_release -v",
    "PYTHONPATH=src python3.11 -m unittest discover -s tests -v"
  ],
  "rollback_plan": "Revert the two PR1 implementation commits to restore generated release notes and the prior unguarded zipapp release workflow.",
  "risk_level": "high",
  "drift_budget": {
    "unrelated_edits": 0,
    "new_dependencies": 0,
    "formatting_drift": "minimal",
    "architecture_drift": "requires_approval",
    "test_weakening": 0
  },
  "steps": [
    {
      "id": "P1",
      "action": "Implement and test the reusable release guard.",
      "files": [
        "scripts/check_release.py",
        "tests/test_release.py",
        ".agent/"
      ],
      "preconditions": [
        "The approved design is committed.",
        "The branch is based on current origin/main."
      ],
      "expected_diff": [
        "A standard-library release guard validates literal versions, exact tags, and CHANGELOG sections.",
        "Focused tests prove success and every required failure mode."
      ],
      "validation": [
        "python3.11 -m unittest tests.test_release.ReleaseGuardTests -v"
      ],
      "evidence_ids": [],
      "execution_mode": "same_session",
      "authority": "edit"
    },
    {
      "id": "P2",
      "action": "Add release history, workflow enforcement, and release procedure documentation.",
      "files": [
        "tests/test_release.py",
        "CHANGELOG.md",
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "docs/packaging.md",
        ".agent/"
      ],
      "preconditions": [
        "P1 is completed and committed."
      ],
      "expected_diff": [
        "CHANGELOG.md records Unreleased, 0.4.0, and 0.3.0.",
        "CI checks version declarations before tests.",
        "The release workflow guards before tests/builds and uses CHANGELOG notes.",
        "Packaging documentation states the exact release order and Python floor."
      ],
      "validation": [
        "python3.11 -m unittest tests.test_release -v",
        "PYTHONPATH=src python3.11 -m unittest discover -s tests -v"
      ],
      "depends_on": [
        "P1"
      ],
      "evidence_ids": [],
      "execution_mode": "same_session",
      "authority": "edit"
    }
  ],
  "evidence_ids": []
}
~~~

- [ ] **Step 3: Lock and initialize execution**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow lock-plan .agent/plan.lock.json
PYTHONPATH=src python3.11 -m agentflow init-execution
PYTHONPATH=src python3.11 -m agentflow validate-plan .agent/plan.lock.json
~~~

Expected: all commands exit 0 and `next-step` reports `P1`.

---

### Task 1: Standard-library release guard

**Files:**
- Create: `tests/test_release.py`
- Create: `scripts/check_release.py`

**Interfaces:**
- Consumes: literal `[project].version` from `pyproject.toml`, literal `__version__` from `src/agentflow/__init__.py`, and dated `## [X.Y.Z] - YYYY-MM-DD` CHANGELOG headings.
- Produces: `check_versions(root: Path) -> str`, `check_release(root: Path, tag: str) -> tuple[str, str]`, and CLI flags `--root`, `--tag`, and `--notes-file`.

- [ ] **Step 1: Claim P1**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow claim-step P1 --agent "$USER"
~~~

- [ ] **Step 2: Write the failing guard tests**

Create `tests/test_release.py` with:

~~~python
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
~~~

- [ ] **Step 3: Run the focused test to verify RED**

Run:

~~~bash
python3.11 -m unittest tests.test_release.ReleaseGuardTests -v
~~~

Expected: FAIL because `scripts/check_release.py` does not exist.

- [ ] **Step 4: Implement the minimum release guard**

Create `scripts/check_release.py` with:

~~~python
#!/usr/bin/env python3
"""Validate Agentflow version declarations, release tags, and CHANGELOG notes."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_PATTERN = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
VERSION_RE = re.compile(rf"^{VERSION_PATTERN}$")
TAG_RE = re.compile(rf"^v(?P<version>{VERSION_PATTERN})$")
PACKAGE_VERSION_RE = re.compile(
    r"^__version__\s*=\s*(?P<quote>['\"])(?P<version>[^'\"]+)(?P=quote)\s*$",
    re.MULTILINE,
)
RELEASE_HEADING_RE = re.compile(
    rf"^## \[(?P<version>{VERSION_PATTERN})\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$",
    re.MULTILINE,
)
SECTION_HEADING_RE = re.compile(r"^## ", re.MULTILINE)


class ReleaseCheckError(ValueError):
    """A release input is missing, malformed, or inconsistent."""


def _project_version(root: Path) -> str:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseCheckError(
            "pyproject.toml project.version must be a literal MAJOR.MINOR.PATCH"
        )
    return version


def _package_version(root: Path) -> str:
    path = root / "src" / "agentflow" / "__init__.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    matches = list(PACKAGE_VERSION_RE.finditer(text))
    if len(matches) != 1:
        raise ReleaseCheckError(
            "src/agentflow/__init__.py must contain exactly one literal "
            "__version__ assignment"
        )
    version = matches[0].group("version")
    if not VERSION_RE.fullmatch(version):
        raise ReleaseCheckError(
            "agentflow.__version__ must be a literal MAJOR.MINOR.PATCH"
        )
    return version


def check_versions(root: Path) -> str:
    project = _project_version(root)
    package = _package_version(root)
    if project != package:
        raise ReleaseCheckError(
            f"version mismatch: pyproject.toml has {project}; "
            f"agentflow.__version__ has {package}"
        )
    return project


def _release_notes(root: Path, version: str) -> str:
    path = root / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    matches = [
        match
        for match in RELEASE_HEADING_RE.finditer(text)
        if match.group("version") == version
    ]
    if len(matches) != 1:
        raise ReleaseCheckError(
            f"found {len(matches)} release headings for {version}; expected 1"
        )
    start = matches[0].end()
    next_heading = SECTION_HEADING_RE.search(text, start)
    end = next_heading.start() if next_heading else len(text)
    notes = text[start:end].strip()
    if not notes:
        raise ReleaseCheckError(f"CHANGELOG release {version} has no notes")
    return notes + "\n"


def check_release(root: Path, tag: str) -> tuple[str, str]:
    tag_match = TAG_RE.fullmatch(tag)
    if not tag_match:
        raise ReleaseCheckError(
            f"invalid release tag {tag!r}; expected vMAJOR.MINOR.PATCH"
        )
    declared = check_versions(root)
    tagged = tag_match.group("version")
    if tagged != declared:
        raise ReleaseCheckError(
            f"version mismatch: tag has {tagged}; declarations have {declared}"
        )
    return declared, _release_notes(root, declared)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--tag")
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args(argv)
    if args.notes_file is not None and args.tag is None:
        parser.error("--notes-file requires --tag")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.tag is None:
            version = check_versions(args.root)
            message = f"version declarations agree: {version}"
        else:
            version, notes = check_release(args.root, args.tag)
            if args.notes_file is not None:
                args.notes_file.write_text(notes, encoding="utf-8")
            message = f"release guard passed: v{version}"
    except ReleaseCheckError as exc:
        sys.stderr.write(f"release check failed: {exc}\n")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 5: Run the focused test to verify GREEN**

Run:

~~~bash
python3.11 -m unittest tests.test_release.ReleaseGuardTests -v
~~~

Expected: 11 tests pass.

- [ ] **Step 6: Record, verify, and complete P1**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P1 --path tests/test_release.py --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P1 --path scripts/check_release.py --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow run --step P1 --gate "python3.11 -m unittest tests.test_release.ReleaseGuardTests -v" --agent "$USER" -- python3.11 -m unittest tests.test_release.ReleaseGuardTests -v
PYTHONPATH=src python3.11 -m agentflow verify-step P1 --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow complete-step P1 --agent "$USER"
~~~

Expected: P1 verifies and completes.

- [ ] **Step 7: Commit the guard**

Run:

~~~bash
git add scripts/check_release.py tests/test_release.py
git commit -m "feat: add release version guard"
~~~

---

### Task 2: CHANGELOG, workflow enforcement, and release procedure

**Files:**
- Modify: `tests/test_release.py`
- Create: `CHANGELOG.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `docs/packaging.md`

**Interfaces:**
- Consumes: `scripts/check_release.py` from Task 1 and existing zipapp build/smoke commands.
- Produces: dated 0.3.0/0.4.0 notes, a PR check that ignores distribution name, an early tag guard, least-privilege jobs, and `--notes-file` GitHub releases.

- [ ] **Step 1: Claim P2**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow claim-step P2 --agent "$USER"
~~~

- [ ] **Step 2: Add failing repository-contract tests**

Insert before the `if __name__ == "__main__"` block in `tests/test_release.py`:

~~~python
@unittest.skipIf(
    sys.version_info < (3, 11), "release guard requires Python 3.11+"
)
class RepositoryReleaseDisciplineTests(unittest.TestCase):
    def test_repository_release_guard_accepts_v040_and_extracts_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(REPO_ROOT),
                    "--tag",
                    "v0.4.0",
                    "--notes-file",
                    str(notes),
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            text = notes.read_text(encoding="utf-8")
            self.assertIn("runtime and MCP status", text)
            self.assertNotIn("## [0.3.0]", text)

    def test_changelog_contains_unreleased_and_backfilled_releases(self) -> None:
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("## [Unreleased]", text)
        self.assertIn("## [0.4.0] - 2026-07-10", text)
        self.assertIn("## [0.3.0] - 2026-07-03", text)

    def test_ci_checks_versions_before_unit_tests(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        check = text.index("python3 scripts/check_release.py")
        tests = text.index("PYTHONPATH=src python3 -m unittest discover")
        self.assertLess(check, tests)

    def test_release_workflow_guards_before_release_and_uses_changelog(self) -> None:
        text = (
            REPO_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        guard = text.index("\n  guard:")
        release = text.index("\n  release:")
        self.assertLess(guard, release)
        self.assertIn("permissions:\n  contents: read", text[:guard])
        release_job = text[release:]
        self.assertIn("needs: guard", release_job)
        self.assertIn("contents: write", release_job)
        self.assertIn("--notes-file", release_job)
        self.assertNotIn("--generate-notes", text)

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
~~~

- [ ] **Step 3: Run repository tests to verify RED**

Run:

~~~bash
python3.11 -m unittest tests.test_release.RepositoryReleaseDisciplineTests -v
~~~

Expected: failures for missing `CHANGELOG.md`, missing CI guard, generated release notes, and incomplete documentation.

- [ ] **Step 4: Create the backfilled CHANGELOG**

Create `CHANGELOG.md` with:

~~~markdown
# Changelog

All notable changes to Agentflow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-10

### Added

- Read-only runtime and MCP status evidence in proof packs.
- Single-writer step leases with renewal and stale-owner recovery.
- Cross-worktree ledger aggregation with collision detection and provenance.
- A public gate/ledger brand kit for the project and release artifacts.

## [0.3.0] - 2026-07-03

### Added

- Portable execution contracts, step claims, command and file receipts,
  resumable verification, and provider-neutral handoffs.
- Deterministic command-risk screening, the dependency-free MCP server, and
  the POSIX Stop-hook enforcement gate.
- CI proof verification, review manifests, capability receipts, workflow
  packs, workflow recommendation, and draft-plan generation.
- Hunk-level drift attribution, the static HTML proof viewer, and the Golem
  integration guide.
- Single-file CLI and MCP zipapps with checksums and a tag-triggered GitHub
  release workflow.

### Changed

- Existing v0.2 proof artifacts remain valid when no execution contract exists.

[Unreleased]: https://github.com/kstruzzieri/agentflow/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kstruzzieri/agentflow/releases/tag/v0.4.0
~~~

- [ ] **Step 5: Wire the CI version check**

In `.github/workflows/ci.yml`, insert immediately after the setup-python step and before `Run unit tests`:

~~~yaml
      - name: Check version declarations
        run: python3 scripts/check_release.py
~~~

- [ ] **Step 6: Replace the release workflow with the guarded PR1 workflow**

Replace `.github/workflows/release.yml` with:

~~~yaml
name: Release

on:
  push:
    tags: ["v*"]

permissions:
  contents: read

jobs:
  guard:
    name: Validate release metadata
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0

      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6
        with:
          python-version: "3.11"

      - name: Check tag, versions, and CHANGELOG
        run: python3 scripts/check_release.py --tag "$GITHUB_REF_NAME"

  release:
    name: Build and publish release artifacts
    needs: guard
    permissions:
      contents: write
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0

      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6
        with:
          python-version: "3.11"

      - name: Validate release and extract notes
        run: >-
          python3 scripts/check_release.py
          --tag "$GITHUB_REF_NAME"
          --notes-file "$RUNNER_TEMP/release-notes.md"

      - name: Run unit tests
        run: PYTHONPATH=src python3 -m unittest discover -s tests

      - name: Build zipapp artifacts
        run: python3 scripts/build_zipapp.py --output-dir dist

      - name: Smoke test artifacts
        run: |
          python3 dist/agentflow.pyz --version
          printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
            | python3 dist/agentflow-mcp.pyz | grep -q '"serverInfo"'

      - name: Generate checksums
        working-directory: dist
        run: sha256sum agentflow.pyz agentflow-mcp.pyz > SHA256SUMS

      - name: Create GitHub release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release create "$GITHUB_REF_NAME" \
            dist/agentflow.pyz dist/agentflow-mcp.pyz dist/SHA256SUMS \
            --title "$GITHUB_REF_NAME" \
            --notes-file "$RUNNER_TEMP/release-notes.md"
~~~

- [ ] **Step 7: Replace the release checklist in packaging documentation**

Replace the existing `## Release checklist` section in `docs/packaging.md` with:

~~~markdown
## Release checklist

The release guard requires Python 3.11 or newer because it reads
`pyproject.toml` with the standard-library `tomllib` module.

1. On `main`, update the version in `pyproject.toml`.
2. Update the same version in `src/agentflow/__init__.py`.
3. Move the relevant notes under `CHANGELOG.md`'s `Unreleased` heading into
   a dated `## [X.Y.Z] - YYYY-MM-DD` release heading, leaving an empty
   `Unreleased` heading for future changes.
4. Run the version check and full suite:

   ```bash
   python3 scripts/check_release.py
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   ```

5. Commit the version and CHANGELOG changes through a normal pull request.
6. After that commit reaches `main`, validate the intended tag before creating
   it:

   ```bash
   python3 scripts/check_release.py --tag vX.Y.Z
   ```

7. Create and push the exact validated tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

8. The `Release` workflow validates the tag, both version declarations, and
   CHANGELOG before it runs tests or builds artifacts. It builds and smokes the
   zipapps, generates `SHA256SUMS`, and creates the GitHub release using the
   matching CHANGELOG section as its notes.
9. Download `agentflow.pyz`, `agentflow-mcp.pyz`, and `SHA256SUMS` from the
   release; run `sha256sum -c SHA256SUMS`, then
   `python3 agentflow.pyz --version`.
~~~

- [ ] **Step 8: Run focused tests to verify GREEN**

Run:

~~~bash
python3.11 -m unittest tests.test_release -v
~~~

Expected: 16 tests pass.

- [ ] **Step 9: Run the full suite through Agentflow**

Run the focused and full gates as durable receipts:

~~~bash
PYTHONPATH=src python3.11 -m agentflow run --step P2 --gate "python3.11 -m unittest tests.test_release -v" --agent "$USER" -- python3.11 -m unittest tests.test_release -v
PYTHONPATH=src python3.11 -m agentflow run --step P2 --gate "PYTHONPATH=src python3.11 -m unittest discover -s tests -v" --agent "$USER" -- /bin/zsh -lc "PYTHONPATH=src python3.11 -m unittest discover -s tests -v"
~~~

Expected: focused release tests pass; the full suite passes with only the existing intentional skips.

- [ ] **Step 10: Record all P2 files, verify, and complete**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P2 --path tests/test_release.py --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P2 --path CHANGELOG.md --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P2 --path .github/workflows/ci.yml --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P2 --path .github/workflows/release.yml --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow record-file-change --step P2 --path docs/packaging.md --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow verify-step P2 --agent "$USER"
PYTHONPATH=src python3.11 -m agentflow complete-step P2 --agent "$USER"
~~~

- [ ] **Step 11: Commit PR1 release discipline**

Run:

~~~bash
git add tests/test_release.py CHANGELOG.md .github/workflows/ci.yml .github/workflows/release.yml docs/packaging.md
git commit -m "ci: enforce release discipline"
~~~

---

### Task 3: Fresh final verification and PR1 handoff

**Files:**
- Verify only: all PR1 files and task-local `.agent/`

**Interfaces:**
- Consumes: completed P1/P2 ledgers and both implementation commits.
- Produces: a verified proof chain, drift audit, final commit SHA, and a PR-ready evidence summary.

- [ ] **Step 1: Confirm current main has not moved**

Run:

~~~bash
git fetch origin
git merge-base --is-ancestor origin/main HEAD
git status --short
~~~

Expected: `origin/main` is an ancestor and the tracked worktree is clean. If it is not an ancestor, rebase onto `origin/main`, open an Agentflow P2 amendment, rerun both P2 gates, re-record any conflict resolutions, and complete the amendment before continuing.

- [ ] **Step 2: Run the Agentflow completion chain**

Run:

~~~bash
PYTHONPATH=src python3.11 -m agentflow verify-run
PYTHONPATH=src python3.11 -m agentflow audit-drift
PYTHONPATH=src python3.11 -m agentflow build-proof
PYTHONPATH=src python3.11 -m agentflow verify-proof
~~~

Expected: all four commands exit 0. No completion claim is made before reading their complete output.

- [ ] **Step 3: Run fresh direct verification**

Run:

~~~bash
python3.11 -m unittest tests.test_release -v
PYTHONPATH=src python3.11 -m unittest discover -s tests -v
python3.11 scripts/check_release.py --tag v0.4.0
git diff --check
git status --short
git rev-parse HEAD
~~~

Expected: focused and full tests pass, the repository's v0.4.0 metadata validates, no whitespace errors exist, only ignored task-local `.agent/` state remains, and the final SHA is captured.

- [ ] **Step 4: Prepare the PR1 evidence summary**

The summary must state:

- Issue #3 scope only; Issue #6 is deliberately deferred until PR1 merges.
- Exact focused/full test counts and skips from the fresh runs.
- Release guard behavior and workflow ordering.
- Agentflow `verify-run`, `audit-drift`, `build-proof`, and `verify-proof` results.
- Final commit SHA.
- No release, tag, PyPI action, owner contact, or support message occurred.

Do not start PR2 or reuse this `.agent/` state. PR2 begins from merged `main` in a new isolated worktree with a new locked plan.
