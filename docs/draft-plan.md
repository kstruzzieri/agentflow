# Draft Plan (`agentflow draft-plan`)

`draft-plan` compiles a machine-authored **task brief** plus a **workflow pack**
into an unlocked, `validate-plan`-valid `.agent/plan.lock.json` draft. It is the
bridge from adaptive workflow *policy* (a [recommendation](recommend-workflow.md)
over a task brief, and a [workflow pack](workflow-packs.md)) to an executable
Agentflow *plan*. It is **deterministic, provider-agnostic, and fail-closed**:
the same inputs always produce the same draft, nothing is inferred from a model,
and a brief that cannot be compiled into a bounded plan is rejected with a
machine-readable diagnostic rather than guessed at.

`draft-plan` never locks a plan. `lock-plan` stays the only authority for
validation and locking.

## Posture vs. catalog

The recommendation and the pack play different roles:

- The **recommendation** is the *lower-bound posture* — given this task, how
  careful must the workflow be (review depth, hunk attribution, review-run
  requirement, required capabilities, required gates)? See
  [recommend-workflow](recommend-workflow.md) for the five built-in archetypes.
- The **workflow pack** is the *executable catalog* — its profiles are real,
  representable workflows, and each profile's plan template provides the step
  topology the draft is built from.

`draft-plan` selects the **least-strict pack profile that satisfies** the
recommended posture, then hydrates that profile's template with the brief. The
final posture is the selected pack profile (a real, lockable workflow), with the
recommendation recorded as the rationale and lower bound. There is no brittle
`profile_id == archetype_id` coupling and no silent merge that invents a policy
no pack profile actually represents.

A profile *satisfies* the lower bound when, on every axis, it is equal or
stricter: its `required_gates` are a superset, its required capabilities are a
superset, its `review_depth` is equal or deeper, its `proof_policy` is equal or
stricter (`hunk_attribution` ordered `off < observe < enforce`;
`require_review_run` stricter when `true`), and its plan template actually
declares the required gates. Among satisfying profiles the least strict wins; a
tie at the minimum, or no match at all, fails closed.

## CLI

```
agentflow draft-plan (--brief <file> | --stdin) --workflow <pack> --objective <text>
    [--profile <id>] [--reason <text>] [--allow-missing-candidates]
    [--write] [--force] [--json] [--root <dir>]
```

- `--brief <file>` / `--stdin` — the task brief JSON source (mutually exclusive;
  one is required). The brief is the same closed schema `recommend-workflow`
  reads (`schemas/task-brief.schema.json`).
- `--workflow <pack>` — path to the workflow pack: a `.agentflow-pack` directory,
  its parent, or a `pack.json` file. Required.
- `--objective <text>` — the plan objective. A brief carries task *signals*, not
  human *intent*, so the objective is supplied here; a blank objective fails
  closed (`brief_too_vague`).
- `--profile <id>` — explicitly select a pack profile instead of letting the
  least-strict solver choose. A profile weaker than the recommendation is refused
  unless `--reason` is supplied.
- `--reason <text>` — rationale recorded in the linked contract; required to
  accept a weaker profile via `--profile`.
- `--allow-missing-candidates` (alias `--greenfield`) — downgrade
  `candidate_file_missing` from a fail-closed error to a warning so a plan can be
  drafted for files that do not exist yet (the normal case when planning new
  work). The missing files are still scoped into `allowed_files`. Path-escape
  rejection (`candidate_file_unsafe`) is never downgraded.
- `--write` — materialize the draft: write `.agent/plan.lock.json` (unlocked) and
  `.agent/workflow.contract.json`. Refuses to overwrite either without `--force`.
- `--json` — emit machine-readable JSON instead of a text summary.
- `--root <dir>` — repo root for candidate-file existence checks and `--write`
  targets (default `.`).

