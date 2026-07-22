from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentflow import __version__
from agentflow import mcp_server as m

REQUIRED_TOOLS = {
    "next_step",
    "claim_step",
    "amend_step",
    "complete_step",
    "verify_step",
    "verify_run",
    "build_proof",
    "verify_proof",
    "status",
    "audit_drift",
    "reclaim_step",
    "renew_lease",
}


def call(method: str, msg_id=1, **params):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        message["params"] = params
    return m.handle_message(message)


class RegistryTests(unittest.TestCase):
    def test_initialize_reports_server_info(self) -> None:
        result = call("initialize")["result"]
        self.assertEqual(result["serverInfo"], {"name": "agentflow", "version": __version__})
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["protocolVersion"], m.PROTOCOL_VERSION)

    def test_initialize_always_returns_implemented_protocol_version(self) -> None:
        # The server implements exactly one version; per spec it must answer with
        # that version regardless of what (if anything) the client requests.
        for requested in ("2025-03-26", "1999-01-01", None):
            response = m.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": requested},
                }
            )
            self.assertEqual(response["result"]["protocolVersion"], m.PROTOCOL_VERSION)

    def test_ping_returns_empty_result(self) -> None:
        self.assertEqual(call("ping")["result"], {})

    def test_tools_list_includes_required_tools(self) -> None:
        tools = call("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue(REQUIRED_TOOLS.issubset(names))
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("description", tool)

    def test_amend_step_tool_registered_and_builds_argv(self) -> None:
        tool = m.TOOLS_BY_NAME["amend_step"]
        self.assertEqual(tool.input_schema["required"], ["step", "agent", "reason"])
        argv = tool.argv(
            {
                "step": "P1",
                "agent": "a",
                "reason": "review fix",
                "reason_code": "review_feedback",
                "root": ".",
            }
        )
        self.assertEqual(
            argv,
            [
                "amend-step", "P1",
                "--agent", "a",
                "--reason", "review fix",
                "--json",
                "--root", ".",
                "--reason-code", "review_feedback",
            ],
        )

    def test_reclaim_step_tool_registered_and_builds_argv(self) -> None:
        names = {tool["name"] for tool in call("tools/list")["result"]["tools"]}
        self.assertIn("reclaim_step", names)
        tool = m.TOOLS_BY_NAME["reclaim_step"]
        self.assertEqual(tool.input_schema["required"], ["step", "agent", "reason"])
        self.assertEqual(
            tool.argv({"step": "P1", "agent": "b", "reason": "crash", "root": "."}),
            ["reclaim-step", "P1", "--agent", "b", "--reason", "crash", "--json", "--root", "."],
        )
        self.assertEqual(
            tool.argv({"step": "P1", "agent": "b", "reason": "crash", "lease_minutes": 45, "root": "."}),
            [
                "reclaim-step", "P1", "--agent", "b", "--reason", "crash",
                "--json", "--root", ".", "--lease-minutes", "45",
            ],
        )

    def test_renew_lease_tool_registered_and_builds_argv(self) -> None:
        names = {tool["name"] for tool in call("tools/list")["result"]["tools"]}
        self.assertIn("renew_lease", names)
        tool = m.TOOLS_BY_NAME["renew_lease"]
        self.assertEqual(tool.input_schema["required"], ["step", "agent"])
        self.assertEqual(
            tool.argv({"step": "P1", "agent": "a", "attempt": "A1", "minutes": 60, "root": "."}),
            [
                "renew-lease", "P1", "--agent", "a", "--json", "--root", ".",
                "--attempt", "A1", "--minutes", "60",
            ],
        )

    def test_lifecycle_tools_expose_agent_input(self) -> None:
        for name in ("complete_step", "verify_step", "finish_step"):
            self.assertIn("agent", m.TOOLS_BY_NAME[name].input_schema["properties"])

    def test_complete_step_forwards_agent(self) -> None:
        tool = m.TOOLS_BY_NAME["complete_step"]
        self.assertEqual(
            tool.argv({"step": "P1", "agent": "a", "root": "."}),
            ["complete-step", "P1", "--json", "--root", ".", "--agent", "a"],
        )

    def test_verify_proof_argv_forwards_strict(self) -> None:
        tool = m.TOOLS_BY_NAME["verify_proof"]
        self.assertEqual(tool.argv({"root": "."}), ["verify-proof", "--root", "."])
        self.assertEqual(
            tool.argv({"root": ".", "strict": True}),
            ["verify-proof", "--root", ".", "--strict"],
        )

    def test_verify_proof_tool_exposes_strict_input(self) -> None:
        schema = m.TOOLS_BY_NAME["verify_proof"].input_schema
        self.assertIn("strict", schema["properties"])

    def test_arbitrary_command_tools_not_exposed(self) -> None:
        names = {tool["name"] for tool in call("tools/list")["result"]["tools"]}
        self.assertNotIn("run", names)
        self.assertNotIn("record_command", names)

    def test_unknown_method_is_method_not_found(self) -> None:
        self.assertEqual(call("bogus")["error"]["code"], -32601)

    def test_notification_returns_none(self) -> None:
        self.assertIsNone(m.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_non_object_message_is_invalid_request(self) -> None:
        response = m.handle_message(["not", "an", "object"])
        self.assertEqual(response["error"]["code"], -32600)

    def test_invalid_jsonrpc_version_is_invalid_request(self) -> None:
        response = m.handle_message({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(response["error"]["code"], -32600)

    def test_tools_call_rejects_non_object_params(self) -> None:
        response = m.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("expected object", response["error"]["message"])

    def test_tools_call_rejects_non_object_arguments(self) -> None:
        response = call("tools/call", name="status", arguments=[])
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("expected object", response["error"]["message"])


class ToolsCallTests(unittest.TestCase):
    def _init_root(self) -> str:
        tmp = tempfile.mkdtemp()
        code, _out, _err = m.run_cli(["init", "--root", tmp])
        self.assertEqual(code, 0)
        return tmp

    def test_status_on_initialized_root(self) -> None:
        root = self._init_root()
        result = call("tools/call", name="status", arguments={"root": root})["result"]
        self.assertFalse(result["isError"])
        self.assertIn("agentflow", result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["exit_code"], 0)

    def test_status_uninitialized_root_is_error(self) -> None:
        tmp = tempfile.mkdtemp()
        result = call("tools/call", name="status", arguments={"root": tmp})["result"]
        self.assertTrue(result["isError"])

    def test_next_step_returns_design_decision_ids_as_structured_data(self) -> None:
        root = Path(self._init_root())
        plan_path = root / ".agent/plan.lock.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan.update(
            {
                "schema_version": "0.4.0",
                "objective": "Exercise MCP step passthrough.",
                "scope": ["Return the raw step."],
                "invariants": ["MCP does not reinterpret decisions."],
                "allowed_files": ["fixture.txt", ".agent/"],
                "validation_gates": ["manual inspection"],
                "rollback_plan": "Delete the fixture.",
                "steps": [
                    {
                        "id": "P1",
                        "action": "Create fixture.",
                        "files": ["fixture.txt"],
                        "preconditions": [],
                        "expected_diff": ["Fixture exists."],
                        "validation": ["manual inspection"],
                        "evidence_ids": [],
                        "design_decision_ids": ["DD-1"],
                    }
                ],
                "design_decisions": [
                    {"id": "DD-1", "text": "Keep the adapter transparent."}
                ],
                "locked": True,
            }
        )
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

        result = call("tools/call", name="next_step", arguments={"root": str(root)})["result"]

        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(
            result["structuredContent"]["data"]["design_decision_ids"],
            ["DD-1"],
        )

    def test_unknown_tool_returns_error(self) -> None:
        response = call("tools/call", name="nope", arguments={})
        self.assertEqual(response["error"]["code"], -32602)

    def test_missing_required_argument_returns_error(self) -> None:
        response = call("tools/call", name="claim_step", arguments={"root": "."})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("step", response["error"]["message"])

    def test_run_cli_returns_exit_code_and_output(self) -> None:
        code, out, err = m.run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("agentflow", out + err)

    def test_run_cli_surfaces_unexpected_exception(self) -> None:
        original_main = m.cli.main

        def boom(_argv):
            raise RuntimeError("kaboom")

        m.cli.main = boom
        try:
            code, _out, err = m.run_cli(["status"])
        finally:
            m.cli.main = original_main
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: kaboom", err)

    def test_run_cli_serializes_concurrent_calls(self) -> None:
        original_main = m.cli.main
        active = 0
        saw_overlap = False
        lock = threading.Lock()
        start = threading.Event()

        def fake_main(_argv):
            nonlocal active, saw_overlap
            with lock:
                active += 1
                saw_overlap = saw_overlap or active > 1
            time.sleep(0.05)
            print("ok")
            with lock:
                active -= 1
            return 0

        m.cli.main = fake_main
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(lambda: (start.wait(), m.run_cli(["status"]))) for _ in range(4)]
                start.set()
                results = [future.result(timeout=5) for future in futures]
        finally:
            m.cli.main = original_main

        self.assertFalse(saw_overlap)
        self.assertEqual([code for _waited, (code, _out, _err) in results], [0, 0, 0, 0])


class ArgvBuilderTests(unittest.TestCase):
    def test_verify_run_no_record_flag(self) -> None:
        self.assertIn("--no-record", m._argv_verify_run({"record": False}))
        self.assertNotIn("--no-record", m._argv_verify_run({"record": True}))

    def test_claim_step_optional_lease(self) -> None:
        argv = m._argv_claim_step({"step": "P1", "agent": "me", "lease_minutes": 30})
        self.assertEqual(argv[:3], ["claim-step", "P1", "--agent"])
        self.assertIn("--lease-minutes", argv)
        self.assertIn("30", argv)

    def test_verify_step_flags(self) -> None:
        argv = m._argv_verify_step({"step": "P1", "strict": True, "replay": True})
        self.assertIn("--strict", argv)
        self.assertIn("--replay", argv)


class StdioTransportTests(unittest.TestCase):
    def test_serve_stdio_handles_messages_and_skips_notifications(self) -> None:
        lines = (
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                ]
            )
            + "\n"
        )
        out = io.StringIO()
        m.serve_stdio(io.StringIO(lines), out)
        responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual([r["id"] for r in responses], [1, 2])

    def test_serve_stdio_parse_error(self) -> None:
        out = io.StringIO()
        m.serve_stdio(io.StringIO("not json\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


class HttpTransportTests(unittest.TestCase):
    @contextlib.contextmanager
    def _server(self):
        server = m.make_http_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _post(self, port: int, payload, path=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path or m.ENDPOINT_PATH}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.status, json.loads(exc.read() or b"{}")

    def test_http_initialize_and_tools_list(self) -> None:
        with self._server() as port:
            status1, init = self._post(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            status2, listed = self._post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual((status1, status2), (200, 200))
        self.assertEqual(init["result"]["serverInfo"]["name"], "agentflow")
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertTrue(REQUIRED_TOOLS.issubset(names))

    def test_http_rejects_wrong_path(self) -> None:
        with self._server() as port:
            status, response = self._post(port, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/wrong")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], -32600)

    def test_http_rejects_non_local_origin(self) -> None:
        with self._server() as port:
            status, response = self._post(
                port, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="http://example.com"
            )
        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], -32600)

    def test_http_rejects_localhost_prefix_origin_bypass(self) -> None:
        with self._server() as port:
            status, response = self._post(
                port, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="http://127.0.0.1.evil.com"
            )
        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], -32600)

    def test_http_rejects_invalid_content_length(self) -> None:
        with self._server() as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.putrequest("POST", m.ENDPOINT_PATH)
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "not-an-int")
                connection.endheaders()
                response = connection.getresponse()
                status = response.status
                body = json.loads(response.read() or b"{}")
            finally:
                connection.close()
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32600)

    def test_http_rejects_chunked_body(self) -> None:
        with self._server() as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.putrequest("POST", m.ENDPOINT_PATH, skip_accept_encoding=True)
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Transfer-Encoding", "chunked")
                connection.endheaders()
                response = connection.getresponse()
                status = response.status
                body = json.loads(response.read() or b"{}")
            finally:
                connection.close()
        self.assertEqual(status, 411)
        self.assertEqual(body["error"]["code"], -32600)

    def test_http_notification_returns_202(self) -> None:
        with self._server() as port:
            status, _body = self._post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)

    def test_http_rejects_oversized_body(self) -> None:
        original_max = m.MAX_BODY_BYTES
        m.MAX_BODY_BYTES = 5
        try:
            with self._server() as port:
                status, response = self._post(port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        finally:
            m.MAX_BODY_BYTES = original_max
        self.assertEqual(status, 413)
        self.assertEqual(response["error"]["code"], -32600)

    def test_http_get_not_allowed(self) -> None:
        with self._server() as port:
            request = urllib.request.Request(f"http://127.0.0.1:{port}{m.ENDPOINT_PATH}")
            try:
                with urllib.request.urlopen(request) as resp:
                    status, body, allow = resp.status, json.loads(resp.read() or b"{}"), resp.headers.get("Allow")
            except urllib.error.HTTPError as exc:
                status, body, allow = exc.status, json.loads(exc.read() or b"{}"), exc.headers.get("Allow")
        self.assertEqual(status, 405)
        self.assertEqual(allow, "POST")
        self.assertEqual(body["error"]["code"], -32600)

    def test_main_parses_transport_flags(self) -> None:
        args = m.build_parser().parse_args(["--transport", "http", "--port", "9", "--host", "127.0.0.1"])
        self.assertEqual((args.transport, args.port), ("http", 9))


from agentflow.mcp_server import TOOLS_BY_NAME


class ReviewToolTests(unittest.TestCase):
    def test_record_review_argv(self) -> None:
        tool = TOOLS_BY_NAME["record_review"]
        argv = tool.argv({"manifest": "docs/ai/state/main/review-manifest.json", "root": "."})
        self.assertEqual(
            argv,
            ["record-review", "--manifest", "docs/ai/state/main/review-manifest.json", "--json", "--root", "."],
        )

    def test_record_review_emit_evidence(self) -> None:
        tool = TOOLS_BY_NAME["record_review"]
        argv = tool.argv({"manifest": "m.json", "emit_evidence": True})
        self.assertIn("--emit-evidence", argv)

    def test_amend_step_finding_refs_argv(self) -> None:
        tool = TOOLS_BY_NAME["amend_step"]
        argv = tool.argv(
            {
                "step": "P1",
                "agent": "a",
                "reason": "r",
                "finding_refs": ["RR-20260620T180000Z-ab12cd34#BP-001"],
            }
        )
        self.assertIn("--finding", argv)
        self.assertIn("RR-20260620T180000Z-ab12cd34#BP-001", argv)


class PorcelainToolTests(unittest.TestCase):
    def test_new_tools_registered(self) -> None:
        names = {t.name for t in m.TOOLS}
        self.assertTrue({"next_action", "finish_step", "finish_run"} <= names)

    def test_next_action_argv_strict(self) -> None:
        self.assertEqual(m._argv_next_action({"root": "."}), ["next-action", "--json", "--root", "."])
        self.assertIn("--strict", m._argv_next_action({"strict": True}))
        self.assertEqual(
            m._argv_next_action({"agent": "worker-a"}),
            ["next-action", "--json", "--root", ".", "--agent", "worker-a"],
        )
        self.assertIn(
            "agent",
            m.TOOLS_BY_NAME["next_action"].input_schema["properties"],
        )

    def test_finish_step_argv_flags(self) -> None:
        argv = m._argv_finish_step({"step": "P1", "attempt": "A1", "strict": True, "replay": True})
        self.assertEqual(argv[:3], ["finish-step", "P1", "--json"])
        self.assertIn("--attempt", argv)
        self.assertIn("A1", argv)
        self.assertIn("--strict", argv)
        self.assertIn("--replay", argv)

    def test_finish_run_argv_strict(self) -> None:
        self.assertEqual(m._argv_finish_run({"root": "."}), ["finish-run", "--json", "--root", "."])
        self.assertIn("--strict", m._argv_finish_run({"strict": True}))

    def test_next_action_tool_exposes_parsed_data(self) -> None:
        tmp = tempfile.mkdtemp()
        result = call(
            "tools/call",
            name="next_action",
            arguments={"root": tmp, "agent": "worker-mcp"},
        )["result"]
        self.assertEqual(result["structuredContent"]["data"]["state"], "uninitialized")
        self.assertEqual(
            result["structuredContent"]["data"]["resumability"]["agent_id"],
            "worker-mcp",
        )

    def test_finish_run_tool_exposes_parsed_data(self) -> None:
        tmp = tempfile.mkdtemp()
        result = call("tools/call", name="finish_run", arguments={"root": tmp})["result"]
        self.assertIn("ok", result["structuredContent"]["data"])


if __name__ == "__main__":
    unittest.main()
