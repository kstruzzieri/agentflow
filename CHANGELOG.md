# Changelog

All notable changes to Agentflow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Amendment-ready review-manifest v1.0 fields with locked-plan ownership
  validation, durable ledger/proof projection, and HTML proof viewing.
- Authoritative, actor-aware resumability state in `next-action --json` and
  its MCP mapping, including lease evaluation, attempt-scoped receipts and
  gates, structured diagnostics, and explicitly marked break-glass recovery.

### Changed

- Legacy review manifests remain recordable and verifiable but are explicitly
  marked non-amendment-ready; proof-pack schema is now 0.10.0 and review-run
  rows are 0.6.0. The manifest schema uses a new major because amendment-ready
  rows add required repair context.

### Fixed

- The `aggregate-ledgers --json` contract now declares the two payload shapes
  the runtime actually emits: analysis/collision (`status`, `sources`,
  `collisions`, `planned`) and successful write (`status`, `sources`,
  `written`). The previously documented single envelope (`source_count`,
  `output`, `dry_run`, `rewrites`) never matched runtime output, so no
  emitted payload changes.

## [0.4.0] - 2026-07-10

### Added

- Read-only runtime and MCP status evidence in proof packs.
- Single-writer step leases with renewal and stale-owner recovery.
- Cross-worktree ledger aggregation with collision detection and provenance.
- A public gate/ledger brand kit for the project and release artifacts.

## [0.3.0] - 2026-07-03

_Released from the pre-public repository history; no tag exists in this
repository, so this heading is intentionally unlinked._

### Added

- Portable execution contracts, step claims, command and file receipts,
  resumable verification, and provider-neutral handoffs.
- Deterministic command-risk screening, the dependency-free MCP server, and
  the POSIX Stop-hook enforcement gate.
- CI proof verification, review manifests, capability receipts, workflow
  packs, workflow recommendation, and draft-plan generation.
- Hunk-level drift attribution, the static HTML proof viewer, and the Golem
  integration guide.
- Single-file CLI and MCP zipapps with checksums and a tag-triggered GitHub
  release workflow.

### Changed

- Existing v0.2 proof artifacts remain valid when no execution contract exists.

[Unreleased]: https://github.com/kstruzzieri/agentflow/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kstruzzieri/agentflow/releases/tag/v0.4.0
