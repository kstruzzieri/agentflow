"""Provider-neutral handoff rendering."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Union

from .contracts import PROVIDER_NEUTRAL_DENYLIST


def _step(plan: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    for item in plan.get("steps", []):
        if isinstance(item, dict) and item.get("id") == step_id:
            return item
    raise ValueError(f"unknown step id: {step_id}")


def lint_handoff_text(text: str) -> List[Dict[str, str]]:
    lower = text.lower()
    findings: List[Dict[str, str]] = []
    for token in PROVIDER_NEUTRAL_DENYLIST:
        if token in lower:
            findings.append(
                {
                    "severity": "error",
                    "message": f"provider-specific token is not allowed in handoff: {token}",
                }
            )
    return findings


def _json_handoff(plan: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    step = _step(plan, step_id)
    return {
        "contract": "agentflow_shell_handoff",
        "objective": plan.get("objective", ""),
        "step": step,
        "allowed_files": step.get("files", []),
        "blocked_files": plan.get("blocked_files", []),
        "non_goals": plan.get("non_goals", []),
        "invariants": plan.get("invariants", []),
        "expected_diff": step.get("expected_diff", []),
        "validation": step.get("validation", []),
        "commands": {
            "claim": f"agentflow claim-step {step_id} --agent $AGENT_ID",
            "run": f"agentflow run --step {step_id} -- <command>",
            "record_file_change": f"agentflow record-file-change --step {step_id} --path <path>",
            "verify": f"agentflow verify-step {step_id}",
            "complete": f"agentflow complete-step {step_id}",
            "block": f"agentflow block-step {step_id} --reason <reason>",
            "fail": f"agentflow fail-step {step_id} --reason <reason>",
        },
        "exit_code_contract": {
            "0": "no error-severity findings",
            "1": "one or more error-severity findings",
            "2": "command usage error",
        },
    }


def _markdown_handoff(payload: Dict[str, Any]) -> str:
    step = payload["step"]
    lines = [
        "# Agentflow Work Packet",
        "",
        "## Objective",
        "",
        payload["objective"],
        "",
        "## Step",
        "",
        f"{step['id']}: {step['action']}",
        "",
        "## Allowed Files",
        "",
        *[f"- {item}" for item in payload["allowed_files"]],
        "",
        "## Blocked Files",
        "",
        *[f"- {item}" for item in payload["blocked_files"]],
        "",
        "## Expected Diff",
        "",
        *[f"- {item}" for item in payload["expected_diff"]],
        "",
        "## Validation",
        "",
        *[f"- {item}" for item in payload["validation"]],
        "",
        "## Commands",
        "",
        *[f"- `{name}`: `{command}`" for name, command in payload["commands"].items()],
        "",
    ]
    return "\n".join(lines)


def export_handoff(plan: Dict[str, Any], step_id: str, output_format: str) -> Union[Dict[str, Any], str]:
    payload = _json_handoff(plan, step_id)
    if output_format == "json":
        text = json.dumps(payload, sort_keys=True)
        findings = lint_handoff_text(text)
        if findings:
            raise ValueError(findings[0]["message"])
        return payload
    if output_format == "markdown":
        markdown = _markdown_handoff(payload)
        findings = lint_handoff_text(markdown)
        if findings:
            raise ValueError(findings[0]["message"])
        return markdown
    raise ValueError("format must be json or markdown")
