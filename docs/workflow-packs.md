# Workflow Packs

A workflow pack is a small, local, inert data file that bundles reusable
Agentflow policy and templates. A pack is the *input library* for the adaptive
workflow track: a pack **profile** projects into a `.agent/workflow.contract.json`
(what workflow was selected), and a pack **plan template** projects into an
unlocked `.agent/plan.lock.json`.

Packs add no runtime dependencies and no behavior of their own. Agentflow reads
exactly one file per pack — `.agentflow-pack/pack.json` — and never reads,
stats, or executes any declared README or hook file. There is no `eval`, no
import of pack data, and no shelling out.

## Layout

```
.agentflow-pack/
  pack.json        # the only file Agentflow reads
  README.md        # optional, human-only; declared by name, never read
  hooks/           # optional hook templates; declared by name, never executed
    pre-commit.sh
```

## Manifest

- `schema_version` — `0.1.0`.
- `id`, `name`, `description` — non-empty strings.
- `plan_templates` — map of template id to an unlocked, valid plan-lock object.
- `profiles` — each carries the workflow-contract-shaped fields: `review_depth`,
  `required_capabilities`, `validation_policy.required_gates`,
  `proof_policy.{hunk_attribution, require_review_run}`, and a `plan_template`
  reference. A profile's `required_gates` must be a subset of its template's
  `validation_gates`.
- `hook_templates` / `readme` — optional, declared by safe relative path; never
  read or executed.

See `schemas/workflow-pack.schema.json` for the full reference.

## Commands

Inspect a pack (read-only; `--json` adds `manifest_sha256`):

```bash
agentflow pack inspect examples/packs/python-library-proof-gate
agentflow pack inspect examples/packs/python-library-proof-gate --json
```

Initialize a project from a pack profile (seeds an unlocked plan and writes the
workflow contract; refuses to overwrite an existing plan/contract without
`--force`):

```bash
agentflow init --pack examples/packs/python-library-proof-gate --profile default \
  --reason "Python library change"
```

Compile a task brief plus a pack into an unlocked draft plan, letting the
recommendation pick the least-strict satisfying profile instead of naming one by
hand (see [draft-plan](draft-plan.md)):

```bash
agentflow draft-plan --brief examples/briefs/medium-feature.brief.json \
  --workflow examples/packs/agentflow-draft-demo \
  --objective "Add an events projection cache" --write
```

## Example packs

- `examples/packs/python-library-proof-gate` — a stdlib-only Python library
  profile: enforced hunk attribution, a unit-test gate, and a TDD plan template.
- `examples/packs/openhands-hook-integration` — declares a `hooks/` template and
  README, demonstrating declared-but-never-executed hook templates.
- `examples/packs/agentflow-draft-demo` — one profile per recommend archetype
  (`docs-only` … `high-risk`), so [draft-plan](draft-plan.md) can compile a brief
  in each posture.

## Deferred

Pack registries and install lifecycle, inheritance/composition, selection hints,
hook execution, and MCP exposure are intentionally out of scope. They will be
added only if #70/#71/#74 prove the need.
