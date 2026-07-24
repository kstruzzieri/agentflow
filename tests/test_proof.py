from __future__ import annotations

import subprocess
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agentflow.artifacts import append_jsonl, create_initial_artifacts, write_json
from agentflow.contracts import (
    AGGREGATION_SCHEMA_VERSION,
    PROOF_PACK_SCHEMA_VERSION,
    REVIEW_RUNS_SCHEMA_VERSION,
)
from agentflow.artifacts import plan_binding_sha256
from agentflow.coverage import (
    build_coverage,
    build_design_decision_coverage,
    build_requirement_coverage,
    evaluate_context_budget,
)
from agentflow.execution import init_execution_artifacts
from agentflow.proof import (
    build_proof,
    canonical_core,
    core_sha256,
    execution_summary,
    render_markdown,
    sha256_file,
    verify_proof,
    verify_proof_checks,
    write_proof_metadata,
)
from agentflow.workflow_contract import write_workflow_contract


# #28: build-proof applies the same full plan contract as validate-plan and
# lock-plan, so every plan a test feeds it needs the contract boilerplate. These
# fields carry no meaning for the assertions below; spread them first so each
# fixture's own keys still win.
PLAN_CONTRACT_FIELDS = {
    "scope": ["Fixture scope."],
    "non_goals": [],
    "invariants": ["Fixture invariant."],
    "allowed_files": ["src/"],
    "blocked_files": [],
    "validation_gates": ["fixture gate"],
    "rollback_plan": "git revert the fixture commit.",
    "risk_level": "low",
    "drift_budget": {
        "unrelated_edits": "none",
        "new_dependencies": "none",
        "formatting_drift": "none",
        "architecture_drift": "none",
    },
    "evidence_ids": [],
}

# Likewise for step shape. Proof construction reads only a step's id, gates,
# status, and completed flag, so none of these perturb what the tests assert.
STEP_CONTRACT_FIELDS = {
    "action": "Fixture step action.",
    "files": ["src/"],
    "preconditions": [],
    "expected_diff": ["fixture diff"],
    "validation": ["fixture gate"],
}


def complete_initial_plan(root: Path) -> Path:
    """Fill in the placeholders `create_initial_artifacts` deliberately writes.

    `agentflow init` emits an empty objective and rollback_plan for a human to
    complete before lock-plan, so since #28 build-proof rejects that plan as
    invalid working state. Tests that only need *a* proof start from here.
    """
    path = root / ".agent/plan.lock.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan.update(PLAN_CONTRACT_FIELDS)
    plan["objective"] = "Fixture objective."
    write_json(path, plan)
    return path


def plan_with_two_steps() -> dict:
    return {
        "steps": [
            {**STEP_CONTRACT_FIELDS, "id": "P1", "evidence_ids": ["E1"]},
            {**STEP_CONTRACT_FIELDS, "id": "P2", "evidence_ids": []},
        ],
        "evidence_ids": ["E1", "E2"],
        "context_budget": {
            "max_files": 1,
            "max_total_bytes": 10,
            "max_log_lines_per_failure": 2,
            "receipts_required": True,
        },
    }


def valid_workflow_contract() -> dict:
    return {
        "schema_version": "0.1.0",
        "workflow_pack": "adaptive.default",
        "workflow_profile": "feature.medium",
        "selected_by": "operator",
        "selection_reason": "Fixture workflow contract.",
        "required_capabilities": [
            {"id": "tdd", "required": True},
            {"id": "review-spec", "required": True},
        ],
        "review_depth": "spec_quality",
        "validation_policy": {"required_gates": ["focused"]},
        "proof_policy": {
            "hunk_attribution": "enforce",
            "require_review_run": False,
        },
    }


