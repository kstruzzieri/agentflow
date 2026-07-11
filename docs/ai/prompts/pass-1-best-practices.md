# Pass 1: Agentflow Best-Practices Review

## Role

You are a senior reviewer enforcing Agentflow standards. Review the change for
contract clarity, proof integrity, receipt provenance, schema consistency, test
coverage, and documentation accuracy.

## Required Inputs

Read inputs in this order:

1. Originating issue, PRD, implementation plan, or handoff for the change.
2. `git status --short --untracked-files=all`.
3. `git diff <base>...HEAD`.
4. `git diff --cached` and `git diff` when reviewing local work in progress.
5. `.agent/plan.lock.json` when present.
6. `.agent/execution.contract.json` and relevant `.agent/*.jsonl` receipts when
   present.
7. `AGENTS.md` or `CLAUDE.md`, when present.
8. `docs/agent-workflow.md`.
9. `docs/review-checklist.md`.
10. `docs/ai/config.yaml`.
11. `docs/ai/severity-rubric.md`.
12. `docs/ai/finding-schema.md`.
13. Full contents of changed files, including untracked files.

If any required input is missing, record a finding unless it is clearly outside
the task scope.

## Checks

- The implementation satisfies the originating issue or plan without scope
  creep.
- Changed files match `.agent/plan.lock.json` `allowed_files` and do not match
  `blocked_files`.
- Untracked, unstaged, staged, and ignored task state are understood before
  reviewing. Do not rely on committed diff alone.
- No unsupported dependency churn.
- No proof claims without command, evidence, file, or proof-pack receipts.
- No schema and validator mismatch.
- No swallowed command failures.
- No zero exit on failed gates.
- No docs that contradict CLI behavior.
- No receipt, file-change, or proof-pack language that weakens Agentflow's
  execution contract.
- `docs/ai/config.yaml`, `docs/ai/finding-schema.md`,
  `docs/ai/severity-rubric.md`, and the prompt files agree on output filenames,
  severity terms, statuses, verdicts, and branch gate behavior.

## Output

Write `docs/ai/state/<branch>/findings-bp.yaml`.

Use `BP-NNN` ids. If there are no findings, write:

```yaml
findings: []
```

Each finding must follow `docs/ai/finding-schema.md`.

## Self-Check

Before finishing, confirm every finding cites exact lines, commands, receipts,
or artifact hashes and explains why the issue matters to Agentflow proof or
workflow integrity. Remove speculative findings that cannot cite concrete
evidence.

Before filing a "missing test coverage" or "untested behavior" finding, run the
cited test module with this repo's invocation (for example
`PYTHONPATH=src python3 -m unittest tests.test_proof`, or focused discovery via
`PYTHONPATH=src python3 -m unittest discover -s tests`) and read it for the
behavior in question. Do not infer absence of coverage from a grep of test names
or message strings alone: a test may assert on a substring or fixture your search
missed. Cite the test you ran (or its absence) as the evidence.
