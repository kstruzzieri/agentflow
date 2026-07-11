# Agentflow Roadmap

Last updated: 2026-07-11

Agentflow `0.4.0` is a standard-library-only Python CLI for plan-locked,
auditable agent work. It supports Python 3.11–3.13 and ships source plus
single-file CLI and MCP zipapps through GitHub Releases.

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

## Next Milestone: v1.0.0

Agentflow is feature-complete as of `0.4.0`. The v1.0.0 milestone is a
stability milestone, not a feature milestone: it turns the proof and execution
formats into promises. Progress is tracked in the
[v1.0.0 milestone](https://github.com/kstruzzieri/agentflow/milestone/1) and
the [tracking issue #11](https://github.com/kstruzzieri/agentflow/issues/11).

### Phase 1 — write down the promises

- [#3](https://github.com/kstruzzieri/agentflow/issues/3) `CHANGELOG.md` and
  release discipline (tag builds fail without a changelog entry).
- [#4](https://github.com/kstruzzieri/agentflow/issues/4) Public API surface
  and semver policy: which of the CLI, exit codes, JSON outputs, `.agent/`
  layout, MCP tools, and environment variables are covered by the guarantee.
- [#7](https://github.com/kstruzzieri/agentflow/issues/7) Platform support
  decision: extend the CI matrix or document support tiers.
- [#8](https://github.com/kstruzzieri/agentflow/issues/8) Security posture
  document covering the command-execution, artifact-publication, and loopback
  HTTP trust boundaries, including the checksum-not-signature limitation.
- [#10](https://github.com/kstruzzieri/agentflow/issues/10) Issue templates
  and code of conduct.

### Phase 2 — freeze

- [#5](https://github.com/kstruzzieri/agentflow/issues/5) Freeze the
  load-bearing schemas at 1.0 with a written compatibility policy:
  additive-only minors, and `verify-proof` 1.x verifies every proof built by
  any 1.y. The freeze follows a soak period with no schema bumps under real
  use.

### Phase 3 — distribution

- [#6](https://github.com/kstruzzieri/agentflow/issues/6) PyPI release with
  trusted publishing, sdist and wheel alongside the zipapps, and a
  cross-version verification test (0.4.0-built proof verified by 1.0).
- [#9](https://github.com/kstruzzieri/agentflow/issues/9) Runnable end-to-end
  examples for CI, MCP clients, workflow packs, and cross-worktree
  aggregation.

### Out of scope for v1.0

- PyInstaller single-binary packaging (deferred until a consumer needs
  Python-free machines; see `docs/packaging.md`).
- Cryptographic proof signing (candidate for 1.1; proof integrity remains
  checksum-based tamper evidence, stated plainly in the security posture doc).
- New workflow features.

Larger changes should begin as a GitHub issue with a concrete use case and
compatibility impact. This roadmap communicates direction, not a release
commitment.
