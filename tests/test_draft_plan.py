"""Unit tests for the draft-plan compiler core (issue #71).

These cover the pure functions only: brief/objective validation, lower-bound
posture extraction from a recommendation, least-strict satisfying profile
selection (plus override / ambiguity / no-match diagnostics), adequacy and
fail-closed checks, and template hydration into an unlocked, validate_plan-valid
draft. CLI/end-to-end behavior lives in test_cli.py.
"""

import copy
import tempfile
import unittest
from pathlib import Path

from agentflow import draft_plan
from agentflow.draft_plan import DraftPlanError
from agentflow.validation import validate_plan


# --- fixtures ---------------------------------------------------------------


def brief(**overrides):
    base = {
        "schema_version": "0.1.0",
        "task_type": "bugfix",
        "declared_risk": "low",
        "security_sensitive": False,
        "candidate_files": ["src/agentflow/receipts.py"],
        "blast_radius": "local",
        "validation_needs": ["unit-tests"],
        "declared_size": "s",
    }
    base.update(overrides)
    return base


def _template(steps, gates):
    return {
        "schema_version": "0.3.0",
        "objective": "TODO: describe the change",
        "scope": ["src/", "tests/"],
        "non_goals": ["No new runtime dependencies"],
        "invariants": ["Standard library only"],
        "allowed_files": ["src/", "tests/"],
        "blocked_files": [],
        "validation_gates": list(gates),
        "rollback_plan": "Revert the branch with git restore .",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [
            {
                "id": f"P{i + 1}",
                "action": f"TODO step {i + 1}",
                "files": ["src/"],
                "preconditions": [],
                "expected_diff": [],
                "validation": ["unit-tests"] if gates else ["docs-build"],
                "evidence_ids": [],
                **({"depends_on": [f"P{i}"]} if i else {}),
            }
            for i in range(steps)
        ],
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def _profile(profile_id, review, hunk, rrr, gates, caps, template):
    return {
        "id": profile_id,
        "review_depth": review,
        "required_capabilities": [{"id": c, "required": True} for c in caps],
        "validation_policy": {"required_gates": list(gates)},
        "proof_policy": {"hunk_attribution": hunk, "require_review_run": rrr},
        "plan_template": template,
    }


def manifest():
    """A pack with a range of profiles to exercise least-strict selection."""
    return {
        "schema_version": "0.1.0",
        "id": "demo-pack",
        "name": "Demo Pack",
        "description": "Fixture pack for draft-plan tests.",
        "plan_templates": {
            "one-step": _template(1, ["unit-tests"]),
            "two-step": _template(2, ["unit-tests"]),
            "one-step-docs": _template(1, ["docs-build"]),
            "two-step-secure": _template(2, ["unit-tests", "security-scan"]),
        },
        "profiles": [
            _profile("docs", "none", "observe", False, [], [], "one-step-docs"),
            _profile("light-fix", "light", "enforce", False, ["unit-tests"], [], "one-step"),
            _profile("tdd", "standard", "enforce", False, ["unit-tests"], [], "two-step"),
            _profile(
                "secure",
                "deep",
                "enforce",
                True,
                ["unit-tests", "security-scan"],
                ["security-review"],
                "two-step-secure",
            ),
        ],
    }


# --- lower-bound extraction -------------------------------------------------


class LowerBoundTests(unittest.TestCase):
    def test_extracts_posture_from_small_bugfix(self):
        from agentflow.recommend import recommend

        report = recommend(brief())
        lb = draft_plan.lower_bound_from_recommendation(report)
        self.assertEqual(lb["required_gates"], {"unit-tests"})
        self.assertEqual(lb["required_capabilities"], set())
        self.assertEqual(lb["review_depth"], "light")
        self.assertEqual(lb["hunk_attribution"], "enforce")
        self.assertIs(lb["require_review_run"], False)

    def test_extracts_high_risk_posture(self):
        from agentflow.recommend import recommend

        report = recommend(brief(declared_risk="high"))
        lb = draft_plan.lower_bound_from_recommendation(report)
        self.assertIn("security-scan", lb["required_gates"])
        self.assertEqual(lb["required_capabilities"], {"security-review"})
        self.assertEqual(lb["review_depth"], "deep")
        self.assertIs(lb["require_review_run"], True)


# --- satisfaction + strictness ----------------------------------------------


class SatisfactionTests(unittest.TestCase):
    def setUp(self):
        self.m = manifest()
        self.lb_small = {
            "required_gates": {"unit-tests"},
            "required_capabilities": set(),
            "review_depth": "light",
            "hunk_attribution": "enforce",
            "require_review_run": False,
        }

    def _p(self, pid):
        return next(p for p in self.m["profiles"] if p["id"] == pid)

    def test_satisfies_when_equal_or_stronger(self):
        for pid in ("light-fix", "tdd", "secure"):
            self.assertTrue(
                draft_plan.profile_satisfies(self._p(pid), self.m, self.lb_small),
                pid,
            )

    def test_fails_when_gates_missing(self):
        self.assertFalse(
            draft_plan.profile_satisfies(self._p("docs"), self.m, self.lb_small)
        )

    def test_fails_when_review_too_shallow(self):
        lb = dict(self.lb_small, review_depth="deep")
        self.assertFalse(
            draft_plan.profile_satisfies(self._p("light-fix"), self.m, lb)
        )

    def test_fails_when_capability_missing(self):
        lb = dict(self.lb_small, required_capabilities={"security-review"})
        self.assertFalse(draft_plan.profile_satisfies(self._p("tdd"), self.m, lb))

    def test_fails_when_review_run_not_required_by_profile(self):
        lb = dict(self.lb_small, require_review_run=True)
        self.assertFalse(draft_plan.profile_satisfies(self._p("tdd"), self.m, lb))

    def test_deep_profile_satisfies_review_run_requirement_even_without_flag(self):
        profile = _profile(
            "deep-no-flag",
            "deep",
            "enforce",
            False,
            ["unit-tests", "security-scan"],
            ["security-review"],
            "two-step-secure",
        )
        manifest_with_profile = copy.deepcopy(self.m)
        manifest_with_profile["profiles"].append(profile)
        lb = dict(
            self.lb_small,
            required_gates={"unit-tests", "security-scan"},
            required_capabilities={"security-review"},
            review_depth="deep",
            require_review_run=True,
        )
        self.assertTrue(
            draft_plan.profile_satisfies(profile, manifest_with_profile, lb)
        )

    def test_strictness_orders_light_below_deep(self):
        self.assertLess(
            draft_plan.profile_strictness(self._p("light-fix")),
            draft_plan.profile_strictness(self._p("secure")),
        )


# --- profile selection ------------------------------------------------------


class SelectProfileTests(unittest.TestCase):
    def setUp(self):
        self.m = manifest()

    def _lb(self, **kw):
        base = {
            "required_gates": {"unit-tests"},
            "required_capabilities": set(),
            "review_depth": "light",
            "hunk_attribution": "enforce",
            "require_review_run": False,
        }
        base.update(kw)
        return base

    def test_picks_least_strict_satisfying_profile(self):
        profile, mode = draft_plan.select_profile(self.m, self._lb())
        self.assertEqual(profile["id"], "light-fix")
        self.assertEqual(mode, draft_plan.SELECTION_MODE)

    def test_no_satisfying_profile_raises(self):
        lb = self._lb(required_capabilities={"security-review"}, review_depth="deep")
        small = {
            "schema_version": "0.1.0",
            "id": "small",
            "name": "Small",
            "description": "x",
            "plan_templates": {"one-step-docs": _template(1, ["docs-build"])},
            "profiles": [
                _profile("docs", "none", "observe", False, [], [], "one-step-docs")
            ],
        }
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.select_profile(small, lb)
        self.assertEqual(ctx.exception.code, "no_satisfying_profile")

    def test_ambiguous_tie_raises(self):
        tie = {
            "schema_version": "0.1.0",
            "id": "tie",
            "name": "Tie",
            "description": "x",
            "plan_templates": {"one-step": _template(1, ["unit-tests"])},
            "profiles": [
                _profile("a", "light", "enforce", False, ["unit-tests"], [], "one-step"),
                _profile("b", "light", "enforce", False, ["unit-tests"], [], "one-step"),
            ],
        }
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.select_profile(tie, self._lb())
        self.assertEqual(ctx.exception.code, "ambiguous_profile")

    def test_explicit_profile_that_satisfies(self):
        profile, mode = draft_plan.select_profile(self.m, self._lb(), profile_id="tdd")
        self.assertEqual(profile["id"], "tdd")
        self.assertEqual(mode, "operator_selected")

    def test_explicit_weaker_profile_without_reason_fails(self):
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.select_profile(self.m, self._lb(), profile_id="docs")
        self.assertEqual(ctx.exception.code, "profile_weaker_than_recommended")

    def test_explicit_weaker_profile_with_reason_allowed(self):
        profile, mode = draft_plan.select_profile(
            self.m, self._lb(), profile_id="docs", reason="operator accepts lighter posture"
        )
        self.assertEqual(profile["id"], "docs")
        self.assertEqual(mode, "operator_override")

    def test_unknown_explicit_profile_fails(self):
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.select_profile(self.m, self._lb(), profile_id="ghost")
        self.assertEqual(ctx.exception.code, "unknown_profile")


# --- adequacy / fail-closed -------------------------------------------------


class CompileTests(unittest.TestCase):
    def test_happy_path_multi_step_valid_unlocked(self):
        # feature task_type avoids candidate-file existence checks.
        result = draft_plan.compile_draft_plan(
            brief(task_type="feature", declared_risk="medium", validation_needs=["lint"]),
            manifest(),
            objective="Fix the receipt id race",
            root=Path("."),
        )
        plan = result["plan"]
        self.assertEqual(validate_plan(plan), [])
        self.assertIs(plan["locked"], False)
        self.assertIsNone(plan["locked_at"])
        self.assertEqual(plan["objective"], "Fix the receipt id race")
        self.assertGreaterEqual(len(plan["steps"]), 2)
        # gates union template + profile + brief needs
        self.assertIn("unit-tests", plan["validation_gates"])
        self.assertIn("lint", plan["validation_gates"])
        # .agent/ and candidate files folded into allowed_files
        self.assertIn(".agent/", plan["allowed_files"])
        self.assertIn("src/agentflow/receipts.py", plan["allowed_files"])
        # workflow extension block, pointer/provenance only
        wf = plan["workflow"]
        self.assertEqual(wf["contract_path"], ".agent/workflow.contract.json")
        self.assertEqual(wf["workflow_pack"], "demo-pack")
        self.assertIn("workflow_profile", wf)
        self.assertIn("recommended_profile", wf)
        self.assertNotIn("review_depth", wf)  # policy lives in the contract, not the plan

    def test_one_step_for_small_bugfix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src/agentflow/receipts.py"
            target.parent.mkdir(parents=True)
            target.write_text("x", encoding="utf-8")
            result = draft_plan.compile_draft_plan(
                brief(), manifest(), objective="Fix bug", root=root
            )
        self.assertEqual(result["profile"]["id"], "light-fix")
        self.assertEqual(len(result["plan"]["steps"]), 1)

    def test_risk_level_is_max_of_template_and_declared(self):
        result = draft_plan.compile_draft_plan(
            brief(task_type="feature", declared_risk="high"),
            manifest(),
            objective="x",
            root=Path("."),
        )
        # declared high outranks the template's low risk
        self.assertEqual(result["plan"]["risk_level"], "high")

    def test_missing_objective_is_too_vague(self):
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.compile_draft_plan(
                brief(task_type="feature"), manifest(), objective="   ", root=Path(".")
            )
        self.assertEqual(ctx.exception.code, "brief_too_vague")

    def test_invalid_brief_rejected(self):
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.compile_draft_plan(
                {"schema_version": "0.1.0", "task_type": "nope", "declared_risk": "low"},
                manifest(),
                objective="x",
                root=Path("."),
            )
        self.assertEqual(ctx.exception.code, "invalid_brief")

    def test_broad_without_candidate_files_is_too_vague(self):
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.compile_draft_plan(
                brief(task_type="feature", blast_radius="cross_cutting", candidate_files=[]),
                manifest(),
                objective="big sweeping change",
                root=Path("."),
            )
        self.assertEqual(ctx.exception.code, "brief_too_vague")

    def test_missing_candidate_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DraftPlanError) as ctx:
                draft_plan.compile_draft_plan(
                    brief(candidate_files=["src/agentflow/ghost.py"]),
                    manifest(),
                    objective="x",
                    root=Path(tmp),
                )
        self.assertEqual(ctx.exception.code, "candidate_file_missing")

    def test_allow_missing_candidates_downgrades_to_warning(self):
        # #89: greenfield planning compiles a plan for files not yet created;
        # the missing-candidate error becomes a returned warning instead.
        with tempfile.TemporaryDirectory() as tmp:
            result = draft_plan.compile_draft_plan(
                brief(candidate_files=["src/agentflow/ghost.py"]),
                manifest(),
                objective="x",
                root=Path(tmp),
                allow_missing_candidates=True,
            )
        self.assertEqual(validate_plan(result["plan"]), [])
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("candidate_file_missing", codes)
        # the missing file is still scoped into allowed_files so the work can land
        self.assertIn("src/agentflow/ghost.py", result["plan"]["allowed_files"])

    def test_no_warning_when_candidates_exist_under_allow_flag(self):
        # The flag only downgrades genuinely-missing files; present files warn-free.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src/agentflow/receipts.py"
            target.parent.mkdir(parents=True)
            target.write_text("x", encoding="utf-8")
            result = draft_plan.compile_draft_plan(
                brief(), manifest(), objective="fix", root=root,
                allow_missing_candidates=True,
            )
        self.assertEqual(result["warnings"], [])

    def test_happy_path_has_empty_warnings(self):
        # Every successful compile exposes a warnings list (empty by default).
        result = draft_plan.compile_draft_plan(
            brief(task_type="feature"), manifest(), objective="x", root=Path(".")
        )
        self.assertEqual(result["warnings"], [])

    def test_unsafe_candidate_still_fails_under_allow_missing(self):
        # --allow-missing-candidates downgrades absence, never a path escape.
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.compile_draft_plan(
                brief(task_type="feature", candidate_files=["../../etc/passwd"]),
                manifest(),
                objective="x",
                root=Path("."),
                allow_missing_candidates=True,
            )
        self.assertEqual(ctx.exception.code, "candidate_file_unsafe")

    def test_feature_skips_candidate_file_existence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = draft_plan.compile_draft_plan(
                brief(task_type="feature", candidate_files=["src/agentflow/new_thing.py"]),
                manifest(),
                objective="add new thing",
                root=Path(tmp),
            )
        self.assertEqual(validate_plan(result["plan"]), [])

    def test_decomposition_required_when_broad_and_single_step(self):
        # large-feature posture but only a one-step deep template available.
        m = {
            "schema_version": "0.1.0",
            "id": "thin",
            "name": "Thin",
            "description": "x",
            "plan_templates": {"one-step": _template(1, ["unit-tests"])},
            "profiles": [
                _profile("deep1", "deep", "enforce", True, ["unit-tests"], [], "one-step")
            ],
        }
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.compile_draft_plan(
                brief(task_type="feature", blast_radius="cross_cutting", candidate_files=["src/a.py"]),
                m,
                objective="broad change",
                root=Path("."),
            )
        self.assertEqual(ctx.exception.code, "decomposition_required")

    def test_task_too_large_for_xl_single_step(self):
        m = {
            "schema_version": "0.1.0",
            "id": "thin",
            "name": "Thin",
            "description": "x",
            "plan_templates": {"one-step": _template(1, ["unit-tests"])},
            "profiles": [
                _profile("deep1", "deep", "enforce", True, ["unit-tests"], [], "one-step")
            ],
        }
        with self.assertRaises(DraftPlanError) as ctx:
            draft_plan.compile_draft_plan(
                brief(task_type="feature", declared_size="xl", candidate_files=["src/a.py"]),
                m,
                objective="huge change",
                root=Path("."),
            )
        self.assertEqual(ctx.exception.code, "task_too_large")

    def test_deterministic(self):
        b = brief(task_type="feature", declared_risk="medium")
        a = draft_plan.compile_draft_plan(b, manifest(), objective="x", root=Path("."))
        c = draft_plan.compile_draft_plan(
            copy.deepcopy(b), manifest(), objective="x", root=Path(".")
        )
        self.assertEqual(a["plan"], c["plan"])

    def test_unsafe_candidate_file_rejected(self):
        # Brief-controlled paths must not escape the repo or land in allowed_files.
        for bad in ["../../etc/passwd", "/abs/path", "a/../b"]:
            with self.subTest(path=bad), self.assertRaises(DraftPlanError) as ctx:
                draft_plan.compile_draft_plan(
                    brief(task_type="feature", candidate_files=[bad]),
                    manifest(),
                    objective="x",
                    root=Path("."),
                )
            self.assertEqual(ctx.exception.code, "candidate_file_unsafe")

    def test_symlink_candidate_file_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "escape").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(DraftPlanError) as ctx:
                draft_plan.compile_draft_plan(
                    brief(task_type="feature", candidate_files=["escape/new.py"]),
                    manifest(),
                    objective="x",
                    root=root,
                )
        self.assertEqual(ctx.exception.code, "candidate_file_unsafe")


