"""Dependency-free MCP server exposing Agentflow CLI commands as MCP tools.

Speaks JSON-RPC 2.0 over two transports:

* stdio: newline-delimited JSON (Claude Code, Codex CLI)
* Streamable HTTP: POST -> ``application/json`` (local LLM front-ends)

The server is a thin protocol-translation layer over :func:`agentflow.cli.main`:
each MCP tool maps to an ``agentflow`` CLI argv vector that is executed
in-process with stdout/stderr captured. No Agentflow business logic is
duplicated here, and only the standard library is used so the package keeps its
``dependencies = []`` invariant.

Only read/query and state-transition tools are exposed. The arbitrary-command
tools (``run``/``record-command``) are intentionally excluded so an MCP client
cannot drive shell execution through this surface.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import __version__, cli

# The single MCP protocol version this server implements. Per the spec, a server
# that supports only one version always responds to initialize with that version.
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agentflow"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ENDPOINT_PATH = "/mcp"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Cap on a single HTTP request body. The MCP messages this server accepts are
# tiny; the cap bounds memory use if a client sends an oversized Content-Length.
MAX_BODY_BYTES = 1 << 20  # 1 MiB
# Per-request socket timeout (seconds). Prevents a stalled client from pinning a
# handler thread indefinitely (slow-loris) on the thread-per-connection server.
REQUEST_TIMEOUT = 30.0

# cli.main redirects the process-global stdout/stderr, so it is not reentrant.
# Serialize all in-process CLI invocations; HTTP tool calls run one at a time.
_CLI_LOCK = threading.Lock()


@dataclass(frozen=True)
class Tool:
    """A single MCP tool mapped to an Agentflow CLI argv builder."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    argv: Callable[[Dict[str, Any]], List[str]]

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _root(arguments: Dict[str, Any]) -> List[str]:
    return ["--root", str(arguments.get("root", "."))]


def _require(arguments: Dict[str, Any], key: str) -> str:
    if key not in arguments or arguments[key] in (None, ""):
        raise KeyError(key)
    return str(arguments[key])


