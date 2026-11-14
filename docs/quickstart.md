# Quickstart

This walk-through takes an empty repository to a verified proof in about two
minutes. You type every command yourself here so you can see each part. In real
use, your coding agent runs them.

The test suite runs the commands in steps 1 to 5, in order, and compares the
sample output shown here with what the tool prints.

## Before you start

- Python 3.11 or newer.
- Git.
- A POSIX shell (Linux, macOS, or WSL or Git Bash on Windows).
- Agentflow itself:

  ```sh
  python3 -m pip install agentflow-proof
  ```

  If pip reports an externally managed environment, use
  `pipx install agentflow-proof` or a virtual environment.

## 1. Make an empty repository

Agentflow reads your changes through Git, so the folder must be a Git
repository. It needs no commits.

```bash
mkdir agentflow-demo
cd agentflow-demo
git init -q
```

## 2. Lock a plan

A plan says what the work is, which files it may touch, and how each step is
checked. `init` creates the `.agent/` folder; `lock-plan` validates the plan and
marks it locked. To change a locked plan, record the change with `amend-plan`.
Once the proof is built, any edit to the plan fails `verify-proof`.

```bash
agentflow init
agentflow lock-plan --stdin <<'JSON'
{
  "schema_version": "1.0.0",
  "objective": "Add a greeting file",
  "scope": ["Create hello.txt"],
  "non_goals": [],
  "invariants": ["Nothing else changes"],
  "allowed_files": ["hello.txt", ".agent/"],
  "blocked_files": [],
  "risk_level": "low",
  "drift_budget": {
    "unrelated_edits": 0,
    "new_dependencies": 0,
    "formatting_drift": "minimal",
    "architecture_drift": "requires_approval"
  },
  "evidence_ids": [],
  "validation_gates": ["test -s hello.txt"],
  "rollback_plan": "Delete hello.txt",
  "steps": [
    {
      "id": "P1",
      "action": "Write hello.txt",
      "files": ["hello.txt"],
      "preconditions": [],
      "expected_diff": ["hello.txt exists"],
      "evidence_ids": [],
      "validation": ["test -s hello.txt"]
    }
  ]
}
JSON
```

Two fields do most of the work:

- `allowed_files` is the boundary. A change to any other file fails the run.
- `steps` is the work, one entry per step. Each step names its files and the
  check (`validation`) that proves it is done.

The plan is piped in instead of saved as a file because a plan file left in the
repository would itself be a change that no step covers.

## 3. Do the work

Open the run, take step `P1`, make the edit, and tell Agentflow about it.

```bash
agentflow init-execution
agentflow claim-step P1 --agent demo
echo hello > hello.txt
agentflow record-file-change --step P1 --path hello.txt
```

Then run the step's check through Agentflow, so the command, its exit code,
and its output are kept as a receipt. The text after `--gate` must match the
step's `validation` entry.

```bash
agentflow run --step P1 --gate "test -s hello.txt" -- test -s hello.txt
```

## 4. Finish

`finish-step` verifies the step and marks it complete. `finish-run` audits the
whole run, builds the proof, and verifies it.

```bash
agentflow finish-step P1
agentflow finish-run
```

`finish-run` ends with:

```text
audit-drift ok
verify-run ok
build-proof ok
verify-proof ok
```

## 5. Check the proof again

Anyone with these files can check the proof later, without the chat that
produced it:

```bash
agentflow verify-proof
```

```text
warning: steps_without_support: warning
validation gates: 1 run by agentflow
proof verified
```

The warning is expected here: the plan cites no evidence for its step. Real
plans record evidence first. `agentflow verify-proof --strict` treats the
warning as a failure, so this demo proof does not pass strict verification.

`verify-proof` checks the proof files against the checksums recorded in the
proof. It does not look at your working files. `verify-run` and `audit-drift`
do: they compare your uncommitted changes with the receipts, so run them
before you commit.

## What you have now

Everything is in `.agent/`, as plain JSON and Markdown:

- `plan.lock.json`: the locked plan.
- `command-receipts.jsonl` and `file-receipts.jsonl`: the check you ran and
  the edit you recorded.
- `proof-pack.json` and `proof-pack.md`: the proof, for machines and for
  people.

Captured command output is stored there too, under `.agent/receipts/`. In your
own repository, add `.agent/` to `.gitignore` and commit that before you start
a run, unless you mean to publish proofs.
[Agent artifacts](agent-artifacts.md) covers what to commit and what to keep
local.

## Next

- [Agent workflow](agent-workflow.md): every phase, including amendments,
  evidence, and multi-step plans.
- [AGENTS.md](../AGENTS.md): the instructions this repository gives its own
  agents. Copy the task loop into yours.
- [MCP server](mcp.md) and [Stop hook](stop-hook.md): connect Agentflow to an
  agent runtime.
- [CI proof gate](ci-proof-gate.md): verify proofs in GitHub Actions.
