# Current 1.1 fixture provenance

Generated for issue #110 with the proof-pack 1.1.0 CLI. It is the first proof
that carries `coverage.validation_gates`, the per-gate record of how each
validation gate was satisfied, so `verify-proof` keeps recomputing and
accepting that field as the code evolves. Generated proof metadata is not
edited.

The workflow covers every way a gate can be satisfied:

| Step | Gate | Satisfied by |
| --- | --- | --- |
| P1 | command gate | `run` (`observed` receipt) |
| P1 | inspection gate | evidence `E1` |
| P2 | legacy gate | `record-command` (`attested` receipt) |
| P3 | legacy gate | `record-command`, then `replay-gates --record` (`reconstructed` receipt) |
| P4 | legacy gate | the step's evidence `E2` |
| P5 | legacy gate | nothing (a warning the contract allows) |

This root is deliberately thin: it records no file changes, so its empty
`file-receipts.jsonl` keeps `tests/test_stability_policy.py` from ranking it as
working state. The working-state schemas did not change in 1.1, so
`current-1.0` stays the representative working-state sample.

## Integrity

Every fixture file is pinned by SHA-256 in `MANIFEST.json` and verified by
`tests/test_proof_compatibility.py`; an unintended byte change fails CI, and
`scripts/check_schema_soak.py` holds the tree to its first committed content.

## Updating the fixture

Do not edit this tree: add a new root beside it. To reproduce it, run from an
empty directory initialized with `git init` (verify-run reads `git status`),
with the CLI under test on `PYTHONPATH`:

1. `agentflow init`, write the five-step plan above (P1 with a structured
   `command` gate `python3 -c "print('observed')"` and an `inspection` gate on
   `E1`; P4 with `evidence_ids: ["E2"]`), and `lock-plan`.
2. `init-execution`, then set `proof_policy.require_command_receipts_for_validation`
   and `require_evidence_for_inspection_gates` to `false` so P5 can complete.
3. `record-evidence` `E1` and `E2`; claim, satisfy, `verify-step` and
   `complete-step` each step as in the table.
4. `verify-run`, `build-proof`, `verify-proof`.

Copy the result without `.git`, `*.lockfile` and `.agent/attempts/`, then
refresh the pins (run from the repo root):

```bash
python3 -c "import hashlib, json, pathlib; root = pathlib.Path('tests/fixtures/compatibility/current-1.1'); pins = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name not in {'MANIFEST.json', 'PROVENANCE.md'}}; (root / 'MANIFEST.json').write_text(json.dumps({'artifacts': pins}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

Record the new generating commit in this file with any update.
