# Agentflow Workflow

Agentflow is a portable development protocol for keeping agentic software work
bounded, evidence-backed, and reviewable. The model proposes facts and edits; the
workflow records the contract, evidence, drift checks, and proof.

## Phases

1. Intake: convert user intent into a bounded objective, non-goals, success
   criteria, risk, and unknowns.
2. Recon: inspect the smallest useful slice of the project and record evidence.
3. Contract: define scope, invariants, allowed files, blocked files, validation
   gates, rollback plan, risk, and drift budget.
4. Plan compile: turn the contract into executable plan steps.
5. Implementation loop: edit only declared files, attribute changes to plan
   steps, and run focused validation.
6. Drift audit: compare changed files to the locked plan. Hunk-level
   attribution is active: `record-file-change` captures per-hunk fingerprints,
   and `audit-drift` / `verify-run` flag any current hunk in an allowed file
   that was not covered by a recorded receipt.
7. Verification: run deterministic checks before relying on model judgment.
8. Proof pack: summarize the objective, scope, evidence, validation, drift
   result, and residual risk.

## Core Artifacts

- `.agent/plan.lock.json`: the task contract and compiled plan steps.
- `.agent/evidence.jsonl`: claims backed by sources.
- `.agent/assumptions.json`: tracked assumptions and their resolution status.
- `.agent/context-receipts.jsonl`: files loaded, reasons, symbols, and use.
- `.agent/failures.jsonl`: compressed failure signatures.
- `.agent/amendments.jsonl`: explicit plan changes.
- `.agent/drift-report.json`: drift audit output.
- `.agent/workflow.contract.json`: optional selected workflow pack/profile,
  required capabilities, review depth, validation policy, and proof policy.
- `.agent/proof-pack.md`: completion proof.
- `.agent/proof-pack.json`: structured completion proof metadata.

## Execution Artifacts

Agentflow v0.3 adds optional execution artifacts. They are present when a task
uses `agentflow init-execution`.

- `.agent/execution.contract.json`: shell-runtime policy for the task.
- `.agent/step-runs.jsonl`: step claim, progress, verification, completion,
  blocked, and failed events.
- `.agent/command-receipts.jsonl`: command invocations, exit codes, timeout
  outcomes, output hashes, and provenance.
- `.agent/file-receipts.jsonl`: changed paths mapped to plan steps and attempts.
- `.agent/verification-runs.jsonl`: step and run verification results.
- `.agent/handoffs/`: generated provider-neutral handoffs.
- `.agent/receipts/`: captured command output referenced by command receipts.

## Optional Requirement Traceability

A plan may declare provider-neutral requirements and acceptance criteria. The
adapter authors the IDs and text; Agentflow validates the references and derives
proof coverage from existing ledgers. It does not generate requirements, tests,
or model judgments.

```json
{
  "requirements": [
    {
      "id": "REQ-API",
      "text": "The API rejects invalid input.",
      "acceptance_criteria": [
        {
          "id": "AC-API-400",
          "text": "Invalid input returns HTTP 400."
        },
        {
          "id": "AC-API-REVIEW",
          "text": "The behavior passes a spec-quality review.",
          "review": {"minimum_depth": "spec_quality"}
        }
      ]
    }
  ],
  "steps": [
    {
      "id": "P1",
      "criterion_ids": ["AC-API-400", "AC-API-REVIEW"],
      "gates": [
        {
          "kind": "command",
          "run": ["python3", "-m", "unittest", "tests.test_api"],
          "criterion_ids": ["AC-API-400"]
        },
        {
          "kind": "inspection",
          "evidence_id": "E-API-RESPONSE",
          "describe": "Inspect the response contract.",
          "criterion_ids": ["AC-API-400"]
        }
      ]
    }
  ]
}
```

Requirement and criterion IDs use `^[A-Za-z][A-Za-z0-9._-]{0,127}$`.
Requirement IDs are unique, criterion IDs are globally unique, and all
`criterion_ids` references must resolve. Gate references must also be a subset
of their parent step's `criterion_ids`. Every criterion must be named by at
least one step before the plan can lock. A criterion may still be `unmapped`
at proof time when an implementing step exists but no command gate, inspection
gate, or review floor explicitly maps deterministic evidence to it.

