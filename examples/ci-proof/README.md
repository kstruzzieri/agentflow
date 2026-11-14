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
argument. Set `AGENTFLOW_BASE_REF` to a commit to compare everything changed
since it, committed or not; that needs a proof root at the top of a Git
repository, since a subdirectory is checked as a copy with no history.

Use the same three commands in GitHub Actions after checkout and Python setup:

```sh
PYTHONPATH=src python3 -m agentflow verify-run --no-record --root "$PROOF_ROOT" --base-ref "$BASE_REF"
PYTHONPATH=src python3 -m agentflow verify-proof --root "$PROOF_ROOT"
PYTHONPATH=src python3 -m agentflow verify-proof --strict --root "$PROOF_ROOT"
```

See [`workflow.yml`](workflow.yml) for a complete job. On a pull request it
checks out the pull request's own commits with full history and passes the base
commit as `--base-ref`, so a source edit with no receipt fails even once it is
committed. On a push it passes `--base-ref HEAD`, which checks the ledgers
only: the pull request already compared the edits, and a squash or rebase
merge can drop the baseline the run recorded. A copied subdirectory is also
compared with its one commit.
The repository CI runs this packet through `tests/test_examples.py`. The
[CI proof gate guide](../../docs/ci-proof-gate.md) covers the baseline and the
minimum-policy flags.
