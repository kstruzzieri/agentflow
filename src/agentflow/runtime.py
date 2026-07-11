"""Runtime config validation and readiness snapshots."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlsplit

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

from .artifacts import try_read_json, utc_now
from .contracts import (
    ADAPTERS,
    MCP_READINESS_CHECKS,
    MCP_TRANSPORTS,
    READINESS_CHECKS,
    ROUTE_POLICIES,
    RUNTIME_CONFIG_SCHEMA_VERSION,
    RUNTIME_SNAPSHOT_SCHEMA_VERSION,
)
from .versioning import validate_schema_version


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_runtime_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for message in validate_schema_version(
        config.get("schema_version"), RUNTIME_CONFIG_SCHEMA_VERSION, "runtime-config"
    ):
        findings.append(
            {
                "id": "runtime_config_schema_version",
                "severity": "error",
                "message": message,
            }
        )

    runtimes = config.get("runtimes", {})
    routes = config.get("routes", {})
    if not isinstance(runtimes, dict):
        findings.append(
            {"id": "runtimes_invalid", "severity": "error", "message": "runtimes must be an object"}
        )
        runtimes = {}
    if not isinstance(routes, dict):
        findings.append(
            {"id": "routes_invalid", "severity": "error", "message": "routes must be an object"}
        )
        routes = {}

    default_runtime = config.get("default_runtime")
    if isinstance(default_runtime, str) and default_runtime not in runtimes:
        findings.append(
            {
                "id": "default_runtime_unknown",
                "severity": "error",
                "message": f"default_runtime {default_runtime} is not configured",
            }
        )

    for runtime_id, runtime in runtimes.items():
        if not isinstance(runtime, dict):
            findings.append(
                {
                    "id": "runtime_invalid",
                    "severity": "error",
                    "message": f"runtime {runtime_id} must be an object",
                }
            )
            continue
        adapter = runtime.get("adapter")
        if adapter not in ADAPTERS:
            findings.append(
                {
                    "id": "adapter_unknown",
                    "severity": "error",
                    "message": f"runtime {runtime_id} has unknown adapter {adapter}",
                }
            )
        if not isinstance(runtime.get("enabled"), bool):
            findings.append(
                {
                    "id": "enabled_invalid",
                    "severity": "error",
                    "message": f"runtime {runtime_id}.enabled must be boolean",
                }
            )
        readiness = runtime.get("readiness", {})
        if isinstance(readiness, dict):
            check = readiness.get("check", "command_exists")
            if check not in READINESS_CHECKS:
                findings.append(
                    {
                        "id": "readiness_check_unknown",
                        "severity": "error",
                        "message": f"runtime {runtime_id} has unknown readiness check {check}",
                    }
                )
        capabilities = runtime.get("capabilities", {})
        if isinstance(capabilities, dict):
            for field in ("declared", "required"):
                value = capabilities.get(field, [])
                if field in capabilities and not isinstance(value, list):
                    findings.append(
                        {
                            "id": f"capabilities_{field}_invalid",
                            "severity": "error",
                            "message": (
                                f"runtime {runtime_id}.capabilities.{field} must be an array"
                            ),
                        }
                    )

    for route_name, route in routes.items():
        if not isinstance(route, dict):
            findings.append(
                {
                    "id": "route_invalid",
                    "severity": "error",
                    "message": f"route {route_name} must be an object",
                }
            )
            continue
        primary = route.get("primary")
        primary_lookup = True
        if primary is None:
            primary = default_runtime if isinstance(default_runtime, str) else "__missing__"
        elif not isinstance(primary, str):
            findings.append(
                {
                    "id": "route_primary_invalid",
                    "severity": "error",
                    "message": f"route {route_name}.primary must be a string",
                }
            )
            primary = "__missing__"
            primary_lookup = False
        if primary_lookup and primary not in runtimes:
            findings.append(
                {
                    "id": "route_primary_unknown",
                    "severity": "error",
                    "message": f"route {route_name}.primary references unknown runtime {primary}",
                }
            )
        fallbacks = route.get("fallbacks", [])
        valid_fallbacks = []
        for fallback in fallbacks if isinstance(fallbacks, list) else []:
            if not isinstance(fallback, str):
                findings.append(
                    {
                        "id": "route_fallback_invalid",
                        "severity": "error",
                        "message": f"route {route_name}.fallbacks entries must be strings",
                    }
                )
                continue
            valid_fallbacks.append(fallback)
            if fallback not in runtimes:
                findings.append(
                    {
                        "id": "route_fallback_unknown",
                        "severity": "error",
                        "message": f"route {route_name}.fallbacks references unknown runtime {fallback}",
                    }
                )
        policy = route.get("policy", "manual_only")
        if policy not in ROUTE_POLICIES:
            findings.append(
                {
                    "id": "route_policy_unknown",
                    "severity": "error",
                    "message": f"route {route_name}.policy is unknown: {policy}",
                }
            )
        target_ids = [primary] + valid_fallbacks
        declared = set()
        for target_id in target_ids:
            runtime = runtimes.get(target_id)
            if isinstance(runtime, dict):
                capabilities = runtime.get("capabilities", {})
                if isinstance(capabilities, dict):
                    declared_values = capabilities.get("declared", [])
                    if isinstance(declared_values, list):
                        declared.update(item for item in declared_values if isinstance(item, str))
        requires = route.get("requires", [])
        if not isinstance(requires, list):
            findings.append(
                {
                    "id": "route_requires_invalid",
                    "severity": "error",
                    "message": f"route {route_name}.requires must be an array",
                }
            )
            requires = []
        required = set(item for item in requires if isinstance(item, str))
        missing = sorted(required - declared)
        if missing:
            findings.append(
                {
                    "id": "route_requires_unsatisfied",
                    "severity": "warning",
                    "message": (
                        f"route {route_name} requirements are not declared by "
                        f"targeted runtimes: {', '.join(missing)}"
                    ),
                }
            )
        primary_runtime = runtimes.get(primary)
        if isinstance(primary_runtime, dict) and primary_runtime.get("enabled") is False:
            findings.append(
                {
                    "id": "route_primary_disabled",
                    "severity": "warning",
                    "message": f"route {route_name}.primary runtime {primary} is disabled",
                }
            )

    mcp_servers = config.get("mcp_servers", {})
    if not isinstance(mcp_servers, dict):
        findings.append(
            {
                "id": "mcp_servers_invalid",
                "severity": "error",
                "message": "mcp_servers must be an object",
            }
        )
        mcp_servers = {}
    for server_id, server in mcp_servers.items():
        if not isinstance(server, dict):
            findings.append(
                {
                    "id": "mcp_server_invalid",
                    "severity": "error",
                    "message": f"mcp server {server_id} must be an object",
                }
            )
            continue
        if not isinstance(server.get("enabled"), bool):
            findings.append(
                {
                    "id": "mcp_enabled_invalid",
                    "severity": "error",
                    "message": f"mcp server {server_id}.enabled must be boolean",
                }
            )
        transport = server.get("transport", "stdio")
        if transport not in MCP_TRANSPORTS:
            findings.append(
                {
                    "id": "mcp_transport_unknown",
                    "severity": "error",
                    "message": f"mcp server {server_id} has unknown transport {transport}",
                }
            )
        declared_tools = server.get("declared_tools", [])
        if "declared_tools" in server and not isinstance(declared_tools, list):
            findings.append(
                {
                    "id": "mcp_declared_tools_invalid",
                    "severity": "error",
                    "message": f"mcp server {server_id}.declared_tools must be an array",
                }
            )
        readiness = server.get("readiness", {})
        if isinstance(readiness, dict):
            check = readiness.get("check", "none")
            if check not in MCP_READINESS_CHECKS:
                findings.append(
                    {
                        "id": "mcp_readiness_check_unknown",
                        "severity": "error",
                        "message": f"mcp server {server_id} has unknown readiness check {check}",
                    }
                )

    return findings


def _declared(runtime: Dict[str, Any]) -> List[str]:
    capabilities = runtime.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return []
    declared = capabilities.get("declared", [])
    if not isinstance(declared, list):
        return []
    return sorted(item for item in declared if isinstance(item, str))


def _required(runtime: Dict[str, Any]) -> List[str]:
    capabilities = runtime.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return []
    required = capabilities.get("required", [])
    if not isinstance(required, list):
        return []
    return sorted(item for item in required if isinstance(item, str))


def _command_available(command: Optional[str]) -> bool:
    if not command:
        return False
    if os.path.isabs(command):
        path = Path(command)
        return path.exists() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


# Ranges that front cloud instance-metadata services or "this host":
# AWS/GCP/Azure/Oracle/DO 169.254.169.254, Alibaba 100.100.100.200 (CGNAT),
# AWS IPv6 IMDS fd00:ec2::254, the 0.0.0.0/8 "this network" wildcard, and IPv6
# link-local. These are never legitimate LLM endpoints, so the opt-in `http`
# readiness probe refuses them while loopback/LAN model servers (ollama,
# LM Studio, self-hosted) stay allowed. This denylist is defense-in-depth: the
# user-configured models_url is the de-facto allowlist, and the probe pins to
# the validated IP (below) so it cannot be a general-purpose SSRF primitive.
_METADATA_BLOCK_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)

_MAX_PROBE_REDIRECTS = 5


def _addresses_to_check(ip: IPAddress) -> List[IPAddress]:
    """The address plus any IPv4 it embeds, so v6 encodings can't smuggle a
    blocked v4 (`::ffff:169.254.169.254`, `::a9fe:a9fe`, 6to4, teredo)."""
    checks: List[IPAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            checks.append(ip.ipv4_mapped)
        if ip.sixtofour:
            checks.append(ip.sixtofour)
        if ip.teredo:
            checks.append(ip.teredo[1])
        # IPv4-compatible (deprecated ::/96), excluding :: and ::1.
        if int(ip) >> 32 == 0 and int(ip) > 1:
            checks.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return checks


def _ip_is_blocked(ip: IPAddress) -> bool:
    for candidate in _addresses_to_check(ip):
        for net in _METADATA_BLOCK_NETS:
            if candidate.version == net.version and candidate in net:
                return True
    return False


def _resolve_validated_ip(host: str, timeout: float) -> Tuple[Optional[IPAddress], Optional[str]]:
    """Resolve a host to one safe IP, or return a refusal reason.

    The resolution result is what the probe then connects to (IP pinning), so
    there is no second, unvalidated lookup — closing DNS-rebinding and
    obfuscated-literal (decimal/hex/octal) bypasses. Resolution failure is
    fail-closed: the probe refuses rather than letting a later lookup proceed
    unchecked. A bounded DNS timeout prevents a hostile resolver from hanging
    runtime-status.
    """
    try:
        resolved: List[IPAddress] = [ipaddress.ip_address(host)]
    except ValueError:
        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            resolved = [
                ipaddress.ip_address(info[4][0].split("%", 1)[0])
                for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            ]
        except (socket.gaierror, OSError, ValueError) as exc:
            return None, f"refusing probe: cannot resolve host {host!r}: {exc}"
        finally:
            socket.setdefaulttimeout(previous)
    if not resolved:
        return None, f"refusing probe: host {host!r} resolved to no addresses"
    for ip in resolved:
        if _ip_is_blocked(ip):
            return None, f"refusing probe to instance-metadata/internal address: {ip}"
    return resolved[0], None


def _probe_once(url: str, timeout: float) -> Tuple[Optional[http.client.HTTPResponse], Optional[str]]:
    """Open one hop, connecting to the validated, pinned IP (not a re-resolve)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None, f"refusing non-http(s) probe scheme: {url!r}"
    host = parts.hostname
    if not host:
        return None, f"refusing probe: no host in {url!r}"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ip, reason = _resolve_validated_ip(host, timeout)
    if reason:
        return None, reason
    raw = socket.create_connection((str(ip), port), timeout=timeout)
    try:
        if parts.scheme == "https":
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = raw
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        authority = host if port in (80, 443) else f"{host}:{port}"
        conn.request("GET", target, headers={"Host": authority})
        return conn.getresponse(), None
    except Exception:
        raw.close()
        raise


