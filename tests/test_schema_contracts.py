from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from agentflow.aggregate import SOURCE_ID_RE
from agentflow.contracts import (
    ADAPTERS,
    AGGREGATION_SCHEMA_VERSION,
    ARTIFACT_PATHS,
    ARTIFACT_COMPATIBILITY_POLICIES,
    ARTIFACT_SCHEMA_VERSIONS,
    BASE_ARTIFACT_PATHS,
    CAPABILITY_RECEIPTS_SCHEMA_VERSION,
    CAPABILITY_STATUSES,
    CHECK_STATUSES,
    CHANGE_KINDS,
    DIFF_COMMAND_VERSION,
    DRIFT_REPORT_SCHEMA_VERSION,
    EVIDENCE_KINDS,
    EXECUTION_ARTIFACT_PATHS,
    EXECUTION_ARTIFACT_SCHEMA_VERSIONS,
    FILE_RECEIPTS_SCHEMA_VERSION,
    GATE_KINDS,
    HUNK_ATTRIBUTION_POLICIES,
    MCP_READINESS_CHECKS,
    MCP_TRANSPORTS,
    PROOF_PACK_SCHEMA_VERSION,
    PROVENANCE_VALUES,
    READINESS_CHECKS,
    REVIEW_RUNS_SCHEMA_VERSION,
    STEP_RUNS_SCHEMA_VERSION,
    RISK_POLICIES,
    ROUTE_POLICIES,
    UNMAPPED_HUNK_REASONS,
    WORKFLOW_CONTRACT_SCHEMA_VERSION,
    WORKFLOW_REVIEW_DEPTHS,
)


ROOT = Path(__file__).resolve().parents[1]

SCHEMA_FILES = {
    "plan-lock": "plan-lock.schema.json",
    "evidence": "evidence.schema.json",
    "assumptions": "assumptions.schema.json",
    "amendments": "amendments.schema.json",
    "context-receipts": "context-receipts.schema.json",
    "drift-report": "drift-report.schema.json",
    "proof-pack": "proof-pack.schema.json",
    "runtime-config": "runtime-config.schema.json",
    "runtime-snapshot": "runtime-snapshot.schema.json",
    "workflow-contract": "workflow-contract.schema.json",
    "capability-receipts": "capability-receipts.schema.json",
    "aggregation": "aggregation.schema.json",
    "execution-contract": "execution-contract.schema.json",
    "step-runs": "step-runs.schema.json",
    "command-receipts": "command-receipts.schema.json",
    "file-receipts": "file-receipts.schema.json",
    "verification-runs": "verification-runs.schema.json",
    "review-runs": "review-runs.schema.json",
}


def load_schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def artifact_schema_version(artifact: str) -> str:
    return (
        EXECUTION_ARTIFACT_SCHEMA_VERSIONS.get(artifact)
        or ARTIFACT_SCHEMA_VERSIONS[artifact]
    )


