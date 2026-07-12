from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path

from agentflow.cli import build_parser
from agentflow.events import project_events
from agentflow.execution import doctor
from agentflow.porcelain import next_action


ROOT = Path(__file__).resolve().parents[1]

STEP_EVENT = {
    "schema_version": "string", "event": "string", "step_id": "string",
    "attempt_id": "string", "agent_id": "string|null", "recorded_at": "string",
    "lease_expires_at": "string|null", "reason": "string|null",
    "reason_code": "string|null", "findings": "array|null",
    "amends_attempt": "string|null", "amends_completed_at": "string|null",
}
COMMAND_RECEIPT = {
    "schema_version": "string", "id": "string", "step_id": "string",
    "attempt_id": "string", "command": "array", "cwd": "string", "env_names": "array",
    "gate": "string|null", "decision": "string", "risk": "object", "provenance": "string",
    "started_at": "string", "finished_at": "string", "exit_code": "integer",
    "timed_out": "boolean", "timeout_seconds": "number|null", "truncated": "boolean",
    "stdout_path": "string", "stdout_sha256": "string", "stdout_truncated": "boolean",
    "stderr_path": "string", "stderr_sha256": "string", "stderr_truncated": "boolean",
}
FILE_RECEIPT = {
    "schema_version": "string", "id": "string", "step_id": "string",
    "attempt_id": "string", "path": "string", "previous_path": "string|null",
    "change_kind": "string", "before_git_blob": "string|null", "after_sha256": "string|null",
    "diff_engine": "string", "diff_algorithm": "string", "diff_command_version": "string",
    "diff_unified": "integer", "hunks": "array", "hunk_attribution": "string",
    "recorded_at": "string",
}
VERIFICATION = {
    "schema_version": "string", "id": "string", "scope": "string", "status": "string",
    "step_id": "string|null", "attempt_id": "string|null", "strict": "boolean",
    "replay": "boolean", "findings": "array", "recorded_at": "string",
}

