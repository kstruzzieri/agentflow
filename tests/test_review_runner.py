from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import subprocess as _subprocess

from agentflow.review import REVIEW_RUN_ID_PATTERN
from agentflow import review_runner
from agentflow import git as af_git


class MintReviewRunIdTest(unittest.TestCase):
    def test_matches_pattern(self) -> None:
        rrid = review_runner.mint_review_run_id()
        self.assertRegex(rrid, REVIEW_RUN_ID_PATTERN)

    def test_two_mints_differ(self) -> None:
        a = review_runner.mint_review_run_id()
        b = review_runner.mint_review_run_id()
        # timestamps may collide within a second; the 8-hex suffix must not.
        self.assertNotEqual(a.rsplit("-", 1)[1], b.rsplit("-", 1)[1])


class LoadFindingsTest(unittest.TestCase):
    def _write(self, payload: Any) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "findings-final.json"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        return tmp

    def test_loads_valid_findings(self) -> None:
        path = self._write({"findings": [
            {"id": "BP-001", "severity": "high", "status": "accepted"},
        ]})
        findings = review_runner.load_findings(path)
        self.assertEqual(findings[0]["id"], "BP-001")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ValueError):
            review_runner.load_findings(Path("/nonexistent/findings-final.json"))

    def test_findings_must_be_list(self) -> None:
        path = self._write({"findings": {"id": "BP-001"}})
        with self.assertRaises(ValueError):
            review_runner.load_findings(path)

    def test_invalid_severity_raises(self) -> None:
        path = self._write({"findings": [
            {"id": "X", "severity": "blocker", "status": "open"},
        ]})
        with self.assertRaises(ValueError):
            review_runner.load_findings(path)

    def test_invalid_status_raises(self) -> None:
        path = self._write({"findings": [
            {"id": "X", "severity": "high", "status": "wontfix"},
        ]})
        with self.assertRaises(ValueError):
            review_runner.load_findings(path)

    def test_missing_id_raises(self) -> None:
        path = self._write({"findings": [
            {"severity": "high", "status": "open"},
        ]})
        with self.assertRaises(ValueError):
            review_runner.load_findings(path)

    def test_duplicate_id_raises(self) -> None:
        path = self._write({"findings": [
            {"id": "A", "severity": "high", "status": "open"},
            {"id": "A", "severity": "medium", "status": "accepted"},
        ]})
        with self.assertRaises(ValueError):
            review_runner.load_findings(path)

    def test_optional_fields_must_be_strings_when_present(self) -> None:
        path = self._write({"findings": [
            {"id": "A", "severity": "high", "status": "open",
             "steelman_verdict": ["confirmed"]},
        ]})
        with self.assertRaises(ValueError):
            review_runner.load_findings(path)


def _sample_config() -> dict:
    return {
        "branch_modifiers": {
            "feat/*": {"gate": "full"},
            "hotfix/*": {"gate": "critical_only"},
            "spike/*": {"gate": "advisory_only"},
            "*": {"gate": "full"},
        },
        "gate_policy": {
            "full": {"blocks_on": ["critical", "high"], "warns_on": ["medium"]},
            "critical_only": {"blocks_on": ["critical"], "warns_on": ["high", "medium"]},
            "advisory_only": {"blocks_on": [], "warns_on": ["critical", "high", "medium"]},
        },
    }


