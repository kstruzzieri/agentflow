from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentflow.artifacts import create_initial_artifacts
from agentflow.workflow_contract import (
    validate_workflow_contract,
    workflow_contract_summary,
    write_workflow_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def run_agentflow(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def valid_contract() -> dict:
    return {
        "schema_version": "0.1.0",
        "workflow_pack": "adaptive.default",
        "workflow_profile": "feature.medium",
        "selected_by": "operator",
        "selection_reason": "Issue #69 changes source, tests, docs, and proof policy.",
        "required_capabilities": [
            {"id": "tdd", "required": True},
            {"id": "review-spec", "required": True},
        ],
        "review_depth": "spec_quality",
        "validation_policy": {
            "required_gates": ["focused", "full-suite-if-source-changed"],
        },
        "proof_policy": {
            "hunk_attribution": "enforce",
            "require_review_run": False,
        },
    }


class WorkflowContractTests(unittest.TestCase):
    def test_valid_contract_has_no_validation_errors(self) -> None:
        self.assertEqual(validate_workflow_contract(valid_contract()), [])

    def test_rejects_unknown_fields(self) -> None:
        contract = valid_contract()
        contract["provider_hint"] = "codex"

        errors = validate_workflow_contract(contract)

        self.assertIn("unknown workflow contract field: provider_hint", errors)

    def test_rejects_malformed_capability_declarations(self) -> None:
        contract = valid_contract()
        contract["required_capabilities"] = [
            {"id": "", "required": True},
            {"id": "review-quality", "required": "yes"},
            {"id": "extra", "required": True, "source": "runtime"},
        ]

        errors = validate_workflow_contract(contract)

        self.assertIn("required_capabilities[1].id must be a non-empty string", errors)
        self.assertIn("required_capabilities[2].required must be boolean", errors)
        self.assertIn("required_capabilities[3] unknown field: source", errors)

    def test_rejects_invalid_review_depth(self) -> None:
        contract = valid_contract()
        contract["review_depth"] = "maximum"

        errors = validate_workflow_contract(contract)

        self.assertTrue(any("review_depth must be one of" in error for error in errors))

    def test_write_workflow_contract_creates_artifact_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_initial_artifacts(root)

            path = write_workflow_contract(root, valid_contract())

            self.assertEqual(path, root / ".agent/workflow.contract.json")
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["workflow_profile"], "feature.medium")
            summary = workflow_contract_summary(written)
            self.assertEqual(summary["workflow_pack"], "adaptive.default")
            self.assertEqual(summary["workflow_profile"], "feature.medium")
            self.assertEqual(summary["required_capabilities"], ["tdd", "review-spec"])

    def test_cli_writes_and_validates_contract_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            source = cwd / "contract.json"
            source.write_text(json.dumps(valid_contract()), encoding="utf-8")

            write_result = run_agentflow(cwd, "workflow-contract", "--from-json", str(source))

            self.assertEqual(write_result.returncode, 0, write_result.stdout + write_result.stderr)
            self.assertIn("wrote .agent/workflow.contract.json", write_result.stdout)
            self.assertTrue((cwd / ".agent/workflow.contract.json").exists())

            validate_result = run_agentflow(cwd, "workflow-contract", "--validate")

            self.assertEqual(
                validate_result.returncode,
                0,
                validate_result.stdout + validate_result.stderr,
            )
            self.assertIn("workflow contract valid", validate_result.stdout)

    def test_cli_rejects_invalid_contract_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            source = cwd / "contract.json"
            contract = valid_contract()
            contract["review_depth"] = "maximum"
            source.write_text(json.dumps(contract), encoding="utf-8")

            result = run_agentflow(cwd, "workflow-contract", "--from-json", str(source))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("review_depth must be one of", result.stdout)
            self.assertFalse((cwd / ".agent/workflow.contract.json").exists())

    def test_cli_rejects_validate_with_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            source = cwd / "contract.json"
            source.write_text(json.dumps(valid_contract()), encoding="utf-8")

            result = run_agentflow(
                cwd,
                "workflow-contract",
                "--from-json",
                str(source),
                "--validate",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("not allowed with argument --from-json", result.stderr)
            self.assertFalse((cwd / ".agent/workflow.contract.json").exists())

    def test_cli_rejects_custom_path_with_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
            source = cwd / "contract.json"
            source.write_text(json.dumps(valid_contract()), encoding="utf-8")

            result = run_agentflow(
                cwd,
                "workflow-contract",
                "custom.contract.json",
                "--from-json",
                str(source),
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("PATH cannot be used with --from-json", result.stderr)
            self.assertFalse((cwd / ".agent/workflow.contract.json").exists())


if __name__ == "__main__":
    unittest.main()
