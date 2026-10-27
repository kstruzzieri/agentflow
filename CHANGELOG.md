# Changelog

All notable changes to Agentflow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.1] - 2026-10-26

Agentflow 1.1.1 closes two gaps through which a step could complete, and a
proof issue, over a gate whose last run failed. `verify-step` judges a
structured command gate by its latest receipt in the attempt instead of its
first (#77), and `complete-step` re-runs that judgment before it closes an
attempt, as `finish-step` already did (#81). A failed gate run followed by an
identical passing one now verifies in the same attempt. No flag, artifact
schema, schema-version constant or `docs/cli-contract.json` change.

### Fixed

- `verify-step` judges a structured command gate (`{"kind": "command", "run":
  [...]}`) by the latest receipt in the attempt with the gate's exact argv. It
  used the first one; legacy `step.validation` gates and requirement coverage
  already used the latest. The verdict moves in both directions. It relaxes: a
  failed first run followed by an identical passing run in the same attempt now
  verifies, and `next-action` recommends `finish-step` instead of asking for the
  same run again. It tightens: an identical failing or timed-out run after a
  passing one now fails `verify-step` instead of being masked, so an attempt
  that verified only because its later failure was ignored fails if
  `verify-step` runs on it again. `finish-step`, `next-action` and the MCP
  `verify_step` tool follow, since they use the same check, and the per-gate
  `receipt_id` in the `next-action --json` resumability projection names the
  later receipt. `complete-step` and `finish-step` still refuse an attempt that
  holds a timed-out receipt anywhere in it. No receipt, artifact schema or
  schema-version constant changes. (#77)
- `complete-step` re-runs the `verify-step` judgment (gates on their latest
  receipts, inspection evidence and file scope) before it closes the attempt, as
  `finish-step` already did. Previously it only checked that `verify-step` had
  passed at some point, so a gate that failed after `verify-step` still
  completed the step, and `verify-run`, `build-proof` and `verify-proof` then
  passed over a failing last run. It now refuses (exit 1, nothing written) with
  `<step> attempt <id> no longer passes verify-step: ...`; rerun the gate to a
  pass, then complete. It reads `.agent/plan.lock.json` for this check and
  refuses (exit 1, `cannot re-check <step> before completing: ...`) when the
  plan is missing, unreadable or not a JSON object. Its other refusals keep
  their messages and order: no or unopened attempt, closed attempt, a command
  still running, a timed-out receipt, no `verify-step` yet, not the owner. The
  MCP `complete_step` tool follows. No flag, artifact schema or schema-version
  constant changes. (#81)

## [1.1.0] - 2026-10-18

Agentflow 1.1.0 closes integrity and recovery gaps in the execution ledger and
the proof chain. Step-ledger writers refuse writes that used to leave
`.agent/step-runs.jsonl` unreadable (#61, #62); `verify-run` and `build-proof`
refuse an unfinished run (#58); a timed-out gate can be recovered with a new
attempt, and `next-action` now leads there instead of into a dead end (#60,
#70); and rebuilding a proof keeps the review policy floor of the proof it
replaces (#66). No artifact schema or schema-version constant changes;
`docs/cli-contract.json` moves to 1.1.0 for its new `value_sets` section.
Integrations that follow `next-action` should handle the new
`attempt_timed_out` and `step_amendment_needed` states and the `block` and
`amend` recovery actions, and fail closed on values they do not recognize.

### Added

- MCP `block_step` tool, so an MCP-only adapter can take the `block` recovery
  action. (#70)

### Changed

- `scripts/check_schema_soak.py` no longer freezes source, tests and workflows
  after the recorded 1.0 transition. It audits the transition commit itself and
  holds the working tree only to the released contract: the eight load-bearing
  schemas, their schema-version constant values, and the existing compatibility
  fixtures (including later additions, pinned when first committed). Previously
  any change to a file from the original freeze set failed
  CI. (#64)
- `verify-run` no longer fails on a timed-out command receipt that a later
  attempt superseded: the receipt's attempt ended `blocked`, `failed` or
  `abandoned`, and a later attempt of the same step, by ledger order, opened and
  completed. It reports a `superseded_timeout` warning naming the receipt and
  both attempts instead, in both modes, and strict `next-action` treats that
  warning as advisory. Every other retained timeout still fails the run,
  including one inside the attempt that completed. The proof still counts
  superseded timeouts in `execution.command_timed_out`. (#60)
- With `--agent`, `next-action`'s in-flight states (`file_receipts_missing`,
  `validation_missing`, `step_unverified`, `step_uncompleted`) omit
  `command`/`args` and add the `continue` denial reason to `diagnostics` when
  the `continue` recovery action is denied (a foreign caller, or an expired
  lease under `lease_policy: enforce`). `docs/cli-contract.json` moves to
  `schema_version` 1.1.0 and gains a `value_sets` section listing the
  `next-action` states and recovery actions. Minor releases may add values to
  those sets, and consumers must treat an unknown value as not actionable (fail
  closed). Explicit agent identities, including literal `$USER`, are shell-quoted
  in printed commands; only an omitted identity uses the expanding default
  `$USER` placeholder. Leading-hyphen identities use `--agent=<value>` in
  generated commands and MCP adapters so argparse preserves them. (#70)

### Fixed

- `block-step`, `complete-step`, `fail-step`, `verify-step`, `finish-step`,
  `renew-lease` and `run`'s automatic lease renewal refuse an attempt whose
  latest event is already terminal (`completed`, `blocked`, `failed` or
  `abandoned`) and append nothing to `.agent/step-runs.jsonl`. Previously the
  append left the ledger unreadable (`execution_state_invalid`). The check runs
  under the step-ledger lock, so concurrent writers cannot race it. The refusal
  exits 2. `complete-step` on an attempt that was already closed when it started
  keeps its existing exit 1 and now names the closed state instead of asking for
  `verify-step`. (#61)
- `claim-step`, `amend-step` and `reclaim-step` no longer reuse an attempt id
  that a standalone `fail-step` already used, which used to leave the ledger
  unreadable. (#61)
- `fail-step` refuses an `--attempt` that already belongs to another step and
  appends nothing to `.agent/step-runs.jsonl`. Previously one mistyped step id
  recorded the attempt under two steps and left the ledger unreadable
  (`execution_state_invalid`). The check runs under the step-ledger lock for
  every step-ledger writer; the refusal exits 2. `fail-step` on an attempt id
  that no step has used stays allowed. (#62)
- `verify-run` fails in both modes while a plan step has an open attempt or has
  never completed. When an execution contract exists, `build-proof` also refuses
  to build a proof over such a run (`invalid working state: execution ledger:`,
  exit 1, nothing written, an existing proof kept); without one, the
  evidence-only flow is unchanged. A step-ledger change during proof construction
  also refuses the build and asks the caller to retry, so a concurrent amendment
  cannot bypass the completeness check. Previously non-strict `finish-run` passed and
  wrote a proof that `verify-proof` accepted with `execution.steps_completed`
  below `steps_total`. Strict mode still also requires each step's latest attempt
  to be completed; a completed step whose later amendment was blocked still
  passes non-strict. Proofs issued by earlier releases still verify. A blocker
  recorded on a step that never completed no longer satisfies the Stop hook
  (`docs/stop-hook.md`), and new proofs always carry empty
  `coverage.expired_leases` and `coverage.no_deadline_open_attempts`. (#58)
- `complete-step` and `finish-step` refuse an attempt that holds a timed-out
  command receipt (exit 1, nothing written). Both also refuse while a command
  is running; a shared attempt lock covers command startup through receipt
  append and verification through completion. `block-step`, `fail-step`, and
  `reclaim-step` also refuse active commands, so recovery cannot strand the run
  with a receipt written after its attempt closed. `next-action` reports
  `attempt_timed_out` with a close the caller may take (`block-step`, or
  `reclaim-step` after lease expiry). It reports `step_amendment_needed` with
  `amend-step` when a completed step needs a replacement attempt: an
  unsuperseded recoverable timeout, a failed latest attempt, or strict
  incompletion. Previously it led to completing the timed-out attempt
  (permanently unprovable) or looped on `finish-run`. `resumability`
  `recovery_actions` gains `block` and `amend`. Generated commands use the
  `--agent` passed to `next-action` and keep `--strict`. Runs already holding
  a completed timed-out attempt stay unrecoverable; archive `.agent/` and
  start a new run. (#70)
- `build-proof`, and so `finish-run`, no longer drops the review policy floor
  recorded in the proof it replaces. A rebuild joins the existing
  `.agent/proof-pack.json` `review.policy` (gate, strictness, required run and
  required depth) into the new proof's policy. Dropping `--strict` or
  `AGENTFLOW_STRICT`, switching off `strict_by_default`, or lowering or deleting
  a contract and then rebuilding keeps the floor. `build-proof` holds a
  proof-build lock from reading that floor until it publishes. It replaces the
  Markdown and then the JSON, each atomically, prints `review policy floor kept
  from existing proof: ...` when the floor raised a field, and exits by the
  strictness it records, listing the warning checks that failed a strict proof.
  Newly failing cases:
  - a rebuild whose run does not meet the inherited floor (exit 1);
  - an existing proof that is unreadable, has a damaged `review.policy`, or has
    a malformed or newer `schema_version` (exit 1, nothing written);
  - a `strict_by_default` contract with warning checks, which used to exit 0
    (exit 1);
  - `build-proof --output` or `view-proof --html --output` naming
    `.agent/proof-pack.json`, or `view-proof`'s own `--proof` file (exit 2,
    nothing written);
  - an `OSError` while publishing, which used to raise a traceback (exit 1,
    `could not publish proof: ...`, the prior proof kept).

  The floor is per root: archiving or deleting the proof, and
  `aggregate-ledgers`, reset it. `verify-proof` is unchanged, and proofs issued
  by earlier releases still verify. (#66)

## [1.0.0] - 2026-10-04

Agentflow 1.0.0 is the stability release. The eight artifact schemas (plan
lock, drift report, proof pack, execution contract, step runs, command
receipts, file receipts, and verification runs) are frozen at `1.0.0` after
the issue #5 soak; the Python distribution is named `agentflow-proof` while
the `agentflow` import package and the `agentflow` and `agentflow-mcp`
commands keep their names; and the promises in `docs/stability.md` and
`docs/compatibility.md` are now in force.

### Added

- `tests/fixtures/compatibility/legacy-0.3/`: a byte-exact snapshot of the
  0.3-era CI proof bundle, now the compatibility matrix's preserved-legacy
  fixture, so the historical `verify-proof` guarantee stays exercised after the
  1.0 transition regenerates the live `tests/fixtures/proof-bundle`.
- Amendment-ready review-manifest v1.0 fields with locked-plan ownership
  validation, durable ledger/proof projection, and HTML proof viewing.
- Authoritative, actor-aware resumability state in `next-action --json` and
  its MCP mapping, including lease evaluation, attempt-scoped receipts and
  gates, structured diagnostics, and explicitly marked break-glass recovery.

### Changed

- The eight artifact schemas are stamped `1.0.0` from the single soaked
  candidate recorded in `docs/schema-freeze-soak.json`; proofs built by
  Agentflow 0.4.0 or later stay verifiable under the surface documented in
  `docs/compatibility.md` (#5).
- Legacy review manifests remain recordable and verifiable but are explicitly
  marked non-amendment-ready. The proof-pack schema and review-run rows moved
  to 0.10.0 and 0.6.0 for this change ahead of the 1.0.0 freeze; the manifest
  schema uses a new major because amendment-ready rows add required repair
  context.

### Fixed

- The 1.0 schema transition is landable again, caught by rehearsing the full
  test suite against a transitioned tree: the published proof-pack
  `bundle_version` pattern now admits the historical `0.4.0+` range and
  `1.0.x` (it is written from the same constant as `schema_version` but sits
  outside the soak guard's pattern exception), and the guard's transition
  window now lets the recorded transition regenerate the live CI proof bundle,
  since `verify-run` carries no cross-major promise. Both changes reset the
  soak candidate; the soak clock restarted on the corrected shape (the window
  is 72 hours; see `docs/schema-freeze-audit.md`).
- `scripts/check_schema_soak.py` now reports an unresolvable
  `candidate_commit` or `transition_commit` with the field, the recorded SHA,
  and the squash-merge cause instead of `fatal: Needed a single revision`
  (#36).
- The `aggregate-ledgers --json` contract now declares the two payload shapes
  the runtime actually emits: analysis/collision (`status`, `sources`,
  `collisions`, `planned`) and successful write (`status`, `sources`,
  `written`). The previously documented single envelope (`source_count`,
  `output`, `dry_run`, `rewrites`) never matched runtime output, so no
  emitted payload changes.

## [0.4.0] - 2026-07-10

### Added

- Read-only runtime and MCP status evidence in proof packs.
- Single-writer step leases with renewal and stale-owner recovery.
- Cross-worktree ledger aggregation with collision detection and provenance.
- A public gate/ledger brand kit for the project and release artifacts.

## [0.3.0] - 2026-07-03

_Released from the pre-public repository history; no tag exists in this
repository, so this heading is intentionally unlinked._

### Added

- Portable execution contracts, step claims, command and file receipts,
  resumable verification, and provider-neutral handoffs.
- Deterministic command-risk screening, the dependency-free MCP server, and
  the POSIX Stop-hook enforcement gate.
- CI proof verification, review manifests, capability receipts, workflow
  packs, workflow recommendation, and draft-plan generation.
- Hunk-level drift attribution, the static HTML proof viewer, and the Golem
  integration guide.
- Single-file CLI and MCP zipapps with checksums and a tag-triggered GitHub
  release workflow.

### Changed

- Existing v0.2 proof artifacts remain valid when no execution contract exists.

[Unreleased]: https://github.com/kstruzzieri/agentflow/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/kstruzzieri/agentflow/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/kstruzzieri/agentflow/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/kstruzzieri/agentflow/releases/tag/v1.0.0
[0.4.0]: https://github.com/kstruzzieri/agentflow/releases/tag/v0.4.0
