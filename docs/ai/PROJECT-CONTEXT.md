# Project context for AI reviewers

Orientation for an automated reviewer or agent working in this repository. It
covers what the project is, the invariants a change must not break, and where
the authoritative contracts live. It is deliberately short; every section points
at the document that actually governs.

Machine-readable index: [`ai-kit.yaml`](ai-kit.yaml).

## What Agentflow is

The standard-library-only `agentflow` Python CLI (from the `agentflow-proof`
distribution, version `0.4.0`, Python 3.11-3.13) supports plan-locked, auditable
agent work. An agent locks a plan, claims a step, runs validation through the
tool so the command becomes a receipt, records every file it changes, and builds
a proof pack that a third party can verify later without trusting the agent.

The product promise is that **a proof verifies later**. Most invariants below
exist to protect that one sentence.

- [`README.md`](../../README.md) — install paths and quick start.
- [`docs/agent-workflow.md`](../agent-workflow.md) — the workflow contract.
- [`docs/roadmap.md`](../roadmap.md) — what exists today.

## Invariants a change must not break

1. **Standard library only.** No runtime dependency, and no test dependency
   either. There is no YAML parser, no `jsonschema`, no `pytest` in this tree.
   Published JSON Schemas are validated by hand-written runtime validators; when
   you change one side you must change the other, and a test must pin them
   together.
2. **Tests are `unittest`.** CI runs
   `PYTHONPATH=src python3 -m unittest discover -s tests -v`. Verify with that
   exact command.
3. **Proofs stay verifiable.** `verify-proof` carries a bounded historical
   guarantee; the other artifact-loading surfaces do not. See
   [`docs/compatibility.md`](../compatibility.md) before touching any schema
   version or validator.
4. **Fail closed.** Ingestion points reject invalid working state rather than
   emitting a proof that overstates what happened. A guard that cannot establish
   a fact must say so, not assume it.
5. **Evidence over assertion.** The tool must never report a conclusion it has
   not established. This applies to the tool's own output as much as to the
   agent's.

## Current state: the v1 schema freeze

The load-bearing schemas are being frozen at 1.0 under
[issue #5](https://github.com/kstruzzieri/agentflow/issues/5). A 21-day soak
proves the shape stopped moving before the version number promises it did.

- [`docs/schema-freeze-audit.md`](../schema-freeze-audit.md) — the audit, the
  freeze set, and the rules of the soak.
- [`docs/schema-freeze-soak.json`](../schema-freeze-soak.json) — the recorded
  candidate.
- `scripts/check_schema_soak.py` — the guard CI runs on every build.

While the soak is active, **any shape or semantic change to a frozen path resets
the candidate**. If a change touches one, say so explicitly in review; it is a
schedule cost, not a style question.

## What review should weigh here

This repository already runs a four-pass review. [`config.json`](config.json) is
the machine policy the runner loads; [`config.yaml`](config.yaml) mirrors it for
humans and adds the pass order, depth profiles, and the path *elevators* that
raise the required depth and severity floor for proof-sensitive code. Report
findings in the shape defined by
[`finding-schema.md`](finding-schema.md), and grade them with
[`severity-rubric.md`](severity-rubric.md); the prompts are in
[`prompts/`](prompts).

Beyond the generic passes, the findings that matter most in this codebase are:

- A published schema and its runtime validator disagreeing.
- A guard, check, or proof field that reports something it did not verify.
- An ingestion point that accepts malformed input instead of failing closed.
- A change to a frozen path during an active soak.
- A new dependency, in `src/` or `tests/`.

Prefer a claim you reproduced over one you inferred. A finding that names the
command that demonstrates it is worth more here than a larger list.
