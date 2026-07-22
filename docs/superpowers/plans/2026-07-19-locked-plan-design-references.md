# Locked-Plan Design References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the minimum optional, locked-plan design-decision reference contract and deterministic proof projection needed to unblock go-llm#286.

**Architecture:** Extend the existing plan-lock traceability model with plan-level decision declarations and step-level IDs, validate it before lock, and reuse the existing semantic plan hash. Add a pure ordered proof projection under `coverage`, then independently recompute that one conditional key during proof verification; keep the CLI and MCP adapters as raw-step pass-throughs.

**Tech Stack:** Python 3 standard library, `unittest`, JSON Schema 2020-12, Agentflow CLI/MCP, Git/GitHub CLI.

## Global Constraints

- Set `PLAN_SCHEMA_VERSION` to exactly `0.4.0` and `PROOF_PACK_SCHEMA_VERSION` to exactly `0.11.0`; do not bump the package or any schema to `1.0.0`.
- Use stable IDs matching exactly `^[A-Za-z][A-Za-z0-9._-]{0,127}$`.
- Keep both plan fields optional; accepted `0.3.x` plans that omit them must remain valid.
- Reject either decision field under a recorded plan schema below `0.4.0` with one upgrade diagnostic and skip decision-content validation.
- Preserve declaration order for proof rows, reference order within each row, and plan step order within each row's `steps`.
- Normalize an omitted decision `references` member to `[]`; permit an explicitly empty references array.
- Omit `coverage.design_decisions` entirely when the locked plan declares no decisions.
- Keep mappings step-only: do not add `gates[].design_decision_ids` and do not synthesize decision data in `draft-plan`.
- Do not change `plan_binding_sha256` or `canonical_core`; tests must pin that their existing semantics already bind the new data.
- Do not add a sidecar, new ledger, new MCP tool, Golem prompt implementation, issue #5 soak work, or final 1.0 bump.
- Do not edit `docs/mcp.md`, `src/agentflow/mcp_server.py`, or `pyproject.toml`.
- Do not edit `CHANGELOG.md` in this branch: it is outside the locked plan's `allowed_files`, so touching it fails `audit-drift`. The `Unreleased` entries for the two schema bumps and the new public fields are deliberately deferred to the follow-up tracker-sync PR (the repo's established pattern, e.g. PRs #88, #96, #119); the draft PR body states this so reviewers read the omission as deliberate.
- Keep `tests/fixtures/compatibility/released-v0.4.0/**` and `tests/fixtures/compatibility/current-aggregated/**` unchanged.
- Issue #14 / PR #24 merges first. Rebase over then-current `origin/main`, preserve #14's aggregation validation, and rerun every focused, full, compatibility, and Agentflow proof gate before publication.
- Open a draft PR containing `Closes #13`; never mark it ready in this task.

---

## File and interface map

- `src/agentflow/validation.py`: owns declaration/reference validation and exports `validate_design_decision_traceability(plan: Dict[str, Any]) -> List[str]`.
- `src/agentflow/coverage.py`: owns the pure projection `build_design_decision_coverage(plan: Dict[str, Any]) -> Dict[str, Any]`.
- `src/agentflow/proof.py`: invokes both helpers during build and independently recomputes the projection during verification.
- `src/agentflow/cli.py`: gives `build-proof` a deliberate decision-traceability diagnostic before entering ledger handling.
- `src/agentflow/contracts.py`, `schemas/plan-lock.schema.json`, and `schemas/proof-pack.schema.json`: publish the two schema-minor changes.
- `src/agentflow/cli_contract.py` and generated `docs/cli-contract.json`: publish the optional raw-step member; no adapter implementation changes.
- `docs/agent-workflow.md`, `docs/golem-integration.md`, and `docs/schema-freeze-audit.md`: define authoring, selection, proof, omission, and version behavior.
- `tests/fixtures/compatibility/current-full/**`: carries one generated, decision-bearing current proof.

The ignored local `.agent/plan.lock.json` maps implementation to P1 (plan
contract), P2 (proof), P3 (public integration/docs), and P4 (post-#14 fixture
and final validation). Claim each step before edits. Run its final green gate
through `agentflow run`, record every changed file, then verify and complete
the step. Red tests run directly so expected failures do not become final proof
receipts.

### Task 1: Validate and bind the locked-plan contract

**Files:**
- Modify: `src/agentflow/contracts.py:11`
- Modify: `src/agentflow/validation.py:46-190,272-391`
- Modify: `schemas/plan-lock.schema.json:22,56-140`
- Modify: `tests/test_cli.py:42-143,197-635`
- Modify: `tests/test_schema_contracts.py:125-235,368-387`
- Modify: `tests/test_review.py:400-406`

**Interfaces:**
- Consumes: `parse_schema_version(value: str)`, `_TRACE_ID_RE`, and existing plan-lock `same_major` compatibility.
- Produces: `validate_design_decision_traceability(plan: Dict[str, Any]) -> List[str]` for Task 2.

- [ ] **Step 1: Claim Agentflow step P1**

Run:

```bash
PYTHONPATH=src python3 -m agentflow claim-step P1 --agent codex
```

Expected: `claimed P1 as A2`.

- [ ] **Step 2: Write failing lock, compatibility, schema, and hash tests**

Add a reusable valid extension fixture in `tests/test_cli.py`:

```python
def design_reference_plan() -> dict:
    plan = valid_plan()
    plan["schema_version"] = "0.4.0"
    plan["design_decisions"] = [
        {
            "id": "DD-1",
            "text": "Use the existing receipt ledger.",
            "references": ["docs/agent-workflow.md"],
        }
    ]
    plan["steps"][0]["design_decision_ids"] = ["DD-1"]
    return plan
```

Add these tests to `AgentflowCliTests`:

