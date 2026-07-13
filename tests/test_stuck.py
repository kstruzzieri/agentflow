from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentflow.stuck import Thresholds, detect_stuck, stuck_block
from agentflow.contracts import ARTIFACT_SCHEMA_VERSIONS, EXECUTION_ARTIFACT_SCHEMA_VERSIONS


def _write(root: Path, name: str, records: list) -> None:
    schema_version = EXECUTION_ARTIFACT_SCHEMA_VERSIONS.get(
        name, ARTIFACT_SCHEMA_VERSIONS.get(name)
    )
    records = [
        {"schema_version": schema_version, **record}
        if schema_version is not None
        else record
        for record in records
    ]
    path = root / ".agent" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _cmd(cr_id, attempt, started, exit_code, command, **extra):
    record = {
        "id": cr_id,
        "step_id": "P1",
        "attempt_id": attempt,
        "started_at": started,
        "exit_code": exit_code,
        "command": command,
        "cwd": ".",
        "decision": extra.pop("decision", "allowed"),
    }
    record.update(extra)
    return record


def _verify(vr_id, attempt, recorded, status, scope="step"):
    record = {"id": vr_id, "scope": scope, "status": status, "recorded_at": recorded}
    if scope == "step":
        record["step_id"] = "P1"
        record["attempt_id"] = attempt
    return record


def _file(fr_id, attempt, recorded, path="a.py"):
    return {
        "id": fr_id,
        "step_id": "P1",
        "attempt_id": attempt,
        "recorded_at": recorded,
        "path": path,
        "change_kind": "modified",
    }


def _run_cli(root: Path, *cli_args):
    return subprocess.run(
        [sys.executable, "-m", "agentflow", "detect-stuck", "--root", str(root), *cli_args],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
        cwd=str(Path(__file__).resolve().parents[1]),
    )


class CoreTests(unittest.TestCase):
    def test_missing_ledgers_return_ok(self) -> None:
        with TemporaryDirectory() as tmp:
            report = detect_stuck(Path(tmp))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["summary"]["rules_evaluated"], 3)
            self.assertEqual(report["summary"]["finding_count"], 0)

    def test_events_without_step_attempt_are_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "verification-runs", [
                _verify("VR1", None, "2026-06-30T10:00:00+00:00", "failed", scope="run"),
                _verify("VR2", None, "2026-06-30T10:01:00+00:00", "failed", scope="run"),
                _verify("VR3", None, "2026-06-30T10:02:00+00:00", "failed", scope="run"),
            ])
            self.assertEqual(detect_stuck(root)["status"], "ok")

    def test_stuck_block_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            block = stuck_block(Path(tmp))
            self.assertEqual(block, {"rules_evaluated": 3, "findings": []})


