# CI proof verification (POSIX shell)

This copyable GitHub Actions pattern validates a committed Agentflow proof root
without mutating its ledgers. It is POSIX-only because the smoke command uses
`sh`.

From a clean source checkout, run:

```sh
PYTHONPATH=src PYTHON=python3 sh examples/ci-proof/smoke.sh
```

Expected result: `verify-run passed` followed by successful normal and strict
proof verification. Point the script at your committed proof root as its first
argument.

Use the same three commands in GitHub Actions after checkout and Python setup:

```sh
PYTHONPATH=src python3 -m agentflow verify-run --no-record --root "$PROOF_ROOT"
PYTHONPATH=src python3 -m agentflow verify-proof --root "$PROOF_ROOT"
PYTHONPATH=src python3 -m agentflow verify-proof --strict --root "$PROOF_ROOT"
```

See [`workflow.yml`](workflow.yml) for a complete job. The repository CI runs
this packet through `tests/test_examples.py`.
