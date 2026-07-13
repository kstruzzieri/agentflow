"""Shared Agentflow artifact contracts and enums."""

from __future__ import annotations

import os
from typing import Optional


TOOL_VERSION = "0.3.0"

PLAN_SCHEMA_VERSION = "0.3.0"
EVIDENCE_SCHEMA_VERSION = "0.2.0"
ASSUMPTIONS_SCHEMA_VERSION = "0.2.0"
AMENDMENTS_SCHEMA_VERSION = "0.2.0"
CONTEXT_RECEIPTS_SCHEMA_VERSION = "0.2.0"
FAILURES_SCHEMA_VERSION = "0.2.0"
DRIFT_REPORT_SCHEMA_VERSION = "0.2.2"
PROOF_PACK_SCHEMA_VERSION = "0.9.0"
RUNTIME_CONFIG_SCHEMA_VERSION = "0.3.0"
RUNTIME_SNAPSHOT_SCHEMA_VERSION = "0.3.0"
WORKFLOW_CONTRACT_SCHEMA_VERSION = "0.1.0"
WORKFLOW_PACK_SCHEMA_VERSION = "0.1.0"
CAPABILITY_RECEIPTS_SCHEMA_VERSION = "0.1.0"
AGGREGATION_SCHEMA_VERSION = "0.1.0"

EXECUTION_CONTRACT_SCHEMA_VERSION = "0.3.0"
STEP_RUNS_SCHEMA_VERSION = "0.5.0"
COMMAND_RECEIPTS_SCHEMA_VERSION = "0.4.0"
FILE_RECEIPTS_SCHEMA_VERSION = "0.4.0"
VERIFICATION_RUNS_SCHEMA_VERSION = "0.4.0"

REVIEW_RUNS_SCHEMA_VERSION = "0.5.0"
REVIEW_MANIFEST_SCHEMA_VERSION = "0.2.0"  # keep in lockstep with review_runner.MANIFEST_SCHEMA_VERSION

ARTIFACT_SCHEMA_VERSIONS = {
    "plan-lock": PLAN_SCHEMA_VERSION,
    "evidence": EVIDENCE_SCHEMA_VERSION,
    "assumptions": ASSUMPTIONS_SCHEMA_VERSION,
    "amendments": AMENDMENTS_SCHEMA_VERSION,
    "context-receipts": CONTEXT_RECEIPTS_SCHEMA_VERSION,
    "failures": FAILURES_SCHEMA_VERSION,
    "drift-report": DRIFT_REPORT_SCHEMA_VERSION,
    "proof-pack": PROOF_PACK_SCHEMA_VERSION,
    "runtime-config": RUNTIME_CONFIG_SCHEMA_VERSION,
    "runtime-snapshot": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
    "workflow-contract": WORKFLOW_CONTRACT_SCHEMA_VERSION,
    "capability-receipts": CAPABILITY_RECEIPTS_SCHEMA_VERSION,
    "review-runs": REVIEW_RUNS_SCHEMA_VERSION,
    "aggregation": AGGREGATION_SCHEMA_VERSION,
}

EXECUTION_ARTIFACT_SCHEMA_VERSIONS = {
    "execution-contract": EXECUTION_CONTRACT_SCHEMA_VERSION,
    "step-runs": STEP_RUNS_SCHEMA_VERSION,
    "command-receipts": COMMAND_RECEIPTS_SCHEMA_VERSION,
    "file-receipts": FILE_RECEIPTS_SCHEMA_VERSION,
    "verification-runs": VERIFICATION_RUNS_SCHEMA_VERSION,
}

# Working-state ingestion policy. Historical cross-major proof compatibility is
# handled only by verify-proof; no generic reader inherits that promise.
POLICY_EXACT = "exact"
POLICY_SAME_MAJOR = "same_major"
POLICY_NONE = "none"
# Fallback used by validate_schema_version for artifacts outside this table
# (e.g. the transient task-brief intake); named so the default is a visible
# decision, not an accident of a .get() call.
DEFAULT_COMPATIBILITY_POLICY = POLICY_SAME_MAJOR

