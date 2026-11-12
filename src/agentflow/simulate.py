"""#113: replay recorded commands against a proposed risk policy, read-only.

Re-classifies every command receipt under a proposed plan and evaluates it
under a proposed ``risk_policy`` with no confirmation, so a team can see what a
stricter setting would have stopped before adopting it. Writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import read_jsonl
from .contracts import EXECUTION_ARTIFACT_PATHS, RISK_POLICIES
from .execution import load_execution_contract
from .risk import classify_command, evaluate_policy
from .validation import validate_plan

# Only these provenances were started by Agentflow (`run`, `replay-gates`), so
# only these could a risk policy have stopped. The rest are advisory.
_RUN_BY_AGENTFLOW = frozenset({"observed", "reconstructed"})


def _categories(risk: Dict[str, Any]) -> List[str]:
    findings = risk.get("findings")
    return sorted(
        {
            finding["category"]
            for finding in (findings if isinstance(findings, list) else [])
            if isinstance(finding, dict) and isinstance(finding.get("category"), str)
        }
    )


def current_risk_policy(root: Path) -> str:
    """The contract's effective risk_policy; a missing contract or key is ``warn``."""
    contract = load_execution_contract(root)
    if contract is None:
        return "warn"
    policy = contract.get("command_policy") if isinstance(contract, dict) else None
    if not isinstance(policy, dict):
        raise ValueError("execution contract command_policy must be an object")
    value = policy.get("risk_policy", "warn")
    if value not in RISK_POLICIES:
        raise ValueError(f"unknown risk_policy {value!r}")
    return value


def simulate_risk(
    root: Path,
    plan: Dict[str, Any],
    proposed_plan: Optional[Dict[str, Any]] = None,
    risk_policy: Optional[str] = None,
    plan_source: str = ".agent/plan.lock.json",
) -> Dict[str, Any]:
    """Report the recorded commands a proposed policy and plan would stop.

    ``plan`` is the current locked plan, used only to detect analyzer drift;
    ``proposed_plan`` (default: ``plan``) scopes the re-classification, and
    ``risk_policy`` (default: the contract's) decides it.
    """
    current = current_risk_policy(root)
    proposed = current if risk_policy is None else risk_policy
    if proposed not in RISK_POLICIES:
        raise ValueError(f"unknown risk_policy {proposed!r}")
    scope = plan if proposed_plan is None else proposed_plan
    for label, candidate in (("current", plan), ("proposed", scope)):
        errors = validate_plan(candidate)
        if errors:
            raise ValueError(f"invalid {label} plan: {'; '.join(errors)}")
    outcome = "blocked" if proposed == "block" else "needs_confirmation"
    # A report, not a gate: a non-object row is skipped and counted below.
    receipts = read_jsonl(
        root / EXECUTION_ARTIFACT_PATHS["command-receipts"], objects_only=False
    )
    stopped: List[Dict[str, Any]] = []
    drift: List[Dict[str, Any]] = []
    by_category: Dict[str, List[Any]] = {}
    skipped = 0
    for receipt in receipts:
        if not isinstance(receipt, dict):
            skipped += 1
            continue
        command = receipt.get("command")
        step_id = receipt.get("step_id")
        provenance = receipt.get("provenance")
        recorded = receipt.get("risk")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
            or not isinstance(step_id, str)
            or any(
                value is not None and not isinstance(value, str)
                for value in (
                    receipt.get("id"), provenance,
                    recorded.get("level") if isinstance(recorded, dict) else None,
                )
            )
        ):
            skipped += 1
            continue
        if isinstance(recorded, dict):
            # Against the current plan, so a proposed plan change is not drift.
            now = classify_command(command, plan, step_id, root=root)
            if now != recorded:
                drift.append(
                    {
                        "receipt_id": receipt.get("id"),
                        "step_id": step_id,
                        "recorded_level": recorded.get("level"),
                        "current_level": now["level"],
                        "recorded_categories": _categories(recorded),
                        "current_categories": _categories(now),
                    }
                )
        if receipt.get("decision") == "blocked":
            continue  # never ran, so nothing new to stop
        if proposed == "require-confirmation" and receipt.get("confirmed") is True:
            continue  # already confirmed, which this policy accepts
        risk = classify_command(command, scope, step_id, root=root)
        if evaluate_policy(risk["level"], proposed, confirmed=False) != "block":
            continue
        categories = _categories(risk)
        stopped.append(
            {
                "receipt_id": receipt.get("id"),
                "step_id": step_id,
                "command": command,
                "provenance": provenance,
                "advisory": provenance not in _RUN_BY_AGENTFLOW,
                "level": risk["level"],
                "categories": categories,
                "outcome": outcome,
            }
        )
        for category in categories:
            by_category.setdefault(category, []).append(receipt.get("id"))
    return {
        "current_risk_policy": current,
        "proposed_risk_policy": proposed,
        "plan": plan_source,
        "receipts": len(receipts),
        "skipped": skipped,
        "stopped": stopped,
        "by_category": dict(sorted(by_category.items())),
        "analyzer_drift": drift,
    }
