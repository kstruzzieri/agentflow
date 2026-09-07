# Current 1.0 fixture provenance

Generated during the issue #5 post-soak transition by replaying the
`current-full` workflow with the 1.0 CLI: the same decision-bearing plan,
workflow capabilities, design-reference coverage, runtime and MCP status,
review recording, requirement criteria, an amended step, and hunk attribution.
Generated proof metadata is not edited.

This root exists because `current-full` could not be updated in place. Every
path under `tests/fixtures/compatibility` is frozen, and the post-soak
carve-out in `scripts/check_schema_soak.py` admits *additions* beneath the
fixture roots but permits wholesale regeneration only under
`tests/fixtures/proof-bundle`. `current-full` therefore stays byte-identical
as the last 0.x working-state sample, and this root carries the 1.0 one.

## Integrity

Every fixture file is pinned by SHA-256 in `MANIFEST.json` and verified by
`tests/test_proof_compatibility.py`; an unintended byte change fails CI. The
name "current" means current as of the transition commit — the fixture is an
immutable snapshot, not a live mirror of the writer's output.

`tests/test_stability_policy.py` selects whichever compatibility root the live
schema constants can still read, preferring the newest, so this root supersedes
`current-full` for working-state payload checks without either being edited.

## Updating the fixture

When a schema change requires a refreshed fixture, do not edit this tree: add
a new root beside it, exactly as this one was added. Regenerate the `.agent/`
tree by replaying the workflow above with the current CLI, then refresh the
pins (run from the repo root):

1. Author and lock the decision-bearing plan at the current plan-lock schema.
2. Seed the workflow contract, runtime configuration, capability receipt, and
   review input; initialize execution and commit the scratch baseline.
3. Claim and complete `P1`, amend and complete it again, then run `verify-run`,
   `audit-drift`, `build-proof`, and `verify-proof`.

```bash
python3 -c "import hashlib, json, pathlib; root = pathlib.Path('tests/fixtures/compatibility/current-1.0'); pins = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name not in {'MANIFEST.json', 'PROVENANCE.md'}}; (root / 'MANIFEST.json').write_text(json.dumps({'artifacts': pins}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

Record the new generating commit in this file with any update.
