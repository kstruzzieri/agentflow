# Two-worktree aggregation

Create one Agentflow run in each isolated worktree, then preview and write the
canonical proof root:

```sh
PYTHONPATH=src python3 -m agentflow aggregate-ledgers \
  --input ../writer-a --source-id writer-a \
  --input ../writer-b --source-id writer-b \
  --output ../canonical --base HEAD --dry-run
PYTHONPATH=src python3 -m agentflow aggregate-ledgers \
  --input ../writer-a --source-id writer-a \
  --input ../writer-b --source-id writer-b \
  --output ../canonical --base HEAD
PYTHONPATH=src python3 -m agentflow verify-run --root ../canonical
PYTHONPATH=src python3 -m agentflow build-proof --root ../canonical
PYTHONPATH=src python3 -m agentflow verify-proof --root ../canonical
```

Expected: dry-run reports no collision, aggregation creates `../canonical/.agent`,
and final run/proof verification passes.