class CoverageTests(unittest.TestCase):
    def test_build_design_decision_coverage_preserves_canonical_order(self) -> None:
        plan = {
            "design_decisions": [
                {
                    "id": "DD-2",
                    "text": "Second declaration is canonical first.",
                    "references": ["ADR-2", "ADR-1"],
                },
                {
                    "id": "DD-1",
                    "text": "First identifier is canonical second.",
                },
            ],
            "steps": [
                {**STEP_CONTRACT_FIELDS, "id": "P2", "design_decision_ids": ["DD-1", "DD-2"]},
                {**STEP_CONTRACT_FIELDS, "id": "P1", "design_decision_ids": ["DD-2"]},
            ],
        }

        self.assertEqual(
            build_design_decision_coverage(plan),
            {
                "design_decisions": [
                    {
                        "id": "DD-2",
                        "text": "Second declaration is canonical first.",
                        "references": ["ADR-2", "ADR-1"],
                        "steps": ["P2", "P1"],
                    },
                    {
                        "id": "DD-1",
                        "text": "First identifier is canonical second.",
                        "references": [],
                        "steps": ["P2"],
                    },
                ]
            },
        )

    def test_build_design_decision_coverage_omits_absent_contract(self) -> None:
        self.assertEqual(build_design_decision_coverage({"steps": []}), {})

    def _fixture(self, tmp: str) -> Path:
        root = Path(tmp)
        create_initial_artifacts(root)
        plan = {**PLAN_CONTRACT_FIELDS, 
            "schema_version": "0.2.0",
            "objective": "Fixture objective.",
            "scope": ["Fixture scope."],
            "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "action": "Do work.", "evidence_ids": ["E1"]}],
            "evidence_ids": ["E1"],
            "context_budget": {"receipts_required": False},
        }
        write_json(root / ".agent/plan.lock.json", plan)
        append_jsonl(
            root / ".agent/evidence.jsonl",
            {
                "schema_version": "0.2.0",
                "id": "E1",
                "claim": "P1 completed.",
                "source": "tests/test_proof.py",
                "confidence": "high",
                "last_verified": "2026-05-31T00:00:00+00:00",
                "supports": ["P1"],
            },
        )
        return root

    def _traceability_proof_fixture(self, tmp: str, receipt_exit=None):
        root = Path(tmp)
        create_initial_artifacts(root)
        write_json(
            root / ".agent/plan.lock.json",
            {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "Verify criterion coverage integrity.",
                "steps": [
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P1",
                        "action": "Run the criterion gate.",
                        "validation": ["criterion-gate"],
                        "gates": [
                            {
                                "kind": "command",
                                "run": ["check", "criterion"],
                                "criterion_ids": ["AC-1"],
                            }
                        ],
                        "criterion_ids": ["AC-1"],
                        "evidence_ids": [],
                    }
                ],
                "evidence_ids": [],
                "requirements": [
                    {
                        "id": "REQ-1",
                        "text": "Coverage is integrity checked.",
                        "acceptance_criteria": [
                            {"id": "AC-1", "text": "The mapped gate passes."}
                        ],
                    }
                ],
            },
        )
        if receipt_exit is not None:
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.4.0",
                    "id": "CR1",
                    "step_id": "P1",
                    "command": ["check", "criterion"],
                    "exit_code": receipt_exit,
                    "decision": "allowed",
                },
            )
        proof = build_proof(root, root / ".agent/plan.lock.json")
        write_proof_metadata(root, proof)
        return root, proof

    def _design_decision_proof_fixture(self, tmp: str):
        root = Path(tmp)
        create_initial_artifacts(root)
        write_json(
            root / ".agent/plan.lock.json",
            {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.4.0",
                "objective": "Verify design decision coverage integrity.",
                "steps": [
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P1",
                        "action": "Apply both decisions.",
                        "design_decision_ids": ["DD-1", "DD-2"],
                        "evidence_ids": [],
                    },
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P2",
                        "action": "Apply the second decision.",
                        "design_decision_ids": ["DD-2"],
                        "evidence_ids": [],
                    },
                ],
                "evidence_ids": [],
                "design_decisions": [
                    {
                        "id": "DD-1",
                        "text": "Keep decisions in the locked plan.",
                        "references": ["ADR-1"],
                    },
                    {
                        "id": "DD-2",
                        "text": "Reuse proof coverage.",
                    },
                ],
            },
        )
        proof = build_proof(root, root / ".agent/plan.lock.json")
        write_proof_metadata(root, proof)
        return root, proof

    def _review_criterion_fixture(self, tmp: str, *, bound: bool) -> Path:
        root = Path(tmp)
        create_initial_artifacts(root)
        plan_path = root / ".agent/plan.lock.json"
        write_json(
            plan_path,
            {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "Project plan-bound review evidence.",
                "steps": [
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P1",
                        "action": "Implement the reviewed criterion.",
                        "criterion_ids": ["AC-REVIEW"],
                        "evidence_ids": [],
                    }
                ],
                "evidence_ids": [],
                "requirements": [
                    {
                        "id": "REQ-REVIEW",
                        "text": "A quality review proves the criterion.",
                        "acceptance_criteria": [
                            {
                                "id": "AC-REVIEW",
                                "text": "The spec-quality review passes.",
                                "review": {"minimum_depth": "spec_quality"},
                            }
                        ],
                    }
                ],
            },
        )
        review_run = {
            "schema_version": REVIEW_RUNS_SCHEMA_VERSION,
            "review_run_id": "RR-20260710T010000Z-ab12cd34",
            "recorded_at": "2026-07-10T01:00:00+00:00",
            "state_dir": "docs/ai/state/main",
            "manifest_path": "docs/ai/state/main/review-manifest.json",
            "manifest_sha256": "0" * 64,
            "gate_status": "pass",
            "active_blocking": [],
            "depth_profile": "deep",
            "artifacts": [],
        }
        if bound:
            review_run["plan_sha256"] = plan_binding_sha256(
                json.loads(plan_path.read_text(encoding="utf-8"))
            )
        append_jsonl(root / ".agent/review-runs.jsonl", review_run)
        return root

    def test_execution_summary_reports_command_risk_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            init_execution_artifacts(root)
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
                    "id": "CR1",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["python3", "-c", "print('ok')"],
                    "cwd": ".",
                    "env_names": [],
                    "started_at": "2026-06-18T10:00:00+00:00",
                    "finished_at": "2026-06-18T10:00:01+00:00",
                    "exit_code": 0,
                    "stdout_path": None,
                    "stderr_path": None,
                    "stdout_sha256": None,
                    "stderr_sha256": None,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "truncated": False,
                    "decision": "allowed",
                    "risk": {"level": "low", "findings": []},
                },
            )
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
                    "id": "CR2",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["rm", "-rf", "fixture.txt"],
                    "cwd": ".",
                    "env_names": [],
                    "started_at": "2026-06-18T10:02:00+00:00",
                    "finished_at": "2026-06-18T10:02:00+00:00",
                    "exit_code": None,
                    "stdout_path": None,
                    "stderr_path": None,
                    "stdout_sha256": None,
                    "stderr_sha256": None,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "truncated": False,
                    "decision": "blocked",
                    "risk": {
                        "level": "high",
                        "findings": [
                            {
                                "category": "destructive_delete",
                                "level": "high",
                                "detail": "rm recursive force",
                            }
                        ],
                    },
                },
            )

            summary = execution_summary(root, plan)

            self.assertEqual(summary["command_decision_counts"].get("allowed"), 1)
            self.assertEqual(summary["command_decision_counts"].get("blocked"), 1)
            self.assertEqual(summary["command_risk_counts"].get("low"), 1)
            self.assertEqual(summary["command_risk_counts"].get("high"), 1)
            self.assertEqual(summary["command_finding_categories"].get("destructive_delete"), 1)
            self.assertEqual(summary["command_confirmed_high_risk"], 0)

    def test_execution_summary_reports_timeout_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            init_execution_artifacts(root)
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
                    "id": "CR1",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["python3", "-c", "import time; time.sleep(2)"],
                    "cwd": ".",
                    "env_names": [],
                    "started_at": "2026-06-23T10:00:00+00:00",
                    "finished_at": "2026-06-23T10:00:01+00:00",
                    "exit_code": None,
                    "stdout_path": None,
                    "stderr_path": None,
                    "stdout_sha256": None,
                    "stderr_sha256": None,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "truncated": False,
                    "decision": "timeout",
                    "timed_out": True,
                    "timeout_seconds": 1,
                },
            )

            summary = execution_summary(root, plan)

            self.assertEqual(summary["command_decision_counts"].get("timeout"), 1)
            self.assertEqual(summary["command_timed_out"], 1)
            self.assertEqual(summary["command_timeout_seconds"], {"1": 1})

    def test_execution_summary_lists_amendments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            init_execution_artifacts(root)
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            append_jsonl(
                root / ".agent/step-runs.jsonl",
                {
                    "schema_version": "0.3.0",
                    "event": "amendment_started",
                    "step_id": "P1",
                    "attempt_id": "A2",
                    "amends_attempt": "A1",
                    "amends_completed_at": "2026-06-18T10:00:00+00:00",
                    "reason": "address review",
                    "reason_code": "review_feedback",
                    "recorded_at": "2026-06-18T11:00:00+00:00",
                },
            )

            summary = execution_summary(root, plan)

            self.assertEqual(len(summary["amendments"]), 1)
            amendment = summary["amendments"][0]
            self.assertEqual(amendment["step_id"], "P1")
            self.assertEqual(amendment["attempt"], "A2")
            self.assertEqual(amendment["amends_attempt"], "A1")
            self.assertEqual(amendment["reason_code"], "review_feedback")

    def _fixture_unsupported_step(self, tmp: str, receipts_required: bool) -> Path:
        root = Path(tmp)
        create_initial_artifacts(root)
        plan = {**PLAN_CONTRACT_FIELDS, 
            "schema_version": "0.2.0",
            "objective": "Fixture objective.",
            "scope": ["Fixture scope."],
            "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "action": "Do work.", "evidence_ids": []}],
            "evidence_ids": [],
            "context_budget": {"receipts_required": receipts_required},
        }
        write_json(root / ".agent/plan.lock.json", plan)
        return root

    def test_build_coverage_reports_unused_and_dangling_references(self) -> None:
        coverage = build_coverage(
            plan_with_two_steps(),
            evidence=[
                {"id": "E1", "supports": ["P1"]},
                {"id": "E2", "supports": ["PX"]},
                {"id": "E3", "supports": []},
            ],
            context_receipts=[
                {"id": "C1", "source": "b.py", "used_for": ["PY"], "bytes": 12}
            ],
            runtime_config={
                "runtimes": {"local": {"enabled": True}},
                "routes": {"reviewer": {"primary": "missing", "fallbacks": ["local"]}},
            },
        )

        self.assertEqual(coverage["steps_without_support"], ["P2"])
        self.assertEqual(coverage["missing_plan_evidence_ids"], [])
        self.assertEqual(coverage["unused_evidence_ids"], ["E3"])
        self.assertEqual(
            coverage["dangling_supports"],
            [{"evidence_id": "E2", "step_id": "PX"}],
        )
        self.assertEqual(
            coverage["dangling_used_for"],
            [{"receipt_id": "C1", "step_id": "PY"}],
        )
        self.assertEqual(
            coverage["dangling_route_runtimes"],
            [{"route": "reviewer", "field": "primary", "runtime": "missing"}],
        )

    def test_build_coverage_reports_missing_step_evidence_ids(self) -> None:
        plan = plan_with_two_steps()
        plan["steps"][0]["evidence_ids"] = ["E_MISSING"]
        plan["evidence_ids"] = []

        coverage = build_coverage(
            plan,
            evidence=[],
            context_receipts=[],
        )

        self.assertEqual(coverage["missing_plan_evidence_ids"], ["E_MISSING"])
        self.assertEqual(coverage["steps_without_support"], ["P1", "P2"])

    def test_build_coverage_checks_default_runtime_for_routes_without_primary(self) -> None:
        coverage = build_coverage(
            plan_with_two_steps(),
            evidence=[],
            context_receipts=[],
            runtime_config={
                "default_runtime": "missing",
                "runtimes": {"local": {"enabled": True}},
                "routes": {"reviewer": {}},
            },
        )

        self.assertEqual(
            coverage["dangling_route_runtimes"],
            [{"route": "reviewer", "field": "default_runtime", "runtime": "missing"}],
        )

    def test_build_proof_projects_satisfied_command_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            plan = {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "Trace a command-backed criterion.",
                "steps": [
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P1",
                        "action": "Run the check.",
                        "validation": ["unit-tests"],
                        "gates": [
                            {
                                "kind": "command",
                                "run": ["python3", "-m", "unittest"],
                                "criterion_ids": ["AC-1"],
                            }
                        ],
                        "criterion_ids": ["AC-1"],
                        "evidence_ids": [],
                    }
                ],
                "evidence_ids": [],
                "requirements": [
                    {
                        "id": "REQ-1",
                        "text": "The command behavior works.",
                        "acceptance_criteria": [
                            {"id": "AC-1", "text": "The unit-test gate passes."}
                        ],
                    }
                ],
            }
            write_json(root / ".agent/plan.lock.json", plan)
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.4.0",
                    "id": "CR1",
                    "step_id": "P1",
                    "command": ["python3", "-m", "unittest"],
                    "gate": "unit-tests",
                    "exit_code": 0,
                    "decision": "allowed",
                },
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            requirement = proof["coverage"]["requirements"][0]
            criterion = requirement["acceptance_criteria"][0]
            self.assertEqual(requirement["status"], "satisfied")
            self.assertEqual(criterion["status"], "satisfied")
            self.assertEqual(criterion["steps"], ["P1"])
            self.assertEqual(
                criterion["evidence"],
                [
                    {
                        "kind": "command",
                        "step_id": "P1",
                        "gate": "python3 -m unittest",
                        "status": "satisfied",
                        "receipt_id": "CR1",
                    }
                ],
            )
            self.assertEqual(
                proof["coverage"]["criterion_status_counts"],
                {"satisfied": 1, "failed": 0, "missing": 0, "unmapped": 0},
            )

    def test_gate_label_alone_is_not_criterion_receipt_evidence(self) -> None:
        # A receipt recorded under the gate's label but with a different argv
        # (e.g. a work command run via --gate) must not satisfy the criterion.
        plan = {
            "steps": [
                {**STEP_CONTRACT_FIELDS, 
                    "id": "P1",
                    "validation": ["unit-tests"],
                    "criterion_ids": ["AC-1"],
                    "gates": [
                        {
                            "kind": "command",
                            "run": ["python3", "-m", "unittest"],
                            "criterion_ids": ["AC-1"],
                        }
                    ],
                }
            ],
            "requirements": [
                {
                    "id": "REQ-1",
                    "text": "Command works.",
                    "acceptance_criteria": [{"id": "AC-1", "text": "Gate passes."}],
                }
            ],
        }
        receipt = {
            "id": "CR1",
            "step_id": "P1",
            "command": ["make", "lint"],
            "gate": "unit-tests",
            "exit_code": 0,
            "decision": "allowed",
        }

        coverage = build_requirement_coverage(plan, [], [receipt], [], "0" * 64)

        criterion = coverage["requirements"][0]["acceptance_criteria"][0]
        self.assertEqual(criterion["status"], "missing")

    def test_env_wrapped_receipt_with_gate_label_satisfies_criterion(self) -> None:
        plan = {
            "steps": [
                {**STEP_CONTRACT_FIELDS, 
                    "id": "P1",
                    "validation": ["unit-tests"],
                    "criterion_ids": ["AC-1"],
                    "gates": [
                        {
                            "kind": "command",
                            "run": ["python3", "-m", "unittest"],
                            "criterion_ids": ["AC-1"],
                        }
                    ],
                }
            ],
            "requirements": [
                {
                    "id": "REQ-1",
                    "text": "Command works.",
                    "acceptance_criteria": [{"id": "AC-1", "text": "Gate passes."}],
                }
            ],
        }
        receipt = {
            "id": "CR1",
            "step_id": "P1",
            "command": ["env", "PYTHONPATH=src", "python3", "-m", "unittest"],
            "gate": "unit-tests",
            "exit_code": 0,
            "decision": "allowed",
        }

        coverage = build_requirement_coverage(plan, [], [receipt], [], "0" * 64)

        criterion = coverage["requirements"][0]["acceptance_criteria"][0]
        self.assertEqual(criterion["status"], "satisfied")
        self.assertEqual(criterion["evidence"][0]["receipt_id"], "CR1")

    def test_build_proof_reports_all_criterion_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            criteria = [
                {"id": "AC-SAT", "text": "Satisfied."},
                {"id": "AC-FAIL", "text": "Failed."},
                {"id": "AC-MISSING", "text": "Missing."},
                {"id": "AC-UNMAPPED", "text": "Unmapped."},
            ]
            steps = []
            for index, criterion in enumerate(criteria, start=1):
                step = {
                    **STEP_CONTRACT_FIELDS,
                    "id": f"P{index}",
                    "action": criterion["text"],
                    "validation": [f"gate-{index}"],
                    "criterion_ids": [criterion["id"]],
                    "evidence_ids": [],
                }
                if criterion["id"] != "AC-UNMAPPED":
                    step["gates"] = [
                        {
                            "kind": "command",
                            "run": ["check", str(index)],
                            "criterion_ids": [criterion["id"]],
                        }
                    ]
                else:
                    step["gates"] = [
                        {"kind": "command", "run": ["check", str(index)]}
                    ]
                steps.append(step)
            plan = {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "Project every criterion state.",
                "steps": steps,
                "evidence_ids": [],
                "requirements": [
                    {
                        "id": "REQ-1",
                        "text": "Command outcomes are projected.",
                        "acceptance_criteria": criteria[:2],
                    },
                    {
                        "id": "REQ-2",
                        "text": "Absent mappings and evidence stay visible.",
                        "acceptance_criteria": criteria[2:],
                    },
                ],
            }
            write_json(root / ".agent/plan.lock.json", plan)
            for receipt_id, step_id, command, exit_code in (
                ("CR1", "P1", ["check", "1"], 0),
                ("CR2", "P2", ["check", "2"], 1),
                ("CR3", "P4", ["check", "4"], 0),
            ):
                append_jsonl(
                    root / ".agent/command-receipts.jsonl",
                    {
                        "schema_version": "0.4.0",
                        "id": receipt_id,
                        "step_id": step_id,
                        "command": command,
                        "exit_code": exit_code,
                        "decision": "allowed",
                    },
                )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            statuses = {
                criterion["id"]: criterion["status"]
                for requirement in proof["coverage"]["requirements"]
                for criterion in requirement["acceptance_criteria"]
            }
            self.assertEqual(
                statuses,
                {
                    "AC-SAT": "satisfied",
                    "AC-FAIL": "failed",
                    "AC-MISSING": "missing",
                    "AC-UNMAPPED": "unmapped",
                },
            )
            self.assertEqual(
                proof["coverage"]["criterion_status_counts"],
                {"satisfied": 1, "failed": 1, "missing": 1, "unmapped": 1},
            )
            check = next(item for item in proof["checks"] if item["id"] == "criteria_satisfied")
            self.assertEqual(check["status"], "failed")
            self.assertEqual(check["count"], 3)

    def test_build_proof_uses_positive_message_when_all_criteria_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proof = self._traceability_proof_fixture(tmp, receipt_exit=0)

            check = next(
                item for item in proof["checks"] if item["id"] == "criteria_satisfied"
            )

            self.assertEqual(check["message"], "all acceptance criteria are satisfied")

    def test_build_proof_projects_inspection_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            criteria = [
                {"id": "AC-INSPECTED", "text": "Inspection exists."},
                {"id": "AC-NOT-INSPECTED", "text": "Inspection is missing."},
            ]
            plan = {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "Project inspection gates.",
                "steps": [
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P1",
                        "action": "Inspect both criteria.",
                        "validation": ["inspect-present", "inspect-missing"],
                        "criterion_ids": [item["id"] for item in criteria],
                        "gates": [
                            {
                                "kind": "inspection",
                                "evidence_id": "E1",
                                "describe": "Present inspection.",
                                "criterion_ids": ["AC-INSPECTED"],
                            },
                            {
                                "kind": "inspection",
                                "evidence_id": "E2",
                                "describe": "Missing inspection.",
                                "criterion_ids": ["AC-NOT-INSPECTED"],
                            },
                        ],
                        "evidence_ids": ["E1", "E2"],
                    }
                ],
                "evidence_ids": ["E1", "E2"],
                "requirements": [
                    {
                        "id": "REQ-INSPECT",
                        "text": "Inspection mappings are explicit.",
                        "acceptance_criteria": criteria,
                    }
                ],
            }
            write_json(root / ".agent/plan.lock.json", plan)
            append_jsonl(
                root / ".agent/evidence.jsonl",
                {"schema_version": "0.2.0", "id": "E1"},
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            projected = proof["coverage"]["requirements"][0]["acceptance_criteria"]
            self.assertEqual([item["status"] for item in projected], ["satisfied", "missing"])
            self.assertEqual(
                projected[0]["evidence"],
                [
                    {
                        "kind": "inspection",
                        "step_id": "P1",
                        "evidence_id": "E1",
                        "status": "satisfied",
                    }
                ],
            )

    def test_build_proof_projects_deep_review_for_spec_quality_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._review_criterion_fixture(tmp, bound=True)
            plan_sha256 = plan_binding_sha256(
                json.loads(
                    (root / ".agent/plan.lock.json").read_text(encoding="utf-8")
                )
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            criterion = proof["coverage"]["requirements"][0]["acceptance_criteria"][0]
            self.assertEqual(criterion["status"], "satisfied")
            self.assertEqual(
                criterion["evidence"],
                [
                    {
                        "kind": "review",
                        "minimum_depth": "spec_quality",
                        "status": "satisfied",
                        "review_run_id": "RR-20260710T010000Z-ab12cd34",
                        "depth_profile": "deep",
                        "plan_sha256": plan_sha256,
                    }
                ],
            )

    def test_build_proof_does_not_use_review_unbound_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._review_criterion_fixture(tmp, bound=False)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            criterion = proof["coverage"]["requirements"][0]["acceptance_criteria"][0]
            self.assertEqual(criterion["status"], "missing")
            self.assertNotIn("review_run_id", criterion["evidence"][0])

    def test_build_proof_revalidates_requirement_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _proof = self._traceability_proof_fixture(tmp)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            criterion = plan["requirements"][0]["acceptance_criteria"][0]
            plan["requirements"][0]["acceptance_criteria"].append(dict(criterion))
            write_json(plan_path, plan)

            with self.assertRaisesRegex(
                ValueError, "duplicate acceptance criterion id: AC-1"
            ):
                build_proof(root, plan_path)

    def test_build_proof_projects_design_decision_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, proof = self._design_decision_proof_fixture(tmp)

            self.assertEqual(
                proof["coverage"]["design_decisions"],
                [
                    {
                        "id": "DD-1",
                        "text": "Keep decisions in the locked plan.",
                        "references": ["ADR-1"],
                        "steps": ["P1"],
                    },
                    {
                        "id": "DD-2",
                        "text": "Reuse proof coverage.",
                        "references": [],
                        "steps": ["P1", "P2"],
                    },
                ],
            )

    def test_build_proof_revalidates_design_decision_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _proof = self._design_decision_proof_fixture(tmp)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["steps"][0]["design_decision_ids"] = ["DD-MISSING"]
            write_json(plan_path, plan)

            with self.assertRaisesRegex(
                ValueError,
                "invalid design decision traceability.*DD-MISSING",
            ):
                build_proof(root, plan_path)

    def test_verify_proof_recomputes_design_decision_coverage(self) -> None:
        mutations = ("text", "references", "steps", "order", "removed")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root, proof = self._design_decision_proof_fixture(tmp)
                rows = proof["coverage"]["design_decisions"]
                if mutation == "text":
                    rows[0]["text"] = "Tampered text."
                elif mutation == "references":
                    rows[0]["references"] = ["ADR-TAMPERED"]
                elif mutation == "steps":
                    rows[0]["steps"] = ["P2"]
                elif mutation == "order":
                    rows.reverse()
                else:
                    proof["coverage"].pop("design_decisions")
                proof["core_sha256"] = core_sha256(proof)
                write_json(root / ".agent/proof-pack.json", proof)

                findings = verify_proof(root, root / ".agent/proof-pack.json")

                self.assertTrue(
                    any(
                        finding["severity"] == "error"
                        and "design decision coverage is stale or tampered"
                        in finding["message"]
                        for finding in findings
                    )
                )

    def test_verify_proof_skips_design_coverage_without_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _proof = self._design_decision_proof_fixture(tmp)
            (root / ".agent/plan.lock.json").unlink()

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertFalse(
                any("design decision coverage" in finding["message"] for finding in findings)
            )

    def test_verify_proof_hints_schema_growth_for_older_decision_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, proof = self._design_decision_proof_fixture(tmp)
            proof["schema_version"] = "0.10.0"
            proof["coverage"].pop("design_decisions")
            proof["core_sha256"] = core_sha256(proof)
            write_json(root / ".agent/proof-pack.json", proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    "design decision coverage is stale or tampered" in finding["message"]
                    and "older schema version (0.10.0 < 0.11.0)" in finding["message"]
                    for finding in findings
                )
            )

    def test_verify_proof_revalidates_requirement_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _proof = self._traceability_proof_fixture(tmp)
            plan_path = root / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            criterion = plan["requirements"][0]["acceptance_criteria"][0]
            plan["requirements"][0]["acceptance_criteria"].append(dict(criterion))
            write_json(plan_path, plan)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "invalid requirement traceability" in finding["message"]
                    and "duplicate acceptance criterion id: AC-1"
                    in finding["message"]
                    for finding in findings
                )
            )

    def test_verify_proof_recomputes_tampered_criterion_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, proof = self._traceability_proof_fixture(tmp, receipt_exit=0)
            criterion = proof["coverage"]["requirements"][0]["acceptance_criteria"][0]
            criterion["status"] = "missing"
            proof["core_sha256"] = core_sha256(proof)
            write_json(root / ".agent/proof-pack.json", proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "criterion coverage is stale or tampered" in finding["message"]
                    for finding in findings
                )
            )

    def test_verify_proof_recomputes_criteria_satisfied_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, proof = self._traceability_proof_fixture(tmp)
            proof["checks"] = [
                check for check in proof["checks"]
                if check.get("id") != "criteria_satisfied"
            ]
            proof["core_sha256"] = core_sha256(proof)
            write_json(root / ".agent/proof-pack.json", proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "criteria_satisfied check is stale or tampered"
                    in finding["message"]
                    for finding in findings
                )
            )

    def test_verify_proof_hints_schema_growth_for_older_criterion_proof(self) -> None:
        # #82 growth path: a 0.8.0 builder accepts a requirements-bearing plan
        # but records no criterion coverage; the mismatch must point at schema
        # growth, not tampering.
        with tempfile.TemporaryDirectory() as tmp:
            root, proof = self._traceability_proof_fixture(tmp)
            proof["schema_version"] = "0.8.0"
            proof["coverage"].pop("requirements", None)
            proof["coverage"].pop("criterion_status_counts", None)
            proof["checks"] = [
                check for check in proof["checks"]
                if check.get("id") != "criteria_satisfied"
            ]
            proof["core_sha256"] = core_sha256(proof)
            write_json(root / ".agent/proof-pack.json", proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "criterion coverage is stale or tampered" in finding["message"]
                    and "older schema version (0.8.0 <" in finding["message"]
                    for finding in findings
                )
            )

    def test_verify_proof_detects_stale_criterion_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, _proof = self._traceability_proof_fixture(tmp)
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "id": "CR1",
                    "step_id": "P1",
                    "command": ["check", "criterion"],
                    "exit_code": 0,
                    "decision": "allowed",
                },
            )

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    "criterion coverage is stale or tampered" in finding["message"]
                    for finding in findings
                )
            )

    def test_legacy_plan_proof_coverage_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            evidence = [json.loads(line) for line in (
                root / ".agent/evidence.jsonl"
            ).read_text(encoding="utf-8").splitlines()]

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(proof["coverage"], build_coverage(plan, evidence, []))
            self.assertNotIn("criteria_satisfied", [item["id"] for item in proof["checks"]])

    def test_markdown_renders_requirement_coverage_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, proof = self._traceability_proof_fixture(tmp, receipt_exit=0)
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))

            markdown = render_markdown(plan, proof, [], {"status": "pass", "notes": []})

            self.assertIn("## Requirement Coverage", markdown)
            self.assertIn("REQ-1 [satisfied]", markdown)
            self.assertIn("AC-1 [satisfied]", markdown)

    def test_evaluate_context_budget_reports_overruns(self) -> None:
        findings = evaluate_context_budget(
            plan_with_two_steps(),
            context_receipts=[
                {"id": "C1", "source": "a.py", "used_for": ["P1"], "bytes": 9},
                {"id": "C2", "source": "b.py", "used_for": ["P2"], "bytes": 9},
            ],
            failures=[
                {"command": "pytest", "relevant_lines": ["1", "2", "3"]},
            ],
        )

        ids = [finding["id"] for finding in findings]
        self.assertEqual(
            ids,
            ["context_max_total_bytes_exceeded", "context_max_files_exceeded", "failure_log_lines_exceeded"],
        )
        self.assertEqual(findings[0]["severity"], "warning")
        self.assertEqual(findings[2]["severity"], "info")
        self.assertEqual(findings[0]["status"], "warning")

    def test_verify_proof_rejects_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proof_path = root / ".agent/proof-pack.json"
            proof_path.parent.mkdir()
            proof = {
                "schema_version": "0.2.0",
                "bundle_version": "0.2.0",
                "meta": {},
                "generated_from": ["../secret.txt"],
                "files": [{"path": "../secret.txt", "sha256": "0" * 64}],
                "checks": [],
                "coverage": {},
            }
            proof["core_sha256"] = core_sha256(proof)
            proof_path.write_text(json.dumps(proof), encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertEqual(findings[0]["severity"], "error")
            self.assertIn("escapes root", findings[0]["message"])

    def test_verify_proof_malformed_metadata_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            proof_path = root / ".agent/proof-pack.json"
            proof_path.write_text("{ broken", encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertTrue(any(finding["severity"] == "error" for finding in findings))

    def test_verify_proof_missing_required_metadata_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            proof_path = root / ".agent/proof-pack.json"
            proof_path.write_text("{}", encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertTrue(any(finding["severity"] == "error" for finding in findings))
            self.assertTrue(any("missing required field" in finding["message"] for finding in findings))

    def test_verify_proof_requires_generated_from_paths_to_be_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            (root / ".agent/plan.lock.json").write_text("{}", encoding="utf-8")
            proof_path = root / ".agent/proof-pack.json"
            proof = {
                "schema_version": "0.2.0",
                "bundle_version": "0.2.0",
                "meta": {},
                "generated_from": [".agent/plan.lock.json"],
                "files": [],
                "checks": [],
                "coverage": {},
            }
            proof["core_sha256"] = core_sha256(proof)
            proof_path.write_text(json.dumps(proof), encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertTrue(any("generated_from" in finding["message"] for finding in findings))

    def test_proof_core_is_reproducible_across_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)

            first = build_proof(root, root / ".agent/plan.lock.json")
            second = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(canonical_core(first), canonical_core(second))

    def test_verify_proof_detects_core_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            proof_path = root / ".agent/proof-pack.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data["core_sha256"] = "0" * 64
            proof_path.write_text(json.dumps(data), encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertTrue(any("core" in finding["message"] for finding in findings))
            # The bundle records the current schema_version, so a mismatch is a
            # real tamper -- it must not be excused as schema growth (#82).
            self.assertFalse(
                any("older schema version" in f["message"].lower() for f in findings)
            )

    def test_verify_proof_reports_older_schema_on_core_mismatch(self) -> None:
        # #82: a bundle built by an older Agentflow whose canonical_core layout
        # predates current members re-hashes to a different core today. That is
        # schema growth, not tampering. verify_proof must still fail (the
        # mismatch is indistinguishable from tampering, so tamper-evidence is
        # preserved), but the message must point at the schema-version gap
        # instead of the generic checksum-mismatch error.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            proof_path = root / ".agent/proof-pack.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data["schema_version"] = "0.1.0"
            data["core_sha256"] = "0" * 64
            proof_path.write_text(json.dumps(data), encoding="utf-8")

            findings = verify_proof(root, proof_path)

            core_findings = [
                f for f in findings if "core" in f["message"].lower()
            ]
            self.assertTrue(core_findings, "expected a core-mismatch finding")
            self.assertIn("older schema version", core_findings[0]["message"].lower())
            self.assertEqual(core_findings[0]["severity"], "error")

    def test_verify_proof_rejects_unparseable_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            proof_path = root / ".agent/proof-pack.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data["schema_version"] = "not-a-version"
            data["core_sha256"] = core_sha256(data)
            proof_path.write_text(json.dumps(data), encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "MAJOR.MINOR.PATCH" in finding["message"]
                    for finding in findings
                )
            )

    def test_canonical_core_membership_pinned_to_schema_version(self) -> None:
        # #82 regression guard: canonical_core is hashed into core_sha256, so
        # adding or removing a member silently re-hashes every proof and reads
        # as tamper on older bundles. If this membership changes, bump
        # PROOF_PACK_SCHEMA_VERSION and update both literals below in the same
        # change so verify_proof can tell schema growth from tampering.
        full_proof = {
            "generated_from": [],
            "files": [],
            "checks": [],
            "coverage": {},
            "review": {},
            "capabilities": {},
            "stuck": {},
            "workflow_contract": {},
            "runtime": {},
            "aggregation": {},
        }
        self.assertEqual(
            sorted(canonical_core(full_proof)),
            [
                "aggregation",
                "capabilities",
                "checks",
                "coverage",
                "files",
                "generated_from",
                "review",
                "runtime",
                "stuck",
                "workflow_contract",
            ],
        )
        self.assertEqual(PROOF_PACK_SCHEMA_VERSION, "0.11.0")

    def _write_snapshot_ledger(self, root: Path) -> None:
        snapshot = {
            "schema_version": "0.3.0",
            "id": "R3",
            "created_at": "2026-07-03T00:00:00Z",
            "runtime_config_sha256": "0" * 64,
            "runtimes": [
                {"id": "local", "status": "ready"},
                {"id": "remote", "status": "unavailable"},
            ],
            "routes": [],
            "mcp_servers": [
                {"id": "github", "status": "ready", "declared_tool_count": 15},
                {"id": "copilot", "status": "configured", "declared_tool_count": 0},
            ],
            "findings": [],
        }
        path = root / ".agent/runtime-snapshots.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"schema_version": "0.3.0", "id": "R2", "runtimes": []}
                )
                + "\n"
            )
            handle.write(json.dumps(snapshot) + "\n")

    def test_proof_runtime_block_from_latest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            self._write_snapshot_ledger(root)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            block = proof["runtime"]
            self.assertEqual(block["latest_snapshot_id"], "R3")
            self.assertEqual(block["runtime_counts"], {"ready": 1, "unavailable": 1})
            self.assertEqual(
                block["mcp_server_counts"], {"configured": 1, "ready": 1}
            )
            self.assertEqual(
                block["mcp_servers"],
                [
                    {"id": "copilot", "status": "configured", "declared_tool_count": 0},
                    {"id": "github", "status": "ready", "declared_tool_count": 15},
                ],
            )
            self.assertIn("runtime", canonical_core(proof))

    def test_proof_without_snapshot_has_no_runtime_block_and_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            (root / ".agent/runtime-snapshots.jsonl").unlink(missing_ok=True)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertNotIn("runtime", proof)
            self.assertNotIn("runtime", canonical_core(proof))
            ids = {check["id"] for check in proof["checks"]}
            self.assertNotIn("runtime_snapshot_readable", ids)

    def test_proof_malformed_snapshot_ledger_warns_and_still_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            (root / ".agent/runtime-snapshots.jsonl").write_text(
                "{not json\n", encoding="utf-8"
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertNotIn("runtime", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["runtime_snapshot_readable"]["status"], "warning")

    def test_proof_stale_runtime_snapshot_warns_and_omits_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            (root / ".agent/runtime.config.json").write_text(
                json.dumps(
                    {
                        "schema_version": "0.3.0",
                        "runtimes": {"local": {"adapter": "custom", "enabled": True}},
                        "routes": {},
                    }
                ),
                encoding="utf-8",
            )
            self._write_snapshot_ledger(root)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("runtime", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["runtime_snapshot_readable"]["status"], "warning")
            self.assertIn("stale", checks["runtime_snapshot_readable"]["message"])

    def test_proof_malformed_mcp_server_ids_warns_and_omits_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            snapshot = {
                "schema_version": "0.3.0",
                "id": "R1",
                "created_at": "2026-07-03T00:00:00Z",
                "runtime_config_sha256": "0" * 64,
                "runtimes": [],
                "routes": [],
                "mcp_servers": [
                    {"id": None, "status": "ready", "declared_tool_count": 1},
                    {"id": "github", "status": "configured", "declared_tool_count": 0},
                ],
                "findings": [],
            }
            (root / ".agent/runtime-snapshots.jsonl").write_text(
                json.dumps(snapshot) + "\n", encoding="utf-8"
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("runtime", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["runtime_snapshot_readable"]["status"], "warning")

    def test_verify_proof_detects_runtime_block_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            self._write_snapshot_ledger(root)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            metadata_path = root / ".agent/proof-pack.json"
            tampered = json.loads(metadata_path.read_text(encoding="utf-8"))
            tampered["runtime"]["mcp_servers"][1]["status"] = "unavailable"
            metadata_path.write_text(json.dumps(tampered), encoding="utf-8")
            findings = verify_proof(root, metadata_path)
            core_findings = [f for f in findings if "core" in f["message"].lower()]
            self.assertTrue(core_findings, "expected a core-mismatch finding")
            self.assertEqual(core_findings[0]["severity"], "error")

    def test_verify_proof_requires_core_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            proof_path = root / ".agent/proof-pack.json"
            data = json.loads(proof_path.read_text(encoding="utf-8"))
            data.pop("core_sha256")
            proof_path.write_text(json.dumps(data), encoding="utf-8")

            findings = verify_proof(root, proof_path)

            self.assertTrue(any("core_sha256" in finding["message"] for finding in findings))

    def test_verify_proof_rejects_malformed_file_entries_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            proof["files"] = ["bad"]
            proof["core_sha256"] = "0" * 64
            proof_path = write_proof_metadata(root, proof)

            try:
                findings = verify_proof(root, proof_path)
            except AttributeError as exc:
                self.fail(f"verify_proof crashed on malformed file entry: {exc}")

            self.assertTrue(any("proof file entry is malformed" in finding["message"] for finding in findings))

    def test_receipts_required_promotes_unsupported_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_unsupported_step(tmp, receipts_required=True)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            steps_check = next(
                check for check in proof["checks"] if check["id"] == "steps_without_support"
            )
            self.assertEqual(steps_check["status"], "failed")

    def test_receipts_optional_keeps_unsupported_steps_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_unsupported_step(tmp, receipts_required=False)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            steps_check = next(
                check for check in proof["checks"] if check["id"] == "steps_without_support"
            )
            self.assertEqual(steps_check["status"], "warning")

    def test_proof_includes_assumptions_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertIn(".agent/assumptions.json", proof["generated_from"])

    def test_verify_proof_detects_runtime_config_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            runtime_config_path = root / ".agent/runtime.config.json"
            runtime_config_path.write_text(
                json.dumps(
                    {
                        "schema_version": "0.2.0",
                        "runtimes": {"local": {"adapter": "custom", "enabled": True}},
                        "routes": {"reviewer": {"primary": "local"}},
                    }
                ),
                encoding="utf-8",
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)

            self.assertIn(".agent/runtime.config.json", proof["generated_from"])

            runtime_config_path.write_text(
                json.dumps(
                    {
                        "schema_version": "0.2.0",
                        "runtimes": {},
                        "routes": {"reviewer": {"primary": "missing"}},
                    }
                ),
                encoding="utf-8",
            )
            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(any("runtime.config.json" in finding["message"] for finding in findings))

    def _cap_row(self, capability, status, provider=None):
        from agentflow.capabilities import build_capability_receipt

        return build_capability_receipt(
            "CAP-" + capability, capability, status, "fixture", provider=provider
        )

    def test_build_proof_capabilities_block_and_missing_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            write_workflow_contract(root, valid_workflow_contract())
            append_jsonl(
                root / ".agent/capability-receipts.jsonl",
                self._cap_row("tdd", "used", "manual"),
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(proof["capabilities"]["required"], ["tdd", "review-spec"])
            self.assertEqual(proof["capabilities"]["recorded"], ["tdd"])
            self.assertEqual(proof["capabilities"]["missing"], ["review-spec"])
            self.assertIn("capabilities", canonical_core(proof))
            check = [
                c for c in proof["checks"] if c["id"] == "required_capabilities_satisfied"
            ][0]
            self.assertEqual(check["status"], "warning")

    def test_strict_proof_fails_on_unwaived_missing_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            write_workflow_contract(root, valid_workflow_contract())
            append_jsonl(
                root / ".agent/capability-receipts.jsonl",
                self._cap_row("tdd", "used", "manual"),
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)

            lenient = verify_proof(root, root / ".agent/proof-pack.json")
            strict = verify_proof(root, root / ".agent/proof-pack.json", strict=True)

            self.assertFalse(
                any(
                    f["severity"] == "error"
                    and "required_capabilities_satisfied" in f["message"]
                    for f in lenient
                )
            )
            self.assertTrue(
                any(
                    f["severity"] == "error"
                    and "required_capabilities_satisfied" in f["message"]
                    for f in strict
                )
            )

    def test_waiver_satisfies_strict_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            write_workflow_contract(root, valid_workflow_contract())
            append_jsonl(
                root / ".agent/capability-receipts.jsonl",
                self._cap_row("tdd", "used", "manual"),
            )
            append_jsonl(
                root / ".agent/capability-receipts.jsonl",
                self._cap_row("review-spec", "waived"),
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json", strict=True)

            self.assertEqual(proof["capabilities"]["missing"], [])
            self.assertFalse(
                any("required_capabilities_satisfied" in f["message"] for f in findings)
            )

    def test_build_proof_flags_malformed_capability_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            write_workflow_contract(root, valid_workflow_contract())
            (root / ".agent/capability-receipts.jsonl").write_text(
                '{"id": "CAP1"}\n', encoding="utf-8"
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)

            check = [
                c for c in proof["checks"] if c["id"] == "capability_receipts_valid"
            ][0]
            self.assertEqual(check["status"], "failed")
            self.assertEqual(proof["capabilities"]["required"], ["tdd", "review-spec"])
            self.assertEqual(proof["capabilities"]["recorded"], [])
            self.assertEqual(proof["capabilities"]["missing"], ["review-spec", "tdd"])
            # The fallback block is still hash-bound: the core checksum verifies
            # even though the malformed ledger trips the receipts check.
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertFalse(
                any("canonical core checksum mismatch" in f["message"] for f in findings)
            )
            self.assertTrue(
                any("capability_receipts_valid" in f["message"] for f in findings)
            )

    def test_build_proof_includes_workflow_contract_summary_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            write_workflow_contract(root, valid_workflow_contract())

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertIn(".agent/workflow.contract.json", proof["generated_from"])
            self.assertEqual(
                proof["workflow_contract"]["workflow_profile"],
                "feature.medium",
            )
            self.assertEqual(
                proof["workflow_contract"]["required_capabilities"],
                ["tdd", "review-spec"],
            )

    def test_verify_proof_detects_workflow_contract_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            write_workflow_contract(root, valid_workflow_contract())
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            contract_path = root / ".agent/workflow.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["selection_reason"] = "Tampered after proof generation."
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any("workflow.contract.json" in finding["message"] for finding in findings)
            )

    def test_markdown_coverage_renders_counts_not_raw_dicts(self) -> None:
        plan = {**PLAN_CONTRACT_FIELDS, "objective": "o", "scope": [], "steps": []}
        proof = {"coverage": {"dangling_supports": [{"evidence_id": "E1", "step_id": "PX"}]}}

        markdown = render_markdown(plan, proof, [], {"status": "pass", "notes": []})

        self.assertNotIn("evidence_id", markdown)
        self.assertIn("dangling_supports: 1", markdown)

    def test_build_proof_includes_execution_ledgers_once_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            from agentflow.execution import claim_step, init_execution_artifacts
            from agentflow.receipts import record_command

            init_execution_artifacts(root)
            plan = {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "Fixture objective.",
                "scope": ["Fixture scope."],
                "non_goals": [],
                "invariants": ["Fixture invariant."],
                "allowed_files": ["fixture.txt", ".agent/"],
                "blocked_files": [],
                "validation_gates": ["python3 -c \"print('ok')\""],
                "rollback_plan": "Delete fixture.txt.",
                "risk_level": "low",
                "drift_budget": {
                    "unrelated_edits": 0,
                    "new_dependencies": 0,
                    "formatting_drift": "minimal",
                    "architecture_drift": "requires_approval",
                    "test_weakening": 0,
                },
                "steps": [
                    {**STEP_CONTRACT_FIELDS, 
                        "id": "P1",
                        "action": "Do work.",
                        "files": ["fixture.txt"],
                        "preconditions": ["Ready."],
                        "expected_diff": ["fixture.txt exists."],
                        "validation": ["python3 -c \"print('ok')\""],
                        "evidence_ids": ["E1"],
                    }
                ],
                "evidence_ids": ["E1"],
                "locked": True,
                "locked_at": "2026-06-01T00:00:00+00:00",
            }
            write_json(root / ".agent/plan.lock.json", plan)
            claim_step(root, plan, "P1", "agent-a")
            record_command(
                root,
                "P1",
                None,
                ["python3", "-c", "print('ok')"],
                0,
                gate="python3 -c \"print('ok')\"",
            )

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertIn("execution", proof)
            self.assertEqual(proof["execution"]["steps_total"], 1)
            paths = [item["path"] for item in proof["files"]]
            self.assertEqual(paths.count(".agent/command-receipts.jsonl"), 1)
            self.assertIn(".agent/execution.contract.json", paths)

    def test_verify_proof_rejects_modified_receipt_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            from agentflow.execution import claim_step, init_execution_artifacts
            from agentflow.receipts import run_command

            init_execution_artifacts(root)
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            plan["schema_version"] = "0.3.0"
            plan["allowed_files"] = ["fixture.txt", ".agent/"]
            plan["blocked_files"] = []
            plan["validation_gates"] = ["python3 -c \"print('ok')\""]
            plan["rollback_plan"] = "Delete fixture."
            plan["risk_level"] = "low"
            plan["drift_budget"] = {
                "unrelated_edits": 0,
                "new_dependencies": 0,
                "formatting_drift": "minimal",
                "architecture_drift": "requires_approval",
                "test_weakening": 0,
            }
            plan["steps"][0]["files"] = ["fixture.txt"]
            plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
            write_json(root / ".agent/plan.lock.json", plan)
            claim_step(root, plan, "P1", "agent-a")
            receipt = run_command(
                root,
                plan,
                "P1",
                None,
                ["python3", "-c", "print('ok')"],
                gate="python3 -c \"print('ok')\"",
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            (root / receipt["stdout_path"]).write_text("tampered\n", encoding="utf-8")

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any("receipt output hash mismatch" in finding["message"] for finding in findings)
            )


class LeaseProofTests(unittest.TestCase):
    def _plan(self) -> dict:
        return {**PLAN_CONTRACT_FIELDS, 
            "schema_version": "0.3.0", "objective": "Lease proof fixture.",
            "scope": ["s"], "non_goals": [], "invariants": ["i"],
            "allowed_files": [".agent/", "f.txt"], "blocked_files": [],
            "validation_gates": ["python3 -c \"print('ok')\""],
            "rollback_plan": "r", "risk_level": "low",
            "drift_budget": {"unrelated_edits": 0, "new_dependencies": 0,
                             "formatting_drift": "minimal",
                             "architecture_drift": "requires_approval",
                             "test_weakening": 0},
            "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "action": "a", "files": ["f.txt"],
                       "preconditions": ["p"], "expected_diff": ["d"],
                       "validation": ["python3 -c \"print('ok')\""],
                       "evidence_ids": []}],
            "evidence_ids": [], "locked": True,
            "locked_at": "2026-06-01T00:00:00+00:00",
        }

    def _enforce_root(self, tmp: str) -> Path:
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", self._plan())
        contract = json.loads(
            (root / ".agent/execution.contract.json").read_text(encoding="utf-8")
        )
        contract["concurrency"].update(
            {"lease_policy": "enforce", "lease_ttl_minutes": 30, "lease_grace_seconds": 30}
        )
        write_json(root / ".agent/execution.contract.json", contract)
        return root

    def test_proof_coverage_reports_abandoned_and_expired(self) -> None:
        from agentflow.execution import claim_step, reclaim_step
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            root = self._enforce_root(tmp)
            claim_step(root, self._plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            reclaim_step(root, self._plan(), "P1", "agent-b", reason="crash", now=future)

            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertIn("abandoned_attempts", proof["coverage"])
            self.assertIn("expired_leases", proof["coverage"])
            self.assertIn("no_deadline_open_attempts", proof["coverage"])
            self.assertTrue(proof["coverage"]["abandoned_attempts"])
            # Lease diagnostics must be inside the tamper-protected canonical core.
            self.assertIn("abandoned_attempts", canonical_core(proof)["coverage"])

    def test_normalized_hash_unaffected_by_lease_and_abandon_timestamps(self) -> None:
        from agentflow.execution import claim_step, reclaim_step
        from datetime import datetime, timedelta, timezone

        def _hash(tmp: str) -> str:
            root = self._enforce_root(tmp)
            claim_step(root, self._plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            reclaim_step(root, self._plan(), "P1", "agent-b", reason="crash", now=future)
            return execution_summary(root, self._plan())["normalized_execution_sha256"]

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            hash_a = _hash(a)
            # Mutate ONLY lease/abandon timestamp fields in the second ledger.
            root_b = self._enforce_root(b)
            claim_step(root_b, self._plan(), "P1", "agent-a", lease_minutes=30)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            reclaim_step(root_b, self._plan(), "P1", "agent-b", reason="crash", now=future)
            path = root_b / ".agent/step-runs.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                if "lease_expires_at" in row and row["lease_expires_at"] is not None:
                    row["lease_expires_at"] = "2099-01-01T00:00:00+00:00"
                if "recorded_at" in row:
                    row["recorded_at"] = "2099-01-01T00:00:00+00:00"
            path.write_text(
                "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                encoding="utf-8",
            )
            hash_b = execution_summary(root_b, self._plan())["normalized_execution_sha256"]
            self.assertEqual(
                hash_a,
                hash_b,
                "lease/abandon timestamps must not churn normalized_execution_sha256",
            )


class ReviewEvidenceExemptionTests(unittest.TestCase):
    def test_kind_review_evidence_not_flagged_unused(self) -> None:
        plan = {"steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "evidence_ids": []}], "evidence_ids": []}
        evidence = [
            {"id": "E-review-RR1", "kind": "review", "claim": "x", "supports": []},
            {"id": "E-orphan", "kind": "user", "claim": "y", "supports": []},
        ]
        coverage = build_coverage(plan, evidence, [], None)
        self.assertNotIn("E-review-RR1", coverage["unused_evidence_ids"])
        self.assertIn("E-orphan", coverage["unused_evidence_ids"])


class ReviewProofTests(unittest.TestCase):
    def _fixture_with_review(self, tmp: str, gate_status: str = "pass", active=None):
        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(
            root / ".agent/plan.lock.json",
            {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0", "objective": "o", "scope": ["s"],
                "non_goals": [], "invariants": ["i"],
                "allowed_files": [".agent/**"], "blocked_files": [],
                "validation_gates": ["python3 -m unittest"],
                "rollback_plan": "r", "risk_level": "low",
                "drift_budget": {
                    "unrelated_edits": 0, "new_dependencies": 0,
                    "formatting_drift": "minimal",
                    "architecture_drift": "requires_approval",
                    "test_weakening": 0,
                },
                "steps": [{**STEP_CONTRACT_FIELDS, 
                    "id": "P1", "action": "a", "files": [".agent/**"],
                    "preconditions": ["p"], "expected_diff": ["d"],
                    "validation": ["python3 -m unittest"], "evidence_ids": [],
                }],
                "evidence_ids": [], "locked": True,
                "locked_at": "2026-06-01T00:00:00+00:00",
            },
        )
        append_jsonl(
            root / ".agent/review-runs.jsonl",
            {
                "schema_version": "0.3.0",
                "review_run_id": "RR-20260620T180000Z-ab12cd34",
                "recorded_at": "2026-06-20T18:00:00+00:00",
                "state_dir": "docs/ai/state/main",
                "manifest_path": "docs/ai/state/main/review-manifest.json",
                "manifest_sha256": "0" * 64,
                "gate_status": gate_status,
                "active_blocking": active or [],
                "findings": {"index": [{"finding_id": "BP-001", "status": "fixed"}]},
                "artifacts": [],
            },
        )
        return root

    def test_review_block_present_and_in_canonical_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertIn("review", proof)
            self.assertEqual(proof["review"]["latest_review_run_id"], "RR-20260620T180000Z-ab12cd34")
            self.assertIn("review", canonical_core(proof))

    def test_review_gate_check_passes_for_passing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp, gate_status="pass")
            proof = build_proof(root, root / ".agent/plan.lock.json")
            gate = [c for c in proof["checks"] if c["id"] == "review_gate"][0]
            self.assertEqual(gate["status"], "passed")

    def test_review_gate_warns_on_failing_gate_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp, gate_status="fail", active=["BP-001"])
            proof = build_proof(root, root / ".agent/plan.lock.json")
            gate = [c for c in proof["checks"] if c["id"] == "review_gate"][0]
            self.assertEqual(gate["status"], "warning")

    def test_strict_promotes_review_gate_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp, gate_status="fail", active=["BP-001"])
            proof = build_proof(root, root / ".agent/plan.lock.json", strict=True)
            gate = [c for c in proof["checks"] if c["id"] == "review_gate"][0]
            self.assertEqual(gate["status"], "failed")


    def test_build_records_warn_policy_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            policy = proof["review"]["policy"]
            self.assertEqual(policy["review_gate_effective"], "warn")
            self.assertFalse(policy["proof_strict_effective"])
            self.assertEqual(policy["verification_semantics"], "ratchet-v1")
            self.assertIn("require_review_run", policy)

    def test_build_records_block_policy_under_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json", strict=True)
            policy = proof["review"]["policy"]
            self.assertEqual(policy["review_gate_effective"], "block")
            self.assertTrue(policy["proof_strict_effective"])

    def test_policy_is_inside_canonical_core_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertIn("policy", canonical_core(proof)["review"])
            before = core_sha256(proof)
            proof["review"]["policy"]["proof_strict_effective"] = True
            self.assertNotEqual(core_sha256(proof), before)

    def test_amendment_projection_is_preserved_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture_with_review(tmp)
            selected_plan = json.loads(
                (root / ".agent/plan.lock.json").read_text(encoding="utf-8")
            )
            selected_plan["objective"] = "selected plan"
            selected_plan_path = root / "selected-plan.json"
            write_json(selected_plan_path, selected_plan)
            ledger_path = root / ".agent/review-runs.jsonl"
            run = json.loads(ledger_path.read_text(encoding="utf-8"))
            run["schema_version"] = REVIEW_RUNS_SCHEMA_VERSION
            run["amendment_ready"] = True
            run["plan_sha256"] = plan_binding_sha256(selected_plan)
            run["findings"] = {
                "counts_by_severity": {"high": 1},
                "counts_by_status": {"accepted": 1},
                "index": [{
                    "finding_id": "BP-001",
                    "severity": "high",
                    "status": "accepted",
                    "owning_step": "P1",
                    "claim": "Broken proof integrity.",
                    "suggested_fix": "Hash the missing artifact.",
                }]
            }
            state = root / "docs/ai/state/main"
            state.mkdir(parents=True, exist_ok=True)
            manifest_path = state / "review-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "review_run_id": run["review_run_id"],
                        "state_dir": "docs/ai/state/main",
                        "gate_status": run["gate_status"],
                        "active_blocking": run.get("active_blocking", []),
                        "amendment_ready": True,
                        "findings": run["findings"],
                        "artifacts": [{"path": "findings-final.json"}],
                    }
                ),
                encoding="utf-8",
            )
            run["manifest_sha256"] = sha256_file(manifest_path)
            ledger_path.write_text(json.dumps(run) + "\n", encoding="utf-8")
            proof = build_proof(root, selected_plan_path)
            self.assertIn("selected-plan.json", proof["generated_from"])
            projected = proof["review"]["review_runs"][0]
            self.assertTrue(projected["amendment_ready"])
            self.assertEqual(projected["findings"], run["findings"])
            write_json(root / ".agent/proof-pack.json", proof)
            selected_plan["objective"] = "tampered after proof build"
            write_json(selected_plan_path, selected_plan)
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertTrue(
                any(
                    finding["message"] == "hash mismatch for selected-plan.json"
                    for finding in findings
                ),
                findings,
            )
            before = core_sha256(proof)
            projected["findings"]["index"][0]["claim"] = "tampered"
            self.assertNotEqual(core_sha256(proof), before)


