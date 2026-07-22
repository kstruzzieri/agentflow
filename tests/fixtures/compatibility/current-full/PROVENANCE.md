# Current full fixture provenance

Generated from the source checkout at commit `fb6cd68` with the current
`python3 -m agentflow` CLI. The fixture intentionally exercises workflow
capabilities, design-reference coverage, runtime and MCP status, review
recording, requirement criteria, an amended step, and hunk attribution.
Generated proof metadata is not edited.

## Integrity

Every fixture file is pinned by SHA-256 in `MANIFEST.json` and verified by
`tests/test_proof_compatibility.py`; an unintended byte change fails CI. The
name "current" means current as of the pinned commit above — the fixture is an
immutable snapshot, not a live mirror of the writer's output.

## Updating the fixture

When a schema change requires a refreshed fixture, regenerate the `.agent/`
tree by replaying the workflow above with the current CLI, then refresh the
pins (run from the repo root):

1. Author and lock the decision-bearing `0.4.0` plan.
2. Seed the workflow contract, runtime configuration, capability receipt, and
   review input; initialize execution and commit the scratch baseline.
3. Claim and complete `P1`, amend and complete it again, then run `verify-run`,
   `audit-drift`, `build-proof`, and `verify-proof`.

```bash
python3 -c "import hashlib, json, pathlib; root = pathlib.Path('tests/fixtures/compatibility/current-full'); pins = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name not in {'MANIFEST.json', 'PROVENANCE.md'}}; (root / 'MANIFEST.json').write_text(json.dumps({'artifacts': pins}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

Record the new generating commit in this file with any update.
