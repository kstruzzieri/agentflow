# Changelog

All notable changes to Agentflow are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `tests/fixtures/compatibility/legacy-0.3/`: a byte-exact snapshot of the
  0.3-era CI proof bundle, now the compatibility matrix's preserved-legacy
  fixture, so the historical `verify-proof` guarantee stays exercised after the
  1.0 transition regenerates the live `tests/fixtures/proof-bundle`.
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

- The 1.0 schema transition is landable again, caught by rehearsing the full
  test suite against a transitioned tree: the published proof-pack
  `bundle_version` pattern now admits the historical `0.4.0+` range and
  `1.0.x` (it is written from the same constant as `schema_version` but sits
  outside the soak guard's pattern exception), and the guard's transition
  window now lets the recorded transition regenerate the live CI proof bundle,
  since `verify-run` carries no cross-major promise. Both changes reset the
  soak candidate; the 21-day clock restarts on the corrected shape.
- `scripts/check_schema_soak.py` now reports an unresolvable
  `candidate_commit` or `transition_commit` with the field, the recorded SHA,
  and the squash-merge cause instead of `fatal: Needed a single revision`
  (#36).
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
