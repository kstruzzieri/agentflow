# Locked-plan design references design

## Decision

Add one optional plan-level `design_decisions` collection and one optional
step-level `design_decision_ids` collection. Agentflow validates and locks the
mapping, binds it through the existing semantic plan hash, and projects it into
proof coverage. No sidecar or second specification store is introduced.

This is a plan-lock schema-minor change from `0.3.0` to `0.4.0` and a proof-pack
schema-minor change from `0.10.0` to `0.11.0`. The plan bump is required because
an older `0.3.x` locker accepts unknown fields; leaving the new validation under
the old version would let it lock decision mappings it cannot validate.

## Locked plan contract

The optional shape is:

```json
{
  "schema_version": "0.4.0",
  "design_decisions": [
    {
      "id": "DD-1",
      "text": "Use the existing receipt ledger.",
      "references": ["docs/agent-workflow.md"]
    }
  ],
  "steps": [
    {
      "id": "P1",
      "design_decision_ids": ["DD-1"]
    }
  ]
}
```

`design_decisions` may be omitted. When present, it must be a non-empty array.
Each record requires:

- `id`: a unique stable ID matching the existing traceability pattern
  `^[A-Za-z][A-Za-z0-9._-]{0,127}$`;
- `text`: a non-blank string; and
- `references`: an optional array of non-blank strings. It may be omitted or
  empty. References are opaque documentation identifiers; Agentflow does not
  resolve paths or URLs.

`steps[].design_decision_ids` may be omitted. When present, it must contain at
least one non-blank string, contain no duplicate ID, and resolve every ID to a
declared design decision. A declared decision need not be selected by a step.

Decision references are deliberately step-only. There is no
`gates[].design_decision_ids` field: gates prove acceptance criteria, while
design decisions constrain implementation context.

## Versioning, validation, and hashing

Set `PLAN_SCHEMA_VERSION` and `schemas/plan-lock.schema.json` to `0.4.0`. A
current reader continues to accept `0.3.x` plans that omit both decision
fields. If a plan declares either `design_decisions` or any
`design_decision_ids` while recording a schema version below `0.4.0`,
validation emits one clear upgrade error and skips decision-content
validation. That check exists only in current readers and keeps the schema
label honest. Old readers fail closed through a separate, existing mechanism:
the `same_major` plan-lock policy rejects a recorded `0.4.0` version whose
minor exceeds the reader's supported minor, so a pre-`0.4.0` locker never
accepts a plan that correctly labels the extension.

The runtime validator follows the existing requirement-traceability pattern:
validate declarations first, collect valid IDs, then validate step references.
Locking already calls this validator, so malformed records, duplicate IDs,
empty decision declarations or step-reference lists, duplicate step references,
and dangling references fail before the plan is written as locked.

No hashing code changes. `plan_binding_sha256` already hashes every plan field
except `locked` and `locked_at`; changing decision text, reference data, or step
applicability therefore changes the semantic binding.

## Proof projection and verification

When decisions exist, `build-proof` adds one conditional
`coverage.design_decisions` array. Each decision appears once in plan
declaration order:

```json
{
  "coverage": {
    "design_decisions": [
      {
        "id": "DD-1",
        "text": "Use the existing receipt ledger.",
        "references": ["docs/agent-workflow.md"],
        "steps": ["P1"]
      }
    ]
  }
}
```

`references` preserves plan order and is normalized to an empty array when
omitted. `steps` lists selecting steps in plan order. Consumers select a
step's slice by filtering the ordered decision rows for that step ID; decision
declaration order is therefore the canonical prompt order.

When a plan has no declared decisions, the `design_decisions` coverage key is
omitted entirely. Existing accepted `0.3.x` plans retain their current coverage
shape and canonical-core semantics. A newly authored `0.4.0` plan naturally has
different locked-plan bytes and hashes even when it omits the optional fields.

The entire `coverage` object is already inside `canonical_core`, so the new
projection is hash-bound. `verify-proof` independently recomputes the exact
conditional projection from `.agent/plan.lock.json` and compares it with the
recorded value. Recomputing `core_sha256` cannot hide a changed, reordered, or
removed decision projection.

The recompute follows the requirement-coverage verifier's two established
semantics. First, when `.agent/plan.lock.json` is absent, the check is skipped
silently rather than failed: a proof is portable and self-contained, and
verification away from the originating working state must not report a missing
plan as tampering. Second, a recorded-versus-expected mismatch on a proof whose
recorded schema version is older than the current one is reported with the
schema-growth diagnostic, not a tamper claim. The version gate makes that path
nearly unreachable for this feature (an old builder rejects a `0.4.0` plan
before building a proof), but the verifier keeps the uniform diagnostics
contract.

Set `PROOF_PACK_SCHEMA_VERSION` and `schemas/proof-pack.schema.json` to
`0.11.0`. Keep the
released-v0.4.0 fixture immutable; its older plan and proof remain the
historical compatibility check. Regenerate the current full-feature fixture so
the live compatibility matrix exercises the new optional load-bearing block.

## Public integration surface

Document plan authoring, lock diagnostics, proof coverage, and omission
behavior in `docs/agent-workflow.md`. Document Golem's deterministic selection
rule in `docs/golem-integration.md`: read the locked step references, resolve
only declared decisions, and preserve proof declaration order without creating
or patching a sidecar. Update the `docs/schema-freeze-audit.md` plan-lock row,
whose recorded version and accepted-range pattern (`^0\.[0-3]\.[0-9]+$`) both
go stale at `0.4.0`.

`next-step --json` returns the selected raw step, so
`design_decision_ids` becomes an optional public JSON member automatically.
Add that optional member to the generated CLI contract and cover the existing
MCP `next_step` parsed-data passthrough. No new MCP tool or input is needed, and
`docs/mcp.md` has no `lock_plan` tool or full-plan schema inventory to update.

## Verification

Test-first coverage will pin:

- omitted fields and accepted legacy `0.3.x` plans;
- rejection of decision fields under a declared pre-`0.4.0` schema;
- empty, malformed, duplicate, and dangling declaration/reference cases;
- JSON Schema parity and lock-plan diagnostics;
- plan-binding changes for text, references, and step applicability;
- deterministic proof ordering, conditional omission, and tamper detection;
- `verify-proof` silent skip when `.agent/plan.lock.json` is absent;
- `next-step` CLI-contract and MCP passthrough behavior;
- the regenerated current-full compatibility fixture; and
- unchanged verification of the released-v0.4.0 fixture.

Focused validation covers plan validation/schema, hashing, proof, CLI contract,
MCP passthrough, and compatibility tests. The final gate runs the full suite and
the complete Agentflow proof loop.

## Non-goals and merge coordination

This change does not add gate-level decision mappings, draft-plan synthesis, a
new ledger, a sidecar, Golem prompt rendering, the issue #5 soak, or the final
`1.0.0` schema bump.

Issue #14 owns aggregation-validator major awareness and overlaps proof/schema
tests. Development may proceed in parallel, but #14 merges first. Before final
validation, rebase this branch over current `origin/main`, resolve the shared
files, and rerun all focused, full-suite, compatibility, and proof gates.
