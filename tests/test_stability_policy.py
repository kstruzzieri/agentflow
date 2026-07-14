from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentflow.cli_contract import JSON_OUTPUTS, build_cli_contract
from agentflow.events import project_events
from agentflow.execution import doctor
from agentflow.porcelain import Action, next_action


ROOT = Path(__file__).resolve().parents[1]


class StabilityPolicyTests(unittest.TestCase):
    def assertJsonContract(self, command: str, payload: object) -> None:
        def is_type(value: object, declared: str) -> bool:
            names = declared.split("|")
            checks = {
                "array": lambda item: isinstance(item, list),
                "boolean": lambda item: isinstance(item, bool),
                "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
                "null": lambda item: item is None,
                "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
                "object": lambda item: isinstance(item, dict),
                "string": lambda item: isinstance(item, str),
            }
            return any(checks[name](value) for name in names)

        def matches(value: object, variant: dict[str, object]) -> bool:
            if not is_type(value, variant["type"]):
                return False
            if variant["type"] == "object":
                keys = variant["keys"]
                required = {key for key, kind in keys.items() if "null" not in kind}
                if not required.issubset(value) or not set(value).issubset(keys):
                    return False
                return all(is_type(item, keys[key]) for key, item in value.items())
            if variant["type"] == "array" and "items" in variant:
                return all(matches(item, variant["items"]) for item in value)
            return True

        self.assertTrue(
            any(matches(payload, variant) for variant in JSON_OUTPUTS[command]),
            f"{command} payload does not match its public JSON contract: {payload!r}",
        )

    def test_json_contract_matches_representative_runtime_payloads(self) -> None:
        root = ROOT / "tests/fixtures/compatibility/current-full"

        def rows(name: str) -> list[dict[str, object]]:
            return [
                json.loads(line)
                for line in (root / ".agent" / name).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        samples = [
            ("doctor", doctor(root)),
            ("events", project_events(root)),
            ("next-action", next_action(root).to_dict()),
            ("next-action", Action(
                "validation_missing", "validation gate is unmet", gate="test gate"
            ).to_dict()),
            ("claim-step", rows("step-runs.jsonl")[0]),
            ("run", rows("command-receipts.jsonl")[0]),
            ("record-file-change", [rows("file-receipts.jsonl")[0]]),
            ("verify-run", rows("verification-runs.jsonl")[0]),
            ("runtime-status", rows("runtime-snapshots.jsonl")[0]),
            (
                "record-review",
                {
                    "schema_version": "0.6.0",
                    "review_run_id": "RR-20260713T000000Z-1234abcd",
                    "recorded_at": "2026-07-13T00:00:00+00:00",
                    "state_dir": "docs/ai/state/main",
                    "manifest_path": "docs/ai/state/main/review-manifest.json",
                    "manifest_sha256": "0" * 64,
                    "plan_sha256": "1" * 64,
                    "policy": "main",
                    "gate_status": "pass",
                    "active_blocking": [],
                    "depth_profile": "deep",
                    "amendment_ready": True,
                    "findings": {"index": []},
                    "artifacts": [],
                },
            ),
            (
                "review-manifest",
                {
                    "schema_version": "1.0.0",
                    "review_run_id": "RR-20260713T000000Z-1234abcd",
                    "state_dir": "docs/ai/state/main",
                    "policy": "main",
                    "gate_status": "pass",
                    "active_blocking": [],
                    "depth_profile": "deep",
                    "amendment_ready": True,
                    "findings": {"index": []},
                    "artifacts": [{"path": "findings-final.json"}],
                },
            ),
        ]
        for command, payload in samples:
            with self.subTest(command=command):
                self.assertJsonContract(command, payload)

    def test_cli_contract_manifest_matches_parser_and_covers_json_modes(self) -> None:
        manifest = json.loads((ROOT / "docs/cli-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest,
            build_cli_contract(),
            "docs/cli-contract.json is stale; regenerate with: "
            "python3 scripts/gen_cli_contract.py",
        )

        pack_path = next(
            argument
            for argument in manifest["commands"]["pack inspect"]
            if argument["names"] == ["path"]
        )
        optional_plan = next(
            argument
            for argument in manifest["commands"]["lock-plan"]
            if argument["names"] == ["plan"]
        )
        remainder = next(
            argument
            for argument in manifest["commands"]["run"]
            if argument["names"] == ["command"]
        )
        self.assertTrue(pack_path["required"])
        self.assertFalse(optional_plan["required"])
        self.assertFalse(remainder["required"])

        json_commands = {
            command
            for command, args in manifest["commands"].items()
            if any("--json" in arg["names"] or "--jsonl" in arg["names"] for arg in args)
        }
        self.assertEqual(set(manifest["json_outputs"]), json_commands)
        for command, variants in manifest["json_outputs"].items():
            with self.subTest(command=command):
                self.assertTrue(variants)
                for variant in variants:
                    self.assertIn(variant["type"], {"array", "object", "null"})
                    if variant["type"] == "object":
                        self.assertTrue(variant["keys"])

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
        self.assertIn("tell the user to **upgrade**", policy)
        self.assertIn("proof-pack **schema** version", policy)
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
        self.assertIn(
            "reset the candidate commit, start date, and 21-day clock", audit
        )


if __name__ == "__main__":
    unittest.main()