def _object_schema(
    properties: Dict[str, Any], required: Optional[List[str]] = None
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_ROOT_PROP = {"root": {"type": "string", "description": "Project root directory (default '.')."}}
_STEP_PROP = {"step": {"type": "string", "description": "Plan step id, for example 'P1'."}}
_ATTEMPT_PROP = {
    "attempt": {"type": "string", "description": "Optional attempt id; defaults to the current attempt."}
}
_AGENT_PROP = {
    "agent": {
        "type": "string",
        "description": "Agent id; required under lease_policy=enforce (defaults to AGENTFLOW_AGENT_ID).",
    }
}
_LEASE_MINUTES_PROP = {
    "lease_minutes": {"type": "integer", "description": "Optional lease length in minutes."}
}


def _argv_status(a: Dict[str, Any]) -> List[str]:
    return ["status", *_root(a)]


def _argv_doctor(a: Dict[str, Any]) -> List[str]:
    return ["doctor", "--json", *_root(a)]


def _argv_next_step(a: Dict[str, Any]) -> List[str]:
    return ["next-step", "--json", *_root(a)]


def _argv_claim_step(a: Dict[str, Any]) -> List[str]:
    argv = ["claim-step", _require(a, "step"), "--agent", _require(a, "agent"), "--json", *_root(a)]
    if a.get("lease_minutes") is not None:
        argv += ["--lease-minutes", str(int(a["lease_minutes"]))]
    return argv


def _argv_amend_step(a: Dict[str, Any]) -> List[str]:
    argv = [
        "amend-step",
        _require(a, "step"),
        "--agent",
        _require(a, "agent"),
        "--reason",
        _require(a, "reason"),
        "--json",
        *_root(a),
    ]
    if a.get("reason_code"):
        argv += ["--reason-code", str(a["reason_code"])]
    for ref in a.get("finding_refs") or []:
        argv += ["--finding", str(ref)]
    if a.get("lease_minutes") is not None:
        argv += ["--lease-minutes", str(int(a["lease_minutes"]))]
    return argv


def _argv_reclaim_step(a: Dict[str, Any]) -> List[str]:
    argv = [
        "reclaim-step",
        _require(a, "step"),
        "--agent",
        _require(a, "agent"),
        "--reason",
        _require(a, "reason"),
        "--json",
        *_root(a),
    ]
    if a.get("lease_minutes") is not None:
        argv += ["--lease-minutes", str(int(a["lease_minutes"]))]
    return argv


def _argv_renew_lease(a: Dict[str, Any]) -> List[str]:
    argv = ["renew-lease", _require(a, "step"), "--agent", _require(a, "agent"), "--json", *_root(a)]
    if a.get("attempt"):
        argv += ["--attempt", str(a["attempt"])]
    if a.get("minutes") is not None:
        argv += ["--minutes", str(int(a["minutes"]))]
    return argv


def _argv_record_review(a: Dict[str, Any]) -> List[str]:
    argv = ["record-review", "--manifest", _require(a, "manifest"), "--json", *_root(a)]
    if a.get("emit_evidence"):
        argv += ["--emit-evidence"]
    return argv


def _argv_complete_step(a: Dict[str, Any]) -> List[str]:
    argv = ["complete-step", _require(a, "step"), "--json", *_root(a)]
    if a.get("attempt"):
        argv += ["--attempt", str(a["attempt"])]
    if a.get("agent"):
        argv += ["--agent", str(a["agent"])]
    return argv


def _argv_verify_step(a: Dict[str, Any]) -> List[str]:
    argv = ["verify-step", _require(a, "step"), "--json", *_root(a)]
    if a.get("attempt"):
        argv += ["--attempt", str(a["attempt"])]
    if a.get("agent"):
        argv += ["--agent", str(a["agent"])]
    if a.get("strict"):
        argv += ["--strict"]
    if a.get("replay"):
        argv += ["--replay"]
    return argv


def _argv_verify_run(a: Dict[str, Any]) -> List[str]:
    argv = ["verify-run", "--json", *_root(a)]
    if a.get("strict"):
        argv += ["--strict"]
    if a.get("record") is False:
        argv += ["--no-record"]
    return argv


def _argv_audit_drift(a: Dict[str, Any]) -> List[str]:
    argv = ["audit-drift", *_root(a)]
    if a.get("plan"):
        argv += ["--plan", str(a["plan"])]
    return argv


def _argv_build_proof(a: Dict[str, Any]) -> List[str]:
    argv = ["build-proof", *_root(a)]
    if a.get("strict"):
        argv += ["--strict"]
    return argv


def _argv_verify_proof(a: Dict[str, Any]) -> List[str]:
    argv = ["verify-proof", *_root(a)]
    if a.get("replay"):
        argv += ["--replay"]
    if a.get("strict"):
        argv += ["--strict"]
    return argv


def _argv_next_action(a: Dict[str, Any]) -> List[str]:
    argv = ["next-action", "--json", *_root(a)]
    if a.get("agent"):
        argv += ["--agent", str(a["agent"])]
    if a.get("strict"):
        argv += ["--strict"]
    return argv


def _argv_finish_step(a: Dict[str, Any]) -> List[str]:
    argv = ["finish-step", _require(a, "step"), "--json", *_root(a)]
    if a.get("attempt"):
        argv += ["--attempt", str(a["attempt"])]
    if a.get("agent"):
        argv += ["--agent", str(a["agent"])]
    if a.get("strict"):
        argv += ["--strict"]
    if a.get("replay"):
        argv += ["--replay"]
    return argv


def _argv_finish_run(a: Dict[str, Any]) -> List[str]:
    argv = ["finish-run", "--json", *_root(a)]
    if a.get("plan"):
        argv += ["--plan", str(a["plan"])]
    if a.get("strict"):
        argv += ["--strict"]
    return argv


TOOLS: List[Tool] = [
    Tool(
        "status",
        "Summarize current Agentflow workflow state for a project root.",
        _object_schema(dict(_ROOT_PROP)),
        _argv_status,
    ),
    Tool(
        "doctor",
        "Check shell runtime readiness for the execution contract.",
        _object_schema(dict(_ROOT_PROP)),
        _argv_doctor,
    ),
    Tool(
        "next_step",
        "Show the next eligible plan step as JSON.",
        _object_schema(dict(_ROOT_PROP)),
        _argv_next_step,
    ),
    Tool(
        "claim_step",
        "Claim a plan step for an agent.",
        _object_schema(
            {
                **_STEP_PROP,
                "agent": {"type": "string", "description": "Agent identifier claiming the step."},
                "lease_minutes": {"type": "integer", "description": "Optional lease length in minutes."},
                **_ROOT_PROP,
            },
            required=["step", "agent"],
        ),
        _argv_claim_step,
    ),
    Tool(
        "amend_step",
        "Open an auditable amendment attempt on a completed plan step.",
        _object_schema(
            {
                **_STEP_PROP,
                "agent": {"type": "string", "description": "Agent identifier opening the amendment."},
                "reason": {"type": "string", "description": "Why the completed step needs a follow-up edit."},
                "reason_code": {
                    "enum": ["review_feedback", "validation_followup", "operator_correction", "other"],
                    "description": "Optional structured amendment category.",
                },
                "finding_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional review-run-scoped finding refs (RR-...#ID).",
                },
                **_LEASE_MINUTES_PROP,
                **_ROOT_PROP,
            },
            required=["step", "agent", "reason"],
        ),
        _argv_amend_step,
    ),
    Tool(
        "reclaim_step",
        "Abandon an expired attempt and open a fresh claim for a new agent.",
        _object_schema(
            {
                **_STEP_PROP,
                **_AGENT_PROP,
                "reason": {"type": "string", "description": "Why the prior attempt is being reclaimed."},
                **_LEASE_MINUTES_PROP,
                **_ROOT_PROP,
            },
            required=["step", "agent", "reason"],
        ),
        _argv_reclaim_step,
    ),
    Tool(
        "renew_lease",
        "Extend an attempt's lease (owner self-recovery under enforce).",
        _object_schema(
            {
                **_STEP_PROP,
                **_AGENT_PROP,
                **_ATTEMPT_PROP,
                "minutes": {"type": "integer", "description": "New lease length in minutes."},
                **_ROOT_PROP,
            },
            required=["step", "agent"],
        ),
        _argv_renew_lease,
    ),
    Tool(
        "complete_step",
        "Complete a verified plan step attempt.",
        _object_schema({**_STEP_PROP, **_ATTEMPT_PROP, **_AGENT_PROP, **_ROOT_PROP}, required=["step"]),
        _argv_complete_step,
    ),
    Tool(
        "verify_step",
        "Verify one step attempt against its evidence and validation gates.",
        _object_schema(
            {
                **_STEP_PROP,
                **_ATTEMPT_PROP,
                **_AGENT_PROP,
                "strict": {"type": "boolean", "description": "Treat warnings as failures."},
                "replay": {"type": "boolean", "description": "Replay attested command gates."},
                **_ROOT_PROP,
            },
            required=["step"],
        ),
        _argv_verify_step,
    ),
    Tool(
        "verify_run",
        "Verify whole-run execution coverage across the locked plan.",
        _object_schema(
            {
                "strict": {"type": "boolean", "description": "Treat warnings as failures."},
                "record": {
                    "type": "boolean",
                    "description": "Append a verification-run entry (default true). Set false for read-only checks.",
                },
                **_ROOT_PROP,
            }
        ),
        _argv_verify_run,
    ),
    Tool(
        "audit_drift",
        "Compare git working-tree changes against the locked plan scope.",
        _object_schema(
            {"plan": {"type": "string", "description": "Plan path (default .agent/plan.lock.json)."}, **_ROOT_PROP}
        ),
        _argv_audit_drift,
    ),
    Tool(
        "build_proof",
        "Generate the proof-pack markdown and metadata for the run.",
        _object_schema({"strict": {"type": "boolean", "description": "Treat warnings as failures."}, **_ROOT_PROP}),
        _argv_build_proof,
    ),
    Tool(
        "verify_proof",
        "Verify proof-pack source hashes against the recorded metadata.",
        _object_schema(
            {
                "replay": {"type": "boolean", "description": "Replay attested command gates."},
                "strict": {"type": "boolean", "description": "Treat warnings as failures."},
                **_ROOT_PROP,
            }
        ),
        _argv_verify_proof,
    ),
    Tool(
        "record_review",
        "Record a review run from its manifest into the review-runs ledger.",
        _object_schema(
            {
                "manifest": {"type": "string", "description": "Path to review-manifest.json."},
                "emit_evidence": {"type": "boolean", "description": "Also write kind:review evidence entries."},
                **_ROOT_PROP,
            },
            required=["manifest"],
        ),
        _argv_record_review,
    ),
    Tool(
        "next_action",
        "Report the next required Agentflow action as a command and JSON.",
        _object_schema({
            **_AGENT_PROP,
            "strict": {"type": "boolean", "description": "Treat warnings as failures."},
            **_ROOT_PROP,
        }),
        _argv_next_action,
    ),
    Tool(
        "finish_step",
        "Verify then complete a step when verification passes.",
        _object_schema(
            {
                **_STEP_PROP,
                **_ATTEMPT_PROP,
                **_AGENT_PROP,
                "strict": {"type": "boolean", "description": "Treat warnings as failures."},
                "replay": {"type": "boolean", "description": "Replay attested command gates."},
                **_ROOT_PROP,
            },
            required=["step"],
        ),
        _argv_finish_step,
    ),
    Tool(
        "finish_run",
        "Run audit-drift, verify-run, build-proof, verify-proof in order.",
        _object_schema(
            {
                "plan": {"type": "string", "description": "Plan path (default .agent/plan.lock.json)."},
                "strict": {"type": "boolean", "description": "Treat warnings as failures."},
                **_ROOT_PROP,
            }
        ),
        _argv_finish_run,
    ),
]