Exit codes: `0` success; `1` a fail-closed compile diagnostic (see below); `2`
invalid arguments (both/neither brief input). With `--json`, failures print
`{ "status": "invalid", "errors": [{ "code", "message" }] }`. A successful draft
carries a `warnings` array of `{ "code", "message" }` (empty unless a non-fatal
diagnostic such as a downgraded `candidate_file_missing` fired); in text mode
warnings print to stderr.

## What the draft contains

The draft is the selected profile's plan template, hydrated from the brief:

- `objective` from `--objective`.
- `validation_gates` = the template's gates ∪ the profile's required gates ∪ the
  brief's `validation_needs`.
- `allowed_files` = the template's allowed files ∪ the brief's `candidate_files`,
  always including `.agent/` so the Agentflow loop can write its artifacts. When
  the selected profile requires a review run (its `review_depth` is
  `spec_quality`/`deep`, or its `proof_policy.require_review_run` is `true`), the
  review-state path `docs/ai/state/` is added too, so the review artifacts that
  run will write are not flagged as out-of-scope drift.
- `risk_level` = the stricter of the template's risk and the brief's
  `declared_risk`.
- `steps` straight from the template (ids, order, and `depends_on` preserved).
  `draft-plan` does not synthesize step topology from the brief.
- a small `workflow` extension block — pointer/provenance only
  (`contract_path`, `workflow_pack`, `workflow_profile`, `recommended_profile`,
  `selection_mode`). The authoritative policy (capabilities, review depth, gates,
  proof policy) lives in the linked `.agent/workflow.contract.json`, not
  duplicated into the plan body. The plan-lock schema is `additionalProperties`,
  so the block is schema-compatible and `lock-plan` preserves it.

## Fail-closed diagnostics

| code | when |
|---|---|
| `invalid_brief` | the brief fails the task-brief schema |
| `brief_too_vague` | no objective, or a broad recommendation with no `candidate_files` to bound it |
| `candidate_file_missing` | a `bugfix`/`refactor`/`docs` brief lists a candidate file that does not exist under `--root` (downgraded to a warning by `--allow-missing-candidates`) |
| `decomposition_required` | the recommendation is broad but the selected template has fewer than two steps |
| `task_too_large` | `declared_size` is `xl` (or ≥ 20 candidate files) and the selected template has fewer than two steps |
| `no_satisfying_profile` | no pack profile meets the recommended posture |
| `ambiguous_profile` | several profiles are equally least-strict; pass `--profile` |
| `profile_weaker_than_recommended` | `--profile` names a profile weaker than the recommendation and no `--reason` was given |
| `invalid_pack` | the workflow pack manifest is invalid |

`feature` briefs skip candidate-file existence because their files are typically
new. Missing optional brief signals are never read as "safe" — that contract is
inherited from `recommend-workflow`. For `bugfix`/`refactor`/`docs` briefs that
deliberately name files about to be created, pass `--allow-missing-candidates`
(alias `--greenfield`) to downgrade the existence failure to a warning instead.

## Example

```bash
agentflow draft-plan \
  --brief examples/briefs/medium-feature.brief.json \
  --workflow examples/packs/agentflow-draft-demo \
  --objective "Add an events projection cache" \
  --write
```

This recommends `medium-feature`, selects the demo pack's least-strict satisfying
profile (also `medium-feature`), and writes an unlocked two-step draft plus the
linked workflow contract. Review and edit the draft, then `lock-plan` to lock it
and continue the normal Agentflow loop.

The `examples/briefs/` directory carries one brief per archetype
(`docs-only`, `small-bugfix`, `medium-feature`, `large-feature`, `high-risk`)
and `examples/packs/agentflow-draft-demo` carries one profile per archetype, so
each posture can be compiled end to end.

## Scope

Profile selection is the deterministic least-strict-satisfying solver over the
pack's existing profile shape; there are no pack selection hints or
decomposition hints, no step synthesis, and no built-in default pack. The
objective is supplied via `--objective` rather than extending the closed task
brief, so the same brief file works for both `recommend-workflow` and
`draft-plan`.
