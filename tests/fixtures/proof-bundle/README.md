# Proof Bundle Fixture

This directory is a committed Agentflow proof bundle used by CI and regression
tests to verify the verifier itself. It is staged into a temporary git repository
before `verify-run` executes, matching the workflow used in GitHub Actions.

The bundle is historical: its plan and drift report describe the task that
originally produced the proof artifacts. Referenced source-tree paths are not
copied into this fixture because `verify-proof` validates the hashed Agentflow
artifacts and receipt output files under `.agent/`.

Use `PYTHONPATH=src python3 -m agentflow verify-run --no-record --root
tests/fixtures/proof-bundle` when the fixture should be checked without changing
`.agent/verification-runs.jsonl`.