class RepeatedCommandFailureTests(unittest.TestCase):
    def test_fires_after_n_identical_failures(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", "2026-06-30T10:00:00+00:00", 1, ["pytest", "-k", "foo"]),
                _cmd("CR2", "A1", "2026-06-30T10:01:00+00:00", 1, ["pytest", "-k", "foo"]),
                _cmd("CR3", "A1", "2026-06-30T10:02:00+00:00", 1, ["pytest", "-k", "foo"]),
            ])
            findings = detect_stuck(root)["findings"]
            self.assertEqual(len(findings), 1)
            finding = findings[0]
            self.assertEqual(finding["rule"], "repeated_command_failure")
            self.assertEqual(finding["severity"], "warning")
            self.assertEqual(finding["step_id"], "P1")
            self.assertEqual(finding["attempt_id"], "A1")
            self.assertEqual(finding["threshold"], 3)
            self.assertEqual(finding["evidence"]["failure_count"], 3)
            self.assertEqual(finding["evidence"]["receipt_ids"], ["CR1", "CR2", "CR3"])
            self.assertEqual(finding["first_event"]["record_id"], "CR1")
            self.assertEqual(finding["last_event"]["record_id"], "CR3")
            self.assertIn("no change in outcome", finding["message"])

    def test_success_interleave_resets_and_does_not_fire(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", "2026-06-30T10:00:00+00:00", 1, ["pytest"]),
                _cmd("CR2", "A1", "2026-06-30T10:01:00+00:00", 1, ["pytest"]),
                _cmd("CR3", "A1", "2026-06-30T10:02:00+00:00", 0, ["pytest"]),
                _cmd("CR4", "A1", "2026-06-30T10:03:00+00:00", 1, ["pytest"]),
            ])
            self.assertEqual(detect_stuck(root)["status"], "ok")

    def test_message_says_consecutively_when_exit_codes_differ(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", "2026-06-30T10:00:00+00:00", 1, ["make"]),
                _cmd("CR2", "A1", "2026-06-30T10:01:00+00:00", 2, ["make"]),
                _cmd("CR3", "A1", "2026-06-30T10:02:00+00:00", 1, ["make"]),
            ])
            message = detect_stuck(root)["findings"][0]["message"]
            self.assertIn("consecutively", message)
            self.assertNotIn("no change in outcome", message)

    def test_different_gate_is_a_different_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", "2026-06-30T10:00:00+00:00", 1, ["pytest"], gate="g1"),
                _cmd("CR2", "A1", "2026-06-30T10:01:00+00:00", 1, ["pytest"], gate="g2"),
                _cmd("CR3", "A1", "2026-06-30T10:02:00+00:00", 1, ["pytest"], gate="g1"),
            ])
            self.assertEqual(detect_stuck(root)["status"], "ok")


class RepeatedVerifyFailureTests(unittest.TestCase):
    def test_fires_on_consecutive_step_verify_failures_without_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "verification-runs", [
                _verify("VR1", "A1", "2026-06-30T10:00:00+00:00", "failed"),
                _verify("VR2", "A1", "2026-06-30T10:01:00+00:00", "failed"),
            ])
            findings = detect_stuck(root)["findings"]
            self.assertEqual(len(findings), 1)
            finding = findings[0]
            self.assertEqual(finding["rule"], "repeated_verify_failure")
            self.assertEqual(finding["threshold"], 2)
            self.assertEqual(finding["evidence"]["verify_count"], 2)
            self.assertEqual(finding["evidence"]["verification_ids"], ["VR1", "VR2"])

    def test_file_change_between_failures_resets(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "verification-runs", [
                _verify("VR1", "A1", "2026-06-30T10:00:00+00:00", "failed"),
                _verify("VR2", "A1", "2026-06-30T10:02:00+00:00", "failed"),
            ])
            _write(root, "file-receipts", [
                _file("FR1", "A1", "2026-06-30T10:01:00+00:00"),
            ])
            self.assertEqual(detect_stuck(root)["status"], "ok")

    def test_run_scope_verifications_do_not_fire_r2(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "verification-runs", [
                _verify("VR1", None, "2026-06-30T10:00:00+00:00", "failed", scope="run"),
                _verify("VR2", None, "2026-06-30T10:01:00+00:00", "failed", scope="run"),
            ])
            self.assertEqual(detect_stuck(root)["status"], "ok")

    def test_passing_verify_breaks_the_streak(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "verification-runs", [
                _verify("VR1", "A1", "2026-06-30T10:00:00+00:00", "failed"),
                _verify("VR2", "A1", "2026-06-30T10:01:00+00:00", "passed"),
                _verify("VR3", "A1", "2026-06-30T10:02:00+00:00", "failed"),
            ])
            self.assertEqual(detect_stuck(root)["status"], "ok")


