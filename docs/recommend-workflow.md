# Recommend Workflow (`agentflow recommend-workflow`)

`recommend-workflow` maps a machine-authored **task brief** to a recommended
workflow posture — one of five built-in archetypes — with nearest-cheaper and
nearest-safer alternatives, a signal trace, and a ready-to-materialize workflow
contract candidate. It is **deterministic, provider-agnostic, and read-only**:
it never writes `.agent/`, never locks a plan, and never mutates a workflow
contract.

It answers "given this task, how careful should the workflow be?" The
recommendation is the input to materialization (`workflow-contract --from-json`,
or a future plan compiler). Agentflow declares the posture; the runtime may
still use specialized skills after the required capabilities are declared.

## The task brief

A brief is a small JSON object of explicit task signals — Agentflow does not
infer them from the repository. Schema: `schemas/task-brief.schema.json`
(closed; unknown fields are rejected).

| field | type | required | role |
|---|---|---|---|
| `schema_version` | string (`0.1.0`) | yes | version gate |
| `task_type` | `docs` \| `bugfix` \| `feature` \| `refactor` | yes | primary signal |
| `declared_risk` | `low` \| `medium` \| `high` | yes | risk signal |
| `security_sensitive` | boolean | no | escalator only |
| `candidate_files` | string[] | no | blast-radius / docs evidence |
| `blast_radius` | `isolated` \| `local` \| `cross_cutting` | no | explicit blast signal |
| `validation_needs` | string[] | no | seeds the contract candidate's gates |
| `declared_size` | `xs` \| `s` \| `m` \| `l` \| `xl` | no | user-declared size |

Example: [`examples/briefs/small-bugfix.brief.json`](../examples/briefs/small-bugfix.brief.json).

### Missing optional fields are `unknown`, never "safe"

An absent signal is never read as a safe value. Absent `candidate_files` is not
"no code files"; absent `blast_radius` is not "isolated"; absent `declared_size`
is not "small". Only an explicitly present, satisfied signal can de-escalate to
the cheaper `docs-only` / `small-bugfix` postures. `security_sensitive` is the
one exception — an escalator that defaults to `false` when absent, because
absence can only make a brief safer-looking, never riskier.

## Archetypes

The built-in catalog `agentflow-default`, ranked cheap (0) to safe (4). Each
archetype is expressed in the `workflow.contract.json` vocabulary, so the
recommendation projects straight into a contract candidate.

| rank | profile | review_depth | hunk_attribution | require_review_run | gates | capabilities |
|---|---|---|---|---|---|---|
| 0 | `docs-only` | none | observe | false | — | — |
| 1 | `small-bugfix` | light | enforce | false | unit-tests | — |
| 2 | `medium-feature` | standard | enforce | false | unit-tests | — |
| 3 | `large-feature` | deep | enforce | true | unit-tests | — |
| 4 | `high-risk` | deep | enforce | true | unit-tests, security-scan | security-review |

The linear rank makes "nearest cheaper" rank−1 and "nearest safer" rank+1.

## Adaptive review profiles (#74)

The selected profile's `review_depth` is enforced at proof time. When
`build-proof` reads `.agent/workflow.contract.json`, each depth contributes a
review-gate floor and a required-run bit, joined over the execution contract's
own review policy (strictness only ever rises, never falls):

| `review_depth` | review-gate floor | requires a review run |
|---|---|---|
| `none` | `ignore` | no |
| `light` | `warn` | no |
| `standard` | `warn` | no |
| `spec_quality` | `block` | yes |
| `deep` | `block` | yes |

The contract's `proof_policy.require_review_run` is OR'd in, so a profile can
demand a run at any depth. `build-proof` emits a `required_review_satisfied`
check and records `required_review_depth` in the hash-bound `review.policy`
block; `verify-proof` turns a missing required run into a hard error (or a
`--strict` error when the floor is `warn`). A `spec_quality` requirement is satisfiable by a lighter
`review-manifest --depth-profile spec_quality` run (`findings-final.json` +
`gate.yaml`); `deep` still requires the full four-pass artifact set
(`findings-final.json`, `findings-final.yaml`, `synthesis.md`, `gate.yaml`).

`review_depth=none` means *no required review run* — it does not suppress a
recorded review run's own gate. The execution contract's default `warn` gate
still applies to a recorded run that reports blocking findings.

## Classification rules

Ordered; first match wins. `broad_signal`, `all_docs`, and `bounded_small` are
derived predicates implemented in `agentflow.recommend`; the thresholds are
`SMALL_FILE_MAX = 5` and `LARGE_FILE_MIN = 20`:

1. `declared_risk == high` or `security_sensitive` → **high-risk**
2. `task_type == docs`, low risk, not security-sensitive, all candidate files
   are docs → **docs-only**
3. any broad signal (cross-cutting blast, size `l`/`xl`, or ≥20 files) →
   **large-feature**
4. `task_type == bugfix`, low risk, not security-sensitive, and explicitly
   bounded (≤5 known files, an explicit small size, an explicit isolated/local
   blast) → **small-bugfix**
5. otherwise → **medium-feature** (the safe floor)

Medium risk floors to `medium-feature` unless rule 3 escalates. An
under-specified bugfix (no explicit files/size/blast) falls to `medium-feature`,
not `small-bugfix`.

## CLI

```
agentflow recommend-workflow (--brief <file> | --stdin) [--json]
    [--selected-profile <id>] [--reason <text>]
```

- `--brief <file>` / `--stdin` — the brief JSON source (mutually exclusive; one
  is required).
- `--json` — emit the full report as JSON instead of text.
- `--selected-profile <id>` — operator override. If it differs from the
  recommendation, `--reason` is required.
- `--reason <text>` — override rationale, recorded in the report and the
  contract candidate's `selection_reason`.

Exit codes: `0` success; `1` invalid brief (not found, bad JSON, schema
errors); `2` invalid arguments (both/neither input, unknown profile, override
without reason). With `--json`, failures print `{ "status": "invalid",
"errors": [{ "code", "message" }] }`.

## Output

Text:

```
$ agentflow recommend-workflow --brief examples/briefs/small-bugfix.brief.json
recommended agentflow-default/small-bugfix
signals: task_type=bugfix, declared_risk=low, security_sensitive=false, candidate_files=2, blast_radius=local, declared_size=s, broad_signal=false, bounded_small=true
rationale: Recommended small-bugfix: a bounded, low-risk fix with few known files.
alternatives:
  cheaper docs-only: choose docs-only for a documentation-only, low-risk change
  safer medium-feature: choose medium-feature for a change that adds bounded new behavior
```

JSON (`--json`) adds `recommended`, `selected`, `signals`, `rationale`,
`alternatives`, `override` (null unless overridden), and a full
`workflow_contract_candidate`. The candidate is a complete, valid
`workflow.contract.json` object — it is **emitted, never written**. To
materialize it, extract the candidate object to a JSON file, then pass that file
to `workflow-contract --from-json`:

```bash
agentflow recommend-workflow --brief examples/briefs/small-bugfix.brief.json --json > recommendation.json
python3 -c 'import json; print(json.dumps(json.load(open("recommendation.json"))["workflow_contract_candidate"], indent=2, sort_keys=True))' > workflow-contract.json
agentflow workflow-contract --from-json workflow-contract.json
```

## Scope

Recommendation is rule-based over explicit brief fields. There is no model-based
classification, no scanning of the repository, and no scoring of custom `--pack`
profiles. Custom packs can still be selected explicitly after reviewing the
recommendation.