class AdaptiveReviewProofTests(unittest.TestCase):
    """#74: build_proof/verify_proof honor the workflow contract's review_depth."""

    def _fixture(
        self, tmp, *, review_depth=None, require_review_run=False, with_run=False,
        run_depth=None,
    ):
        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(
            root / ".agent/plan.lock.json",
            {**PLAN_CONTRACT_FIELDS, 
                "schema_version": "0.3.0",
                "objective": "o",
                "scope": ["s"],
                "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "evidence_ids": []}],
                "evidence_ids": [],
            },
        )
        if review_depth is not None:
            contract = valid_workflow_contract()
            contract["review_depth"] = review_depth
            contract["proof_policy"]["require_review_run"] = require_review_run
            write_workflow_contract(root, contract)
        if with_run:
            run = {
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
            }
            if run_depth is not None:
                run["schema_version"] = REVIEW_RUNS_SCHEMA_VERSION
                run["depth_profile"] = run_depth
            append_jsonl(root / ".agent/review-runs.jsonl", run)
        return root

    def _required_check(self, proof):
        matches = [c for c in proof["checks"] if c["id"] == "required_review_satisfied"]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_build_proof_records_required_review_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="deep")
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertEqual(proof["review"]["policy"]["required_review_depth"], "deep")

    def test_deep_without_run_fails_check_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="deep")
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            self.assertEqual(self._required_check(proof)["status"], "failed")
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertTrue(
                any(
                    f["severity"] == "error" and "required_review_satisfied" in f["message"]
                    for f in findings
                )
            )

    def test_deep_with_run_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="deep", with_run=True)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            self.assertEqual(self._required_check(proof)["status"], "passed")
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertFalse(
                any("required_review_satisfied" in f["message"] for f in findings)
            )

    def test_proof_review_runs_carry_depth_profile(self):
        # #92: a recorded spec_quality run flows depth_profile into the proof's
        # review_runs and satisfies a spec_quality requirement by depth.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(
                tmp, review_depth="spec_quality", require_review_run=True,
                with_run=True, run_depth="spec_quality",
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertEqual(
                proof["review"]["review_runs"][-1]["depth_profile"], "spec_quality"
            )
            check = self._required_check(proof)
            self.assertIn("satisfied_by_depth", check)
            self.assertEqual(check["satisfied_by_depth"], "spec_quality")
            self.assertEqual(check["status"], "passed")

    def test_standard_without_run_does_not_fail_even_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="standard")
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            self.assertEqual(self._required_check(proof)["status"], "passed")
            strict = verify_proof(root, root / ".agent/proof-pack.json", strict=True)
            self.assertFalse(
                any("required_review_satisfied" in f["message"] for f in strict)
            )

    def test_spec_quality_without_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="spec_quality")
            proof = build_proof(root, root / ".agent/plan.lock.json")
            write_proof_metadata(root, proof)
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertTrue(
                any(
                    f["severity"] == "error" and "required_review_satisfied" in f["message"]
                    for f in findings
                )
            )

    def test_deep_floor_escalates_failing_review_gate_without_strict(self):
        # The headline #74 behavior: review_depth=deep alone makes the review gate
        # blocking, so a recorded FAILING run is a hard error with no --strict and
        # no execution-contract block.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="deep")
            append_jsonl(
                root / ".agent/review-runs.jsonl",
                {
                    "schema_version": "0.3.0",
                    "review_run_id": "RR-20260620T180000Z-ffffffff",
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
            proof = build_proof(root, root / ".agent/plan.lock.json")
            gate = [c for c in proof["checks"] if c["id"] == "review_gate"][0]
            self.assertEqual(gate["status"], "failed")

    def test_markdown_surfaces_required_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, review_depth="deep")
            proof = build_proof(root, root / ".agent/plan.lock.json")
            plan = json.loads((root / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            markdown = render_markdown(plan, proof, [], {"status": "pass", "notes": []})
            self.assertIn("## Review Policy", markdown)
            self.assertIn("required_review_depth: deep", markdown)
            self.assertIn("review_run_recorded: no", markdown)


class VerifyReviewTests(unittest.TestCase):
    def _fixture(self, tmp, gate_status="pass", active=None, with_artifact=True, strict=False):
        root = Path(tmp)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(
            root / ".agent/plan.lock.json",
            {**PLAN_CONTRACT_FIELDS, "schema_version": "0.3.0", "objective": "o", "scope": ["s"],
             "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "evidence_ids": []}], "evidence_ids": []},
        )
        state = root / "docs/ai/state/main"
        state.mkdir(parents=True)
        manifest_path = state / "review-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "0.1.0",
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "state_dir": "docs/ai/state/main",
                    "gate_status": gate_status,
                    "active_blocking": active or [],
                    "findings": {"index": []},
                    "artifacts": [{"path": "findings-final.yaml"}],
                }
            ),
            encoding="utf-8",
        )
        from agentflow.review import sha256_file
        artifacts = []
        if with_artifact:
            (state / "findings-final.yaml").write_text("findings: []\n", encoding="utf-8")
            artifacts = [{"path": "docs/ai/state/main/findings-final.yaml",
                          "sha256": sha256_file(state / "findings-final.yaml")}]
        append_jsonl(
            root / ".agent/review-runs.jsonl",
            {"schema_version": "0.3.0", "review_run_id": "RR-20260620T180000Z-ab12cd34",
             "recorded_at": "2026-06-20T18:00:00+00:00", "state_dir": "docs/ai/state/main",
             "manifest_path": "docs/ai/state/main/review-manifest.json",
             "manifest_sha256": sha256_file(manifest_path),
             "gate_status": gate_status, "active_blocking": active or [],
             "findings": {"index": []}, "artifacts": artifacts},
        )
        proof = build_proof(root, root / ".agent/plan.lock.json", strict=strict)
        write_proof_metadata(root, proof)
        return root

    def test_artifact_tamper_is_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            (root / "docs/ai/state/main/findings-final.yaml").write_text("tampered\n", encoding="utf-8")
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            errors = [f for f in findings if f["severity"] == "error"]
            self.assertTrue(any("review artifact" in f["message"] for f in errors))

    def test_manifest_tamper_is_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            (root / "docs/ai/state/main/review-manifest.json").write_text("tampered\n", encoding="utf-8")
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            errors = [f for f in findings if f["severity"] == "error"]
            self.assertTrue(any("review manifest" in f["message"] for f in errors))

    def test_ledger_projection_tamper_fails_even_after_rebuilding_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            ledger_path = root / ".agent/review-runs.jsonl"
            record = json.loads(ledger_path.read_text(encoding="utf-8"))
            record["findings"] = {
                "index": [{
                    "finding_id": "BP-001",
                    "severity": "high",
                    "status": "fixed",
                }]
            }
            ledger_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            write_proof_metadata(root, build_proof(root, root / ".agent/plan.lock.json"))
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "projection mismatch" in finding["message"]
                    for finding in findings
                )
            )

    def test_artifact_absent_is_warning_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            (root / "docs/ai/state/main/findings-final.yaml").unlink()
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertEqual([f for f in findings if f["severity"] == "error"], [])
            self.assertTrue(any(f["severity"] == "warning" for f in findings))

    def test_clean_review_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp)
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertEqual([f for f in findings if f["severity"] == "error"], [])

    def test_failing_gate_is_warning_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, gate_status="fail", active=["BP-001"])
            findings = verify_proof(root, root / ".agent/proof-pack.json")
            self.assertEqual([f for f in findings if f["severity"] == "error"], [])
            self.assertTrue(any(f["severity"] == "warning" for f in findings))

    def test_strict_promotes_persisted_warning_checks_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            write_json(
                root / ".agent/plan.lock.json",
                {**PLAN_CONTRACT_FIELDS, 
                    "schema_version": "0.3.0",
                    "objective": "o",
                    "scope": ["s"],
                    "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "evidence_ids": []}],
                    "evidence_ids": ["E-missing"],
                },
            )
            proof = build_proof(root, root / ".agent/plan.lock.json")
            missing = [c for c in proof["checks"] if c["id"] == "missing_plan_evidence_ids"][0]
            self.assertEqual(missing["status"], "warning")
            write_proof_metadata(root, proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json", strict=True)

            errors = [f for f in findings if f["severity"] == "error"]
            self.assertTrue(any("missing_plan_evidence_ids" in f["message"] for f in errors))

    def test_recorded_strict_promotes_persisted_warning_checks_on_lax_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            write_json(
                root / ".agent/plan.lock.json",
                {**PLAN_CONTRACT_FIELDS, 
                    "schema_version": "0.3.0",
                    "objective": "o",
                    "scope": ["s"],
                    "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "evidence_ids": []}],
                    "evidence_ids": ["E-missing"],
                },
            )
            proof = build_proof(root, root / ".agent/plan.lock.json", strict=True)
            missing = [c for c in proof["checks"] if c["id"] == "missing_plan_evidence_ids"][0]
            self.assertEqual(missing["status"], "warning")
            self.assertTrue(proof["review"]["policy"]["proof_strict_effective"])
            write_proof_metadata(root, proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            errors = [f for f in findings if f["severity"] == "error"]
            self.assertTrue(any("missing_plan_evidence_ids" in f["message"] for f in errors))

    def test_strict_promotes_failing_gate_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, gate_status="fail", active=["BP-001"])
            findings = verify_proof(root, root / ".agent/proof-pack.json", strict=True)
            errors = [f for f in findings if f["severity"] == "error"]
            self.assertTrue(any("review_gate" in f["message"] for f in errors))

    def test_strict_built_failing_gate_errors_on_lax_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, gate_status="fail", active=["BP-001"], strict=True)
            findings = verify_proof(root, root / ".agent/proof-pack.json")  # no strict
            gate = [f for f in findings if "review_gate" in f["message"]]
            self.assertTrue(gate)
            self.assertTrue(all(f["severity"] == "error" for f in gate))

    def test_warn_built_failing_gate_only_warns_on_lax_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fixture(tmp, gate_status="fail", active=["BP-001"], strict=False)
            findings = verify_proof(root, root / ".agent/proof-pack.json")  # no strict
            gate = [f for f in findings if "review_gate" in f["message"]]
            self.assertTrue(gate)
            self.assertTrue(all(f["severity"] == "warning" for f in gate))


