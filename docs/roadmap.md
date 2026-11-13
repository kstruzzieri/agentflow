# Agentflow Roadmap

Last updated: 2026-11-12

Agentflow `1.5.0` is a standard-library-only Python CLI for plan-locked,
auditable agent work. It supports Python 3.11–3.13 and ships as the
`agentflow-proof` distribution on PyPI, as source, and as single-file CLI and
MCP zipapps through GitHub Releases.

## Available Today

- Locked plans, amendments, evidence, assumptions, and context receipts.
- Portable execution contracts with step claims, command receipts, file-change
  receipts, validation gates, and resumable state.
- Drift auditing with hunk-level attribution between recorded work and the
  current Git diff.
- Tamper-evident JSON and Markdown proof packs with strict verification.
- Deterministic command-risk screening and configurable confirmation policy.
- Provider-neutral handoffs, workflow packs, task briefs, workflow
  recommendations, and draft-plan generation.
- Review manifests, adaptive review policy, capability receipts, and CI proof
  verification.
- A dependency-free MCP server over stdio or loopback HTTP, plus a Stop hook
  enforcement gate.
- Single-writer leases, one-worker-per-worktree execution, and cross-worktree
  ledger aggregation.
- Static HTML proof reports and release zipapps for the CLI and MCP server.
- The eight artifact schemas frozen at `1.0.0` under the compatibility policy
  in [`docs/stability.md`](stability.md) and
  [`docs/compatibility.md`](compatibility.md).

## v1.0.0 (shipped 2026-10-04)

