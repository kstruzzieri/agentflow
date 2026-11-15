"""#138: bind the locked plan to the run that started with it.

``init-execution`` pins the plan's binding hash on the run-start row. Every
``amend-plan`` recorded during that run names the plan it replaces and the
plan it accepts, so the plan on disk must be the last link of that chain.

The check holds at the moments it runs. A plan edited and put back between two
checks is not seen; step-run rows carry no plan hash.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Tuple

from .artifacts import UnverifiedSourceError, plan_binding_sha256, read_jsonl
from .contracts import BASE_ARTIFACT_PATHS
from .git import read_run_anchor

AMENDMENTS_PATH = BASE_ARTIFACT_PATHS["amendments"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PlanBindingError(ValueError):
    """The run's amendment history cannot be trusted; the check fails closed."""


def accepted_plan(root: Path) -> Optional[Tuple[str, str]]:
    """``(run_id, plan hash)`` the run currently accepts, or None when unbound.

    Unbound: a run older versions started (no run-start row, or one without a
    plan hash) or one whose plan was not locked when it started. Only this
    run's amendments count, in file order, and each must replace the plan the
    one before it accepted. Raises ValueError rather than guess.
    """
    anchor = read_run_anchor(root)
    if anchor is None or anchor.get("plan_sha256") is None:
        return None
    run_id, accepted = anchor["run_id"], anchor["plan_sha256"]
    for row in read_jsonl(root / AMENDMENTS_PATH):
        if row.get("run_id") != run_id:
            continue
        label = f"amendment {row.get('id')}"
        previous, replacement = row.get("previous_plan_sha256"), row.get("plan_sha256")
        if not (
            isinstance(previous, str) and isinstance(replacement, str)
            and _SHA256.match(previous) and _SHA256.match(replacement)
        ):
            raise PlanBindingError(
                f"{label} in {AMENDMENTS_PATH} has a malformed plan hash; "
                "the plan binding cannot trust it"
            )
        if previous != accepted:
            raise PlanBindingError(
                f"{label} in {AMENDMENTS_PATH} replaces plan {previous[:12]}, but the "
                f"accepted plan is {accepted[:12]}; the ledger was edited or reordered"
            )
        accepted = replacement
    return run_id, accepted


def plan_binding_error(root: Path, plan: Any) -> Optional[str]:
    """One line saying why ``plan`` is not the plan this run accepts, or None."""
    try:
        bound = accepted_plan(root)
    except UnverifiedSourceError:
        raise  # verify-proof reports the source that failed its hash
    except (OSError, ValueError) as exc:
        return str(exc)
    if bound is None:
        return None
    expected = bound[1]
    found = plan_binding_sha256(plan) if isinstance(plan, dict) else None
    if found == expected:
        return None
    return (
        f"plan does not match the accepted plan (expected {expected[:12]}, "
        f"found {found[:12] if found else 'no plan'}) and no amendment records the change; "
        "lock the plan, then record it with amend-plan"
    )

