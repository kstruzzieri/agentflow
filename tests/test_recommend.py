"""Tests for the deterministic workflow recommender (#70)."""

import json
import unittest
from pathlib import Path

from agentflow import recommend as rec
from agentflow.workflow_contract import validate_workflow_contract

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _brief(**overrides):
    base = {"schema_version": "0.1.0", "task_type": "bugfix", "declared_risk": "low"}
    base.update(overrides)
    return base


class ValidateBriefTests(unittest.TestCase):
    def test_accepts_minimal_valid_brief(self) -> None:
        self.assertEqual(rec.validate_brief(_brief()), [])

    def test_rejects_non_object(self) -> None:
        self.assertEqual(
            rec.validate_brief(["not", "a", "dict"]), ["task brief must be a JSON object"]
        )

    def test_reports_missing_required_fields(self) -> None:
        errors = rec.validate_brief({"schema_version": "0.1.0"})
        self.assertIn("missing required task brief field: task_type", errors)
        self.assertIn("missing required task brief field: declared_risk", errors)

    def test_rejects_unknown_field(self) -> None:
        self.assertIn("unknown task brief field: surprise", rec.validate_brief(_brief(surprise=1)))

    def test_rejects_bad_enums(self) -> None:
        errors = rec.validate_brief(_brief(task_type="chore", declared_risk="extreme"))
        self.assertTrue(any("task_type must be one of" in e for e in errors))
        self.assertTrue(any("declared_risk must be one of" in e for e in errors))

    def test_rejects_bad_optional_types(self) -> None:
        errors = rec.validate_brief(
            _brief(
                security_sensitive="yes",
                candidate_files="a.py",
                validation_needs=[""],
                blast_radius="huge",
                declared_size="enormous",
            )
        )
        self.assertTrue(any("security_sensitive must be boolean" in e for e in errors))
        self.assertTrue(any("candidate_files must be an array of non-empty strings" in e for e in errors))
        self.assertTrue(any("validation_needs must be an array of non-empty strings" in e for e in errors))
        self.assertTrue(any("blast_radius must be one of" in e for e in errors))
        self.assertTrue(any("declared_size must be one of" in e for e in errors))


class ClassifyTests(unittest.TestCase):
    def test_catalog_is_rank_ordered_and_indexed(self) -> None:
        self.assertEqual([a.rank for a in rec.ARCHETYPES], [0, 1, 2, 3, 4])
        self.assertEqual(
            [a.profile_id for a in rec.ARCHETYPES],
            ["docs-only", "small-bugfix", "medium-feature", "large-feature", "high-risk"],
        )

    def test_high_risk_wins_first(self) -> None:
        self.assertEqual(rec.classify(_brief(declared_risk="high"))[0], "high-risk")
        self.assertEqual(rec.classify(_brief(security_sensitive=True))[0], "high-risk")
        self.assertEqual(
            rec.classify(_brief(task_type="docs", declared_risk="high",
                                candidate_files=["docs/a.md"]))[0],
            "high-risk",
        )

    def test_docs_only_requires_all_docs(self) -> None:
        self.assertEqual(
            rec.classify(_brief(task_type="docs", candidate_files=["docs/a.md", "README.rst"]))[0],
            "docs-only",
        )
        self.assertEqual(
            rec.classify(_brief(task_type="docs", candidate_files=["src/a.py"]))[0], "medium-feature"
        )
        self.assertEqual(rec.classify(_brief(task_type="docs"))[0], "medium-feature")

    def test_large_docs_only_stays_docs_only(self) -> None:
        files = [f"docs/page{i}.md" for i in range(30)]
        self.assertEqual(rec.classify(_brief(task_type="docs", candidate_files=files))[0], "docs-only")

    def test_broad_signal_escalates_to_large(self) -> None:
        self.assertEqual(rec.classify(_brief(task_type="feature", blast_radius="cross_cutting"))[0], "large-feature")
        self.assertEqual(rec.classify(_brief(task_type="feature", declared_size="xl"))[0], "large-feature")
        self.assertEqual(
            rec.classify(_brief(task_type="feature", candidate_files=[f"f{i}.py" for i in range(20)]))[0],
            "large-feature",
        )

    def test_small_bugfix_needs_explicit_bounds(self) -> None:
        profile = rec.classify(_brief(task_type="bugfix", candidate_files=["a.py", "b.py"],
                                      declared_size="s", blast_radius="local"))[0]
        self.assertEqual(profile, "small-bugfix")

    def test_unknown_bounds_floor_to_medium(self) -> None:
        self.assertEqual(rec.classify(_brief(task_type="bugfix"))[0], "medium-feature")
        self.assertEqual(
            rec.classify(_brief(task_type="bugfix", candidate_files=[],
                                declared_size="s", blast_radius="local"))[0],
            "medium-feature",
        )
        self.assertEqual(
            rec.classify(_brief(task_type="bugfix", candidate_files=["a.py"], declared_size="s"))[0],
            "medium-feature",
        )
        self.assertEqual(
            rec.classify(_brief(task_type="bugfix", candidate_files=["a.py"], blast_radius="local"))[0],
            "medium-feature",
        )
        self.assertEqual(rec.classify(_brief(task_type="feature", declared_risk="medium"))[0], "medium-feature")

    def test_signal_trace_is_stable(self) -> None:
        _, signals, rationale = rec.classify(
            _brief(task_type="bugfix", candidate_files=["a.py", "b.py"], declared_size="s", blast_radius="local")
        )
        self.assertEqual(
            signals,
            [
                "task_type=bugfix",
                "declared_risk=low",
                "security_sensitive=false",
                "candidate_files=2",
                "blast_radius=local",
                "declared_size=s",
                "broad_signal=false",
                "bounded_small=true",
            ],
        )
        self.assertEqual(rationale, "Recommended small-bugfix: a bounded, low-risk fix with few known files.")

    def test_signal_trace_marks_unknowns(self) -> None:
        _, signals, _ = rec.classify(_brief(task_type="bugfix"))
        self.assertIn("candidate_files=unknown", signals)
        self.assertIn("blast_radius=unknown", signals)
        self.assertIn("declared_size=unknown", signals)