TOOLS_BY_NAME: Dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def run_cli(argv: List[str]) -> Tuple[int, str, str]:
    """Run ``agentflow.cli.main(argv)`` in-process, capturing exit code + output."""
    out, err = io.StringIO(), io.StringIO()
    code: Any = 0
    with _CLI_LOCK:
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        except SystemExit as exc:  # argparse errors call sys.exit()
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # never crash the server on a tool failure
            detail = f"{type(exc).__name__}: {exc}"
            err_text = err.getvalue()
            return 1, out.getvalue(), f"{err_text}\n{detail}" if err_text else detail
    return int(code or 0), out.getvalue(), err.getvalue()


def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tools_call(msg_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "Invalid params for tools/call: expected object")
    name = params.get("name")
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(msg_id, -32602, f"Invalid arguments for {name}: expected object")
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return _error(msg_id, -32602, f"Unknown tool: {name!r}")
    try:
        argv = tool.argv(arguments)
    except KeyError as exc:
        return _error(msg_id, -32602, f"Missing required argument for {name}: {exc.args[0]}")
    except (TypeError, ValueError) as exc:
        return _error(msg_id, -32602, f"Invalid arguments for {name}: {exc}")
    code, out, err = run_cli(argv)
    text = out if out.strip() else err
    structured: Dict[str, Any] = {"exit_code": code, "stdout": out, "stderr": err}
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if parsed is not None:
        structured["data"] = parsed
    return _result(
        msg_id,
        {
            "content": [{"type": "text", "text": text.rstrip("\n") or f"exit {code}"}],
            "isError": code != 0,
            "structuredContent": structured,
        },
    )