```python
def test_lock_plan_accepts_design_decision_traceability(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        self.assertEqual(run_agentflow(cwd, "init").returncode, 0)

        result = run_agentflow(
            cwd,
            "lock-plan",
            "--stdin",
            "--json",
            input_text=json.dumps(design_reference_plan()),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        locked = json.loads((cwd / ".agent/plan.lock.json").read_text(encoding="utf-8"))
        self.assertTrue(locked["locked"])
        self.assertEqual(locked["steps"][0]["design_decision_ids"], ["DD-1"])

def test_validate_plan_accepts_v03_without_design_references(self) -> None:
    plan = valid_plan()
    plan["schema_version"] = "0.3.0"

    self.assertEqual(validate_plan(plan), [])

def test_validate_plan_requires_v04_for_design_reference_fields(self) -> None:
    plan = design_reference_plan()
    plan["schema_version"] = "0.3.0"
    plan["design_decisions"] = []
    plan["steps"][0]["design_decision_ids"] = []

    self.assertEqual(
        validate_plan(plan),
        [
            "design decision fields require plan-lock schema_version "
            "0.4.0 or newer"
        ],
    )

def test_validate_plan_accepts_optional_references_and_unselected_decision(self) -> None:
    for decision in (
        {"id": "DD-1", "text": "References omitted."},
        {"id": "DD-1", "text": "References empty.", "references": []},
    ):
        with self.subTest(decision=decision):
            plan = design_reference_plan()
            plan["design_decisions"] = [
                decision,
                {"id": "DD-UNSELECTED", "text": "May remain unselected."},
            ]
            self.assertEqual(validate_plan(plan), [])

def test_validate_plan_rejects_design_decision_declaration_errors(self) -> None:
    cases = (
        (
            [],
            "design_decisions must contain at least one design decision",
        ),
        (
            "DD-1",
            "design_decisions must contain at least one design decision",
        ),
        (
            [{"id": "1DD", "text": "Invalid id."}],
            "design_decisions[1].id has invalid stable id: 1DD",
        ),
        (
            [{"id": "DD-1", "text": "   "}],
            "design_decisions[1].text must be a non-empty string",
        ),
        (
            [{"id": "DD-1", "text": "Valid.", "references": "ADR-1"}],
            "design_decisions[1].references must be a list",
        ),
        (
            [{"id": "DD-1", "text": "Valid.", "references": ["   "]}],
            "design_decisions[1].references must contain only non-empty strings",
        ),
        (
            [
                {"id": "DD-1", "text": "First."},
                {"id": "DD-1", "text": "Duplicate."},
            ],
            "duplicate design decision id: DD-1",
        ),
    )
    for decisions, expected in cases:
        with self.subTest(expected=expected):
            plan = design_reference_plan()
            plan["design_decisions"] = decisions
            self.assertIn(expected, validate_plan(plan))

def test_validate_plan_distinguishes_design_decision_reference_errors(self) -> None:
    cases = (
        (None, "steps[1].design_decision_ids must be a list"),
        (
            [],
            "steps[1].design_decision_ids must contain at least one "
            "design decision id",
        ),
        (
            ["   "],
            "steps[1].design_decision_ids must contain only non-empty strings",
        ),
        (
            ["DD-1", "DD-1"],
            "steps[1].design_decision_ids contains duplicate id: DD-1",
        ),
        (
            ["DD-MISSING"],
            "steps[1].design_decision_ids references unknown design decision "
            "id: DD-MISSING",
        ),
    )
    for references, expected in cases:
        with self.subTest(expected=expected):
            plan = design_reference_plan()
            plan["steps"][0]["design_decision_ids"] = references
            self.assertIn(expected, validate_plan(plan))
```

Update the current/future assertions in the same file:

```python
self.assertEqual(plan["schema_version"], "0.4.0")

plan["schema_version"] = "0.5.0"
self.assertIn(
    "plan-lock schema_version 0.5.0 is incompatible with supported 0.4.0",
    result.stderr,
)
```

Add schema parity tests in `tests/test_schema_contracts.py`:

```python
def test_plan_schema_documents_design_decision_traceability(self) -> None:
    schema = load_schema("plan-lock.schema.json")
    decision = schema["properties"]["design_decisions"]["items"]
    step_refs = schema["properties"]["steps"]["items"]["properties"][
        "design_decision_ids"
    ]

    self.assertEqual(sorted(decision["required"]), ["id", "text"])
    self.assertEqual(
        decision["properties"]["id"]["pattern"],
        schema["properties"]["requirements"]["items"]["properties"]["id"]["pattern"],
    )
    self.assertEqual(decision["properties"]["references"]["minItems"], 0)
    self.assertEqual(step_refs["minItems"], 1)
    self.assertTrue(step_refs["uniqueItems"])

def test_design_reference_plan_schema_version_is_bumped(self) -> None:
    schema = load_schema("plan-lock.schema.json")

    self.assertEqual(PLAN_SCHEMA_VERSION, "0.4.0")
    self.assertRegex(
        PLAN_SCHEMA_VERSION,
        schema["properties"]["schema_version"]["pattern"],
    )
```

Import `PLAN_SCHEMA_VERSION` with the existing contract constants. Extend
`test_plan_binding_hash_ignores_lock_metadata` in `tests/test_review.py`:

```python
def test_plan_binding_hash_includes_design_reference_semantics(self) -> None:
    plan = {
        "design_decisions": [
            {
                "id": "DD-1",
                "text": "Use the existing ledger.",
                "references": ["ADR-1", "ADR-2"],
            }
        ],
        "steps": [{"id": "P1", "design_decision_ids": ["DD-1"]}],
    }
    changed_text = json.loads(json.dumps(plan))
    changed_text["design_decisions"][0]["text"] = "Use a new ledger."
    changed_references = json.loads(json.dumps(plan))
    changed_references["design_decisions"][0]["references"].reverse()
    changed_step = json.loads(json.dumps(plan))
    changed_step["steps"][0]["design_decision_ids"] = []

    baseline = plan_binding_sha256(plan)
    self.assertNotEqual(baseline, plan_binding_sha256(changed_text))
    self.assertNotEqual(baseline, plan_binding_sha256(changed_references))
    self.assertNotEqual(baseline, plan_binding_sha256(changed_step))
```

- [ ] **Step 3: Run the focused tests to verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_cli.AgentflowCliTests \
  tests.test_schema_contracts.SchemaContractTests \
  tests.test_review.BuildReviewRunRecordTests -v
```

Expected: failures for unsupported `0.4.0`, missing decision validation/schema
members, and stale plan-version assertions. The new hash test may already pass;
that is the intended characterization of existing hash behavior.

- [ ] **Step 4: Implement the minimum validator**

Import `parse_schema_version` from `.versioning` in `validation.py` (it lives
in `src/agentflow/versioning.py:24`, not `contracts.py`; `proof.py` imports it
the same way). Replace the
criterion-specific reference-list mechanics with this parameterized helper,
then keep the criterion wrapper so all existing criterion diagnostics remain
byte-for-byte stable:

```python
def _validate_trace_refs(
    prefix: str,
    value: Any,
    known_ids: set[str],
    item_name: str,
    target_name: str,
    errors: List[str],
    non_blank: bool = False,
) -> None:
    if not isinstance(value, list):
        errors.append(f"{prefix} must be a list")
        return
    if not value:
        errors.append(f"{prefix} must contain at least one {item_name} id")
        return
    if not _is_non_empty_string_list(value) or (
        non_blank and any(not item.strip() for item in value)
    ):
        errors.append(f"{prefix} must contain only non-empty strings")
        return
    seen = set()
    for item_id in value:
        if item_id in seen:
            errors.append(f"{prefix} contains duplicate id: {item_id}")
        seen.add(item_id)
        if item_id not in known_ids:
            errors.append(f"{prefix} references unknown {target_name} id: {item_id}")


def _validate_criterion_refs(
    prefix: str,
    value: Any,
    criterion_ids: set[str],
    errors: List[str],
) -> None:
    _validate_trace_refs(
        prefix,
        value,
        criterion_ids,
        "criterion",
        "acceptance criterion",
        errors,
    )
```

Add the public validator:

```python
def _uses_design_decision_fields(plan: Dict[str, Any]) -> bool:
    if "design_decisions" in plan:
        return True
    steps = plan.get("steps")
    return isinstance(steps, list) and any(
        isinstance(step, dict) and "design_decision_ids" in step
        for step in steps
    )


