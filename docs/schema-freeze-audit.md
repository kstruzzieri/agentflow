# V1 schema-freeze audit

Delta-audit baseline: commit
`3fd6e79c4eb1c763f03c128338d719af344ec8cb`, Agentflow 0.4.0, 1,307
tests passing and 1 skipped on Python 3.13. Issues
[#4](https://github.com/kstruzzieri/agentflow/issues/4),
[#5](https://github.com/kstruzzieri/agentflow/issues/5), and
[#11](https://github.com/kstruzzieri/agentflow/issues/11) define the scope.

This baseline includes the aggregation fix from PR #24, the locked-plan design
reference work from PR #26, and the distribution preparation from PR #27.

## Load-bearing inventory

| Artifact / constant | Current | Published schema pattern | Runtime validation | Producer / persistence path | Material consumers | Principal tests/docs |
| --- | --- | --- | --- | --- | --- | --- |
| Plan / `PLAN_SCHEMA_VERSION` | 0.4.0 | `plan-lock`: `^0\.[0-4]\.[0-9]+$` | `validation.validate_plan` applies the declared same-major/lower-or-equal-minor policy and validates shape | `artifacts.default_plan` -> `create_initial_artifacts`; `packs.template_to_plan` -> CLI pack init; `draft_plan.compile_draft_plan` -> `cli.command_draft_plan`; `cli.command_lock_plan`; `aggregate.write_canonical` | validation; lifecycle and execution coverage; proof and aggregation; review, porcelain, viewer, and handoff projections | `test_cli`, `test_packs`, `test_draft_plan`, `test_artifact_versioning`, `test_schema_contracts`, `docs/agent-workflow.md` |
| Execution contract / `EXECUTION_CONTRACT_SCHEMA_VERSION` | 0.3.0 | `execution-contract`: `^0\.[0-3]\.[0-9]+$` | `execution.validate_execution_contract` applies exact-version validation | `execution.default_execution_contract` -> `init_execution_artifacts`; `aggregate.write_canonical` | execution and doctor; receipt, hunk, drift, and verification policy readers; proof, review, aggregation, and porcelain | `test_execution_contract`, `test_execution_verification`, `test_artifact_versioning`, `test_schema_contracts` |
| Proof pack / `PROOF_PACK_SCHEMA_VERSION` | 0.11.0 | `proof-pack`: `^0\.[0-9]+\.[0-9]+$` | `proof._verify_proof` applies the bounded historical `verify-proof` policy before current-shape and integrity checks | `proof.build_proof` -> `proof.write_proof_metadata` | `proof.verify_proof`; `viewer.collect_view_model`; porcelain, CLI, MCP, and CI delegators | `test_proof`, `test_proof_compatibility`, `test_view_proof`, `test_ci_proof_bundle`, `docs/compatibility.md` |
| Step events / `STEP_RUNS_SCHEMA_VERSION` | 0.5.0 | `step-runs`: `^0\.[0-5]\.[0-9]+$` | canonical `artifacts.read_jsonl` paths apply same-major validation | `execution._append_step_event`; `aggregate.write_canonical` | `execution.read_step_state`; receipts and execution coverage; proof, review, aggregation, event/stuck, porcelain, and viewer projections | `test_execution_state`, `test_execution_verification`, `test_events`, `test_artifact_versioning`, `test_aggregate` |
| Command receipts / `COMMAND_RECEIPTS_SCHEMA_VERSION` | 0.4.0 | `command-receipts`: `^0\.[0-4]\.[0-9]+$` | canonical `artifacts.read_jsonl` paths apply same-major validation | `receipts.run_command`; `receipts.record_command`; `aggregate.write_canonical` | receipt replay/output verification; execution coverage; proof, review, aggregation, event/stuck, porcelain, and viewer projections | `test_receipts`, `test_risk`, `test_execution_verification`, `test_artifact_versioning`, `test_aggregate` |
| File receipts / `FILE_RECEIPTS_SCHEMA_VERSION` | 0.4.0 | `file-receipts`: `^0\.[0-4]\.[0-9]+$` | canonical `artifacts.read_jsonl` paths apply same-major validation | `receipts.record_file_change`; `aggregate.write_canonical` | hunk/drift and execution coverage; proof, review, aggregation, event/stuck, porcelain, and viewer projections | `test_receipts`, `test_hunks`, `test_execution_verification`, `test_artifact_versioning`, `test_aggregate` |
| Verification runs / `VERIFICATION_RUNS_SCHEMA_VERSION` | 0.4.0 | `verification-runs`: `^0\.[0-4]\.[0-9]+$` | canonical `artifacts.read_jsonl` paths apply same-major validation | `execution_coverage.verify_step` and `verify_run` via `_append_verification`; `aggregate.write_canonical` | proof and aggregation; event/stuck and porcelain projections | `test_execution_verification`, `test_events`, `test_artifact_versioning`, `test_aggregate`, `test_proof` |
| Drift report / `DRIFT_REPORT_SCHEMA_VERSION` | 0.2.2 | `drift-report`: `^0\.[0-2]\.[0-9]+$` | canonical `artifacts.read_json` paths apply same-major validation | `artifacts.default_drift_report` -> `create_initial_artifacts`; `validation.audit_drift` -> `cli.command_audit_drift` | execution coverage; proof build/verification; status and viewer projections | `test_cli`, `test_proof`, `test_artifact_versioning`, `test_schema_contracts` |

The shared policy table in `contracts.py` and implementation in `versioning.py`
now make the intended split explicit: execution contracts are exact,
load-bearing working-state artifacts otherwise use same-major compatibility,
and only `verify-proof` carries the bounded historical guarantee documented in
`docs/compatibility.md`.

## Auxiliary schemas

Evidence, assumptions, amendments, context receipts, failures, runtime config
and snapshots, workflow contracts and packs, capability receipts, review runs
and manifests, aggregation reports, intake briefs, recommendations, and draft
plans are auxiliary. Their versions continue independently; the load-bearing
1.0 change must not update them by association.

## Defects and blockers

The aggregation 0.x-only JSON Schema pattern and runtime
`_AGGREGATION_SCHEMA_VERSION_RE` originally tracked in
[#14](https://github.com/kstruzzieri/agentflow/issues/14) were fixed by PR #24.
The delta audit found two current-schema defects:

- [`build-proof` accepts schema-invalid working state](https://github.com/kstruzzieri/agentflow/issues/28).
  At `3fd6e79`, the reproduced plan fails its full validator with ten missing
  required fields and the execution contract fails with two structural errors.
  `build-proof` nevertheless exits successfully and emits proof schema 0.11.0
  with `steps_total=0`, `steps_completed=2`, and no failed checks. The build
  path does not apply either full validator.
- [The plan JSON Schema omits the design-reference version gate](https://github.com/kstruzzieri/agentflow/issues/29).
  The published schema allows `design_decisions` and
  `steps[].design_decision_ids` under 0.3.x, while runtime validation and
  `docs/agent-workflow.md` require 0.4.0 or newer.

Issues #28 and #29 are pre-soak blockers. Commit `3fd6e79` is an audit baseline,
**not** a soak candidate. After both close, issue #5 must delta-audit the
then-current `main` again before recording a candidate, start time, or minimum
end time.

One non-blocking maintenance discrepancy remains: `STEP_EVENT_KINDS` omits the
runtime and schema event `amendment_started`, but that constant currently has no
consumer and does not alter emitted or accepted artifacts.

No load-bearing constant may become 1.0.0 until every schema-affecting issue
identified by this audit is closed and the soak below completes.

## Mechanical soak gate

The soak begins only when issue #5 records an exact candidate commit and UTC
start time after all schema defects are closed. A follow-up tracking commit may
add `docs/schema-freeze-soak.json` because that file is outside the freeze set.
The manifest must contain:

- the candidate commit and UTC start time;
- the minimum end time exactly 21 full days later;
- the freeze set of load-bearing constants and paths; and
- recorded CI, MCP, workflow-pack, aggregation, and released-pyz workload runs.

The freeze set is:

- the eight constants listed above in `src/agentflow/contracts.py`;
- their eight files in `schemas/`;
- the executable guard in `scripts/check_schema_soak.py`, its focused contract
  in `tests/test_schema_soak.py`, and its invocation in
  `.github/workflows/ci.yml`;
- canonical storage and version-policy code in `src/agentflow/artifacts.py` and
  `src/agentflow/versioning.py`;
- load-bearing construction, mutation, validation, and aggregation code in
  `src/agentflow/cli.py`, `packs.py`, `draft_plan.py`, `execution.py`,
  `receipts.py`, `hunks.py`, `risk.py`, `git.py`, `execution_coverage.py`,
  `validation.py`, and `aggregate.py`;
- proof and public projection code in `src/agentflow/proof.py`, `coverage.py`,
  `review.py`, `capabilities.py`, `workflow_contract.py`, `events.py`,
  `stuck.py`, `porcelain.py`, `viewer.py`, and `handoff.py`; and
- their pinning tests in `tests/test_schema_contracts.py`,
  `tests/test_artifact_versioning.py`, `tests/test_cli.py`,
  `tests/test_packs.py`, `tests/test_draft_plan.py`,
  `tests/test_execution_contract.py`, `tests/test_execution_state.py`,
  `tests/test_execution_verification.py`, `tests/test_receipts.py`,
  `tests/test_hunks.py`, `tests/test_risk.py`, `tests/test_aggregate.py`,
  `tests/test_proof.py`, `tests/test_review.py`,
  `tests/test_capabilities.py`, `tests/test_workflow_contract.py`,
  `tests/test_events.py`, `tests/test_stuck.py`, `tests/test_porcelain.py`,
  `tests/test_view_proof.py`, `tests/test_handoff.py`,
  `tests/test_proof_compatibility.py`, `tests/fixtures/compatibility/`, and
  `tests/fixtures/proof-bundle/`.

CI must diff that declared freeze set from the candidate commit. Any shape,
requiredness, canonical serialization, or load-bearing semantic change makes
the check fail and must reset the candidate commit, start time, evidence, and
21-day clock. In issue #5's date-level wording, it must "reset the candidate commit, start date, and 21-day clock."
A version-only change after an unchanged soak is allowed. This makes a reset a
Git fact rather than a judgment call.