`build-proof` adds `coverage.requirements` and
`coverage.criterion_status_counts` only when the plan declares requirements.
Each criterion includes its implementing steps, mapped evidence, and one state:

- `satisfied`: every mapped mechanism is satisfied;
- `failed`: at least one mapped command or qualifying review failed;
- `missing`: a mapped receipt, inspection evidence ID, or qualifying review run
  is absent;
- `unmapped`: the criterion has step coverage but no deterministic evidence
  mapping.

When multiple mappings disagree, precedence is `failed` -> `missing` ->
`unmapped` -> `satisfied`. Command evidence uses the latest matching receipt for
the mapped step and structured gate: the receipt's argv must equal the gate's
`run`, or carry the gate's label with the gate's `run` as its argv tail (an
env-style wrapper such as `env KEY=value <run>`). A receipt recorded under the
gate's label but with a different command is never criterion proof. Inspection
evidence uses the existing `.agent/evidence.jsonl` ID. Review evidence uses the
latest recorded run at the declared `spec_quality` or `deep` floor whose
`plan_sha256` matches the current locked plan's canonical content hash (the
plan JSON minus the `locked`/`locked_at` lock bookkeeping, serialized with
sorted keys); `deep` satisfies a `spec_quality` floor, and the run must have
`gate_status: pass` with no active blockers. `record-review` writes this
current-plan binding automatically. A no-op re-lock keeps existing bindings;
any semantic plan change invalidates them. Legacy review rows without
`plan_sha256` remain readable, but cannot satisfy a review-backed criterion. A
successful receipt for a gate without `criterion_ids` is never criterion proof.

The projection is inside the existing hash-bound proof `coverage` member.
`build-proof` revalidates requirement traceability before projecting it, and
`verify-proof` independently revalidates and recomputes the projection from the
locked plan and current evidence/command/review ledgers. Verification also
recomputes the derived `criteria_satisfied` check, so deleting or editing that
check cannot bypass a failed criterion even if the core checksum is recomputed.

This additive extension keeps the plan-lock schema version unchanged (optional
plan fields only) but grows canonical proof content and review-run rows. That
extension introduced proof schema `0.9.0` and review-run schema `0.5.0`; later
additive review projection fields advance the current versions independently.
Older artifacts stay readable
(same major, lower minor). A legacy plan without `requirements` emits neither
traceability coverage key nor the `criteria_satisfied` check, preserving its
prior execution and proof output.

## Optional Design Decision References

A plan may also declare design decisions and associate a step with selected
decision IDs. The declarations and each step list are optional, but a present
`design_decisions` or `design_decision_ids` list must be non-empty. Declaration
IDs are unique, stable IDs; decision text and each reference entry are
non-blank. References are opaque strings, and an explicitly empty
`references: []` is valid. In `coverage.design_decisions`, an omitted plan
`references` member is projected as `[]`. A present step list is unique and
every ID resolves to a declaration; declared decisions may intentionally remain
unselected. Gates and draft plans do not gain decision-reference fields.

```json
{
  "schema_version": "0.4.0",
  "design_decisions": [
    {
      "id": "DD-1",
      "text": "Use the existing receipt ledger.",
      "references": ["docs/agent-workflow.md"]
    }
  ],
  "steps": [
    {
      "id": "P1",
      "design_decision_ids": ["DD-1"]
    }
  ]
}
```

Plans that omit both fields under `0.3.x` remain accepted. A plan that uses
either field must declare schema `0.4.0` or later, so an older locker correctly
rejects a correctly labelled `0.4.0` plan rather than silently dropping data.

When declarations exist, `build-proof` conditionally emits the ordered
`coverage.design_decisions` rows: declaration order, reference order, and each
row's plan-step order are preserved. Without declarations it omits that key.
The projection is in the existing hash-bound proof coverage, and the existing
plan/core hashing already binds the decision data. `verify-proof` independently
revalidates the locked plan and recomputes this projection; consumers must not
patch it. `next-step --json` exposes the raw locked step, including an optional
`design_decision_ids` member when that step selected decisions.

## Artifact Retention

The active root `.agent/` directory is local-only by default. It is safe for a
task loop to write, but it should not be committed or uploaded wholesale without
review. Use [docs/agent-artifacts.md](agent-artifacts.md) to choose between
local-only proof, CI-uploaded proof, PR-attached proof, and committed proof
bundles.