def _http_models(url: str, timeout: float) -> Tuple[bool, List[str], str]:
    if urlsplit(url).scheme not in ("http", "https"):
        return False, [], f"refusing non-http(s) probe scheme: {url!r}"
    current = url
    try:
        for _ in range(_MAX_PROBE_REDIRECTS + 1):
            response, reason = _probe_once(current, timeout)
            if reason:
                return False, [], reason
            try:
                if 300 <= response.status < 400:
                    location = response.getheader("Location")
                    if not location:
                        return False, [], f"redirect ({response.status}) without Location"
                    # Re-validate every hop: a benign URL cannot redirect into a
                    # blocked range.
                    current = urljoin(current, location)
                    continue
                if response.status < 200 or response.status >= 300:
                    return False, [], f"HTTP status {response.status}"
                payload = json.loads(response.read().decode("utf-8"))
                break
            finally:
                response.close()
        else:
            return False, [], f"refusing probe: more than {_MAX_PROBE_REDIRECTS} redirects"
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
        return False, [], str(exc)
    models = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        model_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(model_id, str):
            models.append(model_id)
    return True, sorted(models), "HTTP probe succeeded"


def _http_status(url: str, timeout: float) -> str:
    """Liveness-only probe for #19: one GET, status line only, body never read.

    Returns "succeeded" (any HTTP status line, 3xx-5xx included -- the endpoint
    answered), "refused" (scheme/SSRF/resolution guard blocked the probe), or
    "failed" (connection error / timeout). Redirects are NOT followed: a
    redirect status line already proves liveness. Raw error text (which can
    embed host/IP/url) is discarded here so the snapshot layer only ever sees
    the outcome enum (I5).
    """
    try:
        response, reason = _probe_once(url, timeout)
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        return "failed"
    if reason:
        return "refused"
    response.close()
    return "succeeded"


