from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentflow.contracts import AMENDMENTS_SCHEMA_VERSION, FAILURES_SCHEMA_VERSION
from agentflow.validation import validate_plan


ROOT = Path(__file__).resolve().parents[1]


def run_agentflow(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def valid_plan() -> dict:
    return {
        "schema_version": "0.1.0",
        "objective": "Create a focused test fixture.",
        "scope": ["Add a fixture and prove it works."],
        "non_goals": ["No packaging changes."],
        "invariants": ["Existing command behavior is unchanged."],
        "allowed_files": ["fixture.txt", ".agent/"],
        "blocked_files": ["secrets/"],
        "validation_gates": ["manual inspection"],
        "rollback_plan": "Delete fixture.txt and rerun validation.",
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
                "id": "P1",
                "action": "Create fixture file.",
                "files": ["fixture.txt"],
                "preconditions": ["Workspace initialized."],
                "expected_diff": ["New fixture file."],
                "validation": ["manual inspection"],
                "evidence_ids": ["E1"],
            }
        ],
        "evidence_ids": ["E1"],
        "locked": False,
        "locked_at": None,
    }


class AgentflowCliTests(unittest.TestCase):
    def test_init_creates_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(cwd, "init")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((cwd / ".agent/plan.lock.json").exists())
            self.assertTrue((cwd / ".agent/model-profiles/openai.example.json").exists())

    def test_init_creates_current_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(cwd, "init")
            self.assertEqual(result.returncode, 0, result.stderr)

            plan = json.loads((cwd / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], "0.3.0")
            self.assertTrue((cwd / ".agent/runtime-snapshots.jsonl").exists())
            self.assertFalse((cwd / ".agent/runtime.config.json").exists())

    def test_validate_plan_rejects_empty_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            result = run_agentflow(cwd, "validate-plan")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("objective must not be empty", result.stdout)

    def test_validate_plan_accepts_valid_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(valid_plan(), indent=2),
                encoding="utf-8",
            )
            result = run_agentflow(cwd, "validate-plan")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("plan valid", result.stdout)

    def test_validate_plan_accepts_v01_plan_under_v02_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.1.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = run_agentflow(cwd, "validate-plan")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("plan valid", result.stdout)

    def test_validate_plan_rejects_incompatible_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.4.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = run_agentflow(cwd, "validate-plan")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid plan:", result.stderr)
            self.assertIn("plan-lock schema_version 0.4.0 is incompatible", result.stderr)

    def test_validate_plan_rejects_invalid_step_delegation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            plan["steps"][0]["execution_mode"] = "automatic"
            plan["steps"][0]["authority"] = "admin"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = run_agentflow(cwd, "validate-plan")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("steps[1].execution_mode must be one of", result.stdout)
            self.assertIn("steps[1].authority must be one of", result.stdout)

    def test_validate_plan_accepts_v03_depends_on_and_typed_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["steps"].append(
                {
                    "id": "P2",
                    "action": "Inspect fixture.",
                    "files": ["fixture.txt"],
                    "preconditions": ["P1 completed."],
                    "expected_diff": ["No source changes."],
                    "validation": ["inspection complete"],
                    "evidence_ids": ["E1"],
                    "depends_on": ["P1"],
                    "gates": [
                        {"kind": "command", "run": ["python3", "-c", "print('ok')"]},
                        {
                            "kind": "inspection",
                            "evidence_id": "E1",
                            "describe": "Fixture reviewed.",
                        },
                    ],
                }
            )
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )

            result = run_agentflow(cwd, "validate-plan")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("plan valid", result.stdout)

    def test_lock_plan_accepts_multiple_requirements_and_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["requirements"] = [
                {
                    "id": "REQ-1",
                    "text": "Command and inspection behavior works.",
                    "acceptance_criteria": [
                        {"id": "AC-1", "text": "The command passes."},
                        {"id": "AC-2", "text": "The inspection is recorded."},
                    ],
                },
                {
                    "id": "REQ-2",
                    "text": "Review behavior works.",
                    "acceptance_criteria": [
                        {
                            "id": "AC-3",
                            "text": "The spec-quality review passes.",
                            "review": {"minimum_depth": "spec_quality"},
                        }
                    ],
                },
            ]
            plan["steps"][0]["criterion_ids"] = ["AC-1", "AC-2", "AC-3"]
            plan["steps"][0]["gates"] = [
                {
                    "kind": "command",
                    "run": ["python3", "-m", "unittest"],
                    "criterion_ids": ["AC-1"],
                },
                {
                    "kind": "inspection",
                    "evidence_id": "E1",
                    "describe": "Inspect the fixture.",
                    "criterion_ids": ["AC-2"],
                },
            ]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "locked")

    def test_validate_plan_rejects_unknown_and_cyclic_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["steps"][0]["depends_on"] = ["P2"]
            plan["steps"].append(
                {
                    "id": "P2",
                    "action": "Second step.",
                    "files": ["fixture.txt"],
                    "preconditions": ["P1 completed."],
                    "expected_diff": ["Second edit."],
                    "validation": ["manual inspection"],
                    "evidence_ids": ["E1"],
                    "depends_on": ["P1", "PX"],
                }
            )
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )

            result = run_agentflow(cwd, "validate-plan")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("steps[2].depends_on references unknown step id: PX", result.stdout)
            self.assertIn("depends_on cycle detected", result.stdout)

    def test_lock_plan_from_stdin_writes_canonical_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["objective"] = "Lock adapter-authored stdin plan."

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "locked")
            self.assertEqual(payload["errors"], [])
            self.assertEqual(Path(payload["path"]).name, "plan.lock.json")
            locked = json.loads((cwd / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            self.assertTrue(locked["locked"])
            self.assertIsInstance(locked["locked_at"], str)
            self.assertEqual(locked["objective"], "Lock adapter-authored stdin plan.")

    def test_lock_plan_from_json_file_writes_canonical_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["objective"] = "Lock adapter-authored file plan."
            source = cwd / "adapter-plan.json"
            source.write_text(json.dumps(plan), encoding="utf-8")

            result = run_agentflow(cwd, "lock-plan", "--from-json", str(source), "--json")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "locked")
            self.assertEqual(payload["errors"], [])
            locked = json.loads((cwd / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            self.assertTrue(locked["locked"])
            self.assertEqual(locked["objective"], "Lock adapter-authored file plan.")

    def test_lock_plan_json_diagnostics_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text="{not json",
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "")
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "invalid")
            self.assertEqual(payload["errors"][0]["code"], "invalid_json")

    def test_lock_plan_json_diagnostics_for_invalid_plan_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            del plan["objective"]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "")
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "invalid")
            self.assertIn(
                {"code": "validation_error", "message": "missing required field: objective"},
                payload["errors"],
            )
            unchanged = json.loads((cwd / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            self.assertEqual(unchanged["objective"], "")
            self.assertFalse(unchanged["locked"])

    def test_lock_plan_json_rejects_duplicate_requirement_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            requirement = {
                "id": "REQ-1",
                "text": "Trace the requirement.",
                "acceptance_criteria": [
                    {"id": "AC-1", "text": "The criterion is traced."}
                ],
            }
            plan["requirements"] = [requirement, {**requirement, "acceptance_criteria": [
                {"id": "AC-2", "text": "A second criterion is traced."}
            ]}]
            plan["steps"][0]["criterion_ids"] = ["AC-1", "AC-2"]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn(
                {"code": "validation_error", "message": "duplicate requirement id: REQ-1"},
                payload["errors"],
            )

    def test_lock_plan_json_rejects_duplicate_acceptance_criterion_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["requirements"] = [
                {
                    "id": "REQ-1",
                    "text": "Trace the requirement.",
                    "acceptance_criteria": [
                        {"id": "AC-1", "text": "First criterion."},
                        {"id": "AC-1", "text": "Duplicate criterion."},
                    ],
                }
            ]
            plan["steps"][0]["criterion_ids"] = ["AC-1"]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn(
                {
                    "code": "validation_error",
                    "message": "duplicate acceptance criterion id: AC-1",
                },
                payload["errors"],
            )

    def test_lock_plan_json_rejects_dangling_criterion_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["requirements"] = [
                {
                    "id": "REQ-1",
                    "text": "Trace the requirement.",
                    "acceptance_criteria": [
                        {"id": "AC-1", "text": "Known criterion."}
                    ],
                }
            ]
            plan["steps"][0]["criterion_ids"] = ["AC-MISSING"]
            plan["steps"][0]["gates"] = [
                {
                    "kind": "command",
                    "run": ["python3", "-c", "print('ok')"],
                    "criterion_ids": ["AC-MISSING"],
                }
            ]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            messages = {item["message"] for item in json.loads(result.stdout)["errors"]}
            self.assertIn(
                "steps[1].criterion_ids references unknown acceptance criterion id: AC-MISSING",
                messages,
            )
            self.assertIn(
                "steps[1].gates[1].criterion_ids references unknown acceptance criterion id: AC-MISSING",
                messages,
            )

    def test_lock_plan_json_rejects_criterion_without_step_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["requirements"] = [
                {
                    "id": "REQ-1",
                    "text": "Trace the requirement.",
                    "acceptance_criteria": [
                        {"id": "AC-1", "text": "Criterion needs an implementing step."}
                    ],
                }
            ]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            messages = {item["message"] for item in json.loads(result.stdout)["errors"]}
            self.assertIn(
                "acceptance criterion AC-1 is not mapped to any step",
                messages,
            )

    def test_lock_plan_json_rejects_gate_criterion_from_another_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["requirements"] = [
                {
                    "id": "REQ-1",
                    "text": "Trace both implementation steps.",
                    "acceptance_criteria": [
                        {"id": "AC-1", "text": "First step passes."},
                        {"id": "AC-2", "text": "Second step passes."},
                    ],
                }
            ]
            plan["steps"][0]["criterion_ids"] = ["AC-1"]
            plan["steps"][0]["gates"] = [
                {
                    "kind": "command",
                    "run": ["python3", "-c", "print('ok')"],
                    "criterion_ids": ["AC-2"],
                }
            ]
            second_step = dict(plan["steps"][0])
            second_step.update(
                {
                    "id": "P2",
                    "action": "Implement the second criterion.",
                    "criterion_ids": ["AC-2"],
                }
            )
            second_step.pop("gates")
            plan["steps"].append(second_step)

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            messages = {item["message"] for item in json.loads(result.stdout)["errors"]}
            self.assertIn(
                "steps[1].gates[1].criterion_ids must be a subset of "
                "steps[1].criterion_ids; invalid id: AC-2",
                messages,
            )

    def test_lock_plan_json_rejects_malformed_traceability_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["requirements"] = [
                {
                    "id": "REQ 1",
                    "text": "",
                    "acceptance_criteria": [
                        {
                            "id": "1AC",
                            "text": "",
                            "review": {"minimum_depth": "standard"},
                        }
                    ],
                }
            ]
            plan["steps"][0]["criterion_ids"] = ["1AC", "1AC"]

            result = run_agentflow(
                cwd,
                "lock-plan",
                "--stdin",
                "--json",
                input_text=json.dumps(plan),
            )

            self.assertEqual(result.returncode, 1)
            messages = {item["message"] for item in json.loads(result.stdout)["errors"]}
            self.assertIn("requirements[1].id has invalid stable id: REQ 1", messages)
            self.assertIn("requirements[1].text must be a non-empty string", messages)
            self.assertIn(
                "requirements[1].acceptance_criteria[1].id has invalid stable id: 1AC",
                messages,
            )
            self.assertIn(
                "requirements[1].acceptance_criteria[1].review.minimum_depth must be one of: spec_quality, deep",
                messages,
            )
            self.assertIn("steps[1].criterion_ids contains duplicate id: 1AC", messages)

    def test_lock_plan_json_distinguishes_criterion_id_collection_errors(self) -> None:
        cases = (
            (None, "steps[1].criterion_ids must be a list"),
            ([], "steps[1].criterion_ids must contain at least one criterion id"),
            ([""], "steps[1].criterion_ids must contain only non-empty strings"),
        )
        for value, expected in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
                plan = valid_plan()
                plan["requirements"] = [
                    {
                        "id": "REQ-1",
                        "text": "Trace the requirement.",
                        "acceptance_criteria": [
                            {"id": "AC-1", "text": "Known criterion."}
                        ],
                    }
                ]
                plan["steps"][0]["criterion_ids"] = value

                result = run_agentflow(
                    cwd,
                    "lock-plan",
                    "--stdin",
                    "--json",
                    input_text=json.dumps(plan),
                )

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "")
                messages = {
                    item["message"] for item in json.loads(result.stdout)["errors"]
                }
                self.assertIn(expected, messages)

    def test_lock_plan_json_diagnostics_use_json_decode_cause_for_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            source = cwd / "invalid JSON but valid non-object.json"
            source.write_text("[]", encoding="utf-8")

            result = run_agentflow(cwd, "lock-plan", "--from-json", str(source), "--json")

            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "invalid")
            self.assertEqual(payload["errors"][0]["code"], "invalid_plan_json")

    def test_lock_plan_preserves_existing_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            source = cwd / "custom-plan.json"
            source.write_text(json.dumps(valid_plan(), indent=2), encoding="utf-8")

            result = run_agentflow(cwd, "lock-plan", str(source))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"locked {source.resolve()}", result.stdout)
            locked = json.loads(source.read_text(encoding="utf-8"))
            self.assertTrue(locked["locked"])
            self.assertIsInstance(locked["locked_at"], str)
            root_plan = json.loads((cwd / ".agent/plan.lock.json").read_text(encoding="utf-8"))
            self.assertFalse(root_plan["locked"])

    def test_init_creates_capability_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertTrue((cwd / ".agent/capability-receipts.jsonl").exists())

    def test_record_capability_writes_used_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            result = run_agentflow(
                cwd,
                "record-capability",
                "--id", "CAP1",
                "--capability", "tdd",
                "--provider", "manual",
                "--reason", "red-green-refactor",
                "--evidence", "E1",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = [
                json.loads(line)
                for line in (cwd / ".agent/capability-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(rows[0]["status"], "used")
            self.assertEqual(rows[0]["provider"], "manual")
            self.assertEqual(rows[0]["evidence"], ["E1"])

    def test_waive_capability_omits_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            result = run_agentflow(
                cwd,
                "waive-capability",
                "--id", "CAP2",
                "--capability", "frontend-qa",
                "--reason", "no frontend files changed",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = [
                json.loads(line)
                for line in (cwd / ".agent/capability-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(rows[0]["status"], "waived")
            self.assertNotIn("provider", rows[0])

    def test_record_capability_rejects_empty_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            result = run_agentflow(
                cwd,
                "record-capability",
                "--id", "CAP1",
                "--capability", "   ",
                "--provider", "manual",
                "--reason", "r",
            )
            self.assertEqual(result.returncode, 1)

    def test_record_evidence_accepts_kind_and_supports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)

            result = run_agentflow(
                cwd,
                "record-evidence",
                "--id", "E1",
                "--kind", "file",
                "--claim", "CLI records evidence kind.",
                "--source", "src/agentflow/cli.py",
                "--supports", "P1",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = [
                json.loads(line)
                for line in (cwd / ".agent/evidence.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(entries[0]["kind"], "file")
            self.assertEqual(entries[0]["supports"], ["P1"])

    def test_record_context_appends_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)

            result = run_agentflow(
                cwd,
                "record-context",
                "--id", "C1",
                "--source", "src/agentflow/cli.py",
                "--reason", "Inspect CLI layout.",
                "--used-for", "P1",
                "--bytes", "42",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = [
                json.loads(line)
                for line in (cwd / ".agent/context-receipts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(entries[0]["schema_version"], "0.2.0")
            self.assertEqual(entries[0]["used_for"], ["P1"])
            self.assertEqual(entries[0]["bytes"], 42)

    def test_record_context_rejects_negative_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)

            result = run_agentflow(
                cwd,
                "record-context",
                "--id", "C1",
                "--source", "src/agentflow/cli.py",
                "--reason", "Invalid size.",
                "--bytes", "-1",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((cwd / ".agent/context-receipts.jsonl").read_text(encoding="utf-8"), "")

    def test_runtime_status_json_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            config = {
                "schema_version": "0.2.0",
                "runtimes": {
                    "local": {
                        "adapter": "custom",
                        "enabled": True,
                        "capabilities": {"declared": ["chat"], "required": ["chat"]},
                        "readiness": {"check": "none"},
                    }
                },
                "routes": {"reviewer": {"primary": "local", "requires": ["chat"]}},
            }
            (cwd / ".agent/runtime.config.json").write_text(json.dumps(config), encoding="utf-8")

            result = run_agentflow(cwd, "runtime-status", "--json", "--record")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["runtimes"][0]["status"], "configured")
            snapshots = (cwd / ".agent/runtime-snapshots.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(snapshots), 1)

    def test_runtime_status_prints_mcp_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".agent").mkdir()
            config = {
                "schema_version": "0.3.0",
                "default_runtime": "local",
                "runtimes": {
                    "local": {
                        "adapter": "custom",
                        "enabled": True,
                        "readiness": {"check": "none"},
                    }
                },
                "routes": {},
                "mcp_servers": {
                    "github": {
                        "enabled": True,
                        "transport": "stdio",
                        "declared_tools": ["create_issue"],
                    }
                },
            }
            (cwd / ".agent/runtime.config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            result = run_agentflow(cwd, "runtime-status")
            self.assertEqual(result.returncode, 0)
            self.assertIn("mcp github configured", result.stdout)

    def test_build_proof_writes_structured_metadata_and_reports_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = run_agentflow(cwd, "build-proof")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            proof = json.loads((cwd / ".agent/proof-pack.json").read_text(encoding="utf-8"))
            self.assertEqual(proof["bundle_version"], "0.10.0")
            self.assertIn(".agent/plan.lock.json", proof["generated_from"])
            self.assertEqual(proof["coverage"]["missing_plan_evidence_ids"], ["E1"])
            check_ids = [check["id"] for check in proof["checks"]]
            self.assertIn("missing_plan_evidence_ids", check_ids)
            self.assertTrue((cwd / ".agent/proof-pack.md").exists())

    def test_build_proof_rejects_invalid_requirement_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            criterion = {"id": "AC-1", "text": "The fixture is created."}
            plan["requirements"] = [
                {
                    "id": "REQ-1",
                    "text": "Create the fixture.",
                    "acceptance_criteria": [criterion, dict(criterion)],
                }
            ]
            plan["steps"][0]["criterion_ids"] = ["AC-1"]
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2), encoding="utf-8"
            )

            result = run_agentflow(cwd, "build-proof")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid requirement traceability", result.stderr)
            self.assertIn("duplicate acceptance criterion id: AC-1", result.stderr)
            self.assertFalse((cwd / ".agent/proof-pack.json").exists())

    def test_build_proof_malformed_runtime_config_returns_warning_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            (cwd / ".agent/runtime.config.json").write_text("{ broken", encoding="utf-8")

            result = run_agentflow(cwd, "build-proof")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            proof = json.loads((cwd / ".agent/proof-pack.json").read_text(encoding="utf-8"))
            runtime_check = next(
                check for check in proof["checks"] if check["id"] == "runtime_config_readable"
            )
            self.assertEqual(runtime_check["status"], "warning")
            self.assertIn("malformed", runtime_check["message"])

    def test_build_proof_malformed_jsonl_returns_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            (cwd / ".agent/evidence.jsonl").write_text("{ broken\n", encoding="utf-8")

            result = run_agentflow(cwd, "build-proof")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid ledger", result.stderr)
            self.assertIn("invalid JSONL", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_build_proof_strict_promotes_warning_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            result = run_agentflow(cwd, "build-proof", "--strict")

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((cwd / ".agent/proof-pack.json").exists())

    def test_build_proof_strict_promotes_context_budget_warning_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            plan["context_budget"] = {"max_total_bytes": 1}
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            context = {
                "schema_version": "0.2.0",
                "id": "C1",
                "source": "src/agentflow/cli.py",
                "reason": "Large context.",
                "used_for": ["P1"],
                "bytes": 100,
                "created_at": "2026-05-31T00:00:00+00:00",
            }
            (cwd / ".agent/context-receipts.jsonl").write_text(
                json.dumps(context) + "\n",
                encoding="utf-8",
            )

            result = run_agentflow(cwd, "build-proof", "--strict")

            self.assertNotEqual(result.returncode, 0)
            proof = json.loads((cwd / ".agent/proof-pack.json").read_text(encoding="utf-8"))
            checks = {check["id"]: check for check in proof["checks"]}
            self.assertEqual(checks["context_max_total_bytes_exceeded"]["status"], "warning")

    def test_runtime_status_reports_malformed_config_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            (cwd / ".agent/runtime.config.json").write_text("{ broken", encoding="utf-8")

            result = run_agentflow(cwd, "runtime-status", "--json")

            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["findings"][0]["id"], "runtime_config_malformed")
            self.assertEqual(result.stderr, "")

    def test_runtime_status_strict_fails_on_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            config = {
                "schema_version": "0.2.0",
                "runtimes": {
                    "local": {
                        "adapter": "go-llm",
                        "enabled": True,
                        "readiness": {
                            "check": "command_exists",
                            "command": "/nonexistent/bin",
                        },
                    }
                },
                "routes": {},
            }
            (root / ".agent/runtime.config.json").write_text(
                json.dumps(config),
                encoding="utf-8",
            )

            rc_default = run_agentflow(root, "runtime-status").returncode
            rc_strict = run_agentflow(root, "runtime-status", "--strict").returncode

            self.assertEqual(rc_default, 0)
            self.assertEqual(rc_strict, 1)

    def test_record_failure_uses_failures_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_agentflow(root, "init").returncode, 0)

            result = run_agentflow(
                root,
                "record-failure",
                "--command",
                "pytest",
                "--failure-type",
                "assertion",
                "--suspected-cause",
                "x",
                "--next-action",
                "y",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            entry = json.loads((root / ".agent/failures.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(entry["schema_version"], FAILURES_SCHEMA_VERSION)

    def test_amend_plan_uses_amendments_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_agentflow(root, "init").returncode, 0)

            result = run_agentflow(
                root,
                "amend-plan",
                "--id",
                "A1",
                "--reason",
                "scope correction",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            entry = json.loads((root / ".agent/amendments.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(entry["schema_version"], AMENDMENTS_SCHEMA_VERSION)

    def test_verify_proof_passes_then_detects_modified_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            self.assertEqual(run_agentflow(cwd, "build-proof").returncode, 0)

            clean = run_agentflow(cwd, "verify-proof")
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            self.assertIn("proof verified", clean.stdout)

            plan["objective"] = "Changed after proof."
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            dirty = run_agentflow(cwd, "verify-proof")
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("hash mismatch", dirty.stdout)

    def test_verify_proof_accepts_replay_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            self.assertEqual(run_agentflow(cwd, "build-proof").returncode, 0)

            result = run_agentflow(cwd, "verify-proof", "--replay")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("proof verified", result.stdout)

    def _seed_failing_gate_proof(self, cwd: Path) -> None:
        from agentflow.review import sha256_file

        self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
        plan = valid_plan()
        plan["schema_version"] = "0.3.0"
        (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        state = cwd / "docs/ai/state/main"
        state.mkdir(parents=True)
        manifest_path = state / "review-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "0.1.0",
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "state_dir": "docs/ai/state/main",
                    "gate_status": "fail",
                    "active_blocking": ["BP-001"],
                    "findings": {"index": []},
                    "artifacts": [{"path": "findings-final.json"}],
                }
            ),
            encoding="utf-8",
        )
        (cwd / ".agent/review-runs.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "0.3.0",
                    "review_run_id": "RR-20260620T180000Z-ab12cd34",
                    "recorded_at": "2026-06-20T18:00:00+00:00",
                    "state_dir": "docs/ai/state/main",
                    "manifest_path": "docs/ai/state/main/review-manifest.json",
                    "manifest_sha256": sha256_file(manifest_path),
                    "gate_status": "fail",
                    "active_blocking": ["BP-001"],
                    "findings": {"index": []},
                    "artifacts": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(run_agentflow(cwd, "build-proof").returncode, 0)

    def test_verify_proof_warns_on_failing_gate_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._seed_failing_gate_proof(cwd)
            result = run_agentflow(cwd, "verify-proof")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("proof verified", result.stdout)

    def test_verify_proof_strict_fails_on_failing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._seed_failing_gate_proof(cwd)
            result = run_agentflow(cwd, "verify-proof", "--strict")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("review_gate", result.stdout)

    def test_verify_proof_strict_promotes_persisted_warning_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.2.0"
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
            self.assertEqual(run_agentflow(cwd, "build-proof").returncode, 0)

            result = run_agentflow(cwd, "verify-proof", "--strict")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing_plan_evidence_ids", result.stdout)

    def test_status_reports_runtime_snapshot_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            config = {
                "schema_version": "0.2.0",
                "runtimes": {
                    "local": {
                        "adapter": "custom",
                        "enabled": True,
                        "readiness": {"check": "none"},
                    }
                },
                "routes": {"reviewer": {"primary": "local"}},
            }
            (cwd / ".agent/runtime.config.json").write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(run_agentflow(cwd, "runtime-status", "--record").returncode, 0)

            result = run_agentflow(cwd, "status")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("runtime config present", result.stdout)
            self.assertIn("runtime snapshots 1", result.stdout)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_v03_end_to_end_execution_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["allowed_files"] = ["fixture.txt", ".agent/"]
            plan["validation_gates"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["files"] = ["fixture.txt"]
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )

            self.assertEqual(run_agentflow(cwd, "doctor").returncode, 0)
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(
                    cwd,
                    "record-file-change",
                    "--step",
                    "P1",
                    "--path",
                    "fixture.txt",
                ).returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(
                    cwd,
                    "run",
                    "--step",
                    "P1",
                    "--gate",
                    "python3 -c \"print('ok')\"",
                    "--",
                    "python3",
                    "-c",
                    "print('ok')",
                ).returncode,
                0,
            )
            self.assertEqual(run_agentflow(cwd, "verify-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "complete-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "verify-run").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "build-proof").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "verify-proof").returncode, 0)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_amend_step_cli_opens_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["allowed_files"] = ["fixture.txt", ".agent/"]
            plan["validation_gates"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["files"] = ["fixture.txt"]
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            gate = "python3 -c \"print('ok')\""

            def do_gate() -> None:
                self.assertEqual(
                    run_agentflow(
                        cwd, "run", "--step", "P1", "--gate", gate,
                        "--", "python3", "-c", "print('ok')",
                    ).returncode,
                    0,
                )

            self.assertEqual(run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0)
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(cwd, "record-file-change", "--step", "P1", "--path", "fixture.txt").returncode, 0
            )
            do_gate()
            self.assertEqual(run_agentflow(cwd, "verify-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "complete-step", "P1").returncode, 0)

            rejected = run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a")
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("amend-step", rejected.stderr)

            amended = run_agentflow(
                cwd, "amend-step", "P1", "--agent", "agent-a",
                "--reason", "address review", "--reason-code", "review_feedback",
            )
            self.assertEqual(amended.returncode, 0, amended.stdout + amended.stderr)
            (cwd / "fixture.txt").write_text("amended\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(cwd, "record-file-change", "--step", "P1", "--path", "fixture.txt").returncode, 0
            )
            do_gate()
            self.assertEqual(run_agentflow(cwd, "verify-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "complete-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "verify-run").returncode, 0)

    def test_amend_step_cli_rejects_malformed_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["allowed_files"] = ["fixture.txt", ".agent/"]
            plan["validation_gates"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["files"] = ["fixture.txt"]
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

            gate = "python3 -c \"print('ok')\""
            self.assertEqual(run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0)
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(cwd, "record-file-change", "--step", "P1", "--path", "fixture.txt").returncode, 0
            )
            self.assertEqual(
                run_agentflow(
                    cwd, "run", "--step", "P1", "--gate", gate,
                    "--", "python3", "-c", "print('ok')",
                ).returncode,
                0,
            )
            self.assertEqual(run_agentflow(cwd, "verify-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "complete-step", "P1").returncode, 0)

            rejected = run_agentflow(
                cwd, "amend-step", "P1", "--agent", "agent-a",
                "--reason", "address review", "--finding", "RR-bad",
            )
            self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)

    def test_status_reports_execution_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)

            result = run_agentflow(cwd, "status")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("execution contract present", result.stdout)
            self.assertIn("step runs 0", result.stdout)
            self.assertIn("command receipts 0", result.stdout)
            self.assertIn("file receipts 0", result.stdout)

    def test_status_reports_incompatible_plan_schema_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan_path = cwd / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["schema_version"] = "9.0.0"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            result = run_agentflow(cwd, "status")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("status invalid:", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_status_degrades_on_incompatible_ledger_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            (cwd / ".agent/step-runs.jsonl").write_text(
                '{"schema_version": "9.0.0", "event": "claimed"}\n', encoding="utf-8"
            )

            result = run_agentflow(cwd, "status")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("step runs unreadable:", result.stdout)
            self.assertIn("file receipts 0", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_validate_plan_reports_incompatible_schema_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan_path = cwd / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["schema_version"] = "9.0.0"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            result = run_agentflow(cwd, "validate-plan", ".agent/plan.lock.json")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("invalid plan:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_doctor_reports_incompatible_contract_schema_as_finding(self) -> None:
        # doctor exists to diagnose exactly this state: the version-gate
        # rejection degrades to a finding instead of escaping as a traceback.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            contract_path = cwd / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["schema_version"] = "0.2.0"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")

            result = run_agentflow(cwd, "doctor")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("doctor failed", result.stdout)
            self.assertIn("incompatible", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_next_action_incompatible_plan_is_structured_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan_path = cwd / ".agent/plan.lock.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["schema_version"] = "9.0.0"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            result = run_agentflow(cwd, "next-action", "--json")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["state"], "state_invalid")
            self.assertEqual(
                payload["resumability"]["diagnostics"][0]["code"],
                "plan_invalid",
            )
            self.assertFalse(any(
                action["allowed"]
                for action in payload["resumability"]["recovery_actions"]
            ))
            self.assertNotIn("Traceback", result.stderr)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_audit_drift_fails_out_of_scope_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(valid_plan(), indent=2),
                encoding="utf-8",
            )
            (cwd / "unexpected.txt").write_text("drift\n", encoding="utf-8")

            result = run_agentflow(cwd, "audit-drift")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected.txt", result.stdout)

    def test_init_execution_creates_execution_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)

            result = run_agentflow(cwd, "init-execution")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((cwd / ".agent/execution.contract.json").exists())
            self.assertTrue((cwd / ".agent/step-runs.jsonl").exists())
            self.assertIn("created .agent/execution.contract.json", result.stdout)

    def test_doctor_json_reports_ready_after_init_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)

            result = run_agentflow(cwd, "doctor", "--json")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["contract"]["schema_version"], "0.3.0")

    def test_next_step_and_claim_step_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )

            next_result = run_agentflow(cwd, "next-step", "--json")
            claim_result = run_agentflow(
                cwd,
                "claim-step",
                "P1",
                "--agent",
                "agent-a",
                "--json",
            )

            self.assertEqual(next_result.returncode, 0, next_result.stdout + next_result.stderr)
            self.assertEqual(json.loads(next_result.stdout)["id"], "P1")
            self.assertEqual(claim_result.returncode, 0, claim_result.stdout + claim_result.stderr)
            self.assertEqual(json.loads(claim_result.stdout)["attempt_id"], "A1")

    def test_complete_step_requires_existing_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )

            result = run_agentflow(cwd, "complete-step", "P1")

            self.assertEqual(result.returncode, 2)
            self.assertIn("claim-step first", result.stderr)

    def test_run_records_command_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )

            result = run_agentflow(
                cwd,
                "run",
                "--step",
                "P1",
                "--",
                "python3",
                "-c",
                "print('ok')",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = (cwd / ".agent/command-receipts.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(entries), 1)
            self.assertEqual(json.loads(entries[0])["exit_code"], 0)

    def test_run_timeout_json_prints_receipt_and_exits_124(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["validation_gates"] = ["python3 -c \"print('ok')\""]
            plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
            plan["locked"] = True
            plan["locked_at"] = "2026-06-23T00:00:00+00:00"
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )
            contract_path = cwd / ".agent/execution.contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["command_policy"]["risk_policy"] = "warn"
            contract["command_policy"]["command_timeout_seconds"] = 1
            contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )

            result = run_agentflow(
                cwd,
                "run",
                "--step",
                "P1",
                "--gate",
                "python3 -c \"print('ok')\"",
                "--json",
                "--",
                "python3",
                "-c",
                "import time; time.sleep(2)",
            )

            self.assertEqual(result.returncode, 124)
            self.assertIn("timed out after 1 seconds", result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["decision"], "timeout")
            self.assertEqual(receipt["timed_out"], True)
            self.assertIsNone(receipt["exit_code"])
            self.assertEqual(receipt["timeout_seconds"], 1)
            self.assertEqual(receipt["gate"], "python3 -c \"print('ok')\"")
            ledger_receipts = [
                json.loads(line)
                for line in (cwd / ".agent/command-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(ledger_receipts, [receipt])

    def _risk_run_fixture(self, cwd: Path) -> None:
        self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
        self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
        plan = valid_plan()
        plan["schema_version"] = "0.3.0"
        plan["allowed_files"] = ["fixture.txt", ".agent/"]
        plan["blocked_files"] = ["blocked.txt"]
        plan["validation_gates"] = ["python3 -c \"print('ok')\""]
        plan["steps"][0]["files"] = ["fixture.txt"]
        plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
        plan["locked"] = True
        (cwd / ".agent/plan.lock.json").write_text(
            json.dumps(plan, indent=2),
            encoding="utf-8",
        )
        self.assertEqual(
            run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
            0,
        )

    def _set_cli_risk_policy(self, cwd: Path, policy: str) -> None:
        path = cwd / ".agent/execution.contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["command_policy"]["risk_policy"] = policy
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

    def test_run_blocks_high_risk_under_block_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._risk_run_fixture(cwd)
            self._set_cli_risk_policy(cwd, "block")

            result = run_agentflow(
                cwd, "run", "--step", "P1", "--", "rm", "-rf", "fixture.txt"
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("blocked", result.stderr)
            self.assertIn("risk_policy=block", result.stderr)
            self.assertNotIn("--confirm-risk", result.stderr)
            receipt = json.loads(
                (cwd / ".agent/command-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(receipt["decision"], "blocked")
            self.assertIsNone(receipt["exit_code"])

    def test_run_require_confirmation_block_message_mentions_confirm_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._risk_run_fixture(cwd)

            result = run_agentflow(
                cwd, "run", "--step", "P1", "--", "rm", "-rf", "fixture.txt"
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("blocked", result.stderr)
            self.assertIn("--confirm-risk", result.stderr)

    def test_run_confirm_risk_overrides_require_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._risk_run_fixture(cwd)

            result = run_agentflow(
                cwd,
                "run",
                "--step",
                "P1",
                "--confirm-risk",
                "--",
                "sh",
                "-c",
                "rm -rf nonexistent-agentflow-test-xyz",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(
                (cwd / ".agent/command-receipts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(receipt["decision"], "allowed")
            self.assertTrue(receipt["confirmed"])
            self.assertEqual(receipt["confirmation_source"], "cli")

    def test_run_json_includes_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._risk_run_fixture(cwd)

            result = run_agentflow(
                cwd,
                "run",
                "--step",
                "P1",
                "--json",
                "--",
                "python3",
                "-c",
                "print('ok')",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["decision"], "allowed")
            self.assertEqual(receipt["risk"]["level"], "low")

    def test_record_file_change_cli_rejects_out_of_scope_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )
            (cwd / "outside.txt").write_text("outside\n", encoding="utf-8")

            result = run_agentflow(cwd, "record-file-change", "--step", "P1", "--path", "outside.txt")

            self.assertEqual(result.returncode, 1)
            self.assertIn("outside effective file scope", result.stderr)

    def test_verify_step_json_reports_missing_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )

            result = run_agentflow(cwd, "verify-step", "P1", "--json")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "failed")
            self.assertTrue(payload["errors"])

    def test_complete_step_fails_before_successful_verify_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )

            result = run_agentflow(cwd, "complete-step", "P1")

            self.assertEqual(result.returncode, 1)
            self.assertIn("verify-step must pass before complete-step", result.stderr)

    def test_export_handoff_and_lint_handoff_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )

            export_result = run_agentflow(
                cwd,
                "export-handoff",
                "--step",
                "P1",
                "--format",
                "markdown",
            )

            self.assertEqual(export_result.returncode, 0, export_result.stdout + export_result.stderr)
            handoff_path = cwd / ".agent/handoffs/P1.md"
            self.assertTrue(handoff_path.exists())

            lint_result = run_agentflow(
                cwd,
                "lint-handoff",
                "--input",
                ".agent/handoffs/P1.md",
                "--json",
            )
            self.assertEqual(lint_result.returncode, 0, lint_result.stdout + lint_result.stderr)
            self.assertEqual(json.loads(lint_result.stdout)["findings"], [])

    def test_replay_gates_record_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            plan = valid_plan()
            plan["schema_version"] = "0.3.0"
            plan["locked"] = True
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(plan, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(
                    cwd,
                    "record-command",
                    "--step",
                    "P1",
                    "--exit-code",
                    "0",
                    "--gate",
                    "manual inspection",
                    "--",
                    "python3",
                    "-c",
                    "print('ok')",
                ).returncode,
                0,
            )

            result = run_agentflow(cwd, "replay-gates", "--step", "P1", "--record", "--json")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "passed")


class PorcelainCommandTests(unittest.TestCase):
    def _driven_plan(self) -> dict:
        plan = valid_plan()
        plan["schema_version"] = "0.3.0"
        plan["allowed_files"] = ["fixture.txt", ".agent/"]
        plan["validation_gates"] = ["python3 -c \"print('ok')\""]
        plan["steps"][0]["files"] = ["fixture.txt"]
        plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
        plan["steps"][0]["gates"] = [
            {"kind": "command", "run": ["python3", "-c", "print('ok')"]}
        ]
        plan["locked"] = True
        return plan

    def test_next_action_json_uninitialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(
                cwd,
                "next-action",
                "--json",
                "--agent",
                "worker-explicit",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["state"], "uninitialized")
            self.assertTrue(payload["blocking"])
            self.assertEqual(
                payload["resumability"]["agent_id"],
                "worker-explicit",
            )

    def test_next_action_agent_defaults_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(
                cwd,
                "next-action",
                "--json",
                env_overrides={"AGENTFLOW_AGENT_ID": "worker-env"},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["resumability"]["agent_id"], "worker-env")

    def test_finish_run_reports_blocked_on_uninitialized_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(cwd, "finish-run")
            self.assertEqual(result.returncode, 1)
            self.assertIn("audit-drift", result.stdout + result.stderr)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_finish_run_json_is_parseable_when_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(self._driven_plan(), indent=2), encoding="utf-8"
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(
                    cwd, "record-file-change", "--step", "P1", "--path", "fixture.txt"
                ).returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(
                    cwd, "run", "--step", "P1", "--gate", "python3 -c \"print('ok')\"",
                    "--", "python3", "-c", "print('ok')",
                ).returncode,
                0,
            )
            self.assertEqual(run_agentflow(cwd, "verify-step", "P1").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "complete-step", "P1").returncode, 0)

            # Out-of-scope file makes the first gate (audit-drift) fail.
            (cwd / "unexpected.txt").write_text("drift\n", encoding="utf-8")

            result = run_agentflow(cwd, "finish-run", "--json")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            # stdout must be clean JSON, not gate chatter + JSON.
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["stopped_at"], "audit-drift")

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_finish_step_verifies_and_completes_driven_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(self._driven_plan(), indent=2), encoding="utf-8"
            )
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(
                    cwd, "record-file-change", "--step", "P1", "--path", "fixture.txt"
                ).returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(
                    cwd, "run", "--step", "P1", "--gate", "python3 -c \"print('ok')\"",
                    "--", "python3", "-c", "print('ok')",
                ).returncode,
                0,
            )

            result = run_agentflow(cwd, "finish-step", "P1", "--json")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["step_id"], "P1")
            self.assertEqual(payload["verification_status"], "passed")
            self.assertTrue(payload["verified"])
            self.assertTrue(payload["completed"])

    def test_finish_step_unknown_step_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
            (cwd / ".agent/plan.lock.json").write_text(
                json.dumps(self._driven_plan(), indent=2), encoding="utf-8"
            )
            result = run_agentflow(cwd, "finish-step", "P-nope")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


class EventsCommandTests(unittest.TestCase):
    def _init(self, cwd: Path) -> None:
        self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
        self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
        (cwd / ".agent" / "step-runs.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "0.5.0",
                    "event": "claimed",
                    "step_id": "P1",
                    "attempt_id": "A1",
                    "recorded_at": "2026-06-18T10:00:00+00:00",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_events_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._init(cwd)
            result = run_agentflow(cwd, "events", "--jsonl")
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["type"], "step.claimed")

    def test_events_json_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._init(cwd)
            result = run_agentflow(cwd, "events", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsInstance(payload, list)
            self.assertEqual(payload[0]["type"], "step.claimed")

    def test_events_human_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._init(cwd)
            result = run_agentflow(cwd, "events")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("step.claimed", result.stdout)

    def test_events_invalid_since_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._init(cwd)
            result = run_agentflow(cwd, "events", "--since", "not-a-date")
            self.assertEqual(result.returncode, 2)

    def test_events_jsonl_and_json_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._init(cwd)
            result = run_agentflow(cwd, "events", "--jsonl", "--json")
            self.assertEqual(result.returncode, 2)


import json as _json
import tempfile as _tempfile
from pathlib import Path as _Path

from agentflow.artifacts import create_initial_artifacts, read_jsonl
from agentflow.cli import main as cli_main


def _review_manifest(state_dir: str = "docs/ai/state/main") -> dict:
    return {
        "schema_version": "0.1.0",
        "review_run_id": "RR-20260620T180000Z-ab12cd34",
        "state_dir": state_dir,
        "policy": "full",
        "gate_status": "pass",
        "active_blocking": [],
        "findings": {"counts_by_severity": {}, "counts_by_status": {}, "index": []},
        "artifacts": [{"path": "findings-final.yaml"}, {"path": "gate.yaml"}],
    }


class RecordReviewCliTests(unittest.TestCase):
    def _state(self, root: _Path, manifest: dict | None = None) -> _Path:
        manifest = manifest if manifest is not None else _review_manifest()
        state = root / manifest["state_dir"]
        state.mkdir(parents=True)
        (state / "findings-final.yaml").write_text("findings: []\n", encoding="utf-8")
        (state / "gate.yaml").write_text("gate: pass\n", encoding="utf-8")
        manifest_path = state / "review-manifest.json"
        manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def test_record_review_appends_ledger(self) -> None:
        with _tempfile.TemporaryDirectory() as tmp:
            root = _Path(tmp)
            create_initial_artifacts(root)
            manifest_path = self._state(root)
            code = cli_main(
                ["record-review", "--root", str(root), "--manifest", str(manifest_path)]
            )
            self.assertEqual(code, 0)
            runs = read_jsonl(root / ".agent/review-runs.jsonl")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["review_run_id"], "RR-20260620T180000Z-ab12cd34")

    def test_record_review_rejects_missing_artifact(self) -> None:
        with _tempfile.TemporaryDirectory() as tmp:
            root = _Path(tmp)
            create_initial_artifacts(root)
            manifest_path = self._state(root)
            (root / "docs/ai/state/main/gate.yaml").unlink()
            code = cli_main(
                ["record-review", "--root", str(root), "--manifest", str(manifest_path)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(read_jsonl(root / ".agent/review-runs.jsonl"), [])

    def test_record_review_rejects_out_of_range_schema_version(self) -> None:
        with _tempfile.TemporaryDirectory() as tmp:
            root = _Path(tmp)
            create_initial_artifacts(root)
            manifest = _review_manifest()
            manifest["schema_version"] = "9.9.9"
            manifest_path = self._state(root, manifest)
            code = cli_main(
                ["record-review", "--root", str(root), "--manifest", str(manifest_path)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(read_jsonl(root / ".agent/review-runs.jsonl"), [])

    def test_emit_evidence_writes_exempt_entries(self) -> None:
        with _tempfile.TemporaryDirectory() as tmp:
            root = _Path(tmp)
            create_initial_artifacts(root)
            manifest_path = self._state(root)
            code = cli_main(
                [
                    "record-review",
                    "--root",
                    str(root),
                    "--manifest",
                    str(manifest_path),
                    "--emit-evidence",
                ]
            )
            self.assertEqual(code, 0)
            evidence = read_jsonl(root / ".agent/evidence.jsonl")
            kinds = {e.get("kind") for e in evidence}
            self.assertEqual(kinds, {"review"})
            ids = {e["id"] for e in evidence}
            self.assertIn("E-review-RR-20260620T180000Z-ab12cd34", ids)


class ReviewManifestCommandTest(unittest.TestCase):
    def _setup(self, findings: list) -> Path:
        root = Path(tempfile.mkdtemp())
        normalized = []
        for source in findings:
            row = dict(source)
            if row.get("status") in ("open", "accepted"):
                row.setdefault("claim", "Broken behavior.")
                row.setdefault("suggested_fix", "Repair it.")
                row.setdefault("agentflow_refs", {"plan_step": "P1"})
            normalized.append(row)
        state = root / "docs/ai/state/feat-7"
        state.mkdir(parents=True)
        (state / "findings-final.json").write_text(
            json.dumps({"findings": normalized}), encoding="utf-8")
        (state / "findings-final.yaml").write_text("findings: []\n", encoding="utf-8")
        (state / "synthesis.md").write_text("# Synthesis\n", encoding="utf-8")
        (state / "gate.yaml").write_text("status: pass\n", encoding="utf-8")
        # repo-owned policy config
        (root / "docs/ai").mkdir(parents=True, exist_ok=True)
        config = {
            "branch_modifiers": {"feat/*": {"gate": "full"}, "*": {"gate": "full"}},
            "gate_policy": {
                "full": {"blocks_on": ["critical", "high"], "warns_on": ["medium"]},
            },
        }
        (root / "docs/ai/config.json").write_text(json.dumps(config), encoding="utf-8")
        (root / ".agent").mkdir()
        plan = valid_plan()
        plan["locked"] = True
        (root / ".agent/plan.lock.json").write_text(json.dumps(plan), encoding="utf-8")
        return root

    def test_write_emits_valid_manifest_file(self) -> None:
        root = self._setup([{"id": "A", "severity": "high", "status": "accepted"}])
        result = run_agentflow(
            root, "review-manifest",
            "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7",
            "--branch", "feat/7-evidence",
            "--write",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest_path = root / "docs/ai/state/feat-7/review-manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["gate_status"], "fail")
        self.assertEqual(manifest["active_blocking"], ["A"])
        self.assertEqual(manifest["state_dir"], "docs/ai/state/feat-7")

    def test_strict_exit_nonzero_on_warn_without_mutating_manifest(self) -> None:
        root = self._setup([{"id": "A", "severity": "medium", "status": "open"}])
        result = run_agentflow(
            root, "review-manifest",
            "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7",
            "--branch", "feat/7-evidence",
            "--write", "--strict-exit", "--json",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        printed = json.loads(result.stdout)
        self.assertEqual(printed["gate_status"], "warn")  # not rewritten to fail

    def test_fail_on_block_nonzero_when_blocking(self) -> None:
        root = self._setup([{"id": "A", "severity": "critical", "status": "open"}])
        result = run_agentflow(
            root, "review-manifest",
            "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7",
            "--branch", "feat/7-evidence",
            "--fail-on-block",
        )
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_malformed_findings_exits_nonzero(self) -> None:
        root = self._setup([{"id": "A", "severity": "nope", "status": "open"}])
        result = run_agentflow(
            root, "review-manifest",
            "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7",
            "--branch", "feat/7-evidence",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("severity", result.stderr.lower())

    def test_missing_required_artifact_exits_nonzero(self) -> None:
        root = self._setup([{"id": "A", "severity": "medium", "status": "open"}])
        (root / "docs/ai/state/feat-7/synthesis.md").unlink()
        result = run_agentflow(
            root, "review-manifest",
            "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7",
            "--branch", "feat/7-evidence",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("required review artifact missing", result.stderr)

    def test_runner_output_is_accepted_by_record_review(self) -> None:
        root = self._setup([{"id": "A", "severity": "medium", "status": "open"}])
        # init .agent scaffold so record-review can append the ledger
        init = run_agentflow(root, "init", "--root", str(root))
        self.assertEqual(init.returncode, 0, init.stderr)
        produce = run_agentflow(
            root, "review-manifest",
            "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7",
            "--branch", "feat/7-evidence",
            "--write",
        )
        self.assertEqual(produce.returncode, 0, produce.stderr)
        manifest_rel = "docs/ai/state/feat-7/review-manifest.json"
        record = run_agentflow(
            root, "record-review",
            "--root", str(root),
            "--manifest", manifest_rel,
        )
        self.assertEqual(record.returncode, 0, record.stderr)
        self.assertIn("recorded review run RR-", record.stdout)

    def test_json_exposes_amendment_ready_projection(self) -> None:
        finding = {
            "id": "A",
            "severity": "high",
            "status": "accepted",
            "claim": "The verifier omits a hash.",
            "suggested_fix": "Hash the missing artifact.",
            "file": "src/agentflow/proof.py",
            "line": 10,
            "agentflow_refs": {"plan_step": "P1"},
        }
        root = self._setup([finding])
        result = run_agentflow(
            root, "review-manifest", "--root", str(root),
            "--state-dir", "docs/ai/state/feat-7", "--branch", "feat/7-evidence",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["amendment_ready"])
        self.assertEqual(payload["findings"]["index"][0]["owning_step"], "P1")


class AuditDriftHunkCliTests(unittest.TestCase):
    def _locked_plan(self) -> dict:
        return {"schema_version": "0.3.0", "objective": "x", "scope": [], "non_goals": [],
                "invariants": [], "allowed_files": ["fixture.txt"], "blocked_files": [],
                "validation_gates": [], "rollback_plan": "", "risk_level": "low",
                "drift_budget": {"unrelated_edits": 0, "new_dependencies": 0,
                                 "formatting_drift": "minimal", "architecture_drift": "requires_approval",
                                 "test_weakening": 0},
                "steps": [{"id": "P1", "action": "edit", "files": ["fixture.txt"],
                           "preconditions": [], "expected_diff": [], "validation": [], "evidence_ids": []}],
                "evidence_ids": [], "locked": True, "locked_at": "2026-06-01T00:00:00+00:00"}

    def test_audit_drift_prints_unmapped_hunks(self) -> None:
        from agentflow.artifacts import create_initial_artifacts, write_json
        from agentflow.cli import main as cli_main
        from agentflow.execution import claim_step, init_execution_artifacts
        from agentflow.receipts import record_file_change
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(root), check=True, stdout=subprocess.PIPE)
            create_initial_artifacts(root)
            init_execution_artifacts(root)
            plan = self._locked_plan()
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
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(["audit-drift", "--root", str(root), "--plan", ".agent/plan.lock.json"])
            self.assertEqual(code, 1)
            self.assertIn("unmapped hunks:", out.getvalue())


def _pack_manifest():
    return {
        "schema_version": "0.1.0",
        "id": "python-library-proof-gate",
        "name": "Python Library Proof Gate",
        "description": "Stdlib-only Python library workflow.",
        "plan_templates": {
            "python-library": {
                "schema_version": "0.3.0",
                "objective": "TODO: describe the objective",
                "scope": ["src/"],
                "non_goals": [],
                "invariants": ["stdlib only"],
                "allowed_files": ["src/", ".agent/"],
                "blocked_files": [],
                "validation_gates": ["unit-tests"],
                "rollback_plan": "git restore .",
                "risk_level": "low",
                "drift_budget": {
                    "unrelated_edits": 0,
                    "new_dependencies": 0,
                    "formatting_drift": "minimal",
                    "architecture_drift": "requires_approval",
                },
                "steps": [
                    {
                        "id": "P1",
                        "action": "do the thing",
                        "files": ["src/"],
                        "preconditions": [],
                        "expected_diff": [],
                        "validation": ["unit-tests"],
                        "evidence_ids": [],
                    }
                ],
                "evidence_ids": [],
                "locked": False,
                "locked_at": None,
            }
        },
        "profiles": [
            {
                "id": "default",
                "review_depth": "standard",
                "required_capabilities": [{"id": "python", "required": True}],
                "validation_policy": {"required_gates": ["unit-tests"]},
                "proof_policy": {"hunk_attribution": "enforce", "require_review_run": False},
                "plan_template": "python-library",
            }
        ],
        "hook_templates": [{"id": "pre-commit", "path": "hooks/pre-commit.sh"}],
    }


class PackInspectCliTests(unittest.TestCase):
    def _write_pack(self, root, manifest):
        import json
        from pathlib import Path

        pack_dir = Path(root) / ".agentflow-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _manifest(self):
        return _pack_manifest()

    def test_inspect_text_summary(self):
        import contextlib
        import io
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._write_pack(tmp, self._manifest())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(["pack", "inspect", tmp])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("python-library-proof-gate", text)
            self.assertIn("default", text)
            self.assertIn("hooks/pre-commit.sh", text)

    def test_inspect_json_includes_hash(self):
        import contextlib
        import hashlib
        import io
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            self._write_pack(tmp, self._manifest())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(["pack", "inspect", tmp, "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            raw = (Path(tmp) / ".agentflow-pack" / "pack.json").read_bytes()
            self.assertEqual(payload["manifest_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(payload["id"], "python-library-proof-gate")
            self.assertEqual(payload["profiles"][0]["id"], "default")

    def test_inspect_invalid_pack_returns_nonzero(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            pack_dir = Path(tmp) / ".agentflow-pack"
            pack_dir.mkdir(parents=True)
            (pack_dir / "pack.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(["pack", "inspect", tmp])
            self.assertEqual(code, 1)
            self.assertIn("error:", err.getvalue())

    def test_inspect_invalid_pack_json_returns_error_envelope(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            pack_dir = Path(tmp) / ".agentflow-pack"
            pack_dir.mkdir(parents=True)
            (pack_dir / "pack.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(["pack", "inspect", tmp, "--json"])
            self.assertEqual(code, 1)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "invalid")
            self.assertEqual(payload["errors"][0]["code"], "validation_error")
            self.assertIn("missing required pack field", payload["errors"][0]["message"])


class InitPackCliTests(unittest.TestCase):
    def _write_pack(self, root):
        import json
        from pathlib import Path

        manifest = _pack_manifest()
        pack_dir = Path(root) / "pack" / ".agentflow-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
        return str(Path(root) / "pack")

    def test_init_pack_seeds_plan_and_contract(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path

        from agentflow.validation import validate_plan
        from agentflow.workflow_contract import validate_workflow_contract

        with tempfile.TemporaryDirectory() as tmp:
            pack_path = self._write_pack(tmp)
            project = Path(tmp) / "project"
            project.mkdir()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(
                    [
                        "init",
                        "--root",
                        str(project),
                        "--pack",
                        pack_path,
                        "--profile",
                        "default",
                        "--reason",
                        "dogfooding the pack",
                    ]
                )
            self.assertEqual(code, 0)

            plan = json.loads((project / ".agent" / "plan.lock.json").read_text())
            self.assertEqual(validate_plan(plan), [])
            self.assertIs(plan["locked"], False)
            self.assertIsNone(plan["locked_at"])
            self.assertIn("stdlib only", plan["invariants"])

            contract = json.loads(
                (project / ".agent" / "workflow.contract.json").read_text()
            )
            self.assertEqual(validate_workflow_contract(contract), [])
            self.assertEqual(contract["workflow_pack"], "python-library-proof-gate")
            self.assertEqual(contract["selected_by"], "init --pack")
            self.assertEqual(contract["selection_reason"], "dogfooding the pack")

    def test_profile_without_pack_errors(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(["init", "--root", str(project), "--profile", "default"])
            self.assertEqual(code, 2)

    def test_unknown_profile_errors(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            pack_path = self._write_pack(tmp)
            project = Path(tmp) / "project"
            project.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(
                    ["init", "--root", str(project), "--pack", pack_path, "--profile", "ghost"]
                )
            self.assertEqual(code, 1)

    def test_refuses_overwrite_without_force(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path

        from agentflow.artifacts import create_initial_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            pack_path = self._write_pack(tmp)
            project = Path(tmp) / "project"
            project.mkdir()
            create_initial_artifacts(project)  # pre-existing .agent/plan.lock.json

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(
                    ["init", "--root", str(project), "--pack", pack_path, "--profile", "default"]
                )
            self.assertEqual(code, 1)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_main(
                    [
                        "init",
                        "--root",
                        str(project),
                        "--pack",
                        pack_path,
                        "--profile",
                        "default",
                        "--force",
                    ]
                )
            self.assertEqual(code, 0)


class RecommendWorkflowCliTests(unittest.TestCase):
    def _write_brief(self, cwd: Path, **overrides) -> Path:
        brief = {"schema_version": "0.1.0", "task_type": "bugfix", "declared_risk": "low"}
        brief.update(overrides)
        path = cwd / "brief.json"
        path.write_text(json.dumps(brief), encoding="utf-8")
        return path

    def test_recommend_workflow_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd, candidate_files=["a.py"], declared_size="s",
                                      blast_radius="local")
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["recommended"]["profile"], "small-bugfix")
            self.assertEqual(
                report["workflow_contract_candidate"]["workflow_profile"], "small-bugfix"
            )

    def test_recommend_workflow_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd)
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recommended agentflow-default/medium-feature", result.stdout)

    def test_recommend_workflow_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = json.dumps({
                "schema_version": "0.1.0", "task_type": "docs", "declared_risk": "low",
                "candidate_files": ["docs/x.md"],
            })
            result = run_agentflow(cwd, "recommend-workflow", "--stdin", "--json",
                                   input_text=brief)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["recommended"]["profile"], "docs-only")

    def test_recommend_workflow_brief_and_stdin_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_agentflow(Path(tmp), "recommend-workflow", "--brief", "x.json", "--stdin")
            self.assertEqual(result.returncode, 2)

    def test_recommend_workflow_requires_an_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_agentflow(Path(tmp), "recommend-workflow")
            self.assertEqual(result.returncode, 2)

    def test_recommend_workflow_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(cwd, "recommend-workflow", "--brief",
                                   str(cwd / "nope.json"), "--json")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["status"], "invalid")

    def test_recommend_workflow_invalid_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd, task_type="chore")
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief), "--json")
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "invalid")
            self.assertTrue(any(e["code"] == "validation_error" for e in payload["errors"]))

    def test_recommend_workflow_override_requires_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd)
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief),
                                   "--selected-profile", "high-risk", "--json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                json.loads(result.stdout)["errors"][0]["code"], "override_requires_reason"
            )

    def test_recommend_workflow_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd)
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief),
                                   "--selected-profile", "nope", "--json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "unknown_profile")

    def test_recommend_workflow_non_dict_body_is_validation_error(self) -> None:
        # Valid JSON but not an object: must surface as validation_error (exit 1),
        # not be mislabeled a JSON-parse failure.
        with tempfile.TemporaryDirectory() as tmp:
            result = run_agentflow(Path(tmp), "recommend-workflow", "--stdin", "--json",
                                   input_text="[1, 2, 3]")
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "invalid")
            self.assertTrue(any(e["code"] == "validation_error" for e in payload["errors"]))

    def test_recommend_workflow_is_read_only(self) -> None:
        # The headline contract: a successful run writes nothing (no .agent/).
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd)
            before = {p.name for p in cwd.iterdir()}
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            after = {p.name for p in cwd.iterdir()}
            self.assertEqual(before, after)
            self.assertFalse((cwd / ".agent").exists())

    def test_recommend_workflow_text_failure_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(cwd / "nope.json"))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("invalid brief", result.stderr)

    def test_recommend_workflow_selected_matches_recommendation(self) -> None:
        # --selected-profile equal to the recommendation is not an override and
        # needs no --reason.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = self._write_brief(cwd)
            result = run_agentflow(cwd, "recommend-workflow", "--brief", str(brief),
                                   "--selected-profile", "medium-feature", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIsNone(report["override"])
            self.assertEqual(report["selected"]["profile"], "medium-feature")


EXAMPLES = ROOT / "examples"
DEMO_PACK = EXAMPLES / "packs" / "agentflow-draft-demo"
BRIEFS = EXAMPLES / "briefs"


def _draft_template(steps, gates):
    return {
        "schema_version": "0.3.0",
        "objective": "TODO",
        "scope": ["src/"],
        "non_goals": [],
        "invariants": ["Standard library only"],
        "allowed_files": ["src/", "tests/"],
        "blocked_files": [],
        "validation_gates": list(gates),
        "rollback_plan": "git restore .",
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
                "action": f"TODO {i + 1}",
                "files": ["src/"],
                "preconditions": [],
                "expected_diff": [],
                "validation": list(gates) or ["docs-build"],
                "evidence_ids": [],
                **({"depends_on": [f"P{i}"]} if i else {}),
            }
            for i in range(steps)
        ],
        "evidence_ids": [],
        "locked": False,
        "locked_at": None,
    }


def _write_pack(root: Path, manifest: dict) -> Path:
    pack_dir = root / ".agentflow-pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


class DraftPlanCliTests(unittest.TestCase):
    """End-to-end coverage for `agentflow draft-plan` (issue #71)."""

    def _draft(self, cwd, *extra, input_text=None):
        return run_agentflow(cwd, "draft-plan", *extra, input_text=input_text)

    def test_compiles_each_category_in_report_mode(self):
        # One brief per recommend archetype compiles to a valid, unlocked draft and
        # selects the matching demo-pack profile. --root points at the repo so the
        # docs/bugfix candidate files resolve; report mode never writes.
        for name in ["docs-only", "small-bugfix", "medium-feature", "large-feature", "high-risk"]:
            with self.subTest(category=name), tempfile.TemporaryDirectory() as tmp:
                result = self._draft(
                    Path(tmp),
                    "--brief", str(BRIEFS / f"{name}.brief.json"),
                    "--workflow", str(DEMO_PACK),
                    "--objective", f"Compile the {name} brief",
                    "--root", str(ROOT),
                    "--json",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "draft")
                self.assertEqual(payload["recommended"]["profile"], name)
                self.assertEqual(payload["selected"]["profile"], name)
                plan = payload["plan_candidate"]
                self.assertEqual(validate_plan(plan), [])
                self.assertIs(plan["locked"], False)

    def test_one_step_vs_multi_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "small-bugfix.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "fix", "--root", str(ROOT), "--json",
            )
            large = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "large-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "feature", "--root", str(ROOT), "--json",
            )
        self.assertEqual(len(json.loads(small.stdout)["plan_candidate"]["steps"]), 1)
        self.assertGreaterEqual(len(json.loads(large.stdout)["plan_candidate"]["steps"]), 2)

    def test_write_materializes_unlocked_plan_and_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self._draft(
                cwd, "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "Add an events projection", "--write",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads((cwd / ".agent/plan.lock.json").read_text())
            self.assertIs(plan["locked"], False)
            self.assertEqual(validate_plan(plan), [])
            self.assertEqual(plan["objective"], "Add an events projection")
            self.assertEqual(plan["workflow"]["workflow_pack"], "agentflow-draft-demo")
            self.assertEqual(plan["workflow"]["workflow_profile"], "medium-feature")
            self.assertEqual(plan["workflow"]["contract_path"], ".agent/workflow.contract.json")
            self.assertIn(".agent/", plan["allowed_files"])
            contract = json.loads((cwd / ".agent/workflow.contract.json").read_text())
            self.assertEqual(contract["workflow_profile"], "medium-feature")
            self.assertIn("least-strict", contract["selection_reason"])

    def test_contract_carries_brief_validation_needs(self):
        brief = json.dumps({
            "schema_version": "0.1.0",
            "task_type": "feature",
            "declared_risk": "medium",
            "candidate_files": ["src/agentflow/events.py"],
            "blast_radius": "local",
            "validation_needs": ["unit-tests", "lint"],
            "declared_size": "m",
        })
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            report = self._draft(
                cwd, "--stdin", "--workflow", str(DEMO_PACK),
                "--objective", "from stdin", "--json", input_text=brief,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            gates = json.loads(report.stdout)["workflow_contract"]["validation_policy"]["required_gates"]
            self.assertIn("lint", gates)

            written = self._draft(
                cwd, "--stdin", "--workflow", str(DEMO_PACK),
                "--objective", "from stdin", "--write", input_text=brief,
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            contract = json.loads((cwd / ".agent/workflow.contract.json").read_text())
            self.assertIn("lint", contract["validation_policy"]["required_gates"])

    def test_write_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            args = (
                "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "Add a thing", "--write",
            )
            self.assertEqual(self._draft(cwd, *args).returncode, 0)
            second = self._draft(cwd, *args)
            self.assertEqual(second.returncode, 1)
            self.assertIn("force", (second.stdout + second.stderr).lower())
            self.assertEqual(self._draft(cwd, *args, "--force").returncode, 0)

    def test_non_object_existing_plan_uses_overwrite_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".agent").mkdir()
            (cwd / ".agent/plan.lock.json").write_text("[]", encoding="utf-8")
            result = self._draft(
                cwd, "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "Add a thing",
                "--write", "--json",
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "artifact_exists")

    def test_invalid_brief_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            brief = cwd / "bad.brief.json"
            brief.write_text(
                json.dumps({"schema_version": "0.1.0", "task_type": "nope", "declared_risk": "low"}),
                encoding="utf-8",
            )
            result = self._draft(
                cwd, "--brief", str(brief), "--workflow", str(DEMO_PACK),
                "--objective", "x", "--json",
            )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(payload["errors"][0]["code"], "invalid_brief")

    def test_missing_objective_is_too_vague(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--json",
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "brief_too_vague")

    def test_missing_candidate_file_fails_closed(self):
        # small-bugfix references repo files; an empty tmp root has none of them.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "small-bugfix.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "fix", "--json",
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "candidate_file_missing")

    def test_allow_missing_candidates_downgrades_to_warning(self):
        # #89: --allow-missing-candidates lets a greenfield brief compile and
        # reports the missing files as a warning rather than failing closed.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "small-bugfix.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "fix",
                "--allow-missing-candidates", "--json",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "draft")
        self.assertEqual(validate_plan(payload["plan_candidate"]), [])
        codes = [w["code"] for w in payload["warnings"]]
        self.assertIn("candidate_file_missing", codes)

    def test_greenfield_alias_downgrades_to_warning(self):
        # --greenfield is an accepted alias for --allow-missing-candidates.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "small-bugfix.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "fix",
                "--greenfield", "--json",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        codes = [w["code"] for w in json.loads(result.stdout)["warnings"]]
        self.assertIn("candidate_file_missing", codes)

    def test_review_run_profile_scopes_review_state_path(self):
        # #90: a profile that requires a review run scopes docs/ai/state/ into
        # allowed_files so recorded review artifacts are not flagged as drift.
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "high-risk.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "secure change",
                "--root", str(ROOT), "--json",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)["plan_candidate"]
        self.assertIn("docs/ai/state/", plan["allowed_files"])

    def test_standard_profile_omits_review_state_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "ordinary change",
                "--root", str(ROOT), "--json",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)["plan_candidate"]
        self.assertNotIn("docs/ai/state/", plan["allowed_files"])

    def test_decomposition_required_for_broad_thin_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _write_pack(cwd, {
                "schema_version": "0.1.0",
                "id": "thin",
                "name": "Thin",
                "description": "single-step pack",
                "plan_templates": {"one": _draft_template(1, ["unit-tests"])},
                "profiles": [{
                    "id": "deep1",
                    "review_depth": "deep",
                    "required_capabilities": [],
                    "validation_policy": {"required_gates": ["unit-tests"]},
                    "proof_policy": {"hunk_attribution": "enforce", "require_review_run": True},
                    "plan_template": "one",
                }],
            })
            result = self._draft(
                cwd, "--brief", str(BRIEFS / "large-feature.brief.json"),
                "--workflow", str(cwd), "--objective", "broad change",
                "--root", str(ROOT), "--json",
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "decomposition_required")

    def test_no_satisfying_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _write_pack(cwd, {
                "schema_version": "0.1.0",
                "id": "docsonly",
                "name": "Docs Only",
                "description": "no strong profile",
                "plan_templates": {"docs": _draft_template(1, ["docs-build"])},
                "profiles": [{
                    "id": "docs",
                    "review_depth": "none",
                    "required_capabilities": [],
                    "validation_policy": {"required_gates": []},
                    "proof_policy": {"hunk_attribution": "observe", "require_review_run": False},
                    "plan_template": "docs",
                }],
            })
            result = self._draft(
                cwd, "--brief", str(BRIEFS / "high-risk.brief.json"),
                "--workflow", str(cwd), "--objective", "secure change", "--json",
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "no_satisfying_profile")

    def test_explicit_weaker_profile_requires_reason(self):
        common = (
            "--brief", str(BRIEFS / "high-risk.brief.json"),
            "--workflow", str(DEMO_PACK), "--objective", "secure change",
            "--profile", "docs-only", "--json",
        )
        with tempfile.TemporaryDirectory() as tmp:
            without = self._draft(Path(tmp), *common)
            self.assertEqual(without.returncode, 1)
            self.assertEqual(
                json.loads(without.stdout)["errors"][0]["code"],
                "profile_weaker_than_recommended",
            )
        with tempfile.TemporaryDirectory() as tmp:
            withr = self._draft(Path(tmp), *common, "--reason", "operator accepts lighter posture")
            self.assertEqual(withr.returncode, 0, withr.stderr)
            self.assertEqual(json.loads(withr.stdout)["selected"]["profile"], "docs-only")

    def test_both_brief_and_stdin_is_arg_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--stdin", "--workflow", str(DEMO_PACK), "--objective", "x",
            )
        self.assertEqual(result.returncode, 2)

    def test_stdin_brief(self):
        brief = json.dumps({
            "schema_version": "0.1.0",
            "task_type": "feature",
            "declared_risk": "medium",
            "candidate_files": ["src/agentflow/events.py"],
            "blast_radius": "local",
            "validation_needs": ["unit-tests"],
            "declared_size": "m",
        })
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--stdin", "--workflow", str(DEMO_PACK),
                "--objective", "from stdin", "--json", input_text=brief,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "draft")

    def test_text_summary_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "Add a thing",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agentflow-draft-demo/medium-feature", result.stdout)

    def test_report_carries_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._draft(
                Path(tmp), "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "x", "--json",
            )
        self.assertEqual(json.loads(result.stdout)["schema_version"], "0.2.0")

    def test_drafted_plan_locks_cleanly(self):
        # The headline guarantee: a written draft locks through the normal
        # lock-plan path and the workflow extension block survives.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(self._draft(
                cwd, "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "Add a thing", "--write",
            ).returncode, 0)
            locked = run_agentflow(cwd, "lock-plan")
            self.assertEqual(locked.returncode, 0, locked.stderr)
            plan = json.loads((cwd / ".agent/plan.lock.json").read_text())
            self.assertIs(plan["locked"], True)
            self.assertEqual(plan["workflow"]["workflow_profile"], "medium-feature")

    def test_write_force_refuses_to_clobber_locked_plan(self):
        # --force overrides an unlocked draft, never a locked plan (Non-Goal:
        # no hidden mutation of locked plans).
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            args = (
                "--brief", str(BRIEFS / "medium-feature.brief.json"),
                "--workflow", str(DEMO_PACK), "--objective", "Add a thing", "--write",
            )
            self.assertEqual(self._draft(cwd, *args).returncode, 0)
            self.assertEqual(run_agentflow(cwd, "lock-plan").returncode, 0)
            clobber = self._draft(cwd, *args, "--force", "--json")
            self.assertEqual(clobber.returncode, 1)
            self.assertEqual(
                json.loads(clobber.stdout)["errors"][0]["code"], "locked_plan_exists"
            )
            plan = json.loads((cwd / ".agent/plan.lock.json").read_text())
            self.assertIs(plan["locked"], True)


