# v1 platform/examples soak evidence

Run date: 2026-07-11

Validated commit: `4694f3d16da0affa72d4a563cde665c9b748af7e`.
Runtime: Agentflow `0.4.0` via `PYTHONPATH=src python3 -m agentflow`.
CI: [run 29177747344](https://github.com/kstruzzieri/agentflow/actions/runs/29177747344), six jobs green on Linux/macOS × Python 3.11–3.13.

| Packet | Command / workload | Result |
| --- | --- | --- |
| CI proof | `PYTHONPATH=src PYTHON=python3 sh examples/ci-proof/smoke.sh` | `verify-run passed`; normal and strict proof verification passed. |
| MCP clients | `PYTHONPATH=src python3 examples/mcp-clients/initialize_smoke.py` | JSON-RPC `initialize` returned `serverInfo.name=agentflow`. |
| Workflow pack | `PYTHONPATH=src python3 examples/workflow-pack/run.py` | Passed; recommendation, draft, lock, execution, drift audit, run verification, proof build, and proof verification completed in a temporary checkout. |
| Aggregation | `PYTHONPATH=src python3 examples/aggregation/run.py` | Passed; creates two Git worktrees with independent writers, dry-runs and writes aggregation, then verifies canonical run and proof. |

This is the #5 soak baseline for these workloads. Schema-affecting changes reset
the release soak clock according to issue #11.
