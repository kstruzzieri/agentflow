# CI proof gate

A proof pack shows what an agent did. A CI gate checks that proof on every push
or pull request, so a change cannot merge on the agent's word alone. CI can
also set how strict a run must be, from configuration the agent cannot edit.

This repository runs the gate in `.github/workflows/ci.yml`. A smaller job you
can copy is in [`examples/ci-proof/`](../examples/ci-proof/).

## Checking the source edits

`verify-run` and `audit-drift` compare everything changed since a baseline
commit, committed or not (#139). In a fresh CI checkout nothing is
uncommitted, so name the baseline with `--base-ref`: the checks then compare
with the merge base of HEAD and that ref, and a source edit with no receipt
fails even though it is committed.

```bash
agentflow verify-run --no-record --root "$PROOF_ROOT" --base-ref "$BASE_SHA"
agentflow verify-proof --root "$PROOF_ROOT"
```

Three settings make this work on a pull request:

- Pass the pull request's base commit, `github.event.pull_request.base.sha`.
- Check out the pull request's own commits,
  `ref: ${{ github.event.pull_request.head.sha }}`. The default checkout is a
  merge with the base branch, and a file the base branch also changed no
  longer matches its receipt.
- Fetch the history with `fetch-depth: 0`, so the base commit is there.

Without `--base-ref`, the checks use the baseline the run recorded at
`init-execution`. That record lives in `.agent/`, which the agent can write,
so keep `--base-ref` in CI configuration the agent cannot edit. It is a live
check: `verify-proof` does not re-check it later.

On a push, pass `--base-ref HEAD`, which checks the ledgers only. The pull
request run already compared the source edits, and a squash or rebase merge
can leave the commit the run recorded out of the branch's history, which fails
the recorded-baseline check. The copyable job in
[`examples/ci-proof/`](../examples/ci-proof/) does all of this.

## The two checks

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
with no execution contract. New contracts default to `review_gate: warn`, so a
proof built with the defaults, such as the [Quickstart](quickstart.md)'s, fails
`review_gate=block`. To meet that floor, set `proof_policy.review_gate` to
`block` in `.agent/execution.contract.json` right after `init-execution`,
before the run starts. Keep the floor in CI configuration the agent cannot
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

This repository's workflow reads two variables. They belong to the workflow,
not to the `agentflow` command, and the workflow passes them to
`examples/ci-proof/smoke.sh`.

- `AGENTFLOW_PROOF_ROOT` selects the committed proof bundle. This repository
  points it at `tests/fixtures/proof-bundle` so CI can regression test the
  verifier itself without committing task-local root `.agent/` artifacts. A
  subdirectory like that is checked as a standalone copy with no history.
  Repositories that commit the active task's proof bundle should set it to `.`.
- `AGENTFLOW_PROOF_BASE_REF` selects the baseline commit, passed as
  `--base-ref`. When it is unset and the proof root is `.`, a pull request uses
  its base commit and a push uses `HEAD`. It needs the proof root to be `.`;
  with a subdirectory the workflow fails rather than ignore it.

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
