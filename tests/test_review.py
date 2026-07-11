from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentflow.artifacts import create_initial_artifacts, plan_binding_sha256
from agentflow.contracts import (
    ARTIFACT_PATHS,
    ARTIFACT_SCHEMA_VERSIONS,
    EVIDENCE_KINDS,
    REVIEW_GATE_POLICIES,
)
from agentflow.review import (
    REVIEW_VERIFICATION_SEMANTICS,
    build_review_run_record,
    build_time_review_policy,
    effective_review_policy,
    join_review_gate,
    normalize_artifact_path,
    parse_finding_ref,
    review_checks,
    review_summary,
    validate_manifest,
    verify_review_integrity,
)


def _write_state(root: Path, manifest: dict) -> Path:
    state = root / manifest["state_dir"]
    state.mkdir(parents=True, exist_ok=True)
    for entry in manifest["artifacts"]:
        (state / entry["path"]).write_text(f"content of {entry['path']}", encoding="utf-8")
    manifest_path = state / "review-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


class ReviewConstantsTests(unittest.TestCase):
    def test_review_runs_registered_and_created_by_init(self) -> None:
        self.assertEqual(ARTIFACT_PATHS["review-runs"], ".agent/review-runs.jsonl")
        self.assertIn("review-runs", ARTIFACT_SCHEMA_VERSIONS)
        self.assertIn("review", EVIDENCE_KINDS)
        self.assertEqual(REVIEW_GATE_POLICIES, ("block", "ignore", "warn"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            self.assertTrue((root / ".agent/review-runs.jsonl").exists())


class JoinReviewGateTests(unittest.TestCase):
    def test_orders_ignore_below_warn_below_block(self) -> None:
        self.assertEqual(join_review_gate("ignore", "warn"), "warn")
        self.assertEqual(join_review_gate("warn", "block"), "block")
        self.assertEqual(join_review_gate("ignore", "block"), "block")

    def test_is_commutative_and_idempotent(self) -> None:
        self.assertEqual(join_review_gate("block", "ignore"), "block")
        self.assertEqual(join_review_gate("warn", "warn"), "warn")

    def test_unknown_value_is_treated_as_lowest(self) -> None:
        # An unrecognized recorded value must never raise and never win.
        self.assertEqual(join_review_gate("bogus", "warn"), "warn")
        self.assertEqual(join_review_gate("warn", "bogus"), "warn")

    def test_gate_order_covers_all_policies(self) -> None:
        from agentflow.review import GATE_ORDER

        self.assertEqual(set(GATE_ORDER), set(REVIEW_GATE_POLICIES))


class ParseFindingRefTests(unittest.TestCase):
    def test_parses_review_run_scoped_ref(self) -> None:
        ref = parse_finding_ref("RR-20260620T180000Z-ab12cd34#BP-001")
        self.assertEqual(
            ref,
            {"review_run_id": "RR-20260620T180000Z-ab12cd34", "finding_id": "BP-001"},
        )

    def test_rejects_missing_hash(self) -> None:
        with self.assertRaises(ValueError):
            parse_finding_ref("RR-20260620T180000Z-ab12cd34")

    def test_rejects_bad_review_run_id(self) -> None:
        with self.assertRaises(ValueError):
            parse_finding_ref("RR-nope#BP-001")

    def test_rejects_empty_finding_id(self) -> None:
        with self.assertRaises(ValueError):
            parse_finding_ref("RR-20260620T180000Z-ab12cd34#")

    def test_rejects_trailing_newline_review_run_id(self) -> None:
        # re.match lets '$' match before a final newline, so fullmatch is
        # required to reject a trailing-newline review_run_id.
        with self.assertRaises(ValueError):
            parse_finding_ref("RR-20260620T180000Z-ab12cd34\n#BP-001")


def good_manifest() -> dict:
    return {
        "schema_version": "0.1.0",
        "review_run_id": "RR-20260620T180000Z-ab12cd34",
        "state_dir": "docs/ai/state/main",
        "policy": "full",
        "gate_status": "pass",
        "active_blocking": [],
        "findings": {
            "counts_by_severity": {"high": 0, "medium": 1, "low": 2},
            "counts_by_status": {"open": 1, "fixed": 1, "rejected": 1},
            "index": [
                {
                    "finding_id": "BP-001",
                    "severity": "high",
                    "status": "fixed",
                    "steelman_verdict": "confirmed",
                    "superseded_by": "",
                    "fix_commit": "abc1234",
                }
            ],
        },
        "artifacts": [
            {"path": "findings-final.yaml"},
            {"path": "gate.yaml"},
            {"path": "synthesis.md"},
        ],
    }


class ValidateManifestTests(unittest.TestCase):
    def test_accepts_good_manifest(self) -> None:
        validate_manifest(good_manifest())  # no raise

    def test_rejects_bad_review_run_id(self) -> None:
        m = good_manifest()
        m["review_run_id"] = "RR-bad"
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_bad_gate_status(self) -> None:
        m = good_manifest()
        m["gate_status"] = "green"
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_non_list_active_blocking(self) -> None:
        m = good_manifest()
        m["active_blocking"] = "BP-001"
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_bad_severity_in_index(self) -> None:
        m = good_manifest()
        m["findings"]["index"][0]["severity"] = "blocker"
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_empty_artifacts(self) -> None:
        m = good_manifest()
        m["artifacts"] = []
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_missing_schema_version(self) -> None:
        m = good_manifest()
        del m["schema_version"]
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_out_of_range_schema_version(self) -> None:
        m = good_manifest()
        m["schema_version"] = "9.9.9"
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_major_bump_schema_version(self) -> None:
        m = good_manifest()
        m["schema_version"] = "1.0.0"
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_empty_schema_version(self) -> None:
        m = good_manifest()
        m["schema_version"] = ""
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_rejects_non_string_schema_version(self) -> None:
        m = good_manifest()
        m["schema_version"] = 0.1
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_accepts_supported_schema_version(self) -> None:
        m = good_manifest()
        m["schema_version"] = "0.1.0"
        validate_manifest(m)  # no raise

    def test_rejects_schema_version_with_trailing_newline(self) -> None:
        # ECMA-262 '$' (JSON Schema) matches only true end-of-input, so the
        # published schema rejects an embedded trailing newline. The Python
        # validator must agree; re.match + '$' would silently accept "0.1.0\n".
        m = good_manifest()
        m["schema_version"] = "0.1.0\n"
        with self.assertRaises(ValueError):
            validate_manifest(m)


class NormalizeArtifactPathTests(unittest.TestCase):
    def test_returns_root_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs/ai/state/main").mkdir(parents=True)
            (root / "docs/ai/state/main/gate.yaml").write_text("x", encoding="utf-8")
            rel = normalize_artifact_path(root, "docs/ai/state/main", "gate.yaml")
            self.assertEqual(rel, "docs/ai/state/main/gate.yaml")

    def test_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                normalize_artifact_path(root, "docs/ai/state/main", "/etc/passwd")

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                normalize_artifact_path(root, "docs/ai/state/main", "../../../../etc/passwd")

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "docs/ai/state/main"
            state.mkdir(parents=True)
            outside = root.parent / "outside-secret.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                os.symlink(outside, state / "leak.yaml")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this platform")
            with self.assertRaises(ValueError):
                normalize_artifact_path(root, "docs/ai/state/main", "leak.yaml")


class BuildReviewRunRecordTests(unittest.TestCase):
    def test_builds_record_with_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            manifest_path = _write_state(root, good_manifest())
            record = build_review_run_record(root, manifest_path)
            self.assertEqual(record["schema_version"], "0.5.0")
            self.assertEqual(record["review_run_id"], "RR-20260620T180000Z-ab12cd34")
            self.assertEqual(record["gate_status"], "pass")
            self.assertEqual(record["active_blocking"], [])
            self.assertEqual(len(record["artifacts"]), 3)
            for entry in record["artifacts"]:
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(entry["path"].startswith("docs/ai/state/main/"))
            self.assertRegex(record["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                record["plan_sha256"],
                plan_binding_sha256(
                    json.loads(
                        (root / ".agent/plan.lock.json").read_text(encoding="utf-8")
                    )
                ),
            )

    def test_plan_binding_hash_ignores_lock_metadata(self) -> None:
        plan = {"objective": "x", "steps": [], "locked": False, "locked_at": None}
        relocked = dict(plan, locked=True, locked_at="2026-07-10T00:00:00+00:00")
        changed = dict(plan, objective="y")

        self.assertEqual(plan_binding_sha256(plan), plan_binding_sha256(relocked))
        self.assertNotEqual(plan_binding_sha256(plan), plan_binding_sha256(changed))

    def test_rejects_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            manifest_path = _write_state(root, good_manifest())
            (root / "docs/ai/state/main/gate.yaml").unlink()
            with self.assertRaises(ValueError):
                build_review_run_record(root, manifest_path)

    def test_rejects_state_dir_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            manifest = good_manifest()
            manifest_path = _write_state(root, manifest)
            manifest["state_dir"] = "docs/ai/state/other"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_review_run_record(root, manifest_path)

    def test_rejects_duplicate_review_run_id(self) -> None:
        from agentflow.artifacts import append_jsonl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            manifest_path = _write_state(root, good_manifest())
            append_jsonl(
                root / ".agent/review-runs.jsonl",
                {
                    "schema_version": "0.3.0",
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "recorded_at": "2026-06-20T18:00:00+00:00",
                    "state_dir": "docs/ai/state/main",
                    "manifest_path": "docs/ai/state/main/review-manifest.json",
                    "manifest_sha256": "0" * 64,
                    "gate_status": "pass",
                    "artifacts": [],
                },
            )
            with self.assertRaises(ValueError):
                build_review_run_record(root, manifest_path)

    def test_rejects_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            with self.assertRaises(ValueError):
                build_review_run_record(root, root / "docs/ai/state/main/missing.json")

    def test_malformed_review_runs_row_is_integrity_failure(self) -> None:
        from agentflow.artifacts import append_jsonl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            append_jsonl(root / ".agent/review-runs.jsonl", {"review_run_id": "RR-bad"})
            manifest_path = _write_state(root, good_manifest())
            with self.assertRaises(ValueError):
                build_review_run_record(root, manifest_path)

    def test_malformed_artifact_entry_is_integrity_failure(self) -> None:
        from agentflow.artifacts import append_jsonl
        from agentflow.review import read_review_runs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            append_jsonl(
                root / ".agent/review-runs.jsonl",
                {
                    "schema_version": "0.3.0",
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "recorded_at": "2026-06-20T18:00:00+00:00",
                    "state_dir": "docs/ai/state/main",
                    "manifest_path": "docs/ai/state/main/review-manifest.json",
                    "manifest_sha256": "0" * 64,
                    "gate_status": "pass",
                    "artifacts": ["oops"],
                },
            )
            with self.assertRaises(ValueError):
                read_review_runs(root)

    def test_rejects_directory_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            # Write the regular-file artifacts via the manifest, then add a
            # directory-valued artifact entry that exists() but is not a file.
            manifest = good_manifest()
            manifest_path = _write_state(root, manifest)
            (root / "docs/ai/state/main/subdir").mkdir(parents=True, exist_ok=True)
            manifest["artifacts"].append({"path": "subdir"})
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_review_run_record(root, manifest_path)


class ReviewSummaryTests(unittest.TestCase):
    def test_non_dict_finding_ref_is_skipped(self) -> None:
        from agentflow.artifacts import append_jsonl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            append_jsonl(
                root / ".agent/step-runs.jsonl",
                {
                    "event": "amendment_started",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "finding_refs": ["not-a-dict"],
                },
            )
            summary = review_summary(root)
            self.assertIsInstance(summary, dict)
            self.assertEqual(summary["correlations"], [])
            self.assertEqual(summary["unresolved_finding_refs"], [])


class EffectiveReviewPolicyTests(unittest.TestCase):
    def _root(self, tmp: str, proof_policy=None) -> Path:
        root = Path(tmp)
        (root / ".agent").mkdir(parents=True)
        if proof_policy is not None:
            (root / ".agent/execution.contract.json").write_text(
                json.dumps({"proof_policy": proof_policy}), encoding="utf-8"
            )
        return root

    def test_default_is_warn_non_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "warn")
            self.assertFalse(policy["strict_effective"])

    def test_strict_promotes_warn_to_block_and_records_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            policy = effective_review_policy(root, strict=True)
            self.assertEqual(policy["review_gate"], "block")
            self.assertTrue(policy["strict_effective"])

    def test_recorded_block_floor_raises_a_warn_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # contract default -> caller resolves to warn
            policy = effective_review_policy(
                root,
                strict=False,
                recorded={"review_gate_effective": "block", "proof_strict_effective": True},
            )
            self.assertEqual(policy["review_gate"], "block")
            self.assertTrue(policy["strict_effective"])

    def test_recorded_floor_cannot_downgrade_a_stricter_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, proof_policy={"review_gate": "block"})
            policy = effective_review_policy(
                root,
                strict=False,
                recorded={"review_gate_effective": "ignore", "proof_strict_effective": False},
            )
            self.assertEqual(policy["review_gate"], "block")  # caller block wins the join

    def test_caller_can_ratchet_a_recorded_ignore_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, proof_policy={"review_gate": "warn"})
            policy = effective_review_policy(
                root,
                strict=False,
                recorded={"review_gate_effective": "ignore", "proof_strict_effective": False},
            )
            self.assertEqual(policy["review_gate"], "warn")  # max(ignore, warn)

    def test_corrupt_recorded_value_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            policy = effective_review_policy(
                root, strict=False, recorded={"review_gate_effective": 123}
            )
            self.assertEqual(policy["review_gate"], "warn")

    def test_strict_by_default_contract_not_downgraded_by_lower_recorded_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, proof_policy={"strict_by_default": True})
            policy = effective_review_policy(
                root,
                strict=False,
                recorded={"review_gate_effective": "ignore", "proof_strict_effective": False},
            )
            self.assertEqual(policy["review_gate"], "block")  # strict_by_default promoted warn->block
            self.assertTrue(policy["strict_effective"])


