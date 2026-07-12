from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StabilityPolicyTests(unittest.TestCase):
    def test_public_surface_and_deprecation_contract_is_explicit(self) -> None:
        policy = (ROOT / "docs/stability.md").read_text(encoding="utf-8")

        for surface in (
            "CLI commands and flags",
            "exit codes",
            "JSON output",
            "MCP tools",
            "AGENTFLOW_",
            "Python internals",
            "agentflow-proof",
            "agentflow-mcp",
        ):
            self.assertIn(surface, policy)
        self.assertIn("at least one minor release and 90 days", policy)
        self.assertIn("`deprecations` array", policy)

    def test_historical_promise_is_verify_proof_only_and_bounded(self) -> None:
        policy = (ROOT / "docs/compatibility.md").read_text(encoding="utf-8")

        self.assertIn("`verify-proof`", policy)
        self.assertIn("proofs built by Agentflow 0.4.0 or later", policy)
        self.assertIn("Agentflow 2.0 may drop pre-1.0 proofs", policy)
        self.assertIn("newer schema", policy)
        self.assertIn("upgrade", policy)
        self.assertIn("build-proof before a major upgrade", policy)
        for excluded in (
            "`verify-run`",
            "plan loading",
            "receipt loading",
            "aggregation",
            "mutation commands",
        ):
            self.assertIn(excluded, policy)

    def test_audit_records_inventory_defect_and_mechanical_soak_reset(self) -> None:
        audit = (ROOT / "docs/schema-freeze-audit.md").read_text(encoding="utf-8")

        for schema in (
            "PLAN_SCHEMA_VERSION",
            "EXECUTION_CONTRACT_SCHEMA_VERSION",
            "PROOF_PACK_SCHEMA_VERSION",
            "STEP_RUNS_SCHEMA_VERSION",
            "COMMAND_RECEIPTS_SCHEMA_VERSION",
            "FILE_RECEIPTS_SCHEMA_VERSION",
            "VERIFICATION_RUNS_SCHEMA_VERSION",
            "DRIFT_REPORT_SCHEMA_VERSION",
        ):
            self.assertIn(schema, audit)
        self.assertIn("_AGGREGATION_SCHEMA_VERSION_RE", audit)
        self.assertIn("candidate commit", audit)
        self.assertIn("freeze set", audit)
        self.assertIn("reset", audit.lower())


if __name__ == "__main__":
    unittest.main()
