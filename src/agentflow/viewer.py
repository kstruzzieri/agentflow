"""Static HTML proof viewer (#18).

Renders a self-contained, dependency-free report from ``.agent/proof-pack.json``
and related ledgers. The report is a review aid only; ``verify-proof`` remains
the authoritative check. The renderer emits no ``<script>`` tags and no
external references, and every interpolated value is HTML-escaped.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional

from .artifacts import read_jsonl, try_read_json
from .contracts import EXECUTION_ARTIFACT_PATHS
from .execution import read_step_state


def _try_read_jsonl(path: Path, warnings: List[str]) -> List[Dict[str, Any]]:
    try:
        return read_jsonl(path)
    except (OSError, ValueError):
        warnings.append(f"{path.name} ledger unreadable; section omitted")
        return []


def _try_read_json(path: Path) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return try_read_json(path)
    except OSError as exc:
        return None, str(exc)


def _receipt_href(root: Path, output_dir: Path, relative_path: Any) -> Optional[str]:
    if not isinstance(relative_path, str) or not relative_path:
        return None
    root = root.resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        return None
    if not target.exists():
        return None
    href = os.path.relpath(target, output_dir.resolve()).replace(os.sep, "/")
    if urlsplit(href).scheme:
        return None
    return href


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _steps(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [step for step in _list(plan.get("steps")) if isinstance(step, dict)]


def collect_view_model(root: Path, proof_path: Path, output_path: Path) -> Dict[str, Any]:
    """Read proof + ledgers into a render model. Only the proof is required."""
    if not proof_path.exists():
        raise ValueError(
            f"proof metadata missing: {proof_path}; run `agentflow build-proof` first"
        )
    proof, proof_error = _try_read_json(proof_path)
    if proof is None:
        raise ValueError(f"proof metadata unreadable: {proof_error}; run `agentflow build-proof`")

    warnings: List[str] = []
    plan: Optional[Dict[str, Any]] = None
    plan_path = root / ".agent/plan.lock.json"
    if plan_path.exists():
        plan, plan_error = _try_read_json(plan_path)
        if plan is None:
            warnings.append(f"plan.lock.json unreadable: {plan_error}")

    output_dir = output_path.parent
    command_rows = _try_read_jsonl(root / EXECUTION_ARTIFACT_PATHS["command-receipts"], warnings)
    command_receipts = []
    for receipt in command_rows:
        if not isinstance(receipt, dict):
            continue
        enriched = dict(receipt)
        enriched["stdout_href"] = _receipt_href(root, output_dir, receipt.get("stdout_path"))
        enriched["stderr_href"] = _receipt_href(root, output_dir, receipt.get("stderr_path"))
        command_receipts.append(enriched)
    file_receipts = [
        receipt
        for receipt in _try_read_jsonl(root / EXECUTION_ARTIFACT_PATHS["file-receipts"], warnings)
        if isinstance(receipt, dict)
    ]

    step_state: Optional[Dict[str, Any]] = None
    if (root / EXECUTION_ARTIFACT_PATHS["execution-contract"]).exists():
        try:
            step_state = read_step_state(root)
        except (OSError, ValueError):
            warnings.append("step-runs.jsonl ledger unreadable; step status omitted")

    drift: Optional[Dict[str, Any]] = None
    drift_path = root / ".agent/drift-report.json"
    if drift_path.exists():
        drift, drift_error = _try_read_json(drift_path)
        if drift is None:
            warnings.append(f"drift-report.json unreadable: {drift_error}")

    return {
        "proof": proof,
        "plan": plan,
        "command_receipts": command_receipts,
        "file_receipts": file_receipts,
        "step_state": step_state,
        "drift": drift,
        "warnings": warnings,
    }


class _Raw(str):
    """Cell content that is already safe HTML (built by this module only)."""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _cell(value: Any) -> str:
    return value if isinstance(value, _Raw) else _esc(value)


def _table(headers: List[str], rows: List[List[Any]], row_classes: Optional[List[str]] = None) -> str:
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    body_rows = []
    for index, row in enumerate(rows):
        cells = "".join(f"<td>{_cell(value)}</td>" for value in row)
        cls = row_classes[index] if row_classes else ""
        attr = f' class="{_esc(cls)}"' if cls else ""
        body_rows.append(f"<tr{attr}>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _items(values: List[Any], empty: str) -> str:
    if not values:
        return f"<p>{_esc(empty)}</p>"
    entries = "".join(f"<li>{_cell(value)}</li>" for value in values)
    return f"<ul>{entries}</ul>"


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"


_STATUS_CLASSES = {"passed": "status-passed", "warning": "status-warning", "failed": "status-failed"}

_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
       margin: 2rem auto; max-width: 60rem; padding: 0 1rem; color: #1a1a1a; }
h1 { border-bottom: 2px solid #ccc; padding-bottom: .3rem; }
.banner { background: #fff8e1; border: 1px solid #e0c341; border-radius: 4px; padding: .6rem .8rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .9rem; }
th, td { border: 1px solid #ccc; padding: .3rem .5rem; text-align: left; vertical-align: top; }
th { background: #f2f2f2; }
code, .hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em;
              word-break: break-all; }
.status-passed { background: #eaf7ea; }
.status-warning { background: #fff8e1; }
.status-failed { background: #fdecea; }
section { margin-bottom: 1.2rem; }
"""