class ReviewManifestHelpTests(unittest.TestCase):
    def test_findings_json_help_documents_state_dir_relative(self):
        import subprocess, sys, os
        env = {**os.environ, "PYTHONPATH": "src"}
        out = subprocess.run(
            [sys.executable, "-m", "agentflow", "review-manifest", "--help"],
            capture_output=True, text=True, env=env,
        ).stdout
        self.assertIn("relative to --state-dir", out)
        self.assertIn("findings-final.json", out)


class ReviewManifestDepthProfileCliTests(unittest.TestCase):
    def test_spec_quality_produces_lighter_manifest_end_to_end(self):
        import subprocess, sys, os, json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "docs/ai/state/main"
            state.mkdir(parents=True)
            (state / "findings-final.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
            (state / "gate.yaml").write_text("ok\n", encoding="utf-8")  # no yaml/synthesis
            (root / "docs/ai/config.json").write_text(json.dumps({
                "branch_modifiers": {"*": {"gate": "default"}},
                "gate_policy": {"default": {"blocks_on": ["high"], "warns_on": ["medium"]}},
            }), encoding="utf-8")
            (root / ".agent").mkdir()
            plan = valid_plan()
            plan["locked"] = True
            (root / ".agent/plan.lock.json").write_text(json.dumps(plan), encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": "src"}
            res = subprocess.run(
                [sys.executable, "-m", "agentflow", "review-manifest",
                 "--root", str(root), "--state-dir", "docs/ai/state/main",
                 "--branch", "main", "--depth-profile", "spec_quality",
                 "--write", "--json"],
                capture_output=True, text=True, env=env, cwd=os.getcwd(),
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            manifest = json.loads((state / "review-manifest.json").read_text())
            self.assertEqual(manifest["depth_profile"], "spec_quality")


@unittest.skipIf(shutil.which("git") is None, "git is not available")
class LeaseCliTests(unittest.TestCase):
    def _lease_cli_root(self, tmp: str) -> Path:
        cwd = Path(tmp)
        subprocess.run(["git", "init"], cwd=str(cwd), check=True, stdout=subprocess.PIPE)
        self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
        self.assertEqual(run_agentflow(cwd, "init-execution").returncode, 0)
        plan = valid_plan()
        plan["schema_version"] = "0.3.0"
        plan["allowed_files"] = ["fixture.txt", ".agent/"]
        plan["validation_gates"] = ["python3 -c \"print('ok')\""]
        plan["steps"][0]["validation"] = ["python3 -c \"print('ok')\""]
        plan["steps"][0]["files"] = ["fixture.txt"]
        plan["locked"] = True
        plan["locked_at"] = "2026-06-01T00:00:00+00:00"
        (cwd / ".agent/plan.lock.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        contract_path = cwd / ".agent/execution.contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["concurrency"]["lease_policy"] = "enforce"
        contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        return cwd

    def _backdate_claim_lease(self, cwd: Path) -> None:
        path = cwd / ".agent/step-runs.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[-1]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )

    def test_reclaim_step_recovers_expired_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._lease_cli_root(tmp)
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            self._backdate_claim_lease(cwd)
            result = run_agentflow(
                cwd, "reclaim-step", "P1", "--agent", "agent-b", "--reason", "crash", "--json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["attempt_id"], "A2")

    def test_renew_lease_command_records_metadata_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._lease_cli_root(tmp)
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            result = run_agentflow(
                cwd, "renew-lease", "P1", "--agent", "agent-a", "--minutes", "60", "--json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["event"], "lease_renewed")

    def test_run_requires_agent_under_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._lease_cli_root(tmp)
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            result = run_agentflow(cwd, "run", "--step", "P1", "--", "true")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("AGENTFLOW_AGENT_ID", result.stderr)

    def test_agent_flag_threads_through_run_and_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._lease_cli_root(tmp)
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(
                    cwd, "record-file-change", "--step", "P1",
                    "--path", "fixture.txt", "--agent", "agent-a",
                ).returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(
                    cwd, "run", "--step", "P1", "--agent", "agent-a",
                    "--gate", "python3 -c \"print('ok')\"",
                    "--", "python3", "-c", "print('ok')",
                ).returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(cwd, "verify-step", "P1", "--agent", "agent-a").returncode, 0
            )
            self.assertEqual(
                run_agentflow(cwd, "complete-step", "P1", "--agent", "agent-a").returncode, 0
            )

    def test_foreign_agent_rejected_on_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = self._lease_cli_root(tmp)
            self.assertEqual(
                run_agentflow(cwd, "claim-step", "P1", "--agent", "agent-a").returncode, 0
            )
            (cwd / "fixture.txt").write_text("hello\n", encoding="utf-8")
            self.assertEqual(
                run_agentflow(
                    cwd, "record-file-change", "--step", "P1",
                    "--path", "fixture.txt", "--agent", "agent-a",
                ).returncode,
                0,
            )
            self.assertEqual(
                run_agentflow(
                    cwd, "run", "--step", "P1", "--agent", "agent-a",
                    "--gate", "python3 -c \"print('ok')\"",
                    "--", "python3", "-c", "print('ok')",
                ).returncode,
                0,
            )
            ledger = cwd / ".agent/verification-runs.jsonl"
            before = ledger.read_text(encoding="utf-8")

            result = run_agentflow(cwd, "verify-step", "P1", "--agent", "agent-b")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("owned by agent-a", result.stderr)
            self.assertEqual(ledger.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
