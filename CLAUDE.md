# Claude Code Instructions

Use Agentflow for planned software work in this repository.

Agentflow is not published to PyPI. For development in this repository, install
from this source checkout; prebuilt single-file artifacts (`agentflow.pyz`,
`agentflow-mcp.pyz`) are published on the GitHub Releases page from `v0.4.0` on.
See `README.md` for the supported install paths:

- `uv tool install --editable /path/to/agentflow`
- `pipx install --editable /path/to/agentflow`
- `python3 -m pip install -e .`
- `PYTHONPATH=src python3 -m agentflow` for no-install usage from the repo root

Smoke-check availability before relying on the CLI:

```bash
agentflow --version
agentflow --help
```

If the console script is unavailable, use:

```bash
PYTHONPATH=src python3 -m agentflow --version
PYTHONPATH=src python3 -m agentflow --help
```

## Agentflow Task Loop

Use the same workflow contract as `docs/agent-workflow.md`:

```bash
agentflow init
# Populate .agent/plan.lock.json with the required plan contract:
# objective, scope, invariants, allowed_files, validation_gates, rollback_plan,
# and real steps. Include task files and .agent/ in allowed_files; then lock it.
agentflow lock-plan .agent/plan.lock.json
agentflow init-execution
STEP_ID=P1
VALIDATION_GATE="<matching step.validation entry>"
agentflow claim-step "$STEP_ID" --agent "$USER"
agentflow run --step "$STEP_ID" --gate "$VALIDATION_GATE" -- <validation-or-work-command>
agentflow record-file-change --step "$STEP_ID" --path <changed-path>
agentflow verify-step "$STEP_ID"
agentflow complete-step "$STEP_ID"
agentflow verify-run
agentflow audit-drift
agentflow build-proof
agentflow verify-proof
```

Rules for Claude Code:

- Treat `$STEP_ID` as a real step id from a valid `.agent/plan.lock.json`;
  `agentflow init` creates placeholder fields and an empty default step list,
  so `P1` only works after completing the plan contract and adding that step.
- Set `$VALIDATION_GATE` to the matching step `validation` entry unless the
  step uses structured command gates.
- Include `.agent/` in `allowed_files` when this loop writes Agentflow artifacts
  in the same worktree.
- Keep edits within the active user request and the claimed Agentflow step.
- Record every changed file with `agentflow record-file-change`. Hunk-level
  attribution is active: `record-file-change` fingerprints each diff hunk, and
  any edit inside an allowed file that you do not record fails drift under the
  default `enforce` policy (`proof_policy.hunk_attribution`). Record edits as you
  make them, not in a batch at the end.
- Run validation through `agentflow run` when the command should become proof.
- Do not mark a step complete before `agentflow verify-step` succeeds.
- Do not claim task completion before `agentflow verify-run`, `audit-drift`,
  `build-proof`, and `verify-proof` have been run or an explicit blocker is
  recorded.
- Do not expand work into review-cycle integration unless the user explicitly
  asks for that scope.
