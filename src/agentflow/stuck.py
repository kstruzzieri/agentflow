"""Read-only stuck-loop detection over Agentflow's event projection.

Receipt-pattern detection only: it reasons about repeated command failures,
repeated step-verification failures, and alternating no-op command cycles from
the projected event stream. It never reads model conversation, and it never
writes to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .events import project_events

DEFAULT_MIN_COMMAND_FAILURES = 3
DEFAULT_MIN_VERIFY_FAILURES = 2
DEFAULT_MIN_CYCLE_REPEATS = 3

RULES_EVALUATED = 3


@dataclass(frozen=True)
class Thresholds:
    min_command_failures: int = DEFAULT_MIN_COMMAND_FAILURES
    min_verify_failures: int = DEFAULT_MIN_VERIFY_FAILURES
    min_cycle_repeats: int = DEFAULT_MIN_CYCLE_REPEATS


def _positioned(root: Path) -> List[Dict[str, Any]]:
    """Projected events, each tagged with a monotonic detector ``position``."""
    events = project_events(root)
    for position, event in enumerate(events):
        event["position"] = position
    return events


def _slices(
    events: List[Dict[str, Any]]
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    slices: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for event in events:
        step_id = event.get("step_id")
        attempt_id = event.get("attempt_id")
        if step_id is None or attempt_id is None:
            continue
        slices.setdefault((step_id, attempt_id), []).append(event)
    return slices


def _is_command(event: Dict[str, Any]) -> bool:
    return event.get("type") == "command.recorded"


def _is_file_change(event: Dict[str, Any]) -> bool:
    return event.get("type") == "file.changed"


def _is_step_verify(event: Dict[str, Any]) -> bool:
    return (
        event.get("type") == "verification.run"
        and (event.get("data") or {}).get("scope") == "step"
    )


def _command_failed(data: Dict[str, Any]) -> bool:
    if data.get("timed_out") is True:
        return True
    if data.get("decision") in ("blocked", "timeout"):
        return True
    return data.get("exit_code") != 0


def _progress_in_span(
    slice_events: List[Dict[str, Any]], first_pos: int, last_pos: int
) -> Dict[str, int]:
    file_changes = 0
    passing_verifies = 0
    successful_commands = 0
    for event in slice_events:
        position = event["position"]
        if position <= first_pos or position >= last_pos:
            continue
        if _is_file_change(event):
            file_changes += 1
        elif _is_step_verify(event) and (event.get("data") or {}).get("status") == "passed":
            passing_verifies += 1
        elif _is_command(event) and not _command_failed(event["data"]):
            successful_commands += 1
    return {
        "file_changes": file_changes,
        "passing_verifies": passing_verifies,
        "successful_commands": successful_commands,
    }


def detect_stuck(
    root: Path,
    plan: Optional[Dict[str, Any]] = None,  # reserved for future plan-aware rules
    thresholds: Thresholds = Thresholds(),
) -> Dict[str, Any]:
    events = _positioned(root)
    findings: List[Dict[str, Any]] = []
    for (step_id, attempt_id), slice_events in _slices(events).items():
        findings.extend(
            _repeated_command_failure(step_id, attempt_id, slice_events, thresholds)
        )
        findings.extend(
            _repeated_verify_failure(step_id, attempt_id, slice_events, thresholds)
        )
        findings.extend(
            _alternating_no_op(step_id, attempt_id, slice_events, thresholds)
        )
    # Each rule's finding dict MUST carry "first_position" (and "last_position").
    findings.sort(key=lambda finding: finding["first_position"])
    return {
        "status": "stuck" if findings else "ok",
        "findings": findings,
        "summary": {"rules_evaluated": RULES_EVALUATED, "finding_count": len(findings)},
    }


def stuck_block(
    root: Path, plan: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Advisory proof block: rules evaluated plus findings (no status field)."""
    report = detect_stuck(root, plan)
    return {"rules_evaluated": RULES_EVALUATED, "findings": report["findings"]}


def _identity(data: Dict[str, Any]) -> Tuple[Any, ...]:
    return (tuple(data.get("command") or []), data.get("gate"), data.get("cwd"))


def _fingerprint(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "decision": data.get("decision"),
        "exit_code": data.get("exit_code"),
        "stdout_sha256": data.get("stdout_sha256"),
        "stderr_sha256": data.get("stderr_sha256"),
    }


def _command_finding(step_id, attempt_id, streak, slice_events, thresholds):
    first, last = streak[0], streak[-1]
    fingerprints = [_fingerprint(event["data"]) for event in streak]
    base = (fingerprints[0]["decision"], fingerprints[0]["exit_code"])
    stable = all(
        (fingerprint["decision"], fingerprint["exit_code"]) == base
        for fingerprint in fingerprints
    )
    command = " ".join(streak[0]["data"].get("command") or [])
    count = len(streak)
    if stable:
        message = (
            f"Command `{command}` failed {count}x in "
            f"{step_id}/{attempt_id} with no change in outcome."
        )
    else:
        message = (
            f"Command `{command}` failed {count}x consecutively in "
            f"{step_id}/{attempt_id}."
        )
    return {
        "rule": "repeated_command_failure",
        "severity": "warning",
        "step_id": step_id,
        "attempt_id": attempt_id,
        "threshold": thresholds.min_command_failures,
        "message": message,
        "first_position": first["position"],
        "last_position": last["position"],
        "first_event": first["source"],
        "last_event": last["source"],
        "evidence": {
            "command": list(streak[0]["data"].get("command") or []),
            "gate": streak[0]["data"].get("gate"),
            "cwd": streak[0]["data"].get("cwd"),
            "failure_count": count,
            "fingerprints": fingerprints,
            "receipt_ids": [event["source"]["record_id"] for event in streak],
            "progress_in_span": _progress_in_span(
                slice_events, first["position"], last["position"]
            ),
        },
        "suggested_action": (
            "Change inputs or approach before re-running; identical retries are "
            "not progressing."
        ),
    }