class WorkflowContractReviewPolicyTests(unittest.TestCase):
    """#74: effective_review_policy reads review_depth/proof_policy from the workflow contract."""

    def _root(self, tmp, *, exec_proof_policy=None, review_depth=None, wf_proof_policy=None):
        root = Path(tmp)
        (root / ".agent").mkdir(parents=True)
        if exec_proof_policy is not None:
            (root / ".agent/execution.contract.json").write_text(
                json.dumps({"proof_policy": exec_proof_policy}), encoding="utf-8"
            )
        if review_depth is not None or wf_proof_policy is not None:
            contract = {}
            if review_depth is not None:
                contract["review_depth"] = review_depth
            if wf_proof_policy is not None:
                contract["proof_policy"] = wf_proof_policy
            (root / ".agent/workflow.contract.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
        return root

    def test_review_depth_policy_table(self):
        from agentflow.contracts import REVIEW_DEPTH_POLICY

        self.assertEqual(REVIEW_DEPTH_POLICY["none"], ("ignore", False))
        self.assertEqual(REVIEW_DEPTH_POLICY["light"], ("warn", False))
        self.assertEqual(REVIEW_DEPTH_POLICY["standard"], ("warn", False))
        self.assertEqual(REVIEW_DEPTH_POLICY["spec_quality"], ("block", True))
        self.assertEqual(REVIEW_DEPTH_POLICY["deep"], ("block", True))

    def test_depth_policy_covers_all_review_depths(self):
        from agentflow.contracts import REVIEW_DEPTH_POLICY, WORKFLOW_REVIEW_DEPTHS

        self.assertEqual(set(REVIEW_DEPTH_POLICY), set(WORKFLOW_REVIEW_DEPTHS))

    def test_none_does_not_require_run_and_keeps_warn_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="none")
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "warn")  # join(warn default, ignore) = warn
            self.assertFalse(policy["require_review_run"])
            self.assertEqual(policy["required_review_depth"], "none")

    def test_deep_requires_run_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="deep")
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "block")
            self.assertTrue(policy["require_review_run"])
            self.assertEqual(policy["required_review_depth"], "deep")

    def test_spec_quality_requires_run_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="spec_quality")
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "block")
            self.assertTrue(policy["require_review_run"])

    def test_light_and_standard_warn_without_required_run(self):
        for depth in ("light", "standard"):
            with tempfile.TemporaryDirectory() as tmp:
                root = self._root(tmp, review_depth=depth)
                policy = effective_review_policy(root)
                self.assertEqual(policy["review_gate"], "warn", depth)
                self.assertFalse(policy["require_review_run"], depth)

    def test_depth_floor_never_lowers_execution_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp, exec_proof_policy={"review_gate": "block"}, review_depth="light"
            )
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "block")  # block beats the light warn floor

    def test_workflow_proof_policy_require_review_run_ors_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp, review_depth="light", wf_proof_policy={"require_review_run": True}
            )
            policy = effective_review_policy(root)
            self.assertTrue(policy["require_review_run"])

    def test_absent_workflow_contract_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "warn")
            self.assertFalse(policy["require_review_run"])
            self.assertIsNone(policy["required_review_depth"])

    def test_unknown_review_depth_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="exhaustive")
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "warn")
            self.assertFalse(policy["require_review_run"])
            self.assertIsNone(policy["required_review_depth"])

    def test_malformed_workflow_contract_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir(parents=True)
            (root / ".agent/workflow.contract.json").write_text("{not json", encoding="utf-8")
            policy = effective_review_policy(root)
            self.assertEqual(policy["review_gate"], "warn")
            self.assertFalse(policy["require_review_run"])

    def test_recorded_require_review_run_floors_when_contract_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)  # no workflow contract at verify time
            policy = effective_review_policy(
                root,
                strict=False,
                recorded={"require_review_run": True, "required_review_depth": "deep"},
            )
            self.assertTrue(policy["require_review_run"])
            self.assertEqual(policy["required_review_depth"], "deep")

    def test_legacy_recorded_policy_skips_live_workflow_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="deep")
            policy = effective_review_policy(
                root,
                strict=False,
                recorded={
                    "review_gate_effective": "warn",
                    "proof_strict_effective": False,
                    "require_review_run": False,
                    "verification_semantics": "ratchet-v1",
                },
            )
            self.assertEqual(policy["review_gate"], "warn")
            self.assertFalse(policy["require_review_run"])
            self.assertIsNone(policy["required_review_depth"])

    def test_build_time_policy_records_required_review_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="deep")
            recorded = build_time_review_policy(root)
            self.assertEqual(recorded["required_review_depth"], "deep")
            self.assertTrue(recorded["require_review_run"])


