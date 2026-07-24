# V1 schema-freeze audit

Delta-audit baseline: commit
`bfa0b82608ec3eb390d1dae944fecf167b320bdd`, Agentflow 0.4.0, 1,379
tests passing and 1 skipped on Python 3.13. Issues
[#4](https://github.com/kstruzzieri/agentflow/issues/4),
[#5](https://github.com/kstruzzieri/agentflow/issues/5), and
[#11](https://github.com/kstruzzieri/agentflow/issues/11) define the scope.

This baseline includes the schema-soak guard from PR #30 and the two pre-soak
blocker fixes from PR #31, on top of the earlier aggregation fix (PR #24),
locked-plan design references (PR #26), and distribution preparation (PR #27).

**Result: GO.** Every schema-affecting defect this audit opened is closed, the
eight published patterns agree with their runtime validators, and the candidate
survives the full suite and all five soak workloads. `bfa0b82` is the recorded
soak candidate.

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

Every schema-affecting defect this audit opened is now closed:

- The aggregation 0.x-only JSON Schema pattern and runtime
  `_AGGREGATION_SCHEMA_VERSION_RE`
  ([#14](https://github.com/kstruzzieri/agentflow/issues/14)) were fixed by
  PR #24; the published aggregation pattern is now full three-part semver and
  accepts a supported 1.0 manifest.
- [`build-proof` accepts schema-invalid working state](https://github.com/kstruzzieri/agentflow/issues/28)
  was fixed by PR #31. `build_proof` now applies the full `validation.validate_plan`
  and `execution.validate_execution_contract` contracts before any proof output,
  and writes nothing when either fails, so the internally impossible
  `steps_total=0` / `steps_completed=2` proof from `3fd6e79` can no longer be
  produced.
- [The plan JSON Schema omits the design-reference version gate](https://github.com/kstruzzieri/agentflow/issues/29)
  was fixed by PR #31. `schemas/plan-lock.schema.json` now forbids
  `design_decisions` and `steps[].design_decision_ids` on `0.0.x`-`0.3.x` plans
  through a conditional whose version pattern is pinned to
  `validation.LEGACY_DESIGN_DECISION_VERSION_PATTERN`, so the published schema
  and runtime agree.

The `bfa0b82` delta audit found no new schema-affecting defect. The eight
published patterns each accept their current constant version, and the two
representations of the design-reference boundary — the schema regex and the
runtime version-tuple comparison — select the same versions.

One non-blocking maintenance discrepancy remains, unchanged from the prior
audit: `STEP_EVENT_KINDS` omits the runtime and schema event
`amendment_started`. That event is emitted by `execution.py`, accepted by the
`step-runs` schema enum, and read by `proof.py`, `review.py`, and
`execution_coverage.py`; `STEP_EVENT_KINDS` itself has no consumer in `src/` or
`tests/`, so the stale constant does not gate, emit, or accept anything. It is
not part of the frozen load-bearing set.

No load-bearing constant may become 1.0.0 until every schema-affecting issue
identified by this audit is closed and the soak below completes. As of this
baseline, the first condition holds.

## Mechanical soak gate

The soak begins only when issue #5 records an exact candidate commit after all
schema defects are closed. A tracking commit adds `docs/schema-freeze-soak.json`
because that file is outside the freeze set. The manifest must contain:

- the candidate commit;
- a `workflow_run_id` — `null` until a trusted main-CI run is observed, then the
  numeric id of that run;
- the eight load-bearing constants exactly as the candidate declares them;
- the freeze set of load-bearing paths; and
- the CI, MCP, workflow-pack, aggregation, and released-pyz workload runs,
  recorded during the soak.

The manifest does **not** declare the clock, and the guard does not trust a
local timestamp for it. `scripts/check_schema_soak.py` starts the soak from a
trusted observation of `main`: the `workflow_run_id` must resolve, through the
GitHub API, to a completed successful **push** run of this repository's
`.github/workflows/ci.yml` on `main` whose head commit contains the manifest's
recording commit. The soak start is that run's `created_at`, and the minimum end
is 21 days later. Anchoring the start to a CI observation rather than a commit
time means shortening the soak would require forging a GitHub Actions run, not
editing a string.

Recording proceeds in two phases. First the manifest lands with
`workflow_run_id` `null` and no workloads; the guard reports the candidate as
*awaiting trusted main-CI observation* and holds the freeze diff without starting
the clock. Once that manifest is on `main` and its own push run has succeeded,
the `workflow_run_id` is set to that run and the clock starts. Each workload is
then recorded with a timestamp no earlier than the trusted start and no later
than the present, so issue #5's requirement to exercise them *during* the soak
is satisfied by appending to the manifest as the runs happen. The guard reports
the soak complete only once the 21 days have elapsed **and** all five workloads
are recorded.

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
  `stuck.py`, `runtime.py`, `porcelain.py`, `viewer.py`, and `handoff.py`; and
- their pinning tests in `tests/test_schema_contracts.py`,
  `tests/test_artifact_versioning.py`, `tests/test_versioning.py`,
  `tests/test_cli.py`, `tests/test_packs.py`, `tests/test_draft_plan.py`,
  `tests/test_execution_contract.py`, `tests/test_execution_state.py`,
  `tests/test_execution_verification.py`, `tests/test_receipts.py`,
  `tests/test_hunks.py`, `tests/test_risk.py`, `tests/test_aggregate.py`,
  `tests/test_proof.py`, `tests/test_review.py`,
  `tests/test_capabilities.py`, `tests/test_workflow_contract.py`,
  `tests/test_events.py`, `tests/test_stuck.py`, `tests/test_runtime.py`,
  `tests/test_porcelain.py`, `tests/test_view_proof.py`,
  `tests/test_handoff.py`, `tests/test_proof_compatibility.py`,
  `tests/test_ci_proof_bundle.py`, `tests/fixtures/compatibility/`, and
  `tests/fixtures/proof-bundle/`.

`runtime.py` is frozen because `proof.runtime_block` folds the recorded runtime
snapshot into the proof canonical core; a reshape there is a load-bearing
change even though `runtime.py` never names a load-bearing constant.

CI must diff that declared freeze set from the candidate commit. Any shape,
requiredness, canonical serialization, or load-bearing semantic change makes
the check fail and must reset the candidate commit, evidence, and 21-day clock.
This makes a reset a Git fact rather than a judgment call.

The one exception is issue #5's version-only change. Once the 21 days have
elapsed, and only then, the guard accepts a `contracts.py` whose sole difference
from the candidate is a strict increase in one or more of the eight load-bearing
constants; the file must be otherwise identical after AST normalization, and
every other frozen path must still match. That is what lets the soaked shape
become the shape assigned 1.0.0 without discarding the soak that earned it.
Before the clock elapses the same edit is rejected, and the guard refuses any
candidate that already declares a 1.0 load-bearing constant.
