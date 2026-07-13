# Review Checklist

Use this checklist before calling a task complete.

## Correctness

- The implementation satisfies the objective.
- Success criteria are covered by validation.
- Edge cases introduced during recon were handled or listed as risk.

## Scope

- Changed files are inside `allowed_files`.
- No `blocked_files` changed.
- No unrelated refactors or formatting churn were introduced.
- Dependency and architecture changes match the drift budget.

## Evidence

- Important claims have evidence ids.
- Evidence sources are specific enough to re-check.
- Open assumptions are resolved or explicitly accepted as residual risk.

## Verification

- Focused tests or inspections were run for each plan step.
- Broader checks were run when shared behavior changed.
- Failed commands were compressed into failure signatures before retrying.
- Every active review finding has a locked-plan `owning_step`, claim, and
  actionable `suggested_fix`; optional locations use the documented shape.
- Legacy review runs are treated as `amendment_ready: false`, never inferred.
- The review depth matches the selected workflow profile: when the
  `workflow.contract.json` `review_depth` is `spec_quality` or `deep`, a review
  run is recorded so `required_review_satisfied` passes. A `spec_quality`
  requirement is satisfiable by a lighter `review-manifest --depth-profile
  spec_quality` run (`findings-final.json` + `gate.yaml`); `deep` still requires
  the full four-pass artifact set.

## Maintainability

- Code follows local patterns.
- New abstractions remove real complexity.
- Documentation is updated when behavior or workflow expectations changed.

## Security

- New input paths validate untrusted data.
- Secrets are not logged or committed.
- Permissions, network access, and dependency changes are justified.

## Token Discipline

- Context was loaded for stated reasons.
- Large logs were summarized into failure signatures.
- The final response cites proof rather than confidence.
