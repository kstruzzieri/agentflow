# Current aggregated fixture provenance

Generated from the source checkout at commit `90525e2` with the current
`aggregate-ledgers`, `build-proof`, and `verify-proof` commands. Two sibling Git
worktrees supplied non-overlapping P1/P2 ledgers; the canonical output preserves
their `WTalpha-` and `WTbeta-` namespaces. Generated proof metadata is not edited.

## Integrity

Every fixture file is pinned by SHA-256 in `MANIFEST.json` and verified by
`tests/test_proof_compatibility.py`; an unintended byte change fails CI. The
name "current" means current as of the pinned commit above — the fixture is an
immutable snapshot, not a live mirror of the writer's output.

## Updating the fixture

When a schema change requires a refreshed fixture, regenerate the `.agent/`
tree by replaying the two-worktree aggregation workflow above with the current
CLI, then refresh the pins (run from the repo root).

Note that since #28 `build-proof` applies the full plan and execution-contract
validators, so the two source worktrees must carry a plan that satisfies the
whole plan contract and an execution contract that satisfies its own. The
pinned plan in this snapshot predates that gate and no longer builds; replaying
the workflow with it fails with `invalid working state: plan:`. That does not
affect this fixture's purpose — `verify-proof` never applies those validators,
which is what keeps an older proof verifiable — but a refresh must start from
complete source artifacts.

```bash
python3 -c "import hashlib, json, pathlib; root = pathlib.Path('tests/fixtures/compatibility/current-aggregated'); pins = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name not in {'MANIFEST.json', 'PROVENANCE.md'}}; (root / 'MANIFEST.json').write_text(json.dumps({'artifacts': pins}, indent=2, sort_keys=True) + '\n', encoding='utf-8')"
```

Record the new generating commit in this file with any update.