def _design_decision_schema_is_legacy(plan: Dict[str, Any]) -> bool:
    recorded = plan.get("schema_version")
    if not isinstance(recorded, str):
        return False
    try:
        version = parse_schema_version(recorded)
    except ValueError:
        return False
    return (version.major, version.minor) < (0, 4)


def validate_design_decision_traceability(plan: Dict[str, Any]) -> List[str]:
    """Validate optional locked-plan design decisions and step references."""
    if not _uses_design_decision_fields(plan):
        return []
    if _design_decision_schema_is_legacy(plan):
        return [
            "design decision fields require plan-lock schema_version "
            "0.4.0 or newer"
        ]

    errors: List[str] = []
    decision_ids: set[str] = set()
    if "design_decisions" in plan:
        decisions = plan["design_decisions"]
        if not isinstance(decisions, list) or not decisions:
            errors.append(
                "design_decisions must contain at least one design decision"
            )
        else:
            for index, decision in enumerate(decisions, start=1):
                prefix = f"design_decisions[{index}]"
                if not isinstance(decision, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                decision_id = decision.get("id")
                if (
                    not isinstance(decision_id, str)
                    or not _TRACE_ID_RE.fullmatch(decision_id)
                ):
                    errors.append(
                        f"{prefix}.id has invalid stable id: {decision_id}"
                    )
                elif decision_id in decision_ids:
                    errors.append(f"duplicate design decision id: {decision_id}")
                else:
                    decision_ids.add(decision_id)
                text = decision.get("text")
                if not isinstance(text, str) or not text.strip():
                    errors.append(f"{prefix}.text must be a non-empty string")
                if "references" in decision:
                    references = decision["references"]
                    if not isinstance(references, list):
                        errors.append(f"{prefix}.references must be a list")
                    elif any(
                        not isinstance(reference, str) or not reference.strip()
                        for reference in references
                    ):
                        errors.append(
                            f"{prefix}.references must contain only "
                            "non-empty strings"
                        )

    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        errors.append("steps must be a list for design decision traceability")
        return errors
    for index, step in enumerate(steps, start=1):
        if isinstance(step, dict) and "design_decision_ids" in step:
            _validate_trace_refs(
                f"steps[{index}].design_decision_ids",
                step["design_decision_ids"],
                decision_ids,
                "design decision",
                "design decision",
                errors,
                non_blank=True,
            )
    return errors
```

Call `validate_design_decision_traceability(plan)` immediately after
`validate_requirement_traceability(plan)` in `validate_plan`.

- [ ] **Step 5: Publish plan schema 0.4.0**

Set:

```python
PLAN_SCHEMA_VERSION = "0.4.0"
```

Widen the plan JSON Schema pattern to `^0\.[0-4]\.[0-9]+$` and add these
optional members:

```json
"design_decisions": {
  "type": "array",
  "minItems": 1,
  "items": {
    "type": "object",
    "required": ["id", "text"],
    "properties": {
      "id": {
        "type": "string",
        "pattern": "^[A-Za-z][A-Za-z0-9._-]{0,127}$"
      },
      "text": {"type": "string", "minLength": 1},
      "references": {
        "type": "array",
        "minItems": 0,
        "items": {"type": "string", "minLength": 1}
      }
    },
    "additionalProperties": true
  }
}
```

Inside each step's `properties`, add:

```json
"design_decision_ids": {
  "type": "array",
  "items": {"type": "string", "minLength": 1},
  "minItems": 1,
  "uniqueItems": true
}
```

Do not add a gate property and do not touch `artifacts.py`.

- [ ] **Step 6: Run the P1 green gate through Agentflow**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m agentflow run \
  --step P1 \
  --gate "python3 -m unittest tests.test_cli tests.test_schema_contracts tests.test_review -q" \
  -- python3 -m unittest tests.test_cli tests.test_schema_contracts tests.test_review -q
```

Expected: receipt exits `0` and all three modules pass.

- [ ] **Step 7: Record, verify, complete, and commit P1**

Record each of the six changed files:

```bash
PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --path src/agentflow/contracts.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --path src/agentflow/validation.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --path schemas/plan-lock.schema.json
PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --path tests/test_cli.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --path tests/test_schema_contracts.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --path tests/test_review.py
PYTHONPATH=src python3 -m agentflow verify-step P1
PYTHONPATH=src python3 -m agentflow complete-step P1
git add src/agentflow/contracts.py src/agentflow/validation.py schemas/plan-lock.schema.json tests/test_cli.py tests/test_schema_contracts.py tests/test_review.py
git commit -m "feat: validate locked-plan design references"
```

Expected: P1 verifies and completes; the commit contains no hashing
implementation change.

### Task 2: Project and independently verify design decisions

**Files:**
- Modify: `src/agentflow/contracts.py:18`
- Modify: `src/agentflow/coverage.py:190`
- Modify: `src/agentflow/proof.py:28-45,412-519,910`
- Modify: `src/agentflow/cli.py:80-84,1114-1140`
- Modify: `schemas/proof-pack.schema.json:32-52,92-160`
- Modify: `tests/test_cli.py:853-897`
- Modify: `tests/test_proof.py:72-180,777-913,1088-1125,2342-2350,2649-2677`
- Modify: `tests/test_schema_contracts.py:208-235,368-495`

**Interfaces:**
- Consumes: `validate_design_decision_traceability(plan)` from Task 1.
- Produces: `build_design_decision_coverage(plan: Dict[str, Any]) -> Dict[str, Any]` and verifier-owned exact comparison of `coverage.design_decisions`.

- [ ] **Step 1: Claim Agentflow step P2**

Run:

```bash
PYTHONPATH=src python3 -m agentflow claim-step P2 --agent codex
```

Expected: a new open attempt for P2.

- [ ] **Step 2: Write failing pure projection and proof-integrity tests**

Import `build_design_decision_coverage` in `tests/test_proof.py`, then add:

```python
def test_build_design_decision_coverage_preserves_canonical_order(self) -> None:
    plan = {
        "design_decisions": [
            {
                "id": "DD-2",
                "text": "Second declaration is canonical first.",
                "references": ["ADR-2", "ADR-1"],
            },
            {
                "id": "DD-1",
                "text": "First identifier is canonical second.",
            },
        ],
        "steps": [
            {"id": "P2", "design_decision_ids": ["DD-1", "DD-2"]},
            {"id": "P1", "design_decision_ids": ["DD-2"]},
        ],
    }

    self.assertEqual(
        build_design_decision_coverage(plan),
        {
            "design_decisions": [
                {
                    "id": "DD-2",
                    "text": "Second declaration is canonical first.",
                    "references": ["ADR-2", "ADR-1"],
                    "steps": ["P2", "P1"],
                },
                {
                    "id": "DD-1",
                    "text": "First identifier is canonical second.",
                    "references": [],
                    "steps": ["P2"],
                },
            ]
        },
    )

def test_build_design_decision_coverage_omits_absent_contract(self) -> None:
    self.assertEqual(build_design_decision_coverage({"steps": []}), {})
```

Add this fixture helper beside `_traceability_proof_fixture`:

```python
def _design_decision_proof_fixture(self, tmp: str):
    root = Path(tmp)
    create_initial_artifacts(root)
    write_json(
        root / ".agent/plan.lock.json",
        {
            "schema_version": "0.4.0",
            "objective": "Verify design decision coverage integrity.",
            "steps": [
                {
                    "id": "P1",
                    "action": "Apply both decisions.",
                    "design_decision_ids": ["DD-1", "DD-2"],
                    "evidence_ids": [],
                },
                {
                    "id": "P2",
                    "action": "Apply the second decision.",
                    "design_decision_ids": ["DD-2"],
                    "evidence_ids": [],
                },
            ],
            "evidence_ids": [],
            "design_decisions": [
                {
                    "id": "DD-1",
                    "text": "Keep decisions in the locked plan.",
                    "references": ["ADR-1"],
                },
                {
                    "id": "DD-2",
                    "text": "Reuse proof coverage.",
                },
            ],
        },
    )
    proof = build_proof(root, root / ".agent/plan.lock.json")
    write_proof_metadata(root, proof)
    return root, proof
```

Add build/recompute tests:

```python
def test_build_proof_projects_design_decision_coverage(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _root, proof = self._design_decision_proof_fixture(tmp)

        self.assertEqual(
            proof["coverage"]["design_decisions"],
            [
                {
                    "id": "DD-1",
                    "text": "Keep decisions in the locked plan.",
                    "references": ["ADR-1"],
                    "steps": ["P1"],
                },
                {
                    "id": "DD-2",
                    "text": "Reuse proof coverage.",
                    "references": [],
                    "steps": ["P1", "P2"],
                },
            ],
        )

def test_build_proof_revalidates_design_decision_traceability(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root, _proof = self._design_decision_proof_fixture(tmp)
        plan_path = root / ".agent/plan.lock.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["steps"][0]["design_decision_ids"] = ["DD-MISSING"]
        write_json(plan_path, plan)

        with self.assertRaisesRegex(
            ValueError,
            "invalid design decision traceability.*DD-MISSING",
        ):
            build_proof(root, plan_path)

def test_verify_proof_recomputes_design_decision_coverage(self) -> None:
    mutations = ("text", "references", "steps", "order", "removed")
    for mutation in mutations:
        with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
            root, proof = self._design_decision_proof_fixture(tmp)
            rows = proof["coverage"]["design_decisions"]
            if mutation == "text":
                rows[0]["text"] = "Tampered text."
            elif mutation == "references":
                rows[0]["references"] = ["ADR-TAMPERED"]
            elif mutation == "steps":
                rows[0]["steps"] = ["P2"]
            elif mutation == "order":
                rows.reverse()
            else:
                proof["coverage"].pop("design_decisions")
            proof["core_sha256"] = core_sha256(proof)
            write_json(root / ".agent/proof-pack.json", proof)

            findings = verify_proof(root, root / ".agent/proof-pack.json")

            self.assertTrue(
                any(
                    finding["severity"] == "error"
                    and "design decision coverage is stale or tampered"
                    in finding["message"]
                    for finding in findings
                )
            )

def test_verify_proof_skips_design_coverage_without_plan(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root, _proof = self._design_decision_proof_fixture(tmp)
        (root / ".agent/plan.lock.json").unlink()

        findings = verify_proof(root, root / ".agent/proof-pack.json")

        self.assertFalse(
            any("design decision coverage" in finding["message"] for finding in findings)
        )

def test_verify_proof_hints_schema_growth_for_older_decision_proof(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root, proof = self._design_decision_proof_fixture(tmp)
        proof["schema_version"] = "0.10.0"
        proof["coverage"].pop("design_decisions")
        proof["core_sha256"] = core_sha256(proof)
        write_json(root / ".agent/proof-pack.json", proof)

        findings = verify_proof(root, root / ".agent/proof-pack.json")

        self.assertTrue(
            any(
                "design decision coverage is stale or tampered" in finding["message"]
                and "older schema version (0.10.0 < 0.11.0)" in finding["message"]
                for finding in findings
            )
        )
```

Add this beside the existing CLI requirement diagnostic test:

```python
def test_build_proof_rejects_invalid_design_decision_traceability(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        self.assertEqual(run_agentflow(cwd, "init").returncode, 0)
        plan = valid_plan()
        plan["schema_version"] = "0.4.0"
        plan["design_decisions"] = []
        plan["steps"][0]["design_decision_ids"] = ["DD-MISSING"]
        (cwd / ".agent/plan.lock.json").write_text(
            json.dumps(plan, indent=2),
            encoding="utf-8",
        )

        result = run_agentflow(cwd, "build-proof")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid design decision traceability", result.stderr)
        self.assertNotIn("invalid ledger", result.stderr)
        self.assertFalse((cwd / ".agent/proof-pack.json").exists())
```

Add proof schema parity in `tests/test_schema_contracts.py`:

```python
def test_proof_schema_documents_design_decision_coverage(self) -> None:
    schema = load_schema("proof-pack.schema.json")
    coverage = schema["properties"]["coverage"]["properties"][
        "design_decisions"
    ]
    decision = schema["$defs"]["designDecisionCoverage"]

    self.assertEqual(
        coverage["items"]["$ref"],
        "#/$defs/designDecisionCoverage",
    )
    self.assertEqual(
        sorted(decision["required"]),
        ["id", "references", "steps", "text"],
    )
    self.assertTrue(decision["properties"]["steps"]["uniqueItems"])
```

Update every current proof pin from `0.10.0` to `0.11.0` in
`tests/test_cli.py`, `tests/test_schema_contracts.py`, and
`tests/test_proof.py`. Change the deliberately future proof versions at
`tests/test_proof.py:2655,2670` from `0.11.0` to `0.12.0`.

- [ ] **Step 3: Run the focused tests to verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_cli.AgentflowCliTests \
  tests.test_proof.CoverageTests \
  tests.test_proof.ProofSchemaGateTests \
  tests.test_schema_contracts.SchemaContractTests -v
```

Expected: failures for the missing coverage helper, missing verifier projection,
missing proof schema member, stale proof version, and missing CLI diagnostic.

- [ ] **Step 4: Implement the pure ordered projection**

Add to `coverage.py`:

```python
def build_design_decision_coverage(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Project optional design decisions in canonical plan order."""
    decisions = plan.get("design_decisions")
    if not isinstance(decisions, list) or not decisions:
        return {}

    steps_by_decision: Dict[str, List[str]] = {}
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            continue
        for decision_id in step.get("design_decision_ids", []):
            if isinstance(decision_id, str):
                steps_by_decision.setdefault(decision_id, []).append(step["id"])

    return {
        "design_decisions": [
            {
                "id": decision["id"],
                "text": decision["text"],
                "references": list(decision.get("references", [])),
                "steps": list(steps_by_decision.get(decision["id"], [])),
            }
            for decision in decisions
        ]
    }
```

- [ ] **Step 5: Integrate build-time validation and coverage**

Import both new helpers in `proof.py`. Immediately after requirement
traceability validation in `build_proof`, add:

```python
design_errors = validate_design_decision_traceability(plan)
if design_errors:
    raise ValueError(
        "invalid design decision traceability: " + "; ".join(design_errors)
    )
```

Immediately after requirement coverage is merged, add:

```python
coverage.update(build_design_decision_coverage(plan))
```

In `cli.py`, import the validator and add this block after the existing
requirement precheck in `command_build_proof`:

```python
design_errors = validate_design_decision_traceability(plan)
if design_errors:
    print("invalid design decision traceability", file=sys.stderr)
    for error in design_errors:
        print(f"- {error}", file=sys.stderr)
    return 1
```

- [ ] **Step 6: Implement independent verification**

Extract the existing requirement verifier's growth suffix without changing its
text:

```python
def _coverage_growth_hint(proof: Dict[str, Any]) -> str:
    if not _recorded_schema_is_older(
        proof.get("schema_version"), PROOF_PACK_SCHEMA_VERSION
    ):
        return ""
    return (
        ": proof was built by an older schema version "
        f"({proof.get('schema_version')} < {PROOF_PACK_SCHEMA_VERSION}); "
        "rebuild with current Agentflow to re-verify"
    )
```

Use `growth_hint = _coverage_growth_hint(proof)` in
`_verify_requirement_coverage`, then add:

```python
def _verify_design_decision_coverage(
    root: Path, proof: Dict[str, Any]
) -> List[Dict[str, Any]]:
    recorded_coverage = proof.get("coverage", {})
    recorded = (
        {"design_decisions": recorded_coverage["design_decisions"]}
        if isinstance(recorded_coverage, dict)
        and "design_decisions" in recorded_coverage
        else {}
    )
    plan_path = root / ".agent/plan.lock.json"
    if not plan_path.exists():
        return []
    try:
        plan = read_json(plan_path)
        errors = validate_design_decision_traceability(plan)
        if errors:
            raise ValueError(
                "invalid design decision traceability: " + "; ".join(errors)
            )
        expected = build_design_decision_coverage(plan)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [
            {
                "severity": "error",
                "message": (
                    "design decision coverage could not be recomputed: "
                    f"{exc}"
                ),
            }
        ]
    if recorded != expected:
        return [
            {
                "severity": "error",
                "message": (
                    "proof design decision coverage is stale or tampered"
                    + _coverage_growth_hint(proof)
                ),
            }
        ]
    return []
```

Invoke it immediately after `_verify_requirement_coverage(root, proof)`.

- [ ] **Step 7: Publish proof schema 0.11.0**

Set:

```python
PROOF_PACK_SCHEMA_VERSION = "0.11.0"
```

Add this optional `coverage` property:

```json
"design_decisions": {
  "type": "array",
  "items": {"$ref": "#/$defs/designDecisionCoverage"}
}
```

Add this definition:

```json
"designDecisionCoverage": {
  "type": "object",
  "required": ["id", "text", "references", "steps"],
  "properties": {
    "id": {"type": "string"},
    "text": {"type": "string"},
    "references": {
      "type": "array",
      "items": {"type": "string"}
    },
    "steps": {
      "type": "array",
      "items": {"type": "string"},
      "uniqueItems": true
    }
  },
  "additionalProperties": true
}
```

Do not change `canonical_core`.

- [ ] **Step 8: Run the P2 green gate through Agentflow**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m agentflow run \
  --step P2 \
  --gate "python3 -m unittest tests.test_cli tests.test_proof tests.test_schema_contracts -q" \
  -- python3 -m unittest tests.test_cli tests.test_proof tests.test_schema_contracts -q
```

Expected: receipt exits `0`.

- [ ] **Step 9: Record, verify, complete, and commit P2**

Record these eight changed files, then close P2:

```bash
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path src/agentflow/contracts.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path src/agentflow/coverage.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path src/agentflow/proof.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path src/agentflow/cli.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path schemas/proof-pack.schema.json
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path tests/test_cli.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path tests/test_proof.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --path tests/test_schema_contracts.py
PYTHONPATH=src python3 -m agentflow verify-step P2
PYTHONPATH=src python3 -m agentflow complete-step P2
git add src/agentflow/contracts.py src/agentflow/coverage.py src/agentflow/proof.py src/agentflow/cli.py schemas/proof-pack.schema.json tests/test_cli.py tests/test_proof.py tests/test_schema_contracts.py
git commit -m "feat: project design decisions into proofs"
```

Expected: P2 verifies and completes.

### Task 3: Freeze the public step surface and document Golem consumption

**Files:**
- Modify: `src/agentflow/cli_contract.py:174`
- Modify: `docs/cli-contract.json`
- Modify: `tests/test_cli.py:1522-1550`
- Modify: `tests/test_mcp_server.py:182-215`
- Modify: `tests/test_stability_policy.py:20-60,157-185`
- Modify: `docs/agent-workflow.md:54-152,216-233`
- Modify: `docs/golem-integration.md:61-69,115-151`
- Modify: `docs/schema-freeze-audit.md:12-14`

**Interfaces:**
- Consumes: the raw locked step returned by existing `next_step(root, plan)`.
- Produces: an optional public `design_decision_ids: array|null` JSON member and deterministic Golem selection rules.

- [ ] **Step 1: Claim Agentflow step P3**

Run:

```bash
PYTHONPATH=src python3 -m agentflow claim-step P3 --agent codex
```

Expected: a new open attempt for P3.

- [ ] **Step 2: Pin CLI, manifest, and MCP passthrough behavior**

Extend `test_next_step_and_claim_step_cli`:

```python
plan = design_reference_plan()
plan["locked"] = True
(cwd / ".agent/plan.lock.json").write_text(
    json.dumps(plan, indent=2),
    encoding="utf-8",
)

next_payload = json.loads(next_result.stdout)
self.assertEqual(next_payload["id"], "P1")
self.assertEqual(next_payload["design_decision_ids"], ["DD-1"])
```

Add to `StabilityPolicyTests`:

```python
def test_next_step_contract_documents_optional_design_decision_ids(self) -> None:
    step = JSON_OUTPUTS["next-step"][0]

    self.assertEqual(
        step["keys"]["design_decision_ids"],
        "array|null",
    )
    self.assertJsonContract(
        "next-step",
        {
            "id": "P1",
            "action": "Implement.",
            "files": ["src/x.py"],
            "preconditions": [],
            "validation": ["python3 -m unittest"],
            "expected_diff": ["Feature exists."],
            "evidence_ids": [],
        },
    )
```

Replace the MCP text-only test with a parsed-data assertion:

```python
def test_next_step_returns_design_decision_ids_as_structured_data(self) -> None:
    root = Path(self._init_root())
    plan_path = root / ".agent/plan.lock.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.update(
        {
            "schema_version": "0.4.0",
            "objective": "Exercise MCP step passthrough.",
            "scope": ["Return the raw step."],
            "invariants": ["MCP does not reinterpret decisions."],
            "allowed_files": ["fixture.txt", ".agent/"],
            "validation_gates": ["manual inspection"],
            "rollback_plan": "Delete the fixture.",
            "steps": [
                {
                    "id": "P1",
                    "action": "Create fixture.",
                    "files": ["fixture.txt"],
                    "preconditions": [],
                    "expected_diff": ["Fixture exists."],
                    "validation": ["manual inspection"],
                    "evidence_ids": [],
                    "design_decision_ids": ["DD-1"],
                }
            ],
            "design_decisions": [
                {"id": "DD-1", "text": "Keep the adapter transparent."}
            ],
            "locked": True,
        }
    )
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    result = call(
        "tools/call",
        name="next_step",
        arguments={"root": str(root)},
    )["result"]

    self.assertFalse(result["isError"])
    self.assertEqual(
        result["structuredContent"]["data"]["design_decision_ids"],
        ["DD-1"],
    )
```

Import `Path` in `tests/test_mcp_server.py`.

- [ ] **Step 3: Run tests to verify the contract is RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_cli.AgentflowCliTests.test_next_step_and_claim_step_cli \
  tests.test_mcp_server.ToolsCallTests \
  tests.test_stability_policy.StabilityPolicyTests -v
```

Expected: the CLI/MCP characterization assertions pass through existing raw
step behavior, while the contract assertion and generated-manifest parity fail
because `design_decision_ids` is not declared yet.

- [ ] **Step 4: Add the optional CLI contract member and regenerate**

Change `JSON_OUTPUTS["next-step"]` so its object keys are:

```python
{
    "id": "string",
    "action": "string",
    "files": "array",
    "preconditions": "array",
    "validation": "array",
    "expected_diff": "array",
    "evidence_ids": "array",
    "design_decision_ids": "array|null",
}
```

Run:

```bash
python3 scripts/gen_cli_contract.py
```

Expected: `wrote docs/cli-contract.json`. Do not edit production MCP code.

- [ ] **Step 5: Document the contract**

Add an **Optional Design Decision References** section after requirement
traceability in `docs/agent-workflow.md`. Include this complete example:

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

State explicitly that declarations are optional but non-empty when present;
`id` is unique and stable; `text` and reference entries are non-blank;
references are opaque; step lists are optional but non-empty/unique/resolved;
unselected declarations are legal; gates and draft plans do not gain fields;
`0.3.x` omission remains accepted; and old lockers reject correctly labelled
`0.4.0` plans. Document the conditional ordered proof row, omitted-key
behavior, existing plan/core hashing, independent verification, and
`next-step --json` raw-step exposure.

Add **Optional Design Decision Selection** after requirement traceability in
`docs/golem-integration.md`. Include this deterministic algorithm:

```python
selected_ids = set(step.get("design_decision_ids", []))
selected_decisions = [
    decision
    for decision in plan.get("design_decisions", [])
    if decision["id"] in selected_ids
]
```

Specify that Golem emits schema `0.4.0` whenever it authors either decision
field, treats lock diagnostics as compiler feedback, uses declaration order as
prompt order, consumes the same order from proof coverage, and never creates
or patches a sidecar or proof metadata.

Update the two load-bearing rows in `docs/schema-freeze-audit.md`:

```text
Plan / PLAN_SCHEMA_VERSION: 0.4.0, plan-lock pattern ^0\.[0-4]\.[0-9]+$
Proof pack / PROOF_PACK_SCHEMA_VERSION: 0.11.0
```

Do not change `docs/mcp.md`.

- [ ] **Step 6: Run the P3 green gate through Agentflow**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m agentflow run \
  --step P3 \
  --gate "python3 -m unittest tests.test_cli tests.test_mcp_server tests.test_stability_policy -q" \
  -- python3 -m unittest tests.test_cli tests.test_mcp_server tests.test_stability_policy -q
```

Expected: receipt exits `0`; the generated manifest matches.

- [ ] **Step 7: Record, verify, complete, and commit P3**

Record all eight files:

```bash
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path src/agentflow/cli_contract.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path docs/cli-contract.json
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path tests/test_cli.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path tests/test_mcp_server.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path tests/test_stability_policy.py
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path docs/agent-workflow.md
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path docs/golem-integration.md
PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --path docs/schema-freeze-audit.md
PYTHONPATH=src python3 -m agentflow verify-step P3
PYTHONPATH=src python3 -m agentflow complete-step P3
git add src/agentflow/cli_contract.py docs/cli-contract.json tests/test_cli.py tests/test_mcp_server.py tests/test_stability_policy.py docs/agent-workflow.md docs/golem-integration.md docs/schema-freeze-audit.md
git commit -m "docs: publish design reference integration contract"
```

Expected: P3 verifies and completes.

### Task 4: Rebase after #14 and regenerate the current proof fixture

**Files:**
- Modify: `tests/test_proof_compatibility.py:105-137`
- Regenerate: `tests/fixtures/compatibility/current-full/.agent/**`
- Modify: `tests/fixtures/compatibility/current-full/PROVENANCE.md`
- Regenerate: `tests/fixtures/compatibility/current-full/MANIFEST.json`

**Interfaces:**
- Consumes: Tasks 1-3 and merged PR #24 aggregation validation.
- Produces: a checksum-pinned current proof that exercises
  `coverage.design_decisions` without changing historical fixtures.

- [ ] **Step 1: Confirm #14 is merged, then rebase**

Run:

```bash
gh pr view 24 --repo kstruzzieri/agentflow --json state,mergedAt,mergeCommit,url
git fetch origin
git rebase origin/main
```

Expected: PR #24 reports `state: MERGED` with non-null `mergedAt`; the rebase
completes. If #24 is still open, pause this task without claiming completion or
publishing #13. On conflicts in `proof.py`, `proof-pack.schema.json`,
`test_proof.py`, or `test_schema_contracts.py`, preserve #24's
major-neutral aggregation schema guard, 640-character bound, and tests while
retaining Task 2's separate decision-coverage code.

- [ ] **Step 2: Rerun every focused gate after the rebase**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_cli.AgentflowCliTests \
  tests.test_schema_contracts.SchemaContractTests \
  tests.test_review.BuildReviewRunRecordTests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_proof.CoverageTests \
  tests.test_proof.ProofSchemaGateTests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_stability_policy.StabilityPolicyTests \
  tests.test_mcp_server.ToolsCallTests -v
```

Expected: every focused module passes on rebased main.

- [ ] **Step 3: Claim P4 and write the failing compatibility assertion**

Run:

```bash
PYTHONPATH=src python3 -m agentflow claim-step P4 --agent codex
```

In `test_current_full_fixture_exercises_load_bearing_optional_blocks`, add:

```python
self.assertEqual(
    proof["coverage"]["design_decisions"],
    [
        {
            "id": "DD-FIXTURE",
            "text": "Keep design guidance inside the locked plan.",
            "references": ["docs/golem-integration.md"],
            "steps": ["P1"],
        }
    ],
)
```

Change the future-schema mutation from `0.11.0` to `0.12.0`.

- [ ] **Step 4: Run the compatibility test to verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_proof_compatibility.ProofCompatibilityMatrixTests -v
```

Expected: the current-full optional-block test fails because the existing
fixture has no `coverage.design_decisions`; released-v0.4.0 still verifies.

- [ ] **Step 5: Create an isolated scratch fixture root**

Create a fresh directory with `mktemp -d`, record its absolute path as
`FIXTURE_ROOT`, initialize Git there, then run:

```bash
FIXTURE_ROOT=$(mktemp -d /private/tmp/agentflow-current-full.XXXXXX)
git -C "$FIXTURE_ROOT" init
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow init --root "$FIXTURE_ROOT"
```

Expected: a new Agentflow scaffold under the scratch Git root. Use
`apply_patch` for the static files in the following steps.

- [ ] **Step 6: Author and lock the decision-bearing fixture plan**

Replace `$FIXTURE_ROOT/.agent/plan.lock.json` with:

```json
{
  "allowed_files": [".agent/", "docs/ai/", "fixture.txt"],
  "blocked_files": [],
  "design_decisions": [
    {
      "id": "DD-FIXTURE",
      "text": "Keep design guidance inside the locked plan.",
      "references": ["docs/golem-integration.md"]
    }
  ],
  "drift_budget": {
    "architecture_drift": "requires_approval",
    "formatting_drift": "minimal",
    "new_dependencies": 0,
    "test_weakening": 0,
    "unrelated_edits": 0
  },
  "evidence_ids": [],
  "invariants": [
    "All generated proof artifacts come from the current Agentflow CLI."
  ],
  "locked": false,
  "locked_at": null,
  "non_goals": ["Exercise aggregation; that has a separate fixture."],
  "objective": "Build a full-featured current proof compatibility fixture.",
  "requirements": [
    {
      "acceptance_criteria": [
        {
          "id": "AC-FIXTURE",
          "text": "The fixture command exits zero."
        }
      ],
      "id": "REQ-FIXTURE",
      "text": "The fixture records a successful command criterion."
    }
  ],
  "risk_level": "low",
  "rollback_plan": "Delete and regenerate the fixture root.",
  "schema_version": "0.4.0",
  "scope": [
    "Exercise criterion, design-reference, capability, runtime, review, amendment, and hunk proof blocks."
  ],
  "steps": [
    {
      "action": "Create and amend fixture.txt.",
      "criterion_ids": ["AC-FIXTURE"],
      "design_decision_ids": ["DD-FIXTURE"],
      "evidence_ids": [],
      "expected_diff": ["fixture.txt contains two generated lines."],
      "files": ["fixture.txt"],
      "gates": [
        {
          "criterion_ids": ["AC-FIXTURE"],
          "kind": "command",
          "run": ["/usr/bin/python3", "-c", "pass"]
        }
      ],
      "id": "P1",
      "preconditions": ["Plan and workflow contract are locked."],
      "validation": ["/usr/bin/python3 -c pass"]
    }
  ],
  "validation_gates": ["/usr/bin/python3 -c pass"]
}
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow lock-plan "$FIXTURE_ROOT/.agent/plan.lock.json" --json
```

Expected: `status: locked` and a locked `0.4.0` plan.

- [ ] **Step 7: Seed workflow, runtime, and review inputs**

Create the review input directories:

```bash
mkdir -p "$FIXTURE_ROOT/docs/ai/state/main"
```

Create `$FIXTURE_ROOT/workflow-contract.json`:

```json
{
  "proof_policy": {
    "hunk_attribution": "enforce",
    "require_review_run": false
  },
  "required_capabilities": [{"id": "tdd", "required": true}],
  "review_depth": "standard",
  "schema_version": "0.1.0",
  "selected_by": "operator",
  "selection_reason": "Exercise optional proof blocks.",
  "validation_policy": {"required_gates": ["focused"]},
  "workflow_pack": "compatibility.fixture",
  "workflow_profile": "full"
}
```

Create `$FIXTURE_ROOT/.agent/runtime.config.json`:

```json
{
  "schema_version": "0.3.0",
  "default_runtime": "local",
  "runtimes": {
    "local": {
      "adapter": "go-llm",
      "enabled": true,
      "capabilities": {"declared": ["chat"], "required": ["chat"]},
      "readiness": {
        "check": "command_exists",
        "command": "/usr/bin/python3"
      }
    }
  },
  "routes": {
    "worker": {
      "primary": "local",
      "fallbacks": [],
      "policy": "prefer_local",
      "requires": ["chat"],
      "allow_degraded": true
    }
  },
  "mcp_servers": {
    "fixture": {
      "enabled": true,
      "transport": "stdio",
      "declared_tools": ["verify_proof"],
      "readiness": {
        "check": "command_exists",
        "command": "/usr/bin/python3"
      }
    }
  }
}
```

Create `$FIXTURE_ROOT/docs/ai/config.json`:

```json
{
  "branch_modifiers": {"*": {"gate": "default"}},
  "gate_policy": {
    "default": {"blocks_on": ["high"], "warns_on": ["medium"]}
  }
}
```

Create `$FIXTURE_ROOT/docs/ai/state/main/findings-final.json`:

```json
{"findings": []}
```

Create `$FIXTURE_ROOT/docs/ai/state/main/review-manifest.json`:

```json
{
  "active_blocking": [],
  "artifacts": [{"path": "findings-final.json"}],
  "depth_profile": "standard",
  "findings": {
    "counts_by_severity": {},
    "counts_by_status": {},
    "index": []
  },
  "gate_status": "pass",
  "policy": "default",
  "review_run_id": "RR-20260719T120000Z-dd130001",
  "schema_version": "0.2.0",
  "state_dir": "docs/ai/state/main"
}
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow workflow-contract --root "$FIXTURE_ROOT" \
  --from-json "$FIXTURE_ROOT/workflow-contract.json"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow init-execution --root "$FIXTURE_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow record-capability --root "$FIXTURE_ROOT" \
  --id CAP1 --capability tdd --provider superpowers \
  --reason "Fixture records TDD capability."
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow runtime-status --root "$FIXTURE_ROOT" --record --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow record-review --root "$FIXTURE_ROOT" \
  --manifest "$FIXTURE_ROOT/docs/ai/state/main/review-manifest.json" --json
git -C "$FIXTURE_ROOT" config user.name "Agentflow Fixture"
git -C "$FIXTURE_ROOT" config user.email "fixture@agentflow.invalid"
git -C "$FIXTURE_ROOT" add .
git -C "$FIXTURE_ROOT" commit -m "fixture baseline"
```

Expected: the workflow contract is written, execution artifacts are
initialized, capability/runtime rows are recorded, and the review row binds to
the new plan SHA. The scratch repository has a baseline commit so hunk
attribution and drift compare execution changes against a real Git tree.

- [ ] **Step 8: Replay the initial and amendment attempts**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow claim-step P1 --root "$FIXTURE_ROOT" \
  --agent fixture --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow run --root "$FIXTURE_ROOT" --step P1 \
  --gate "/usr/bin/python3 -c pass" -- /usr/bin/python3 -c pass
```

Create `$FIXTURE_ROOT/fixture.txt` with exactly:

```text
initial generated content
```

Then run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow record-file-change --root "$FIXTURE_ROOT" \
  --step P1 --path fixture.txt --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow verify-step P1 --root "$FIXTURE_ROOT" --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow complete-step P1 --root "$FIXTURE_ROOT" --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow amend-step P1 --root "$FIXTURE_ROOT" \
  --agent fixture --reason "Exercise amendment receipts." \
  --reason-code operator_correction --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow run --root "$FIXTURE_ROOT" --step P1 \
  --gate "/usr/bin/python3 -c pass" -- /usr/bin/python3 -c pass
```

Append exactly this second line with `apply_patch`:

```text
amended generated content
```

Finish the amendment:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow record-file-change --root "$FIXTURE_ROOT" \
  --step P1 --path fixture.txt --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow verify-step P1 --root "$FIXTURE_ROOT" --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow complete-step P1 --root "$FIXTURE_ROOT" --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow verify-run --root "$FIXTURE_ROOT" --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow audit-drift --root "$FIXTURE_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow build-proof --root "$FIXTURE_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/agentflow-ticket-13-design-references/src \
  python3 -m agentflow verify-proof --root "$FIXTURE_ROOT"
```

Expected: both attempts verify, drift passes, and the generated proof verifies.
Do not edit `proof-pack.json` or `proof-pack.md` by hand.

- [ ] **Step 9: Install and checksum the generated current-full fixture**

Copy only `.agent`, `docs`, and `fixture.txt` from the scratch root into
`tests/fixtures/compatibility/current-full`; never copy its `.git` directory
or scratch `workflow-contract.json`.

Run:

```bash
cp -R "$FIXTURE_ROOT/.agent" tests/fixtures/compatibility/current-full/
cp -R "$FIXTURE_ROOT/docs" tests/fixtures/compatibility/current-full/
cp "$FIXTURE_ROOT/fixture.txt" tests/fixtures/compatibility/current-full/fixture.txt
```

Update `PROVENANCE.md` with the actual short output of
`git rev-parse --short HEAD`, add design-reference coverage to its capability
sentence, and list the replay sequence from Steps 6-8. Then run the existing
pin generator exactly:

```bash
python3 -c "import hashlib, json, pathlib; root = pathlib.Path('tests/fixtures/compatibility/current-full'); pins = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name not in {'MANIFEST.json', 'PROVENANCE.md'}}; (root / 'MANIFEST.json').write_text(json.dumps({'artifacts': pins}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

Expected: `MANIFEST.json` pins every non-provenance fixture file.

- [ ] **Step 10: Run compatibility and immutable-fixture checks**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_proof_compatibility.ProofCompatibilityMatrixTests -v
git diff --exit-code origin/main -- tests/fixtures/compatibility/released-v0.4.0
git diff --exit-code origin/main -- tests/fixtures/compatibility/current-aggregated
```

Expected: the entire proof compatibility matrix passes and both immutable
fixture diffs are empty.

- [ ] **Step 11: Run full validation through the P4 Agentflow gate**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m agentflow run \
  --step P4 \
  --gate "python3 -m unittest discover -s tests -q" \
  -- python3 -m unittest discover -s tests -q
```

Expected: the full suite exits `0`. If sandboxed socket or multiprocessing
tests fail on permissions, rerun this same gate outside the sandbox and use
that receipt.

- [ ] **Step 12: Record the actual fixture diff, verify, and complete P4**

Run `git diff --name-only` and record every changed path under
`tests/fixtures/compatibility/current-full/`, plus
`tests/test_proof_compatibility.py`, against P4 with one
`record-file-change` command per path. Then run:

```bash
PYTHONPATH=src python3 -m agentflow verify-step P4
PYTHONPATH=src python3 -m agentflow complete-step P4
PYTHONPATH=src python3 -m agentflow verify-run
PYTHONPATH=src python3 -m agentflow audit-drift
PYTHONPATH=src python3 -m agentflow build-proof
PYTHONPATH=src python3 -m agentflow verify-proof
```

Expected: P4 and the full run verify, drift passes, and both proof build and
proof verification succeed.

- [ ] **Step 13: Commit the generated fixture**

Run:

```bash
git add tests/test_proof_compatibility.py tests/fixtures/compatibility/current-full
git commit -m "test: refresh design reference compatibility proof"
```

Expected: the commit includes generated current-full changes only, never either
immutable fixture.

### Task 5: Review, push, and open the draft PR

**Files:**
- Review only: every file changed since `origin/main`
- External write: branch `codex/ticket-13-design-references` and one draft GitHub PR

**Interfaces:**
- Consumes: a green rebased branch and verified Agentflow proof from Task 4.
- Produces: a pushed branch and draft PR that closes issue #13 when merged.

- [ ] **Step 1: Run verification-before-completion checks**

Run:

```bash
git status --short --branch
git diff --check origin/main
git log --oneline --decorate origin/main..HEAD
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest \
  tests.test_cli.AgentflowCliTests \
  tests.test_schema_contracts.SchemaContractTests \
  tests.test_review.BuildReviewRunRecordTests \
  tests.test_proof.CoverageTests \
  tests.test_proof.ProofSchemaGateTests \
  tests.test_stability_policy.StabilityPolicyTests \
  tests.test_mcp_server.ToolsCallTests \
  tests.test_proof_compatibility.ProofCompatibilityMatrixTests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m agentflow verify-run
PYTHONPATH=src python3 -m agentflow audit-drift
PYTHONPATH=src python3 -m agentflow build-proof
PYTHONPATH=src python3 -m agentflow verify-proof
```

Expected: clean worktree, no whitespace errors, only intentional commits, every
focused/full test passes, and all Agentflow terminal gates pass. Review the
final diff against issue #13 and the approved design; correct any actionable
finding and rerun this entire step before publication.

- [ ] **Step 2: Push the rebased branch**

Run:

```bash
git push -u origin codex/ticket-13-design-references
```

Expected: the remote branch is created or updated successfully.

- [ ] **Step 3: Open the draft PR**

Run:

```bash
gh pr create \
  --repo kstruzzieri/agentflow \
  --base main \
  --head codex/ticket-13-design-references \
  --draft \
  --title "Add locked-plan design references" \
  --body "Closes #13

Adds optional locked-plan design decisions and step references, deterministic
hash-bound proof coverage with independent recomputation, public next-step/MCP
passthrough contracts, Golem integration guidance, and refreshed current proof
compatibility coverage.

Validation:
- focused plan/proof/CLI/MCP/compatibility tests
- full unittest suite
- Agentflow verify-run, audit-drift, build-proof, and verify-proof

This PR intentionally excludes a sidecar, Golem prompt implementation, issue #5
soak work, and the final 1.0 schema bump. CHANGELOG entries for the schema
bumps follow in the tracker-sync PR because CHANGELOG.md sits outside this
run's locked allowed_files."
```

Expected: GitHub returns a draft PR URL.

- [ ] **Step 4: Confirm draft state and #14 ancestry**

Run:

```bash
gh pr view --repo kstruzzieri/agentflow \
  codex/ticket-13-design-references \
  --json url,isDraft,baseRefName,headRefName,mergeable
git merge-base --is-ancestor origin/main HEAD
```

Expected: `isDraft: true`, base `main`, the requested head branch, and
`origin/main` is an ancestor. Do not run `gh pr ready`.