class ResolveGatePolicyTest(unittest.TestCase):
    def test_feat_branch_resolves_to_full(self) -> None:
        name, blocks, warns = review_runner.resolve_gate_policy(
            _sample_config(), "feat/7-evidence")
        self.assertEqual(name, "full")
        self.assertEqual(blocks, ["critical", "high"])
        self.assertEqual(warns, ["medium"])

    def test_hotfix_branch_resolves_to_critical_only(self) -> None:
        name, _, _ = review_runner.resolve_gate_policy(
            _sample_config(), "hotfix/urgent")
        self.assertEqual(name, "critical_only")

    def test_unmatched_branch_falls_back_to_star(self) -> None:
        name, _, _ = review_runner.resolve_gate_policy(
            _sample_config(), "main")
        self.assertEqual(name, "full")

    def test_specific_pattern_beats_star(self) -> None:
        # "spike/*" must win over "*" for a spike branch.
        name, blocks, _ = review_runner.resolve_gate_policy(
            _sample_config(), "spike/idea")
        self.assertEqual(name, "advisory_only")
        self.assertEqual(blocks, [])

    def test_refs_heads_prefix_is_normalized_before_matching(self) -> None:
        name, _, _ = review_runner.resolve_gate_policy(
            _sample_config(), "refs/heads/hotfix/urgent")
        self.assertEqual(name, "critical_only")

    def test_origin_prefix_is_normalized_before_matching(self) -> None:
        config = _sample_config()
        config["branch_modifiers"]["release/*"] = {"gate": "strict_medium_blocks"}
        config["gate_policy"]["strict_medium_blocks"] = {
            "blocks_on": ["critical", "high", "medium"],
            "warns_on": [],
        }
        name, _, _ = review_runner.resolve_gate_policy(config, "origin/release/1.0")
        self.assertEqual(name, "strict_medium_blocks")

    def test_load_policy_config_reads_json(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "config.json"
        tmp.write_text(json.dumps(_sample_config()), encoding="utf-8")
        config = review_runner.load_policy_config(tmp)
        self.assertIn("gate_policy", config)

    def test_load_policy_config_missing_raises(self) -> None:
        with self.assertRaises(ValueError):
            review_runner.load_policy_config(Path("/nonexistent/config.json"))

    def test_unknown_gate_name_raises(self) -> None:
        config = _sample_config()
        config["branch_modifiers"]["feat/*"]["gate"] = "ghost"
        with self.assertRaises(ValueError):
            review_runner.resolve_gate_policy(config, "feat/x")

    def test_modifier_must_be_object(self) -> None:
        config = _sample_config()
        config["branch_modifiers"]["feat/*"] = "full"
        with self.assertRaises(ValueError):
            review_runner.resolve_gate_policy(config, "feat/x")

    def test_policy_lists_must_be_lists(self) -> None:
        config = _sample_config()
        config["gate_policy"]["full"]["blocks_on"] = "critical"
        with self.assertRaises(ValueError):
            review_runner.resolve_gate_policy(config, "feat/x")

    def test_policy_lists_must_use_known_severities(self) -> None:
        config = _sample_config()
        config["gate_policy"]["full"]["warns_on"] = ["blocker"]
        with self.assertRaises(ValueError):
            review_runner.resolve_gate_policy(config, "feat/x")


FULL_BLOCKS = ["critical", "high"]
FULL_WARNS = ["medium"]


class ComputeGateTest(unittest.TestCase):
    def test_active_critical_blocks(self) -> None:
        status, blocking = review_runner.compute_gate(
            [{"id": "A", "severity": "critical", "status": "accepted"}],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "fail")
        self.assertEqual(blocking, ["A"])

    def test_active_medium_warns_under_full(self) -> None:
        status, blocking = review_runner.compute_gate(
            [{"id": "A", "severity": "medium", "status": "open"}],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "warn")
        self.assertEqual(blocking, [])

    def test_rejected_critical_does_not_block(self) -> None:
        status, blocking = review_runner.compute_gate(
            [{"id": "A", "severity": "critical", "status": "rejected"}],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "pass")
        self.assertEqual(blocking, [])

    def test_superseded_and_fixed_excluded(self) -> None:
        status, blocking = review_runner.compute_gate(
            [
                {"id": "A", "severity": "high", "status": "superseded"},
                {"id": "B", "severity": "high", "status": "fixed"},
            ],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "pass")
        self.assertEqual(blocking, [])

    def test_downgraded_finding_blocks_on_final_severity(self) -> None:
        # An accepted finding whose FINAL severity is high still blocks under full.
        status, blocking = review_runner.compute_gate(
            [{"id": "A", "severity": "high", "status": "accepted",
              "steelman_verdict": "downgraded"}],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "fail")
        self.assertEqual(blocking, ["A"])

    def test_advisory_only_critical_warns_not_fails(self) -> None:
        status, blocking = review_runner.compute_gate(
            [{"id": "A", "severity": "critical", "status": "open"}],
            [], ["critical", "high", "medium"])
        self.assertEqual(status, "warn")
        self.assertEqual(blocking, [])

    def test_blocking_preserves_input_order(self) -> None:
        status, blocking = review_runner.compute_gate(
            [
                {"id": "first", "severity": "critical", "status": "open"},
                {"id": "second", "severity": "high", "status": "open"},
            ],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "fail")
        self.assertEqual(blocking, ["first", "second"])

    def test_no_findings_passes(self) -> None:
        status, blocking = review_runner.compute_gate([], FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "pass")
        self.assertEqual(blocking, [])

    def test_low_severity_is_silent_pass_under_full(self) -> None:
        # 'low' is in neither blocks_on nor warns_on for full: no block, no warn.
        status, blocking = review_runner.compute_gate(
            [{"id": "A", "severity": "low", "status": "open"}],
            FULL_BLOCKS, FULL_WARNS)
        self.assertEqual(status, "pass")
        self.assertEqual(blocking, [])


class ProjectFindingsTest(unittest.TestCase):
    def test_counts_cover_all_findings(self) -> None:
        proj = review_runner.project_findings([
            {"id": "A", "severity": "high", "status": "accepted"},
            {"id": "B", "severity": "high", "status": "rejected"},
            {"id": "C", "severity": "medium", "status": "open"},
        ])
        self.assertEqual(proj["counts_by_severity"], {"high": 2, "medium": 1})
        self.assertEqual(
            proj["counts_by_status"], {"accepted": 1, "rejected": 1, "open": 1})

    def test_index_rows_minimal_shape(self) -> None:
        proj = review_runner.project_findings([
            {"id": "A", "severity": "low", "status": "open"},
        ])
        self.assertEqual(
            proj["index"], [{"finding_id": "A", "severity": "low", "status": "open"}])

    def test_index_carries_optional_fields_when_present(self) -> None:
        proj = review_runner.project_findings([
            {"id": "A", "severity": "high", "status": "superseded",
             "superseded_by": "B", "steelman_verdict": "superseded", "fix_commit": ""},
        ])
        row = proj["index"][0]
        self.assertEqual(row["superseded_by"], "B")
        self.assertEqual(row["steelman_verdict"], "superseded")
        self.assertNotIn("fix_commit", row)  # empty optional fields are omitted


class BuildArtifactsTest(unittest.TestCase):
    def _state_dir(self, names: list) -> Path:
        d = Path(tempfile.mkdtemp())
        for name in names:
            (d / name).write_text("x", encoding="utf-8")
        return d

    def test_lists_only_present_known_artifacts_in_fixed_order(self) -> None:
        d = self._state_dir([
            "findings-final.json", "findings-final.yaml", "gate.yaml",
            "synthesis.md", "unrelated.txt",
        ])
        artifacts = review_runner.build_artifacts(d)
        paths = [a["path"] for a in artifacts]
        self.assertEqual(
            paths,
            ["findings-final.json", "findings-final.yaml", "synthesis.md", "gate.yaml"],
        )
        self.assertNotIn("unrelated.txt", paths)

    def test_includes_optional_passes_when_present(self) -> None:
        d = self._state_dir([
            "findings-final.json", "findings-final.yaml", "findings-bp.yaml",
            "findings-adv.yaml", "synthesis.md", "gate.yaml", "ready-for-pr.md",
        ])
        paths = [a["path"] for a in review_runner.build_artifacts(d)]
        self.assertEqual(paths, [
            "findings-final.json", "findings-final.yaml", "findings-bp.yaml",
            "findings-adv.yaml", "synthesis.md", "gate.yaml", "ready-for-pr.md",
        ])

    def test_missing_required_artifact_raises(self) -> None:
        d = self._state_dir(["findings-final.json", "findings-final.yaml", "gate.yaml"])
        with self.assertRaises(ValueError):
            review_runner.build_artifacts(d)


class BuildManifestTest(unittest.TestCase):
    def test_assembles_valid_manifest(self) -> None:
        projection = review_runner.project_findings([
            {"id": "A", "severity": "high", "status": "accepted"},
        ])
        manifest = review_runner.build_manifest(
            review_run_id="RR-20260622T101010Z-0a1b2c3d",
            state_dir="docs/ai/state/feat-7",
            policy_name="full",
            gate_status="fail",
            active_blocking=["A"],
            projection=projection,
            artifacts=[{"path": "findings-final.json"}],
        )
        self.assertEqual(manifest["schema_version"], review_runner.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest["state_dir"], "docs/ai/state/feat-7")
        self.assertEqual(manifest["policy"], "full")
        self.assertEqual(manifest["gate_status"], "fail")
        self.assertEqual(manifest["active_blocking"], ["A"])
        # must satisfy the verifier's contract validator
        from agentflow.review import validate_manifest as vm
        vm(manifest)  # raises on any contract violation


class CurrentBranchTest(unittest.TestCase):
    def _init_repo(self, branch: str) -> Path:
        root = Path(tempfile.mkdtemp())
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        import os as _os
        full_env = {**_os.environ, **env}
        _subprocess.run(["git", "init", "-q", str(root)], check=True, env=full_env)
        _subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", branch],
                        check=True, env=full_env)
        (root / "f.txt").write_text("x", encoding="utf-8")
        _subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=full_env)
        _subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                        check=True, env=full_env)
        return root

    def test_returns_checked_out_branch(self) -> None:
        root = self._init_repo("feat/7-evidence")
        self.assertEqual(af_git.current_branch(root), "feat/7-evidence")

    def test_returns_none_outside_repo(self) -> None:
        self.assertIsNone(af_git.current_branch(Path(tempfile.mkdtemp())))