The v1.0.0 milestone was a stability milestone, not a feature milestone: it
turned the proof and execution formats into promises. The record lives in the
[v1.0.0 milestone](https://github.com/kstruzzieri/agentflow/milestone/1) and
the [tracking issue #11](https://github.com/kstruzzieri/agentflow/issues/11).

- Promises written down: `CHANGELOG.md` and release discipline
  ([#3](https://github.com/kstruzzieri/agentflow/issues/3)), the public API
  surface and semver policy
  ([#4](https://github.com/kstruzzieri/agentflow/issues/4)), platform support
  tiers ([#7](https://github.com/kstruzzieri/agentflow/issues/7)), the
  security posture ([#8](https://github.com/kstruzzieri/agentflow/issues/8)),
  and the public-project templates
  ([#10](https://github.com/kstruzzieri/agentflow/issues/10)).
- Freeze: the load-bearing schemas soaked and were stamped `1.0.0`
  ([#5](https://github.com/kstruzzieri/agentflow/issues/5)); `verify-proof`
  1.x verifies every proof built by any 1.y, and the 0.4.0-built fixture stays
  in the compatibility matrix.
- Distribution: PyPI release through trusted publishing with the wheel and
  sdist alongside the zipapps
  ([#6](https://github.com/kstruzzieri/agentflow/issues/6)) and runnable
  end-to-end examples
  ([#9](https://github.com/kstruzzieri/agentflow/issues/9)). The bare
  `agentflow` PyPI name is pursued separately under PEP 541; if it transfers,
  only `project.name` changes.

## v1.4.0 (shipped 2026-11-10)

- The proof records how each validation gate was satisfied (run by Agentflow,
  replayed, self-reported, inspection evidence, or missing), `verify-proof`
  recomputes that record and prints a one-line summary, and the docs define
  each command-receipt provenance value
  ([#110](https://github.com/kstruzzieri/agentflow/issues/110)). The
  proof-pack schema moves to `1.1.0`, the first additive minor bump under the
  compatibility policy; Agentflow 1.3 and older refuse a `1.1.0` proof and ask
  for an upgrade.
- `verify-proof --require KEY=MIN` lets the verifier, not the agent's own
  execution contract, set the minimum policy a proof must meet: hunk
  attribution, the review gate, the command risk policy, leases, and the
  receipt requirements. The MCP `verify_proof` tool takes the same list as
  `require`
  ([#111](https://github.com/kstruzzieri/agentflow/issues/111)). No schema
  changes.
- A floor on `risk_policy`, `lease_policy` or `hunk_attribution` also checks
  the contract the work ran under: every step event, command and file
  receipt, verification run and the drift report records the hash of the
  contract it was decided under, and `verify-proof --require` on those keys
  fails unless each names the hash-verified contract
  ([#117](https://github.com/kstruzzieri/agentflow/issues/117)). The
  step-runs, command-receipts, file-receipts, verification-runs and
  drift-report schemas move to `1.1.0`; Agentflow 1.3 and older reject the
  new records in working state.
- Every replay of an attested gate (`replay-gates`, the `--replay` options of
  `verify-step`, `finish-step` and `verify-proof`, and MCP `replay`) applies
  the risk policy and command timeout that `run` applies, and refuses to run
  under an invalid execution contract, so `replay-gates --record` can no
  longer turn a command the policy would block into a trusted receipt
  ([#119](https://github.com/kstruzzieri/agentflow/issues/119)). No schema
  changes; `verify-proof --replay` now fails on a proof whose contract blocks
  one of its high-risk attested gates.

## v1.5.0 (shipped 2026-11-12)

- `agentflow simulate-risk` re-checks every recorded command against a
  proposed `risk_policy` (`--risk-policy`) and, optionally, a proposed plan
  (`--plan`), and reports which commands it would have blocked or held for
  confirmation, grouped by risk category, before a team adopts the stricter
  setting. Commands Agentflow did not start itself (self-reported or
  external) are marked advisory, and receipts whose recorded risk no longer
  matches today's analyzer are listed separately as analyzer drift. It writes
  nothing and exits 0 whatever it finds, so it is a report, not a gate
  ([#113](https://github.com/kstruzzieri/agentflow/issues/113)). No schema
  changes.
- `verify-proof` judges every hashed source by the bytes it hashed. The checks
  after the hash check (criterion, design-decision and validation-gate
  coverage, receipt outputs, review integrity and `--replay`) no longer read
  those files from disk again, so a file swapped while a proof is being
  verified cannot change the verdict, and a source that failed its hash is
  reported instead of read again
  ([#126](https://github.com/kstruzzieri/agentflow/issues/126)). No schema
  changes; output is unchanged for a bundle whose files do not change during
  verification.

## On main, not yet released

- Bound self-confirmation for high-risk commands. With
  `command_policy.confirmation` set to `bound` (it requires
  `risk_policy: "require-confirmation"`), `--confirm-risk` and
  `AGENTFLOW_CONFIRM_RISK=1` no longer let a high-risk command run. A separate
  `agentflow confirm-command --step STEP -- <argv>` records a confirmation tied
  to the step, the exact argv, the plan and the contract; it expires (15
  minutes by default, 24 hours at most) and `run` consumes it once. The proof
  records each confirmation, `verify-proof` checks it against the ledger and
  receipts, and `verify-proof --require confirmation=bound` makes it a floor.
  This proves binding, not who confirmed
  ([#112](https://github.com/kstruzzieri/agentflow/issues/112), phase 1).
  New schema versions: execution contract 1.1.0, command receipts and proof
  packs 1.2.0, and the new command-confirmations ledger 1.0.0. Agentflow 1.5
  and older cannot read the receipts and proofs this version writes, and `run`
  now refuses a contract that is present but unreadable instead of falling
  back to defaults.

## After 1.0

Open issue that tightens who approves risky commands:

- Host approval: an approver outside the agent confirms each high-risk
  command (`confirmation: "host"`, phase 2 of
  [#112](https://github.com/kstruzzieri/agentflow/issues/112), owned by
  Golem). Phase 1, binding the confirmation to the exact command, is on main.

No next milestone is committed. Other candidates, each starting as an issue
with a concrete use case and compatibility impact:

- PyInstaller single-binary packaging (deferred until a consumer needs
  Python-free machines; see `docs/packaging.md`).
- Cryptographic proof signing (proof integrity remains checksum-based tamper
  evidence, stated plainly in the security posture doc).
- New workflow features, within the additive-only minor policy.

This roadmap communicates direction, not a release commitment.
