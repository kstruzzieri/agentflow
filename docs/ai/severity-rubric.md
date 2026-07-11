# Agentflow Review Severity Rubric

Use this rubric for all four passes in the Agentflow review cycle. Severity is
about impact on Agentflow's workflow and proof guarantees. Confidence is a
separate field and must not be used as a substitute for impact.

## Severity Levels

`critical`: A flaw that can make Agentflow falsely prove incomplete work,
accept tampered proof metadata, allow unverified steps to complete, hide failed
validation behind a zero exit, corrupt required artifacts, or expose secrets
through receipts or proofs.

`high`: A contract, state-machine, schema, receipt, or CLI behavior regression
that blocks reliable execution or review but does not by itself prove a false
success.

`medium`: A maintainability, documentation, or completeness issue that
increases future review cost or leaves a plausible edge case uncovered.

`low`: Advisory polish, phrasing, examples, or non-blocking consistency issues.

## Decision Tree

1. Can this make `verify-run`, `audit-drift`, `build-proof`, or `verify-proof`
   pass when they should fail? Mark Critical.
2. Can this break a documented Agentflow contract, state transition, schema
   acceptance rule, or CLI exit-code expectation? Mark High.
3. Does this increase future maintenance cost, ambiguity, or missing coverage
   without breaking today's contract? Mark Medium.
4. Is this only presentation or preference? Mark Low.

## Agentflow Elevators

These triggers set minimum severity. Escalate above the floor when the
Critical condition is present.

| Trigger | Floor | Critical escalation |
| --- | --- | --- |
| Schema and validator drift in `schemas/`, `src/agentflow/validation.py`, or `src/agentflow/contracts.py` | High | Invalid artifacts can pass or valid locked plans/proofs can be rejected silently. |
| Proof-pack integrity or hash verification in `src/agentflow/proof.py` or proof schemas | High | Stale, missing, or tampered proof artifacts can verify successfully. |
| Receipt provenance, command/file receipts, or replay behavior in `src/agentflow/receipts.py` or `src/agentflow/execution_coverage.py` | High | Forged, mismatched, or replayed receipts can satisfy a proof gate. |
| Execution contract state-machine behavior in `src/agentflow/execution.py` or execution ledgers | High | A step can complete without verification, ownership, or dependency order. |
| CLI command behavior and exit-code contracts in `src/agentflow/cli.py` | High | A failed validation, drift audit, or proof check exits zero. |
| Dependency and packaging changes in `pyproject.toml` or `uv.lock` | Medium | Escalate to High if the no-runtime-dependency invariant or source-checkout install path changes. |

## Branch Behavior

`codex/*` and `feature/*` branches use the full gate: active Critical and High
findings block, Medium findings warn, and Low findings are advisory.

`hotfix/*` branches block on Critical only. Active High findings must be listed
as mandatory follow-up debt in `ready-for-pr.md` or the equivalent release
notes for the hotfix.

`release/*` branches use the strict gate: active Critical, High, and Medium
findings block until fixed, rejected, superseded, or downgraded below the
branch policy's block list. A downgraded finding still blocks when its final
severity remains Critical, High, or Medium.

`spike/*` branches are advisory only. Pass 4 should still summarize active
Critical and High findings clearly, but the review cycle does not block the
branch.

All other branches use the full gate.

## Confidence Handling

High confidence means the reviewer can cite exact lines, receipts, command
output, or artifact hashes that directly support the finding.

Medium confidence means the risk is likely based on evidence, but pass 3 must
verify nearby context before preserving the severity.

Low confidence means the risk is plausible. Pass 3 must confirm, downgrade, or
reject the finding before pass 4 makes a gate decision from it.

Do not lower severity because confidence is low. Instead, keep the severity tied
to impact and require the defender pass to settle the evidence.