In particular, review `.agent/command-receipts.jsonl`, runtime metadata, review
ledgers, and `.agent/receipts/` before publishing them. Command receipts can
disclose command strings, paths, environment variable names, and output hashes;
receipt files contain captured stdout/stderr.

## Event Stream

`agentflow events` projects the four append-only execution ledgers
(`step-runs.jsonl`, `command-receipts.jsonl`, `file-receipts.jsonl`,
`verification-runs.jsonl`) into a single chronological stream. It is a
**read-only projection, not a new source of truth** — the ledgers remain
authoritative and the command never writes to disk.

Each event carries a `source` pointer (`ledger`, `record_id`, `index`) so it can
be traced back to the originating ledger and receipt id. Ordering is
deterministic and replay-stable: events sort by `(timestamp, ledger, append
index)`, so projecting the same `.agent/` twice yields identical output even when
second-precision timestamps tie.

```bash
agentflow events                 # human-readable, one line per event
agentflow events --jsonl         # newline-delimited JSON (one event per line)
agentflow events --json          # single JSON array
agentflow events --since 2026-06-18T10:00:00+00:00   # inclusive lower bound
```

## Agent Task Loop

All agent front ends should follow the same local loop. Use the installed
`agentflow` console script when available, or replace `agentflow` with
`PYTHONPATH=src python3 -m agentflow` from the repository root.

```bash
agentflow init
# Populate .agent/plan.lock.json with the required plan contract:
# objective, scope, invariants, allowed_files, validation_gates, rollback_plan,
# and real steps. Include task files and .agent/ in allowed_files; then lock it.
agentflow lock-plan .agent/plan.lock.json
agentflow init-execution
STEP_ID=P1
VALIDATION_GATE="<matching step.validation entry>"
agentflow claim-step "$STEP_ID" --agent "$USER"
agentflow run --step "$STEP_ID" --gate "$VALIDATION_GATE" -- <validation-or-work-command>
agentflow record-file-change --step "$STEP_ID" --path <changed-path>
agentflow verify-step "$STEP_ID"
agentflow complete-step "$STEP_ID"
agentflow verify-run
agentflow audit-drift
agentflow build-proof
agentflow verify-proof
```

- `init` creates the plan, evidence, assumptions, context, drift, runtime, and
  proof scaffold.
- `lock-plan` validates and locks the populated plan. `init` starts with
  placeholder fields and an empty `steps` list, so `$STEP_ID` must come from a
  real task step in a valid `.agent/plan.lock.json`; `P1` only works after
  completing the plan contract and adding that step. Include `.agent/` in
  `allowed_files` when the loop writes Agentflow artifacts in the same worktree.
  Adapter-backed callers can avoid hand-editing the artifact by supplying the
  same plan contract as JSON:

  ```bash
  agentflow lock-plan --stdin --json < plan.json
  agentflow lock-plan --from-json plan.json --json
  ```

  These forms validate through the same plan validator, set `locked` and
  `locked_at`, and write `.agent/plan.lock.json` by default. Invalid JSON or
  invalid plan fields return nonzero with `status: "invalid"` and structured
  `errors` when `--json` is used. The positional form
  `agentflow lock-plan .agent/plan.lock.json` remains the in-place manual path.
- `workflow-contract --from-json workflow-contract.json` validates and writes
  `.agent/workflow.contract.json` when an adaptive workflow has selected a
  policy for the task. `workflow-contract --validate` checks the current
  artifact. The contract stays provider-agnostic; it records policy
  requirements, not runtime-specific skill execution.
- `record-capability` and `waive-capability` append to
  `.agent/capability-receipts.jsonl` as specialized practices run (TDD, security
  review, etc.) or are knowingly waived. `record-capability` needs `--provider`
  (a free string); `waive-capability` omits it; both need `--reason`. Agentflow
  records evidence only; it never invokes the practice. `build-proof` compares
  these against the contract's `required_capabilities`.
- `init-execution` creates the execution contract and additive ledgers.
- `claim-step` records which agent owns the step attempt.
- `run` executes a command with the configured timeout and records the observed
  command receipt. A timed-out command records `decision: "timeout"`,
  `timed_out: true`, `timeout_seconds`, and `exit_code: null`. Use `--gate` with
  the matching legacy `step.validation` entry unless the step uses structured
  command gates.