class ProduceManifestTest(unittest.TestCase):
    def _root_with(self, branch_dir: str, findings: list) -> Path:
        root = Path(tempfile.mkdtemp())
        state = root / "docs/ai/state" / branch_dir
        state.mkdir(parents=True)
        (state / "findings-final.json").write_text(
            json.dumps({"findings": findings}), encoding="utf-8")
        (state / "findings-final.yaml").write_text("findings: []\n", encoding="utf-8")
        (state / "synthesis.md").write_text("# Synthesis\n", encoding="utf-8")
        (state / "gate.yaml").write_text("status: pass\n", encoding="utf-8")
        (root / "config.json").write_text(json.dumps(_sample_config()), encoding="utf-8")
        return root

    def test_produces_manifest_with_resolved_gate(self) -> None:
        root = self._root_with(
            "feat-7", [{"id": "A", "severity": "high", "status": "accepted"}])
        manifest = review_runner.produce_manifest(
            root=root,
            state_dir="docs/ai/state/feat-7",
            branch="feat/7-evidence",
            findings_json=None,
            config_path=root / "config.json",
        )
        self.assertEqual(manifest["gate_status"], "fail")
        self.assertEqual(manifest["active_blocking"], ["A"])
        self.assertEqual(manifest["state_dir"], "docs/ai/state/feat-7")
        self.assertEqual(manifest["policy"], "full")

    def test_state_dir_escaping_root_raises(self) -> None:
        root = self._root_with("feat-7", [])
        with self.assertRaises(ValueError):
            review_runner.produce_manifest(
                root=root, state_dir="../escape", branch="feat/x",
                findings_json=None, config_path=root / "config.json")

    def test_relative_findings_json_resolves_under_state_dir(self) -> None:
        root = self._root_with(
            "feat-7", [{"id": "A", "severity": "medium", "status": "open"}])
        manifest = review_runner.produce_manifest(
            root=root,
            state_dir="docs/ai/state/feat-7",
            branch="feat/7-evidence",
            findings_json="findings-final.json",
            config_path=root / "config.json",
        )
        self.assertEqual(manifest["gate_status"], "warn")

    def test_relative_findings_json_parent_traversal_raises(self) -> None:
        root = self._root_with("feat-7", [])
        state_parent = root / "docs/ai/state"
        (state_parent / "other.json").write_text(
            json.dumps({"findings": [
                {"id": "A", "severity": "high", "status": "open"},
            ]}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            review_runner.produce_manifest(
                root=root,
                state_dir="docs/ai/state/feat-7",
                branch="feat/7-evidence",
                findings_json="../other.json",
                config_path=root / "config.json",
            )

    def test_custom_findings_json_is_recorded_as_artifact(self) -> None:
        root = self._root_with(
            "feat-7", [{"id": "LOW", "severity": "low", "status": "open"}])
        state = root / "docs/ai/state/feat-7"
        (state / "custom.json").write_text(
            json.dumps({"findings": [
                {"id": "HIGH", "severity": "high", "status": "open"},
            ]}),
            encoding="utf-8",
        )
        manifest = review_runner.produce_manifest(
            root=root,
            state_dir="docs/ai/state/feat-7",
            branch="feat/7-evidence",
            findings_json="custom.json",
            config_path=root / "config.json",
        )
        self.assertEqual(manifest["gate_status"], "fail")
        self.assertEqual(manifest["findings"]["index"][0]["finding_id"], "HIGH")
        self.assertIn(
            {"path": "custom.json"},
            manifest["artifacts"],
        )


class ExitCodeTest(unittest.TestCase):
    def _manifest(self, gate_status: str, blocking: list) -> dict:
        return {"gate_status": gate_status, "active_blocking": blocking}

    def test_default_is_zero_even_on_warn(self) -> None:
        m = self._manifest("warn", [])
        self.assertEqual(review_runner.exit_code_for(m, False, False), 0)

    def test_fail_on_block_nonzero_when_blocking(self) -> None:
        m = self._manifest("fail", ["A"])
        self.assertEqual(review_runner.exit_code_for(m, True, False), 1)

    def test_fail_on_block_zero_when_warn(self) -> None:
        m = self._manifest("warn", [])
        self.assertEqual(review_runner.exit_code_for(m, True, False), 0)

    def test_strict_exit_nonzero_on_warn(self) -> None:
        m = self._manifest("warn", [])
        self.assertEqual(review_runner.exit_code_for(m, False, True), 1)

    def test_strict_exit_does_not_mutate_manifest(self) -> None:
        m = self._manifest("warn", [])
        review_runner.exit_code_for(m, False, True)
        self.assertEqual(m["gate_status"], "warn")  # unchanged


class StateDirHintTests(unittest.TestCase):
    def test_relative_path_gets_base_hint(self):
        from agentflow.review_runner import _state_dir_hint
        hint = _state_dir_hint("findings-final.json", "docs/ai/state/main")
        self.assertEqual(hint, " (--findings-json is resolved relative to --state-dir)")

    def test_doubled_path_suggests_stripped_remainder(self):
        from agentflow.review_runner import _state_dir_hint
        hint = _state_dir_hint(
            "docs/ai/state/main/findings-final.json", "docs/ai/state/main"
        )
        self.assertIn("did you mean --findings-json findings-final.json?", hint)

    def test_sibling_prefix_is_not_treated_as_doubling(self):
        from agentflow.review_runner import _state_dir_hint
        hint = _state_dir_hint(
            "docs/ai/state/main2/findings-final.json", "docs/ai/state/main"
        )
        self.assertNotIn("did you mean", hint)

    def test_absolute_and_default_get_no_hint(self):
        from agentflow.review_runner import _state_dir_hint
        self.assertIsNone(_state_dir_hint("/abs/findings-final.json", "docs/ai/state/main"))
        self.assertIsNone(_state_dir_hint(None, "docs/ai/state/main"))


class FindingsJsonNotFoundTests(unittest.TestCase):
    def test_doubled_relative_path_raises_with_suggestion(self):
        import tempfile
        from pathlib import Path
        from agentflow.review_runner import produce_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "docs/ai/state/main"
            state.mkdir(parents=True)
            (root / "docs/ai/config.json").parent.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError) as ctx:
                produce_manifest(
                    root=root,
                    state_dir="docs/ai/state/main",
                    branch="main",
                    findings_json="docs/ai/state/main/findings-final.json",
                    config_path=root / "docs/ai/config.json",
                )
            msg = str(ctx.exception)
            self.assertIn("did you mean --findings-json findings-final.json?", msg)


class DepthProfileArtifactTests(unittest.TestCase):
    def _scaffold(self, tmp, artifacts):
        from pathlib import Path
        import json
        root = Path(tmp)
        state = root / "docs/ai/state/main"
        state.mkdir(parents=True)
        (state / "findings-final.json").write_text(
            json.dumps({"findings": []}), encoding="utf-8"
        )
        for name in artifacts:
            (state / name).write_text("x", encoding="utf-8")
        cfg = root / "docs/ai/config.json"
        cfg.write_text(json.dumps({
            "branch_modifiers": {"*": {"gate": "default"}},
            "gate_policy": {"default": {"blocks_on": ["high"], "warns_on": ["medium"]}},
        }), encoding="utf-8")
        return root, cfg

    def test_deep_requires_full_four_pass(self):
        import tempfile
        from agentflow.review_runner import produce_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg = self._scaffold(tmp, [])  # only findings-final.json present
            with self.assertRaises(ValueError):
                produce_manifest(root, "docs/ai/state/main", "main", None, cfg,
                                 depth_profile="deep")

    def test_spec_quality_needs_only_gate_yaml(self):
        import tempfile
        from agentflow.review_runner import produce_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg = self._scaffold(tmp, ["gate.yaml"])
            manifest = produce_manifest(root, "docs/ai/state/main", "main", None, cfg,
                                        depth_profile="spec_quality")
            self.assertEqual(manifest["depth_profile"], "spec_quality")

    def test_standard_needs_sidecar_only(self):
        import tempfile
        from agentflow.review_runner import produce_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg = self._scaffold(tmp, [])
            manifest = produce_manifest(root, "docs/ai/state/main", "main", None, cfg,
                                        depth_profile="standard")
            self.assertEqual(manifest["depth_profile"], "standard")

    def test_default_depth_is_deep(self):
        import tempfile
        from agentflow.review_runner import produce_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg = self._scaffold(tmp, ["findings-final.yaml", "synthesis.md", "gate.yaml"])
            manifest = produce_manifest(root, "docs/ai/state/main", "main", None, cfg)
            self.assertEqual(manifest["depth_profile"], "deep")

    def test_unknown_depth_profile_fails_closed(self):
        import tempfile
        from agentflow.review_runner import produce_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root, cfg = self._scaffold(tmp, ["gate.yaml"])
            with self.assertRaises(ValueError):
                produce_manifest(root, "docs/ai/state/main", "main", None, cfg,
                                 depth_profile="exhaustive")


if __name__ == "__main__":
    unittest.main()
