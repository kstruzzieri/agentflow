from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


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


SCHEMA_FILES = tuple(sorted(guard.SCHEMA_FILE_CONSTANTS))


def _published_schema(pattern: str) -> str:
    return (
        json.dumps(
            {
                "title": "fixture",
                "properties": {
                    "schema_version": {"type": "string", "pattern": pattern}
                },
            },
            indent=2,
        )
        + "\n"
    )


class SoakHarness:
    """Shared temp-repo harness.

    Not a TestCase: subclassing a TestCase would re-run every inherited test
    once per subclass.
    """

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
            if relative in guard.SCHEMA_FILE_CONSTANTS:
                # Published schemas carry a schema_version pattern, and the
                # guard now checks that each one accepts its constant. A stub
                # without one would fail that check for the wrong reason.
                content = _published_schema(r"^0\.[0-9]+\.[0-9]+$")
            elif path.suffix == ".py":
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
            "workflow_run_id": 123,
            "schema_versions": dict(SCHEMA_VERSIONS),
            "freeze_paths": list(FREEZE_PATHS),
            "workloads": [
                {
                    "id": workload_id,
                    "command": f"run {workload_id}",
                    "commit": self.candidate,
                    "outcome": "passed",
                    "recorded_at_utc": self._stamp(self.now - timedelta(hours=1)),
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
        self.workflow_run = {
            "repository": {"full_name": "agentflow/test"},
            "path": ".github/workflows/ci.yml",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": self._git("rev-parse", "HEAD"),
            "created_at": self._stamp(started),
        }

    def _elapsed_manifest(self, manifest: dict | None = None) -> None:
        self._write_manifest(manifest or self._manifest(), age=timedelta(days=25))

    def _bump(self, **versions: str) -> None:
        merged = dict(SCHEMA_VERSIONS)
        merged.update(versions)
        (self.root / CONTRACTS).write_text(
            _contracts_source(merged), encoding="utf-8"
        )

    def _run(self, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_API_URL": "https://api.github.test",
                    "GITHUB_REPOSITORY": "agentflow/test",
                    "GITHUB_TOKEN": "test-token",
                },
                clear=False,
            ),
            patch.object(
                guard,
                "_fetch_workflow_run",
                return_value=getattr(self, "workflow_run", {}),
                create=True,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            returncode = guard.main(["--root", str(root or self.root)])
        return subprocess.CompletedProcess(
            [sys.executable, str(SCRIPT), "--root", str(root or self.root)],
            returncode,
            stdout.getvalue(),
            stderr.getvalue(),
        )


class SchemaSoakCheckerTests(SoakHarness, unittest.TestCase):
    """Every clock here is relative to now, so no fixture expires with time."""

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

    def test_missing_workflow_run_waits_without_unlocking_the_soak(self) -> None:
        self._write_manifest(
            self._manifest(workflow_run_id=None, workloads=[]), age=timedelta(days=25)
        )

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("awaiting trusted main-CI observation", result.stdout)

    def test_missing_workflow_run_rejects_early_workload_evidence(self) -> None:
        self._write_manifest(self._manifest(workflow_run_id=None), age=timedelta(days=25))

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("require a trusted main-CI observation", result.stderr)

    def test_elapsed_clock_with_partial_workloads_remains_in_progress(self) -> None:
        manifest = self._manifest(workloads=[])
        self._elapsed_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pending workloads", result.stdout)
        self.assertNotIn("schema soak complete", result.stdout)

    def test_elapsed_partial_workloads_do_not_allow_the_version_bump(self) -> None:
        self._elapsed_manifest(self._manifest(workloads=[]))
        self._bump(PLAN_SCHEMA_VERSION="1.0.0")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)
        self.assertIn(CONTRACTS, result.stderr)

    def test_workflow_run_must_contain_the_candidate_recording_commit(self) -> None:
        self._write_manifest(self._manifest())
        self.workflow_run["head_sha"] = self.candidate

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain the candidate recording commit", result.stderr)

    def test_workflow_run_must_use_the_ci_workflow_path(self) -> None:
        self._write_manifest(self._manifest())
        self.workflow_run["path"] = ".github/workflows/other.yml"

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must belong to this repository's CI workflow", result.stderr)

    def test_workflow_run_accepts_the_real_ci_path(self) -> None:
        self._write_manifest(self._manifest())

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reintroduced_candidate_uses_its_current_recording_sequence(self) -> None:
        self._write_manifest(self._manifest())
        old_recording = self._git("rev-parse", "HEAD")
        replacement = self._commit("candidate B", self.now - timedelta(days=2))
        self._write_manifest(self._manifest(candidate_commit=replacement))
        self._write_manifest(self._manifest())
        self.workflow_run["head_sha"] = old_recording

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain the candidate recording commit", result.stderr)

    def test_trusted_run_an_hour_short_of_21_days_is_in_progress(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=20, hours=23))

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak in progress", result.stdout)

    def test_github_run_timestamp_not_git_recording_timestamp_sets_clock(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=40))
        self.workflow_run["created_at"] = self._stamp(self.now - timedelta(days=20))

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak in progress", result.stdout)

    # -- version-only bump carve-out -------------------------------------

    def test_rejects_version_bump_before_soak_elapses(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=1))
        self._bump(PLAN_SCHEMA_VERSION="1.0.0")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)
        self.assertIn(CONTRACTS, result.stderr)

    def test_allows_version_only_bump_after_soak_elapses(self) -> None:
        # Stays inside the range the published patterns already accept, so this
        # isolates the version-only rule. Crossing to 1.0 additionally requires
        # the schemas to move; see OneZeroTransitionTests.
        self._elapsed_manifest()
        # 0.99.0 outranks every current constant, including proof-pack 0.11.0.
        self._bump(**{name: "0.99.0" for name in SCHEMA_VERSIONS})

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak complete", result.stdout)

    def test_rejects_constants_bumped_past_their_published_patterns(self) -> None:
        # A constants-only jump to 1.0 leaves every published pattern rejecting
        # the version the tree now declares -- the half-transition #5 forbids.
        self._elapsed_manifest()
        self._bump(**{name: "1.0.0" for name in SCHEMA_VERSIONS})

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("published schema patterns disagree", result.stderr)

    def test_rejects_version_bump_bundled_with_another_change(self) -> None:
        self._elapsed_manifest()
        (self.root / CONTRACTS).write_text(
            _contracts_source({**SCHEMA_VERSIONS, "PLAN_SCHEMA_VERSION": "0.99.0"})
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
        self.assertIn("must not be earlier than trusted soak start", result.stderr)

    def test_rejects_date_only_utc_timestamp_without_traceback(self) -> None:
        manifest = self._manifest()
        manifest["workloads"][0]["recorded_at_utc"] = "2026-07-22Z"
        self._write_manifest(manifest)

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be UTC", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class OneZeroTransitionTests(SoakHarness, unittest.TestCase):
    """Issue #5's 1.0 transition must be expressible inside the freeze set.

    The published patterns all reject `1.0.0`, so the bump is not a
    `contracts.py` edit in isolation: the schemas, the version-pinning tests,
    and a new 1.0 fixture have to move with it. These pin that the whole
    transition passes together and that nothing else rides along with it.
    """

    PINNED_TEST = "tests/test_versioning.py"

    def setUp(self) -> None:
        super().setUp()
        # The base fixture writes stub JSON; the transition rules only mean
        # something against schemas shaped like the published ones.
        for name in SCHEMA_FILES:
            (self.root / name).write_text(
                _published_schema(r"^0\.[0-9]+\.[0-9]+$"), encoding="utf-8"
            )
        (self.root / self.PINNED_TEST).write_text(
            'VALUE = 1\nPINNED = "0.4.0"\n', encoding="utf-8"
        )
        self.candidate = self._commit(
            "realistic candidate", self.now - timedelta(days=30)
        )

    def _bump_schemas(self, pattern: str = r"^1\.[0-9]+\.[0-9]+$") -> None:
        for name in SCHEMA_FILES:
            (self.root / name).write_text(_published_schema(pattern), encoding="utf-8")

    def _transition(self) -> None:
        self._bump(**{name: "1.0.0" for name in SCHEMA_VERSIONS})
        self._bump_schemas()
        (self.root / self.PINNED_TEST).write_text(
            'VALUE = 1\nPINNED = "1.0.0"\n', encoding="utf-8"
        )
        fixture = self.root / "tests/fixtures/compatibility/one-zero"
        fixture.mkdir(parents=True, exist_ok=True)
        (fixture / "proof.json").write_text(
            '{"schema_version": "1.0.0"}\n', encoding="utf-8"
        )

    def test_complete_transition_passes_after_the_soak(self) -> None:
        self._elapsed_manifest()
        self._transition()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema soak complete", result.stdout)

    def test_complete_transition_is_rejected_before_the_soak_elapses(self) -> None:
        self._write_manifest(self._manifest(), age=timedelta(days=1))
        self._transition()

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze set changed since candidate", result.stderr)

    def test_rejects_a_pattern_that_still_rejects_the_new_constant(self) -> None:
        """The half-transition that byte-identical schemas would hide."""
        self._elapsed_manifest()
        self._transition()
        (self.root / "schemas/plan-lock.schema.json").write_text(
            _published_schema(r"^0\.[0-9]+\.[0-9]+$"), encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("published schema patterns disagree", result.stderr)
        self.assertIn("PLAN_SCHEMA_VERSION 1.0.0", result.stderr)

    def test_rejects_a_schema_edited_beyond_its_version_pattern(self) -> None:
        self._elapsed_manifest()
        self._transition()
        schema = json.loads(
            (self.root / "schemas/plan-lock.schema.json").read_text(encoding="utf-8")
        )
        schema["title"] = "tampered"
        (self.root / "schemas/plan-lock.schema.json").write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("schemas/plan-lock.schema.json", result.stderr)

    def test_rejects_a_pinning_test_edited_beyond_its_semver_literals(self) -> None:
        self._elapsed_manifest()
        self._transition()
        (self.root / self.PINNED_TEST).write_text(
            'VALUE = 2\nPINNED = "1.0.0"\n', encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn(self.PINNED_TEST, result.stderr)

    def test_rejects_modifying_an_existing_immutable_fixture(self) -> None:
        self._elapsed_manifest()
        self._transition()
        (self.root / "tests/fixtures/compatibility/fixture.txt").write_text(
            "tampered\n", encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("tests/fixtures/compatibility/fixture.txt", result.stderr)

    def test_unchanged_tree_still_passes_a_completed_soak(self) -> None:
        self._elapsed_manifest()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)


class LedgerLockFreezeTests(SoakHarness, unittest.TestCase):
    """`locks.py` serializes every append to the frozen ledgers."""

    def test_changing_the_ledger_lock_resets_the_soak(self) -> None:
        self._write_manifest(self._manifest())
        (self.root / "src/agentflow/locks.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/agentflow/locks.py", result.stderr)


class FreezeSetContractTests(unittest.TestCase):
    def test_ledger_locking_paths_are_frozen(self) -> None:
        # execution.py, receipts.py, and review.py hold this lock while
        # appending frozen ledgers, so its semantics are load-bearing.
        for path in (
            "src/agentflow/locks.py",
            "tests/test_lease_locking.py",
            "tests/test_receipt_concurrency.py",
            "tests/test_lease_enforcement.py",
        ):
            self.assertIn(path, guard.FREEZE_PATHS)

    def test_every_published_schema_maps_to_a_load_bearing_constant(self) -> None:
        self.assertEqual(
            set(guard.SCHEMA_FILE_CONSTANTS.values()), set(guard.SCHEMA_CONSTANTS)
        )
        for path in guard.SCHEMA_FILE_CONSTANTS:
            self.assertIn(path, guard.FREEZE_PATHS)

    def test_published_patterns_accept_the_declared_constants(self) -> None:
        # The live repository must always satisfy the coherence rule the
        # transition window enforces, soak or no soak.
        import re

        constants = guard._schema_versions(
            (REPO_ROOT / guard.CONTRACTS_PATH).read_text(encoding="utf-8"),
            "contracts.py",
        )
        for path, constant in sorted(guard.SCHEMA_FILE_CONSTANTS.items()):
            pattern = guard._schema_version_pattern(
                (REPO_ROOT / path).read_bytes()
            )
            self.assertIsNotNone(pattern, path)
            self.assertRegex(constants[constant], re.compile(pattern), path)

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
        self.assertIn("actions: read", workflow)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)


if __name__ == "__main__":
    unittest.main()