# Every declared artifact is classified explicitly (no comprehension fill), so
# adding an artifact forces a policy decision here and the guard test in
# test_schema_contracts can actually fail on an unclassified one.
ARTIFACT_COMPATIBILITY_POLICIES = {
    "plan-lock": POLICY_SAME_MAJOR,
    "evidence": POLICY_SAME_MAJOR,
    "assumptions": POLICY_SAME_MAJOR,
    "amendments": POLICY_SAME_MAJOR,
    "context-receipts": POLICY_SAME_MAJOR,
    "failures": POLICY_SAME_MAJOR,
    "drift-report": POLICY_SAME_MAJOR,
    "proof-pack": POLICY_NONE,
    "runtime-config": POLICY_SAME_MAJOR,
    "runtime-snapshot": POLICY_SAME_MAJOR,
    "workflow-contract": POLICY_SAME_MAJOR,
    "capability-receipts": POLICY_SAME_MAJOR,
    "review-runs": POLICY_SAME_MAJOR,
    "aggregation": POLICY_SAME_MAJOR,
    "execution-contract": POLICY_EXACT,
    "step-runs": POLICY_SAME_MAJOR,
    "command-receipts": POLICY_SAME_MAJOR,
    "file-receipts": POLICY_SAME_MAJOR,
    "verification-runs": POLICY_SAME_MAJOR,
}

# Derived summaries and transient intake files are intentionally excluded.
BASE_ARTIFACT_PATHS = {
    "plan-lock": ".agent/plan.lock.json",
    "evidence": ".agent/evidence.jsonl",
    "assumptions": ".agent/assumptions.json",
    "context-receipts": ".agent/context-receipts.jsonl",
    "failures": ".agent/failures.jsonl",
    "amendments": ".agent/amendments.jsonl",
    "drift-report": ".agent/drift-report.json",
    "runtime-config": ".agent/runtime.config.json",
    "runtime-snapshot": ".agent/runtime-snapshots.jsonl",
    "workflow-contract": ".agent/workflow.contract.json",
    "capability-receipts": ".agent/capability-receipts.jsonl",
    "review-runs": ".agent/review-runs.jsonl",
}

EXECUTION_ARTIFACT_PATHS = {
    "execution-contract": ".agent/execution.contract.json",
    "step-runs": ".agent/step-runs.jsonl",
    "command-receipts": ".agent/command-receipts.jsonl",
    "file-receipts": ".agent/file-receipts.jsonl",
    "verification-runs": ".agent/verification-runs.jsonl",
}

ARTIFACT_PATHS = {
    **BASE_ARTIFACT_PATHS,
    **EXECUTION_ARTIFACT_PATHS,
    # "aggregation" lives only here, not in BASE_ARTIFACT_PATHS or
    # EXECUTION_ARTIFACT_PATHS: those two dicts drive aggregate.py's own
    # merge-source path list (_ALL_PATHS) and proof.py's normalized execution
    # hash, and adding aggregation.json to either would fold it into the
    # cross-worktree merge/hash it is itself the output of.
    "aggregation": ".agent/aggregation.json",
}

ADAPTERS = ("claude", "codex", "custom", "go-llm", "openai-compatible")
ROUTE_POLICIES = (
    "manual_only",
    "prefer_local",
    "prefer_low_cost",
    "prefer_quality",
    "prefer_speed",
)
READINESS_CHECKS = ("command_exists", "command_spawn", "http", "http_status", "none")
# #19: MCP servers are evidence-only. http (the /v1/models capability probe) is
# runtime-specific; MCP liveness uses http_status.
MCP_READINESS_CHECKS = ("command_exists", "command_spawn", "http_status", "none")
MCP_TRANSPORTS = ("http", "sse", "stdio")
MCP_SERVER_STATUSES = ("configured", "ready", "unavailable")
RUNTIME_STATUSES = ("configured", "ready", "degraded", "unavailable")
CAPABILITY_SOURCES = ("declared", "probed", "none")
EVIDENCE_KINDS = ("command", "file", "log", "review", "runtime", "test", "url", "user")
CHECK_STATUSES = (
    "failed",
    "not_applicable",
    "not_run",
    "passed",
    "skipped",
    "warning",
)
EXECUTION_MODES = ("assistive", "external", "manual", "same_session")
AUTHORITIES = ("advise", "commit", "edit", "read", "verify")
RISK_LEVELS = ("low", "medium", "high")
RISK_POLICIES = ("block", "require-confirmation", "warn")
REVIEW_GATE_POLICIES = ("block", "ignore", "warn")
WORKFLOW_REVIEW_DEPTHS = ("deep", "light", "none", "spec_quality", "standard")
# #74: each workflow review_depth contributes a (review-gate floor, requires-run)
# policy. Joined over the execution-contract gate at proof time; the floor can only
# raise strictness, never lower it. ``none`` requires no run but does not suppress a
# recorded review run's own gate (the join leaves the execution default in place).
REVIEW_DEPTH_POLICY = {
    "none": ("ignore", False),
    "light": ("warn", False),
    "standard": ("warn", False),
    "spec_quality": ("block", True),
    "deep": ("block", True),
}
# Total order over review depths (#71 draft-plan solver, #92 recorded-run depth).
REVIEW_DEPTH_ORDER = {"none": 0, "light": 1, "standard": 2, "spec_quality": 3, "deep": 4}


