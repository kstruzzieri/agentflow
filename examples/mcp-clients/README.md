# MCP stdio clients

Claude Code configuration:

```json
{"mcpServers":{"agentflow":{"command":"python3","args":["-m","agentflow.mcp_server"],"env":{"PYTHONPATH":"src"}}}}
```

Generic JSON-RPC stdio clients use the same command and send `initialize`:

```sh
PYTHONPATH=src python3 examples/mcp-clients/initialize_smoke.py
```

Expected result: JSON with `result.serverInfo.name` equal to `agentflow`.
For an installed checkout, replace the command with `agentflow-mcp`.

HTTP is unauthenticated: bind only to loopback and never expose it to untrusted
networks. See [the MCP security boundary](../../docs/mcp.md#security-boundary).
