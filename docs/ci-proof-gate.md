# CI proof gate

A proof pack shows what an agent did. A CI gate checks that proof on every push
or pull request, so a change cannot merge on the agent's word alone. CI can
also set how strict a run must be, from configuration the agent cannot edit.

This repository runs the gate in `.github/workflows/ci.yml`. A smaller job you
can copy is in [`examples/ci-proof/`](../examples/ci-proof/).

## What a plain checkout does not check

`verify-run` and `audit-drift` read changed files from `git status`. In a fresh
CI checkout nothing is uncommitted, so running them there checks the proof's
own files and says nothing about the source edits in the pull request (#139).

To check source edits against their receipts, CI has to rebuild the state the
agent worked in. This repository's workflow does that in its "Stage configured
proof bundle" step:

1. Copy the files from the pull request's base commit into a temporary
   directory and commit them there as the baseline.
2. Replace those files with the pull request's files, uncommitted.
3. Run the checks against that directory.

The copyable job in `examples/ci-proof/` skips this. It verifies a committed
proof bundle as it stands, which is the right check for a bundle and not a
check of source edits.

## The two checks

The workflow stages a proof root into a temporary Git repository, then runs:

```bash
agentflow verify-run --no-record --root "$PROOF_ROOT"
agentflow verify-proof --root "$PROOF_ROOT"
```

`verify-run --no-record` is for CI and other read-only checks: it validates the
execution ledger without appending a new entry to
`.agent/verification-runs.jsonl`, so `verify-proof` can still validate the
committed proof hashes afterward.

This repository's own workflow tests the code in the tree, so it runs the same
commands as `PYTHONPATH=src python3 -m agentflow ...`. In your repository,
install `agentflow-proof` in the job and call `agentflow`.

## Let CI set the minimum policy

How strict a run is comes from `.agent/execution.contract.json`, which the agent
doing the work can edit. On its own, `verify-proof` accepts a proof built under
`hunk_attribution: off` or `review_gate: ignore`. To let CI set the minimum
instead, pass `--require KEY=MIN`, once per key:

```bash
agentflow verify-proof --root "$PROOF_ROOT" \
  --require hunk_attribution=enforce --require review_gate=block
```

A setting below its floor fails with `policy_below_floor`, and so does a proof
with no execution contract. Keep the floor in CI configuration the agent cannot
change. The [Changelog](../CHANGELOG.md) lists the keys, and passing an unknown
key prints the valid ones.

A floor on `risk_policy`, `lease_policy` or `hunk_attribution` also requires
every run record, and a drift report from `audit-drift`, to name the contract
the proof hashes, so raising those settings after the work fails it.

Known limit: receipt requirements can still be tightened after a step completes
without failing the matching floor, and a proof can even verify with a failed
gate under the tighter receipt policy. See the
[Security model](security-model.md).

## Choosing the proof root and baseline

The workflow reads two variables. They belong to the workflow, not to the
`agentflow` command.

- `AGENTFLOW_PROOF_ROOT` selects the committed proof bundle. This repository
  points it at `tests/fixtures/proof-bundle` so CI can regression test the
  verifier itself without committing task-local root `.agent/` artifacts.
  Repositories that commit the active task's proof bundle should set it to `.`.
- `AGENTFLOW_PROOF_BASE_REF` selects the baseline commit. When it is unset and
  the proof root is `.`, a pull request uses the PR base commit as the temporary
  Git baseline before overlaying the current proof root, so `verify-run` still
  sees source edits that lack file receipts. Set it explicitly when the
  baseline should be a different commit.

```yaml
env:
  AGENTFLOW_PROOF_ROOT: .
  AGENTFLOW_PROOF_BASE_REF: ${{ github.event.pull_request.base.sha }}
```

[Agent artifacts](agent-artifacts.md) covers when to commit a proof bundle at
all.

## Expected failures

- Missing `.agent/proof-pack.json`: CI fails before verification starts.
- Stale proof metadata, hash mismatches, or modified receipt output files:
  `verify-proof` exits non-zero.
- Incomplete execution ledgers, drift failures, or unmapped changed files:
  `verify-run` exits non-zero.