- `record-file-change` maps each edited path back to the claimed step and
  captures per-hunk fingerprints (sha256 of changed lines, span-independent).
  Out-of-band edits inside an allowed file that are not re-recorded fail drift
  under the default `enforce` policy (`proof_policy.hunk_attribution`).
- `verify-step` checks the step attempt has required command and file receipts.
- `complete-step` closes only a successfully verified step.
- `verify-run` checks the full execution ledger for completion coverage.
- `audit-drift` writes the current `.agent/drift-report.json` used by the proof
  pack.
- `build-proof` writes `.agent/proof-pack.md` and `.agent/proof-pack.json`. If
  `.agent/workflow.contract.json` exists, the proof includes a concise workflow
  contract summary and source hash. It also emits a `capabilities` block
  (`required`/`recorded`/`waived`/`missing`) and a
  `required_capabilities_satisfied` check; a missing required capability warns,
  and strict proof promotes it to an error unless a waiver is recorded.
- `verify-proof` re-checks proof metadata and referenced artifact hashes,
  including stale or tampered workflow contract metadata when the proof
  references it.
- `view-proof --html` renders `.agent/proof-pack.json` and the execution
  ledgers into a self-contained static report at `.agent/proof-report.html`
  (override with `--output`). The report has no JavaScript and no external
  references, escapes all recorded content, and links to command receipt
  stdout/stderr files when they exist. It is a review aid for humans;
  `verify-proof` remains the authoritative check, and `view-proof` never
  writes to any ledger.

### Amending a completed step

A completed step rejects a new `claim-step` and rejects new-work receipts on its
terminal attempt (including via an explicit `--attempt`). To attach a follow-up
edit after completion — for example a review-feedback fix — open an auditable
amendment:

```bash
agentflow amend-step P1 --agent "$USER" --reason "address review: null-check" \
    --reason-code review_feedback
agentflow record-file-change --step P1 --path src/foo.py
agentflow run --step P1 --gate "<validation>" -- <validation-command>
agentflow verify-step P1
agentflow complete-step P1
```

`amend-step` opens a new attempt linked to the prior completed attempt
(`amends_attempt`); it is the only sanctioned post-completion attempt opener. The
amendment is gated by `verify-step` like any attempt and is surfaced in
`build-proof` and `verify-run`. `reason_code` is one of `review_feedback`,
`validation_followup`, `operator_correction`, or `other`.

## Porcelain shortcuts

The Agent Task Loop above lists every plumbing command explicitly. Three
porcelain commands wrap the common sequences so a front end can drive the loop
without restating each step. They are convenience wrappers only: the plumbing
commands remain authoritative and porcelain adds no new receipt or proof
semantics.

- `next-action` is read-only. It inspects the current `.agent/` state and reports
  the single next required action across the ordered loop states (uninitialized,
  plan unlocked, execution uninitialized, step unclaimed, missing file receipts,
  missing validation, unverified step, uncompleted step, failing drift,
  unverified run, missing/stale/failing proof, complete). It prints a copy-paste
  `agentflow ...` command; `--json` emits the same as a parseable object.
  The additive `resumability` object reports locked contract hashes, the
  actionable step and open attempt, owner and evaluated lease state,
  attempt-scoped receipts and gates, structured diagnostics, and the
  `claim`/`continue`/`renew`/`reclaim`/break-glass `fail` actions allowed for
  `--agent` (or `AGENTFLOW_AGENT_ID`). Break-glass failure is always marked
  `automatic: false`.
  `next-action` is advisory and always exits zero (reporting a blocking state is
  still a successful report); `--strict` only promotes warnings to failures when
  classifying which state to report.
- `finish-step <id>` runs `verify-step` then `complete-step`. It never completes
  a step unless verification passes. `--attempt`, `--strict`, `--replay`, and
  `--json` are passed through.
- `finish-run` runs the terminal gates in order — `audit-drift` -> `verify-run`
  -> `build-proof` -> `verify-proof` — and stops at the first failing gate.
  `--json` emits `{ok, stopped_at, gates, diagnostics}`.

A typical run discovers the next step, verifies and completes it, then runs the
terminal gates:

```bash
agentflow next-action --agent "$USER"  # report action and actor-specific recovery
agentflow finish-step P1       # verify-step then complete-step
agentflow finish-run           # audit-drift -> verify-run -> build-proof -> verify-proof
```