class ContractCandidateTests(unittest.TestCase):
    def test_every_archetype_candidate_is_a_valid_workflow_contract(self) -> None:
        for arch in rec.ARCHETYPES:
            candidate = rec.build_contract_candidate(
                arch.profile_id, _brief(),
                selected_by="recommend-workflow", selection_reason="because",
            )
            self.assertEqual(validate_workflow_contract(candidate), [], arch.profile_id)

    def test_candidate_merges_validation_needs_into_gates(self) -> None:
        candidate = rec.build_contract_candidate(
            "small-bugfix", _brief(validation_needs=["lint", "unit-tests"]),
            selected_by="recommend-workflow", selection_reason="because",
        )
        self.assertEqual(candidate["validation_policy"]["required_gates"], ["lint", "unit-tests"])

    def test_high_risk_candidate_declares_security_capability(self) -> None:
        candidate = rec.build_contract_candidate(
            "high-risk", _brief(),
            selected_by="recommend-workflow", selection_reason="because",
        )
        self.assertEqual(
            candidate["required_capabilities"], [{"id": "security-review", "required": True}]
        )


class RecommendTests(unittest.TestCase):
    def test_no_override(self) -> None:
        report = rec.recommend(_brief(task_type="bugfix", candidate_files=["a.py"],
                                      declared_size="s", blast_radius="local"))
        self.assertEqual(report["recommended"]["profile"], "small-bugfix")
        self.assertEqual(report["selected"]["profile"], "small-bugfix")
        self.assertIsNone(report["override"])
        self.assertEqual(
            report["alternatives"],
            [
                {"profile": "docs-only", "relation": "cheaper",
                 "reason": "choose docs-only for a documentation-only, low-risk change"},
                {"profile": "medium-feature", "relation": "safer",
                 "reason": "choose medium-feature for a change that adds bounded new behavior"},
            ],
        )
        self.assertEqual(report["workflow_contract_candidate"]["selected_by"], "recommend-workflow")
        self.assertEqual(report["workflow_contract_candidate"]["selection_reason"], report["rationale"])

    def test_alternatives_clamp_at_ends(self) -> None:
        docs = rec.recommend(_brief(task_type="docs", candidate_files=["docs/a.md"]))
        self.assertEqual([a["relation"] for a in docs["alternatives"]], ["safer"])
        high = rec.recommend(_brief(declared_risk="high"))
        self.assertEqual([a["relation"] for a in high["alternatives"]], ["cheaper"])

    def test_override_records_rationale(self) -> None:
        report = rec.recommend(_brief(task_type="bugfix", candidate_files=["a.py"],
                                      declared_size="s", blast_radius="local"),
                               selected_profile="medium-feature", reason="broader than declared")
        self.assertEqual(report["selected"]["profile"], "medium-feature")
        self.assertEqual(
            report["override"],
            {"from_profile": "small-bugfix", "to_profile": "medium-feature",
             "reason": "broader than declared"},
        )
        candidate = report["workflow_contract_candidate"]
        self.assertEqual(candidate["workflow_profile"], "medium-feature")
        self.assertEqual(candidate["selected_by"], "recommend-workflow --selected-profile")
        self.assertEqual(
            candidate["selection_reason"],
            "Override: small-bugfix -> medium-feature. broader than declared",
        )

    def test_override_same_as_recommended_is_not_an_override(self) -> None:
        report = rec.recommend(_brief(task_type="bugfix"), selected_profile="medium-feature")
        self.assertIsNone(report["override"])
        self.assertEqual(report["selected"]["profile"], "medium-feature")

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(rec.RecommendError) as ctx:
            rec.recommend(_brief(), selected_profile="nope")
        self.assertEqual(ctx.exception.code, "unknown_profile")

    def test_override_without_reason_raises(self) -> None:
        with self.assertRaises(rec.RecommendError) as ctx:
            rec.recommend(_brief(task_type="bugfix"), selected_profile="high-risk")
        self.assertEqual(ctx.exception.code, "override_requires_reason")


class RenderTextTests(unittest.TestCase):
    def test_no_override(self) -> None:
        report = rec.recommend(_brief(task_type="bugfix", candidate_files=["a.py"],
                                      declared_size="s", blast_radius="local"))
        lines = rec.render_text(report)
        self.assertEqual(lines[0], "recommended agentflow-default/small-bugfix")
        self.assertTrue(any(line.startswith("signals: ") for line in lines))
        self.assertIn("alternatives:", lines)
        self.assertTrue(any("cheaper docs-only:" in line for line in lines))

    def test_override_block(self) -> None:
        report = rec.recommend(_brief(task_type="bugfix"),
                               selected_profile="high-risk", reason="touches auth")
        lines = rec.render_text(report)
        self.assertEqual(lines[1], "selected agentflow-default/high-risk (override)")
        self.assertEqual(lines[2], "override: medium-feature -> high-risk: touches auth")


class ExampleBriefTests(unittest.TestCase):
    def test_example_brief_recommends_small_bugfix(self) -> None:
        brief = json.loads(
            (_REPO_ROOT / "examples/briefs/small-bugfix.brief.json").read_text(encoding="utf-8")
        )
        self.assertEqual(rec.validate_brief(brief), [])
        self.assertEqual(rec.classify(brief)[0], "small-bugfix")


if __name__ == "__main__":
    unittest.main()
