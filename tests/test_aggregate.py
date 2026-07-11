# tests/test_aggregate.py
import json
import subprocess
import sys
import unittest
from pathlib import Path

from agentflow import aggregate
from agentflow.aggregate import Source, parse_sources
from agentflow.artifacts import append_jsonl, write_json

ROOT = Path(__file__).resolve().parents[1]


class ParseSourcesTests(unittest.TestCase):
    def test_pairs_inputs_and_ids_with_default_label(self):
        sources, errors = parse_sources(["/tmp/a", "/tmp/b"], ["w1", "w2"], None)
        self.assertEqual(errors, [])
        self.assertEqual([s.source_id for s in sources], ["w1", "w2"])
        self.assertEqual(sources[0].prefix, "WTw1-")
        self.assertEqual(sources[0].label, "a")  # default = dir basename

    def test_rejects_bad_charset(self):
        _, errors = parse_sources(["/tmp/a"], ["W1!"], None)
        self.assertTrue(any("invalid --source-id" in e for e in errors))

    def test_rejects_trailing_newline_source_id(self):
        # re.match + "$" lets a trailing newline slip through (regressed as a
        # match/fullmatch bug); fullmatch closes it.
        _, errors = parse_sources(["/tmp/a"], ["w1\n"], None)
        self.assertTrue(any("invalid --source-id" in e for e in errors))

    def test_rejects_trailing_space_source_id(self):
        _, errors = parse_sources(["/tmp/a"], ["w1 "], None)
        self.assertTrue(any("invalid --source-id" in e for e in errors))

    def test_rejects_duplicate_source_id(self):
        _, errors = parse_sources(["/tmp/a", "/tmp/b"], ["w1", "w1"], None)
        self.assertTrue(any("duplicate --source-id" in e for e in errors))

    def test_rejects_count_mismatch(self):
        _, errors = parse_sources(["/tmp/a", "/tmp/b"], ["w1"], None)
        self.assertTrue(any("must equal" in e for e in errors))

    def test_rejects_label_count_mismatch(self):
        _, errors = parse_sources(["/tmp/a", "/tmp/b"], ["w1", "w2"], ["only-one"])
        self.assertTrue(any("--label count" in e and "must equal" in e for e in errors))

    def test_uses_supplied_labels(self):
        sources, errors = parse_sources(["/tmp/a", "/tmp/b"], ["w1", "w2"], ["alpha", "beta"])
        self.assertEqual(errors, [])
        self.assertEqual([s.label for s in sources], ["alpha", "beta"])


def build_tree(root: Path, *, steps, files=None, contract=None):
    """Write a minimal stub input worktree at `root` (intentionally not a full
    schema shape). The #110 dry-run analysis reads bytes/rows, not schemas, so
    these stubs stay minimal; schema_version strings only track current
    constants (plan-lock 0.3.0, execution-contract 0.3.0) to avoid drift, they
    are not validated here.

    steps: list of step_ids that reach a `completed` event with attempt A1.
    files: list of (path, sha256) file-receipt rows (relative to root).
    contract: optional execution-contract dict (defaults to a shared stub).
    """
    agent = root / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    write_json(agent / "plan.lock.json", {"schema_version": "0.3.0", "objective": "o", "steps": []})
    write_json(agent / "execution.contract.json", contract or {"schema_version": "0.3.0", "command_policy": {"receipt_store": "by_attempt"}})
    for step_id in steps:
        append_jsonl(agent / "step-runs.jsonl", {"schema_version": "0.4.0", "event": "claimed", "step_id": step_id, "attempt_id": "A1", "recorded_at": "2026-07-04T00:00:00+00:00"})
        append_jsonl(agent / "step-runs.jsonl", {"schema_version": "0.4.0", "event": "completed", "step_id": step_id, "attempt_id": "A1", "recorded_at": "2026-07-04T00:01:00+00:00"})
    for path, sha in (files or []):
        append_jsonl(agent / "file-receipts.jsonl", {"schema_version": "0.3.1", "id": "FR1", "step_id": steps[0], "attempt_id": "A1", "path": path, "change_kind": "modified", "before_git_blob": None, "after_sha256": sha, "recorded_at": "2026-07-04T00:01:00+00:00"})
    return root


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo_with_worktrees(tmp):
    """Git-backed two-worktree fixture: `out` repo with worktrees `a` (step P1,
    src/x.py) and `b` (step P2, src/y.py). Shared by WriteCanonicalTests and
    AggregationProvenanceEmissionTests.
    """
    from agentflow.receipts import sha256_path
    out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
    _git(out, "init", "-q"); _git(out, "config", "user.email", "t@t"); _git(out, "config", "user.name", "t")
    (out / "seed.txt").write_text("seed", encoding="utf-8"); _git(out, "add", "-A"); _git(out, "commit", "-qm", "seed")
    a = Path(tmp) / "a"; b = Path(tmp) / "b"
    _git(out, "worktree", "add", "-q", "-b", "wa", str(a), "HEAD")
    _git(out, "worktree", "add", "-q", "-b", "wb", str(b), "HEAD")
    fx = out / "src/x.py"; fx.write_text("x", encoding="utf-8")
    fy = out / "src/y.py"; fy.write_text("y", encoding="utf-8")
    build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(fx))])
    build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(fy))])
    return out, a, b


def _repo_with_one_worktree(tmp):
    """Single-worktree variant of `_repo_with_worktrees`, for source_count == 1
    coverage."""
    from agentflow.receipts import sha256_path
    out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
    _git(out, "init", "-q"); _git(out, "config", "user.email", "t@t"); _git(out, "config", "user.name", "t")
    (out / "seed.txt").write_text("seed", encoding="utf-8"); _git(out, "add", "-A"); _git(out, "commit", "-qm", "seed")
    a = Path(tmp) / "a"
    _git(out, "worktree", "add", "-q", "-b", "wa", str(a), "HEAD")
    fx = out / "src/x.py"; fx.write_text("x", encoding="utf-8")
    build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(fx))])
    return out, a


class FixtureSmokeTests(unittest.TestCase):
    def test_build_tree_writes_ledgers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = build_tree(Path(tmp), steps=["P1"])
            self.assertTrue((root / ".agent/step-runs.jsonl").exists())


class MustMatchTests(unittest.TestCase):
    def _sources(self, tmp, contract_a, contract_b):
        a = build_tree(Path(tmp) / "a", steps=["P1"], contract=contract_a)
        b = build_tree(Path(tmp) / "b", steps=["P2"], contract=contract_b)
        return [Source(a, "w1", "a"), Source(b, "w2", "b")]

    def test_mismatched_contract_is_collision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sources = self._sources(
                tmp,
                {"schema_version": "0.3.0", "command_policy": {"receipt_store": "by_attempt"}},
                {"schema_version": "0.3.0", "command_policy": {"receipt_store": "content_addressed"}},
            )
            cols = aggregate._must_match_collisions(sources)
            self.assertTrue(any(c["kind"] == "must_match_mismatch" and c["artifact"] == "execution-contract" for c in cols))

    def test_identical_singletons_no_collision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            same = {"schema_version": "0.3.0", "command_policy": {"receipt_store": "by_attempt"}}
            sources = self._sources(tmp, same, dict(same))
            self.assertEqual(aggregate._must_match_collisions(sources), [])


