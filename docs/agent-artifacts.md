# Agent Artifact Policy

Agentflow writes task state, receipts, drift results, and proof metadata under
`.agent/`. Those files are evidence, but they are not all meant to enter git.
The default policy is local-only: keep root `.agent/` in `.gitignore`, build a
proof pack for the operator, and publish only the subset needed for the
workflow.

## Policy Summary

| Workflow | Recommended handling | Use when |
| --- | --- | --- |
| Local-only proof | Keep `.agent/` ignored and local. Share the final summary or selected proof files by hand when needed. | Solo work, doc-only work, low-risk changes, or early dogfooding. |
| CI-uploaded proof | Keep `.agent/` ignored, then upload a reviewed proof bundle as a CI artifact with bounded retention. | Reviewers need downloadable evidence, but the repository should not retain task-local state. |
| PR-attached proof | Attach a reviewed proof bundle to the PR description, a review comment, or a release artifact. Do not blindly attach raw `.agent/`. | A PR needs durable evidence without committing generated state. |
| Committed proof bundle | Commit a deliberate proof root that contains the `.agent/` files needed by `verify-run` and `verify-proof`. | Regulated workflows, release gates, verifier fixtures, or projects that want proof to be part of history. |

This repository uses the local-only default for the active root `.agent/`.
CI verifies a committed fixture under `tests/fixtures/proof-bundle` rather than
committing task-local root artifacts.

## What To Publish

Useful proof artifacts:

- `.agent/plan.lock.json`: task contract, scope, allowed files, and steps.
- `.agent/workflow.contract.json`: selected adaptive workflow pack/profile,
  required capabilities, review depth, validation policy, and proof policy.
- `.agent/proof-pack.md`: human-readable completion proof.
- `.agent/proof-pack.json`: structured proof metadata and source hashes.
- `.agent/drift-report.json`: drift audit result used by the proof pack.
- `.agent/execution.contract.json`: execution policy for runs that used
  `init-execution`.
- `.agent/step-runs.jsonl`, `.agent/file-receipts.jsonl`, and
  `.agent/verification-runs.jsonl`: execution, file-change, and verification
  ledgers.
- `.agent/capability-receipts.jsonl`: evidence that required specialized
  practices (TDD, security review, etc.) were used or knowingly waived.
