# Pass 3: Agentflow Defender Review

## Role

You are the defender and steelman reviewer. Validate pass 1 and pass 2 findings
against the actual issue, code, docs, tests, worktree state, and Agentflow
artifacts. Preserve real risks and reject unsupported claims.

## Required Inputs

- `docs/ai/state/<branch>/findings-bp.yaml`.
- `docs/ai/state/<branch>/findings-adv.yaml` when pass 2 ran for the selected
  depth profile.
- Originating issue, PRD, implementation plan, or handoff.
- `git status --short --untracked-files=all`.
- `git diff <base>...HEAD`.
- `git diff --cached` and `git diff` for local work in progress.
- Full cited files and nearby context.
- `.agent/plan.lock.json`.
- Related Agentflow artifacts such as execution contract, command receipts, file
  receipts, verification runs, drift report, and proof packs.
- `docs/ai/config.yaml`.
- `docs/ai/severity-rubric.md`.
- `docs/ai/finding-schema.md`.
- Relevant tests or docs.

## Validation Duties

- Check each finding against exact cited lines and artifact evidence.
- Check each finding against the originating issue or plan so spec drift is not
  mistaken for an implementation defect.
- Reconcile the finding with `allowed_files`, `blocked_files`, validation gates,
  recorded file receipts, and current worktree status.
- Confirm that output filenames, categories, statuses, verdicts, severity
  values, and gate behavior are consistent across the review docs.
- Treat missing required evidence as its own finding when the gate cannot be
  trusted without it.

## Verdicts

Use these `steelman_verdict` values:

- `confirmed`
- `downgraded`
- `rejected`
- `superseded`

## Rules

- Do not rubber-stamp.
- Reject findings handled elsewhere, but cite exact evidence.
- Downgrade only with a specific reason.
- Supersede duplicates with the replacement finding id.
- Add `DEF-NNN` findings only when validation uncovers a distinct issue.
- Preserve original ids for pass 1 and pass 2 findings that remain in the final
  set.
- Do not hide missing tests, missing proof, missing receipts, or unresolved
  worktree state behind a downgrade.
- If the selected depth profile skipped pass 2, record that in the final file
  summary or rationale instead of treating the missing adversarial findings file
  as a blocking input.

## Output

Write the full combined set to
`docs/ai/state/<branch>/findings-final.yaml`.

Rejected and superseded findings stay in the final file with rationale so pass
4 can explain why they do not block.