class OverlapTests(unittest.TestCase):
    def test_same_step_with_different_rows_collides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            b = build_tree(Path(tmp) / "b", steps=["P1"])
            path = b / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[0]["agent_id"] = "different"
            path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            cols = aggregate._step_overlap_collisions(sources)
            self.assertTrue(any(c["kind"] == "step_overlap" and c["step_id"] == "P1" for c in cols))

    def test_same_step_with_byte_identical_rows_is_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            b = build_tree(Path(tmp) / "b", steps=["P1"])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            self.assertEqual(aggregate._step_overlap_collisions(sources), [])

    def test_overlapping_file_receipt_paths_collide(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            b = build_tree(Path(tmp) / "b", steps=["P2"], files=[("src/x.py", "b" * 64)])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            cols = aggregate._file_overlap_collisions(sources)
            self.assertTrue(any(c["kind"] == "file_overlap" and c["path"] == "src/x.py" for c in cols))

    def test_shared_baseline_file_receipt_is_ok(self):
        # Both trees inherit the same prerequisite step P0 (identical receipt row) => not a collision.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P0"], files=[("src/shared.py", "a" * 64)])
            b = build_tree(Path(tmp) / "b", steps=["P0"], files=[("src/shared.py", "a" * 64)])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            self.assertEqual(aggregate._file_overlap_collisions(sources), [])

    def test_disjoint_steps_and_files_no_collision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            b = build_tree(Path(tmp) / "b", steps=["P2"], files=[("src/y.py", "b" * 64)])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            self.assertEqual(aggregate._step_overlap_collisions(sources), [])
            self.assertEqual(aggregate._file_overlap_collisions(sources), [])


class ConcatDupTests(unittest.TestCase):
    def _two(self, tmp, evid_a, evid_b):
        a = build_tree(Path(tmp) / "a", steps=["P1"])
        b = build_tree(Path(tmp) / "b", steps=["P2"])
        append_jsonl(a / ".agent/evidence.jsonl", evid_a)
        append_jsonl(b / ".agent/evidence.jsonl", evid_b)
        return [Source(a, "w1", "a"), Source(b, "w2", "b")]

    def test_same_id_different_row_collides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sources = self._two(
                tmp,
                {"id": "E1", "claim": "from a"},
                {"id": "E1", "claim": "from b"},
            )
            cols = aggregate._concat_dup_collisions(sources)
            self.assertTrue(any(c["kind"] == "concat_dup_mismatch" and c["id"] == "E1" for c in cols))

    def test_same_id_identical_row_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            row = {"id": "E1", "claim": "shared"}
            sources = self._two(tmp, dict(row), dict(row))
            self.assertEqual(aggregate._concat_dup_collisions(sources), [])


class ReceiptFileTests(unittest.TestCase):
    def _tree_with_cr(self, root, stdout_text, recorded_sha):
        build_tree(root, steps=["P1"])
        rdir = root / ".agent/receipts/A1"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "CR1.stdout.txt").write_text(stdout_text, encoding="utf-8")
        append_jsonl(root / ".agent/command-receipts.jsonl", {
            "schema_version": "0.3.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
            "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
            "finished_at": "t", "exit_code": 0, "truncated": False,
            "stdout_path": ".agent/receipts/A1/CR1.stdout.txt", "stdout_sha256": recorded_sha,
            "stderr_path": None, "stderr_sha256": None,
        })
        return root

    def test_hash_mismatch_collides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree_with_cr(Path(tmp) / "a", "hello", "0" * 64)  # wrong sha
            cols = aggregate._receipt_file_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "receipt_hash_mismatch" for c in cols))

    def test_missing_file_collides(self):
        import tempfile
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree_with_cr(Path(tmp) / "a", "hello", "0" * 64)
            (root / ".agent/receipts/A1/CR1.stdout.txt").unlink()
            cols = aggregate._receipt_file_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "receipt_file_missing" for c in cols))

    def test_matching_hash_ok(self):
        import tempfile
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            rdir = root / ".agent/receipts/A1"; rdir.mkdir(parents=True, exist_ok=True)
            f = rdir / "CR1.stdout.txt"; f.write_text("hello", encoding="utf-8")
            append_jsonl(root / ".agent/command-receipts.jsonl", {
                "schema_version": "0.3.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
                "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
                "finished_at": "t", "exit_code": 0, "truncated": False,
                "stdout_path": ".agent/receipts/A1/CR1.stdout.txt", "stdout_sha256": sha256_path(f),
                "stderr_path": None, "stderr_sha256": None,
            })
            self.assertEqual(aggregate._receipt_file_collisions([Source(root, "w1", "a")]), [])


class ReviewArtifactTests(unittest.TestCase):
    REVIEW_ID = "RR-20260704T000000Z-abcdef12"

    def _tree_with_review(self, root: Path, *, artifact_sha=None):
        from agentflow.review import sha256_file
        build_tree(root, steps=["P1"])
        state = root / ".agent/reviews" / self.REVIEW_ID
        state.mkdir(parents=True, exist_ok=True)
        manifest = state / "manifest.json"
        artifact = state / "findings.json"
        manifest.write_text('{"ok": true}', encoding="utf-8")
        artifact.write_text('{"findings": []}', encoding="utf-8")
        append_jsonl(root / ".agent/review-runs.jsonl", {
            "schema_version": "0.4.0",
            "review_run_id": self.REVIEW_ID,
            "recorded_at": "2026-07-04T00:00:00+00:00",
            "state_dir": ".agent/reviews/" + self.REVIEW_ID,
            "manifest_path": ".agent/reviews/" + self.REVIEW_ID + "/manifest.json",
            "manifest_sha256": sha256_file(manifest),
            "gate_status": "pass",
            "artifacts": [{
                "path": ".agent/reviews/" + self.REVIEW_ID + "/findings.json",
                "sha256": artifact_sha or sha256_file(artifact),
            }],
        })
        return root

    def test_missing_review_manifest_collides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree_with_review(Path(tmp) / "a")
            (root / ".agent/reviews" / self.REVIEW_ID / "manifest.json").unlink()
            cols = aggregate._review_artifact_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "review_manifest_missing" for c in cols))

    def test_review_artifact_hash_mismatch_collides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree_with_review(Path(tmp) / "a", artifact_sha="0" * 64)
            cols = aggregate._review_artifact_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "review_artifact_hash_mismatch" for c in cols))

    def test_well_formed_review_artifacts_no_collision(self):
        # Fail-closed safety: a fully consistent review run (manifest + artifact
        # present, hashes matching) must NOT report a collision, or it would
        # wrongly block the #111 write path.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree_with_review(Path(tmp) / "a")
            self.assertEqual(aggregate._review_artifact_collisions([Source(root, "w1", "a")]), [])

    def test_duplicate_review_run_id_differing_collides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = self._tree_with_review(Path(tmp) / "a")
            b = self._tree_with_review(Path(tmp) / "b")
            path = b / ".agent/review-runs.jsonl"  # same review_run_id, one differing field
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[0]["gate_status"] = "fail"
            path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
            cols = aggregate._review_dup_collisions([Source(a, "w1", "a"), Source(b, "w2", "b")])
            self.assertTrue(any(c["kind"] == "review_dup_id" and c["review_run_id"] == self.REVIEW_ID for c in cols))

    def test_duplicate_review_run_id_byte_identical_is_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = self._tree_with_review(Path(tmp) / "a")
            b = self._tree_with_review(Path(tmp) / "b")
            self.assertEqual(aggregate._review_dup_collisions([Source(a, "w1", "a"), Source(b, "w2", "b")]), [])


class BaseCommitTests(unittest.TestCase):
    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _new_repo(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@t")
        self._git(root, "config", "user.name", "t")
        (root / "seed.txt").write_text("seed", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "seed")
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    def _add_worktree(self, repo: Path, path: Path, branch: str):
        self._git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")

    def test_explicit_base_agrees_no_collision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            base = self._new_repo(out)
            a = Path(tmp) / "a"; b = Path(tmp) / "b"
            self._add_worktree(out, a, "worker-a")
            self._add_worktree(out, b, "worker-b")
            build_tree(a, steps=["P1"])
            build_tree(b, steps=["P2"])
            bases = aggregate._source_bases([Source(a, "w1", "a"), Source(b, "w2", "b")], "HEAD", out)
            self.assertEqual(bases, {"w1": base, "w2": base})
            cols = aggregate._base_commit_collisions(bases)
            self.assertEqual(cols, [])

    def test_mismatched_bases_collide(self):
        bases = {"w1": "a" * 40, "w2": "b" * 40}
        cols = aggregate._base_commit_collisions(bases)
        self.assertTrue(any(c["kind"] == "base_commit_mismatch" for c in cols))

    def test_unresolved_base_is_collision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"  # not a git repo
            build_tree(a, steps=["P1"])
            bases = aggregate._source_bases([Source(a, "w1", "a")], "HEAD", a)
            cols = aggregate._base_commit_collisions(bases)
            self.assertTrue(any(c["kind"] == "base_commit_unresolved" for c in cols))


class PreconditionTests(unittest.TestCase):
    def test_missing_output_file_is_collision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            out = Path(tmp) / "out"; out.mkdir()
            cols = aggregate._precondition_collisions([Source(a, "w1", "a")], out)
            self.assertTrue(any(c["kind"] == "precondition_missing" and c["path"] == "src/x.py" for c in cols))

    def test_hash_mismatch_is_collision(self):
        import tempfile
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            (out / "src/x.py").write_text("real", encoding="utf-8")
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "b" * 64)])  # receipt sha != file
            cols = aggregate._precondition_collisions([Source(a, "w1", "a")], out)
            self.assertTrue(any(c["kind"] == "precondition_hash_mismatch" for c in cols))

    def test_matching_output_ok(self):
        import tempfile
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            f = out / "src/x.py"; f.write_text("real", encoding="utf-8")
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", sha256_path(f))])
            self.assertEqual(aggregate._precondition_collisions([Source(a, "w1", "a")], out), [])

    def test_deleted_receipt_requires_absent_output_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            (out / "src/x.py").write_text("still here", encoding="utf-8")
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/file-receipts.jsonl", {
                "schema_version": "0.3.1", "id": "FR1", "step_id": "P1", "attempt_id": "A1",
                "path": "src/x.py", "change_kind": "deleted", "before_git_blob": "abc",
                "after_sha256": None, "recorded_at": "2026-07-04T00:01:00+00:00",
            })
            cols = aggregate._precondition_collisions([Source(a, "w1", "a")], out)
            self.assertTrue(any(c["kind"] == "precondition_deleted_present" for c in cols))


