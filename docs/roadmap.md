# Agentflow Roadmap

Last updated: 2026-11-19

Agentflow `1.9.0` is a standard-library-only Python CLI for plan-locked,
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

Nothing yet. Items appear here as they merge.

## Next: 1.10.0 (planned)

- `agentflow setup <runtime>` writes or merges a runtime's config into a
  repository, from the same templates as the docs, and never overwrites an
  existing entry without `--force`. The templates move into the package, since
  an installed `setup` cannot read `examples/`
  ([#158](https://github.com/kstruzzieri/agentflow/issues/158)).
- A Claude Code plugin that bundles the MCP server, the Stop hook and the
  Agentflow task loop as a skill ([#159](https://github.com/kstruzzieri/agentflow/issues/159)).

## Shipped

Each release's full notes are in the [changelog](../CHANGELOG.md).

| Release | Date | In one line |
| --- | --- | --- |
| [1.9.0](../CHANGELOG.md#190---2026-11-19) | 2026-11-19 | Works from an installed package with Claude Code, Codex CLI and GitHub Copilot: `agentflow stop-gate`, copy-ready configs and a guide, checked in CI from the built wheel ([#164](https://github.com/kstruzzieri/agentflow/issues/164), [#156](https://github.com/kstruzzieri/agentflow/issues/156), [#157](https://github.com/kstruzzieri/agentflow/issues/157)). |
| [1.8.0](../CHANGELOG.md#180---2026-11-17) | 2026-11-17 | Once a step claim records the run ID, the checks detect a missing run-start row, and `next-action` points an unstarted run at `init-execution` ([#152](https://github.com/kstruzzieri/agentflow/issues/152), [#153](https://github.com/kstruzzieri/agentflow/issues/153)). |
| [1.7.0](../CHANGELOG.md#170---2026-11-16) | 2026-11-16 | Checks hold when the agent does not cooperate: drift covers committed work, the plan is bound to the run, and `claim-step` needs `init-execution` ([#138](https://github.com/kstruzzieri/agentflow/issues/138), [#139](https://github.com/kstruzzieri/agentflow/issues/139), [#148](https://github.com/kstruzzieri/agentflow/issues/148)). |
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
