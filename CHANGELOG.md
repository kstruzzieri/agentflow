# Changelog

All notable changes to Agentflow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `agentflow simulate-risk` shows which recorded commands a stricter
  `risk_policy` or a proposed plan would have stopped, without changing
  anything. It classifies every command receipt again under the proposed plan
  (`--plan`, default the locked plan) and evaluates it under the proposed
  policy (`--risk-policy`, default the contract's) with no confirmation. It
  lists the commands that would newly be blocked or need confirmation, grouped
  by finding category, marks commands Agentflow did not start as advisory, and
  reports analyzer drift: receipts whose recorded risk differs from today's
  classification under the current plan. `--json` prints the report;
  `docs/cli-contract.json` lists the command and its output. It writes nothing
  to `.agent/` and exits 0 unless the plan or contract cannot be read. (#113)

### Fixed

- `verify-proof` now judges every hashed source by the bytes it hashed. Since
  #117 the policy-floor check did, but the checks after it read the plan, the
  evidence, the execution contract and the step-runs, command-receipts and
  review-runs ledgers from disk again: criterion, design-decision and
  validation-gate coverage, receipt outputs, review integrity and `--replay`.
  On a root still writable during verification, a file swapped after its hash
  check was the one they judged, so a tampered bundle could pass. They now read
  the bytes that passed the hash check, a hashed file deleted after it counts
  as present, and a source that failed its hash is reported instead of read
  again. The contract `verify-proof` reads when it starts no longer decides
  either. Output is unchanged for a bundle whose files do not change during
  verification. No schema, CLI, `docs/cli-contract.json` or exit-code change.
  (#126)

## [1.4.0] - 2026-11-10

Agentflow 1.4.0 lets whoever verifies a proof, not the agent that built it,
decide how strict it must be. A verifier can require a minimum policy, check
that the work ran under that policy, and see how each validation gate was
satisfied. Replaying a recorded gate is now screened like any other command.

The proof now shows, gate by gate, how each validation gate was satisfied, and
the docs define what each command-receipt provenance value means (#110). The
proof-pack schema moves to 1.1.0, the first additive minor bump of a
load-bearing schema since 1.0.

**Upgrade note: Agentflow 1.3 and older reject a 1.1.0 proof** with "newer
schema than supported ... upgrade Agentflow", and their `build-proof` refuses to
replace one. Build and verify proofs with the same release. Proofs built by
earlier releases verify unchanged. #110 leaves CLI flags, exit codes, JSON
output and `docs/cli-contract.json` unchanged.

`verify-proof` can now hold a proof to a minimum policy that the verifier
chooses, instead of accepting whatever the agent's own execution contract says
(#111). Without the new flag, every proof verifies exactly as before.

A floor on `risk_policy`, `lease_policy` or `hunk_attribution` now checks the
contract the work ran under, not only the one `build-proof` hashed (#117).
Each record written under a runtime policy names the contract it was decided
under, and the five ledger schemas that carry the name move to 1.1.0.

**Upgrade note: Agentflow 1.3 and older reject 1.1.0 step-run, command-receipt,
file-receipt, verification-run and drift-report records** in working state.
Finish a run with the release that started it. This release reads ledgers
that mix 1.0 and 1.1 records.

Replaying an attested gate now applies the risk policy and command timeout
that `run` applies, so `replay-gates --record` can no longer turn a command
the policy would block into a trusted receipt (#119).

### Added

- `verify-proof --require KEY=MIN`, repeatable, fails a proof whose execution
  contract sets `KEY` below `MIN`. Keys and their order, weakest first:
  `hunk_attribution` (`off`, `observe`, `enforce`), `review_gate` (`ignore`,
  `warn`, `block`), `risk_policy` (`warn`, `require-confirmation`, `block`),
  `lease_policy` (`advisory`, `enforce`), and the `proof_policy` booleans
  `strict_by_default`, `require_command_receipts_for_validation`,
  `require_file_receipts_for_changed_files`,
  `require_managed_receipts_for_validation`,
  `require_evidence_for_inspection_gates` and `require_review_run` (`false`,
  `true`). A missing optional key counts as the value Agentflow enforces
  without it. A miss is reported as `policy_below_floor: ...` and exits 1; a
  malformed `--require` exits 2. The check reads the contract only after its
  hash and the proof core verify, so a contract edited after `build-proof`
  fails as tampering, not as below the floor. A proof with no execution
  contract fails any floor. The floor compares each setting as written:
  `strict_by_default` and a workflow `review_depth` do not count toward
  another key's floor. The floor is never written into the proof, and the
  proof-pack schema is unchanged. Known limit: receipt requirements can be
  tightened after a step completes without failing the matching floor: an
  attested receipt can produce a failed gate under
  `require_managed_receipts_for_validation=true` while the proof still
  verifies. See `docs/security-model.md` for this limitation. (#111)
- The MCP `verify_proof` tool takes an optional `require` array of `KEY=MIN`
  strings and passes each one as `--require`. `docs/cli-contract.json` lists
  the new flag. (#111)
- Step-run events, command receipts (blocked, run, timed out, attested and
  reconstructed), file receipts, verification runs and the drift report that
  `audit-drift` writes record `execution_contract_sha256`, the SHA-256 of the
  execution contract bytes the command read its policy from. Each command
  reads the contract once, when it starts, so a command started by `run` or a
  gate replayed by `replay-gates` that rewrites the contract leaves its records
  naming the contract they ran under. The
  step-runs, command-receipts, file-receipts, verification-runs and
  drift-report schemas move to 1.1.0 (additive), and the `--json` output of
  `claim-step`, `run`, `record-file-change`, `verify-run` and the other
  commands that print these records carries the field.
  `docs/cli-contract.json` lists it. (#117)
- `verify-proof --require` on `risk_policy`, `lease_policy` or
  `hunk_attribution` also fails with `policy_below_floor` unless every record
  in the hashed step-runs, command-receipts, file-receipts and
  verification-runs ledgers, and a hashed drift report written by
  `audit-drift`, carries the verified contract's hash. A ledger or drift
  report missing from the proof fails such a floor. A contract changed mid-run
  fails it even if the change is reverted, and records written before this
  change carry no pin and fail it too. Floors on other
  keys, and `verify-proof` without `--require`, are unchanged. (#117)

- `build-proof` writes `coverage.validation_gates` when an execution contract
  exists: one entry per gate of each step's latest completed attempt, with
  `step_id`, `attempt_id`, `gate`, `kind` (`command`, `inspection` or
  `legacy`), `status` (`satisfied`, `missing` or `failed`), the `receipt_id`
  and its `provenance`, or the `evidence_id` that met the gate. It uses
  `verify-step`'s own rules under the execution contract's policy. The field
  is inside the hashed proof core, and `verify-proof` recomputes it from the
  hashed ledgers, so an edited or removed entry fails with "proof validation
  gate coverage is stale or tampered". (#110)
- After a proof verifies, `verify-proof` prints one human line before
  `proof verified`, for example `validation gates: 1 run by agentflow, 1
  self-reported, 2 inspection evidence`. `view-proof --html` shows the same line
  above a per-gate table. `finish-run` output is unchanged. (#110)
- `docs/agent-workflow.md` documents the four provenance values. `observed`
  (written by `run`), `attested` (self-reported, written by `record-command`),
  `reconstructed` (written by `replay-gates --record`), and `managed`, which
  is reserved for an outside runner such as CI and never written by Agentflow
  1.x. The table says which ones strict mode accepts. It also documents that
  provenance is not a signature. (#110)
- A `current-1.1` compatibility fixture covers every way a gate can be
  satisfied. (#110)

### Changed

- `PROOF_PACK_SCHEMA_VERSION` is 1.1.0, and `schemas/proof-pack.schema.json`
  accepts `1.1.x` and documents `coverage.validation_gates`. (#110)
- The CI schema guard (`scripts/check_schema_soak.py`) accepts an additive
  minor bump of a load-bearing schema after 1.0. The schema-version constant
  must rise within its major, and the published schema may only gain members
  under `properties` or `$defs`. Its version pattern must still accept every
  version it accepted before. Any other change to a released schema still
  fails CI. (#110)
- `aggregate-ledgers` writes the step-runs, command-receipts, file-receipts
  and verification-runs ledgers to the canonical root even when they are
  empty, as `init-execution` does, so a runtime-policy floor can tell a
  removed ledger from an empty one. (#117)
- Every replay of an attested gate (`replay-gates`, `verify-step --replay`,
  `finish-step --replay`, `verify-proof --replay` and the MCP `replay` option)
  classifies the command under the current plan and applies
  `command_policy.risk_policy` as `run` does. Replay has no confirmation
  input, so under `block` or `require-confirmation` a high-risk gate is not
  executed, `replay-gates --record` writes no receipt for it, and the replay
  fails with `replay blocked by risk_policy ...`. Replay also applies the
  timeout `run` uses (`command_timeout_seconds`, or the gate's
  `timeout_seconds`); a replay that runs past it fails with `replay timed out`
  and records nothing. Under `warn`, or a contract without `risk_policy`, a
  replay that finishes in time behaves as before. Upgrade impact:
  `verify-proof --replay` fails for a proof whose contract blocks one of its
  high-risk attested gates; verify it without `--replay`. (#119)

## [1.3.0] - 2026-11-04

Agentflow 1.3.0 stops an edit from a stopped attempt from passing unverified. A
replacement attempt can no longer finish a step on a file that a blocked,
failed or abandoned attempt last recorded (#102). `verify-run`, `finish-run`
and the stop gate now fail in every mode when such an edit is still in the
tree (#103).

**Upgrade note for adapters and stop-hook users: a run that passed on a stopped
attempt's unverified edit now fails.** It keeps failing until a replacement
attempt completes. `next-action` reports `step_amendment_needed`, and its
`amend` action is `automatic: false` when this is the only cause, so an adapter
must surface it to an operator instead of amending unattended. No new command,
flag, JSON key, state, artifact schema, schema-version constant or
`docs/cli-contract.json` value, and exit codes keep their meaning.

### Changed

- `verify-step`, `finish-step` and `complete-step` now refuse an attempt
  (exit 1) when a changed file in its step's scope was last recorded by
  another attempt that has not completed: a blocked, failed or abandoned
  attempt it replaced, or another step's open or stopped attempt on a shared
  file. Recording the file in the current attempt clears it. Until now these
  commands checked only the receipts the attempt wrote, so a replacement could
  finish on a stopped attempt's edit without recording it, although
  `next-action` asks for that record. The finding is an error even without
  `--strict`. The attempt's own receipts and any completed attempt's receipts
  still count. No new command, flag, JSON key, state or schema, and exit codes
  keep their meaning. (#102)
- Git status parsing preserves spaces, non-ASCII characters and literal ` -> `
  in filenames, so receipt ownership checks cannot skip these files. Drift
  auditing and file receipts use the same NUL-delimited parser, including
  rename source paths. (#102)
- `verify-run`, `finish-run` and the stop gate now fail in every mode (exit 1)
  when a changed file's latest receipt matches the working tree but a blocked,
  failed or abandoned attempt wrote it, and the file does not match its latest
  receipt from a completed attempt. Until now a non-strict run could report
  complete on an edit no attempt had verified. **Adapters and stop-hook users:
  a run that passed this way now fails until a replacement attempt completes.**
  `next-action` reports `step_amendment_needed` for the step on the receipt,
  with `amend-step <step> --reason 'replace <status> attempt <id>: unverified
  edit to <path>'`. That `amend` action is `automatic: false` unless a timeout,
  failed or strict cause also applies. A blocked amendment that left no edit
  still passes without `--strict`. No new command, flag, JSON key, state or
  schema, and exit codes keep their meaning. (#103)

## [1.2.2] - 2026-11-03

Agentflow 1.2.2 restores fail-closed resume in `next-action`. In 1.2.1, a
blocked or failed attempt's file receipt covered the attempt that replaced it,
so a driver that refuses to run gates while `file_receipts_missing` stands
could finish a step on the stopped attempt's partial edit. Users of 1.2.1
should upgrade. No state, recovery action, artifact schema, schema-version
constant, `docs/cli-contract.json` value or exit-code change.

### Fixed

- `next-action` again asks an attempt that replaces a blocked or failed one to
  record that attempt's edits before its gates run. Since 1.2.1 (#97), any
  matching receipt counted, so the stopped attempt's receipt covered its
  replacement, which went straight to `validation_missing`. That broke
  fail-closed resume for a driver that refuses to run gates while
  `file_receipts_missing` stands. A changed file's latest receipt now counts
  only when the open attempt wrote it or the attempt that wrote it completed:
  another completed step's receipt and an amendment's earlier completed
  receipt still count. `verify-step`, `finish-step` and `verify-run` are
  unchanged. No state, recovery action, schema or exit-code change. (#100)

## [1.2.1] - 2026-11-02

Agentflow 1.2.1 fixes three cases where `next-action` sent an agent the wrong
way. It no longer asks a step to record a file again when the file already has
a matching receipt from another step or attempt (#97). It guides an edit made
after a step completed to `amend-step` instead of a `finish-run` that always
fails (#73). It reports a ledger change only when a fresh read of the ledgers
shows one, so a deterministic bug no longer looks like a race (#94). No state,
recovery action, artifact schema, schema-version constant,
`docs/cli-contract.json` value or exit-code change.

### Fixed

- `next-action` no longer asks a step to record a changed file that already has
  a matching receipt. A file in the step's scope counts as recorded when its
  latest file receipt, from any step or attempt, matches the file as it is now
  (a deleted file needs a deleted receipt), the check `verify-run` makes. It
  used to require a receipt from the open attempt itself. When two steps
  listed the same file, following it copied the first step's hunks into a new
  receipt and made the second step the file's latest owner in the proof, and
  an amendment had to record again files its step had already recorded. A
  file the open attempt recorded and then edited again is now
  `file_receipts_missing`; `next-action` used to send it to `finish-step`, and
  `finish-run` then failed. Enforced hunk attribution also requires the
  current hunks to be recorded, even when the content hash still matches
  after a staged rename or file type change. Working-tree inspection errors
  return blocking `state_invalid` diagnostics instead of a traceback. No
  state, recovery action, schema or exit-code change. (#97)
- `next-action` guides an edit made after a step completed to `amend-step`
  instead of a `finish-run` that always fails. When every changed file with no
  matching receipt is in exactly one step's effective scope and drift reports
  no out-of-scope or blocked file, it reports `step_amendment_needed` with
  `amend-step <step> --reason 'record post-completion edit to <path>'`. The
  `amend` action is `automatic: false` while any changed file lacks a matching
  receipt, for timeout, failed and strict amendments too, because receipts
  cannot show who made the edit. Otherwise `drift_failing` and `run_unverified`
  name each path and the step that could record it. `drift_failing` now lists
  blocked files and unmapped hunks (path, header and fingerprint) as well as
  out-of-scope files, and its reason names the categories present; it used to
  say "changes fall outside plan scope" with no diagnostics for an unmapped
  hunk. `record-file-change`, `run` and `verify-step` on a completed step with
  no open attempt say to open one with `amend-step` instead of `claim-step`. A
  git or file error while inspecting changed files reports `state_invalid`. No
  state, recovery action, schema or exit-code change. (#73)
- `next-action` reports "execution ledgers changed during next-action; run it
  again" only when the ledgers really changed. When a check after the
  validated snapshot raises, `next-action` reads the step, command, file
  receipt and verification ledgers again. It reports `state_invalid` only if
  they differ from the snapshot or cannot be read; otherwise the original
  error propagates with its traceback. It used to report any `AttributeError`,
  `KeyError`, `TypeError`, `OSError` or `ValueError` raised while choosing the
  action as a ledger change, on the claim path too, so a deterministic bug
  looked like a race and the rerun it asked for gave the same answer every
  time. (#94)

## [1.2.0] - 2026-10-29

Agentflow 1.2.0 shows why a run is stuck. `next-step --explain` reports why
no step is eligible (#71). `next-action` reports the blockers recorded with
`block-step` and `fail-step`, and the stop hook prints the current ones when it
refuses a stop (#72). `next-action` now answers only from the ledger rows it
validated, and agent-written ledger text reaches the terminal with
non-printable characters shown as `?`. Every change is additive under
`docs/stability.md`: a new flag, a new `next-action --json` key, new
`value_sets` codes in `docs/cli-contract.json` and an optional MCP input. No
artifact schema or schema-version constant changes.

### Added

- `next-action --json` reports `recorded_blockers`. It has one entry per
  attempt closed by `block-step` or `fail-step`, in ledger order, with
  `step_id`, `attempt_id`, `event` (`blocked` or `failed`), `reason`,
  `recorded_at`, `attempt_owner` and `superseded_by` (the first later attempt
  of the same step, or `null` for a current blocker). It is derived from the
  validated step ledger and absent for `uninitialized`, `plan_unlocked`,
  `execution_uninitialized` and `state_invalid`. `state`, `blocking` and the
  recovery actions are unchanged. The MCP `next_action` tool carries the field.
  Human `next-action` output adds one `recorded blocker:` line per current
  blocker, with control characters shown as `?` and the reason cut at 200
  characters. (#72)

- `next-step --explain` reports why no step is eligible. With `--json` it
  prints `{step, reason_code, diagnostics}`, where `reason_code` is
  `eligible`, `dependencies_unmet`, `open_attempts` or `steps_complete`.
  `steps_complete` requires that no attempt is open anywhere, amendments
  included; it does not mean the run verifies (use `next-action`). With
  `--explain`, an invalid plan, contract or ledger exits 1 instead of looking
  like a finished run. Plain `next-step` and `next-step --json` are
  unchanged. `docs/cli-contract.json` lists the codes in `value_sets`. MCP
  `next_step` takes an optional `explain` input. (#71)

### Changed

- `hooks/stop-gate.sh` prints `agentflow next-action` (the next action and any
  current recorded blocker) and a fixed line pointing to operator interruption
  to stderr when it refuses a stop. Stdout and exit codes are unchanged, and a
  recorded blocker still does not satisfy the gate. (#72)

### Fixed

- `next-action` reduces step state and selects the eligible step from the
  `.agent/step-runs.jsonl` rows it validates and derives `recorded_blockers`
  from. It used to read the ledger again for both, so a row appended during
  the call could change the step state or the step offered to claim without
  passing validation. (#72 follow-up)

- `next-action` answers from that validated snapshot or not at all. The
  amend, in-flight, run, drift and proof checks read `.agent/` again, so when
  the step, command or file receipt ledgers no longer equal the validated read
  it now reports `state_invalid` ("execution ledgers changed during
  next-action; run it again") instead of an answer steered by a row that never
  passed validation. Malformed or unreadable ledgers encountered by the
  downstream checks produce the same diagnostic. A claim offer comes from the
  snapshot alone and is unaffected. (#72 follow-up)

- Standard output of `events`, `next-action`, `next-step --explain`,
  `finish-step`, `finish-run`, `verify-step`, `detect-stuck` and
  `replay-gates` shows non-printable characters in agent-written ledger text
  (reasons, agent, attempt and receipt ids, recorded argv, recorded paths) as
  `?`, as the `recorded blocker:` lines already did, so ANSI escapes, line
  breaks and bidi overrides written to `.agent/` no longer reach the terminal
  through these commands. `events` cuts a reason at 200 characters. `--json`
  and `--jsonl` output is unchanged. The next entry covers the remaining
  human output and error messages. (#71 follow-up)

- Human output of `audit-drift`, `verify-run`, `build-proof`, `verify-proof`,
  `status`, `doctor`, `runtime-status` and `lint-handoff`, the acknowledgement
  lines of commands that write `.agent/` ledgers, and error messages on stderr
  also show non-printable characters as `?`. So does that text where it is
  captured: `finish-run --json` `diagnostics`, and MCP `content` text,
  `structuredContent.stdout` and `structuredContent.stderr` (the `status`,
  `audit_drift`, `build_proof` and `verify_proof` tools return human output,
  and a failing tool returns its stderr), including the error text the MCP
  server builds for an unexpected exception. The rendering is deliberately
  lossy: tab, NBSP and ZWJ also become `?`, and a multi-line message such as
  git's own stderr prints as one line. Gate output is sanitized before
  `finish-run` splits it into lines, so a recorded line break no longer adds
  `diagnostics` entries. Captured human text has no byte-stability guarantee.
  JSON keys and types, raw values in other `--json` output and in `.agent/`
  files, flags and exit codes are unchanged. (#71 follow-up)

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

[Unreleased]: https://github.com/kstruzzieri/agentflow/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/kstruzzieri/agentflow/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/kstruzzieri/agentflow/compare/v1.2.2...v1.3.0
[1.2.2]: https://github.com/kstruzzieri/agentflow/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/kstruzzieri/agentflow/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/kstruzzieri/agentflow/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/kstruzzieri/agentflow/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/kstruzzieri/agentflow/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/kstruzzieri/agentflow/releases/tag/v1.0.0
[0.4.0]: https://github.com/kstruzzieri/agentflow/releases/tag/v0.4.0