def review_depth_rank(depth: str) -> int:
    """Rank a validated review depth (KeyError on an unknown value)."""
    return REVIEW_DEPTH_ORDER[depth]


def recorded_review_depth(depth: Optional[str]) -> str:
    """Resolve a recorded run's ``depth_profile``.

    Absent (``None``) is a legacy pre-#92 run and ranks as ``deep`` — it
    satisfies every requirement, so old ledgers keep verifying. A present but
    out-of-enum value is malformed review evidence, not a silent ``deep``.
    """
    if depth is None:
        return "deep"
    if depth not in REVIEW_DEPTH_ORDER:
        raise ValueError(f"review depth invalid: {depth!r}")
    return depth


def review_depth_satisfies(recorded: str, required: Optional[str]) -> bool:
    """True when a recorded run depth satisfies a required depth.

    ``required=None`` means a run is required but no depth floor was declared
    (e.g. an execution-contract-only ``require_review_run``), so any recorded
    run satisfies it. Never treat ``None`` as ``deep`` — that would silently
    raise strictness.
    """
    if required is None:
        return True
    return review_depth_rank(recorded) >= review_depth_rank(required)


CAPABILITY_STATUSES = ("used", "waived")
CAPABILITY_IDS = (
    "tdd",
    "debugging",
    "security-review",
    "frontend-qa",
    "review-spec",
    "review-quality",
    "strict-verification",
)
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
COMMAND_DECISIONS = ("allowed", "blocked", "timeout")
RISK_CATEGORIES = (
    "blocked_path",
    "credential_read",
    "destructive_delete",
    "permission_change",
    "pipe_to_shell",
    "privilege_escalation",
    "write_outside_scope",
)
PROVENANCE_VALUES = ("attested", "managed", "observed", "reconstructed")
TRUSTED_PROVENANCE_VALUES = ("managed", "observed", "reconstructed")
CHANGE_KINDS = ("added", "deleted", "modified", "renamed")
DIFF_COMMAND_VERSION = "afhunk-v1"
HUNK_ATTRIBUTION_POLICIES = ("enforce", "observe", "off")
UNMAPPED_HUNK_REASONS = ("no_matching_hunk",)
GATE_KINDS = ("command", "inspection")
STEP_EVENT_KINDS = (
    "abandoned",
    "blocked",
    "claimed",
    "completed",
    "failed",
    "in_progress",
    "lease_renewed",
    "verified",
)
RECEIPT_STORES = ("by_attempt", "content_addressed")
WRITER_MODELS = ("single_writer",)
LEASE_POLICIES = ("advisory", "enforce")
DEFAULT_LEASE_TTL_MINUTES = 30
DEFAULT_LEASE_GRACE_SECONDS = 30
PROVIDER_NEUTRAL_DENYLIST = (
    "browser tool",
    "chatgpt",
    "claude",
    "codex",
    "mcp",
    "provider function",
    "superpowers",
)


def strict_mode(cli_strict: bool = False) -> bool:
    return cli_strict or os.environ.get("AGENTFLOW_STRICT") == "1"
