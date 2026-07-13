# Workflow-pack walkthrough

From a clean checkout, select a workflow posture from a brief, then compile an
unlocked plan from the shipped pack:

```sh
PYTHONPATH=src python3 examples/workflow-pack/run.py
```

Expected result: `workflow-pack example passed`; the workload runs the full
recommendation, draft, lock, execution, and proof chain in a temporary checkout.

```sh
PYTHONPATH=src python3 -m agentflow recommend-workflow --brief examples/briefs/docs-only.brief.json
PYTHONPATH=src python3 -m agentflow draft-plan --brief examples/briefs/docs-only.brief.json \
  --workflow examples/packs/agentflow-draft-demo \
  --objective "Document the workflow-pack walkthrough" --write
PYTHONPATH=src python3 -m agentflow lock-plan .agent/plan.lock.json
PYTHONPATH=src python3 -m agentflow init-execution
```

The first command recommends `docs-only`; the second writes the selected pack's
plan and workflow contract. Add a real step receipt, then run `verify-run`,
`audit-drift`, `build-proof`, and `verify-proof` to finish the run.