def _spawn_liveness(command_args: List[str], grace: float) -> Tuple[bool, str]:
    """Treat a process that stays alive past grace as reachable."""
    if not command_args:
        return False, "no command configured for command_spawn"
    try:
        proc = subprocess.Popen(
            command_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, TypeError, ValueError) as exc:
        return False, f"failed to spawn: {exc}"
    try:
        returncode = proc.wait(timeout=grace)
        return returncode == 0, f"command exited {returncode}"
    except subprocess.TimeoutExpired:
        return True, "process alive after grace period"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


def _normalized_declared_tools(server: Dict[str, Any]) -> List[str]:
    # #19/P3: strings only, deduped, sorted -> stable snapshots and proof diffs.
    tools = server.get("declared_tools", [])
    if not isinstance(tools, list):
        return []
    return sorted({item for item in tools if isinstance(item, str)})


def _mcp_server_status(
    server_id: str, server: Dict[str, Any], allow_probe: bool
) -> Dict[str, Any]:
    """Evidence-only MCP status per the #19 rule table.

    Messages come from a closed vocabulary; probe targets (url/command/args)
    are never echoed into the snapshot (I5). No MCP/JSON-RPC is ever spoken
    (I1) -- liveness only.
    """
    declared_tools = _normalized_declared_tools(server)
    readiness = server.get("readiness", {})
    if not isinstance(readiness, dict):
        readiness = {}
    # Absent readiness is declared-only ("not probed"); an explicit
    # {"check": "none"} is a deliberate opt-out ("readiness check disabled").
    declared_only = not readiness
    check = readiness.get("check", "none")
    enabled = server.get("enabled") is True
    transport = server.get("transport", "stdio")
    if transport not in MCP_TRANSPORTS:
        transport = "stdio"

    status = "configured"
    reachable = False
    check_status = "not_run"
    message = "not probed"
    probe_ran = False

    if not enabled:
        status = "unavailable"
        check_status = "skipped"
        message = "server disabled"
    elif check == "none":
        if not declared_only:
            check_status = "not_applicable"
            message = "readiness check disabled"
        # declared-only stays not_run / "not probed"
    elif check == "command_exists":
        command = readiness.get("command")
        if isinstance(command, str) and command:
            probe_ran = True
            reachable = _command_available(command)
            status = "ready" if reachable else "unavailable"
            check_status = "passed" if reachable else "failed"
            message = "command found" if reachable else "command not found"
        # no command -> stays configured / not probed
    elif check == "command_spawn":
        if allow_probe:
            command = readiness.get("command")
            args = readiness.get("args", [])
            command_args = (
                [command] + args
                if isinstance(command, str) and isinstance(args, list)
                else []
            )
            spawn_config = readiness.get("spawn", {})
            if not isinstance(spawn_config, dict):
                spawn_config = {}
            try:
                grace = float(spawn_config.get("grace_seconds", 0.5))
            except (TypeError, ValueError):
                grace = 0.5
            probe_ran = True
            reachable, _raw = _spawn_liveness(command_args, max(0.0, grace))
            status = "ready" if reachable else "unavailable"
            check_status = "passed" if reachable else "failed"
            # fixed vocabulary: discard _spawn_liveness's raw message (I5)
            message = "process alive after grace period" if reachable else "command exited"
        else:
            message = "command_spawn skipped without --probe"
    elif check == "http_status":
        if allow_probe:
            url = readiness.get("url", "")
            try:
                timeout = float(readiness.get("timeout_seconds", 2))
            except (TypeError, ValueError):
                timeout = 2.0
            probe_ran = True
            outcome = _http_status(url if isinstance(url, str) else "", timeout)
            reachable = outcome == "succeeded"
            status = "ready" if reachable else "unavailable"
            check_status = "passed" if reachable else "failed"
            message = f"http status probe {outcome}"
        else:
            message = "http status probe skipped without --probe"

    entry = {
        "id": server_id,
        "transport": transport,
        "enabled": enabled,
        "status": status,
        "reachable": reachable,
        "declared_tools": declared_tools,
        "declared_tool_count": len(declared_tools),
        "tool_source": "declared",
        "checks": [
            {
                "name": check,
                "status": check_status,
                "severity": "info",
                "message": message,
            }
        ],
    }
    entry["_probe_ran"] = probe_ran  # stripped by the caller after findings
    return entry


