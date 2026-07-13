# v1 Platform Support and Executable Examples Design

## Goal

Deliver the v1.0 platform posture from issue #7 and four independently
executable, copyable issue #9 example packets that also serve as schema-soak
workloads.

## Decisions

- Linux and macOS are supported and exercised by CI on Python 3.11–3.13.
- Windows is best effort: the Python CLI may work, but Windows has no
  release-blocking CI leg and POSIX shell components are unsupported natively.
- `hooks/stop-gate.sh` and example commands that require `sh` are explicitly
  POSIX-only.
- The CI matrix change lands before the CI-proof example changes CI.
- Each example is self-contained under `examples/`, has a README and an
  automated smoke test, and invokes only shipped commands.

## Structure

1. The integration worktree owns the support-tier documentation, the portable
   CI matrix, shared smoke-test discovery, and final index links.
2. Four packet directories cover CI proof verification, MCP stdio clients,
   workflow-pack execution, and two-worktree ledger aggregation. Packet setup
   data is deliberately minimal and generated under temporary directories so a
   clean checkout can execute it without committed task receipts.
3. Tests validate documentation command paths and exercise each packet through
   the CLI. The CI-proof packet is also run by repository CI after the matrix
   update.

## Verification

- Run every README command in a fresh temporary checkout with Python 3.11+.
- Run each focused smoke test, then the full unit suite.
- Run the track's Agentflow execution receipts, drift audit, and proof chain.
- Record commands, Agentflow version, checkout commit, and outcomes in a soak
  evidence report for issue #5. Updating GitHub issues is outside this local
  implementation pass unless explicit credentials and authorization are
  available.

## Boundaries

No native Windows hook replacement, new workflow features, or changes to proof
schemas are in scope. A platform-independent defect found during macOS testing
is fixed only when it remains inside these boundaries; otherwise it is reported
as a blocker.