class VerifyReviewIntegrityRatchetTests(unittest.TestCase):
    def _failing_gate_root(self, tmp: str) -> Path:
        from agentflow.artifacts import append_jsonl
        from agentflow.execution import init_execution_artifacts

        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        append_jsonl(
            root / ".agent/review-runs.jsonl",
            {
                "schema_version": "0.3.0",
                "review_run_id": "RR-20260620T180000Z-ab12cd34",
                "recorded_at": "2026-06-20T18:00:00+00:00",
                "state_dir": "docs/ai/state/main",
                "manifest_path": "docs/ai/state/main/review-manifest.json",
                "manifest_sha256": "0" * 64,
                "gate_status": "fail",
                "active_blocking": ["BP-001"],
                "findings": {"index": []},
                "artifacts": [],
            },
        )
        return root

    def _gate_findings(self, findings):
        return [f for f in findings if "review_gate" in f.get("message", "")]

    def test_failing_gate_warns_without_recorded_floor(self) -> None:
        from agentflow.review import verify_review_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = self._failing_gate_root(tmp)
            findings = verify_review_integrity(root, strict=False)
            gate = self._gate_findings(findings)
            self.assertTrue(gate)
            self.assertTrue(all(f["severity"] == "warning" for f in gate))

    def test_recorded_block_floor_makes_failing_gate_an_error(self) -> None:
        from agentflow.review import verify_review_integrity

        with tempfile.TemporaryDirectory() as tmp:
            root = self._failing_gate_root(tmp)
            findings = verify_review_integrity(
                root,
                strict=False,
                recorded={"review_gate_effective": "block", "proof_strict_effective": True},
            )
            gate = self._gate_findings(findings)
            self.assertTrue(any(f["severity"] == "error" for f in gate))