class SchemaContractTests(unittest.TestCase):
    def test_every_declared_artifact_has_one_compatibility_policy(self) -> None:
        # The policy table is a hand-written literal (not derived from the
        # schema-version dicts), so this equality genuinely fails when a new
        # artifact is declared without an explicit policy decision.
        declared = set(ARTIFACT_SCHEMA_VERSIONS) | set(EXECUTION_ARTIFACT_SCHEMA_VERSIONS)
        self.assertEqual(set(ARTIFACT_COMPATIBILITY_POLICIES), declared)
        self.assertEqual(
            set(ARTIFACT_COMPATIBILITY_POLICIES.values()),
            {"exact", "same_major", "none"},
        )
        strict = {
            name: policy
            for name, policy in ARTIFACT_COMPATIBILITY_POLICIES.items()
            if policy != "same_major"
        }
        self.assertEqual(
            strict, {"execution-contract": "exact", "proof-pack": "none"}
        )

    def test_runtime_config_enums_match_python_constants(self) -> None:
        schema = load_schema("runtime-config.schema.json")
        defs = schema["$defs"]
        self.assertEqual(tuple(sorted(defs["adapter"]["enum"])), ADAPTERS)
        self.assertEqual(tuple(sorted(defs["route_policy"]["enum"])), ROUTE_POLICIES)
        self.assertEqual(tuple(sorted(defs["readiness_check"]["enum"])), READINESS_CHECKS)

    def test_runtime_config_schema_mcp_server_matches_constants(self) -> None:
        schema = load_schema("runtime-config.schema.json")
        defs = schema["$defs"]
        self.assertEqual(tuple(sorted(defs["mcp_transport"]["enum"])), MCP_TRANSPORTS)
        self.assertEqual(
            tuple(sorted(defs["mcp_readiness_check"]["enum"])), MCP_READINESS_CHECKS
        )
        self.assertIn("mcp_servers", schema["properties"])
        self.assertEqual(
            schema["properties"]["schema_version"]["pattern"], "^0\\.[0-3]\\.[0-9]+$"
        )

    def test_evidence_kinds_match_python_constants(self) -> None:
        schema = load_schema("evidence.schema.json")
        self.assertEqual(tuple(sorted(schema["properties"]["kind"]["enum"])), EVIDENCE_KINDS)

    def test_proof_check_statuses_match_python_constants(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        status_enum = schema["$defs"]["check"]["properties"]["status"]["enum"]
        self.assertEqual(tuple(sorted(status_enum)), CHECK_STATUSES)

    def test_schema_version_accepts_current_artifact_version(self) -> None:
        for artifact, filename in SCHEMA_FILES.items():
            schema = load_schema(filename)
            spec = schema["properties"]["schema_version"]
            current = artifact_schema_version(artifact)
            self.assertNotIn(
                "const",
                spec,
                f"{filename} pins schema_version with const; use a range",
            )
            self.assertIn("pattern", spec, f"{filename} missing schema_version pattern")
            self.assertRegex(
                current,
                spec["pattern"],
                f"{filename} pattern rejects current version {current}",
            )

    def test_proof_bundle_version_accepts_current_artifact_version(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        spec = schema["properties"]["bundle_version"]
        self.assertNotIn("const", spec)
        self.assertRegex(ARTIFACT_SCHEMA_VERSIONS["proof-pack"], spec["pattern"])

    def test_proof_schema_requires_core_checksum(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        self.assertIn("core_sha256", schema["required"])

    def test_execution_schema_versions_accept_current_versions(self) -> None:
        for artifact, filename in SCHEMA_FILES.items():
            schema = load_schema(filename)
            current = artifact_schema_version(artifact)
            spec = schema["properties"]["schema_version"]
            self.assertNotIn("const", spec)
            self.assertRegex(current, spec["pattern"])

    def test_review_manifest_pattern_matches_python_validator(self) -> None:
        # The hand-rolled validator is the only enforced gate (stdlib-only, no
        # jsonschema at runtime), so its schema_version regex must stay identical
        # to the published schema's pattern. This guards against the exact drift
        # that #47 closed: editing one side without the other.
        from agentflow.review import MANIFEST_SCHEMA_VERSION_PATTERN

        schema = load_schema("review-manifest.schema.json")
        self.assertEqual(
            schema["properties"]["schema_version"]["pattern"],
            MANIFEST_SCHEMA_VERSION_PATTERN.pattern,
        )

    def test_execution_receipt_enums_match_python_constants(self) -> None:
        command_schema = load_schema("command-receipts.schema.json")
        file_schema = load_schema("file-receipts.schema.json")
        plan_schema = load_schema("plan-lock.schema.json")
        self.assertEqual(
            tuple(sorted(command_schema["properties"]["provenance"]["enum"])),
            PROVENANCE_VALUES,
        )
        self.assertEqual(
            tuple(sorted(file_schema["properties"]["change_kind"]["enum"])),
            CHANGE_KINDS,
        )
        gate_kind = plan_schema["properties"]["steps"]["items"]["properties"]["gates"]["items"]["properties"]["kind"]["enum"]
        self.assertEqual(tuple(sorted(gate_kind)), GATE_KINDS)

    def test_plan_schema_documents_requirement_traceability(self) -> None:
        schema = load_schema("plan-lock.schema.json")
        requirement = schema["properties"]["requirements"]["items"]
        criterion = requirement["properties"]["acceptance_criteria"]["items"]
        step = schema["properties"]["steps"]["items"]
        gate = step["properties"]["gates"]["items"]

        self.assertEqual(sorted(requirement["required"]), ["acceptance_criteria", "id", "text"])
        self.assertEqual(sorted(criterion["required"]), ["id", "text"])
        self.assertEqual(
            criterion["properties"]["review"]["properties"]["minimum_depth"]["enum"],
            ["spec_quality", "deep"],
        )
        self.assertTrue(step["properties"]["criterion_ids"]["uniqueItems"])
        self.assertTrue(gate["properties"]["criterion_ids"]["uniqueItems"])
        self.assertEqual(
            requirement["properties"]["id"]["pattern"],
            criterion["properties"]["id"]["pattern"],
        )

    def test_proof_schema_documents_requirement_coverage(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        coverage = schema["properties"]["coverage"]
        requirement = schema["$defs"]["requirementCoverage"]
        criterion = schema["$defs"]["criterionCoverage"]
        evidence = schema["$defs"]["criterionEvidence"]

        self.assertEqual(
            coverage["properties"]["requirements"]["items"]["$ref"],
            "#/$defs/requirementCoverage",
        )
        self.assertEqual(
            sorted(coverage["properties"]["criterion_status_counts"]["required"]),
            ["failed", "missing", "satisfied", "unmapped"],
        )
        self.assertEqual(
            requirement["properties"]["status"]["enum"],
            ["satisfied", "failed", "missing", "unmapped"],
        )
        self.assertEqual(
            criterion["properties"]["status"]["enum"],
            ["satisfied", "failed", "missing", "unmapped"],
        )
        self.assertEqual(evidence["properties"]["kind"]["enum"], ["command", "inspection", "review"])

    def test_execution_contract_risk_policy_enum_matches_constant(self) -> None:
        schema = load_schema("execution-contract.schema.json")
        policy = schema["properties"]["command_policy"]["properties"]["risk_policy"]
        self.assertEqual(tuple(sorted(policy["enum"])), RISK_POLICIES)

    def test_workflow_contract_schema_matches_python_constants(self) -> None:
        schema = load_schema("workflow-contract.schema.json")
        self.assertRegex(
            WORKFLOW_CONTRACT_SCHEMA_VERSION,
            schema["properties"]["schema_version"]["pattern"],
        )
        self.assertRegex("0.0.0", schema["properties"]["schema_version"]["pattern"])
        self.assertEqual(
            tuple(sorted(schema["properties"]["review_depth"]["enum"])),
            WORKFLOW_REVIEW_DEPTHS,
        )
        hunk_policy = schema["$defs"]["proofPolicy"]["properties"]["hunk_attribution"]
        self.assertEqual(tuple(sorted(hunk_policy["enum"])), HUNK_ATTRIBUTION_POLICIES)

    def test_proof_schema_documents_workflow_contract_summary(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        self.assertIn("workflow_contract", schema["properties"])

    def test_capability_receipts_schema_matches_python_constants(self) -> None:
        schema = load_schema("capability-receipts.schema.json")
        self.assertRegex(
            CAPABILITY_RECEIPTS_SCHEMA_VERSION,
            schema["properties"]["schema_version"]["pattern"],
        )
        self.assertEqual(
            tuple(sorted(schema["properties"]["status"]["enum"])),
            CAPABILITY_STATUSES,
        )

    def test_proof_schema_documents_capabilities_summary(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        self.assertEqual(
            schema["properties"]["capabilities"]["$ref"],
            "#/$defs/capabilities",
        )
        props = schema["$defs"]["capabilities"]["properties"]
        self.assertEqual(sorted(props), ["missing", "recorded", "required", "waived"])

    def test_command_receipt_exit_code_allows_null(self) -> None:
        schema = load_schema("command-receipts.schema.json")
        exit_code = schema["properties"]["exit_code"]["type"]
        self.assertIn("integer", exit_code)
        self.assertIn("null", exit_code)

    def test_command_receipt_decision_enum_matches_constant(self) -> None:
        from agentflow.contracts import COMMAND_DECISIONS

        schema = load_schema("command-receipts.schema.json")
        self.assertEqual(
            tuple(sorted(schema["properties"]["decision"]["enum"])),
            COMMAND_DECISIONS,
        )

    def test_timeout_fields_are_documented_in_schemas(self) -> None:
        execution_schema = load_schema("execution-contract.schema.json")
        plan_schema = load_schema("plan-lock.schema.json")
        command_schema = load_schema("command-receipts.schema.json")
        proof_schema = load_schema("proof-pack.schema.json")

        command_policy = execution_schema["properties"]["command_policy"]["properties"]
        self.assertEqual(command_policy["command_timeout_seconds"]["type"], "integer")
        self.assertEqual(command_policy["command_timeout_seconds"]["minimum"], 1)

        gate_props = (
            plan_schema["properties"]["steps"]["items"]["properties"]["gates"]
            ["items"]["properties"]
        )
        self.assertEqual(gate_props["timeout_seconds"]["type"], "integer")
        self.assertEqual(gate_props["timeout_seconds"]["minimum"], 1)

        self.assertIn("timeout", command_schema["properties"]["decision"]["enum"])
        self.assertEqual(command_schema["properties"]["timed_out"]["type"], "boolean")
        self.assertEqual(command_schema["properties"]["timeout_seconds"]["type"], "integer")
        self.assertEqual(command_schema["properties"]["timeout_seconds"]["minimum"], 1)

        execution_props = proof_schema["$defs"]["execution"]["properties"]
        self.assertEqual(execution_props["command_timed_out"]["type"], "integer")
        self.assertEqual(execution_props["command_timed_out"]["minimum"], 0)
        self.assertEqual(
            execution_props["command_timeout_seconds"]["additionalProperties"]["type"],
            "integer",
        )
        self.assertEqual(
            execution_props["command_timeout_seconds"]["additionalProperties"]["minimum"],
            0,
        )

    def test_plan_schema_defines_depends_on_and_gates(self) -> None:
        schema = load_schema("plan-lock.schema.json")
        step_props = schema["properties"]["steps"]["items"]["properties"]
        self.assertIn("depends_on", step_props)
        self.assertIn("gates", step_props)

    def test_step_runs_schema_allows_amendment_started(self) -> None:
        schema = load_schema("step-runs.schema.json")
        self.assertIn("amendment_started", schema["properties"]["event"]["enum"])
        self.assertEqual(
            schema["properties"]["amends_attempt"]["pattern"],
            "^(WT[a-z0-9]{1,16}-)?A[0-9]+$",
        )
        self.assertIn("review_feedback", schema["properties"]["reason_code"]["enum"])
        self.assertIn("amends_completed_at", schema["properties"])


class ReviewSchemaTests(unittest.TestCase):
    def test_evidence_kind_review_in_schema(self) -> None:
        schema = load_schema("evidence.schema.json")
        self.assertIn("review", schema["properties"]["kind"]["enum"])

    def test_step_runs_allows_finding_refs(self) -> None:
        schema = load_schema("step-runs.schema.json")
        self.assertIn("finding_refs", schema["properties"])

    def test_review_runs_schema_loads(self) -> None:
        schema = load_schema("review-runs.schema.json")
        self.assertEqual(schema["title"], "Agentflow Review Run Ledger Entry")
        for field in ("review_run_id", "gate_status", "artifacts"):
            self.assertIn(field, schema["properties"])

    def test_review_criterion_evidence_documents_plan_binding(self) -> None:
        review_schema = load_schema("review-runs.schema.json")
        proof_schema = load_schema("proof-pack.schema.json")
        review_hash = review_schema["properties"]["plan_sha256"]
        evidence_hash = proof_schema["$defs"]["criterionEvidence"]["properties"][
            "plan_sha256"
        ]

        self.assertEqual(review_hash["pattern"], "^[0-9a-f]{64}$")
        self.assertEqual(evidence_hash, review_hash)

    def test_criterion_traceability_schema_versions_bumped(self) -> None:
        # #124: proof coverage grows requirements/criterion_status_counts and
        # review-runs rows grow plan_sha256, so both artifacts bump per the
        # #82 growth convention.
        self.assertEqual(PROOF_PACK_SCHEMA_VERSION, "0.10.0")
        self.assertEqual(REVIEW_RUNS_SCHEMA_VERSION, "0.6.0")

    def test_bumped_versions_still_match_schema_patterns(self) -> None:
        proof_schema = load_schema("proof-pack.schema.json")
        for field in ("schema_version", "bundle_version"):
            self.assertRegex(
                PROOF_PACK_SCHEMA_VERSION,
                proof_schema["properties"][field]["pattern"],
                field,
            )
        review_schema = load_schema("review-runs.schema.json")
        self.assertRegex(
            REVIEW_RUNS_SCHEMA_VERSION,
            review_schema["properties"]["schema_version"]["pattern"],
        )

    def test_review_manifest_schema_loads(self) -> None:
        schema = load_schema("review-manifest.schema.json")
        self.assertIn("review_run_id", schema["properties"])

    def test_review_manifest_version_constants_in_lockstep(self) -> None:
        from agentflow.contracts import REVIEW_MANIFEST_SCHEMA_VERSION
        from agentflow.review_runner import MANIFEST_SCHEMA_VERSION
        self.assertEqual(REVIEW_MANIFEST_SCHEMA_VERSION, MANIFEST_SCHEMA_VERSION)

    def test_amendment_ready_review_contract_is_versioned_and_documented(self) -> None:
        from agentflow.contracts import REVIEW_MANIFEST_SCHEMA_VERSION

        self.assertEqual(REVIEW_MANIFEST_SCHEMA_VERSION, "1.0.0")
        self.assertEqual(REVIEW_RUNS_SCHEMA_VERSION, "0.6.0")
        self.assertEqual(PROOF_PACK_SCHEMA_VERSION, "0.10.0")
        manifest = load_schema("review-manifest.schema.json")
        ledger = load_schema("review-runs.schema.json")
        proof = load_schema("proof-pack.schema.json")
        self.assertIn("amendment_ready", manifest["properties"])
        self.assertIn("amendment_ready", ledger["properties"])
        finding = manifest["properties"]["findings"]["properties"]["index"]["items"]
        for field in ("owning_step", "claim", "location", "suggested_fix"):
            self.assertIn(field, finding["properties"])
        self.assertTrue(ledger["properties"]["findings"]["additionalProperties"])
        self.assertTrue(
            proof["$defs"]["reviewRunSummary"]["properties"]["findings"]
            ["additionalProperties"]
        )
        self.assertEqual(
            ledger["allOf"][0]["then"]["properties"]["findings"]["$ref"],
            "#/$defs/findingsProjection",
        )
        self.assertEqual(
            proof["$defs"]["reviewRunSummary"]["allOf"][0]["then"]
            ["properties"]["findings"]["$ref"],
            "#/$defs/reviewFindingsProjection",
        )
        self.assertEqual(
            set(ledger["$defs"]["findingsProjection"]["required"]),
            {"counts_by_severity", "counts_by_status", "index"},
        )
        self.assertEqual(
            set(proof["$defs"]["reviewFindingsProjection"]["required"]),
            {"counts_by_severity", "counts_by_status", "index"},
        )
        self.assertNotIn("required", proof["$defs"]["reviewRunSummary"])

    def test_legacy_manifest_rows_remain_extensible_but_v1_fields_are_explicit(self) -> None:
        schema = load_schema("review-manifest.schema.json")
        finding = schema["properties"]["findings"]["properties"]["index"]["items"]
        self.assertTrue(finding["additionalProperties"])
        v1_item = schema["allOf"][0]["then"]["properties"]["findings"][
            "properties"
        ]["index"]["items"]
        self.assertEqual(
            set(v1_item["propertyNames"]["enum"]),
            set(finding["properties"]),
        )

    def test_depth_profile_enum_matches_workflow_review_depths(self) -> None:
        from agentflow.contracts import WORKFLOW_REVIEW_DEPTHS
        for filename in ("review-manifest.schema.json", "review-runs.schema.json"):
            schema = load_schema(filename)
            enum = schema["properties"]["depth_profile"]["enum"]
            self.assertEqual(sorted(enum), sorted(WORKFLOW_REVIEW_DEPTHS), filename)

    def test_execution_contract_review_gate(self) -> None:
        schema = load_schema("execution-contract.schema.json")
        props = schema["properties"]["proof_policy"]["properties"]
        self.assertEqual(sorted(props["review_gate"]["enum"]), ["block", "ignore", "warn"])

    def test_proof_schema_documents_review_policy(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        policy = schema["$defs"]["review"]["properties"]["policy"]
        self.assertEqual(policy["$ref"], "#/$defs/reviewPolicy")

        review_policy = schema["$defs"]["reviewPolicy"]
        self.assertEqual(
            sorted(review_policy["required"]),
            [
                "proof_strict_effective",
                "require_review_run",
                "review_gate_effective",
                "verification_semantics",
            ],
        )
        props = review_policy["properties"]
        self.assertEqual(sorted(props["review_gate_effective"]["enum"]), ["block", "ignore", "warn"])
        self.assertEqual(props["proof_strict_effective"]["type"], "boolean")
        self.assertEqual(props["require_review_run"]["type"], "boolean")
        self.assertEqual(props["verification_semantics"]["enum"], ["ratchet-v1"])


class LeaseSchemaContractTests(unittest.TestCase):
    def test_lease_schema_versions_bumped(self) -> None:
        self.assertEqual(PROOF_PACK_SCHEMA_VERSION, "0.10.0")
        self.assertEqual(DRIFT_REPORT_SCHEMA_VERSION, "0.2.2")
        self.assertEqual(STEP_RUNS_SCHEMA_VERSION, "0.5.0")

    def test_proof_pattern_accepts_lease_version(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        for field in ("schema_version", "bundle_version"):
            self.assertRegex(
                PROOF_PACK_SCHEMA_VERSION,
                schema["properties"][field]["pattern"],
                field,
            )

    def test_drift_schema_documents_stale_attempts(self) -> None:
        schema = load_schema("drift-report.schema.json")
        self.assertRegex(
            DRIFT_REPORT_SCHEMA_VERSION,
            schema["properties"]["schema_version"]["pattern"],
        )
        self.assertIn("stale_attempts", schema["properties"])
        item = schema["properties"]["stale_attempts"]["items"]
        self.assertEqual(
            sorted(item["required"]),
            ["attempt_id", "expired_at", "note", "owner", "step_id"],
        )


class HunkContractTests(unittest.TestCase):
    def test_versions_bumped(self) -> None:
        self.assertEqual(FILE_RECEIPTS_SCHEMA_VERSION, "0.4.0")
        self.assertEqual(DRIFT_REPORT_SCHEMA_VERSION, "0.2.2")

    def test_hunk_enums_defined(self) -> None:
        self.assertEqual(HUNK_ATTRIBUTION_POLICIES, ("enforce", "observe", "off"))
        self.assertEqual(UNMAPPED_HUNK_REASONS, ("no_matching_hunk",))
        self.assertEqual(DIFF_COMMAND_VERSION, "afhunk-v1")

    def test_bumped_versions_still_match_schema_patterns(self) -> None:
        file_schema = load_schema("file-receipts.schema.json")
        drift_schema = load_schema("drift-report.schema.json")
        self.assertRegex(FILE_RECEIPTS_SCHEMA_VERSION, file_schema["properties"]["schema_version"]["pattern"])
        self.assertRegex(DRIFT_REPORT_SCHEMA_VERSION, drift_schema["properties"]["schema_version"]["pattern"])

    def test_file_receipt_schema_documents_hunks(self) -> None:
        schema = load_schema("file-receipts.schema.json")
        props = schema["properties"]
        self.assertEqual(
            tuple(sorted(props["hunk_attribution"]["enum"])),
            tuple(sorted(("hunked", "whole_file_fallback", "disabled"))),
        )
        self.assertEqual(props["hunks"]["type"], "array")
        self.assertNotIn("hunks", schema["required"])  # additive, back-compat

    def test_execution_contract_hunk_policy_enum_matches_constant(self) -> None:
        schema = load_schema("execution-contract.schema.json")
        policy = schema["properties"]["proof_policy"]["properties"]["hunk_attribution"]
        self.assertEqual(tuple(sorted(policy["enum"])), tuple(sorted(HUNK_ATTRIBUTION_POLICIES)))

    def test_drift_unmapped_hunks_allows_string_or_object(self) -> None:
        schema = load_schema("drift-report.schema.json")
        items = schema["properties"]["unmapped_hunks"]["items"]
        self.assertIn("oneOf", items)
        object_variant = next(v for v in items["oneOf"] if v.get("type") == "object")
        self.assertEqual(
            tuple(sorted(object_variant["properties"]["reason"]["enum"])),
            tuple(sorted(UNMAPPED_HUNK_REASONS)),
        )


class AggregationSchemaContractTests(unittest.TestCase):
    def test_aggregation_path_wiring(self) -> None:
        self.assertEqual(ARTIFACT_PATHS["aggregation"], ".agent/aggregation.json")
        self.assertNotIn("aggregation", BASE_ARTIFACT_PATHS)
        self.assertNotIn("aggregation", EXECUTION_ARTIFACT_PATHS)

    def test_mode_enum_is_cross_worktree_only(self) -> None:
        schema = load_schema("aggregation.schema.json")
        self.assertEqual(schema["properties"]["mode"]["enum"], ["cross_worktree"])

    def test_sources_item_requires_five_fields(self) -> None:
        schema = load_schema("aggregation.schema.json")
        item = schema["$defs"]["source"]
        self.assertEqual(
            sorted(item["required"]),
            sorted(
                [
                    "source_id",
                    "root_label",
                    "base_commit",
                    "head_commit",
                    "namespaced_prefix",
                ]
            ),
        )

    def test_source_def_identical_in_standalone_and_proof_pack(self) -> None:
        agg_schema = load_schema("aggregation.schema.json")
        proof_schema = load_schema("proof-pack.schema.json")
        self.assertEqual(
            agg_schema["$defs"]["source"],
            proof_schema["$defs"]["aggregation"]["properties"]["sources"]["items"],
        )

    def test_namespaced_prefix_accepts_wt_wrapped_source_id(self) -> None:
        schema = load_schema("aggregation.schema.json")
        pattern = schema["$defs"]["source"]["properties"]["namespaced_prefix"]["pattern"]
        source_id = "a1b2c3d4"
        self.assertIsNotNone(SOURCE_ID_RE.fullmatch(source_id))
        self.assertIsNotNone(re.fullmatch(pattern, f"WT{source_id}-"))

    def test_proof_pack_documents_optional_aggregation_property(self) -> None:
        schema = load_schema("proof-pack.schema.json")
        self.assertIn("aggregation", schema["properties"])
        self.assertNotIn("aggregation", schema["required"])

    def test_embedded_aggregation_def_requires_schema_version(self) -> None:
        # #112 whole-branch review: build_proof embeds .agent/aggregation.json
        # verbatim, so a version-stripped hand-crafted block must fail the
        # pack schema the same way the standalone schema + docs/agent-artifacts.md
        # §9 example require schema_version.
        proof_schema = load_schema("proof-pack.schema.json")
        required = proof_schema["$defs"]["aggregation"]["required"]
        self.assertEqual(
            sorted(required),
            sorted(["mode", "source_count", "sources", "schema_version"]),
        )
        # proof.py's build_proof embed guard (_aggregation_manifest_errors)
        # validates schema_version too, not just a three-key subset.
        build_proof_guard_keys = {"schema_version", "mode", "source_count", "sources"}
        self.assertTrue(build_proof_guard_keys.issubset(set(required)))

    def test_embedded_schema_version_pattern_matches_standalone(self) -> None:
        # The standalone pattern is tied to AGGREGATION_SCHEMA_VERSION by the
        # generic SCHEMA_FILES loop; this ties the proof-pack's embedded def
        # to the standalone schema so the two can't silently diverge.
        agg_schema = load_schema("aggregation.schema.json")
        proof_schema = load_schema("proof-pack.schema.json")
        self.assertEqual(
            proof_schema["$defs"]["aggregation"]["properties"]["schema_version"]["pattern"],
            agg_schema["properties"]["schema_version"]["pattern"],
        )

    def test_schema_version_pattern_is_strict_and_major_neutral(self) -> None:
        agg_schema = load_schema("aggregation.schema.json")
        proof_schema = load_schema("proof-pack.schema.json")
        patterns = (
            agg_schema["properties"]["schema_version"]["pattern"],
            proof_schema["$defs"]["aggregation"]["properties"]["schema_version"]["pattern"],
        )
        self.assertEqual(patterns[0], patterns[1])
        for version in (AGGREGATION_SCHEMA_VERSION, "1.0.0", "12.34.56"):
            with self.subTest(version=version):
                self.assertIsNotNone(re.fullmatch(patterns[0], version))
        for version in ("01.0.0", "1.00.0", "1.0", "1.0.0-rc1"):
            with self.subTest(version=version):
                self.assertIsNone(re.fullmatch(patterns[0], version))


if __name__ == "__main__":
    unittest.main()
