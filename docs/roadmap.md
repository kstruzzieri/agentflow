# Agentflow Roadmap

Last updated: 2026-10-04

Agentflow `1.0.0` is a standard-library-only Python CLI for plan-locked,
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

## After 1.0

No next milestone is committed. Candidates, each starting as an issue with a
concrete use case and compatibility impact:

- PyInstaller single-binary packaging (deferred until a consumer needs
  Python-free machines; see `docs/packaging.md`).
- Cryptographic proof signing (proof integrity remains checksum-based tamper
  evidence, stated plainly in the security posture doc).
- New workflow features, within the additive-only minor policy.

This roadmap communicates direction, not a release commitment.
