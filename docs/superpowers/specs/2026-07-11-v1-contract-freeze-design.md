# V1 contract and schema-freeze preparation design

## Decision

Agentflow will make `verify-proof` its only historical cross-version artifact
guarantee. Mutable `.agent/` state follows per-artifact compatibility policies
and receives no cross-major promise. CLI and MCP surfaces follow SemVer;
Python internals do not.

The historical horizon is bounded: Agentflow 1.x verifies proofs built by 0.4.0
or later and by earlier 1.x minors. Agentflow 2.0 may retire pre-1.0 proofs only
with the normal deprecation window and a migration tool.

## Components

1. `docs/stability.md` classifies CLI, JSON, MCP, environment, artifact,
   distribution, and Python surfaces and defines a one-minor/90-day window.
2. `docs/compatibility.md` defines proof-only history, working-state policies,
   schema evolution, upgrade fencing, and fixture coverage.
3. `docs/schema-freeze-audit.md` inventories each load-bearing schema from
   constant through writer/reader/test and defines a mechanical soak reset.
4. A compatibility-policy table in `contracts.py` becomes the single declared
   source for exact, same-major, or no-guarantee ingestion.
5. `verify-proof` gains an explicit schema gate whose forward-version failure
   says “newer schema; upgrade,” never “tampered.”
6. A table-driven harness verifies the preserved fixture, a genuine released
   v0.4.0 fixture, a full current fixture, and an aggregated fixture.

## Constraints

- No load-bearing schema constant changes to 1.0.0.
- The existing historical fixture remains byte-for-byte intact.
- Generated historical artifacts come from the checksum-pinned release pyz;
  they are not edited to look old.
- Schema defects receive dedicated linked issues. The discovered aggregation
  regex defect is #14 and blocks the soak.
- README and CONTRIBUTING links are a final isolated change for shared-doc
  integration.

## Verification

Behavior changes use red-green tests. Focused suites cover policy text,
versioning, artifact readers, proof diagnostics, schemas, and the compatibility
matrix. The full 1,108-test baseline is rerun, followed by `verify-run`,
`audit-drift`, `build-proof`, and `verify-proof` through Agentflow.
