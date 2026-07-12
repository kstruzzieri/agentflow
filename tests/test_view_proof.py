from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentflow.artifacts import append_jsonl, try_read_json, write_json
from agentflow.viewer import collect_view_model, render_html

ROOT = Path(__file__).resolve().parents[1]


def run_agentflow(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def sample_model() -> dict:
    return {
        "proof": {
            "schema_version": "0.5.0",
            "meta": {"created_at": "2026-07-01T00:00:00+00:00", "tool_version": "0.3.0"},
            "core_sha256": "c0ffee" * 10 + "beef",
            "files": [
                {"path": ".agent/plan.lock.json", "sha256": "ab" * 32},
            ],
            "checks": [
                {"id": "drift_audit", "status": "passed"},
                {"id": "unused_evidence_ids", "status": "warning", "count": 1},
                {"id": "dangling_supports", "status": "failed", "count": 2},
            ],
        },
        "plan": {
            "objective": "Ship the fixture feature.",
            "scope": ["Touch only fixture files."],
            "validation_gates": ["PYTHONPATH=src python3 -m unittest discover -s tests"],
            "steps": [
                {"id": "P1", "action": "Do the work.", "validation": ["unit tests"]},
            ],
        },
        "command_receipts": [
            {
                "id": "CR1",
                "step_id": "P1",
                "provenance": "observed",
                "command": ["echo", "<script>alert(1)</script>"],
                "exit_code": 0,
                "decision": "allowed",
                "risk": {"level": "low"},
                "stdout_path": ".agent/receipts/A1/CR1.stdout.txt",
                "stderr_path": None,
                "stdout_href": "receipts/A1/CR1.stdout.txt",
                "stderr_href": None,
            }
        ],
        "file_receipts": [
            {
                "id": "FR1",
                "step_id": "P1",
                "path": '"><img src=x onerror=alert(1)>.txt',
                "change_kind": "modified",
            }
        ],
        "step_state": {
            "steps": {"P1": {"completed": True}},
            "attempts": {"A1": {"step_id": "P1", "status": "completed"}},
        },
        "drift": {
            "status": "pass",
            "notes": ["note with <b>markup</b>"],
            "unmapped_hunks": [{"path": "stray.txt"}],
        },
        "warnings": [],
    }


class RenderHtmlTests(unittest.TestCase):
    def test_report_contains_all_sections(self) -> None:
        html_out = render_html(sample_model())
        for heading in (
            "Objective",
            "Scope",
            "Step Status",
            "Validation Gates",
            "Command Receipts",
            "File Receipts",
            "Drift Audit",
            "Checks",
            "Residual Warnings",
            "Proof Hashes",
        ):
            self.assertIn(f"<h2>{heading}</h2>", html_out)
        self.assertIn("Ship the fixture feature.", html_out)
        self.assertIn("Touch only fixture files.", html_out)
        self.assertIn("c0ffee", html_out)

    def test_banner_names_verify_proof_as_authoritative(self) -> None:
        html_out = render_html(sample_model())
        self.assertIn("Review aid", html_out)
        self.assertIn("verify-proof", html_out)
        self.assertIn("authoritative", html_out)

    def test_hostile_content_is_escaped(self) -> None:
        html_out = render_html(sample_model())
        self.assertNotIn("<script", html_out)
        self.assertNotIn("<img", html_out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_out)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(1)&gt;.txt", html_out)

    def test_no_external_references(self) -> None:
        html_out = render_html(sample_model())
        self.assertNotIn("http://", html_out)
        self.assertNotIn("https://", html_out)

    def test_receipt_output_links(self) -> None:
        html_out = render_html(sample_model())
        self.assertIn('<a href="receipts/A1/CR1.stdout.txt">stdout</a>', html_out)
        # stderr has no href: no stderr anchor at all
        self.assertNotIn(">stderr</a>", html_out)

    def test_missing_optional_data_renders_not_recorded(self) -> None:
        model = sample_model()
        model["plan"] = None
        model["command_receipts"] = []
        model["file_receipts"] = []
        model["step_state"] = None
        model["drift"] = None
        html_out = render_html(model)
        self.assertIn("Plan not recorded.", html_out)
        self.assertIn("No command receipts recorded.", html_out)
        self.assertIn("No file receipts recorded.", html_out)
        self.assertIn("Drift report not recorded.", html_out)

    def test_step_status_reports_completion_and_attempts(self) -> None:
        html_out = render_html(sample_model())
        self.assertIn("<h2>Step Status</h2>", html_out)
        self.assertIn("P1", html_out)
        self.assertIn("completed", html_out)

    def test_step_status_without_execution_ledger(self) -> None:
        model = sample_model()
        model["step_state"] = None
        html_out = render_html(model)
        self.assertIn("execution ledger not recorded", html_out)

    def test_residual_warnings_list_warning_checks(self) -> None:
        html_out = render_html(sample_model())
        self.assertIn("unused_evidence_ids", html_out)
        self.assertIn('class="status-warning"', html_out)
        self.assertIn('class="status-failed"', html_out)

    def test_render_is_deterministic(self) -> None:
        self.assertEqual(render_html(sample_model()), render_html(sample_model()))

    def test_collect_warnings_surface_in_report(self) -> None:
        model = sample_model()
        model["warnings"] = ["command receipts ledger unreadable"]
        html_out = render_html(model)
        self.assertIn("command receipts ledger unreadable", html_out)

    def test_malformed_plan_steps_do_not_break_rendering(self) -> None:
        model = sample_model()
        model["plan"]["scope"] = "bad scope"
        model["plan"]["validation_gates"] = "not a list"
        model["plan"]["steps"] = [
            "bad step",
            {"id": "P2", "action": "String validation.", "validation": "unit tests"},
            {"id": "P3", "action": "List validation.", "validation": ["real gate"]},
        ]
        model["command_receipts"][0]["risk"] = "not a dict"
        model["drift"]["notes"] = "bad notes"
        html_out = render_html(model)
        self.assertIn("No scope recorded.", html_out)
        self.assertIn("P3", html_out)
        self.assertIn("real gate", html_out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_out)
        self.assertNotIn("<li>b</li>", html_out)
        self.assertNotIn("<li>u</li>", html_out)


def _write_fixture(root: Path, with_execution: bool = True) -> None:
    write_json(
        root / ".agent/plan.lock.json",
        {
            "objective": "Fixture objective.",
            "scope": ["Fixture scope."],
            "validation_gates": ["fixture gate"],
            "steps": [{"id": "P1", "action": "Do work.", "validation": ["unit tests"]}],
        },
    )
    write_json(
        root / ".agent/proof-pack.json",
        {
            "schema_version": "0.5.0",
            "meta": {"created_at": "2026-07-01T00:00:00+00:00", "tool_version": "0.3.0"},
            "core_sha256": "ab" * 32,
            "files": [{"path": ".agent/plan.lock.json", "sha256": "cd" * 32}],
            "checks": [{"id": "drift_audit", "status": "passed"}],
        },
    )
    write_json(
        root / ".agent/drift-report.json",
        {"schema_version": "0.2.0", "status": "pass", "notes": []},
    )
    if not with_execution:
        return
    write_json(root / ".agent/execution.contract.json", {"schema_version": "0.3.0"})
    stdout_file = root / ".agent/receipts/A1/CR1.stdout.txt"
    stdout_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_file.write_text("ok\n", encoding="utf-8")
    append_jsonl(
        root / ".agent/command-receipts.jsonl",
        {
            "id": "CR1",
            "step_id": "P1",
            "attempt_id": "A1",
            "provenance": "observed",
            "command": ["echo", "ok"],
            "exit_code": 0,
            "decision": "allowed",
            "risk": {"level": "low"},
            "stdout_path": ".agent/receipts/A1/CR1.stdout.txt",
            "stderr_path": ".agent/receipts/A1/CR1.stderr.txt",
        },
    )
    append_jsonl(
        root / ".agent/file-receipts.jsonl",
        {"id": "FR1", "step_id": "P1", "path": "fixture.txt", "change_kind": "modified"},
    )


class CollectViewModelTests(unittest.TestCase):
    def test_full_fixture_collects_all_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            model = collect_view_model(
                root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
            )
            self.assertEqual(model["plan"]["objective"], "Fixture objective.")
            self.assertEqual(model["proof"]["schema_version"], "0.5.0")
            self.assertEqual(len(model["command_receipts"]), 1)
            self.assertEqual(len(model["file_receipts"]), 1)
            self.assertEqual(model["drift"]["status"], "pass")
            self.assertEqual(model["warnings"], [])

    def test_hrefs_relative_to_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            model = collect_view_model(
                root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
            )
            receipt = model["command_receipts"][0]
            self.assertEqual(receipt["stdout_href"], "receipts/A1/CR1.stdout.txt")
            # recorded stderr path has no file on disk: no link
            self.assertIsNone(receipt["stderr_href"])

    def test_hrefs_for_custom_output_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            model = collect_view_model(
                root, root / ".agent/proof-pack.json", root / "reports/out.html"
            )
            receipt = model["command_receipts"][0]
            self.assertEqual(receipt["stdout_href"], "../.agent/receipts/A1/CR1.stdout.txt")

    def test_unsafe_receipt_output_paths_do_not_emit_hrefs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            _write_fixture(root)
            external = base / "secret.txt"
            external.write_text("secret\n", encoding="utf-8")
            scheme_path = root / ".agent/javascript:alert(1)"
            scheme_path.write_text("x\n", encoding="utf-8")
            for receipt_id, stdout_path in (
                ("ABS", str(external)),
                ("DOTDOT", "../secret.txt"),
                ("SCHEME", ".agent/javascript:alert(1)"),
            ):
                append_jsonl(
                    root / ".agent/command-receipts.jsonl",
                    {
                        "id": receipt_id,
                        "step_id": "P1",
                        "attempt_id": "A1",
                        "stdout_path": stdout_path,
                    },
                )
            model = collect_view_model(
                root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
            )
            hrefs = {receipt["id"]: receipt["stdout_href"] for receipt in model["command_receipts"]}
            self.assertEqual(hrefs["CR1"], "receipts/A1/CR1.stdout.txt")
            self.assertIsNone(hrefs["ABS"])
            self.assertIsNone(hrefs["DOTDOT"])
            self.assertIsNone(hrefs["SCHEME"])

    def test_missing_optional_artifacts_degrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root, with_execution=False)
            (root / ".agent/drift-report.json").unlink()
            (root / ".agent/plan.lock.json").unlink()
            model = collect_view_model(
                root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
            )
            self.assertIsNone(model["plan"])
            self.assertIsNone(model["step_state"])
            self.assertIsNone(model["drift"])
            self.assertEqual(model["command_receipts"], [])
            self.assertEqual(model["file_receipts"], [])
            # renders without raising
            self.assertIn("<h2>Objective</h2>", render_html(model))

    def test_unreadable_ledger_degrades_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            (root / ".agent/command-receipts.jsonl").write_text("{not json\n", encoding="utf-8")
            model = collect_view_model(
                root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
            )
            self.assertEqual(model["command_receipts"], [])
            self.assertTrue(any("command-receipts" in warning for warning in model["warnings"]))

    def test_ledger_oserror_degrades_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            with patch("agentflow.viewer.read_jsonl", side_effect=OSError("permission denied")):
                model = collect_view_model(
                    root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
                )
            self.assertEqual(model["command_receipts"], [])
            self.assertEqual(model["file_receipts"], [])
            self.assertTrue(any("command-receipts" in warning for warning in model["warnings"]))
            self.assertTrue(any("file-receipts" in warning for warning in model["warnings"]))

    def test_optional_json_oserror_degrades_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)

            def fake_try_read_json(path: Path) -> tuple[dict | None, str | None]:
                if path.name in {"plan.lock.json", "drift-report.json"}:
                    raise OSError("permission denied")
                return try_read_json(path)

            with patch("agentflow.viewer.try_read_json", side_effect=fake_try_read_json):
                model = collect_view_model(
                    root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
                )
            self.assertIsNone(model["plan"])
            self.assertIsNone(model["drift"])
            self.assertTrue(any("plan.lock.json unreadable" in warning for warning in model["warnings"]))
            self.assertTrue(any("drift-report.json unreadable" in warning for warning in model["warnings"]))

    def test_missing_proof_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError) as ctx:
                collect_view_model(
                    root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
                )
            self.assertIn("build-proof", str(ctx.exception))

    def test_proof_oserror_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            with patch("agentflow.viewer.try_read_json", side_effect=OSError("permission denied")):
                with self.assertRaises(ValueError) as ctx:
                    collect_view_model(
                        root, root / ".agent/proof-pack.json", root / ".agent/proof-report.html"
                    )
            self.assertIn("build-proof", str(ctx.exception))