class AnalyzeTests(unittest.TestCase):
    def test_clean_two_tree_report(self):
        import tempfile
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            fx = out / "src/x.py"; fx.write_text("x", encoding="utf-8")
            fy = out / "src/y.py"; fy.write_text("y", encoding="utf-8")
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", sha256_path(fx))])
            b = build_tree(Path(tmp) / "b", steps=["P2"], files=[("src/y.py", sha256_path(fy))])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            report = aggregate.analyze(sources, out, base_ref="HEAD")
            # base_ref HEAD on non-git temp dirs => unresolved base is the only collision class here
            non_base = [c for c in report["collisions"] if not c["kind"].startswith("base_commit")]
            self.assertEqual(non_base, [])
            self.assertEqual(report["planned"]["w1"]["attempt_ids"], {"A1": "WTw1-A1"})
            self.assertEqual(report["planned"]["w2"]["file_receipt_ids"], {"FR1": "WTw2-FR1"})
            self.assertEqual({s["source_id"] for s in report["sources"]}, {"w1", "w2"})

    def test_collision_sets_status(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            b = build_tree(Path(tmp) / "b", steps=["P1"])
            path = b / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[0]["agent_id"] = "different"
            path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
            report = aggregate.analyze([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(report["status"], "collision")
            self.assertTrue(any(c["kind"] == "step_overlap" for c in report["collisions"]))

    def test_run_scope_verification_ids_are_not_planned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/verification-runs.jsonl", {
                "schema_version": "0.3.0", "id": "VR1", "scope": "run",
                "recorded_at": "2026-07-04T00:00:00+00:00", "findings": [],
            })
            append_jsonl(a / ".agent/verification-runs.jsonl", {
                "schema_version": "0.3.0", "id": "VR2", "scope": "step", "step_id": "P1", "attempt_id": "A1",
                "recorded_at": "2026-07-04T00:00:00+00:00", "findings": [],
            })
            report = aggregate.analyze([Source(a, "w1", "a")], out, base_ref="HEAD")
            self.assertEqual(report["planned"]["w1"]["verification_run_ids"], {"VR2": "WTw1-VR2"})


class CliAggregateTests(unittest.TestCase):
    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _new_repo(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@t")
        self._git(root, "config", "user.name", "t")
        (root / "seed.txt").write_text("seed", encoding="utf-8")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "seed")

    def _add_worktree(self, repo: Path, path: Path, branch: str):
        self._git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")

    def _run(self, cwd, *args):
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, "-m", "agentflow", *args],
            cwd=str(cwd), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_dry_run_json_reports_planned_rewrites(self):
        import tempfile
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            self._new_repo(out)
            a = Path(tmp) / "a"; b = Path(tmp) / "b"
            self._add_worktree(out, a, "worker-a")
            self._add_worktree(out, b, "worker-b")
            fx = out / "src/x.py"; fx.write_text("x", encoding="utf-8")
            fy = out / "src/y.py"; fy.write_text("y", encoding="utf-8")
            build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(fx))])
            build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(fy))])
            proc = self._run(
                tmp, "aggregate-ledgers",
                "--input", str(a), "--source-id", "w1",
                "--input", str(b), "--source-id", "w2",
                "--output", str(out), "--base", "HEAD", "--dry-run", "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["collisions"], [])
            self.assertEqual(payload["planned"]["w1"]["attempt_ids"], {"A1": "WTw1-A1"})

    def test_missing_dry_run_flag_writes_or_collides(self):
        # #111: omitting --dry-run now takes the write path. These stub dirs
        # are not git repos, so base_ref "HEAD" is unresolved -> a
        # base_commit_unresolved collision, and nothing is written.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build_tree(Path(tmp) / "a", steps=["P1"])
            proc = self._run(tmp, "aggregate-ledgers", "--input", str(Path(tmp) / "a"), "--source-id", "w1", "--output", str(tmp))
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertFalse((Path(tmp) / ".agent").exists())

    def test_duplicate_source_id_exits_2(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build_tree(Path(tmp) / "a", steps=["P1"])
            build_tree(Path(tmp) / "b", steps=["P2"])
            proc = self._run(
                tmp, "aggregate-ledgers",
                "--input", str(Path(tmp) / "a"), "--source-id", "w1",
                "--input", str(Path(tmp) / "b"), "--source-id", "w1",
                "--output", str(tmp), "--dry-run",
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("duplicate --source-id", proc.stderr)

    def test_collision_human_output_exits_1(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            self._new_repo(out)
            a = Path(tmp) / "a"; b = Path(tmp) / "b"
            self._add_worktree(out, a, "worker-a")
            self._add_worktree(out, b, "worker-b")
            build_tree(a, steps=["P1"])
            build_tree(b, steps=["P1"])
            path = b / ".agent/step-runs.jsonl"  # same step P1, one differing row => step_overlap
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows[0]["agent_id"] = "different"
            path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
            proc = self._run(
                tmp, "aggregate-ledgers",
                "--input", str(a), "--source-id", "w1",
                "--input", str(b), "--source-id", "w2",
                "--output", str(out), "--base", "HEAD", "--dry-run",
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("aggregate-ledgers dry-run: collision", proc.stdout)
            self.assertIn("collision: step_overlap", proc.stdout)


class HardeningTests(unittest.TestCase):
    """Fail-closed hardening from the /code-review pass (C1 traversal, I3/I4)."""

    def _symlink_or_skip(self, target: Path, link: Path):
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")

    def _cr_row(self, stdout_path, sha="0" * 64):
        return {
            "schema_version": "0.3.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
            "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
            "finished_at": "t", "exit_code": 0, "truncated": False,
            "stdout_path": stdout_path, "stdout_sha256": sha,
            "stderr_path": None, "stderr_sha256": None,
        }

    def test_receipt_traversal_path_is_unsafe_not_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(root / ".agent/command-receipts.jsonl", self._cr_row("../../../../etc/passwd"))
            cols = aggregate._receipt_file_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "stdout_path" for c in cols))
            # fail-closed: no missing/hash collision was produced by reading an escaped path
            self.assertFalse(any(c["kind"] in ("receipt_file_missing", "receipt_hash_mismatch") for c in cols))

    def test_receipt_absolute_path_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(root / ".agent/command-receipts.jsonl", self._cr_row("/etc/hosts"))
            cols = aggregate._receipt_file_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "unsafe_path" for c in cols))

    def test_receipt_symlink_escape_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.txt"; outside.write_text("secret", encoding="utf-8")
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            rdir = root / ".agent/receipts/A1"; rdir.mkdir(parents=True, exist_ok=True)
            self._symlink_or_skip(outside, rdir / "CR1.stdout.txt")
            append_jsonl(root / ".agent/command-receipts.jsonl", self._cr_row(".agent/receipts/A1/CR1.stdout.txt"))
            cols = aggregate._receipt_file_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "stdout_path" for c in cols))

    def test_review_manifest_traversal_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(root / ".agent/review-runs.jsonl", {
                "schema_version": "0.4.0", "review_run_id": "RR-x",
                "recorded_at": "2026-07-04T00:00:00+00:00",
                "manifest_path": "../../../../etc/passwd", "manifest_sha256": "0" * 64,
                "gate_status": "pass", "artifacts": [{"path": "/etc/hosts", "sha256": "0" * 64}],
            })
            cols = aggregate._review_artifact_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "manifest_path" for c in cols))
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "artifact_path" for c in cols))

    def test_review_manifest_symlink_escape_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.json"; outside.write_text("{}", encoding="utf-8")
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            state = root / ".agent/reviews/RR-x"; state.mkdir(parents=True, exist_ok=True)
            self._symlink_or_skip(outside, state / "manifest.json")
            append_jsonl(root / ".agent/review-runs.jsonl", {
                "schema_version": "0.4.0", "review_run_id": "RR-x",
                "recorded_at": "2026-07-04T00:00:00+00:00",
                "manifest_path": ".agent/reviews/RR-x/manifest.json", "manifest_sha256": "0" * 64,
                "gate_status": "pass", "artifacts": [],
            })
            cols = aggregate._review_artifact_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "manifest_path" for c in cols))

    def test_review_artifact_symlink_escape_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.json"; outside.write_text("{}", encoding="utf-8")
            root = build_tree(Path(tmp) / "a", steps=["P1"])
            state = root / ".agent/reviews/RR-x"; state.mkdir(parents=True, exist_ok=True)
            manifest = state / "manifest.json"; manifest.write_text("{}", encoding="utf-8")
            self._symlink_or_skip(outside, state / "artifact.json")
            from agentflow.review import sha256_file
            append_jsonl(root / ".agent/review-runs.jsonl", {
                "schema_version": "0.4.0", "review_run_id": "RR-x",
                "recorded_at": "2026-07-04T00:00:00+00:00",
                "manifest_path": ".agent/reviews/RR-x/manifest.json", "manifest_sha256": sha256_file(manifest),
                "gate_status": "pass", "artifacts": [{"path": ".agent/reviews/RR-x/artifact.json", "sha256": "0" * 64}],
            })
            cols = aggregate._review_artifact_collisions([Source(root, "w1", "a")])
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "artifact_path" for c in cols))

    def test_precondition_traversal_path_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("../escape.py", "a" * 64)])
            cols = aggregate._precondition_collisions([Source(a, "w1", "a")], out)
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "file_receipt_path" for c in cols))
            self.assertFalse(any(c["kind"].startswith("precondition_") for c in cols))

    def test_precondition_output_symlink_escape_is_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.py"; outside.write_text("secret", encoding="utf-8")
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            self._symlink_or_skip(outside, out / "src/x.py")
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            cols = aggregate._precondition_collisions([Source(a, "w1", "a")], out)
            self.assertTrue(any(c["kind"] == "unsafe_path" and c["field"] == "file_receipt_path" for c in cols))

    def test_precondition_non_deleted_null_after_is_malformed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            (out / "src/x.py").write_text("real", encoding="utf-8")
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/file-receipts.jsonl", {
                "schema_version": "0.3.1", "id": "FR1", "step_id": "P1", "attempt_id": "A1",
                "path": "src/x.py", "change_kind": "modified", "before_git_blob": None,
                "after_sha256": None, "recorded_at": "2026-07-04T00:01:00+00:00",
            })
            cols = aggregate._precondition_collisions([Source(a, "w1", "a")], out)
            self.assertTrue(any(c["kind"] == "precondition_malformed" and c["path"] == "src/x.py" for c in cols))

    def test_malformed_ledger_fails_closed_in_analyze(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            (a / ".agent/evidence.jsonl").write_text("{ this is not json\n", encoding="utf-8")
            report = aggregate.analyze([Source(a, "w1", "a")], out, base_ref="HEAD")
            self.assertEqual(report["status"], "collision")
            self.assertTrue(any(c["kind"] == "malformed_ledger" and c["ledger"] == "evidence" for c in report["collisions"]))
            self.assertEqual(report["planned"], {})
            # source summaries still present (built without reading the corrupt ledger)
            self.assertEqual({s["source_id"] for s in report["sources"]}, {"w1"})

    def test_non_object_jsonl_row_fails_closed_in_analyze(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            (a / ".agent/evidence.jsonl").write_text("[]\n", encoding="utf-8")
            report = aggregate.analyze([Source(a, "w1", "a")], out, base_ref="HEAD")
            self.assertEqual(report["status"], "collision")
            self.assertTrue(any(c["kind"] == "malformed_ledger" and c["ledger"] == "evidence" for c in report["collisions"]))

    def test_file_receipt_non_string_recorded_at_fails_closed_in_analyze(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/file-receipts.jsonl", {
                "schema_version": "0.3.1", "id": "FR2", "step_id": "P1", "attempt_id": "A1",
                "path": "src/x.py", "change_kind": "modified", "before_git_blob": None,
                "after_sha256": "a" * 64, "recorded_at": None,
            })
            report = aggregate.analyze([Source(a, "w1", "a")], out, base_ref="HEAD")
            self.assertEqual(report["status"], "collision")
            self.assertTrue(any(c["kind"] == "malformed_ledger" and c["ledger"] == "file-receipts" for c in report["collisions"]))


class SchemaBumpTests(unittest.TestCase):
    def test_execution_ledger_versions_bumped_for_namespacing(self):
        from agentflow import contracts
        self.assertEqual(contracts.STEP_RUNS_SCHEMA_VERSION, "0.5.0")
        self.assertEqual(contracts.COMMAND_RECEIPTS_SCHEMA_VERSION, "0.4.0")
        self.assertEqual(contracts.FILE_RECEIPTS_SCHEMA_VERSION, "0.4.0")
        self.assertEqual(contracts.VERIFICATION_RUNS_SCHEMA_VERSION, "0.4.0")

    def test_pre_bump_rows_still_validate_backward_compat(self):
        from agentflow import contracts
        from agentflow.versioning import is_schema_version_compatible
        self.assertTrue(is_schema_version_compatible("0.4.0", contracts.STEP_RUNS_SCHEMA_VERSION))
        self.assertTrue(is_schema_version_compatible("0.3.0", contracts.COMMAND_RECEIPTS_SCHEMA_VERSION))
        self.assertTrue(is_schema_version_compatible("0.3.1", contracts.FILE_RECEIPTS_SCHEMA_VERSION))
        self.assertTrue(is_schema_version_compatible("0.3.0", contracts.VERIFICATION_RUNS_SCHEMA_VERSION))


class WritePrimitiveTests(unittest.TestCase):
    def test_baseline_canon_flags_rows_in_two_sources(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P0", "P1"])
            b = build_tree(Path(tmp) / "b", steps=["P0", "P2"])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            rel = ".agent/step-runs.jsonl"
            baseline = aggregate._baseline_canon(sources, rel)
            p0_rows = [aggregate._canon(r) for r in aggregate.read_jsonl(a / rel) if r["step_id"] == "P0"]
            self.assertTrue(all(c in baseline for c in p0_rows))
            p1_rows = [aggregate._canon(r) for r in aggregate.read_jsonl(a / rel) if r["step_id"] == "P1"]
            self.assertTrue(all(c not in baseline for c in p1_rows))

    def test_attempt_map_namespaces_only_tree_local_attempts(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"; b = Path(tmp) / "b"
            build_tree(a, steps=["P0"]); build_tree(b, steps=["P0"])
            for step, attempt in (("P1", "A2"),):
                append_jsonl(a / ".agent/step-runs.jsonl", {"schema_version": "0.5.0", "event": "claimed", "step_id": step, "attempt_id": attempt, "recorded_at": "2026-07-05T00:02:00+00:00"})
                append_jsonl(a / ".agent/step-runs.jsonl", {"schema_version": "0.5.0", "event": "completed", "step_id": step, "attempt_id": attempt, "recorded_at": "2026-07-05T00:03:00+00:00"})
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amap = aggregate._attempt_map(Source(a, "w1", "a"), baseline)
            self.assertEqual(amap, {"A2": "WTw1-A2"})

    def test_receipt_store_reads_contract_policy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            self.assertEqual(aggregate._receipt_store([Source(a, "w1", "a")]), "by_attempt")
            ca = build_tree(Path(tmp) / "b", steps=["P1"], contract={"schema_version": "0.3.0", "command_policy": {"receipt_store": "content_addressed"}})
            self.assertEqual(aggregate._receipt_store([Source(ca, "w2", "b")]), "content_addressed")


class RewriteMergeTests(unittest.TestCase):
    def _two(self, tmp):
        from agentflow.artifacts import append_jsonl
        a = Path(tmp) / "a"; b = Path(tmp) / "b"
        build_tree(a, steps=["P0"]); build_tree(b, steps=["P0"])
        for root, step in ((a, "P1"), (b, "P2")):
            append_jsonl(root / ".agent/step-runs.jsonl", {"schema_version": "0.5.0", "event": "claimed", "step_id": step, "attempt_id": "A2", "recorded_at": "2026-07-05T00:02:00+00:00"})
            append_jsonl(root / ".agent/step-runs.jsonl", {"schema_version": "0.5.0", "event": "completed", "step_id": step, "attempt_id": "A2", "recorded_at": "2026-07-05T00:03:00+00:00"})
        return [Source(a, "w1", "a"), Source(b, "w2", "b")]

    def test_step_runs_dedupe_baseline_and_namespace_local(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sources = self._two(tmp)
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows = aggregate._merge_step_runs(sources, baseline, amaps)
            steps = [(r["step_id"], r["attempt_id"], r["event"]) for r in rows]
            self.assertEqual(steps.count(("P0", "A1", "completed")), 1)
            self.assertIn(("P1", "WTw1-A2", "completed"), steps)
            self.assertIn(("P2", "WTw2-A2", "completed"), steps)
            self.assertLess(steps.index(("P1", "WTw1-A2", "claimed")), steps.index(("P1", "WTw1-A2", "completed")))

    def test_step_runs_namespaces_attempt_cross_refs(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/step-runs.jsonl", {"schema_version": "0.5.0", "event": "claimed", "step_id": "P1", "attempt_id": "A2", "amends_attempt": "A1", "superseded_by": None, "recorded_at": "2026-07-05T00:05:00+00:00"})
            sources = [Source(a, "w1", "a")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows = aggregate._merge_step_runs(sources, baseline, amaps)
            amend = [r for r in rows if r.get("amends_attempt")][0]
            self.assertEqual(amend["attempt_id"], "WTw1-A2")
            self.assertEqual(amend["amends_attempt"], "WTw1-A1")

    def test_verification_runs_drops_run_scope_and_namespaces_step_scope(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/verification-runs.jsonl", {"schema_version": "0.4.0", "id": "VR1", "scope": "run", "recorded_at": "2026-07-05T00:00:00+00:00", "findings": []})
            append_jsonl(a / ".agent/verification-runs.jsonl", {"schema_version": "0.4.0", "id": "VR2", "scope": "step", "step_id": "P1", "attempt_id": "A1", "recorded_at": "2026-07-05T00:00:00+00:00", "findings": []})
            sources = [Source(a, "w1", "a")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows = aggregate._merge_verification_runs(sources, amaps)
            ids = [r["id"] for r in rows]
            self.assertNotIn("VR1", ids)
            self.assertIn("WTw1-VR2", ids)
            self.assertEqual(rows[0]["attempt_id"], "WTw1-A1")

    def test_file_receipts_dedupe_baseline_and_namespace_local(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P0"], files=[("src/shared.py", "a" * 64)])
            b = build_tree(Path(tmp) / "b", steps=["P0"], files=[("src/shared.py", "a" * 64)])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows = aggregate._merge_file_receipts(sources, amaps)
            shared = [r for r in rows if r["path"] == "src/shared.py"]
            self.assertEqual(len(shared), 1)
            self.assertEqual(shared[0]["id"], "FR1")


class CommandReceiptMergeTests(unittest.TestCase):
    def _tree_with_cr(self, root):
        from agentflow.receipts import sha256_path
        from agentflow.artifacts import append_jsonl
        build_tree(root, steps=["P1"], contract={"schema_version": "0.3.0", "command_policy": {"receipt_store": "by_attempt"}})
        rdir = root / ".agent/receipts/A1"; rdir.mkdir(parents=True, exist_ok=True)
        f = rdir / "CR1.stdout.txt"; f.write_text("hello", encoding="utf-8")
        append_jsonl(root / ".agent/command-receipts.jsonl", {
            "schema_version": "0.4.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
            "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
            "finished_at": "t", "exit_code": 0, "truncated": False,
            "stdout_path": ".agent/receipts/A1/CR1.stdout.txt", "stdout_sha256": sha256_path(f),
            "stderr_path": None, "stderr_sha256": None,
        })
        return root

    def test_by_attempt_namespaces_ids_and_rewrites_paths(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = self._tree_with_cr(Path(tmp) / "a")
            sources = [Source(a, "w1", "a")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows, copies = aggregate._merge_command_receipts(sources, amaps, "by_attempt")
            self.assertEqual(rows[0]["id"], "WTw1-CR1")
            self.assertEqual(rows[0]["attempt_id"], "WTw1-A1")
            self.assertEqual(rows[0]["stdout_path"], ".agent/receipts/WTw1-A1/WTw1-CR1.stdout.txt")
            self.assertEqual(len(copies), 1)
            src_abs, dst_rel, expected = copies[0]
            self.assertEqual(dst_rel, ".agent/receipts/WTw1-A1/WTw1-CR1.stdout.txt")
            self.assertTrue(src_abs.exists())
            self.assertEqual(expected, rows[0]["stdout_sha256"])

    def test_content_addressed_copies_verbatim_no_path_rewrite(self):
        import tempfile
        import hashlib
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            root = build_tree(Path(tmp) / "a", steps=["P1"], contract={"schema_version": "0.3.0", "command_policy": {"receipt_store": "content_addressed"}})
            data = b"payload"
            digest = hashlib.sha256(data).hexdigest()
            cpath = root / ".agent/receipts/sha256" / digest[:2] / digest
            cpath.parent.mkdir(parents=True, exist_ok=True); cpath.write_bytes(data)
            rel = ".agent/receipts/sha256/" + digest[:2] + "/" + digest
            append_jsonl(root / ".agent/command-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
                "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
                "finished_at": "t", "exit_code": 0, "truncated": False,
                "stdout_path": rel, "stdout_sha256": digest, "stderr_path": None, "stderr_sha256": None,
            })
            sources = [Source(root, "w1", "a")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows, copies = aggregate._merge_command_receipts(sources, amaps, "content_addressed")
            self.assertEqual(rows[0]["id"], "WTw1-CR1")
            self.assertEqual(rows[0]["stdout_path"], rel)
            self.assertEqual(copies[0][1], rel)


class MalformedIdTests(unittest.TestCase):
    def test_traversal_command_receipt_id_is_malformed(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/command-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "../../../../tmp/x", "step_id": "P1", "attempt_id": "A1",
                "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
                "finished_at": "t", "exit_code": 0, "truncated": False,
                "stdout_path": None, "stdout_sha256": None, "stderr_path": None, "stderr_sha256": None,
            })
            cols = aggregate._malformed_id_collisions([Source(a, "w1", "a")])
            self.assertTrue(any(c["kind"] == "malformed_id" and c["field"] == "id" for c in cols))

    def test_bad_attempt_id_in_step_runs_is_malformed(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/step-runs.jsonl", {"schema_version": "0.5.0", "event": "claimed", "step_id": "P2", "attempt_id": "A1/../evil", "recorded_at": "t"})
            cols = aggregate._malformed_id_collisions([Source(a, "w1", "a")])
            self.assertTrue(any(c["kind"] == "malformed_id" and c["field"] == "attempt_id" for c in cols))

    def test_plain_local_ids_are_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            self.assertEqual(aggregate._malformed_id_collisions([Source(a, "w1", "a")]), [])

    def test_malformed_id_fails_closed_in_analyze(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/file-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "FR1/../../evil", "step_id": "P1", "attempt_id": "A1",
                "path": "src/x.py", "change_kind": "modified", "before_git_blob": None,
                "after_sha256": "a" * 64, "recorded_at": "2026-07-05T00:01:00+00:00",
            })
            report = aggregate.analyze([Source(a, "w1", "a")], out, base_ref="HEAD")
            self.assertEqual(report["status"], "collision")
            self.assertTrue(any(c["kind"] == "malformed_id" for c in report["collisions"]))


class PlanWriteTests(unittest.TestCase):
    def test_concat_dedupes_baseline_and_orders_by_timestamp(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"]); b = build_tree(Path(tmp) / "b", steps=["P2"])
            shared = {"id": "E0", "claim": "shared", "recorded_at": "2026-07-05T00:00:00+00:00"}
            append_jsonl(a / ".agent/evidence.jsonl", dict(shared))
            append_jsonl(b / ".agent/evidence.jsonl", dict(shared))
            append_jsonl(a / ".agent/evidence.jsonl", {"id": "E1", "claim": "a-only", "recorded_at": "2026-07-05T00:02:00+00:00"})
            rows = aggregate._merge_concat([Source(a, "w1", "a"), Source(b, "w2", "b")], ".agent/evidence.jsonl")
            ids = [r["id"] for r in rows]
            self.assertEqual(ids.count("E0"), 1)
            self.assertEqual(ids, ["E0", "E1"])

    def test_review_runs_dedupe_and_plan_copies(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = ReviewArtifactTests()._tree_with_review(Path(tmp) / "a")
            b = ReviewArtifactTests()._tree_with_review(Path(tmp) / "b")
            rows, copies = aggregate._merge_review_runs([Source(a, "w1", "a"), Source(b, "w2", "b")])
            self.assertEqual(len(rows), 1)
            dst_rels = {c[1] for c in copies}
            self.assertIn(".agent/reviews/" + ReviewArtifactTests.REVIEW_ID + "/manifest.json", dst_rels)
            self.assertIn(".agent/reviews/" + ReviewArtifactTests.REVIEW_ID + "/findings.json", dst_rels)

    def test_plan_write_assembles_all_sections(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            b = build_tree(Path(tmp) / "b", steps=["P2"], files=[("src/y.py", "b" * 64)])
            plan = aggregate.plan_write([Source(a, "w1", "a"), Source(b, "w2", "b")])
            self.assertIn(".agent/step-runs.jsonl", plan["ledgers"])
            self.assertIn(".agent/plan.lock.json", plan["must_match"])
            attempts = {r["attempt_id"] for r in plan["ledgers"][".agent/step-runs.jsonl"]}
            self.assertEqual(attempts, {"WTw1-A1", "WTw2-A1"})


class RowTimestampTests(unittest.TestCase):
    def test_evidence_last_verified_drives_concat_order(self):
        # Evidence rows carry last_verified (not recorded_at); _merge_concat must
        # order them by it, not fall back to source/input position.
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            append_jsonl(a / ".agent/evidence.jsonl", {"id": "E2", "last_verified": "2026-07-05T00:09:00+00:00"})
            append_jsonl(a / ".agent/evidence.jsonl", {"id": "E1", "last_verified": "2026-07-05T00:01:00+00:00"})
            rows = aggregate._merge_concat([Source(a, "w1", "a")], ".agent/evidence.jsonl")
            self.assertEqual([r["id"] for r in rows], ["E1", "E2"])  # sorted by last_verified, not file order


class WriteCanonicalTests(unittest.TestCase):
    def test_writes_canonical_agent_with_namespaced_ids(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            result = aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)
            rows = aggregate.read_jsonl(out / ".agent/step-runs.jsonl")
            attempts = {r["attempt_id"] for r in rows}
            self.assertEqual(attempts, {"WTw1-A1", "WTw2-A1"})

    def test_collision_writes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            for root, extra in ((a, "one"), (b, "two")):
                path = root / ".agent/step-runs.jsonl"
                path.write_text(
                    json.dumps({"schema_version": "0.5.0", "event": "completed", "step_id": "P1", "attempt_id": "A1", "agent_id": extra, "recorded_at": "2026-07-05T00:01:00+00:00"}, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            result = aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(result["status"], "collision")
            self.assertFalse((out / ".agent").exists())

    def test_no_staging_leftovers_on_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            result = aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok")
            leftovers = [p.name for p in out.iterdir() if p.name.startswith(".agent.aggregate")]
            self.assertEqual(leftovers, [])

    def test_stage_rejects_unsafe_copy_destination(self):
        # Defense in depth: a copy tuple whose dst_rel escapes the staging root
        # must raise before anything is copied.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"; agent_dir = staging / ".agent"; agent_dir.mkdir(parents=True)
            src = Path(tmp) / "src.txt"; src.write_text("x", encoding="utf-8")
            plan = {"must_match": {}, "ledgers": {}, "receipt_copies": [(src, "../escape.txt", None)], "review_copies": []}
            with self.assertRaises(ValueError):
                aggregate._stage_canonical(plan, agent_dir)
            self.assertFalse((Path(tmp) / "escape.txt").exists())

    def test_swap_failure_restores_pre_existing_agent(self):
        # If the final os.replace(agent_dir, final) fails after the pre-existing
        # .agent was moved aside, the original tree must be restored intact and
        # the original error must propagate.
        import os as _os
        import tempfile
        import unittest.mock
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            (out / ".agent").mkdir()
            (out / ".agent/sentinel.txt").write_text("ORIGINAL", encoding="utf-8")
            real_replace = _os.replace
            calls = {"n": 0}

            def flaky_replace(src, dst):
                calls["n"] += 1
                if calls["n"] == 2:  # the agent_dir -> final swap
                    raise OSError("boom")
                return real_replace(src, dst)

            with unittest.mock.patch("agentflow.aggregate.os.replace", flaky_replace):
                with self.assertRaises(OSError):
                    aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertTrue((out / ".agent/sentinel.txt").exists())
            self.assertEqual((out / ".agent/sentinel.txt").read_text(encoding="utf-8"), "ORIGINAL")
            leftovers = [p.name for p in out.iterdir() if p.name.startswith(".agent.aggregate")]
            self.assertEqual(leftovers, [])


class AggregationProvenanceEmissionTests(unittest.TestCase):
    """#112: write_canonical() must emit the .agent/aggregation.json provenance
    singleton alongside the merged ledgers. Uses the git-backed two-worktree
    fixture (_repo_with_worktrees): a real base_ref is needed for analyze()
    to resolve base_commit (an unresolved base is itself a collision), so
    these fixture roots are real git repos/worktrees, not plain dirs.
    """

    def test_emits_aggregation_json_with_expected_shape(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            result = aggregate.write_canonical(sources, out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)

            agg_path = out / aggregate.ARTIFACT_PATHS["aggregation"]
            self.assertTrue(agg_path.exists())
            payload = json.loads(agg_path.read_text(encoding="utf-8"))

            # Whole-dict pin: any extra/missing top-level key fails, and the
            # emitted sources array must equal the returned result's sources.
            self.assertEqual(payload, {
                "schema_version": aggregate.AGGREGATION_SCHEMA_VERSION,
                "mode": "cross_worktree",
                "source_count": 2,
                "sources": result["sources"],
            })

            self.assertEqual(len(payload["sources"]), 2)
            self.assertEqual(payload["sources"][0]["source_id"], "w1")
            self.assertEqual(payload["sources"][0]["root_label"], "a")
            self.assertEqual(payload["sources"][0]["namespaced_prefix"], "WTw1-")
            self.assertIn("base_commit", payload["sources"][0])
            self.assertIn("head_commit", payload["sources"][0])

            self.assertEqual(payload["sources"][1]["source_id"], "w2")
            self.assertEqual(payload["sources"][1]["root_label"], "b")
            self.assertEqual(payload["sources"][1]["namespaced_prefix"], "WTw2-")
            self.assertIn("base_commit", payload["sources"][1])
            self.assertIn("head_commit", payload["sources"][1])

    def test_non_git_sources_have_null_base_and_head_commit(self):
        # A plain (non-git) fixture pair cannot reach status "ok" (unresolved
        # base is itself a fail-closed collision), but the base_commit/
        # head_commit keys must still be present with null values in the
        # collision report's per-source summaries -- proving _head()/
        # _base_commit() degrade to None rather than raising.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            b = build_tree(Path(tmp) / "b", steps=["P2"])
            out = Path(tmp) / "out"
            out.mkdir()
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            result = aggregate.write_canonical(sources, out)
            self.assertEqual(result["status"], "collision")
            for summary in result["sources"]:
                self.assertIn("base_commit", summary)
                self.assertIn("head_commit", summary)
                self.assertIsNone(summary["base_commit"])
                self.assertIsNone(summary["head_commit"])
            self.assertFalse((out / aggregate.ARTIFACT_PATHS["aggregation"]).exists())

    def test_collision_run_emits_no_aggregation_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            # Force a collision: both trees complete the same step id with
            # different rows (same pattern as WriteCanonicalTests).
            for source, extra in zip(sources, ("one", "two")):
                path = source.root / ".agent/step-runs.jsonl"
                path.write_text(
                    json.dumps({"schema_version": "0.5.0", "event": "completed", "step_id": "P1", "attempt_id": "A1", "agent_id": extra, "recorded_at": "2026-07-05T00:01:00+00:00"}, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            result = aggregate.write_canonical(sources, out, base_ref="HEAD")
            self.assertEqual(result["status"], "collision")
            self.assertFalse((out / aggregate.ARTIFACT_PATHS["aggregation"]).exists())
            self.assertFalse((out / ".agent").exists())

    def test_stale_input_aggregation_json_is_ignored_and_untouched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]
            bogus = {"mode": "bogus"}
            stale_path = a / ".agent/aggregation.json"
            write_json(stale_path, bogus)
            stale_bytes = stale_path.read_bytes()

            result = aggregate.write_canonical(sources, out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)

            agg_path = out / aggregate.ARTIFACT_PATHS["aggregation"]
            payload = json.loads(agg_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "cross_worktree")
            self.assertEqual(payload["source_count"], 2)
            self.assertNotEqual(payload, bogus)

            # Input tree's stale file is byte-untouched (inputs immutable).
            self.assertEqual(stale_path.read_bytes(), stale_bytes)

    def test_single_source_emits_singleton_aggregation_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a = _repo_with_one_worktree(tmp)
            sources = [Source(a, "w1", "a")]
            result = aggregate.write_canonical(sources, out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)

            agg_path = out / aggregate.ARTIFACT_PATHS["aggregation"]
            payload = json.loads(agg_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_count"], 1)
            self.assertEqual(payload["sources"][0]["namespaced_prefix"], "WTw1-")

    def test_reaggregate_into_same_output_root_replaces_canonical(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = _repo_with_worktrees(tmp)
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b")]

            first = aggregate.write_canonical(sources, out, base_ref="HEAD")
            self.assertEqual(first["status"], "ok", first)

            second = aggregate.write_canonical(sources, out, base_ref="HEAD")
            self.assertEqual(second["status"], "ok", second)

            agg_path = out / aggregate.ARTIFACT_PATHS["aggregation"]
            payload = json.loads(agg_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source_count"], 2)
            self.assertEqual(
                sorted(s["source_id"] for s in payload["sources"]), ["w1", "w2"]
            )


class CliWriteTests(unittest.TestCase):
    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _run(self, cwd, *args):
        import os
        env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run([sys.executable, "-m", "agentflow", *args], cwd=str(cwd), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def _setup(self, tmp):
        from agentflow.receipts import sha256_path
        out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
        self._git(out, "init", "-q"); self._git(out, "config", "user.email", "t@t"); self._git(out, "config", "user.name", "t")
        (out / "seed.txt").write_text("seed", encoding="utf-8"); self._git(out, "add", "-A"); self._git(out, "commit", "-qm", "seed")
        a = Path(tmp) / "a"; b = Path(tmp) / "b"
        self._git(out, "worktree", "add", "-q", "-b", "wa", str(a), "HEAD")
        self._git(out, "worktree", "add", "-q", "-b", "wb", str(b), "HEAD")
        fx = out / "src/x.py"; fx.write_text("x", encoding="utf-8"); fy = out / "src/y.py"; fy.write_text("y", encoding="utf-8")
        build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(fx))])
        build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(fy))])
        return out, a, b

    def test_write_run_emits_canonical_and_exits_0(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = self._setup(tmp)
            proc = self._run(tmp, "aggregate-ledgers", "--input", str(a), "--source-id", "w1", "--input", str(b), "--source-id", "w2", "--output", str(out), "--base", "HEAD", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertTrue((out / ".agent/step-runs.jsonl").exists())

            sources_by_id = {s["source_id"]: s for s in payload["sources"]}
            self.assertEqual(sorted(sources_by_id), ["w1", "w2"])
            self.assertEqual(sources_by_id["w1"]["namespaced_prefix"], "WTw1-")
            self.assertEqual(sources_by_id["w2"]["namespaced_prefix"], "WTw2-")

    def test_collision_write_run_exits_1_no_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = self._setup(tmp)
            for root, extra in ((a, "one"), (b, "two")):
                (root / ".agent/step-runs.jsonl").write_text(json.dumps({"schema_version": "0.5.0", "event": "completed", "step_id": "P1", "attempt_id": "A1", "agent_id": extra, "recorded_at": "t"}, sort_keys=True) + "\n", encoding="utf-8")
            proc = self._run(tmp, "aggregate-ledgers", "--input", str(a), "--source-id", "w1", "--input", str(b), "--source-id", "w2", "--output", str(out), "--base", "HEAD")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertFalse((out / ".agent").exists())


class EndToEndAggregateTests(unittest.TestCase):
    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _run(self, cwd, *args):
        import os
        env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run([sys.executable, "-m", "agentflow", *args], cwd=str(cwd), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_positive_fixture_verifies_and_detects_aggregation_tamper(self):
        import tempfile
        from agentflow.receipts import sha256_path
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            self._git(out, "init", "-q"); self._git(out, "config", "user.email", "t@t"); self._git(out, "config", "user.name", "t")
            (out / "seed.txt").write_text("seed", encoding="utf-8"); self._git(out, "add", "-A"); self._git(out, "commit", "-qm", "seed")
            a = Path(tmp) / "a"; b = Path(tmp) / "b"
            self._git(out, "worktree", "add", "-q", "-b", "wa", str(a), "HEAD")
            self._git(out, "worktree", "add", "-q", "-b", "wb", str(b), "HEAD")
            fx = out / "src/x.py"; fx.write_text("x", encoding="utf-8"); fy = out / "src/y.py"; fy.write_text("y", encoding="utf-8")
            build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(fx))])
            build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(fy))])
            rdir = a / ".agent/receipts/A1"; rdir.mkdir(parents=True, exist_ok=True)
            f = rdir / "CR1.stdout.txt"; f.write_text("run output", encoding="utf-8")
            append_jsonl(a / ".agent/command-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
                "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
                "finished_at": "t", "exit_code": 0, "truncated": False,
                "stdout_path": ".agent/receipts/A1/CR1.stdout.txt", "stdout_sha256": sha256_path(f),
                "stderr_path": None, "stderr_sha256": None,
            })
            agg = self._run(tmp, "aggregate-ledgers", "--input", str(a), "--source-id", "w1", "--input", str(b), "--source-id", "w2", "--output", str(out), "--base", "HEAD", "--json")
            self.assertEqual(agg.returncode, 0, agg.stderr)
            self.assertTrue((out / ".agent/receipts/WTw1-A1/WTw1-CR1.stdout.txt").exists())
            proof = self._run(out, "build-proof")
            self.assertEqual(proof.returncode, 0, proof.stdout + proof.stderr)

            # #112: build-proof embeds the aggregation provenance singleton
            # emitted by write_canonical (#111), keyed by the fixture's own
            # WTw1-/WTw2- source ids -- a cross-worktree proof declares that
            # it is cross-worktree (design #30 S9).
            proof_payload = json.loads((out / ".agent/proof-pack.json").read_text(encoding="utf-8"))
            self.assertEqual(proof_payload["aggregation"]["source_count"], 2)
            self.assertEqual(
                [s["namespaced_prefix"] for s in proof_payload["aggregation"]["sources"]],
                ["WTw1-", "WTw2-"],
            )
            self.assertIn(".agent/aggregation.json", proof_payload["generated_from"])

            vproof = self._run(out, "verify-proof")
            self.assertEqual(vproof.returncode, 0, vproof.stdout + vproof.stderr)

            # Tamper the canonical root's aggregation provenance after the proof
            # was built: verify-proof must catch it like any other hash-bound
            # proof source, giving the aggregation block real tamper-evidence
            # end to end (not just at the unit level in test_proof.py).
            agg_path = out / ".agent/aggregation.json"
            tampered = json.loads(agg_path.read_text(encoding="utf-8"))
            tampered["sources"][0]["source_id"] = "tampered"
            agg_path.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")

            retampered = self._run(out, "verify-proof")
            self.assertNotEqual(retampered.returncode, 0, retampered.stdout + retampered.stderr)
            self.assertIn("hash mismatch for .agent/aggregation.json", retampered.stdout)


class EndToEndNegativeTests(unittest.TestCase):
    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _run(self, cwd, *args):
        import os
        env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run([sys.executable, "-m", "agentflow", *args], cwd=str(cwd), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_both_trees_complete_same_step_aborts_with_no_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
            self._git(out, "init", "-q"); self._git(out, "config", "user.email", "t@t"); self._git(out, "config", "user.name", "t")
            (out / "seed.txt").write_text("seed", encoding="utf-8"); self._git(out, "add", "-A"); self._git(out, "commit", "-qm", "seed")
            a = Path(tmp) / "a"; b = Path(tmp) / "b"
            self._git(out, "worktree", "add", "-q", "-b", "wa", str(a), "HEAD")
            self._git(out, "worktree", "add", "-q", "-b", "wb", str(b), "HEAD")
            build_tree(a, steps=["P1"]); build_tree(b, steps=["P1"])
            pb = b / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in pb.read_text(encoding="utf-8").splitlines()]
            rows[0]["agent_id"] = "different"
            pb.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
            proc = self._run(tmp, "aggregate-ledgers", "--input", str(a), "--source-id", "w1", "--input", str(b), "--source-id", "w2", "--output", str(out), "--base", "HEAD")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertFalse((out / ".agent").exists())


class ReviewFixTests(unittest.TestCase):
    """Coverage added from the /code-review pass."""

    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _repo(self, tmp):
        out = Path(tmp) / "out"; (out / "src").mkdir(parents=True)
        self._git(out, "init", "-q"); self._git(out, "config", "user.email", "t@t"); self._git(out, "config", "user.name", "t")
        (out / "seed.txt").write_text("seed", encoding="utf-8"); self._git(out, "add", "-A"); self._git(out, "commit", "-qm", "seed")
        a = Path(tmp) / "a"; b = Path(tmp) / "b"
        self._git(out, "worktree", "add", "-q", "-b", "wa", str(a), "HEAD")
        self._git(out, "worktree", "add", "-q", "-b", "wb", str(b), "HEAD")
        (out / "src/x.py").write_text("x", encoding="utf-8")
        (out / "src/y.py").write_text("y", encoding="utf-8")
        return out, a, b

    def test_content_addressed_relocation_end_to_end(self):
        import tempfile
        import hashlib
        from agentflow.receipts import sha256_path
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = self._repo(tmp)
            contract = {"schema_version": "0.3.0", "command_policy": {"receipt_store": "content_addressed"}}
            build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(out / "src/x.py"))], contract=contract)
            build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(out / "src/y.py"))], contract=contract)
            data = b"content output"; digest = hashlib.sha256(data).hexdigest()
            cpath = a / ".agent/receipts/sha256" / digest[:2] / digest
            cpath.parent.mkdir(parents=True, exist_ok=True); cpath.write_bytes(data)
            rel = ".agent/receipts/sha256/" + digest[:2] + "/" + digest
            append_jsonl(a / ".agent/command-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
                "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
                "finished_at": "t", "exit_code": 0, "truncated": False,
                "stdout_path": rel, "stdout_sha256": digest, "stderr_path": None, "stderr_sha256": None,
            })
            result = aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)
            self.assertTrue((out / rel).exists())                       # copied verbatim to the content-hash path
            self.assertEqual((out / rel).read_bytes(), data)
            crs = aggregate.read_jsonl(out / ".agent/command-receipts.jsonl")
            self.assertEqual(crs[0]["id"], "WTw1-CR1")                  # ledger id namespaced
            self.assertEqual(crs[0]["stdout_path"], rel)               # path unchanged (content hash)

    def test_review_artifacts_relocate_inside_canonical_agent(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        from agentflow.receipts import sha256_path
        from agentflow.review import sha256_file
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = self._repo(tmp)
            build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(out / "src/x.py"))])
            build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(out / "src/y.py"))])
            state = a / "docs/ai/state/main"; state.mkdir(parents=True)
            manifest = state / "review-manifest.json"; manifest.write_text("{}", encoding="utf-8")
            artifact = state / "findings-final.json"; artifact.write_text("[]", encoding="utf-8")
            rid = "RR-20260705T000000Z-abcdef12"
            append_jsonl(a / ".agent/review-runs.jsonl", {
                "schema_version": "0.4.0", "review_run_id": rid,
                "recorded_at": "2026-07-05T00:00:00+00:00",
                "state_dir": "docs/ai/state/main",
                "manifest_path": "docs/ai/state/main/review-manifest.json",
                "manifest_sha256": sha256_file(manifest),
                "gate_status": "pass",
                "artifacts": [{"path": "docs/ai/state/main/findings-final.json", "sha256": sha256_file(artifact)}],
            })
            result = aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(result["status"], "ok", result)
            row = aggregate.read_jsonl(out / ".agent/review-runs.jsonl")[0]
            self.assertEqual(row["state_dir"], f".agent/reviews/{rid}")
            self.assertEqual(row["manifest_path"], f".agent/reviews/{rid}/review-manifest.json")
            self.assertEqual(row["artifacts"][0]["path"], f".agent/reviews/{rid}/findings-final.json")
            self.assertTrue((out / row["manifest_path"]).exists())
            self.assertTrue((out / row["artifacts"][0]["path"]).exists())

    def test_malformed_id_fails_closed_in_write_canonical(self):
        import tempfile
        from agentflow.receipts import sha256_path
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = self._repo(tmp)
            build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(out / "src/x.py"))])
            build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(out / "src/y.py"))])
            append_jsonl(a / ".agent/command-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "../../../../etc/passwd", "step_id": "P1", "attempt_id": "A1",
                "provenance": "observed", "command": ["true"], "cwd": ".", "started_at": "t",
                "finished_at": "t", "exit_code": 0, "truncated": False,
                "stdout_path": None, "stdout_sha256": None, "stderr_path": None, "stderr_sha256": None,
            })
            result = aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            self.assertEqual(result["status"], "collision")
            self.assertTrue(any(c["kind"] == "malformed_id" for c in result["collisions"]))
            self.assertFalse((out / ".agent").exists())

    def test_three_source_baseline_dedupe(self):
        # build_tree assigns attempt A1 to every step, so P0 (shared) and each
        # tree's own step share the literal A1 in-source; the row-level baseline
        # discriminator must still dedupe P0 once and namespace only the local step.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P0", "P1"])
            b = build_tree(Path(tmp) / "b", steps=["P0", "P2"])
            c = build_tree(Path(tmp) / "c", steps=["P0", "P3"])
            sources = [Source(a, "w1", "a"), Source(b, "w2", "b"), Source(c, "w3", "c")]
            baseline = aggregate._baseline_canon(sources, ".agent/step-runs.jsonl")
            amaps = {s.source_id: aggregate._attempt_map(s, baseline) for s in sources}
            rows = aggregate._merge_step_runs(sources, baseline, amaps)
            steps = [(r["step_id"], r["attempt_id"], r["event"]) for r in rows]
            self.assertEqual(steps.count(("P0", "A1", "completed")), 1)  # baseline once across 3 sources
            self.assertIn(("P1", "WTw1-A1", "completed"), steps)
            self.assertIn(("P2", "WTw2-A1", "completed"), steps)
            self.assertIn(("P3", "WTw3-A1", "completed"), steps)

    def test_swap_restore_failure_preserves_backup(self):
        # If the final swap AND the restore both fail, the pre-existing .agent must
        # survive in a .bak dir for manual recovery, never be deleted.
        import os as _os
        import tempfile
        import unittest.mock
        from agentflow.receipts import sha256_path
        with tempfile.TemporaryDirectory() as tmp:
            out, a, b = self._repo(tmp)
            build_tree(a, steps=["P1"], files=[("src/x.py", sha256_path(out / "src/x.py"))])
            build_tree(b, steps=["P2"], files=[("src/y.py", sha256_path(out / "src/y.py"))])
            (out / ".agent").mkdir()
            (out / ".agent/sentinel.txt").write_text("ORIGINAL", encoding="utf-8")
            real_replace = _os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] >= 2:  # the agent_dir->final swap AND the restore both fail
                    raise OSError("boom")
                return real_replace(src, dst)

            with unittest.mock.patch("agentflow.aggregate.os.replace", flaky):
                with self.assertRaises(OSError):
                    aggregate.write_canonical([Source(a, "w1", "a"), Source(b, "w2", "b")], out, base_ref="HEAD")
            baks = [p for p in out.iterdir() if p.name.endswith(".bak")]
            self.assertEqual(len(baks), 1, [p.name for p in out.iterdir()])
            self.assertEqual((baks[0] / "sentinel.txt").read_text(encoding="utf-8"), "ORIGINAL")


