# Agentflow Roadmap

Last updated: 2026-07-10

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

## Near-Term Priorities

1. Stabilize the `0.4.x` contracts and improve diagnostics without breaking
   existing proof bundles.
2. Reduce installation friction and evaluate a PyPI release once packaging and
   upgrade behavior are ready for general use.
3. Expand end-to-end examples for CI, MCP clients, workflow packs, and
   cross-worktree aggregation.
4. Continue security hardening at command-execution, artifact-publication, and
   local HTTP trust boundaries.

Larger changes should begin as a GitHub issue with a concrete use case and
compatibility impact. This roadmap communicates direction, not a release
commitment.