class RatchetV1AdvisoryWarningTests(unittest.TestCase):
    """Tests for advisory warnings on unrecognized/unknown recorded review policy fields."""

    def _empty_root(self, tmp: str) -> Path:
        from agentflow.execution import init_execution_artifacts

        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        return root

    def _failing_gate_root(self, tmp: str) -> Path:
        # Reuse the ledger setup from VerifyReviewIntegrityRatchetTests.
        from agentflow.artifacts import append_jsonl
        from agentflow.execution import init_execution_artifacts

        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        append_jsonl(
            root / ".agent/review-runs.jsonl",
            {
                "schema_version": "0.3.0",
                "review_run_id": "RR-20260620T180000Z-ab12cd34",
                "recorded_at": "2026-06-20T18:00:00+00:00",
                "state_dir": "docs/ai/state/main",
                "manifest_path": "docs/ai/state/main/review-manifest.json",
                "manifest_sha256": "0" * 64,
                "gate_status": "fail",
                "active_blocking": ["BP-001"],
                "findings": {"index": []},
                "artifacts": [],
            },
        )
        return root

    # Item 1: unrecognized review_gate_effective
    def test_unrecognized_gate_emits_warning_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._empty_root(tmp)
            findings = verify_review_integrity(
                root, strict=False, recorded={"review_gate_effective": "blcok"}
            )
            messages = [f["message"] for f in findings]
            advisory = [f for f in findings if "unrecognized recorded" in f.get("message", "")]
            self.assertTrue(advisory, f"expected advisory finding; got: {messages}")
            self.assertTrue(all(f["severity"] == "warning" for f in advisory))
            self.assertIn("blcok", advisory[0]["message"])

    def test_empty_recorded_gate_emits_warning_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._empty_root(tmp)
            findings = verify_review_integrity(
                root, strict=False, recorded={"review_gate_effective": ""}
            )
            advisory = [f for f in findings if "unrecognized recorded" in f.get("message", "")]
            self.assertTrue(advisory, f"expected advisory finding; got: {findings}")
            self.assertTrue(all(f["severity"] == "warning" for f in advisory))
            self.assertIn("''", advisory[0]["message"])

    def test_unrecognized_gate_floor_not_applied_falls_back_to_caller(self) -> None:
        # With an unrecognized gate, the floor is not applied.
        # Caller resolves to 'warn' (default), so a failing gate is a warning not an error.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._failing_gate_root(tmp)
            findings = verify_review_integrity(
                root, strict=False, recorded={"review_gate_effective": "blcok"}
            )
            gate_findings = [f for f in findings if "review_gate" in f.get("message", "")]
            # Should not be escalated to error (floor was not applied)
            self.assertFalse(
                any(f["severity"] == "error" for f in gate_findings),
                f"floor should not have been applied; gate_findings: {gate_findings}",
            )

    # Item 2: unknown verification_semantics
    def test_unknown_verification_semantics_emits_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._empty_root(tmp)
            findings = verify_review_integrity(
                root,
                strict=False,
                recorded={"review_gate_effective": "block", "verification_semantics": "ratchet-v2"},
            )
            advisory = [
                f for f in findings if "unknown verification_semantics" in f.get("message", "")
            ]
            self.assertTrue(advisory, f"expected semantics advisory; got: {[f['message'] for f in findings]}")
            self.assertTrue(all(f["severity"] == "warning" for f in advisory))
            self.assertIn("ratchet-v2", advisory[0]["message"])

    def test_current_semantics_emits_no_warning(self) -> None:
        # ratchet-v1 (current) must NOT emit the semantics warning.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._failing_gate_root(tmp)
            findings = verify_review_integrity(
                root,
                strict=False,
                recorded={
                    "review_gate_effective": "block",
                    "verification_semantics": "ratchet-v1",
                },
            )
            advisory = [
                f for f in findings if "unknown verification_semantics" in f.get("message", "")
            ]
            self.assertFalse(advisory, f"should not emit semantics warning for v1; got: {advisory}")
            # The block floor must still apply (failing gate -> error).
            gate_findings = [f for f in findings if "review_gate" in f.get("message", "")]
            self.assertTrue(
                any(f["severity"] == "error" for f in gate_findings),
                f"block floor should still apply; gate_findings: {gate_findings}",
            )

    # Item 4: recorded=None emits neither advisory
    def test_none_recorded_emits_no_advisory_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._empty_root(tmp)
            findings = verify_review_integrity(root, strict=False, recorded=None)
            advisory = [
                f
                for f in findings
                if "unrecognized recorded" in f.get("message", "")
                or "unknown verification_semantics" in f.get("message", "")
            ]
            self.assertFalse(advisory, f"no advisory warnings expected for recorded=None; got: {advisory}")

    # Item 5: constant test
    def test_constant_value(self) -> None:
        self.assertEqual(REVIEW_VERIFICATION_SEMANTICS, "ratchet-v1")

    def test_build_time_policy_uses_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir(parents=True)
            policy_block = build_time_review_policy(root, strict=False)
            self.assertEqual(
                policy_block["verification_semantics"],
                REVIEW_VERIFICATION_SEMANTICS,
            )