JSON_OUTPUTS = {
    "pack inspect": [{"type": "object", "keys": {"status": "string", "errors": "array", "id": "string|null", "version": "string|null", "profiles": "array|null", "manifest_sha256": "string|null"}}],
    "doctor": [{"type": "object", "keys": {"status": "string", "contract": "object|null", "findings": "array"}}],
    "lock-plan": [{"type": "object", "keys": {"status": "string", "errors": "array", "path": "string|null"}}],
    "recommend-workflow": [{"type": "object", "keys": {"status": "string", "errors": "array", "recommended": "object|null", "selected": "object|null", "selection_mode": "string|null", "reason": "string|null"}}],
    "draft-plan": [{"type": "object", "keys": {"schema_version": "string|null", "status": "string", "errors": "array|null", "path": "string|null", "contract_path": "string|null", "plan_candidate": "object|null", "workflow_contract": "object|null", "recommended": "object|null", "selected": "object|null", "selection_mode": "string|null", "warnings": "array|null"}}],
    "record-review": [{"type": "object", "keys": {"schema_version": "string", "review_run_id": "string", "recorded_at": "string", "manifest_sha256": "string", "gate_status": "string", "active_blocking": "array", "findings": "array", "source": "string"}}],
    "review-manifest": [{"type": "object", "keys": {"schema_version": "string", "review_run_id": "string", "gate_status": "string", "active_blocking": "array", "findings": "array", "artifacts": "object", "depth_profile": "string"}}],
    "next-step": [{"type": "object", "keys": {"id": "string", "action": "string", "files": "array", "preconditions": "array", "validation": "array", "expected_diff": "array", "evidence_ids": "array"}}, {"type": "null"}],
    "claim-step": [{"type": "object", "keys": STEP_EVENT}],
    "amend-step": [{"type": "object", "keys": STEP_EVENT}],
    "complete-step": [{"type": "object", "keys": STEP_EVENT}],
    "block-step": [{"type": "object", "keys": STEP_EVENT}],
    "fail-step": [{"type": "object", "keys": STEP_EVENT}],
    "reclaim-step": [{"type": "object", "keys": STEP_EVENT}],
    "renew-lease": [{"type": "object", "keys": STEP_EVENT}],
    "run": [{"type": "object", "keys": COMMAND_RECEIPT}],
    "record-command": [{"type": "object", "keys": COMMAND_RECEIPT}],
    "record-file-change": [{"type": "array", "items": {"type": "object", "keys": FILE_RECEIPT}}],
    "verify-step": [{"type": "object", "keys": VERIFICATION}],
    "verify-run": [{"type": "object", "keys": VERIFICATION}],
    "aggregate-ledgers": [{"type": "object", "keys": {"status": "string", "collisions": "array", "sources": "array", "source_count": "integer", "output": "string", "dry_run": "boolean", "rewrites": "array"}}],
    "detect-stuck": [{"type": "object", "keys": {"schema_version": "string", "status": "string", "findings": "array", "thresholds": "object", "recorded_at": "string"}}],
    "lint-handoff": [{"type": "object", "keys": {"findings": "array"}}],
    "replay-gates": [{"type": "object", "keys": {"status": "string", "errors": "array", "warnings": "array", "receipts": "array", "recorded": "boolean"}}],
    "runtime-status": [{"type": "object", "keys": {"schema_version": "string|null", "id": "string|null", "status": "string|null", "runtimes": "array|null", "mcp_servers": "array|null", "routes": "array|null", "findings": "array", "runtime_config_sha256": "string|null", "created_at": "string|null"}}],
    "events": [{"type": "array", "items": {"type": "object", "keys": {"timestamp": "string", "type": "string", "step_id": "string|null", "attempt_id": "string|null", "source": "object", "data": "object"}}}],
    "next-action": [{"type": "object", "keys": {"state": "string", "reason": "string", "blocking": "boolean", "command": "string|null", "args": "array|null", "step_id": "string|null", "diagnostics": "array"}}],
    "finish-step": [{"type": "object", "keys": {"verification_status": "string", "verified": "boolean", "completed": "boolean", "diagnostics": "array"}}],
    "finish-run": [{"type": "object", "keys": {"ok": "boolean", "gates": "array", "stopped_at": "string|null", "diagnostics": "array"}}],
}


def build_cli_contract() -> dict[str, object]:
    def arguments(parser: argparse.ArgumentParser) -> list[dict[str, object]]:
        result = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) or action.dest == "help":
                continue
            default = action.default
            if default is argparse.SUPPRESS or callable(default):
                default = None
            required = bool(action.option_strings and action.required) or (
                not action.option_strings
                and action.nargs not in ("?", "*", argparse.REMAINDER)
            )
            result.append({
                "names": action.option_strings or [action.dest],
                "required": required, "nargs": action.nargs, "default": default,
                "choices": list(action.choices) if action.choices is not None else None,
                "type": getattr(action.type, "__name__", None), "action": type(action).__name__,
            })
        return result

    commands: dict[str, list[dict[str, object]]] = {}

    def walk(parser: argparse.ArgumentParser, prefix: str = "") -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    command = f"{prefix} {name}".strip()
                    commands[command] = arguments(child)
                    walk(child, command)

    walk(build_parser())
    return {"schema_version": "1.0.0", "commands": commands, "json_outputs": JSON_OUTPUTS}


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

        samples = {
            "doctor": doctor(root),
            "events": project_events(root),
            "next-action": next_action(root).to_dict(),
            "claim-step": rows("step-runs.jsonl")[0],
            "run": rows("command-receipts.jsonl")[0],
            "record-file-change": [rows("file-receipts.jsonl")[0]],
            "verify-run": rows("verification-runs.jsonl")[0],
            "runtime-status": rows("runtime-snapshots.jsonl")[0],
        }
        for command, payload in samples.items():
            with self.subTest(command=command):
                self.assertJsonContract(command, payload)

    def test_cli_contract_manifest_matches_parser_and_covers_json_modes(self) -> None:
        manifest = json.loads((ROOT / "docs/cli-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest, build_cli_contract())

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
