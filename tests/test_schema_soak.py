from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_schema_soak.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SCHEMA_VERSIONS = {
    "COMMAND_RECEIPTS_SCHEMA_VERSION": "0.4.0",
    "DRIFT_REPORT_SCHEMA_VERSION": "0.2.2",
    "EXECUTION_CONTRACT_SCHEMA_VERSION": "0.3.0",
    "FILE_RECEIPTS_SCHEMA_VERSION": "0.4.0",
    "PLAN_SCHEMA_VERSION": "0.4.0",
    "PROOF_PACK_SCHEMA_VERSION": "0.11.0",
    "STEP_RUNS_SCHEMA_VERSION": "0.5.0",
    "VERIFICATION_RUNS_SCHEMA_VERSION": "0.4.0",
}

FREEZE_PATHS = (
    ".github/workflows/ci.yml",
    "schemas/command-receipts.schema.json",
    "schemas/drift-report.schema.json",
    "schemas/execution-contract.schema.json",
    "schemas/file-receipts.schema.json",
    "schemas/plan-lock.schema.json",
    "schemas/proof-pack.schema.json",
    "schemas/step-runs.schema.json",
    "schemas/verification-runs.schema.json",
    "scripts/check_schema_soak.py",
    "src/agentflow/aggregate.py",
    "src/agentflow/artifacts.py",
    "src/agentflow/capabilities.py",
    "src/agentflow/cli.py",
    "src/agentflow/contracts.py",
    "src/agentflow/coverage.py",
    "src/agentflow/draft_plan.py",
    "src/agentflow/events.py",
    "src/agentflow/execution.py",
    "src/agentflow/execution_coverage.py",
    "src/agentflow/git.py",
    "src/agentflow/handoff.py",
    "src/agentflow/hunks.py",
    "src/agentflow/packs.py",
    "src/agentflow/porcelain.py",
    "src/agentflow/proof.py",
    "src/agentflow/receipts.py",
    "src/agentflow/review.py",
    "src/agentflow/risk.py",
    "src/agentflow/stuck.py",
    "src/agentflow/validation.py",
    "src/agentflow/versioning.py",
    "src/agentflow/viewer.py",
    "src/agentflow/workflow_contract.py",
    "tests/fixtures/compatibility",
    "tests/fixtures/proof-bundle",
    "tests/test_aggregate.py",
    "tests/test_artifact_versioning.py",
    "tests/test_capabilities.py",
    "tests/test_cli.py",
    "tests/test_draft_plan.py",
    "tests/test_events.py",
    "tests/test_execution_contract.py",
    "tests/test_execution_state.py",
    "tests/test_execution_verification.py",
    "tests/test_handoff.py",
    "tests/test_hunks.py",
    "tests/test_packs.py",
    "tests/test_porcelain.py",
    "tests/test_proof.py",
    "tests/test_proof_compatibility.py",
    "tests/test_receipts.py",
    "tests/test_review.py",
    "tests/test_risk.py",
    "tests/test_schema_contracts.py",
    "tests/test_schema_soak.py",
    "tests/test_stuck.py",
    "tests/test_view_proof.py",
    "tests/test_workflow_contract.py",
)

WORKLOAD_IDS = (
    "aggregation",
    "ci-proof",
    "mcp-stdio",
    "released-v0.4.0",
    "workflow-pack",
)


class SchemaSoakCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for relative in FREEZE_PATHS:
            path = self.root / relative
            if relative in {
                "tests/fixtures/compatibility",
                "tests/fixtures/proof-bundle",
            }:
                path.mkdir(parents=True, exist_ok=True)
                path /= "fixture.txt"
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".py":
                content = "VALUE = 1\n"
            elif path.suffix == ".json":
                content = '{"flag": true}\n'
            else:
                content = f"baseline for {relative}\n"
            path.write_text(content, encoding="utf-8")
        contracts = "".join(
            f'{name} = "{version}"\n'
            for name, version in SCHEMA_VERSIONS.items()
        )
        (self.root / "src/agentflow/contracts.py").write_text(
            contracts, encoding="utf-8"
        )
        self._git("init", "-q")
        self._git("config", "user.email", "ci@agentflow.invalid")
        self._git("config", "user.name", "agentflow-ci")
        self.candidate = self._commit(
            "candidate", "2026-07-01T00:00:00+00:00"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True
        ).strip()

    def _commit(self, message: str, timestamp: str) -> str:
        self._git("add", "-A")
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = timestamp
        env["GIT_COMMITTER_DATE"] = timestamp
        subprocess.run(
            ["git", "commit", "-qm", message],
            cwd=self.root,
            env=env,
            check=True,
        )
        return self._git("rev-parse", "HEAD")

    def _manifest(self) -> dict:
        candidate_time = datetime.fromisoformat(
            self._git("show", "-s", "--format=%cI", self.candidate)
        ).astimezone(timezone.utc)
        started = candidate_time + timedelta(minutes=2)
        recorded = (candidate_time + timedelta(seconds=30)).isoformat().replace(
            "+00:00", "Z"
        )
        return {
            "schema_version": "0.1.0",
            "candidate_commit": self.candidate,
            "start_time_utc": started.isoformat().replace("+00:00", "Z"),
            "minimum_end_time_utc": (
                started + timedelta(days=21)
            ).isoformat().replace("+00:00", "Z"),
            "schema_versions": dict(SCHEMA_VERSIONS),
            "freeze_paths": list(FREEZE_PATHS),
            "workloads": [
                {
                    "id": workload_id,
                    "command": f"run {workload_id}",
                    "commit": self.candidate,
                    "outcome": "passed",
                    "recorded_at_utc": recorded,
                    "url": None,
                }
                for workload_id in WORKLOAD_IDS
            ],
        }

    def _write_manifest(self, manifest: dict) -> None:
        path = self.root / "docs/schema-freeze-soak.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._commit("record soak", "2026-07-01T00:01:00+00:00")

    def _run(self, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root or self.root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_absent_manifest_reports_soak_not_started(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "schema soak not started: docs/schema-freeze-soak.json is absent",
        )

    def test_absent_manifest_rejects_1_0_schema_version(self) -> None:
        contracts = self.root / "src/agentflow/contracts.py"
        contracts.write_text(
            contracts.read_text(encoding="utf-8").replace(
                'PLAN_SCHEMA_VERSION = "0.4.0"',
                'PLAN_SCHEMA_VERSION = "1.0.0"',
            ),
            encoding="utf-8",
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema constants must remain pre-1.0", result.stderr)

    def test_absent_manifest_outside_git_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(Path(tmp))

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema soak check failed:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_manifest_removed_after_soak_started(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "docs/schema-freeze-soak.json").unlink()

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("manifest was removed after the soak started", result.stderr)

    def test_valid_manifest_accepts_unchanged_candidate(self) -> None:
        self._write_manifest(self._manifest())

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"schema soak guard passed: {self.candidate}", result.stdout)

    def test_rejects_incomplete_declared_freeze_set(self) -> None:
        manifest = self._manifest()
        manifest["freeze_paths"].pop()
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze_paths must match the audited freeze set", result.stderr)

    def test_rejects_load_bearing_change_after_candidate(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "src/agentflow/proof.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)
        self.assertIn("src/agentflow/proof.py", result.stderr)

    def test_allows_semantically_equivalent_python_and_json_formatting(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "src/agentflow/proof.py").write_text(
            "# formatting-only comment\n\nVALUE=1\n", encoding="utf-8"
        )
        schema = self.root / "schemas/proof-pack.schema.json"
        schema.write_text(
            json.dumps(json.loads(schema.read_text(encoding="utf-8")), indent=4)
            + "\n",
            encoding="utf-8",
        )

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_json_boolean_changed_to_integer(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "schemas/proof-pack.schema.json").write_text(
            '{"flag": 1}\n', encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schemas/proof-pack.schema.json", result.stderr)

    def test_rejects_duplicate_keys_in_frozen_json(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "schemas/proof-pack.schema.json").write_text(
            '{"flag": false, "flag": true}\n', encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("frozen JSON path is invalid", result.stderr)
        self.assertIn("schemas/proof-pack.schema.json", result.stderr)

    def test_rejects_frozen_file_replaced_by_symlink(self) -> None:
        self._write_manifest(self._manifest())
        shadow = self.root / "shadow.py"
        shadow.write_text("VALUE = 1\n", encoding="utf-8")
        frozen = self.root / "src/agentflow/proof.py"
        frozen.unlink()
        frozen.symlink_to("../../shadow.py")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/agentflow/proof.py", result.stderr)

    def test_rejects_frozen_file_mode_change(self) -> None:
        self._write_manifest(self._manifest())
        frozen = self.root / "src/agentflow/proof.py"
        frozen.chmod(0o755)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/agentflow/proof.py", result.stderr)

    def test_rejects_minimum_end_that_is_not_exactly_21_days(self) -> None:
        manifest = self._manifest()
        manifest["minimum_end_time_utc"] = "2026-07-21T00:00:00Z"
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("minimum_end_time_utc must be exactly 21 days", result.stderr)

    def test_rejects_start_before_manifest_records_candidate(self) -> None:
        manifest = self._manifest()
        candidate_time = datetime.fromisoformat(
            self._git("show", "-s", "--format=%cI", self.candidate)
        ).astimezone(timezone.utc)
        started = candidate_time + timedelta(seconds=30)
        manifest["start_time_utc"] = started.isoformat().replace("+00:00", "Z")
        manifest["minimum_end_time_utc"] = (
            started + timedelta(days=21)
        ).isoformat().replace("+00:00", "Z")
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("start_time_utc must not predate manifest record", result.stderr)

    def test_rejects_date_only_utc_timestamp_without_traceback(self) -> None:
        manifest = self._manifest()
        manifest["start_time_utc"] = "2026-07-22Z"
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("start_time_utc must be UTC", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_schema_versions_that_do_not_match_candidate(self) -> None:
        manifest = self._manifest()
        manifest["schema_versions"]["PLAN_SCHEMA_VERSION"] = "0.3.0"
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema_versions do not match candidate", result.stderr)

    def test_rejects_unresolvable_candidate_without_traceback(self) -> None:
        manifest = self._manifest()
        manifest["candidate_commit"] = "0" * 40
        for workload in manifest["workloads"]:
            workload["commit"] = manifest["candidate_commit"]
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema soak check failed:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_non_string_workload_id_without_traceback(self) -> None:
        manifest = self._manifest()
        manifest["workloads"][0]["id"] = []
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("workload ids must be strings", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_workload_recorded_after_soak_start(self) -> None:
        manifest = self._manifest()
        start = datetime.fromisoformat(
            manifest["start_time_utc"].replace("Z", "+00:00")
        )
        manifest["workloads"][0]["recorded_at_utc"] = (
            start + timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be later than start_time_utc", result.stderr)

    def test_rejects_workload_backdated_before_candidate(self) -> None:
        manifest = self._manifest()
        candidate_time = datetime.fromisoformat(
            self._git("show", "-s", "--format=%cI", self.candidate)
        ).astimezone(timezone.utc)
        manifest["workloads"][0]["recorded_at_utc"] = (
            candidate_time - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be earlier than candidate_commit", result.stderr)

    def test_rejects_malformed_manifest_without_traceback(self) -> None:
        path = self.root / "docs/schema-freeze-soak.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n", encoding="utf-8")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema soak check failed:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rejects_non_utf8_manifest_without_traceback(self) -> None:
        path = self.root / "docs/schema-freeze-soak.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema soak check failed:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_ci_runs_schema_soak_guard_before_unit_tests(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        guard = workflow.index("run: python3 scripts/check_schema_soak.py")
        tests = workflow.index("PYTHONPATH=src python3 -m unittest discover")
        self.assertLess(guard, tests)


if __name__ == "__main__":
    unittest.main()