class RequiredReviewSatisfiedCheckTests(unittest.TestCase):
    """#74: review_checks reports required-vs-recorded review evidence by depth."""

    def _root(self, tmp, *, review_depth=None, wf_proof_policy=None, with_run=False):
        from agentflow.artifacts import append_jsonl
        from agentflow.execution import init_execution_artifacts

        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        if review_depth is not None or wf_proof_policy is not None:
            contract = {}
            if review_depth is not None:
                contract["review_depth"] = review_depth
            if wf_proof_policy is not None:
                contract["proof_policy"] = wf_proof_policy
            (root / ".agent/workflow.contract.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )
        if with_run:
            append_jsonl(
                root / ".agent/review-runs.jsonl",
                {
                    "schema_version": "0.3.0",
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "recorded_at": "2026-06-20T18:00:00+00:00",
                    "state_dir": "docs/ai/state/main",
                    "manifest_path": "docs/ai/state/main/review-manifest.json",
                    "manifest_sha256": "0" * 64,
                    "gate_status": "pass",
                    "active_blocking": [],
                    "findings": {"index": []},
                    "artifacts": [],
                },
            )
        return root

    def _required_check(self, root, strict=False, recorded=None):
        from agentflow.review import review_checks

        summary = review_summary(root)
        matches = [
            c
            for c in review_checks(root, summary, strict, recorded)
            if c["id"] == "required_review_satisfied"
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_deep_without_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._required_check(self._root(tmp, review_depth="deep"))
            self.assertEqual(check["status"], "failed")
            self.assertEqual(check["required_review_depth"], "deep")

    def test_spec_quality_without_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._required_check(self._root(tmp, review_depth="spec_quality"))
            self.assertEqual(check["status"], "failed")

    def test_deep_with_run_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._required_check(self._root(tmp, review_depth="deep", with_run=True))
            self.assertEqual(check["status"], "passed")

    def test_none_passes_without_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._required_check(self._root(tmp, review_depth="none"))
            self.assertEqual(check["status"], "passed")

    def test_light_passes_without_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._required_check(self._root(tmp, review_depth="light"))
            self.assertEqual(check["status"], "passed")

    def test_contract_required_run_warns_when_missing_under_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp, review_depth="light", wf_proof_policy={"require_review_run": True}
            )
            self.assertEqual(self._required_check(root)["status"], "warning")

    def test_no_contract_passes_with_null_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = self._required_check(self._root(tmp))
            self.assertEqual(check["status"], "passed")
            self.assertIsNone(check["required_review_depth"])

    def test_required_run_under_ignore_floor_is_not_run(self):
        # A required-but-missing run under an `ignore` gate floor reports
        # not_run, not a contradictory passed-with-"none recorded".
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp, review_depth="none", wf_proof_policy={"require_review_run": True}
            )
            (root / ".agent/execution.contract.json").write_text(
                json.dumps({"proof_policy": {"review_gate": "ignore"}}), encoding="utf-8"
            )
            summary = review_summary(root)
            checks = review_checks(root, summary)
            self.assertEqual(
                [c for c in checks if c["id"] == "required_review_satisfied"][0]["status"],
                "not_run",
            )
            self.assertEqual(
                [c for c in checks if c["id"] == "review_gate"][0]["status"],
                "not_run",
            )

    def test_legacy_recorded_policy_does_not_require_live_workflow_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, review_depth="deep")
            findings = verify_review_integrity(
                root,
                recorded={
                    "review_gate_effective": "warn",
                    "proof_strict_effective": False,
                    "require_review_run": False,
                    "verification_semantics": "ratchet-v1",
                },
            )
            self.assertFalse(findings)


