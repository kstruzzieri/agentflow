# Agentflow Roadmap

Last updated: 2026-11-14

Agentflow `1.6.0` is a standard-library-only Python CLI for plan-locked,
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

## On main, not yet released

- The README is a short landing page, with a tested
  [quickstart](quickstart.md), a [CI proof gate guide](ci-proof-gate.md), and
  a [documentation index](README.md). Reference material moved from the README
  into `docs/`, and the docs now state two enforcement limits plainly
  ([#141](https://github.com/kstruzzieri/agentflow/pull/141)).

## Next: 1.7.0 (planned)

The aim: the checks keep holding when the agent being checked does not
cooperate. The README work that opens this release is already on main.

- Drift checks cover committed work. A run records its starting commit, so
  `verify-run` and `audit-drift` compare everything changed since then instead
  of only uncommitted changes, and both accept `--base-ref` so CI no longer
  needs to rebuild a baseline by hand ([#139](https://github.com/kstruzzieri/agentflow/issues/139)).
- Plan edits are bound to the run. A change to the locked plan without a
  recorded amendment fails verification instead of passing unnoticed until the
  proof is built ([#138](https://github.com/kstruzzieri/agentflow/issues/138)).
- Small rough edges from a first-run pass: a hint when `.agent/` is not
  ignored, a one-line error outside a Git repository, `--require` keys in the
  help text, a clearer strict-mode label, and stuck-loop counting of refused
  commands ([#140](https://github.com/kstruzzieri/agentflow/issues/140)).
- Docs and release polish: follow-ups from the README change
  ([#142](https://github.com/kstruzzieri/agentflow/issues/142)), and a check of the README on PyPI plus refreshed package
  metadata before the release is tagged ([#143](https://github.com/kstruzzieri/agentflow/issues/143)).

Until #138 and #139 ship, both limits are stated in
[`docs/security-model.md`](security-model.md). Items move up to "On main, not
yet released" as they merge.

## Shipped

Each release's full notes are in the [changelog](../CHANGELOG.md).

| Release | Date | In one line |
| --- | --- | --- |
| [1.6.0](../CHANGELOG.md#160---2026-11-12) | 2026-11-12 | Bound confirmation for high-risk commands: tied to the exact command, expiring, used once ([#112](https://github.com/kstruzzieri/agentflow/issues/112), phase 1). |
| [1.5.0](../CHANGELOG.md#150---2026-11-12) | 2026-11-12 | `simulate-risk` previews a stricter risk policy; `verify-proof` judges each source by the bytes it hashed. |
| [1.4.0](../CHANGELOG.md#140---2026-11-10) | 2026-11-10 | The proof records how each gate was satisfied; `verify-proof --require` lets CI set the minimum policy. |
| [1.3.0](../CHANGELOG.md#130---2026-11-04) | 2026-11-04 | An edit from a stopped attempt can no longer pass unverified. |
| [1.2.2](../CHANGELOG.md#122---2026-11-03) | 2026-11-03 | Restores fail-closed resume in `next-action`. |
| [1.2.1](../CHANGELOG.md#121---2026-11-02) | 2026-11-02 | Fixes three cases where `next-action` pointed an agent the wrong way. |
| [1.2.0](../CHANGELOG.md#120---2026-10-29) | 2026-10-29 | Shows why a run is stuck: `next-step --explain` and recorded blockers. |
| [1.1.1](../CHANGELOG.md#111---2026-10-26) | 2026-10-26 | A step can no longer complete over a gate whose last run failed. |
| [1.1.0](../CHANGELOG.md#110---2026-10-18) | 2026-10-18 | Closes integrity and recovery gaps in the execution ledger and proof chain. |
| [1.0.0](../CHANGELOG.md#100---2026-10-04) | 2026-10-04 | The stability release: eight artifact schemas frozen, PyPI distribution, written compatibility promises ([milestone](https://github.com/kstruzzieri/agentflow/milestone/1), [#11](https://github.com/kstruzzieri/agentflow/issues/11)). |

## Later, not committed

- Host approval: an approver outside the agent confirms each high-risk
  command (`confirmation: "host"`, phase 2 of [#112](https://github.com/kstruzzieri/agentflow/issues/112), owned by Golem).
- PyInstaller single-binary packaging (deferred until a consumer needs
  Python-free machines; see `docs/packaging.md`).
- Cryptographic proof signing (proof integrity remains checksum-based tamper
  evidence, stated plainly in the security posture doc).
- New workflow features, within the additive-only minor policy.

Each of these starts as an issue with a concrete use case and compatibility
impact. This roadmap communicates direction, not a release commitment.
