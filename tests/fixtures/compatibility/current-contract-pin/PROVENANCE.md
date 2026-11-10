# Current contract-pin fixture provenance

Generated for issue #117 on branch `feat/117-contract-pin` (base `56d98c7`)
with the CLI whose step-runs, command-receipts, file-receipts,
verification-runs and drift-report schemas are 1.1.0. It is the first proof in
which every run record carries `execution_contract_sha256`, the SHA-256 of the
execution contract it was written under, so `verify-proof --require` on
`risk_policy`, `lease_policy` or `hunk_attribution` keeps accepting it as the
code evolves. Generated proof metadata is not edited.

The workflow is one step, `P1`, with a structured `command` gate: record a
runtime snapshot, claim the step, write `fixture.txt` and `record-file-change`
it, satisfy the gate with `run`, then `verify-step`, `complete-step`,
`verify-run`, `audit-drift` and `build-proof`, all under the unchanged default
contract. Every record in the four ledgers and the drift report names that
contract.

Its working state is the newest the live readers accept, so
`tests/test_stability_policy.py` ranks it as the representative working-state
sample in place of `current-1.0` and checks the `--json` contract of
`claim-step`, `run`, `record-file-change`, `verify-run` and `runtime-status`
against its 1.1 rows.

## Integrity

Every fixture file is pinned by SHA-256 in `MANIFEST.json` and verified by
`tests/test_proof_compatibility.py`; an unintended byte change fails CI, and
`scripts/check_schema_soak.py` holds the tree to its first committed content.

## Updating the fixture

Do not edit this tree: add a new root beside it. To reproduce it, run from an
empty directory initialized with `git init` and one empty commit (verify-run
reads `git status`), with the CLI under test on `PYTHONPATH`:

1. `agentflow init`, write the one-step plan (`P1`, files `fixture.txt`,
   validation `fixture gate`, a `command` gate
   `python3 -c "print('pinned')"`, allowed files `fixture.txt` and `.agent/`),
   and `lock-plan`.
2. `init-execution`, copy `current-1.0/.agent/runtime.config.json` into
   `.agent/` and run `runtime-status --record`, then `claim-step P1`, write
   `fixture.txt`,
   `record-file-change --step P1 --path fixture.txt`, and
   `run --step P1 -- python3 -c "print('pinned')"`.
3. `verify-step P1`, `complete-step P1`, `verify-run`, `audit-drift`,
   `build-proof`, then `verify-proof --require risk_policy=require-confirmation
   --require lease_policy=advisory --require hunk_attribution=enforce`.

Copy the result without `.git`, `*.lockfile` and `.agent/attempts/`, then
refresh the pins (run from the repo root):

```bash
python3 -c "import hashlib, json, pathlib; root = pathlib.Path('tests/fixtures/compatibility/current-contract-pin'); pins = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name not in {'MANIFEST.json', 'PROVENANCE.md'}}; (root / 'MANIFEST.json').write_text(json.dumps({'artifacts': pins}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

Record the new generating commit in this file with any update.