class RequiredReviewDepthTests(unittest.TestCase):
    def _check(self, tmp, required_depth, run_depths, require_run=True):
        """Return the required_review_satisfied check for synthetic runs."""
        from agentflow.review import review_checks

        root = Path(tmp)
        agent = root / ".agent"
        agent.mkdir(exist_ok=True)
        contract = {"proof_policy": {"require_review_run": require_run}}
        # required_depth=None means "a run is required but no depth floor". Depth
        # "none" resolves to a non-None required_review_depth, so omit review_depth
        # entirely to keep required_review_depth None while require_review_run holds.
        if required_depth is not None:
            contract["review_depth"] = required_depth
        (agent / "workflow.contract.json").write_text(
            json.dumps(contract), encoding="utf-8"
        )
        summary = {
            "review_runs": [
                {"review_run_id": f"RR-2026x-{i:08d}", "gate_status": "pass",
                 "active_blocking": [], "counts_by_severity": {}, "counts_by_status": {},
                 "artifacts": [], "depth_profile": d}
                for i, d in enumerate(run_depths)
            ],
            "unresolved_finding_refs": [],
        }
        checks = review_checks(root, summary, strict=False)
        return next(c for c in checks if c["id"] == "required_review_satisfied")

    def test_deep_requirement_needs_deep_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._check(tmp, "deep", ["spec_quality"])
            self.assertEqual(c["status"], "failed")
            self.assertEqual(c["satisfied_by_depth"], "spec_quality")
            self.assertIn("requires a run at >= deep", c["message"])

    def test_deep_requirement_satisfied_by_deep_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._check(tmp, "deep", ["spec_quality", "deep"])
            self.assertEqual(c["status"], "passed")
            self.assertEqual(c["satisfied_by_depth"], "deep")

    def test_spec_quality_requirement_satisfied_by_spec_quality_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._check(tmp, "spec_quality", ["spec_quality"])
            self.assertEqual(c["status"], "passed")

    def test_absent_recorded_depth_is_legacy_deep(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._check(tmp, "deep", [None])  # old ledger row
            self.assertEqual(c["status"], "passed")
            self.assertEqual(c["satisfied_by_depth"], "deep")

    def test_require_run_without_depth_floor_passes_on_any_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._check(tmp, None, ["light"], require_run=True)
            self.assertEqual(c["status"], "passed")

    def test_require_run_none_depth_no_runs_message_omits_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._check(tmp, None, [], require_run=True)
            self.assertIsNone(c["required_review_depth"])
            self.assertIsNone(c["satisfied_by_depth"])
            self.assertNotIn("None", c["message"])
            self.assertEqual(c["message"], "a review run is required; none recorded")


class ReviewDepthHelperTests(unittest.TestCase):
    def test_rank_orders_the_ladder(self):
        from agentflow.contracts import review_depth_rank
        self.assertLess(review_depth_rank("standard"), review_depth_rank("spec_quality"))
        self.assertLess(review_depth_rank("spec_quality"), review_depth_rank("deep"))

    def test_recorded_absent_is_deep(self):
        from agentflow.contracts import recorded_review_depth
        self.assertEqual(recorded_review_depth(None), "deep")

    def test_recorded_present_unknown_is_malformed(self):
        from agentflow.contracts import recorded_review_depth
        with self.assertRaises(ValueError):
            recorded_review_depth("exhaustive")

    def test_satisfies_none_required_is_any_run(self):
        from agentflow.contracts import review_depth_satisfies
        self.assertTrue(review_depth_satisfies("light", None))

    def test_satisfies_is_monotonic(self):
        from agentflow.contracts import review_depth_satisfies
        self.assertTrue(review_depth_satisfies("deep", "spec_quality"))
        self.assertTrue(review_depth_satisfies("spec_quality", "spec_quality"))
        self.assertFalse(review_depth_satisfies("spec_quality", "deep"))


class ManifestDepthProfileValidationTests(unittest.TestCase):
    def _base(self):
        return {
            "schema_version": "0.2.0",
            "review_run_id": "RR-20260630T000000Z-abcdef01",
            "state_dir": "docs/ai/state/main",
            "gate_status": "pass",
            "active_blocking": [],
            "findings": {"index": []},
            "artifacts": [{"path": "findings-final.json"}],
        }

    def test_absent_depth_profile_is_valid(self):
        from agentflow.review import validate_manifest
        validate_manifest(self._base())  # no raise

    def test_valid_depth_profile_accepted(self):
        from agentflow.review import validate_manifest
        m = self._base()
        m["depth_profile"] = "spec_quality"
        validate_manifest(m)  # no raise

    def test_out_of_enum_depth_profile_rejected(self):
        from agentflow.review import validate_manifest
        m = self._base()
        m["depth_profile"] = "exhaustive"
        with self.assertRaises(ValueError):
            validate_manifest(m)


class LedgerDepthProfileTests(unittest.TestCase):
    def _write_manifest(self, root, depth_profile=None):
        import json
        from pathlib import Path
        create_initial_artifacts(Path(root))
        state = Path(root) / "docs/ai/state/main"
        state.mkdir(parents=True, exist_ok=True)
        (state / "findings-final.json").write_text(
            json.dumps({"findings": []}), encoding="utf-8"
        )
        manifest = {
            "schema_version": "0.2.0",
            "review_run_id": "RR-20260630T000000Z-abcdef01",
            "state_dir": "docs/ai/state/main",
            "policy": "default",
            "gate_status": "pass",
            "active_blocking": [],
            "findings": {"index": []},
            "artifacts": [{"path": "findings-final.json"}],
        }
        if depth_profile is not None:
            manifest["depth_profile"] = depth_profile
        path = state / "review-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_record_defaults_absent_manifest_depth_to_deep(self):
        import tempfile
        from pathlib import Path
        from agentflow.review import build_review_run_record
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest(tmp, depth_profile=None)
            record = build_review_run_record(Path(tmp), manifest_path)
            self.assertEqual(record["depth_profile"], "deep")

    def test_record_copies_declared_depth(self):
        import tempfile
        from pathlib import Path
        from agentflow.review import build_review_run_record
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest(tmp, depth_profile="spec_quality")
            record = build_review_run_record(Path(tmp), manifest_path)
            self.assertEqual(record["depth_profile"], "spec_quality")

    def test_read_review_runs_rejects_out_of_enum_depth(self):
        import tempfile, json
        from pathlib import Path
        from agentflow.review import read_review_runs
        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / ".agent"
            agent.mkdir()
            row = {
                "schema_version": "0.4.0",
                "review_run_id": "RR-20260630T000000Z-abcdef01",
                "recorded_at": "2026-06-30T00:00:00Z",
                "state_dir": "docs/ai/state/main",
                "manifest_path": "docs/ai/state/main/review-manifest.json",
                "manifest_sha256": "0" * 64,
                "gate_status": "pass",
                "artifacts": [],
                "depth_profile": "exhaustive",
            }
            (agent / "review-runs.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_review_runs(Path(tmp))