class CriticizeReviewTests(unittest.TestCase):
    """Fail-closed hardening from the /criticize-review pass."""

    def test_intra_source_duplicate_command_receipt_id_fails_closed(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            # Two CR1 rows in ONE source, differing content -> would collapse to
            # the same namespaced id + physical path and silently overwrite.
            for text in ("first", "second"):
                append_jsonl(a / ".agent/command-receipts.jsonl", {
                    "schema_version": "0.4.0", "id": "CR1", "step_id": "P1", "attempt_id": "A1",
                    "provenance": "observed", "command": [text], "cwd": ".", "started_at": "t",
                    "finished_at": "t", "exit_code": 0, "truncated": False,
                    "stdout_path": None, "stdout_sha256": None, "stderr_path": None, "stderr_sha256": None,
                })
            cols = aggregate._intra_source_dup_id_collisions([Source(a, "w1", "a")])
            self.assertTrue(any(c["kind"] == "intra_source_dup_id" and c["id"] == "CR1" for c in cols))

    def test_unique_ids_within_source_ok(self):
        import tempfile
        from agentflow.artifacts import append_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"], files=[("src/x.py", "a" * 64)])
            append_jsonl(a / ".agent/file-receipts.jsonl", {
                "schema_version": "0.4.0", "id": "FR2", "step_id": "P1", "attempt_id": "A1",
                "path": "src/y.py", "change_kind": "modified", "before_git_blob": None,
                "after_sha256": "b" * 64, "recorded_at": "2026-07-05T00:02:00+00:00",
            })
            self.assertEqual(aggregate._intra_source_dup_id_collisions([Source(a, "w1", "a")]), [])

    def test_malformed_execution_contract_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            (a / ".agent/execution.contract.json").write_text("{ truncated", encoding="utf-8")
            cols = aggregate._malformed_contract_collisions([Source(a, "w1", "a")])
            self.assertTrue(any(c["kind"] == "malformed_contract" for c in cols))

    def test_empty_sources_is_collision_not_crash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"; out.mkdir()
            report = aggregate.analyze([], out, base_ref="HEAD")
            self.assertEqual(report["status"], "collision")
            self.assertTrue(any(c["kind"] == "no_sources" for c in report["collisions"]))

    def test_nonexistent_output_dir_exits_2(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a = build_tree(Path(tmp) / "a", steps=["P1"])
            env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
            proc = subprocess.run(
                [sys.executable, "-m", "agentflow", "aggregate-ledgers",
                 "--input", str(a), "--source-id", "w1",
                 "--output", str(Path(tmp) / "does-not-exist"), "--dry-run"],
                cwd=str(tmp), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("is not a directory", proc.stderr)
