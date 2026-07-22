"""Generator for the machine-readable CLI contract manifest.

``docs/cli-contract.json`` is produced by ``scripts/gen_cli_contract.py`` from
:func:`build_cli_contract`; ``tests/test_stability_policy.py`` re-derives the
same inventory so any undocumented CLI surface drift fails CI.

The serialized argument records are a *stable projection* of the parser: they
deliberately expose only user-visible facts (names, requiredness, arity,
defaults, choices) and never argparse internals such as action class names or
``type`` callable names, which docs/stability.md classifies as changeable
Python internals.
"""

from __future__ import annotations

import argparse

from .cli import build_parser


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
RESUMABILITY = {
    "type": "object|null",
    "keys": {
        "contract": {
            "type": "object",
            "keys": {
                "plan_schema_version": "string|null",
                "plan_sha256": "string|null",
                "locked": "boolean",
                "locked_at": "string|null",
                "execution_schema_version": "string|null",
                "execution_contract_sha256": "string|null",
            },
        },
        "agent_id": "string|null",
        "step": {
            "type": "object|null",
            "keys": {
                "id": "string",
                "state": "string",
                "completed": "boolean",
            },
        },
        "attempt": {
            "type": "object|null",
            "keys": {
                "id": "string",
                "state": "string|null",
                "owner": "string|null",
                "open": "boolean",
            },
        },
        "lease": {
            "type": "object",
            "keys": {
                "policy": "string|null",
                "ttl_minutes": "integer|null",
                "grace_seconds": "integer|null",
                "expires_at": "string|null",
                "state": "string",
                "exclusive": "boolean",
            },
        },
        "receipts": {
            "type": "object",
            "keys": {
                "commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "keys": {
                            "id": "string",
                            "gate": "string|null",
                            "command": "array|null",
                            "exit_code": "integer|null",
                            "decision": "string|null",
                            "timed_out": "boolean",
                            "provenance": "string|null",
                            "finished_at": "string|null",
                        },
                    },
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "keys": {
                            "id": "string",
                            "path": "string|null",
                            "change_kind": "string|null",
                            "recorded_at": "string|null",
                        },
                    },
                },
            },
        },
        "gates": {
            "type": "array",
            "items": {
                "type": "object",
                "keys": {
                    "kind": "string",
                    "label": "string",
                    "status": "string",
                    "receipt_id": "string|null",
                    "evidence_id": "string|null",
                },
            },
        },
        "recovery_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "keys": {
                    "action": "string",
                    "allowed": "boolean",
                    "automatic": "boolean",
                    "break_glass": "boolean",
                    "reason": "string",
                },
            },
        },
        "diagnostics": {
            "type": "array",
            "items": {
                "type": "object",
                "keys": {
                    "code": "string",
                    "message": "string",
                    "artifact": "string",
                },
            },
        },
    },
}

JSON_OUTPUTS = {
    "pack inspect": [{"type": "object", "keys": {"status": "string", "errors": "array", "id": "string|null", "version": "string|null", "profiles": "array|null", "manifest_sha256": "string|null"}}],
    "doctor": [{"type": "object", "keys": {"status": "string", "contract": "object|null", "findings": "array"}}],
    "lock-plan": [{"type": "object", "keys": {"status": "string", "errors": "array", "path": "string|null"}}],
    "recommend-workflow": [{"type": "object", "keys": {"status": "string", "errors": "array", "recommended": "object|null", "selected": "object|null", "selection_mode": "string|null", "reason": "string|null"}}],
    "draft-plan": [{"type": "object", "keys": {"schema_version": "string|null", "status": "string", "errors": "array|null", "path": "string|null", "contract_path": "string|null", "plan_candidate": "object|null", "workflow_contract": "object|null", "recommended": "object|null", "selected": "object|null", "selection_mode": "string|null", "warnings": "array|null"}}],
    "record-review": [{"type": "object", "keys": {"schema_version": "string", "review_run_id": "string", "recorded_at": "string", "state_dir": "string", "manifest_path": "string", "manifest_sha256": "string", "plan_sha256": "string", "policy": "string|null", "gate_status": "string", "active_blocking": "array", "depth_profile": "string", "amendment_ready": "boolean", "findings": "object", "artifacts": "array"}}],
    "review-manifest": [{"type": "object", "keys": {"schema_version": "string", "review_run_id": "string", "state_dir": "string", "policy": "string", "gate_status": "string", "active_blocking": "array", "depth_profile": "string", "amendment_ready": "boolean", "findings": "object", "artifacts": "array"}}],
    "next-step": [{"type": "object", "keys": {"id": "string", "action": "string", "files": "array", "preconditions": "array", "validation": "array", "expected_diff": "array", "evidence_ids": "array", "design_decision_ids": "array|null"}}, {"type": "null"}],
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
    "aggregate-ledgers": [
        {"type": "object", "keys": {"status": "string", "sources": "array", "collisions": "array", "planned": "object"}},
        {"type": "object", "keys": {"status": "string", "sources": "array", "written": "object"}},
    ],
    "detect-stuck": [{"type": "object", "keys": {"schema_version": "string", "status": "string", "findings": "array", "thresholds": "object", "recorded_at": "string"}}],
    "lint-handoff": [{"type": "object", "keys": {"findings": "array"}}],
    "replay-gates": [{"type": "object", "keys": {"status": "string", "errors": "array", "warnings": "array", "receipts": "array", "recorded": "boolean"}}],
    "runtime-status": [{"type": "object", "keys": {"schema_version": "string|null", "id": "string|null", "status": "string|null", "runtimes": "array|null", "mcp_servers": "array|null", "routes": "array|null", "findings": "array", "runtime_config_sha256": "string|null", "created_at": "string|null"}}],
    "events": [{"type": "array", "items": {"type": "object", "keys": {"timestamp": "string", "type": "string", "step_id": "string|null", "attempt_id": "string|null", "source": "object", "data": "object"}}}],
    "next-action": [{"type": "object", "keys": {"state": "string", "reason": "string", "blocking": "boolean", "command": "string|null", "args": "array|null", "step_id": "string|null", "gate": "string|null", "diagnostics": "array", "resumability": RESUMABILITY}}],
    "finish-step": [{"type": "object", "keys": {"verification_status": "string", "verified": "boolean", "completed": "boolean", "diagnostics": "array"}}],
    "finish-run": [{"type": "object", "keys": {"ok": "boolean", "gates": "array", "stopped_at": "string|null", "diagnostics": "array"}}],
}

_MULTI_VALUE_NARGS = ("*", "+", argparse.REMAINDER)


def build_cli_contract() -> dict:
    def arguments(parser: argparse.ArgumentParser) -> list:
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
                "takes_value": action.nargs != 0,
                "multiple": action.nargs in _MULTI_VALUE_NARGS
                or isinstance(action, argparse._AppendAction),
            })
        return result

    commands: dict = {}

    def walk(parser: argparse.ArgumentParser, prefix: str = "") -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    command = f"{prefix} {name}".strip()
                    commands[command] = arguments(child)
                    walk(child, command)

    walk(build_parser())
    return {"schema_version": "1.0.0", "commands": commands, "json_outputs": JSON_OUTPUTS}