class AlternatingNoOpTests(unittest.TestCase):
    def test_fires_on_period_two_cycle_repeated_k_times(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = [f"2026-06-30T10:0{i}:00+00:00" for i in range(6)]
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", times[0], 0, ["a"]),
                _cmd("CR2", "A1", times[1], 0, ["b"]),
                _cmd("CR3", "A1", times[2], 0, ["a"]),
                _cmd("CR4", "A1", times[3], 0, ["b"]),
                _cmd("CR5", "A1", times[4], 0, ["a"]),
                _cmd("CR6", "A1", times[5], 0, ["b"]),
            ])
            findings = [
                f for f in detect_stuck(root)["findings"]
                if f["rule"] == "alternating_no_op"
            ]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["threshold"], 3)
            self.assertEqual(findings[0]["evidence"]["period"], 2)
            self.assertEqual(findings[0]["evidence"]["repeats"], 3)

    def test_does_not_fire_below_threshold(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", "2026-06-30T10:00:00+00:00", 0, ["a"]),
                _cmd("CR2", "A1", "2026-06-30T10:01:00+00:00", 0, ["b"]),
                _cmd("CR3", "A1", "2026-06-30T10:02:00+00:00", 0, ["a"]),
                _cmd("CR4", "A1", "2026-06-30T10:03:00+00:00", 0, ["b"]),
            ])
            findings = [
                f for f in detect_stuck(root)["findings"]
                if f["rule"] == "alternating_no_op"
            ]
            self.assertEqual(findings, [])

    def test_file_change_in_span_suppresses(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = [f"2026-06-30T10:0{i}:00+00:00" for i in range(7)]
            _write(root, "command-receipts", [
                _cmd("CR1", "A1", times[0], 0, ["a"]),
                _cmd("CR2", "A1", times[1], 0, ["b"]),
                _cmd("CR3", "A1", times[2], 0, ["a"]),
                _cmd("CR4", "A1", times[4], 0, ["b"]),
                _cmd("CR5", "A1", times[5], 0, ["a"]),
                _cmd("CR6", "A1", times[6], 0, ["b"]),
            ])
            _write(root, "file-receipts", [
                _file("FR1", "A1", times[3]),
            ])
            findings = [
                f for f in detect_stuck(root)["findings"]
                if f["rule"] == "alternating_no_op"
            ]
            self.assertEqual(findings, [])


class CliTests(unittest.TestCase):
    def _stuck_root(self, tmp: str) -> Path:
        root = Path(tmp)
        _write(root, "command-receipts", [
            _cmd("CR1", "A1", "2026-06-30T10:00:00+00:00", 1, ["pytest"]),
            _cmd("CR2", "A1", "2026-06-30T10:01:00+00:00", 1, ["pytest"]),
            _cmd("CR3", "A1", "2026-06-30T10:02:00+00:00", 1, ["pytest"]),
        ])
        return root

    def test_exit_zero_by_default_even_when_stuck(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._stuck_root(tmp)
            result = _run_cli(root, "--json")
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "stuck")
            self.assertEqual(len(payload["findings"]), 1)

    def test_strict_exits_one_when_findings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._stuck_root(tmp)
            result = _run_cli(root, "--strict")
            self.assertEqual(result.returncode, 1)

    def test_strict_exits_zero_when_clean(self) -> None:
        with TemporaryDirectory() as tmp:
            result = _run_cli(Path(tmp), "--strict")
            self.assertEqual(result.returncode, 0)

    def test_threshold_flag_changes_detection(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._stuck_root(tmp)
            result = _run_cli(root, "--json", "--min-command-failures", "4")
            self.assertEqual(json.loads(result.stdout)["status"], "ok")

    def test_threshold_flags_reject_zero(self) -> None:
        for flag in (
            "--min-command-failures",
            "--min-verify-failures",
            "--min-cycle-repeats",
        ):
            with TemporaryDirectory() as tmp:
                result = _run_cli(Path(tmp), flag, "0")
                self.assertEqual(result.returncode, 2)
                self.assertIn("positive integer", result.stderr)


if __name__ == "__main__":
    unittest.main()