def build_runtime_status(
    config_path: Path,
    record_id: str,
    allow_probe: bool = False,
) -> Dict[str, Any]:
    config, read_error = try_read_json(config_path)
    config_hash = sha256_path(config_path)
    if config is None:
        return {
            "schema_version": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
            "id": record_id,
            "created_at": utc_now(),
            "runtime_config_sha256": config_hash,
            "runtimes": [],
            "routes": [],
            "mcp_servers": [],
            "findings": [
                {
                    "id": "runtime_config_malformed",
                    "severity": "error",
                    "message": read_error,
                }
            ],
        }
    findings = validate_runtime_config(config)
    runtimes = config.get("runtimes", {})
    routes = config.get("routes", {})
    default_runtime = config.get("default_runtime")
    if not isinstance(runtimes, dict):
        runtimes = {}
    if not isinstance(routes, dict):
        routes = {}
    runtime_statuses: List[Dict[str, Any]] = []
    runtime_lookup: Dict[str, Dict[str, Any]] = {}

    for runtime_id, runtime in sorted(runtimes.items()):
        if not isinstance(runtime, dict):
            continue
        declared = _declared(runtime)
        required = _required(runtime)
        readiness = runtime.get("readiness", {})
        if not isinstance(readiness, dict):
            readiness = {}
        check = readiness.get("check", "command_exists")
        reachable = False
        observed: List[str] = []
        capability_source = "none"
        check_name = check
        severity = "info"
        status = "configured"
        message = "not probed"
        check_status = "failed"
        probe_skipped = False

        if check == "none":
            status = "configured"
            message = "readiness check disabled"
            check_status = "not_applicable"
        elif check == "http":
            if allow_probe:
                http_config = readiness.get("http", {})
                if not isinstance(http_config, dict):
                    http_config = {}
                url = http_config.get("models_url", "")
                try:
                    timeout = float(http_config.get("timeout_seconds", 2))
                except (TypeError, ValueError):
                    timeout = 2.0
                reachable, models, message = _http_models(url, timeout)
                observed = sorted(set(models).intersection(declared))
                capability_source = "probed"
            else:
                message = "http probe skipped without --probe"
                check_status = "not_run"
                probe_skipped = True
        elif check == "http_status":
            if allow_probe:
                url = readiness.get("url", "")
                try:
                    timeout = float(readiness.get("timeout_seconds", 2))
                except (TypeError, ValueError):
                    timeout = 2.0
                outcome = _http_status(url if isinstance(url, str) else "", timeout)
                reachable = outcome == "succeeded"
                message = f"http status probe {outcome}"
                check_status = "passed" if reachable else "failed"
            else:
                message = "http status probe skipped without --probe"
                check_status = "not_run"
                probe_skipped = True
        elif check == "command_spawn" and allow_probe:
            command = readiness.get("command")
            args = readiness.get("args", [])
            command_args = [command] + args if isinstance(command, str) and isinstance(args, list) else []
            spawn_config = readiness.get("spawn", {})
            if not isinstance(spawn_config, dict):
                spawn_config = {}
            try:
                grace = float(spawn_config.get("grace_seconds", 0.5))
            except (TypeError, ValueError):
                grace = 0.5
            reachable, message = _spawn_liveness(command_args, max(0.0, grace))
        else:
            if check == "command_spawn":
                check_name = "command_exists"
                message = "command_spawn downgraded to command_exists without --probe"
            command = readiness.get("command")
            reachable = _command_available(command if isinstance(command, str) else None)
            if check != "command_spawn":
                message = "command exists" if reachable else "command not found"

        if reachable:
            if capability_source == "none":
                observed = declared
                capability_source = "declared"
            missing = sorted(set(required) - set(observed))
            status = "degraded" if missing else "ready"
            check_status = "passed"
        elif check != "none" and not probe_skipped:
            status = "unavailable"
        if runtime.get("enabled") is False:
            reachable = False
            observed = []
            capability_source = "none"
            status = "unavailable"
            check_status = "skipped"
            message = "runtime disabled"

        runtime_entry = {
            "id": runtime_id,
            "adapter": runtime.get("adapter", "custom"),
            "enabled": bool(runtime.get("enabled")),
            "status": status,
            "declared_capabilities": declared,
            "observed_capabilities": observed,
            "capability_source": capability_source,
            "reachable": reachable,
            "version": "unknown",
            "checks": [
                {
                    "name": check_name,
                    "status": check_status,
                    "severity": severity,
                    "message": message,
                }
            ],
        }
        runtime_statuses.append(runtime_entry)
        runtime_lookup[runtime_id] = runtime_entry

    for entry in runtime_statuses:
        if entry["status"] == "unavailable" and entry["enabled"]:
            findings.append(
                {
                    "id": "runtime_unavailable",
                    "severity": "warning",
                    "message": f"runtime {entry['id']} is unavailable",
                }
            )

    route_statuses: List[Dict[str, Any]] = []
    for role, route in sorted(routes.items()):
        if not isinstance(route, dict):
            continue
        primary = route.get("primary")
        if primary is None and isinstance(default_runtime, str):
            primary = default_runtime
        if not isinstance(primary, str):
            primary = "__missing__"
        primary_status = runtime_lookup.get(
            primary, {"status": "unavailable", "observed_capabilities": []}
        )
        route_requires = route.get("requires", [])
        if not isinstance(route_requires, list):
            route_requires = []
        required = sorted(item for item in route_requires if isinstance(item, str))
        observed = set(primary_status.get("observed_capabilities", []))
        missing = sorted(set(required) - observed)
        route_entry = {
            "role": role,
            "primary": primary,
            "status": (
                "degraded"
                if missing and primary_status.get("status") == "ready"
                else primary_status.get("status", "unavailable")
            ),
            "missing_capabilities": missing,
        }
        if route.get("allow_degraded") is True and route_entry["status"] == "degraded":
            route_entry["status"] = "ready"
            route_entry["allow_degraded_applied"] = True
        route_statuses.append(route_entry)
        if route.get("required") is True and route_entry["status"] == "unavailable":
            findings.append(
                {
                    "id": "required_route_unavailable",
                    "severity": "error",
                    "message": f"required route {role} is unavailable",
                }
            )

    mcp_statuses: List[Dict[str, Any]] = []
    mcp_servers = config.get("mcp_servers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    for server_id, server in sorted(mcp_servers.items()):
        if not isinstance(server, dict):
            continue
        entry = _mcp_server_status(server_id, server, allow_probe)
        probe_ran = entry.pop("_probe_ran")
        if entry["enabled"] and probe_ran and entry["status"] == "unavailable":
            findings.append(
                {
                    "id": "mcp_server_unavailable",
                    "severity": "warning",
                    "message": f"mcp server {entry['id']} is unavailable",
                }
            )
        mcp_statuses.append(entry)

    return {
        "schema_version": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
        "id": record_id,
        "created_at": utc_now(),
        "runtime_config_sha256": config_hash,
        "runtimes": runtime_statuses,
        "routes": route_statuses,
        "mcp_servers": mcp_statuses,
        "findings": findings,
    }
