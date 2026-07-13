# V1 schema-freeze audit

Audit baseline: commit `477ab69`, Agentflow 0.4.0, 1,108 tests passing and 3
skipped. Issues [#4](https://github.com/kstruzzieri/agentflow/issues/4),
[#5](https://github.com/kstruzzieri/agentflow/issues/5), and
[#11](https://github.com/kstruzzieri/agentflow/issues/11) define the scope.

## Load-bearing inventory

| Artifact / constant | Current | Published schema pattern | Runtime validation | Writer | Readers | Principal tests/docs |
| --- | --- | --- | --- | --- | --- | --- |
| Plan / `PLAN_SCHEMA_VERSION` | 0.3.0 | `plan-lock`: `^0\.[0-3]\.[0-9]+$` | `validation.validate_plan` uses same-major/lower-or-equal-minor validation | `artifacts.create_initial_artifacts`, `draft_plan`, `lock-plan` | validation, execution coverage, proof building/verification, aggregation | `test_cli`, `test_validation`, `test_schema_contracts`, `docs/agent-workflow.md` |
| Execution contract / `EXECUTION_CONTRACT_SCHEMA_VERSION` | 0.3.0 | `execution-contract`: `^0\.[0-3]\.[0-9]+$` | `execution.validate_execution_contract` currently exact-match | `execution.create_execution_contract` | execution state, receipts, verification, proof, aggregation, doctor | `test_execution_contract`, `test_execution_verification`, `test_schema_contracts` |
| Proof pack / `PROOF_PACK_SCHEMA_VERSION` | 0.10.0 | `proof-pack`: `^0\.[0-9]+\.[0-9]+$` | metadata shape and integrity checks; baseline `verify_proof` has no generic schema-version gate | `proof.build_proof`, `write_proof_metadata` | `verify_proof`, viewer, CI fixture verifier | `test_proof`, `test_cli`, `test_ci_proof_bundle`, `docs/agent-workflow.md` |
| Step events / `STEP_RUNS_SCHEMA_VERSION` | 0.5.0 | `step-runs`: `^0\.[0-5]\.[0-9]+$` | no version gate in baseline ledger readers | `execution.append_step_event` | state projection, verification, proof, aggregation, event stream | `test_execution_state`, `test_execution_verification`, `test_aggregate` |
| Command receipts / `COMMAND_RECEIPTS_SCHEMA_VERSION` | 0.4.0 | `command-receipts`: `^0\.[0-4]\.[0-9]+$` | no version gate in baseline ledger readers | `receipts.record_command*` | verification/replay, proof, aggregation, event stream | `test_receipts`, `test_execution_verification`, `test_aggregate` |
| File receipts / `FILE_RECEIPTS_SCHEMA_VERSION` | 0.4.0 | `file-receipts`: `^0\.[0-4]\.[0-9]+$` | no version gate in baseline ledger readers | `receipts.record_file_change` | drift/step verification, proof, aggregation, event stream | `test_receipts`, `test_execution_verification`, `test_aggregate` |
| Verification runs / `VERIFICATION_RUNS_SCHEMA_VERSION` | 0.4.0 | `verification-runs`: `^0\.[0-4]\.[0-9]+$` | no version gate in baseline ledger readers | `execution_coverage.verify_step`, `verify_run` | proof, aggregation, event stream | `test_execution_verification`, `test_aggregate`, `test_proof` |
| Drift report / `DRIFT_REPORT_SCHEMA_VERSION` | 0.2.2 | `drift-report`: `^0\.[0-2]\.[0-9]+$` | writer emits current version; baseline readers do not gate | `validation.audit_drift` | proof building and verification | `test_validation`, `test_proof`, `test_schema_contracts` |

The audit confirms the original inconsistency: plans use same-major
lower-or-equal-minor compatibility, execution contracts require exact equality,
most ledger readers do not validate versions, and `verify-proof` performs
integrity checks without the generic version gate. The implementation work
centralizes those declared policies without broadening the historical promise
beyond `verify-proof`.

## Auxiliary schemas

Evidence, assumptions, amendments, context receipts, failures, runtime config
and snapshots, workflow contracts and packs, capability receipts, review runs
and manifests, aggregation reports, intake briefs, recommendations, and draft
plans are auxiliary. Their versions continue independently; the load-bearing
1.0 change must not update them by association.

## Defects and blockers

The aggregation JSON Schema and its runtime twin
`_AGGREGATION_SCHEMA_VERSION_RE` both hardcode `^0\.[0-1]\.[0-9]+$`. They
would reject an otherwise-supported 1.0 aggregation manifest. This
schema-affecting defect is tracked separately in
[#14](https://github.com/kstruzzieri/agentflow/issues/14). It is a pre-soak
blocker and is intentionally not hidden in this contract-freeze preparation
change.

No load-bearing constant may become 1.0.0 until every schema-affecting issue
identified by this audit is closed and the soak below completes.

## Mechanical soak gate

The soak begins only when issue #5 records an exact candidate commit and date
after all schema defects are closed. At that commit, add a committed
`docs/schema-freeze-soak.json` containing:

- the candidate commit and start date;
- the 21-day minimum end date;
- the freeze set of load-bearing constants and paths; and
- recorded CI, MCP, workflow-pack, aggregation, and released-pyz workload runs.

The freeze set is:

- the eight constants listed above in `src/agentflow/contracts.py`;
- their eight files in `schemas/`;
- canonical serialization and ingestion code in `artifacts.py`, `execution.py`,
  `receipts.py`, `execution_coverage.py`, `validation.py`, and `proof.py`; and
- schema-contract and compatibility-matrix fixtures/tests.

CI must diff that freeze set from the candidate commit. Any shape, requiredness,
canonical serialization, or load-bearing semantic change makes the check fail
and must reset the candidate commit, start date, and 21-day clock. A version-only
change after an unchanged soak is allowed. This makes a reset a Git fact rather
than a judgment call.