def _repeated_command_failure(step_id, attempt_id, slice_events, thresholds):
    findings: List[Dict[str, Any]] = []
    streaks: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}

    def flush(key: Tuple[Any, ...]) -> None:
        streak = streaks.get(key) or []
        if len(streak) >= thresholds.min_command_failures:
            findings.append(
                _command_finding(step_id, attempt_id, streak, slice_events, thresholds)
            )
        streaks[key] = []

    for event in slice_events:
        if not _is_command(event):
            continue
        key = _identity(event["data"])
        if _command_failed(event["data"]):
            streaks.setdefault(key, []).append(event)
        else:
            flush(key)
    for key in list(streaks.keys()):
        flush(key)
    return findings


def _verify_finding(step_id, attempt_id, streak, slice_events, thresholds):
    first, last = streak[0], streak[-1]
    count = len(streak)
    return {
        "rule": "repeated_verify_failure",
        "severity": "warning",
        "step_id": step_id,
        "attempt_id": attempt_id,
        "threshold": thresholds.min_verify_failures,
        "message": (
            f"Step verification failed {count}x in {step_id}/{attempt_id} "
            f"with no new file changes."
        ),
        "first_position": first["position"],
        "last_position": last["position"],
        "first_event": first["source"],
        "last_event": last["source"],
        "evidence": {
            "verify_count": count,
            "verification_ids": [event["source"]["record_id"] for event in streak],
            "progress_in_span": _progress_in_span(
                slice_events, first["position"], last["position"]
            ),
        },
        "suggested_action": (
            "Verification keeps failing with no file changes; change the code "
            "before re-verifying."
        ),
    }


def _repeated_verify_failure(step_id, attempt_id, slice_events, thresholds):
    findings: List[Dict[str, Any]] = []
    streak: List[Dict[str, Any]] = []

    def flush() -> None:
        if len(streak) >= thresholds.min_verify_failures:
            findings.append(
                _verify_finding(step_id, attempt_id, streak, slice_events, thresholds)
            )
        streak.clear()

    for event in slice_events:
        if _is_step_verify(event):
            if (event.get("data") or {}).get("status") == "failed":
                streak.append(event)
            else:
                flush()
        elif _is_file_change(event):
            flush()
    flush()
    return findings


def _no_progress_in_window(slice_events, first_pos, last_pos) -> bool:
    for event in slice_events:
        position = event["position"]
        if position < first_pos or position > last_pos:
            continue
        if _is_file_change(event):
            return False
        if _is_step_verify(event) and (event.get("data") or {}).get("status") == "passed":
            return False
    return True


def _cycle_finding(step_id, attempt_id, window, slice_events, thresholds, period, repeats):
    first, last = window[0], window[-1]
    cycle = [" ".join(window[i]["data"].get("command") or []) for i in range(period)]
    return {
        "rule": "alternating_no_op",
        "severity": "warning",
        "step_id": step_id,
        "attempt_id": attempt_id,
        "threshold": thresholds.min_cycle_repeats,
        "message": (
            f"Commands {' -> '.join(cycle)} cycled {repeats}x in "
            f"{step_id}/{attempt_id} without changing files or passing verification."
        ),
        "first_position": first["position"],
        "last_position": last["position"],
        "first_event": first["source"],
        "last_event": last["source"],
        "evidence": {
            "cycle": [list(window[i]["data"].get("command") or []) for i in range(period)],
            "period": period,
            "repeats": repeats,
            "receipt_ids": [event["source"]["record_id"] for event in window],
            "progress_in_span": _progress_in_span(
                slice_events, first["position"], last["position"]
            ),
        },
        "suggested_action": (
            "Commands are oscillating without progress; step back and change "
            "approach instead of repeating the cycle."
        ),
    }


def _alternating_no_op(step_id, attempt_id, slice_events, thresholds):
    commands = [event for event in slice_events if _is_command(event)]
    findings: List[Dict[str, Any]] = []
    total = len(commands)
    for period in (2, 3):
        span = period * thresholds.min_cycle_repeats
        index = 0
        while index + span <= total:
            window = commands[index : index + span]
            base = [tuple(window[i]["data"].get("command") or []) for i in range(period)]
            is_cycle = all(
                tuple(window[offset]["data"].get("command") or []) == base[offset % period]
                for offset in range(span)
            )
            if is_cycle and len(set(base)) >= 2 and _no_progress_in_window(
                slice_events, window[0]["position"], window[-1]["position"]
            ):
                findings.append(
                    _cycle_finding(
                        step_id, attempt_id, window, slice_events,
                        thresholds, period, thresholds.min_cycle_repeats,
                    )
                )
                index += span
            else:
                index += 1
    return findings