class ReviewStateScopeTests(unittest.TestCase):
    """#90: scope the review-state path into allowed_files when the selected
    profile requires a review run (mirroring the proof-time condition)."""

    def _p(self, **kw):
        base = dict(
            profile_id="x", review="standard", hunk="enforce", rrr=False,
            gates=["unit-tests"], caps=[], template="two-step",
        )
        base.update(kw)
        return _profile(
            base["profile_id"], base["review"], base["hunk"], base["rrr"],
            base["gates"], base["caps"], base["template"],
        )

    def test_helper_true_when_proof_policy_requires_run(self):
        self.assertTrue(draft_plan._profile_requires_review_run(self._p(rrr=True)))

    def test_helper_true_for_deep_even_without_explicit_flag(self):
        # review_depth deep implies a required run at proof time even if the
        # profile's proof_policy.require_review_run is False.
        self.assertTrue(
            draft_plan._profile_requires_review_run(self._p(review="deep", rrr=False))
        )

    def test_helper_true_for_spec_quality(self):
        self.assertTrue(
            draft_plan._profile_requires_review_run(self._p(review="spec_quality", rrr=False))
        )

    def test_helper_false_for_standard_without_flag(self):
        self.assertFalse(
            draft_plan._profile_requires_review_run(self._p(review="standard", rrr=False))
        )

    def test_compile_scopes_state_path_for_review_run_profile(self):
        result = draft_plan.compile_draft_plan(
            brief(task_type="feature", declared_risk="high"),
            manifest(), objective="secure change", root=Path("."),
        )
        self.assertEqual(result["profile"]["id"], "secure")
        self.assertIn(draft_plan.REVIEW_STATE_PATH, result["plan"]["allowed_files"])
        self.assertEqual(validate_plan(result["plan"]), [])

    def test_compile_omits_state_path_for_non_review_run_profile(self):
        result = draft_plan.compile_draft_plan(
            brief(task_type="feature", declared_risk="medium"),
            manifest(), objective="ordinary change", root=Path("."),
        )
        self.assertNotIn(draft_plan.REVIEW_STATE_PATH, result["plan"]["allowed_files"])


class BroadAndOrderTests(unittest.TestCase):
    def test_is_broad_true_for_high_risk_broad(self):
        from agentflow.recommend import recommend

        report = recommend(
            brief(
                task_type="feature",
                declared_risk="high",
                blast_radius="cross_cutting",
                candidate_files=["src/a.py"],
            )
        )
        self.assertEqual(report["recommended"]["profile"], "high-risk")
        self.assertTrue(draft_plan.is_broad(report))

    def test_is_broad_false_for_bounded_high_risk(self):
        from agentflow.recommend import recommend

        report = recommend(
            brief(
                task_type="feature",
                declared_risk="high",
                blast_radius="local",
                declared_size="s",
                candidate_files=["src/a.py"],
            )
        )
        self.assertEqual(report["recommended"]["profile"], "high-risk")
        self.assertFalse(draft_plan.is_broad(report))

    def test_review_depth_order_places_spec_quality_between_standard_and_deep(self):
        order = draft_plan.REVIEW_DEPTH_ORDER
        self.assertLess(order["standard"], order["spec_quality"])
        self.assertLess(order["spec_quality"], order["deep"])


if __name__ == "__main__":
    unittest.main()