class HunkProofSummaryTests(unittest.TestCase):
    def _minimal_plan(self) -> dict:
        return {**PLAN_CONTRACT_FIELDS, "schema_version": "0.3.0", "objective": "x",
                "allowed_files": ["b.py"], "blocked_files": [], "risk_level": "low",
                "drift_budget": {"unrelated_edits": 0, "new_dependencies": 0,
                                 "formatting_drift": "minimal", "architecture_drift": "requires_approval",
                                 "test_weakening": 0},
                "steps": [], "evidence_ids": [], "locked": True, "locked_at": "2026-06-01T00:00:00+00:00"}

    def test_execution_summary_lists_unmapped_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            write_json(
                root / ".agent/drift-report.json",
                {
                    "schema_version": "0.2.1", "status": "fail",
                    "changed_files": ["a.py", "b.py"],
                    "unmapped_hunks": [
                        {"path": "b.py", "hash": "f" * 64, "old_start": 1, "old_count": 0,
                         "new_start": 1, "new_count": 2, "reason": "no_matching_hunk"},
                        {"path": "b.py", "hash": "e" * 64, "old_start": 9, "old_count": 0,
                         "new_start": 9, "new_count": 1, "reason": "no_matching_hunk"},
                    ],
                    "out_of_scope_files": [], "blocked_files_changed": [],
                    "dependency_changes": [], "test_weakening": [], "notes": [],
                    "generated_at": "2026-06-22T00:00:00+00:00",
                },
            )
            summary = execution_summary(root, self._minimal_plan())
            self.assertEqual(summary["unmapped_changed_files"], ["b.py"])

    def test_observe_unmapped_does_not_fail_strict_proof(self) -> None:
        from agentflow.execution import claim_step
        from agentflow.receipts import record_file_change
        from agentflow.validation import audit_drift
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            # Flip the contract to observe mode.
            contract_path = root / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["proof_policy"]["hunk_attribution"] = "observe"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            plan = self._minimal_plan()
            plan["allowed_files"] = ["fixture.txt", ".agent/"]
            plan["steps"] = [{"id": "P1", "action": "edit", "files": ["fixture.txt"],
                              "preconditions": [], "expected_diff": [], "validation": [], "evidence_ids": []}]
            write_json(root / ".agent/plan.lock.json", plan)
            claim_step(root, plan, "P1", "agent-a")
            seed = "\n".join(f"l{i}" for i in range(1, 21)) + "\n"
            (root / "fixture.txt").write_text(seed, encoding="utf-8")
            subprocess.run(["git", "add", "fixture.txt"], cwd=str(root), check=True)
            subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-m", "seed"],
                cwd=str(root), check=True, stdout=subprocess.PIPE,
            )
            lines = seed.splitlines()
            lines[1] = "RECORDED"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            record_file_change(root, plan, "P1", None, "fixture.txt")
            lines[18] = "STRAY"
            (root / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            write_json(root / ".agent/drift-report.json", audit_drift(root, plan))
            proof = build_proof(root, root / ".agent/plan.lock.json", strict=True)
            findings = verify_proof_checks(proof, strict=True)
            self.assertFalse(
                any("drift_audit" in f.get("message", "") and f["severity"] == "error" for f in findings)
            )


class StuckProofTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        create_initial_artifacts(root)
        plan = {**PLAN_CONTRACT_FIELDS, 
            "schema_version": "0.2.0",
            "objective": "Stuck fixture.",
            "scope": ["Stuck fixture."],
            "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "action": "Do work.", "evidence_ids": ["E1"]}],
            "evidence_ids": ["E1"],
            "context_budget": {"receipts_required": False},
        }
        write_json(root / ".agent/plan.lock.json", plan)
        append_jsonl(
            root / ".agent/evidence.jsonl",
            {
                "schema_version": "0.2.0",
                "id": "E1",
                "claim": "P1 completed.",
                "source": "tests/test_proof.py",
                "confidence": "high",
                "last_verified": "2026-05-31T00:00:00+00:00",
                "supports": ["P1"],
            },
        )
        for index in range(3):
            append_jsonl(
                root / ".agent/command-receipts.jsonl",
                {
                    "schema_version": "0.3.0",
                    "id": f"CRX{index}",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "provenance": "observed",
                    "command": ["pytest", "-k", "foo"],
                    "cwd": ".",
                    "started_at": f"2026-06-30T10:0{index}:00+00:00",
                    "finished_at": f"2026-06-30T10:0{index}:01+00:00",
                    "exit_code": 1,
                    "decision": "allowed",
                },
            )
        return root

    def test_proof_has_stuck_block_in_core_not_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertIn("stuck", proof)
            self.assertEqual(proof["stuck"]["rules_evaluated"], 3)
            self.assertTrue(
                any(
                    finding["rule"] == "repeated_command_failure"
                    for finding in proof["stuck"]["findings"]
                )
            )
            self.assertNotIn("stuck", [check["id"] for check in proof["checks"]])

    def test_stuck_block_is_covered_by_core_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            self.assertIn("stuck", canonical_core(proof))

    def test_proof_schema_version_is_bumped(self) -> None:
        self.assertEqual(PROOF_PACK_SCHEMA_VERSION, "0.11.0")


class AggregationProvenanceProofTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        create_initial_artifacts(root)
        plan = {**PLAN_CONTRACT_FIELDS, 
            "schema_version": "0.2.0",
            "objective": "Aggregation fixture.",
            "scope": ["Aggregation fixture."],
            "steps": [{**STEP_CONTRACT_FIELDS, "id": "P1", "action": "Do work.", "evidence_ids": ["E1"]}],
            "evidence_ids": ["E1"],
            "context_budget": {"receipts_required": False},
        }
        write_json(root / ".agent/plan.lock.json", plan)
        append_jsonl(
            root / ".agent/evidence.jsonl",
            {
                "schema_version": "0.2.0",
                "id": "E1",
                "claim": "P1 completed.",
                "source": "tests/test_proof.py",
                "confidence": "high",
                "last_verified": "2026-05-31T00:00:00+00:00",
                "supports": ["P1"],
            },
        )
        return root

    def _aggregation_manifest(self) -> dict:
        return {
            "schema_version": AGGREGATION_SCHEMA_VERSION,
            "mode": "cross_worktree",
            "source_count": 2,
            "sources": [
                {
                    "source_id": "alpha",
                    "root_label": "worktree-alpha",
                    "base_commit": "abc123",
                    "head_commit": "def456",
                    "namespaced_prefix": "WTalpha-",
                },
                {
                    "source_id": "beta",
                    "root_label": "worktree-beta",
                    "base_commit": "abc123",
                    "head_commit": "ghi789",
                    "namespaced_prefix": "WTbeta-",
                },
            ],
        }
    def test_proof_without_aggregation_file_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".agent/aggregation.json").unlink(missing_ok=True)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            self.assertNotIn("aggregation", canonical_core(proof))
            self.assertNotIn(
                "aggregation_valid", [check["id"] for check in proof["checks"]]
            )
            self.assertNotIn(".agent/aggregation.json", proof["generated_from"])

    def test_proof_embeds_aggregation_manifest_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(proof["aggregation"], manifest)
            self.assertIn(".agent/aggregation.json", proof["generated_from"])
            file_paths = {item["path"] for item in proof["files"]}
            self.assertIn(".agent/aggregation.json", file_paths)
            self.assertEqual(canonical_core(proof)["aggregation"], manifest)

    def test_future_supported_major_is_not_rejected_by_syntax_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["schema_version"] = "1.0.0"
            write_json(root / ".agent/aggregation.json", manifest)

            with patch("agentflow.proof.AGGREGATION_SCHEMA_VERSION", "1.0.0"):
                proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(proof["aggregation"], manifest)

    def test_future_major_is_rejected_until_policy_supports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["schema_version"] = "1.0.0"
            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertIn("incompatible", checks["aggregation_valid"]["message"])

    def test_core_hash_changes_when_aggregation_block_is_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_json(root / ".agent/aggregation.json", self._aggregation_manifest())
            proof = build_proof(root, root / ".agent/plan.lock.json")

            mutated = json.loads(json.dumps(proof))
            mutated["aggregation"]["sources"][0]["source_id"] = "tampered"

            self.assertNotEqual(core_sha256(proof), core_sha256(mutated))

    def test_verify_proof_detects_aggregation_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_json(root / ".agent/aggregation.json", self._aggregation_manifest())
            proof = build_proof(root, root / ".agent/plan.lock.json")
            proof_path = write_proof_metadata(root, proof)

            tampered_manifest = self._aggregation_manifest()
            tampered_manifest["sources"][0]["source_id"] = "tampered"
            write_json(root / ".agent/aggregation.json", tampered_manifest)

            findings = verify_proof(root, proof_path)

            self.assertTrue(
                any(
                    "hash mismatch for .agent/aggregation.json" in finding["message"]
                    for finding in findings
                )
            )

    def test_malformed_aggregation_json_fails_check_and_omits_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".agent/aggregation.json").write_text("{not json", encoding="utf-8")

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertIn("aggregation_valid", checks)
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")

    def test_empty_object_aggregation_json_fails_shape_check_and_omits_block(
        self,
    ) -> None:
        # A well-formed-JSON-but-wrong-shape aggregation.json (e.g. `{}`) must
        # not be embedded verbatim -- it would produce a proof["aggregation"]
        # block that violates the pack schema with no failed check to explain
        # why.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_json(root / ".agent/aggregation.json", {})

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertIn("aggregation_valid", checks)
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            message = checks["aggregation_valid"]["message"]
            self.assertIn("schema_version", message)
            self.assertIn("mode", message)
            self.assertIn("source_count", message)
            self.assertIn("sources", message)

    def test_aggregation_json_missing_all_fields_fails_shape_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            write_json(root / ".agent/aggregation.json", {"foo": 1})

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")

            findings = verify_proof_checks(proof)
            self.assertTrue(
                any(
                    finding["severity"] == "error" and "aggregation_valid" in finding["message"]
                    for finding in findings
                )
            )

    def test_wrong_mode_fails_shape_check_and_omits_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = {
                "schema_version": "0.1.0",
                "mode": "single",
                "source_count": 1,
                "sources": [
                    {
                        "source_id": "alpha",
                        "root_label": "worktree-alpha",
                        "base_commit": "abc123",
                        "head_commit": "def456",
                        "namespaced_prefix": "WTalpha-",
                    }
                ],
            }
            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("mode", checks["aggregation_valid"]["message"])

    def test_source_count_mismatch_fails_shape_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["sources"] = [manifest["sources"][0]]
            manifest["source_count"] = 2

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("source_count", checks["aggregation_valid"]["message"])

    def test_sources_non_list_fails_shape_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["sources"] = "nope"

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("sources", checks["aggregation_valid"]["message"])

    def test_source_entry_missing_field_fails_shape_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            del manifest["sources"][0]["head_commit"]

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("head_commit", checks["aggregation_valid"]["message"])

    def test_schema_version_and_id_patterns_fail_shape_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["schema_version"] = "bogus"
            manifest["sources"][0]["source_id"] = "BAD"
            manifest["sources"][0]["namespaced_prefix"] = "WTBAD-"

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            message = checks["aggregation_valid"]["message"]
            self.assertIn("schema_version", message)
            self.assertIn("source_id", message)
            self.assertIn("namespaced_prefix", message)

    def test_schema_version_pattern_fail_shape_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["schema_version"] = "0.01.0"

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("schema_version", checks["aggregation_valid"]["message"])

    def test_schema_version_length_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["schema_version"] = "0.1." + ("9" * 636)
            self.assertEqual(len(manifest["schema_version"]), 640)

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(proof["aggregation"], manifest)

            manifest["schema_version"] = "0.1." + ("9" * 637)
            self.assertEqual(len(manifest["schema_version"]), 641)

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("at most 640 characters", checks["aggregation_valid"]["message"])

    def test_valid_manifest_with_extra_keys_embeds_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            manifest = self._aggregation_manifest()
            manifest["extra_top_level"] = "allowed"
            manifest["sources"][0]["extra_source_key"] = "also allowed"

            write_json(root / ".agent/aggregation.json", manifest)

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertEqual(proof["aggregation"], manifest)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertNotIn("aggregation_valid", checks)

    def test_non_object_aggregation_json_fails_check_and_omits_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".agent/aggregation.json").write_text("[1, 2, 3]", encoding="utf-8")

            proof = build_proof(root, root / ".agent/plan.lock.json")

            self.assertNotIn("aggregation", proof)
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertIn("aggregation_valid", checks)
            self.assertEqual(checks["aggregation_valid"]["status"], "failed")
            self.assertIn("must be a JSON object", checks["aggregation_valid"]["message"])


class ProofSchemaGateTests(unittest.TestCase):
    def test_newer_proof_schema_requests_upgrade_without_tamper_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            complete_initial_plan(root)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            proof["schema_version"] = "0.12.0"
            proof_path = write_proof_metadata(root, proof)

            findings = verify_proof(root, proof_path)
            messages = "\n".join(finding["message"] for finding in findings)

            self.assertIn("newer", messages)
            self.assertIn("upgrade Agentflow", messages)
            self.assertNotIn("tamper", messages.lower())

    def test_newer_schema_gate_precedes_current_shape_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)
            complete_initial_plan(root)
            proof = build_proof(root, root / ".agent/plan.lock.json")
            proof["schema_version"] = "0.12.0"
            del proof["meta"]
            proof_path = write_proof_metadata(root, proof)

            findings = verify_proof(root, proof_path)
            messages = "\n".join(finding["message"] for finding in findings)

            self.assertIn("newer schema", messages)
            self.assertIn("upgrade Agentflow", messages)
            self.assertNotIn("missing required field meta", messages)


if __name__ == "__main__":
    unittest.main()
