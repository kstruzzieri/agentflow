# Resumability projection design

## Decision

Extend `next-action --json` with one additive `resumability` object and pass an
optional agent identity through the existing CLI and MCP tool. Keep the current
top-level fields and human-readable output unchanged.

The projection is derived at read time from the locked plan, execution
contract, step-run ledger, command/file receipts, and existing gate verifier.
It creates no artifact and executes no recovery action.

## Public shape

`resumability` contains:

- `contract`: plan and execution-contract schema versions and stable hashes;
- `step`: the single actionable step, or `null`;
- `attempt`: the single open attempt, or `null`;
- `lease`: policy, configured TTL/grace, deadline, and evaluated state;
- `receipts`: command and file receipts scoped to the reported attempt;
- `gates`: gate results produced by the existing verification semantics;
- `recovery_actions`: claim, continue, renew, reclaim, and break-glass fail
  decisions for the supplied agent identity; and
- `diagnostics`: structured errors that suppress recovery actions.

Each recovery action reports whether Agentflow permits it, why, and whether it
is an ordinary automatic action or explicit break glass. Missing identity never
permits owner-only actions.

## State selection and safety

The projection selects an attempt only when exactly one open attempt exists.
Multiple open attempts, an inconsistent attempt id, malformed JSON, an
incompatible artifact version, or an invalid execution contract produces
structured diagnostics and no recovery action.

Lease decisions mirror current behavior:

- enforced live leases permit only the owner to continue or renew;
- enforced expired finite leases permit the owner to renew and an identified
  agent to reclaim;
- advisory leases are labeled advisory and do not imply exclusive ownership;
- no-deadline attempts are never reclaimable; and
- terminal attempts are not resumable, renewable, or reclaimable.

`fail-step` remains visible only as `break_glass: true` and
`automatic: false`.

## Compatibility and verification

The existing `next-action` fields and text rendering stay unchanged. The MCP
tool still delegates to the CLI and only adds the optional `agent` argument.
The generated CLI contract documents the additive JSON member.

Focused tests cover owner/foreign/missing identity, live/expired/advisory and
no-deadline leases, attempt-scoped receipts and gates, terminal and ambiguous
state, malformed/incompatible artifacts, MCP forwarding, and byte-for-byte
read-only behavior. The full Python 3.13 suite remains the final gate.
