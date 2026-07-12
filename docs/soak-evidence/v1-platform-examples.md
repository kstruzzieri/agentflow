# v1 platform/examples soak evidence

Run date: 2026-07-11

Checkout: disposable worktree at commit `78bcd01` (integrated Track C branch).
Runtime: Agentflow `0.4.0` via `PYTHONPATH=src python3 -m agentflow`.

| Packet | Command / workload | Result |
| --- | --- | --- |
| CI proof | `PYTHONPATH=src PYTHON=python3 sh examples/ci-proof/smoke.sh` | `verify-run passed`; normal and strict proof verification passed. |
| MCP clients | `PYTHONPATH=src python3 examples/mcp-clients/initialize_smoke.py` | JSON-RPC `initialize` returned `serverInfo.name=agentflow`. |
| Workflow pack | Commands in `examples/workflow-pack/README.md` through `init-execution` | Recommended `agentflow-default/docs-only`; drafted and locked plan and workflow contract. |
| Aggregation | `tests.test_aggregate.EndToEndAggregateTests.test_positive_fixture_verifies_and_detects_aggregation_tamper` | Passed; creates two Git worktrees, aggregates ledgers, and verifies generated proof. |

This is the #5 soak baseline for these workloads. Schema-affecting changes reset
the release soak clock according to issue #11.