def _step_status_body(plan: Optional[Dict[str, Any]], step_state: Optional[Dict[str, Any]]) -> str:
    if not plan:
        return "<p>Plan not recorded.</p>"
    steps = _steps(plan)
    if not steps:
        return "<p>No plan steps recorded.</p>"
    states = step_state.get("steps", {}) if step_state else {}
    attempts = step_state.get("attempts", {}) if step_state else {}
    rows: List[List[Any]] = []
    for step in steps:
        step_id = step.get("id", "unknown")
        if step_state is None:
            status = "execution ledger not recorded"
            attempt_count: Any = ""
        else:
            status = "completed" if states.get(step_id, {}).get("completed") else "not completed"
            attempt_count = sum(
                1 for attempt in attempts.values() if attempt.get("step_id") == step_id
            )
        rows.append([step_id, step.get("action", ""), status, attempt_count])
    return _table(["Step", "Action", "Status", "Attempts"], rows)


def _output_link(receipt: Dict[str, Any], stream: str) -> Any:
    href = receipt.get(f"{stream}_href")
    if href:
        return _Raw(f'<a href="{_esc(href)}">{_esc(stream)}</a>')
    path = receipt.get(f"{stream}_path")
    return path or ""


def _command_receipts_body(receipts: List[Dict[str, Any]]) -> str:
    if not receipts:
        return "<p>No command receipts recorded.</p>"
    rows: List[List[Any]] = []
    for receipt in receipts:
        command = receipt.get("command")
        rendered = " ".join(str(part) for part in command) if isinstance(command, list) else str(command or "")
        risk_value = receipt.get("risk")
        risk = risk_value if isinstance(risk_value, dict) else {}
        outputs = [_output_link(receipt, stream) for stream in ("stdout", "stderr")]
        links = _Raw(" ".join(_cell(value) for value in outputs if value != ""))
        rows.append(
            [
                receipt.get("id", ""),
                receipt.get("step_id", ""),
                receipt.get("provenance", ""),
                receipt.get("decision", ""),
                "" if receipt.get("exit_code") is None else receipt.get("exit_code"),
                risk.get("level", ""),
                _Raw(f"<code>{_esc(rendered)}</code>"),
                links,
            ]
        )
    return _table(
        ["Id", "Step", "Provenance", "Decision", "Exit", "Risk", "Command", "Output"], rows
    )


def _file_receipts_body(receipts: List[Dict[str, Any]]) -> str:
    if not receipts:
        return "<p>No file receipts recorded.</p>"
    rows = [
        [receipt.get("id", ""), receipt.get("step_id", ""), receipt.get("path", ""), receipt.get("change_kind", "")]
        for receipt in receipts
    ]
    return _table(["Id", "Step", "Path", "Change"], rows)


