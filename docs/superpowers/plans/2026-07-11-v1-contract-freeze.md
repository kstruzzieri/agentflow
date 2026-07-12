# V1 Contract and Schema-Freeze Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issue #4 and the immediately actionable audit, fixture, and compatibility-harness work in issue #5 without crossing the 21-day soak gate.

**Architecture:** Public promises live in two policy documents and an auditable schema inventory. Runtime ingestion policy is centralized at the existing artifact/version boundary; `verify-proof` alone adds the historical exception. A table-driven CLI harness treats committed generated fixtures as black-box compatibility inputs.

**Tech Stack:** Python 3.11+ standard library, `unittest`, JSON/JSONL artifacts, Agentflow CLI, GitHub release pyz.

## Global Constraints

- Do not change a load-bearing schema constant to `1.0.0`.
- Preserve `tests/fixtures/proof-bundle` unchanged.
- Add no dependency.
- Generate the v0.4.0 fixture only with the release asset whose SHA-256 is `6617b33de632e174fffb7f3e869ab0793fff4df62c324a0cb017c9d5c5ed671c`.
- Keep README and CONTRIBUTING edits in the final commit.

---

### Task 1: Policy, design, and audit

**Files:** create the three policy/audit documents, design, this plan, and `tests/test_stability_policy.py`.

**Interfaces:** consumes issues #4/#5/#11 and the approved design; produces the precise policy used by Tasks 2–3.

- [x] Write a failing test that requires every public surface, the bounded proof-only guarantee, issue #14, and mechanical soak reset.
- [x] Run `python3 -m unittest tests.test_stability_policy -v`; expect missing-file failures.
- [ ] Add the minimum documents that satisfy the policy contract.
- [ ] Rerun the focused test; expect three passing tests.
- [ ] Record all P1 files, verify/complete P1, and commit `docs: define v1 stability and freeze policy`.

### Task 2: Executable compatibility policies

**Files:** `contracts.py`, `versioning.py`, `artifacts.py`, `execution.py`, `proof.py`, and focused version/schema tests.

**Interfaces:** produces `ARTIFACT_COMPATIBILITY_POLICIES` and a shared validator accepting `(actual, supported, artifact, policy)`; `verify_proof` consumes the shared diagnostic classification.

- [ ] Add failing tests proving every declared artifact has a policy, exact and same-major differ, generic readers reject incompatible rows, and newer proofs request an upgrade.
- [ ] Run the focused suite and confirm each test fails for missing behavior.
- [ ] Implement the smallest shared table/validator and route existing reader entry points through it.
- [ ] Run `python3 -m unittest tests.test_versioning tests.test_artifact_versioning tests.test_schema_contracts tests.test_proof -v`; expect success.
- [ ] Record files, verify/complete P2, and commit `feat: enforce declared artifact compatibility policies`.

### Task 3: Compatibility fixture matrix

**Files:** `tests/fixtures/compatibility/` and `tests/test_proof_compatibility.py`.

**Interfaces:** a fixture table supplies name, root, expected result, provenance, and optional diagnostic class to a subprocess invocation of `python -m agentflow verify-proof`.

- [ ] Add the table-driven test first and confirm it fails because generated fixture roots are absent.
- [ ] Download `agentflow.pyz` from release v0.4.0, verify its published digest, and generate—not hand-edit—the historical proof workflow.
- [ ] Generate a full current proof and an aggregated current proof, each with provenance/readme metadata.
- [ ] Run `python3 -m unittest tests.test_proof_compatibility -v`; expect the complete matrix to pass.
- [ ] Record files, verify/complete P3, and commit `test: add proof compatibility matrix`.

### Task 4: Shared links and complete verification

**Files:** `README.md` and `CONTRIBUTING.md` only.

**Interfaces:** exposes `docs/stability.md` from both contributor entry points.

- [ ] Add one concise stability-policy link to each file.
- [ ] Run `python3 -m unittest discover -s tests -v` through `agentflow run` and confirm zero failures.
- [ ] Record both files, verify/complete P4, then run `verify-run`, `audit-drift`, `build-proof`, and `verify-proof`.
- [ ] Update branch against current `main`, rerun the complete validation/proof chain, and report the final candidate commit while keeping the soak blocked on #14.
