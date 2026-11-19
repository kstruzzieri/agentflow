"""Tests for scripts/check_pypi_releases.py and the workflow that schedules it."""

from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import check_pypi_releases as watch


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pypi-watch.yml"
WHEEL = "agentflow_proof-1.0.0-py3-none-any.whl"
SDIST = "agentflow_proof-1.0.0.tar.gz"
GOOD = "a" * 64
OTHER = "b" * 64
PYPI = watch.PYPI_URL.format(project=watch.PROJECT)


def _sums_url(version: str) -> str:
    return watch.SUMS_URL.format(repository=watch.REPOSITORY, version=version)


def _pypi(releases: dict[str, dict[str, str]]) -> bytes:
    return json.dumps(
        {
            "releases": {
                version: [
                    {"filename": name, "digests": {"sha256": digest}}
                    for name, digest in files.items()
                ]
                for version, files in releases.items()
            }
        }
    ).encode()


def _sums(files: dict[str, str]) -> bytes:
    return "".join(f"{digest}  {name}\n" for name, digest in files.items()).encode()


class PyPIReleaseWatchTests(unittest.TestCase):
    def problems(self, pages: dict[str, bytes]) -> list[str]:
        return watch.find_problems(pages.get)[0]

    def test_matching_files_report_no_problem(self) -> None:
        files = {WHEEL: GOOD, SDIST: OTHER}
        pages = {
            PYPI: _pypi({"1.0.0": files}),
            # The release also lists the zipapps; PyPI does not carry them.
            _sums_url("1.0.0"): _sums({"agentflow.pyz": GOOD, **files}),
        }

        problems, versions, checked = watch.find_problems(pages.get)

        self.assertEqual(problems, [])
        self.assertEqual((versions, checked), (1, 2))

    def test_reports_a_file_whose_hash_differs_from_the_release(self) -> None:
        pages = {
            PYPI: _pypi({"1.0.0": {WHEEL: OTHER}}),
            _sums_url("1.0.0"): _sums({WHEEL: GOOD}),
        }

        (problem,) = self.problems(pages)

        self.assertIn("1.0.0", problem)
        self.assertIn(WHEEL, problem)
        self.assertIn(OTHER, problem)
        self.assertIn(GOOD, problem)

    def test_reports_a_version_with_no_matching_release(self) -> None:
        pages = {PYPI: _pypi({"9.9.9": {WHEEL: GOOD}})}

        (problem,) = self.problems(pages)

        self.assertIn("9.9.9", problem)
        self.assertIn("v9.9.9", problem)

    def test_reports_a_file_the_release_did_not_record(self) -> None:
        extra = "agentflow_proof-1.0.0-cp311-cp311-manylinux_2_28_x86_64.whl"
        pages = {
            PYPI: _pypi({"1.0.0": {WHEEL: GOOD, extra: OTHER}}),
            _sums_url("1.0.0"): _sums({WHEEL: GOOD}),
        }

        (problem,) = self.problems(pages)

        self.assertIn(extra, problem)

    def test_a_version_with_no_files_is_not_a_problem(self) -> None:
        pages = {PYPI: _pypi({"0.9.0": {}})}

        self.assertEqual(watch.find_problems(pages.get), ([], 0, 0))

    def test_an_unreadable_checksum_file_or_missing_project_is_an_error(self) -> None:
        with self.assertRaises(watch.ReleaseWatchError):
            watch.parse_sums("not a checksum line\n")
        with self.assertRaises(watch.ReleaseWatchError):
            watch.find_problems({}.get)

    def test_main_exits_1_on_a_problem_and_0_when_everything_matches(self) -> None:
        good = {
            PYPI: _pypi({"1.0.0": {WHEEL: GOOD}}),
            _sums_url("1.0.0"): _sums({WHEEL: GOOD}),
        }
        bad = {**good, PYPI: _pypi({"1.0.0": {WHEEL: OTHER}})}
        for pages, code, stream, expected in (
            (good, 0, "stdout", "1 version"),
            (bad, 1, "stderr", WHEEL),
        ):
            with self.subTest(code=code):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(watch, "_fetch", pages.get):
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(watch.main([]), code)
                printed = {"stdout": out, "stderr": err}[stream].getvalue()
                self.assertIn(expected, printed)

    def test_main_exits_2_when_the_check_could_not_run(self) -> None:
        def offline(url: str) -> bytes:
            raise OSError("network is unreachable")

        err = io.StringIO()
        with mock.patch.object(watch, "_fetch", offline), redirect_stderr(err):
            self.assertEqual(watch.main([]), 2)
        self.assertIn("could not check", err.getvalue())


class PyPIWatchWorkflowTests(unittest.TestCase):
    def test_workflow_is_scheduled_read_only_and_pinned(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertRegex(text, r'(?m)^  schedule:\n    - cron: "[^"]+"$')
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("permissions:\n  contents: read\n", text)
        self.assertEqual(text.count("permissions:"), 1)
        for forbidden in ("secrets.", "id-token", ": write"):
            self.assertNotIn(forbidden, text)
        self.assertIn("run: python3 scripts/check_pypi_releases.py\n", text)
        uses = re.findall(r"uses: (\S+)(.*)", text)
        self.assertTrue(uses)
        for reference, comment in uses:
            self.assertRegex(reference, r"@[0-9a-f]{40}$")
            self.assertRegex(comment, r"^ # v\d+(?:\.\d+)*$")


if __name__ == "__main__":
    unittest.main()
