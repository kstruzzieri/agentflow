from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_title.py"


class PullRequestTitleTests(unittest.TestCase):
    def _run(self, title: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={"PR_TITLE": title} if title is not None else {},
            capture_output=True,
            text=True,
        )

    def test_accepts_documented_types_scopes_and_breaking_markers(self) -> None:
        titles = (
            "feat: add confirmations",
            "fix(proof): reject replaced files (#126)",
            "docs: update the roadmap",
            "refactor(cli): simplify parsing",
            "test: cover missing inputs",
            "chore(release): prepare v1.5.0",
            "ci: validate PR titles",
            "build: update the backend",
            "perf: reduce repeated reads",
            "revert: restore the old behavior",
            "feat!: remove a deprecated option",
            "feat(cli)!: remove a deprecated command",
            "docs: clarify café examples",
        )
        for title in titles:
            with self.subTest(title=title):
                result = self._run(title)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_missing_or_malformed_titles(self) -> None:
        titles = (
            None, "", "update the roadmap", "unknown: update the roadmap",
            "Docs: update the roadmap", " docs: update the roadmap",
            "docs:update the roadmap", "docs:  update the roadmap",
            "docs:", "docs: ", "docs: \t", "docs(): update the roadmap",
            "docs(cli: update the roadmap", "docs(cli)): update the roadmap",
            "docs(cli parser): update the roadmap", "docs!!: update the roadmap",
            "docs: update\nthe roadmap", "docs: update\rthe roadmap",
            "Merge pull request #1 from example/branch",
        )
        for title in titles:
            with self.subTest(title=title):
                result = self._run(title)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("type(scope): description", result.stderr)

    def test_shell_metacharacters_are_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "unexpected"
            result = self._run(f"docs: explain $(touch {marker}) and `touch {marker}`")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

    def test_invalid_title_is_not_echoed_as_a_workflow_command(self) -> None:
        result = self._run("\n::error::untrusted text")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("::error::untrusted text", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