def handle_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a JSON-RPC response dict, or ``None`` for notifications."""
    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid Request: expected object")
    msg_id = message.get("id")
    if message.get("jsonrpc") != "2.0":
        return _error(msg_id, -32600, "Invalid Request: jsonrpc must be '2.0'")
    method = message.get("method")
    if not isinstance(method, str):
        return _error(msg_id, -32600, "Invalid Request: method must be a string")
    if method == "initialize":
        # This server implements exactly one protocol version, so per spec it
        # always answers with that version regardless of what the client requests.
        return _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [tool.describe() for tool in TOOLS]})
    if method == "tools/call":
        params = message.get("params", {})
        if params is None:
            params = {}
        return _tools_call(msg_id, params)
    if msg_id is None:
        return None  # unknown notification: ignore
    return _error(msg_id, -32601, f"Method not found: {method}")


# --- stdio transport -----------------------------------------------------


def _write_message(stream: Any, obj: Dict[str, Any]) -> None:
    stream.write(json.dumps(obj) + "\n")
    stream.flush()


def serve_stdio(stdin: Any = None, stdout: Any = None) -> None:
    """Serve newline-delimited JSON-RPC over the given (or process) streams."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write_message(stdout, _error(None, -32700, "Parse error"))
            continue
        response = handle_message(message)
        if response is not None:
            _write_message(stdout, response)


# --- Streamable HTTP transport (JSON response mode) ----------------------


def _origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


class _Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT  # bound a stalled connection's hold on this thread

    def _send_json(
        self, status: int, obj: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None
    ) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (extra_headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        # This server implements the JSON response mode only; it does not open a
        # server-initiated SSE stream, so GET is not allowed.
        self._send_json(
            405,
            _error(None, -32600, "Method not allowed; POST JSON-RPC to this endpoint"),
            extra_headers={"Allow": "POST"},
        )

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        if self.path.split("?")[0] != ENDPOINT_PATH:
            self._send_json(404, _error(None, -32600, "Not found"))
            return
        if not _origin_allowed(self.headers.get("Origin")):
            self._send_json(403, _error(None, -32600, "Origin not allowed"))
            return
        # http.server does not decode chunked bodies; without Content-Length the
        # body would be silently ignored, so reject it explicitly.
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._send_json(411, _error(None, -32600, "Chunked Transfer-Encoding not supported; send Content-Length"))
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send_json(400, _error(None, -32600, "Invalid Content-Length"))
            return
        if length < 0:
            self._send_json(400, _error(None, -32600, "Invalid Content-Length"))
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, _error(None, -32600, f"Request body exceeds {MAX_BODY_BYTES} bytes"))
            return
        body = self.rfile.read(length) if length else b""
        try:
            message = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, _error(None, -32700, "Parse error"))
            return
        response = handle_message(message)
        if response is None:
            self.send_response(202)
            self.end_headers()
            return
        self._send_json(200, response)

    def log_message(self, *args: Any) -> None:  # silence default request logging
        return


def make_http_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        print(
            f"warning: binding MCP HTTP transport to non-loopback host {host!r}. "
            "This transport is unauthenticated; only bind interfaces you trust.",
            file=sys.stderr,
        )
    return ThreadingHTTPServer((host, port), _Handler)


def serve_http(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = make_http_server(host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentflow-mcp", description="Agentflow MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.transport == "http":
        serve_http(args.host, args.port)
    else:
        serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
