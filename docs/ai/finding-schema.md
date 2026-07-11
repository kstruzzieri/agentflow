# Agentflow Review Finding Schema

This schema defines the manual review-cycle findings used by the four-pass
Agentflow review policy. It is a documentation contract for reviewers and
agents, not an executable validator.

## Local State Layout

Review output is local to a branch, pull request, or task run:

```text
docs/ai/state/<branch-or-pr-id>/
  findings-bp.yaml
  findings-adv.yaml
  findings-final.yaml
  synthesis.md
  gate.yaml
  ready-for-pr.md  # conditional; only when gate.yaml passes
```

`docs/ai/state/` is gitignored. Reviewers may keep intermediate notes there
without adding task-local findings to a feature diff.

## Finding File Shape

Each findings file is YAML. Use `findings: []` when a pass has no findings.
Otherwise, write findings as a list under `findings`:

```yaml
findings:
  - id: BP-001
    pass: best-practices
    severity: high
    category: proof-integrity
    file: src/agentflow/proof.py
    line: 120
    line_end: 132
    claim: "verify-proof can pass when proof metadata omits a hashed artifact"
    evidence: |
      The verifier iterates only entries present in proof-pack.json.
      It never compares the metadata artifact list against required proof artifacts.
    why_it_matters: |
      Agentflow proof packs are used to prove a task's completion. If a required
      artifact can be omitted, a stale or incomplete proof can look valid to CI or
      a human reviewer.
    suggested_fix: |
      Compare the proof metadata artifact set against the required artifact list
      before checking hashes, and fail when any required artifact is absent.
    confidence: high
    status: open
    steelman_verdict: pending
    steelman_reason: ""
    superseded_by: ""
    fix_commit: ""
    agentflow_refs:
      plan_step: P2
      evidence_ids: ["E2"]
      command_receipts: []
      file_receipts: []
      proof_artifacts: [".agent/proof-pack.json"]
```

## Fields

`id`: Stable finding id. Use `BP-NNN` for pass 1, `ADV-NNN` for pass 2,
`DEF-NNN` for distinct issues found during pass 3, and preserve existing ids
when pass 3 validates or rewrites a finding.

`pass`: One of `best-practices`, `adversarial`, `defender`, or
`synthesis-gate`.

`severity`: One of `critical`, `high`, `medium`, or `low`, using
`docs/ai/severity-rubric.md`.

`category`: The risk surface or review class. Use the categories below.

`file`, `line`, `line_end`: Exact location for the primary evidence. Use the
smallest line range that supports the claim. For repository-wide issues, set
`file` to the main contract or documentation file and explain the broader scope
in `evidence`.

`claim`: One sentence describing the observed defect or risk.

`evidence`: Concrete code, documentation, command, receipt, or proof-pack facts
that support the claim.

`why_it_matters`: Agentflow-specific impact. Explain how the issue affects
workflow integrity, proof reliability, receipt provenance, replay behavior,
schema acceptance, command exits, or task execution.

`suggested_fix`: Actionable repair direction. Do not prescribe unrelated
refactors.

`confidence`: `high`, `medium`, or `low`. Confidence is independent from
severity.

`status`: Current lifecycle value. The active statuses are `open` and
`accepted`.

`steelman_verdict`: Pass 3 decision for findings from pass 1 and pass 2.

`steelman_reason`: Evidence-backed pass 3 rationale. Required for downgraded,
rejected, and superseded findings.

`superseded_by`: Replacement finding id when a finding is merged into a clearer
or broader finding.

`fix_commit`: Commit hash or short identifier once the finding is fixed.

`agentflow_refs`: Optional but preferred references to Agentflow proof
artifacts, plan steps, evidence, and receipts.

## Machine projection: review-manifest.json

Pass 4 emits a compact `findings-final.json` sidecar beside `findings-final.yaml`,
one object per final finding with exactly `id`, final `severity`, `status`, and
(when set) `steelman_verdict`, `superseded_by`, and `fix_commit`, wrapped as
`{"findings": [ ... ]}`. Pass 4 then runs `agentflow review-manifest` to produce
`review-manifest.json` from that sidecar — do not hand-project the manifest.
Agentflow hashes the YAML/Markdown artifacts and the manifest, then writes a
durable record to `.agent/review-runs.jsonl`; it never parses the YAML and never
re-adjudicates a finding. `review-manifest.gate_status` is the deterministic
finding-policy status computed from the sidecar; full pass-4 readiness remains in
`gate.yaml`. See `schemas/review-manifest.schema.json` for the authoritative
manifest contract.

## Categories

- `correctness`
- `security`
- `error-handling`
- `testing`
- `documentation`
- `architecture`
- `dependencies`
- `schema-validator-drift`
- `proof-integrity`
- `receipt-provenance`
- `replay-behavior`
- `execution-state`
- `cli-contract`
- `packaging`

Use the Agentflow-specific categories when a finding touches proof packs,
receipt provenance, replay behavior, execution state, schemas, or command
contracts. Use the general categories for ordinary code or documentation
review findings.

## Status Lifecycle

- `open`: Finding has not been accepted, rejected, superseded, or fixed.
- `accepted`: Pass 3 or a maintainer accepts the finding as valid.
- `rejected`: Pass 3 proves the finding is not valid.
- `superseded`: Another finding covers the same issue more accurately.
- `fixed`: A committed change resolves the accepted finding.

Open and accepted findings are active unless pass 3 marks them fixed,
rejected, or superseded.

## Defender Verdicts

- `pending`: Pass 3 has not evaluated the finding.
- `confirmed`: The evidence supports the claim and severity.
- `downgraded`: The finding is valid, but severity or scope was overstated.
- `rejected`: The cited evidence does not support the claim, the behavior is
  intentional, or another artifact already enforces the needed guarantee.
- `superseded`: A different finding should represent the issue.

Pass 3 must not rubber-stamp findings. A downgraded, rejected, or superseded
finding needs a specific `steelman_reason` citing code, docs, tests, receipts,
or proof artifacts.

## Gate Blocking Rules

Under the full gate, active Critical and High findings block pass 4. Use the
finding's final severity after pass 3 validation; a downgraded finding still
blocks if its final severity remains Critical or High. Medium findings warn
unless the branch policy says Medium blocks. Low findings are advisory. Fixed,
rejected, and superseded findings never block pass 4.