All three are also exposed as MCP tools (`next_action`, `finish_step`,
`finish_run`) with the parsed JSON in `structuredContent.data`.

## Review cycle

When a manual review produces findings, Agentflow records each review run as
durable evidence and correlates fixes back to the findings they resolve. The
required artifact set depends on `review-manifest --depth-profile`: `deep`
(default) needs the full four-pass set; `spec_quality` needs only
`findings-final.json` + `gate.yaml`; lower depths need only the findings
sidecar. The end-to-end review -> fix -> amend -> proof sequence (shown here
for the full `deep` path) is:

```text
1. Agent runs the four-pass review (docs/ai prompts)
   -> findings-final.yaml, gate.yaml, synthesis.md, review-manifest.json
2. agentflow record-review --manifest .../review-manifest.json
   -> RR-... record in .agent/review-runs.jsonl
      (current plan + manifest + artifacts hashed; amendment projection retained)
3. For each finding that needs a fix (amendment opens the attempt and sets the
   pointer — no re-claim):
     agentflow amend-step P3 --agent "$USER" --reason "address review finding BP-001" --reason-code review_feedback --finding RR-...#BP-001
     agentflow run --step P3 --gate "<gate>" -- <work/validation command>
     agentflow record-file-change --step P3 --path <changed-path>
     agentflow verify-step P3
     agentflow complete-step P3
4. Re-run the review after fixes -> new review-manifest.json
   (BP-001 status:fixed, active_blocking shrinks)
   -> agentflow record-review --manifest ...   (new RR-... record; append-only)
5. agentflow build-proof
   -> proof pack and view-proof surface review runs, amendment readiness,
      finding context, and finding<->amendment correlations
6. agentflow verify-proof
   -> rehashes the review-runs ledger (via proof files) AND, as an extra pass,
      rehashes the source artifacts from the hashes inside each review-run
      record; reports review_gate per policy
```

Current v1.0 manifests declare `amendment_ready: true`. Active `open` and
`accepted` rows require `owning_step`, `claim`, and `suggested_fix`; `location`
is optional. `record-review` reads only the manifest, validates each supplied
owner against the current locked plan, hashes the plan, YAML/Markdown artifacts,
and manifest, then atomically appends the intact projection to
`.agent/review-runs.jsonl` without parsing or re-adjudicating YAML. Validation,
duplicate-ID, ownership, or artifact failure appends nothing.

Manifest v0.0-v0.2 remains recordable and verifiable. Its ledger and proof
projection explicitly report `amendment_ready: false`, retain existing finding
fields, and never synthesize an owner. Inactive findings in v1.0 remain visible
without requiring amendment fields. The proof core binds the complete review
summary, `verify-proof` rehashes its source ledger and artifacts, and
`view-proof --html` renders the amendment context.
`amend-step --finding RR-...#ID` links an amendment attempt to the finding it
resolves, and `build-proof`/`verify-proof` surface and re-hash the review
evidence.

## Plan Lock Rules

- Scope must be concrete enough to validate.
- Allowed and blocked paths must be explicit.
- Every plan step must list files, expected diff, validation, and evidence.
- New facts that invalidate the plan require an amendment.
- Dependency churn, test weakening, and architecture drift must be declared.

## Evidence Rules

Evidence entries use this shape:

```json
{
  "id": "E1",
  "claim": "The API validates request bodies before persistence.",
  "source": "src/api/orders.ts:42",
  "confidence": "high",
  "last_verified": "2026-05-29T00:00:00Z"
}
```

Sources can be files, line ranges, commands, test results, logs, docs, URLs, or
explicit user statements.

## Drift Rules

A drift audit fails when changed files are outside `allowed_files` or match
`blocked_files`. Hunk-level attribution is also active: `audit-drift` and
`verify-run` report `unmapped_hunks` for any diff hunk inside an allowed file
that does not match a recorded file-receipt hunk. The `proof_policy.hunk_attribution`
knob in the execution contract governs severity: `enforce` (default when a
contract exists) fails drift, `observe` produces a warning without blocking,
and `off` disables hunk checks entirely. Legacy receipts without a `hunks` key
fall back to whole-file coverage and are never flagged. Binary and unparseable
files also fall back to whole-file coverage at verify-run time.

## Completion Rule

The final user response should summarize the proof pack. It should not replace
the proof pack.
