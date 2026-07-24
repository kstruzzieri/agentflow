from __future__ import annotations

import importlib.util
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


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_schema_soak", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The freeze set, workload ids, and manifest version are imported rather than
# restated so the guard stays the single source of truth for all three.
guard = _load_guard()
FREEZE_PATHS = tuple(sorted(guard.FREEZE_PATHS))
WORKLOAD_IDS = tuple(sorted(guard.WORKLOAD_IDS))
FIXTURE_DIRECTORIES = ("tests/fixtures/compatibility", "tests/fixtures/proof-bundle")
CONTRACTS = guard.CONTRACTS_PATH

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


def _contracts_source(versions: dict[str, str]) -> str:
    return "".join(f'{name} = "{version}"\n' for name, version in versions.items())


class SchemaSoakCheckerTests(unittest.TestCase):
    """Every clock here is relative to now, so no fixture expires with time."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.now = datetime.now(timezone.utc)
        for relative in FREEZE_PATHS:
            path = self.root / relative
            if relative in FIXTURE_DIRECTORIES:
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
        (self.root / CONTRACTS).write_text(
            _contracts_source(SCHEMA_VERSIONS), encoding="utf-8"
        )
        self._git("init", "-q")
        self._git("config", "user.email", "ci@agentflow.invalid")
        self._git("config", "user.name", "agentflow-ci")
        self.candidate = self._commit("candidate", self.now - timedelta(days=30))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True
        ).strip()

    def _commit(self, message: str, when: datetime) -> str:
        self._git("add", "-A")
        env = os.environ.copy()
        stamp = when.isoformat()
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        subprocess.run(
            ["git", "commit", "-qm", message, "--allow-empty"],
            cwd=self.root,
            env=env,
            check=True,
        )
        return self._git("rev-parse", "HEAD")

    def _stamp(self, when: datetime) -> str:
        return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _manifest(self, **overrides) -> dict:
        manifest = {
            "schema_version": guard.MANIFEST_SCHEMA_VERSION,
            "candidate_commit": self.candidate,
            "schema_versions": dict(SCHEMA_VERSIONS),
            "freeze_paths": list(FREEZE_PATHS),
            "workloads": [
                {
                    "id": workload_id,
                    "command": f"run {workload_id}",
                    "commit": self.candidate,
                    "outcome": "passed",
                    "recorded_at_utc": self._stamp(self.now - timedelta(days=29)),
                    "url": None,
                }
                for workload_id in WORKLOAD_IDS
            ],
        }
        manifest.update(overrides)
        return manifest

    def _write_manifest(self, manifest: dict, age: timedelta | None = None) -> None:
        """Record the manifest. ``age`` is how long ago the soak clock started."""
        path = self.root / "docs/schema-freeze-soak.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        started = self.now - (age if age is not None else timedelta(days=1))
        self._commit("record soak", started)

    def _elapsed_manifest(self, manifest: dict | None = None) -> None:
        self._write_manifest(manifest or self._manifest(), age=timedelta(days=25))

    def _bump(self, **versions: str) -> None:
        merged = dict(SCHEMA_VERSIONS)
        merged.update(versions)
        (self.root / CONTRACTS).write_text(
            _contracts_source(merged), encoding="utf-8"
        )

    def _run(self, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root or self.root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    # -- soak not started ------------------------------------------------

    def test_absent_manifest_reports_soak_not_started(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "schema soak not started: docs/schema-freeze-soak.json is absent",
        )

    def test_absent_manifest_rejects_1_0_schema_version(self) -> None:
        self._bump(PLAN_SCHEMA_VERSION="1.0.0")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema constants must remain pre-1.0", result.stderr)

    def test_absent_manifest_rejects_stale_freeze_path(self) -> None:
        (self.root / "src/agentflow/coverage.py").unlink()

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze path missing from the working tree", result.stderr)
        self.assertIn("src/agentflow/coverage.py", result.stderr)

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

    # -- the clock -------------------------------------------------------

    def test_active_soak_reports_remaining_time(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=1))

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak in progress", result.stdout)
        self.assertIn("19d", result.stdout)
        self.assertNotIn("complete", result.stdout)

    def test_elapsed_soak_reports_complete(self) -> None:
        self._elapsed_manifest()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"schema soak complete: {self.candidate}", result.stdout)

    def test_soak_start_is_derived_from_the_recording_commit(self) -> None:
        """An hour short of 21 days is still in progress; no field can say otherwise."""
        self._write_manifest(self._manifest(), age=timedelta(days=20, hours=23))

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak in progress", result.stdout)

    def test_rejects_recording_commit_older_than_candidate(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=40))

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not predate candidate_commit", result.stderr)

    # -- version-only bump carve-out -------------------------------------

    def test_rejects_version_bump_before_soak_elapses(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=1))
        self._bump(PLAN_SCHEMA_VERSION="1.0.0")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)
        self.assertIn(CONTRACTS, result.stderr)

    def test_allows_version_only_bump_after_soak_elapses(self) -> None:
        self._elapsed_manifest()
        self._bump(**{name: "1.0.0" for name in SCHEMA_VERSIONS})

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak complete", result.stdout)

    def test_rejects_version_bump_bundled_with_another_change(self) -> None:
        self._elapsed_manifest()
        (self.root / CONTRACTS).write_text(
            _contracts_source({**SCHEMA_VERSIONS, "PLAN_SCHEMA_VERSION": "1.0.0"})
            + 'SNEAKY_NEW_FIELD = "added"\n',
            encoding="utf-8",
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)
        self.assertIn(CONTRACTS, result.stderr)

    def test_rejects_version_downgrade_after_soak_elapses(self) -> None:
        self._elapsed_manifest()
        self._bump(PLAN_SCHEMA_VERSION="0.3.0")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)

    def test_rejects_other_frozen_change_after_soak_elapses(self) -> None:
        """Elapsing the clock unlocks contracts.py only, not the rest of the set."""
        self._elapsed_manifest()
        (self.root / "src/agentflow/proof.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/agentflow/proof.py", result.stderr)

    def test_rejects_candidate_that_already_declares_1_0(self) -> None:
        """A 1.0 candidate would publish 1.0 on day zero with no soak behind it."""
        bumped = {name: "1.0.0" for name in SCHEMA_VERSIONS}
        (self.root / CONTRACTS).write_text(
            _contracts_source(bumped), encoding="utf-8"
        )
        self.candidate = self._commit("premature bump", self.now - timedelta(days=29))
        self._write_manifest(self._manifest(schema_versions=bumped))

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must record pre-1.0 schema constants", result.stderr)

    # -- freeze set ------------------------------------------------------

    def test_valid_manifest_accepts_unchanged_candidate(self) -> None:
        self._write_manifest(self._manifest())

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.candidate, result.stdout)

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

    def test_rejects_frozen_path_deleted_from_worktree(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "src/agentflow/coverage.py").unlink()

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)
        self.assertIn("src/agentflow/coverage.py", result.stderr)

    def test_rejects_new_file_under_frozen_fixture_directory(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "tests/fixtures/compatibility/extra.json").write_text(
            '{"added": true}\n', encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("tests/fixtures/compatibility/extra.json", result.stderr)

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

    def test_accepts_unchanged_symlink_in_freeze_set(self) -> None:
        link = self.root / "tests/fixtures/proof-bundle/link.txt"
        link.symlink_to("fixture.txt")
        self.candidate = self._commit("add symlink", self.now - timedelta(days=29))
        self._write_manifest(self._manifest())

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_retargeted_symlink_in_freeze_set(self) -> None:
        link = self.root / "tests/fixtures/proof-bundle/link.txt"
        link.symlink_to("fixture.txt")
        self.candidate = self._commit("add symlink", self.now - timedelta(days=29))
        self._write_manifest(self._manifest())
        link.unlink()
        link.symlink_to("../compatibility/fixture.txt")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("tests/fixtures/proof-bundle/link.txt", result.stderr)

    def test_rejects_frozen_file_mode_change(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "src/agentflow/proof.py").chmod(0o755)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/agentflow/proof.py", result.stderr)

    # -- manifest shape --------------------------------------------------

    def test_rejects_schema_versions_that_do_not_match_candidate(self) -> None:
        manifest = self._manifest()
        manifest["schema_versions"]["PLAN_SCHEMA_VERSION"] = "0.3.0"
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schema_versions do not match candidate", result.stderr)

    def test_rejects_declared_start_time_as_unknown_field(self) -> None:
        """The clock is a Git fact; the manifest may not restate it."""
        manifest = self._manifest()
        manifest["start_time_utc"] = self._stamp(self.now)
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown fields: start_time_utc", result.stderr)

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

    def test_rejects_candidate_that_is_not_an_ancestor_of_head(self) -> None:
        self._git("checkout", "-q", "-b", "sidebranch")
        (self.root / "src/agentflow/proof.py").write_text(
            "VALUE = 3\n", encoding="utf-8"
        )
        orphan = self._commit("sidebranch work", self.now - timedelta(days=29))
        self._git("checkout", "-q", "-")
        manifest = self._manifest(candidate_commit=orphan)
        for workload in manifest["workloads"]:
            workload["commit"] = orphan
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be an ancestor of HEAD", result.stderr)

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

    def test_reports_unreadable_manifest_history_distinctly(self) -> None:
        path = self.root / "docs/schema-freeze-soak.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n", encoding="utf-8")
        self._commit("record corrupt soak", self.now - timedelta(days=2))
        path.write_text(
            json.dumps(self._manifest(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("unreadable in Git history", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # -- workloads -------------------------------------------------------

    def test_rejects_non_string_workload_id_without_traceback(self) -> None:
        manifest = self._manifest()
        manifest["workloads"][0]["id"] = []
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("workload ids must be strings", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_allows_workload_recorded_during_the_soak(self) -> None:
        """Issue #5 requires the workloads to be exercised during the soak."""
        manifest = self._manifest()
        manifest["workloads"][0]["recorded_at_utc"] = self._stamp(
            self.now - timedelta(hours=1)
        )
        self._write_manifest(manifest, age=timedelta(days=5))

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_workload_recorded_in_the_future(self) -> None:
        manifest = self._manifest()
        manifest["workloads"][0]["recorded_at_utc"] = self._stamp(
            self.now + timedelta(days=1)
        )
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be in the future", result.stderr)

    def test_rejects_workload_backdated_before_candidate(self) -> None:
        manifest = self._manifest()
        manifest["workloads"][0]["recorded_at_utc"] = self._stamp(
            self.now - timedelta(days=31)
        )
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be earlier than candidate_commit", result.stderr)

    def test_rejects_date_only_utc_timestamp_without_traceback(self) -> None:
        manifest = self._manifest()
        manifest["workloads"][0]["recorded_at_utc"] = "2026-07-22Z"
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be UTC", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class FreezeSetContractTests(unittest.TestCase):
    def test_every_freeze_path_exists_in_this_repository(self) -> None:
        missing = sorted(
            path for path in guard.FREEZE_PATHS if not (REPO_ROOT / path).exists()
        )

        self.assertEqual(missing, [])

    def test_contracts_path_is_frozen(self) -> None:
        self.assertIn(guard.CONTRACTS_PATH, guard.FREEZE_PATHS)

    def test_ci_runs_schema_soak_guard_before_unit_tests(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        guard_index = workflow.index("run: python3 scripts/check_schema_soak.py")
        tests_index = workflow.index("PYTHONPATH=src python3 -m unittest discover")
        self.assertLess(guard_index, tests_index)


if __name__ == "__main__":
    unittest.main()
