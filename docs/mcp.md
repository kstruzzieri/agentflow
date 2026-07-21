# Agentflow MCP Server

Agentflow ships a dependency-free [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes the core workflow commands as MCP tools. It is the universal
integration point: Claude Code and Codex CLI discover the tools over **stdio**,
and local LLM front-ends (Ollama/Open WebUI, llama.cpp) discover them over
**Streamable HTTP**. One server, all runtimes.

## Design

The server is a thin protocol-translation layer. It speaks JSON-RPC 2.0 and maps
each MCP tool to an `agentflow` CLI argv vector that is executed **in-process**
via `agentflow.cli.main` with stdout/stderr captured. No Agentflow business logic
is duplicated, and only the Python standard library is used — the package keeps
its `dependencies = []` invariant.

> The official `mcp` Python SDK was evaluated and rejected: it pulls ~28
> transitive dependencies (uvicorn, starlette, pydantic, cryptography, …), which
> would break Agentflow's no-runtime-dependency guarantee (`dependencies = []`).
> The hand-rolled JSON-RPC server uses only the standard library and runs on
> every supported interpreter (Python 3.11+).

## Exposed tools

| Tool | CLI command | Purpose |
| --- | --- | --- |
| `status` | `status` | Summarize current workflow state |
| `doctor` | `doctor` | Check shell runtime readiness |
| `next_step` | `next-step` | Next eligible plan step (JSON) |
| `claim_step` | `claim-step` | Claim a plan step (`step`, `agent`) |
| `amend_step` | `amend-step` | Open an auditable amendment on a completed step (`step`, `agent`, `reason`, optional `reason_code`, optional `finding_refs` array of `RR-...#ID` strings) |
| `reclaim_step` | `reclaim-step` | Abandon an expired attempt and open a fresh claim |
| `renew_lease` | `renew-lease` | Extend the current owner's step lease |
| `record_review` | `record-review` | Record a review run from its manifest into the review-runs ledger (`manifest` required, optional `emit_evidence`) |
| `complete_step` | `complete-step` | Complete a verified step attempt |
| `verify_step` | `verify-step` | Verify one step attempt |
| `verify_run` | `verify-run` | Verify whole-run coverage |
| `audit_drift` | `audit-drift` | Compare git changes to plan scope |
| `build_proof` | `build-proof` | Generate the proof pack |
| `verify_proof` | `verify-proof` | Verify proof-pack source hashes |
| `next_action` | `next-action` | Report the single next required action as a command and JSON |
| `finish_step` | `finish-step` | Verify then complete a step when verification passes (`step`) |
| `finish_run` | `finish-run` | Run audit-drift -> verify-run -> build-proof -> verify-proof in order |

Every tool accepts an optional `root` argument (defaults to `.`).

### Security boundary

The arbitrary-command tools (`run`, `record-command`) are intentionally **not**
exposed, so an MCP client cannot submit a new argv directly. This is not a
categorically non-executing surface: `verify_step`, `verify_proof`, and
`finish_step` expose `replay`, which executes attested command gates already
present under the client-selected root with the server process's permissions.
Treat every selected root and its attested receipt ledger as executable input,
and do not allow an untrusted client to select or replay it.

The HTTP transport:

- binds `127.0.0.1` by default and prints a warning when bound to a non-loopback
  host;
- rejects non-localhost `Origin` headers (parsed, not prefix-matched) to resist
  browser DNS-rebinding;
- caps request bodies at 1 MiB (`413` beyond that), rejects chunked
  `Transfer-Encoding` (`411`, since it requires `Content-Length`), and applies a
  per-request socket timeout so a stalled client cannot pin a handler thread;
- only answers `POST` to `/mcp` (`GET` → `405`, other paths → `404`).

**The HTTP transport is unauthenticated.** The `Origin` check only defends
browser-originated requests; a non-browser client can omit `Origin`. Bind it to
loopback (or another interface you fully trust) and do not expose it to untrusted
networks. Tool calls are serialized by a process-wide lock, because the
underlying CLI redirects the process stdout/stderr and is not reentrant.

## Running

### stdio (Claude Code, Codex CLI)

```bash
PYTHONPATH=src python3 -m agentflow.mcp_server
# or, after an editable install:
agentflow-mcp
```

The repository ships discovery configs so the server is found automatically:

- Claude Code reads [`.mcp.json`](../.mcp.json).
- Codex CLI reads [`.codex/config.toml`](../.codex/config.toml).

### Streamable HTTP (local LLM front-ends)

```bash
PYTHONPATH=src python3 -m agentflow.mcp_server --transport http --host 127.0.0.1 --port 8765
```

The endpoint is `POST http://127.0.0.1:8765/mcp` and returns
`application/json`. Point an MCP-capable HTTP client (Open WebUI, llama.cpp
WebUI) at that URL.

## Protocol smoke test

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | PYTHONPATH=src python3 -m agentflow.mcp_server
```

Expected: an `initialize` result with `serverInfo.name == "agentflow"` followed by
a `tools/list` result enumerating the seventeen tools above.

## Recording other MCP servers as evidence (#19)

Everything above describes the **agentflow-mcp server**: Agentflow exposing its
own workflow commands *as* MCP tools to a client. This section is the inverse
and shares nothing with it but the acronym: Agentflow can also *record* that
your environment declares other MCP servers, as read-only evidence in runtime
snapshots and proof packs.

The boundary is strict. Agentflow is **neither an MCP client nor a tool
router** here:

- It never sends `initialize`, `tools/list`, or any JSON-RPC to a declared
  server. Liveness is observed only via a `PATH` lookup (`command_exists`), an
  opt-in spawn-liveness check (`command_spawn`, `--probe`), or an opt-in bare
  HTTP `GET` that reads the status line and never the body (`http_status`,
  `--probe`).
- Tool names come from your configuration (`declared_tools`) and are recorded
  with `"tool_source": "declared"` — never learned from the wire.
- `mcp_servers` entries are not valid `routes` targets. Routing and capability
  satisfaction remain runtime-only concepts.
- Snapshots record availability only: id, transport, enabled, status,
  reachable, declared tool names/count, and a fixed-vocabulary check message.
  Endpoints, commands, args, environment, and credentials are never stored.

Declare servers in `.agent/runtime.config.json`:

```json
{
  "mcp_servers": {
    "github": {
      "enabled": true,
      "transport": "stdio",
      "declared_tools": ["create_issue", "list_prs"],
      "readiness": { "check": "command_exists", "command": "github-mcp-server" }
    },
    "agentflow-local": {
      "enabled": true,
      "transport": "http",
      "declared_tools": ["next_action", "finish_step"],
      "readiness": {
        "check": "http_status",
        "url": "http://127.0.0.1:8765/mcp",
        "timeout_seconds": 2
      }
    }
  }
}
```

`agentflow runtime-status --record` captures the block into
`.agent/runtime-snapshots.jsonl` (add `--probe` to run the opt-in liveness
checks); `agentflow build-proof` then summarizes the latest snapshot into a
tamper-evident `runtime` block in the proof pack. Without `--probe`, and for
servers with no `readiness`, the status is `configured` — declared, recorded,
nothing executed.
