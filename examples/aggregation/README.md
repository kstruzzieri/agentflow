# Two-worktree aggregation

Run the complete two-writer workload from a clean checkout:

```sh
PYTHONPATH=src python3 examples/aggregation/run.py
```

Expected result: `aggregation example passed`. The script creates one Agentflow
run in each isolated Git worktree, previews the collision-free merge, writes a
canonical proof root, then verifies the run and proof.

The underlying aggregation commands are:

```sh
PYTHONPATH=src python3 -m agentflow aggregate-ledgers \
  --input ../writer-a --source-id writera \
  --input ../writer-b --source-id writerb \
  --output ../canonical --base HEAD --dry-run
PYTHONPATH=src python3 -m agentflow aggregate-ledgers \
  --input ../writer-a --source-id writera \
  --input ../writer-b --source-id writerb \
  --output ../canonical --base HEAD
PYTHONPATH=src python3 -m agentflow verify-run --root ../canonical
PYTHONPATH=src python3 -m agentflow build-proof --root ../canonical
PYTHONPATH=src python3 -m agentflow verify-proof --root ../canonical
```

Expected: dry-run reports no collision, aggregation creates `../canonical/.agent`,
and final run/proof verification passes.