- `.agent/aggregation.json`: cross-worktree aggregation provenance, present
  only in a canonical root produced by `aggregate-ledgers` (see
  [Cross-Worktree Aggregation Provenance](#cross-worktree-aggregation-provenance)).

For a bundle that must pass `verify-proof`, `.agent/proof-pack.json` is the
manifest. Preserve the relative `.agent/` layout and include every path listed
in its `generated_from` and `files[].path` entries. That set can include
non-ledger sources such as evidence, assumptions, context receipts, failures,
amendments, workflow contracts, capability receipts, runtime snapshots, runtime
config, review runs, drift output, and execution ledgers. If `.agent/command-receipts.jsonl`
references stdout/stderr receipt paths, include those receipt output files too
or do not claim the uploaded bundle is independently re-verifiable.

Review before publishing:

- `.agent/command-receipts.jsonl`: records commands, working directories,
  environment variable names, risk findings, output paths, and stdout/stderr
  hashes. It does not store environment values, but command strings and paths can
  still disclose sensitive details.
- `.agent/receipts/`: captured stdout and stderr. Treat these files as sensitive
  by default. They can contain logs, paths, stack traces, snippets, secrets
  printed by tools, or proprietary output. Publish them only after review or
  redaction.
- `.agent/runtime.config.json` and `.agent/runtime-snapshots.jsonl`: runtime
  names, adapters, readiness checks, commands, and endpoints. Review before
  publishing, especially for private services.
- `.agent/workflow.contract.json`: intended to be provider-agnostic policy
  evidence, but still review it for internal workflow names or rationale before
  publishing.
- `.agent/context-receipts.jsonl`, `.agent/failures.jsonl`, and
  `.agent/review-runs.jsonl`: useful provenance, but they may reveal file paths,
  review state, or failure signatures.
- `.agent/handoffs/`: provider-neutral handoffs can still include task context
  and should be reviewed before sharing.

Derived or transient files such as `.agent/attempts/` and lock sidecars are not
authoritative proof. Do not publish them unless a debugging task needs them.

## Capability Receipts

`.agent/capability-receipts.jsonl` records that required specialized practices
ran, without coupling Agentflow to any provider. Agentflow declares required
capabilities (in the workflow contract's `required_capabilities`) and records
evidence; it never invokes a skill or provider. `provider` is a free string.

A used receipt:

```json
{
  "schema_version": "0.1.0",
  "id": "CAP1",
  "capability": "tdd",
  "status": "used",
  "provider": "manual",
  "reason": "Used red-green-refactor for the parser change.",
  "evidence": ["E1"],
  "recorded_at": "2026-06-27T12:00:00+00:00"
}
```

A waiver omits `provider`:

```json
{
  "schema_version": "0.1.0",
  "id": "CAP2",
  "capability": "frontend-qa",
  "status": "waived",
  "reason": "No frontend files changed.",
  "evidence": [],
  "recorded_at": "2026-06-27T12:00:00+00:00"
}
```

Record rows with `agentflow record-capability` (status `used`, `--provider`
required) and `agentflow waive-capability` (status `waived`, no provider).
`reason` is required for both.

Canonical capability ids are `tdd`, `debugging`, `security-review`,
`frontend-qa`, `review-spec`, `review-quality`, and `strict-verification`. The
list is a recommendation, not an enforced enum. Lower-kebab ids are recommended,
but any non-empty id is accepted so new workflow packs need no Agentflow release.
Ids compare exactly and are never normalized.

`build-proof` compares the contract's required capabilities against the ledger
and emits a `capabilities` block (`required`, `recorded`, `waived`, `missing`)
plus a `required_capabilities_satisfied` check. A missing required capability is
a warning that strict proof (`--strict`, `AGENTFLOW_STRICT=1`, or a recorded
strict floor) promotes to an error. A recorded waiver satisfies strict proof but
stays visible in the `capabilities` block. The block is hash-bound through
`core_sha256`, so post-proof tampering is detected.

Because the `capabilities` block is part of `core_sha256`, a proof bundle built
by an Agentflow version before this block existed re-hashes to a different core
under a newer Agentflow and fails re-verification, even though nothing was
tampered. Any change to the hash-bound `canonical_core` membership bumps
`PROOF_PACK_SCHEMA_VERSION`, so `verify-proof` can tell schema growth from
tampering: on a core mismatch it compares the bundle's recorded `schema_version`
against the current one. When the bundle is older, the finding reads `proof
canonical core checksum mismatch: proof was built by an older schema version
(... < ...); rebuild with current Agentflow to re-verify` instead of the bare
tamper message. The finding is still a hard error -- an older recorded version
cannot prove the bundle is untouched -- so rebuild the proof with the current
version. A mismatch on a current-version bundle keeps the bare checksum-mismatch
message, preserving tamper-evidence.

## Cross-Worktree Aggregation Provenance

`.agent/aggregation.json` is a provenance singleton written only by
`aggregate-ledgers` (design #30/#112); ordinary single-tree Agentflow runs
never produce it. It exists only in a canonical output root produced by a
non-dry-run `aggregate-ledgers` that reached status `ok`; a collision run or
a `--dry-run` writes nothing and leaves the file absent.

```json
{
  "schema_version": "0.1.0",
  "mode": "cross_worktree",
  "source_count": 2,
  "sources": [
    {
      "source_id": "w1",
      "root_label": "a",
      "base_commit": "e0df164eecf4cbaaf112c1dd244ab546b1216326",
      "head_commit": "e0df164eecf4cbaaf112c1dd244ab546b1216326",
      "namespaced_prefix": "WTw1-"
    },
    {
      "source_id": "w2",
      "root_label": "b",
      "base_commit": "e0df164eecf4cbaaf112c1dd244ab546b1216326",
      "head_commit": "e0df164eecf4cbaaf112c1dd244ab546b1216326",
      "namespaced_prefix": "WTw2-"
    }
  ]
}
```

Schema: `schemas/aggregation.schema.json` (`0.1.0`). `build-proof` reads this
file when present, embeds it verbatim as `proof["aggregation"]`, and folds it
into `canonical_core` so it is bound into `core_sha256` alongside the rest of
the hash-bound proof. The file itself is also hashed into the proof manifest
(`generated_from` and `files[]`), so `verify-proof` detects any post-proof
edit to `.agent/aggregation.json` the same way it detects tampering with any
other proof source. The hash-bound `proof["aggregation"]` block (and its
`files`/`generated_from` manifest entry) is the authority: a
`.agent/aggregation.json` file on disk with no matching proof entry proves
nothing, since `verify-proof` only re-hashes files the proof itself declares.
Note `core_sha256` is a self-referential checksum, not a signature: a holder
who strips `proof["aggregation"]` and recomputes the core hash over the
reduced proof passes `verify-proof`, since the check only confirms the proof
is internally consistent, not that it retains any particular block. `verify-proof`
detects *edits* to a declared block, not *removal* of the declaration itself.

`root_label` is operator-supplied free text (whatever the source worktree was
labeled at aggregation time), not a validated or sanitized identifier.
Consumers rendering it (a viewer, a report, a terminal) must treat it as
untrusted input and escape it before display.

## Adaptive Review Depth

The workflow contract's `review_depth` drives a proof-time review policy
(#74). `build-proof` reads `.agent/workflow.contract.json`, maps the depth to a
review-gate floor plus a required-run bit, and joins it with the execution
contract's review policy (strictness only rises). It records the resolved
`required_review_depth`, `review_gate_effective`, and `require_review_run` in
the hash-bound `review.policy` block and emits a `required_review_satisfied`
check comparing the required review evidence against the recorded
`review-runs.jsonl`. A missing required run is a hard `verify-proof` error for
`spec_quality`/`deep` (or a `--strict` error when the floor is `warn`). Because
`review.policy` is part of `core_sha256`, the recorded floor ratchets: deleting
the workflow contract before `verify-proof` cannot drop a recorded run
requirement. See `docs/recommend-workflow.md` for the depth table. Review-run
evidence and capability receipts stay separate: a review run is the recorded
review, capability receipts are practice evidence.

## Allowed Files

When Agentflow writes `.agent/` artifacts in the same worktree, include
`.agent/` in the plan's `allowed_files`. This keeps drift audits honest: proof
artifacts are expected outputs of the run, not unrelated local churn.

For local-only workflows, listing `.agent/` in `allowed_files` is still correct
even though git ignores the directory. For committed or PR-attached proof
workflows, either list `.agent/` or list the exact committed proof-root paths
that are expected to change. Keep source, tests, schemas, and generated proof
state in separate plan steps when the blast radius differs.

Example local-only plan scope:

```json
"allowed_files": [
  ".agent/",
  "docs/agent-artifacts.md",
  "README.md"
]
```

Example committed fixture scope:

```json
"allowed_files": [
  ".agent/",
  "tests/fixtures/proof-bundle/.agent/",
  "tests/test_ci_proof_bundle.py"
]
```

## Local-Only Workflow

Use this for most day-to-day development:

1. Keep `/.agent/` or `.agent/` ignored in the project `.gitignore`.
2. Run the normal Agentflow task loop from the repository root.
3. Include `.agent/` in `allowed_files`.
4. Finish with `verify-run`, `audit-drift`, `build-proof`, and `verify-proof`.
5. Summarize the proof pack in the final response or PR description.
6. Do not commit `.agent/`.

This gives the operator a complete local audit trail without turning every task
into generated repository state.

## PR-Attached Or CI-Uploaded Proof

Use this when reviewers need proof artifacts but the repository should not keep
them in history:

1. Build and verify the proof locally or in CI.
2. Copy only the reviewed proof bundle into a temporary artifact directory.
3. Preserve the relative `.agent/` layout and include `.agent/proof-pack.md`,
   `.agent/proof-pack.json`, every proof source named by
   `.agent/proof-pack.json`, and any referenced receipt output files needed for
   `verify-proof`.
4. Review `.agent/command-receipts.jsonl`, `.agent/runtime*`, review ledgers,
   and receipt output files before upload.
5. Upload with bounded retention or attach to the PR after redaction.

Do not publish raw `.agent/receipts/` by default. If receipt outputs are needed
for re-verification, prefer short retention and access controls.

## Committed Proof Workflow

Use this only when proof is meant to be part of project history:

1. Choose an explicit proof root. It may be the repository root for projects that
   commit active `.agent/`, or a fixture path such as
   `tests/fixtures/proof-bundle`.
2. Remove the relevant proof root from `.gitignore` or add a precise negation.
3. Set CI to verify that root, for example `AGENTFLOW_PROOF_ROOT=.` for active
   root proof or `AGENTFLOW_PROOF_ROOT=tests/fixtures/proof-bundle` for a
   fixture.
4. Include the proof root in `allowed_files` for tasks that refresh it.
5. Review receipt output and runtime files before every commit.

Committed proof bundles are durable evidence. Treat them like test fixtures or
release artifacts: small, intentional, reviewed, and reproducible.

## Golem And Adapter-Backed Runs

Golem-backed workflows should follow the same artifact policy as any other
adapter-backed Agentflow run. The adapter may drive planning, execution,
receipts, or CI upload, but the artifacts remain ordinary Agentflow artifacts.

Recommended defaults:

- Keep the working `.agent/` local or CI-uploaded while iterating.
- Have the adapter expose a reviewed proof bundle, not raw task state, when a PR
  or external audit needs evidence.
- Preserve `command-receipts.jsonl` and `.agent/receipts/` long enough to debug
  failed gates, then apply the retention policy for the workflow.
- Commit proof only when the repository has explicitly chosen committed proof
  bundles and CI verifies the chosen `AGENTFLOW_PROOF_ROOT`.
- Keep Golem-specific setup in adapter documentation. Agentflow core remains
  front-end and provider agnostic.

See [docs/golem-integration.md](golem-integration.md) for the full
integration contract; it links back here for artifact retention and
publication rules.
