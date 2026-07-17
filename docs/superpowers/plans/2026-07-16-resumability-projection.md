# Resumability Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose authoritative, actor-specific resumability state through the existing `next-action --json` and MCP surfaces.

**Architecture:** Add one read-only projection in `porcelain.py`, reuse `execution.py` lease/lifecycle helpers and `execution_coverage.py` gate evaluation, and thread agent identity through the existing CLI/MCP adapters. Preserve all current top-level JSON and text behavior.

**Tech Stack:** Python 3.11+ standard library, `unittest`, Agentflow CLI/MCP, generated JSON contract.

## Global Constraints

- Add no command, artifact, dependency, scheduler, lock, or lifecycle semantic.
- Never select an attempt when more than one open attempt exists.
- Never authorize owner-only recovery without an agent identity.
- Keep `fail-step` explicit break glass and non-automatic.
- Keep current `next-action` top-level fields and text output compatible.

---

### Task 1: Record the approved design and plan

**Files:**
- Create: `docs/superpowers/specs/2026-07-16-resumability-projection-design.md`
- Create: `docs/superpowers/plans/2026-07-16-resumability-projection.md`

**Interfaces:**
- Consumes: issue #20 Agent Brief and handoff prompt.
- Produces: the field names and safety rules used by Tasks 2 and 3.

- [ ] **Step 1: Add the design and implementation plan**

Document the additive `resumability` object, exact state-selection rule, lease
matrix, structured diagnostics, compatibility boundary, and tests.

- [ ] **Step 2: Run the planning gate**

Run:

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest tests.test_stability_policy -q
```

Expected: existing stability-policy tests pass.

- [ ] **Step 3: Record and complete Agentflow step P0**

Record both files, verify P0, complete P0, and commit:

```bash
git commit -m "docs: plan authoritative resumability projection"
```

### Task 2: Build the shared projection test-first

**Files:**
- Modify: `src/agentflow/execution_coverage.py`
- Modify: `src/agentflow/porcelain.py`
- Modify: `tests/test_porcelain.py`

**Interfaces:**
- Consumes: `read_step_state`, lease helpers, plan binding hash, attempt-scoped
  receipt readers, and existing gate matching.
- Produces: `resumability_projection(root, plan, agent_id, strict=False)` and
  the additive `Action.resumability` payload.

- [ ] **Step 1: Write failing behavioral tests**

Add tests that construct enforced/advisory roots and assert:

```python
projection = porcelain.resumability_projection(root, plan, agent_id="agent-a")
self.assertTrue(action(projection, "continue")["allowed"])
self.assertFalse(action(projection, "reclaim")["allowed"])
```

Also assert attempt-scoped receipt/gate filtering, terminal behavior, ambiguity,
malformed/incompatible diagnostics, and unchanged `.agent` file bytes.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest tests.test_porcelain -q
```

Expected: failures because the projection and additive field do not exist.

- [ ] **Step 3: Implement the minimum shared behavior**

Add gate result metadata inside the existing verifier loop, then build the
projection from existing readers. Use the exact selection guard:

```python
open_attempts = [
    (step_id, attempt_id)
    for step_id, step in state["steps"].items()
    for attempt_id in step.get("open_attempts", [])
]
if len(open_attempts) > 1:
    return diagnostic_projection("ambiguous_open_attempts", ...)
```

Derive action permissions from current policy, owner, deadline, expiry, and
agent identity. Do not mutate any ledger.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest tests.test_porcelain -q
```

Expected: all porcelain tests pass.

### Task 3: Expose and freeze the public surface

**Files:**
- Modify: `src/agentflow/cli.py`
- Modify: `src/agentflow/mcp_server.py`
- Modify: `src/agentflow/cli_contract.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_stability_policy.py`
- Modify: `docs/cli-contract.json`
- Modify: `docs/agent-workflow.md`
- Modify: `docs/golem-integration.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 2 projection.
- Produces: `next-action --agent`, MCP `next_action.agent`, and the documented
  additive JSON contract.

- [ ] **Step 1: Write failing CLI/MCP/contract tests**

Assert the CLI defaults `--agent` from `AGENTFLOW_AGENT_ID`, MCP forwards the
argument, `structuredContent.data.resumability` is present, and the public
contract accepts the nested object without changing existing fields.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest tests.test_cli tests.test_mcp_server tests.test_stability_policy -q
```

Expected: failures for the missing flag, MCP property, and contract member.

- [ ] **Step 3: Add the minimum adapter and documentation changes**

Pass `args.agent` into `porcelain.next_action`, add the optional MCP property
and argv flag, add `"resumability": "object"` to `JSON_OUTPUTS["next-action"]`,
regenerate `docs/cli-contract.json`, and document the actor-specific recovery
projection.

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest tests.test_cli tests.test_mcp_server tests.test_stability_policy -q
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest discover -s tests -q
```

Expected: 0 failures.

- [ ] **Step 5: Complete Agentflow proof**

Record every changed file, verify and complete P2, then run `verify-run`,
`audit-drift`, `build-proof`, and `verify-proof`.