def _drift_body(drift: Optional[Dict[str, Any]]) -> str:
    if drift is None:
        return "<p>Drift report not recorded.</p>"
    parts = [f"<p>Status: <strong>{_esc(drift.get('status', 'missing'))}</strong></p>"]
    notes = [note for note in _list(drift.get("notes"))]
    if notes:
        parts.append(_items(notes, ""))
    unmapped = [
        entry.get("path", "")
        for entry in _list(drift.get("unmapped_hunks"))
        if isinstance(entry, dict)
    ]
    if unmapped:
        parts.append("<p>Unmapped hunks:</p>")
        parts.append(_items(unmapped, ""))
    return "".join(parts)


def _checks_body(proof: Dict[str, Any]) -> str:
    checks = [check for check in _list(proof.get("checks")) if isinstance(check, dict)]
    if not checks:
        return "<p>No checks recorded.</p>"
    rows: List[List[Any]] = []
    classes: List[str] = []
    for check in checks:
        status = str(check.get("status", ""))
        message = check.get("message", "")
        if not message and "count" in check:
            message = f"count: {check.get('count')}"
        rows.append([check.get("id", "unknown"), status, message])
        classes.append(_STATUS_CLASSES.get(status, ""))
    return _table(["Check", "Status", "Detail"], rows, classes)


def _residual_warnings_body(proof: Dict[str, Any], collect_warnings: List[str]) -> str:
    warnings = [
        f"{check.get('id', 'unknown')}: {check.get('message', check.get('count', ''))}"
        for check in _list(proof.get("checks"))
        if isinstance(check, dict) and check.get("status") == "warning"
    ]
    warnings.extend(collect_warnings)
    return _items(warnings, "No residual warnings.")


def _hashes_body(proof: Dict[str, Any]) -> str:
    rows = [
        [item.get("path", ""), _Raw(f'<span class="hash">{_esc(item.get("sha256", ""))}</span>')]
        for item in _list(proof.get("files"))
        if isinstance(item, dict)
    ]
    table = _table(["Path", "SHA-256"], rows) if rows else "<p>No file hashes recorded.</p>"
    core = proof.get("core_sha256", "")
    return table + f'<p>Core checksum: <span class="hash">{_esc(core)}</span></p>'


def render_html(model: Dict[str, Any]) -> str:
    proof: Dict[str, Any] = model.get("proof") or {}
    plan: Optional[Dict[str, Any]] = model.get("plan")
    meta = proof.get("meta", {}) if isinstance(proof.get("meta"), dict) else {}

    objective = (plan or {}).get("objective") or (
        "Plan not recorded." if plan is None else "No objective recorded."
    )
    gates: List[Any] = list(_list((plan or {}).get("validation_gates")))
    for step in _steps(plan or {}):
        gates.extend(_list(step.get("validation")))

    sections = [
        _section("Objective", f"<p>{_esc(objective)}</p>"),
        _section("Scope", _items(_list((plan or {}).get("scope")), "No scope recorded.")),
        _section("Step Status", _step_status_body(plan, model.get("step_state"))),
        _section("Validation Gates", _items(gates, "No validation gates recorded.")),
        _section("Command Receipts", _command_receipts_body(model.get("command_receipts", []))),
        _section("File Receipts", _file_receipts_body(model.get("file_receipts", []))),
        _section("Drift Audit", _drift_body(model.get("drift"))),
        _section("Checks", _checks_body(proof)),
        _section("Residual Warnings", _residual_warnings_body(proof, model.get("warnings", []))),
        _section("Proof Hashes", _hashes_body(proof)),
    ]
    meta_line = (
        f"Generated {_esc(meta.get('created_at', 'unknown'))} by Agentflow "
        f"{_esc(meta.get('tool_version', 'unknown'))} "
        f"(proof schema {_esc(proof.get('schema_version', 'unknown'))})"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Agentflow Proof Report</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<h1>Agentflow Proof Report</h1>"
        '<p class="banner">Review aid. <code>agentflow verify-proof</code> is the '
        "authoritative check.</p>"
        f"<p>{meta_line}</p>"
        f"{''.join(sections)}"
        "</body></html>\n"
    )