class ViewProofCliTests(unittest.TestCase):
    def test_happy_path_writes_default_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            result = run_agentflow(root, "view-proof", "--html")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("created .agent/proof-report.html", result.stdout)
            report = (root / ".agent/proof-report.html").read_text(encoding="utf-8")
            self.assertIn("<h2>Objective</h2>", report)
            self.assertIn('href="receipts/A1/CR1.stdout.txt"', report)

    def test_failed_checks_still_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            proof_path = root / ".agent/proof-pack.json"
            write_json(
                proof_path,
                {
                    "schema_version": "0.5.0",
                    "meta": {},
                    "core_sha256": "ab" * 32,
                    "files": [],
                    "checks": [{"id": "drift_audit", "status": "failed"}],
                },
            )
            result = run_agentflow(root, "view-proof", "--html")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_proof_exits_one_and_names_build_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_agentflow(root, "view-proof", "--html")
            self.assertEqual(result.returncode, 1)
            self.assertIn("build-proof", result.stderr)

    def test_without_html_flag_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            result = run_agentflow(root, "view-proof")
            self.assertEqual(result.returncode, 2)
            self.assertIn("only --html output is supported", result.stderr)
            self.assertFalse((root / ".agent/proof-report.html").exists())

    def test_custom_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            result = run_agentflow(root, "view-proof", "--html", "--output", "reports/out.html")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = (root / "reports/out.html").read_text(encoding="utf-8")
            self.assertIn('href="../.agent/receipts/A1/CR1.stdout.txt"', report)

    def test_view_proof_mutates_no_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fixture(root)
            before = {
                path: (root / path).read_bytes()
                for path in (
                    ".agent/proof-pack.json",
                    ".agent/plan.lock.json",
                    ".agent/command-receipts.jsonl",
                    ".agent/file-receipts.jsonl",
                    ".agent/drift-report.json",
                )
            }
            result = run_agentflow(root, "view-proof", "--html")
            self.assertEqual(result.returncode, 0, result.stderr)
            for path, content in before.items():
                self.assertEqual((root / path).read_bytes(), content, path)


if __name__ == "__main__":
    unittest.main()
