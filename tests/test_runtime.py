from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from agentflow import runtime as runtime_module
from agentflow.runtime import build_runtime_status, validate_runtime_config


class ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"data": [{"id": "chat"}, {"id": "judge"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class RecordingStatusHandler(BaseHTTPRequestHandler):
    """Answers 405 to everything and records raw requests for the I1 test."""

    requests: list = []  # (command, path, body_bytes)

    def do_GET(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        type(self).requests.append((self.command, self.path, body))
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def valid_runtime_config(command: str) -> dict:
    return {
        "schema_version": "0.2.0",
        "default_runtime": "local",
        "runtimes": {
            "local": {
                "adapter": "go-llm",
                "enabled": True,
                "capabilities": {"declared": ["chat", "judge"], "required": ["chat"]},
                "readiness": {"check": "command_exists", "command": command},
            }
        },
        "routes": {
            "reviewer": {
                "primary": "local",
                "fallbacks": [],
                "policy": "prefer_local",
                "requires": ["chat"],
                "allow_degraded": True,
            }
        },
    }


def config_with_mcp(command: str) -> dict:
    config = valid_runtime_config(command)
    config["schema_version"] = "0.3.0"
    config["mcp_servers"] = {
        "github": {
            "enabled": True,
            "transport": "stdio",
            "declared_tools": ["create_issue", "list_prs"],
            "readiness": {"check": "command_exists", "command": command},
        }
    }
    return config


class RuntimeTests(unittest.TestCase):
    def test_validate_runtime_config_rejects_unknown_route_runtime(self) -> None:
        config = valid_runtime_config(sys.executable)
        config["routes"]["reviewer"]["primary"] = "missing"

        findings = validate_runtime_config(config)

        self.assertEqual(findings[0]["severity"], "error")
        self.assertEqual(findings[0]["id"], "route_primary_unknown")

    def test_command_exists_status_is_ready_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["readiness"] = {
                "check": "command_spawn",
                "command": sys.executable,
                "args": ["-c", "raise SystemExit(9)"],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1", allow_probe=False)

            runtime = status["runtimes"][0]
            self.assertEqual(runtime["status"], "ready")
            self.assertEqual(runtime["checks"][0]["name"], "command_exists")
            self.assertEqual(runtime["checks"][0]["severity"], "info")

    def test_command_spawn_runs_only_when_probe_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["readiness"] = {
                "check": "command_spawn",
                "command": sys.executable,
                "args": ["-c", "raise SystemExit(7)"],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1", allow_probe=True)

            runtime = status["runtimes"][0]
            self.assertEqual(runtime["status"], "unavailable")
            self.assertEqual(runtime["checks"][0]["name"], "command_spawn")

    def test_http_probe_derives_observed_capabilities_from_models(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), ModelsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "runtime.config.json"
                config = valid_runtime_config(sys.executable)
                config["runtimes"]["local"]["readiness"] = {
                    "check": "http",
                    "http": {
                        "models_url": f"http://127.0.0.1:{server.server_port}/v1/models",
                        "timeout_seconds": 2,
                    },
                }
                config_path.write_text(json.dumps(config), encoding="utf-8")

                status = build_runtime_status(config_path, record_id="R1", allow_probe=True)

                runtime = status["runtimes"][0]
                self.assertEqual(runtime["status"], "ready")
                self.assertEqual(runtime["observed_capabilities"], ["chat", "judge"])
                self.assertEqual(runtime["capability_source"], "probed")
        finally:
            server.shutdown()
            server.server_close()

    def test_http_probe_is_skipped_without_probe_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["readiness"] = {
                "check": "http",
                "http": {"models_url": "http://127.0.0.1:1/v1/models"},
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with patch("agentflow.runtime._http_models", side_effect=AssertionError("called")):
                status = build_runtime_status(config_path, record_id="R1")

            runtime = status["runtimes"][0]
            self.assertEqual(runtime["status"], "configured")
            self.assertEqual(runtime["checks"][0]["name"], "http")
            self.assertEqual(runtime["checks"][0]["status"], "not_run")

    def test_build_runtime_status_reports_malformed_config_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config_path.write_text(
                json.dumps({"schema_version": "0.2.0", "runtimes": [], "routes": []}),
                encoding="utf-8",
            )

            status = build_runtime_status(config_path, record_id="R1")

            self.assertEqual(status["runtimes"], [])
            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("runtimes_invalid", finding_ids)
            self.assertIn("routes_invalid", finding_ids)

    def test_runtime_status_malformed_config_returns_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            (root / ".agent/runtime.config.json").write_text("{ not json", encoding="utf-8")

            snapshot = build_runtime_status(
                root / ".agent/runtime.config.json", record_id="R1", allow_probe=False
            )

            self.assertEqual(snapshot["runtimes"], [])
            self.assertTrue(
                any(
                    finding["severity"] == "error" and "malformed" in finding["message"]
                    for finding in snapshot["findings"]
                )
            )

    def test_spawn_liveness_treats_long_lived_process_as_reachable(self) -> None:
        reachable, message = runtime_module._spawn_liveness(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            grace=0.3,
        )

        self.assertTrue(reachable)
        self.assertIn("alive", message)

    def test_spawn_liveness_treats_clean_exit_as_reachable(self) -> None:
        reachable, _message = runtime_module._spawn_liveness(
            [sys.executable, "-c", "raise SystemExit(0)"],
            grace=1.0,
        )

        self.assertTrue(reachable)

    def test_spawn_liveness_treats_nonzero_exit_as_unavailable(self) -> None:
        reachable, _message = runtime_module._spawn_liveness(
            [sys.executable, "-c", "raise SystemExit(7)"],
            grace=1.0,
        )

        self.assertFalse(reachable)

    def test_http_models_rejects_non_http_scheme(self) -> None:
        ok, models, message = runtime_module._http_models("file:///etc/passwd", timeout=1)

        self.assertFalse(ok)
        self.assertEqual(models, [])
        self.assertIn("scheme", message)

    def test_http_models_rejects_link_local_metadata_address(self) -> None:
        # Cloud metadata endpoint (169.254.169.254) is the classic SSRF target.
        ok, models, message = runtime_module._http_models(
            "http://169.254.169.254/latest/meta-data/", timeout=1
        )

        self.assertFalse(ok)
        self.assertEqual(models, [])
        self.assertIn("metadata", message)

    def test_http_models_rejects_cgnat_metadata_address(self) -> None:
        ok, _, message = runtime_module._http_models(
            "http://100.100.100.200/latest/meta-data/", timeout=1
        )

        self.assertFalse(ok)
        self.assertIn("metadata", message)

    def test_http_models_rejects_ipv4_compatible_ipv6_metadata(self) -> None:
        # ::a9fe:a9fe embeds 169.254.169.254; v6 encodings must not bypass.
        ok, _, message = runtime_module._http_models(
            "http://[::a9fe:a9fe]/latest/meta-data/", timeout=1
        )

        self.assertFalse(ok)
        self.assertIn("metadata", message)

    def test_http_models_rejects_ipv4_mapped_ipv6_metadata(self) -> None:
        ok, _, message = runtime_module._http_models(
            "http://[::ffff:169.254.169.254]/latest/meta-data/", timeout=1
        )

        self.assertFalse(ok)
        self.assertIn("metadata", message)

    def test_http_models_rejects_6to4_metadata(self) -> None:
        # 2002:a9fe:a9fe:: is 6to4 embedding 169.254.169.254.
        ok, _, message = runtime_module._http_models(
            "http://[2002:a9fe:a9fe::]/latest/meta-data/", timeout=1
        )

        self.assertFalse(ok)
        self.assertIn("metadata", message)

    def test_http_models_rejects_teredo_metadata(self) -> None:
        # 2001:0:4137:9e76::5601:5601 is teredo whose client is 169.254.169.254.
        ok, _, message = runtime_module._http_models(
            "http://[2001:0:4137:9e76::5601:5601]/latest/meta-data/", timeout=1
        )

        self.assertFalse(ok)
        self.assertIn("metadata", message)

    def test_resolve_validated_ip_allows_loopback_and_rfc1918(self) -> None:
        # Local LLM servers (ollama, LM Studio) live on loopback/LAN and must not
        # be blocked. Test the resolver directly: deterministic, no socket open.
        ip, reason = runtime_module._resolve_validated_ip("127.0.0.1", timeout=1)
        self.assertIsNone(reason)
        self.assertEqual(str(ip), "127.0.0.1")
        ip, reason = runtime_module._resolve_validated_ip("192.168.1.50", timeout=1)
        self.assertIsNone(reason)
        self.assertEqual(str(ip), "192.168.1.50")

    def test_resolve_validated_ip_fails_closed_on_resolution_error(self) -> None:
        ip, reason = runtime_module._resolve_validated_ip(
            "no-such-host.invalid", timeout=1
        )
        self.assertIsNone(ip)
        self.assertIsNotNone(reason)
        self.assertIn("cannot resolve", reason)

    def test_http_models_revalidates_redirect_into_metadata(self) -> None:
        # A benign, allowed URL that 302s to the metadata endpoint must be
        # refused at the redirected hop, not followed.
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ok, _, message = runtime_module._http_models(
                f"http://127.0.0.1:{server.server_port}/v1/models", timeout=2
            )
            self.assertFalse(ok)
            self.assertIn("metadata", message)
        finally:
            server.shutdown()
            server.server_close()

    def test_runtime_http_status_probe_marks_declared_capabilities(self) -> None:
        RecordingStatusHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), RecordingStatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "runtime.config.json"
                config = valid_runtime_config(sys.executable)
                config["runtimes"]["local"]["readiness"] = {
                    "check": "http_status",
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    "timeout_seconds": 2,
                }
                config_path.write_text(json.dumps(config), encoding="utf-8")

                status = build_runtime_status(config_path, "R1", allow_probe=True)

                entry = status["runtimes"][0]
                self.assertEqual(entry["status"], "ready")
                self.assertTrue(entry["reachable"])
                self.assertEqual(entry["observed_capabilities"], ["chat", "judge"])
                self.assertEqual(entry["capability_source"], "declared")
                self.assertEqual(
                    entry["checks"][0]["message"], "http status probe succeeded"
                )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_runtime_http_status_skipped_without_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["readiness"] = {
                "check": "http_status",
                "url": "http://127.0.0.1:1/mcp",
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, "R1", allow_probe=False)

            entry = status["runtimes"][0]
            self.assertEqual(entry["status"], "configured")
            self.assertFalse(entry["reachable"])
            self.assertEqual(entry["checks"][0]["status"], "not_run")
            self.assertEqual(
                entry["checks"][0]["message"],
                "http status probe skipped without --probe",
            )

    def test_required_route_unavailable_emits_error_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config("/nonexistent/bin")
            config["routes"]["reviewer"]["required"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("required_route_unavailable", finding_ids)

    def test_disabled_runtime_does_not_become_ready_when_command_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["enabled"] = False
            config["routes"]["reviewer"]["required"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            runtime = status["runtimes"][0]
            self.assertEqual(runtime["status"], "unavailable")
            self.assertEqual(runtime["observed_capabilities"], [])
            self.assertFalse(runtime["reachable"])
            self.assertEqual(status["routes"][0]["status"], "unavailable")
            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("required_route_unavailable", finding_ids)

    def test_non_list_declared_capabilities_returns_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["capabilities"]["declared"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("capabilities_declared_invalid", finding_ids)

    def test_non_list_route_requires_returns_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["routes"]["reviewer"]["requires"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("route_requires_invalid", finding_ids)

    def test_non_string_route_primary_returns_finding_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["routes"]["reviewer"]["primary"] = ["local"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            self.assertEqual(status["routes"][0]["primary"], "__missing__")
            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("route_primary_invalid", finding_ids)

    def test_non_string_route_fallback_returns_finding_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["routes"]["reviewer"]["fallbacks"] = [["local"]]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("route_fallback_invalid", finding_ids)

    def test_route_allow_degraded_promotes_degraded_route_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            config["runtimes"]["local"]["capabilities"] = {
                "declared": ["chat"],
                "required": ["chat"],
            }
            config["routes"]["reviewer"]["requires"] = ["judge"]
            config["routes"]["reviewer"]["allow_degraded"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            route = status["routes"][0]
            self.assertEqual(route["status"], "ready")
            self.assertTrue(route["allow_degraded_applied"])

    def test_route_without_primary_uses_default_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            del config["routes"]["reviewer"]["primary"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            self.assertEqual(status["routes"][0]["primary"], "local")
            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertNotIn("route_primary_unknown", finding_ids)

    def test_route_without_primary_and_default_records_schema_valid_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = valid_runtime_config(sys.executable)
            del config["default_runtime"]
            del config["routes"]["reviewer"]["primary"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status = build_runtime_status(config_path, record_id="R1")

            self.assertEqual(status["routes"][0]["primary"], "__missing__")
            finding_ids = [finding["id"] for finding in status["findings"]]
            self.assertIn("route_primary_unknown", finding_ids)


    def test_validate_mcp_servers_accepts_valid_block(self) -> None:
        findings = validate_runtime_config(config_with_mcp(sys.executable))
        self.assertEqual(findings, [])

    def test_validate_mcp_servers_rejects_bad_fields(self) -> None:
        config = config_with_mcp(sys.executable)
        config["mcp_servers"]["github"]["enabled"] = "yes"
        config["mcp_servers"]["github"]["transport"] = "websocket"
        config["mcp_servers"]["github"]["declared_tools"] = "create_issue"
        config["mcp_servers"]["github"]["readiness"] = {"check": "http"}
        ids = {finding["id"] for finding in validate_runtime_config(config)}
        self.assertIn("mcp_enabled_invalid", ids)
        self.assertIn("mcp_transport_unknown", ids)
        self.assertIn("mcp_declared_tools_invalid", ids)
        self.assertIn("mcp_readiness_check_unknown", ids)

    def test_mcp_invalid_enabled_and_transport_fail_closed_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            config["mcp_servers"]["github"]["enabled"] = "yes"
            config["mcp_servers"]["github"]["transport"] = "websocket"
            config["mcp_servers"]["github"]["readiness"] = {
                "check": "http_status",
                "url": "http://127.0.0.1:9/mcp",
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with patch.object(runtime_module, "_http_status") as http_probe:
                status = build_runtime_status(config_path, "R1", allow_probe=True)

            http_probe.assert_not_called()
            server = status["mcp_servers"][0]
            self.assertFalse(server["enabled"])
            self.assertEqual(server["transport"], "stdio")
            self.assertEqual(server["status"], "unavailable")
            self.assertEqual(server["checks"][0]["status"], "skipped")
            self.assertEqual(server["checks"][0]["message"], "server disabled")

    def test_route_cannot_target_mcp_server(self) -> None:
        # I4: mcp_servers ids are not runtimes; routing to one fails as unknown.
        config = config_with_mcp(sys.executable)
        config["routes"]["reviewer"]["primary"] = "github"
        ids = {finding["id"] for finding in validate_runtime_config(config)}
        self.assertIn("route_primary_unknown", ids)

    def test_http_status_405_is_succeeded(self) -> None:
        RecordingStatusHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), RecordingStatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            self.assertEqual(runtime_module._http_status(url, timeout=2.0), "succeeded")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_http_status_refuses_metadata_and_bad_scheme(self) -> None:
        self.assertEqual(
            runtime_module._http_status("http://169.254.169.254/mcp", 1.0), "refused"
        )
        self.assertEqual(runtime_module._http_status("ftp://host/mcp", 1.0), "refused")

    def test_http_status_dead_port_is_failed(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), RecordingStatusHandler)
        port = server.server_port
        server.server_close()  # port now dead
        self.assertEqual(
            runtime_module._http_status(f"http://127.0.0.1:{port}/mcp", 1.0), "failed"
        )

    def test_http_status_sends_bare_get_and_no_jsonrpc(self) -> None:
        # I1: protocol silence -- one GET, empty body, no MCP handshake tokens.
        RecordingStatusHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), RecordingStatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            runtime_module._http_status(
                f"http://127.0.0.1:{server.server_port}/mcp", 2.0
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(len(RecordingStatusHandler.requests), 1)
        command, _path, body = RecordingStatusHandler.requests[0]
        self.assertEqual(command, "GET")
        self.assertEqual(body, b"")
        self.assertNotIn(b"initialize", body)
        self.assertNotIn(b"tools/list", body)

    def test_mcp_declared_only_is_configured_and_inert(self) -> None:
        # I3: no readiness -> configured; nothing runs even with allow_probe.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            del config["mcp_servers"]["github"]["readiness"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with patch.object(runtime_module, "_http_status") as http_probe, patch.object(
                runtime_module, "_spawn_liveness"
            ) as spawn_probe:
                status = build_runtime_status(config_path, "R1", allow_probe=True)

            http_probe.assert_not_called()
            spawn_probe.assert_not_called()
            server = status["mcp_servers"][0]
            self.assertEqual(server["status"], "configured")
            self.assertFalse(server["reachable"])
            self.assertEqual(server["tool_source"], "declared")
            self.assertEqual(server["declared_tools"], ["create_issue", "list_prs"])
            self.assertEqual(server["declared_tool_count"], 2)
            self.assertEqual(server["checks"][0]["message"], "not probed")

    def test_mcp_command_exists_ready_and_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)  # readiness: command_exists
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status = build_runtime_status(config_path, "R1", allow_probe=False)
            server = status["mcp_servers"][0]
            self.assertEqual(server["status"], "ready")
            self.assertTrue(server["reachable"])
            self.assertEqual(server["checks"][0]["message"], "command found")

            config["mcp_servers"]["github"]["readiness"]["command"] = (
                "definitely-not-a-real-binary-12345"
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status = build_runtime_status(config_path, "R2", allow_probe=False)
            server = status["mcp_servers"][0]
            self.assertEqual(server["status"], "unavailable")
            self.assertEqual(server["checks"][0]["message"], "command not found")
            ids = {finding["id"] for finding in status["findings"]}
            self.assertIn("mcp_server_unavailable", ids)

    def test_mcp_command_exists_without_command_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            config["mcp_servers"]["github"]["readiness"] = {"check": "command_exists"}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status = build_runtime_status(config_path, "R1", allow_probe=False)
            self.assertEqual(status["mcp_servers"][0]["status"], "configured")

    def test_mcp_probe_checks_gated_without_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            config["mcp_servers"]["github"]["readiness"] = {
                "check": "http_status",
                "url": "http://127.0.0.1:1/mcp",
            }
            config["mcp_servers"]["copilot"] = {
                "enabled": True,
                "transport": "stdio",
                "readiness": {"check": "command_spawn", "command": sys.executable},
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with patch.object(runtime_module, "_http_status") as http_probe, patch.object(
                runtime_module, "_spawn_liveness"
            ) as spawn_probe:
                status = build_runtime_status(config_path, "R1", allow_probe=False)

            http_probe.assert_not_called()
            spawn_probe.assert_not_called()
            by_id = {entry["id"]: entry for entry in status["mcp_servers"]}
            self.assertEqual(by_id["github"]["status"], "configured")
            self.assertEqual(
                by_id["github"]["checks"][0]["message"],
                "http status probe skipped without --probe",
            )
            self.assertEqual(by_id["copilot"]["status"], "configured")
            self.assertEqual(
                by_id["copilot"]["checks"][0]["message"],
                "command_spawn skipped without --probe",
            )
            ids = {finding["id"] for finding in status["findings"]}
            self.assertNotIn("mcp_server_unavailable", ids)  # nothing ran -> no warn

    def test_mcp_http_status_probe_405_is_ready(self) -> None:
        RecordingStatusHandler.requests = []
        server_obj = HTTPServer(("127.0.0.1", 0), RecordingStatusHandler)
        thread = threading.Thread(target=server_obj.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "runtime.config.json"
                config = config_with_mcp(sys.executable)
                config["mcp_servers"]["github"]["transport"] = "http"
                config["mcp_servers"]["github"]["readiness"] = {
                    "check": "http_status",
                    "url": f"http://127.0.0.1:{server_obj.server_port}/mcp",
                    "timeout_seconds": 2,
                }
                config_path.write_text(json.dumps(config), encoding="utf-8")
                status = build_runtime_status(config_path, "R1", allow_probe=True)
                server = status["mcp_servers"][0]
                self.assertEqual(server["status"], "ready")
                self.assertEqual(
                    server["checks"][0]["message"], "http status probe succeeded"
                )
        finally:
            server_obj.shutdown()
            thread.join()
            server_obj.server_close()

    def test_mcp_disabled_server_is_unavailable_without_probe_or_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            config["mcp_servers"]["github"]["enabled"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(runtime_module, "_http_status") as http_probe:
                status = build_runtime_status(config_path, "R1", allow_probe=True)
            http_probe.assert_not_called()
            server = status["mcp_servers"][0]
            self.assertEqual(server["status"], "unavailable")
            self.assertEqual(server["checks"][0]["message"], "server disabled")
            ids = {finding["id"] for finding in status["findings"]}
            self.assertNotIn("mcp_server_unavailable", ids)  # disabled != failed probe

    def test_mcp_declared_tools_normalized(self) -> None:
        # P3 finding: strings only, deduped, sorted; count = unique tools.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            config["mcp_servers"]["github"]["declared_tools"] = [
                "list_prs", "create_issue", "list_prs", 7, None,
            ]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status = build_runtime_status(config_path, "R1", allow_probe=False)
            server = status["mcp_servers"][0]
            self.assertEqual(server["declared_tools"], ["create_issue", "list_prs"])
            self.assertEqual(server["declared_tool_count"], 2)

    def test_mcp_snapshot_redacts_probe_targets(self) -> None:
        # I5: url/command/args never appear anywhere in the serialized snapshot.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            secret_url = "http://secret-host.internal:9999/mcp"
            config["mcp_servers"]["github"]["readiness"] = {
                "check": "http_status",
                "url": secret_url,
                "timeout_seconds": 1,
            }
            config["mcp_servers"]["copilot"] = {
                "enabled": True,
                "transport": "stdio",
                "readiness": {
                    "check": "command_spawn",
                    "command": "/opt/secret/mcp-binary",
                    "args": ["--token", "hunter2"],
                },
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status = build_runtime_status(config_path, "R1", allow_probe=True)
            serialized = json.dumps(status)
            self.assertNotIn("secret-host.internal", serialized)
            self.assertNotIn(secret_url, serialized)
            self.assertNotIn("/opt/secret/mcp-binary", serialized)
            self.assertNotIn("hunter2", serialized)

    def test_mcp_command_spawn_receives_no_jsonrpc_on_stdin(self) -> None:
        # I1 (stdio side): the spawned process must see an empty stdin.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "stdin-bytes.txt"
            script = Path(tmp) / "fake-mcp-server.py"
            script.write_text(
                "import sys, pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_bytes(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            config_path = Path(tmp) / "runtime.config.json"
            config = config_with_mcp(sys.executable)
            config["mcp_servers"]["github"]["readiness"] = {
                "check": "command_spawn",
                "command": sys.executable,
                "args": [str(script)],
                "spawn": {"grace_seconds": 5},
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            build_runtime_status(config_path, "R1", allow_probe=True)
            self.assertTrue(not marker.exists() or marker.read_bytes() == b"")


if __name__ == "__main__":
    unittest.main()
