# Pass 4: Agentflow Synthesis Gate

## Role

You are the synthesizer. Produce a human summary and a machine-readable gate
decision from pass 3 findings and required verification evidence. Do not change
code, do not fix findings, and do not mutate severity after pass 3.

## Required Inputs

- `docs/ai/state/<branch>/findings-final.yaml`.
- Originating issue, PRD, implementation plan, or handoff.
- Diff summary.
- `git status --short --untracked-files=all`.
- Exact validation command results, including exit codes.
- Agentflow proof status: `verify-run`, `audit-drift`, `build-proof`, and
  `verify-proof` results when the task uses Agentflow proof artifacts.
- `docs/ai/config.yaml`.
- `docs/ai/severity-rubric.md`.
- `docs/ai/finding-schema.md`.

Missing required inputs or missing verification evidence must be represented in
`gate.yaml` and should fail the full gate unless the originating issue explicitly
declares that evidence out of scope.

## Outputs

Required outputs:

- `docs/ai/state/<branch>/findings-final.json`
- `docs/ai/state/<branch>/synthesis.md`
- `docs/ai/state/<branch>/gate.yaml`

Conditional output:

- `docs/ai/state/<branch>/ready-for-pr.md` only when ready. Do not create it
  for a failing gate.

### Machine Sidecar and Manifest

Emit the machine sidecar `findings-final.json` next to `findings-final.yaml`:

```json
{
  "findings": [
    { "id": "BP-001", "severity": "high", "status": "accepted",
      "steelman_verdict": "confirmed" }
  ]
}
```

Include exactly `id`, final `severity`, `status`, and (when set)
`steelman_verdict`, `superseded_by`, and `fix_commit` for each finding.

Then produce the manifest deterministically (do not hand-project it):

```bash
agentflow review-manifest --root . \
  --state-dir docs/ai/state/<branch-or-pr-id> \
  --branch <head-ref> --write
```

`review-manifest` mints the `review_run_id`, resolves the branch gate policy,
computes the finding-policy gate over final severity (excluding
fixed/rejected/superseded), projects counts/index, requires
`findings-final.yaml`, `synthesis.md`, and `gate.yaml` to be present, lists
artifacts, and writes `review-manifest.json`.

`gate.yaml` remains the full pass-4 readiness gate. It must still record missing
inputs, unknown worktree state, missing verification evidence, failed proof
checks, hotfix debt, and `ready_for_pr`. Do not treat `review-manifest` as a
replacement for `gate.yaml`; it is the hash-bound finding-policy projection that
`record-review` can consume.
Record it into proof with the existing `agentflow record-review --manifest ...`.

## Gate Logic

For the full gate, active Critical and High findings block. Use the final
severity written by pass 3. Fixed, rejected, and superseded findings do not
count. Medium findings block only under the strict release gate. On hotfix
branches, active High findings become tracked debt and must be listed in the
readiness artifact.

Fail the gate when required review inputs are missing, when worktree state is
unknown, when verification evidence is missing, or when proof checks are
required but absent.

## `gate.yaml` Shape

```yaml
status: pass
policy: full
blocking_findings: []
warnings: []
missing_inputs: []
verification:
  commands: []
  proof:
    verify_run: ""
    audit_drift: ""
    build_proof: ""
    verify_proof: ""
ready_for_pr: true
```

Use `status: fail` when active findings, missing inputs, missing verification,
or failed proof checks block the selected branch policy.

## `synthesis.md` Shape

Summarize:

- Review scope.
- Required inputs reviewed.
- Worktree state, including untracked or unstaged files.
- Commands and proof checks observed.
- Confirmed blockers.
- Non-blocking warnings.
- Rejected or superseded findings worth mentioning.
- Residual risk.

## Readiness Artifact

Write `ready-for-pr.md` only when `gate.yaml` has `status: pass`,
`ready_for_pr: true`, no blocking findings, no missing required inputs, and no
missing required verification evidence.

## Read-Only Rule

This pass is read-only. If a fix is needed, stop with a failing gate and let a
separate implementation step address the finding.
