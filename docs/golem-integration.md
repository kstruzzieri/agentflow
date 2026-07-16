# Golem Integration Guide

This guide is for developers implementing the Golem (go-llm) adapter that uses
Agentflow as its durable planning/execution/review proof layer
([go-llm#209](https://github.com/kstruzzieri/go-llm/issues/209)). It documents
the contract from the Agentflow side: which surface to call, in what order,
and where the security boundaries sit.

Agentflow stays LLM- and front-end-agnostic. Golem is one adapter among many
(Claude Code and Codex CLI use the same surface), and nothing in Agentflow
core knows Golem exists. Everything below uses only the public CLI and MCP
surface; if the guide ever disagrees with `agentflow --help` or
[docs/mcp.md](mcp.md), those are authoritative.

## Ownership Split

| Concern | Owner |
| --- | --- |
| LLM planning, reasoning, provider calls | Golem |
| Requirement/criterion authoring, stable IDs, and mappings | Golem |
| Code edits and user interaction | Golem |
| Shell command execution and approval | Golem (host) |
| Locked plan state (`.agent/plan.lock.json`) | Agentflow |
| Traceability validation and deterministic proof projection | Agentflow |
| Step lifecycle (claim, verify, complete, amend) | Agentflow |
| Command/file receipts and hunk attribution | Agentflow |
| Drift audit against plan scope | Agentflow |
| Review-run evidence ledger | Agentflow |
| Proof pack generation and verification | Agentflow |

Two hard rules fall out of this split:

- Agentflow never calls a model. It has no provider configuration and no
  network dependency.
- Golem never writes `.agent/` ledgers directly. All state transitions go
  through Agentflow commands so receipts and hashes stay consistent.

## Prerequisites

Golem should detect Agentflow before starting an Agentflow-backed run and
fail with a clear configuration error otherwise:

```bash
agentflow --version               # is the CLI on PATH?
agentflow --help                  # is the CLI runnable?
```

When the console script is not installed, the supported fallback is
`PYTHONPATH=src python3 -m agentflow` from an Agentflow checkout (see
[README.md](../README.md) for install paths). Treat a failing version probe
as "Agentflow unavailable", not as a reason to reimplement any part of the
loop.

## Expected Command Sequence

A single Agentflow-backed Golem run drives this sequence. Golem authors the
plan (it owns planning); Agentflow validates and locks it.

```bash
agentflow init
# Golem generates the full plan contract as JSON: schema_version, objective,
# scope, non_goals, invariants, risk_level, drift_budget, evidence_ids,
# allowed_files, blocked_files, validation_gates, rollback_plan, steps.
# Each step needs id, action, files, expected_diff, preconditions,
# validation (list), evidence_ids. Record evidence first with
# `agentflow record-evidence` so evidence_ids resolve.
# Optional spec-driven plans also include requirements[].acceptance_criteria,
# steps[].criterion_ids, gates[].criterion_ids, and per-criterion review floors.
agentflow lock-plan --from-json plan.json --json   # or: --stdin
agentflow init-execution
agentflow doctor --json                            # execution contract is present

# Per plan step, repeat:
agentflow claim-step P1 --agent golem
#   ... Golem edits files ...
agentflow record-file-change --step P1 --path <changed-path>   # per edit, as it happens
agentflow run --step P1 --gate "<one step.validation entry>" -- <validation-command>
agentflow finish-step P1          # verify-step, then complete-step iff it passes

# Terminal gates, once all steps are complete:
agentflow finish-run              # audit-drift -> verify-run -> build-proof -> verify-proof
```

Notes for the adapter:

- `lock-plan --from-json` / `--stdin` is the machine path: it validates
  through the same plan validator as the manual flow and returns structured
  `errors` with nonzero exit on an invalid plan. Do not hand-edit
  `.agent/plan.lock.json` after locking.
- `run --gate` takes one exact gate label at a time. For legacy plans, choose
  the matching string from `steps[].validation`; do not pass the full
  validation list.
- `next-action --json --agent <worker-id>` is the state probe. It reports the
  existing advisory action plus an authoritative `resumability` object derived
  from the locked plan, execution contract, and attempt-scoped ledgers. Consume
  its step/attempt owner, evaluated lease, receipts, gates, diagnostics, and
  allowed recovery actions instead of reconstructing those rules. It always
  exits zero; malformed or ambiguous state has diagnostics and no allowed
  action, while `fail` is explicitly marked as non-automatic break-glass.
- Record file changes as they happen, not batched at the end. Hunk-level
  attribution fingerprints each diff hunk; an unrecorded edit inside an
  allowed file fails drift under the default `enforce` policy.
- Post-completion fixes go through `amend-step` (optionally with
  `--finding RR-...#ID` to correlate a review finding). A completed step
  rejects a fresh `claim-step`.
- The review cycle (`record-review`, finding-linked amendments) is optional and
  documented in [docs/agent-workflow.md](agent-workflow.md). An adapter that
  implements it must consume only review runs with `amendment_ready: true`, use
  each active row's validated `owning_step`, `claim`, optional `location`, and
  `suggested_fix`, and preserve `RR-...#finding-id`. It must not infer ownership
  from filenames, model output, or legacy manifests. Runs marked
  `amendment_ready: false` remain display-only until authoritative context is
  produced; a first adapter may defer the review cycle entirely.

## Optional Requirement Traceability

Golem may compile an approved specification into the optional plan extension
documented in [Agentflow Workflow](agent-workflow.md#optional-requirement-traceability).
Golem owns the authored requirement/criterion text and stable IDs. Agentflow
owns validation, ledger correlation, state projection, and proof verification.

Adapter rules:

- Keep requirement IDs unique and criterion IDs globally unique, using the
  published stable-ID pattern.
- Put every criterion in at least one `steps[].criterion_ids` list. Plan locking
  rejects criteria with no implementing step.
- Put `criterion_ids` on each structured command or inspection gate that proves
  a criterion, and keep each gate list within its parent step's list. A
  successful but unmapped command is not criterion evidence.
- For review-backed criteria, declare `review.minimum_depth` as
  `spec_quality` or `deep`. Do not write review results into the plan; use the
  existing `record-review` path after plan lock. Each new review run records
  the locked plan's canonical content hash (lock bookkeeping excluded, so a
  no-op re-lock keeps the binding); a legacy or differently bound run cannot
  satisfy the criterion.
- Treat `lock-plan --json` duplicate, dangling, malformed, and uncovered-ID
  diagnostics as compiler feedback and regenerate the candidate plan.

After `build-proof`, consume `coverage.requirements` as the deterministic
requirement -> criterion -> step/evidence projection. The criterion state is
`satisfied`, `failed`, `missing`, or `unmapped`; any non-satisfied state fails
the conditional `criteria_satisfied` check. `build-proof` revalidates the
traceability contract, and `verify-proof` revalidates it and recomputes both the
projection and derived check from the locked plan and current evidence, command
receipts, and plan-bound review runs. Golem must not cache or patch coverage or
check metadata itself.

Plans that omit `requirements` remain the legacy adapter path: no traceability
coverage keys or criterion proof check are emitted. Agentflow adds no provider
field and no requirements ledger.

## Choosing a Surface: CLI vs MCP

Golem can shell out to the `agentflow` CLI or speak MCP (stdio or Streamable
HTTP) to the bundled server ([docs/mcp.md](mcp.md)). Both hit the same
in-process CLI; MCP returns parsed JSON in `structuredContent.data`, which is
usually more convenient for an adapter. The `next_action` MCP tool accepts the
same optional `agent` identity and returns the same `resumability` projection.

All seventeen MCP tools are state-transition or read-only operations on
`.agent/` and are safe for Golem to call autonomously:

`status`, `doctor`, `next_step`, `next_action`, `claim_step`, `amend_step`,
`reclaim_step`, `renew_lease`, `record_review`, `complete_step`, `verify_step`,
`finish_step`, `verify_run`, `finish_run`, `audit_drift`, `build_proof`,
`verify_proof`.

Two CLI commands are deliberately **not** exposed over MCP:

- `agentflow run` — executes an arbitrary shell command and records the
  receipt.
- `agentflow record-command` — records a receipt for a command the host
  already ran.

These are the shell-execution paths. They must stay on the CLI, invoked by
the Golem host process under whatever command-approval policy Golem applies
to its own shell tool. An MCP client — including a compromised or confused
one — cannot drive shell execution through the Agentflow server.

`record-file-change` is also CLI-only today; a Golem adapter that manages
state over MCP still shells out for `run` and `record-file-change`.

## Security Boundaries

- **Agentflow risk screening is not a sandbox.** `agentflow run` classifies
  commands (see [docs/command-risk.md](command-risk.md)) and can require
  confirmation, but classification is advisory string analysis. It does not
  isolate the process, limit filesystem access, or make a dangerous command
  safe. Command approval and sandboxing are Golem's responsibility.
- **The MCP HTTP transport is unauthenticated.** Bind loopback only; the
  `Origin` check defends against browsers, not arbitrary local clients. For
  a single-machine adapter, prefer stdio.
- **Receipts capture command output.** `.agent/command-receipts.jsonl` and
  `.agent/receipts/` can contain command strings, paths, environment variable
  names, and captured stdout/stderr. Apply the retention and publication
  policy in [docs/agent-artifacts.md](agent-artifacts.md) before uploading or
  committing anything under `.agent/`.

## Required `.agent/` Artifacts

Golem should treat these as opaque Agentflow state — created and mutated only
through commands, validated with `next-action`/`verify-*`:

| Phase | Artifacts that must exist |
| --- | --- |
| After `init` | Base scaffolds: `plan.lock.json` (unlocked), `evidence.jsonl`, `assumptions.json`, context/failure/amendment/review/capability/runtime ledgers, `drift-report.json`, `proof-pack.md`, `model-profiles/*.example.json` |
| After `lock-plan` | `plan.lock.json` with `locked: true` and real steps |
| After `init-execution` | `execution.contract.json`, empty execution ledgers, `attempts/`, `handoffs/`, `receipts/` |
| During steps | Populated `step-runs.jsonl`, `command-receipts.jsonl`, `file-receipts.jsonl`, `verification-runs.jsonl`, `receipts/` |
| After `finish-run` | Updated `drift-report.json` and `proof-pack.md`; created or updated `proof-pack.json` |
| After `aggregate-ledgers` | `aggregation.json` in the canonical root (cross-worktree merges only); folded into `proof-pack.json` provenance by the next `build-proof` |

The full artifact inventory is in
[docs/agent-workflow.md](agent-workflow.md); retention and publication rules
(including the Golem-specific defaults) are in
[docs/agent-artifacts.md](agent-artifacts.md).

## Single-Writer First

Run exactly one Golem worker per worktree. Agentflow's execution contract
rejects `multi_writer` today. Single-writer lease enforcement
([#20](https://github.com/kstruzzieri/agentflow/issues/20)) has shipped, but
shared-tree multi-writer coordination remains out of scope. Cross-worktree proof
aggregation ([#30](https://github.com/kstruzzieri/agentflow/issues/30)) is now
implemented: run one worker per worktree, then merge the immutable trees with
`aggregate-ledgers` into a single canonical `.agent/` proof. An adapter that
wants parallelism should use that one-worker-per-worktree model rather than
inventing its own locking on top of `.agent/`.

## Failure Modes

| Symptom | Adapter behavior |
| --- | --- |
| `agentflow --version` fails | Report "Agentflow unavailable" with install hint; do not fall back to an unproofed run silently |
| `lock-plan` returns `status: "invalid"` | Surface the structured `errors` to the planning layer; regenerate the plan |
| `.agent/` state unclear (crash, resume) | Run `next-action --json --agent <worker-id>`; follow only an allowed non-break-glass recovery action, or surface diagnostics |
| Gate command times out | The receipt records `decision: "timeout"`; the step will not verify — fix the gate or the timeout in `execution.contract.json` |
| `finish-run` stops at a gate | `--json` reports `stopped_at`; resolve that gate before re-running |

## Related Issues

- [go-llm#209](https://github.com/kstruzzieri/go-llm/issues/209) — parent:
  Agentflow-backed task mode in Golem
- [#20](https://github.com/kstruzzieri/agentflow/issues/20) — authoritative
  read-only resumability projection
- [#30](https://github.com/kstruzzieri/agentflow/issues/30) — deferred
  parallelism design
- [#19](https://github.com/kstruzzieri/agentflow/issues/19) — MCP/runtime
  status as read-only evidence
