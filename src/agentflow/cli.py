"""Command line interface for Agentflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .aggregate import analyze, parse_sources, write_canonical
from .artifacts import (
    SCHEMA_VERSION,
    append_jsonl,
    create_initial_artifacts,
    read_json,
    read_jsonl,
    utc_now,
    write_json,
)
from .contracts import (
    AMENDMENTS_SCHEMA_VERSION,
    CONTEXT_RECEIPTS_SCHEMA_VERSION,
    EVIDENCE_KINDS,
    EVIDENCE_SCHEMA_VERSION,
    FAILURES_SCHEMA_VERSION,
    WORKFLOW_REVIEW_DEPTHS,
    strict_mode,
)
from .execution import (
    AMENDMENT_REASON_CODES,
    amend_step,
    block_step,
    claim_step,
    complete_step,
    doctor,
    fail_step,
    init_execution_artifacts,
    mark_step_verified,
    next_step,
    reclaim_step,
    renew_lease,
    require_lifecycle_owner,
)
from .execution_coverage import verify_run, verify_step
from .events import filter_events_since, project_events, valid_since
from .capabilities import append_capability_receipt, build_capability_receipt
from .handoff import export_handoff, lint_handoff_text
from . import porcelain
from .packs import (
    PackError,
    find_profile,
    inspect_summary,
    load_pack,
    profile_to_contract,
    render_inspect_summary,
    template_to_plan,
)
from .draft_plan import (
    DRAFT_PLAN_SCHEMA_VERSION,
    DraftPlanError,
    compile_draft_plan,
    selection_reason,
)
from .proof import build_proof, render_markdown, verify_proof, write_proof_metadata
from .viewer import collect_view_model, render_html
from .stuck import Thresholds, detect_stuck
from .recommend import (
    RecommendError,
    recommend as recommend_workflow,
    render_text as render_recommendation,
    validate_brief,
)
from .receipts import record_command, record_file_change, replay_gates, run_command
from .review import build_review_run_record, parse_finding_ref, review_evidence_entries
from .review_runner import MANIFEST_FILENAME, exit_code_for, produce_manifest
from .runtime import build_runtime_status
from .validation import (
    audit_drift,
    validate_plan,
    validate_requirement_traceability,
)
from .workflow_contract import (
    WORKFLOW_CONTRACT_PATH,
    validate_workflow_contract,
    write_workflow_contract,
)


def print_lines(lines: List[str]) -> None:
    for line in lines:
        print(line)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    pack_path = getattr(args, "pack", None)
    profile_id = getattr(args, "profile", None)
    if profile_id and not pack_path:
        print("error: --profile requires --pack", file=sys.stderr)
        return 2
    if pack_path:
        return _command_init_with_pack(root, pack_path, profile_id, args)
    created, skipped = create_initial_artifacts(root, force=args.force)
    for path in created:
        print(f"created {path}")
    for path in skipped:
        print(f"skipped {path}")
    return 0


def _command_init_with_pack(
    root: Path, pack_path: str, profile_id: Optional[str], args: argparse.Namespace
) -> int:
    if not profile_id:
        print("error: --pack requires --profile", file=sys.stderr)
        return 2
    try:
        pack = load_pack(Path(pack_path))
        profile = profile_to_contract(
            pack.manifest,
            profile_id,
            "init --pack",
            args.reason
            or f"Selected pack {pack.manifest['id']} profile {profile_id} via init --pack.",
        )
        template_id = find_profile(pack.manifest, profile_id)["plan_template"]
        plan = template_to_plan(pack.manifest, template_id)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    plan_path = root / ".agent" / "plan.lock.json"
    contract_path = root / WORKFLOW_CONTRACT_PATH
    if (plan_path.exists() or contract_path.exists()) and not args.force:
        print(
            "error: refusing to overwrite existing .agent/plan.lock.json or "
            ".agent/workflow.contract.json (use --force)",
            file=sys.stderr,
        )
        return 1

    # Scaffold base artifacts without force: never wipe pre-existing ledgers
    # when --force was passed only to override the plan/contract conflict above.
    # The plan and contract below are written explicitly regardless.
    created, _skipped = create_initial_artifacts(root, force=False)
    for path in created:
        print(f"created {path}")
    write_json(plan_path, plan)
    write_workflow_contract(root, profile)
    print(f"seeded .agent/plan.lock.json from template {template_id}")
    print(f"wrote {WORKFLOW_CONTRACT_PATH}")
    return 0


def command_pack_inspect(args: argparse.Namespace) -> int:
    try:
        pack = load_pack(Path(args.path))
    except PackError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "invalid",
                        "errors": [_plan_diagnostic("validation_error", str(exc))],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    summary = inspect_summary(pack)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_lines(render_inspect_summary(summary))
    return 0


def command_init_execution(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    created, skipped = init_execution_artifacts(root, force=args.force)
    for path in created:
        print(f"created {path}")
    for path in skipped:
        print(f"skipped {path}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = doctor(root)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"doctor {result['status']}")
        for finding in result["findings"]:
            print(f"{finding['severity']}: {finding['message']}")
    return 1 if result["status"] == "failed" else 0


def command_intake(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    source = Path(args.from_file)
    text = source.read_text(encoding="utf-8")
    objective = next((line.strip() for line in text.splitlines() if line.strip()), "")
    intake = {
        # Intake is a transient draft artifact and intentionally tracks the plan schema.
        "schema_version": SCHEMA_VERSION,
        "objective": objective,
        "non_goals": [],
        "success_criteria": [],
        "risk_level": "low",
        "requires_user_approval": False,
        "initial_unknowns": [],
        "source": str(source),
        "created_at": utc_now(),
    }
    output = root / ".agent/intake.json"
    write_json(output, intake)
    print(f"created {output.relative_to(root)}")
    return 0


def _load_plan(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"plan file not found: {path}")
    return read_json(path)


def _plan_diagnostic(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _print_plan_result(
    status: str,
    *,
    path: Optional[Path] = None,
    errors: Optional[List[Dict[str, str]]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "status": status,
        "errors": errors or [],
    }
    if path is not None:
        payload["path"] = str(path)
    print(json.dumps(payload, indent=2, sort_keys=True))


def _json_decode_message(source: str, exc: json.JSONDecodeError) -> str:
    return f"{source}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"


def _coerce_plan_object(data: Any, source: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: top-level value must be a JSON object")
    return data


def _load_plan_from_path(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"plan file not found: {path}")
    try:
        return _coerce_plan_object(read_json(path), str(path))
    except json.JSONDecodeError as exc:
        raise ValueError(_json_decode_message(str(path), exc)) from exc


def _load_plan_from_stdin() -> Dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise ValueError(_json_decode_message("stdin", exc)) from exc
    return _coerce_plan_object(data, "stdin")


def _load_root_plan(root: Path) -> Dict[str, Any]:
    return read_json(root / ".agent/plan.lock.json")


def command_validate_plan(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    try:
        plan = _load_plan(plan_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"invalid plan: {exc}", file=sys.stderr)
        return 1

    errors = validate_plan(plan)
    if errors:
        print("plan invalid")
        for error in errors:
            print(f"- {error}")
        return 1

    print("plan valid")
    return 0


def command_lock_plan(args: argparse.Namespace) -> int:
    if args.stdin and args.from_json:
        message = "--stdin and --from-json are mutually exclusive"
        if args.json:
            _print_plan_result(
                "invalid",
                errors=[_plan_diagnostic("invalid_arguments", message)],
            )
        else:
            print(message, file=sys.stderr)
        return 2

    output_path = Path(args.plan).resolve()
    try:
        if args.stdin:
            plan = _load_plan_from_stdin()
        elif args.from_json:
            plan = _load_plan_from_path(Path(args.from_json).resolve())
        else:
            plan = _load_plan_from_path(output_path)
    except FileNotFoundError as exc:
        if args.json:
            _print_plan_result(
                "invalid",
                errors=[_plan_diagnostic("input_not_found", str(exc))],
            )
        else:
            print(f"invalid plan: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        code = (
            "invalid_json"
            if isinstance(exc.__cause__, json.JSONDecodeError)
            else "invalid_plan_json"
        )
        if args.json:
            _print_plan_result("invalid", errors=[_plan_diagnostic(code, str(exc))])
        else:
            print(f"invalid plan: {exc}", file=sys.stderr)
        return 1

    errors = validate_plan(plan)
    if errors:
        if args.json:
            _print_plan_result(
                "invalid",
                errors=[_plan_diagnostic("validation_error", error) for error in errors],
            )
        else:
            print("plan invalid")
            for error in errors:
                print(f"- {error}")
        return 1

    plan["locked"] = True
    plan["locked_at"] = utc_now()
    write_json(output_path, plan)
    if args.json:
        _print_plan_result("locked", path=output_path)
    else:
        print(f"locked {output_path}")
    return 0


def _load_json_object(path: Path, kind: str) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{kind} file not found: {path}")
    try:
        data = read_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(_json_decode_message(str(path), exc)) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level value must be a JSON object")
    return data


def _print_workflow_contract_errors(errors: List[str]) -> None:
    print("workflow contract invalid")
    for error in errors:
        print(f"- {error}")


def command_workflow_contract(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if args.from_json:
        if args.path != WORKFLOW_CONTRACT_PATH:
            print(
                f"PATH cannot be used with --from-json; writes go to {WORKFLOW_CONTRACT_PATH}",
                file=sys.stderr,
            )
            return 2
        source = Path(args.from_json)
        if not source.is_absolute():
            source = root / source
        try:
            contract = _load_json_object(source, "workflow contract")
        except (FileNotFoundError, ValueError) as exc:
            print(f"workflow contract invalid: {exc}", file=sys.stderr)
            return 1
        errors = validate_workflow_contract(contract)
        if errors:
            _print_workflow_contract_errors(errors)
            return 1
        path = write_workflow_contract(root, contract)
        print(f"wrote {path.relative_to(root)}")
        return 0

    target = Path(args.path or WORKFLOW_CONTRACT_PATH)
    if not target.is_absolute():
        target = root / target
    try:
        contract = _load_json_object(target, "workflow contract")
    except (FileNotFoundError, ValueError) as exc:
        print(f"workflow contract invalid: {exc}", file=sys.stderr)
        return 1
    errors = validate_workflow_contract(contract)
    if errors:
        _print_workflow_contract_errors(errors)
        return 1
    print("workflow contract valid")
    return 0


def command_amend_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    amendment = {
        "schema_version": AMENDMENTS_SCHEMA_VERSION,
        "id": args.id,
        "reason": args.reason,
        "new_evidence": args.evidence or [],
        "changed_scope": args.changed_scope or [],
        "changed_steps": args.changed_steps or [],
        "changed_risk": args.changed_risk,
        "requires_approval": args.requires_approval,
        "created_at": utc_now(),
    }
    append_jsonl(root / ".agent/amendments.jsonl", amendment)
    print(f"recorded amendment {args.id}")
    return 0


def command_record_evidence(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "id": args.id,
        "claim": args.claim,
        "source": args.source,
        "confidence": args.confidence,
        "last_verified": utc_now(),
    }
    if args.kind:
        evidence["kind"] = args.kind
    if args.supports:
        evidence["supports"] = args.supports
    append_jsonl(root / ".agent/evidence.jsonl", evidence)
    print(f"recorded evidence {args.id}")
    return 0


def command_record_review(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    try:
        record = build_review_run_record(root, manifest_path)
    except ValueError as exc:
        print(f"invalid review manifest: {exc}", file=sys.stderr)
        return 1
    append_jsonl(root / ".agent/review-runs.jsonl", record)
    if args.emit_evidence:
        for entry in review_evidence_entries(record):
            append_jsonl(root / ".agent/evidence.jsonl", entry)
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"recorded review run {record['review_run_id']}")
    return 0


def command_record_capability(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        row = build_capability_receipt(
            args.id,
            args.capability,
            "used",
            args.reason,
            provider=args.provider,
            evidence=args.evidence,
        )
    except ValueError as exc:
        print(f"invalid capability receipt: {exc}", file=sys.stderr)
        return 1
    append_capability_receipt(root, row)
    print(f"recorded capability {row['id']}")
    return 0


def command_waive_capability(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        row = build_capability_receipt(
            args.id,
            args.capability,
            "waived",
            args.reason,
            provider=None,
            evidence=args.evidence,
        )
    except ValueError as exc:
        print(f"invalid capability receipt: {exc}", file=sys.stderr)
        return 1
    append_capability_receipt(root, row)
    print(f"waived capability {row['id']}")
    return 0


def command_review_manifest(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        manifest = produce_manifest(
            root=root,
            state_dir=args.state_dir,
            branch=args.branch,
            findings_json=args.findings_json,
            config_path=config_path,
            depth_profile=args.depth_profile,
        )
    except ValueError as exc:
        print(f"review-manifest failed: {exc}", file=sys.stderr)
        return 1
    if args.write:
        out_dir = (root / args.state_dir).resolve()
        write_json(out_dir / MANIFEST_FILENAME, manifest)
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print(
            f"review-manifest {manifest['review_run_id']} "
            f"gate_status={manifest['gate_status']} "
            f"active_blocking={len(manifest['active_blocking'])}"
        )
    return exit_code_for(manifest, args.fail_on_block, args.strict_exit)


def command_record_context(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if args.bytes is not None and args.bytes < 0:
        print("context bytes must be non-negative", file=sys.stderr)
        return 1
    receipt = {
        "schema_version": CONTEXT_RECEIPTS_SCHEMA_VERSION,
        "id": args.id,
        "source": args.source,
        "reason": args.reason,
        "used_for": args.used_for or [],
        "created_at": utc_now(),
    }
    if args.bytes is not None:
        receipt["bytes"] = args.bytes
    append_jsonl(root / ".agent/context-receipts.jsonl", receipt)
    print(f"recorded context receipt {args.id}")
    return 0


def command_record_failure(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    failure = {
        "schema_version": FAILURES_SCHEMA_VERSION,
        "command": args.command,
        "failure_type": args.failure_type,
        "relevant_lines": args.relevant_lines or [],
        "suspected_cause": args.suspected_cause,
        "next_action": args.next_action,
        "created_at": utc_now(),
    }
    append_jsonl(root / ".agent/failures.jsonl", failure)
    print(f"recorded failure signature for {args.command}")
    return 0


def command_audit_drift(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = root / plan_path

    try:
        plan = _load_plan(plan_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"invalid plan: {exc}", file=sys.stderr)
        return 1

    report = audit_drift(root, plan)
    output = root / ".agent/drift-report.json"
    write_json(output, report)
    print(f"drift audit {report['status']}")
    if report["out_of_scope_files"]:
        print("out of scope:")
        print_lines([f"- {path}" for path in report["out_of_scope_files"]])
    if report["blocked_files_changed"]:
        print("blocked files changed:")
        print_lines([f"- {path}" for path in report["blocked_files_changed"]])
    if report.get("unmapped_hunks"):
        print("unmapped hunks:")
        hunk_lines = []
        for entry in report["unmapped_hunks"]:
            if isinstance(entry, dict):
                hunk_lines.append(
                    f"- {entry['path']} @@ -{entry['old_start']},{entry['old_count']} "
                    f"+{entry['new_start']},{entry['new_count']} @@ "
                    f"{entry['hash'][:12]} ({entry['reason']})"
                )
            else:
                hunk_lines.append(f"- {entry}")
        print_lines(hunk_lines)
    for note in report["notes"]:
        print(f"note: {note}")
    return 1 if report["status"] == "fail" else 0


def command_next_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    step = next_step(root, plan)
    if args.json:
        print(json.dumps(step, indent=2, sort_keys=True))
    elif step:
        print(step["id"])
    else:
        print("no eligible step")
    return 0


def command_claim_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    try:
        event = claim_step(root, plan, args.step, args.agent, args.lease_minutes)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(f"claimed {event['step_id']} as {event['attempt_id']}")
    return 0


def command_amend_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    finding_refs = []
    for raw in args.finding or []:
        try:
            finding_refs.append(parse_finding_ref(raw))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        event = amend_step(
            root,
            plan,
            args.step,
            args.agent,
            args.reason,
            args.reason_code,
            finding_refs=finding_refs or None,
            lease_minutes=args.lease_minutes,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(
            f"amended {event['step_id']} as {event['attempt_id']} "
            f"(amends {event['amends_attempt']})"
        )
    return 0


def command_complete_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        event = complete_step(root, args.step, args.attempt, agent_id=args.agent)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(f"completed {event['step_id']} {event['attempt_id']}")
    return 0


def command_block_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        event = block_step(root, args.step, args.attempt, args.reason, agent_id=args.agent)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(f"blocked {event['step_id']} {event['attempt_id']}")
    return 0


def command_fail_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        event = fail_step(root, args.step, args.attempt, args.reason, agent_id=args.agent)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(f"failed {event['step_id']} {event['attempt_id']}")
    return 0


def command_reclaim_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    try:
        event = reclaim_step(
            root, plan, args.step, args.agent, args.reason, args.lease_minutes
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(f"reclaimed {event['step_id']} as {event['attempt_id']}")
    return 0


def command_renew_lease(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        event = renew_lease(root, args.step, args.attempt, args.agent, args.minutes)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(event, indent=2, sort_keys=True))
    else:
        print(f"renewed {event['step_id']} {event['attempt_id']} "
              f"until {event['lease_expires_at']}")
    return 0


def _normalize_remainder(command: List[str]) -> List[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def command_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    command = _normalize_remainder(args.command)
    if not command:
        print("run requires a command after --", file=sys.stderr)
        return 2
    confirmed = bool(getattr(args, "confirm_risk", False)) or (
        os.environ.get("AGENTFLOW_CONFIRM_RISK") == "1"
    )
    source = "cli" if getattr(args, "confirm_risk", False) else ("env" if confirmed else None)
    try:
        receipt = run_command(
            root, plan, args.step, args.attempt, command,
            gate=args.gate, confirmed=confirmed, confirmation_source=source,
            agent_id=args.agent,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if receipt.get("decision") == "blocked":
        categories = sorted({f["category"] for f in receipt["risk"]["findings"]})
        risk_policy = receipt.get("risk_policy")
        message = (
            "blocked high-risk command ("
            + ", ".join(categories)
            + f"); risk_policy={risk_policy} refused execution."
        )
        if risk_policy == "require-confirmation":
            message += " Re-run with --confirm-risk to override."
        print(message, file=sys.stderr)
        if args.json:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        return 2
    if receipt.get("decision") == "timeout":
        timeout_seconds = receipt.get("timeout_seconds")
        print(f"command timed out after {timeout_seconds} seconds", file=sys.stderr)
        if args.json:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        return 124
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"recorded {receipt['id']} exit {receipt['exit_code']}")
    return int(receipt["exit_code"])


def command_record_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    command = _normalize_remainder(args.command)
    if not command:
        print("record-command requires a command after --", file=sys.stderr)
        return 2
    try:
        receipt = record_command(
            root,
            args.step,
            args.attempt,
            command,
            args.exit_code,
            gate=args.gate,
            plan=plan,
            agent_id=args.agent,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"recorded {receipt['id']} attested exit {receipt['exit_code']}")
    return 0


def command_record_file_change(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    receipts = []
    try:
        for path in args.path:
            receipts.append(
                record_file_change(root, plan, args.step, args.attempt, path, agent_id=args.agent)
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(receipts, indent=2, sort_keys=True))
    else:
        for receipt in receipts:
            print(f"recorded {receipt['id']} {receipt['path']}")
    return 0


def command_verify_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    try:
        resolved_attempt = require_lifecycle_owner(
            root, args.step, args.attempt, args.agent, action="verify"
        )
        result = verify_step(
            root,
            plan,
            args.step,
            resolved_attempt,
            strict_mode(args.strict),
            args.replay,
        )
        if result["status"] != "failed":
            mark_step_verified(
                root,
                args.step,
                resolved_attempt,
                [*result["errors"], *result["warnings"]],
                agent_id=args.agent,
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"verify-step {result['status']}")
        for finding in [*result["errors"], *result["warnings"]]:
            print(f"{finding['severity']}: {finding['message']}")
    return 1 if result["status"] == "failed" else 0


def command_verify_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    try:
        plan = _load_plan(plan_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"invalid plan: {exc}", file=sys.stderr)
        return 1
    result = verify_run(root, plan, strict_mode(args.strict), record=args.record)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"verify-run {result['status']}")
        for finding in [*result["errors"], *result["warnings"]]:
            print(f"{finding['severity']}: {finding['message']}")
    return 1 if result["status"] == "failed" else 0


def command_aggregate_ledgers(args: argparse.Namespace) -> int:
    inputs = args.input or []
    source_ids = args.source_id or []
    if not inputs:
        print("aggregate-ledgers: at least one --input with --source-id is required", file=sys.stderr)
        return 2
    sources, errors = parse_sources(inputs, source_ids, args.label)
    if errors:
        for err in errors:
            print(f"aggregate-ledgers: {err}", file=sys.stderr)
        return 2
    output_root = Path(args.output).resolve()
    if not output_root.is_dir():
        print(
            f"aggregate-ledgers: --output {output_root} is not a directory "
            "(it must already contain the merged source tree)",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        report = analyze(sources, output_root, base_ref=args.base)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(
                f"aggregate-ledgers dry-run: {report['status']} "
                f"({len(sources)} sources, {len(report['collisions'])} collisions)"
            )
            for col in report["collisions"]:
                detail = {k: v for k, v in col.items() if k != "kind"}
                print(f"collision: {col['kind']} {json.dumps(detail, sort_keys=True)}")
        return 1 if report["status"] == "collision" else 0

    result = write_canonical(sources, output_root, base_ref=args.base)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["status"] == "collision":
        print(
            f"aggregate-ledgers: {result['status']} "
            f"({len(sources)} sources, {len(result['collisions'])} collisions); nothing written"
        )
        for col in result["collisions"]:
            detail = {k: v for k, v in col.items() if k != "kind"}
            print(f"collision: {col['kind']} {json.dumps(detail, sort_keys=True)}")
    else:
        print(f"aggregate-ledgers: wrote canonical .agent/ into {output_root} ({len(sources)} sources)")
    return 1 if result["status"] == "collision" else 0


def command_detect_stuck(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    try:
        plan = _load_plan(plan_path)
    except (FileNotFoundError, json.JSONDecodeError):
        plan = None
    thresholds = Thresholds(
        min_command_failures=args.min_command_failures,
        min_verify_failures=args.min_verify_failures,
        min_cycle_repeats=args.min_cycle_repeats,
    )
    report = detect_stuck(root, plan, thresholds)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"detect-stuck {report['status']}")
        for finding in report["findings"]:
            location = f"{finding['step_id']}/{finding['attempt_id']}"
            print(f"warning [{location}]: {finding['message']}")
            print(f"  -> {finding['suggested_action']}")
    return 1 if (args.strict and report["findings"]) else 0


def command_export_handoff(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    try:
        rendered = export_handoff(plan, args.step, args.format)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
    else:
        suffix = "json" if args.format == "json" else "md"
        output = root / ".agent/handoffs" / f"{args.step}.{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        output.write_text(json.dumps(rendered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        output.write_text(str(rendered), encoding="utf-8")
    print(f"created {output.relative_to(root)}")
    return 0


def command_lint_handoff(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    path = Path(args.input)
    if not path.is_absolute():
        path = root / path
    findings = lint_handoff_text(path.read_text(encoding="utf-8"))
    result = {"findings": findings}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for finding in findings:
            print(f"{finding['severity']}: {finding['message']}")
        if not findings:
            print("handoff valid")
    return 1 if findings else 0


def command_replay_gates(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    result = replay_gates(root, plan, step_id=args.step, record=args.record)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"replay-gates {result['status']}")
        for finding in [*result["errors"], *result["warnings"]]:
            print(f"{finding['severity']}: {finding['message']}")
    return 1 if result["status"] == "failed" else 0


def command_runtime_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    if not config_path.exists():
        message = f"runtime config missing: {config_path.relative_to(root)}"
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "missing",
                        "findings": [{"severity": "warning", "message": message}],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(message)
        return 1 if strict_mode(args.strict) else 0

    snapshot = build_runtime_status(
        config_path,
        record_id=f"R{len(read_jsonl(root / '.agent/runtime-snapshots.jsonl')) + 1}",
        allow_probe=args.probe,
    )
    if args.record:
        append_jsonl(root / ".agent/runtime-snapshots.jsonl", snapshot)
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        for runtime in snapshot["runtimes"]:
            print(f"{runtime['id']} {runtime['status']}")
        for server in snapshot.get("mcp_servers", []):
            print(f"mcp {server['id']} {server['status']}")
        for finding in snapshot.get("findings", []):
            print(f"{finding['severity']}: {finding['message']}")
    has_error = any(item.get("severity") == "error" for item in snapshot.get("findings", []))
    has_warning = any(item.get("severity") == "warning" for item in snapshot.get("findings", []))
    return 1 if has_error or (strict_mode(args.strict) and has_warning) else 0


def _markdown_list(items: List[Any], empty: str = "None.") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def command_build_proof(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output

    try:
        plan = _load_plan(plan_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"invalid plan: {exc}", file=sys.stderr)
        return 1
    traceability_errors = validate_requirement_traceability(plan)
    if traceability_errors:
        print("invalid requirement traceability", file=sys.stderr)
        for error in traceability_errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    try:
        proof = build_proof(root, plan_path, strict_mode(args.strict))
    except ValueError as exc:
        print(f"invalid ledger: {exc}", file=sys.stderr)
        return 1
    write_proof_metadata(root, proof)
    drift_path = root / ".agent/drift-report.json"
    drift = read_json(drift_path) if drift_path.exists() else {"status": "missing", "notes": []}
    try:
        evidence = read_jsonl(root / ".agent/evidence.jsonl")
    except ValueError as exc:
        print(f"invalid ledger: {exc}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(plan, proof, evidence, drift), encoding="utf-8")
    print(f"created {output.relative_to(root)}")
    print("created .agent/proof-pack.json")

    has_failed = any(check.get("status") == "failed" for check in proof["checks"])
    has_warning = any(check.get("status") == "warning" for check in proof["checks"])
    return 1 if has_failed or (strict_mode(args.strict) and has_warning) else 0


def command_verify_proof(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    proof_path = Path(args.proof)
    if not proof_path.is_absolute():
        proof_path = root / proof_path
    if not proof_path.exists():
        print(f"proof metadata missing: {proof_path.relative_to(root)}")
        return 1
    findings = verify_proof(root, proof_path, replay=args.replay, strict=strict_mode(args.strict))
    for finding in findings:
        print(f"{finding['severity']}: {finding['message']}")
    if any(finding.get("severity") == "error" for finding in findings):
        return 1
    print("proof verified")
    return 0


def command_view_proof(args: argparse.Namespace) -> int:
    if not args.html:
        print("only --html output is supported; pass --html", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    proof_path = Path(args.proof)
    if not proof_path.is_absolute():
        proof_path = root / proof_path
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    try:
        model = collect_view_model(root, proof_path, output)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(model), encoding="utf-8")
    print(f"created {output.relative_to(root) if output.is_relative_to(root) else output}")
    return 0


def _event_detail(event: Dict[str, Any]) -> str:
    data = event["data"]
    event_type = event["type"]
    if event_type == "command.recorded":
        command = data.get("command")
        rendered = " ".join(command) if isinstance(command, list) else ""
        return f"exit={data.get('exit_code')} {rendered}".strip()
    if event_type == "file.changed":
        return f"{data.get('change_kind')} {data.get('path')}".strip()
    if event_type == "verification.run":
        return f"{data.get('scope')} {data.get('status')}".strip()
    return str(data.get("reason") or data.get("agent_id") or "")


def command_events(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    events = project_events(root)
    if args.since is not None:
        if not valid_since(args.since):
            print(f"invalid --since timestamp: {args.since}", file=sys.stderr)
            return 2
        events = filter_events_since(events, args.since)
    if args.jsonl:
        for event in events:
            print(json.dumps(event, sort_keys=True))
    elif args.json:
        print(json.dumps(events, indent=2, sort_keys=True))
    else:
        for event in events:
            step = event.get("step_id") or "-"
            attempt = event.get("attempt_id") or "-"
            line = f"{event['timestamp']}  {event['type']}  {step}/{attempt}  {_event_detail(event)}"
            print(line.rstrip())
    return 0


def command_next_action(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    action = porcelain.next_action(root, strict_mode(args.strict))
    if args.json:
        print(json.dumps(action.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"next-action {action.state}: {action.reason}")
        if action.command:
            print(action.command)
        for line in action.diagnostics:
            print(f"  {line}")
    return 0


def command_finish_step(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan = _load_root_plan(root)
    try:
        result = porcelain.finish_step(
            root, plan, args.step, args.attempt,
            strict_mode(args.strict), args.replay,
            agent_id=args.agent,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"finish-step {result['verification_status']} "
              f"verified={result['verified']} completed={result['completed']}")
        for line in result["diagnostics"]:
            print(f"  {line}")
    return 0 if result["completed"] else 1


def command_finish_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    # finish_run captures each delegated gate's output internally, so stdout
    # stays clean for --json and the failing gate's detail is surfaced in
    # result["diagnostics"] rather than swallowed.
    result = porcelain.finish_run(root, plan_path, strict_mode(args.strict))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for gate in result["gates"]:
            print(f"{gate['name']} {gate['status']}")
        if not result["ok"]:
            print(f"stopped at {result['stopped_at']}")
            for line in result["diagnostics"]:
                print(f"  {line}")
    return 0 if result["ok"] else 1


def command_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_path = root / ".agent/plan.lock.json"
    drift_path = root / ".agent/drift-report.json"
    evidence_path = root / ".agent/evidence.jsonl"
    failures_path = root / ".agent/failures.jsonl"
    runtime_config_path = root / ".agent/runtime.config.json"
    runtime_snapshots_path = root / ".agent/runtime-snapshots.jsonl"
    execution_contract_path = root / ".agent/execution.contract.json"
    step_runs_path = root / ".agent/step-runs.jsonl"
    command_receipts_path = root / ".agent/command-receipts.jsonl"
    file_receipts_path = root / ".agent/file-receipts.jsonl"

    print(f"agentflow {__version__}")
    print(f"root {root}")
    if not plan_path.exists():
        print("status uninitialized")
        print("missing .agent/plan.lock.json")
        return 1

    try:
        plan = read_json(plan_path)
    except json.JSONDecodeError as exc:
        print(f"status invalid: {exc}")
        return 1

    print(f"objective {plan.get('objective') or 'TBD'}")
    print(f"locked {bool(plan.get('locked'))}")
    print(f"steps {len(plan.get('steps', []))}")
    print(f"evidence {len(read_jsonl(evidence_path)) if evidence_path.exists() else 0}")
    print(f"failures {len(read_jsonl(failures_path)) if failures_path.exists() else 0}")
    if drift_path.exists():
        drift = read_json(drift_path)
        print(f"drift {drift.get('status', 'unknown')}")
    else:
        print("drift missing")
    print(f"runtime config {'present' if runtime_config_path.exists() else 'missing'}")
    print(
        "runtime snapshots "
        f"{len(read_jsonl(runtime_snapshots_path)) if runtime_snapshots_path.exists() else 0}"
    )
    print(f"execution contract {'present' if execution_contract_path.exists() else 'missing'}")
    print(f"step runs {len(read_jsonl(step_runs_path)) if step_runs_path.exists() else 0}")
    print(
        "command receipts "
        f"{len(read_jsonl(command_receipts_path)) if command_receipts_path.exists() else 0}"
    )
    print(
        "file receipts "
        f"{len(read_jsonl(file_receipts_path)) if file_receipts_path.exists() else 0}"
    )
    return 0


def _load_brief_from_path(path: Path) -> Any:
    # Return the parsed JSON as-is; validate_brief is the single source of truth
    # for the "must be an object" rule, so a non-dict body surfaces as a
    # validation_error rather than being mislabeled a JSON-parse failure.
    if not path.exists():
        raise FileNotFoundError(f"brief file not found: {path}")
    try:
        return read_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(_json_decode_message(str(path), exc)) from exc


def _load_brief_from_stdin() -> Any:
    try:
        return json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise ValueError(_json_decode_message("stdin", exc)) from exc


def _recommend_fail(code: str, message: str, *, json_mode: bool, exit_code: int) -> int:
    if json_mode:
        _print_plan_result("invalid", errors=[_plan_diagnostic(code, message)])
    else:
        print(message if exit_code == 2 else f"invalid brief: {message}", file=sys.stderr)
    return exit_code


def command_recommend_workflow(args: argparse.Namespace) -> int:
    if args.brief and args.stdin:
        return _recommend_fail(
            "invalid_arguments", "--brief and --stdin are mutually exclusive",
            json_mode=args.json, exit_code=2,
        )
    if not args.brief and not args.stdin:
        return _recommend_fail(
            "invalid_arguments", "one of --brief or --stdin is required",
            json_mode=args.json, exit_code=2,
        )

    try:
        brief = (
            _load_brief_from_stdin()
            if args.stdin
            else _load_brief_from_path(Path(args.brief).resolve())
        )
    except FileNotFoundError as exc:
        return _recommend_fail("input_not_found", str(exc), json_mode=args.json, exit_code=1)
    except ValueError as exc:
        code = "invalid_json" if isinstance(exc.__cause__, json.JSONDecodeError) else "invalid_brief_json"
        return _recommend_fail(code, str(exc), json_mode=args.json, exit_code=1)

    errors = validate_brief(brief)
    if errors:
        if args.json:
            _print_plan_result(
                "invalid",
                errors=[_plan_diagnostic("validation_error", error) for error in errors],
            )
        else:
            print("brief invalid")
            for error in errors:
                print(f"- {error}")
        return 1

    try:
        report = recommend_workflow(
            brief, selected_profile=args.selected_profile, reason=args.reason
        )
    except RecommendError as exc:
        return _recommend_fail(exc.code, str(exc), json_mode=args.json, exit_code=2)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_lines(render_recommendation(report))
    return 0


def _draft_fail(code: str, message: str, *, json_mode: bool, exit_code: int) -> int:
    if json_mode:
        _print_plan_result("invalid", errors=[_plan_diagnostic(code, message)])
    else:
        print(message, file=sys.stderr)
    return exit_code


def _existing_plan_is_locked(path: Path) -> bool:
    # A malformed/unreadable existing plan reports "not locked" and falls through
    # to the normal overwrite guard rather than masking the real problem.
    try:
        plan = read_json(path)
        return isinstance(plan, dict) and plan.get("locked") is True
    except (ValueError, OSError):
        return False


def _print_draft_warnings(warnings: List[Dict[str, str]]) -> None:
    # Non-fatal diagnostics (e.g. #89 greenfield candidate_file_missing) go to
    # stderr so machine consumers reading stdout JSON stay unaffected.
    for warning in warnings:
        print(f"warning [{warning['code']}]: {warning['message']}", file=sys.stderr)


def _render_draft_summary(payload: Dict[str, Any]) -> List[str]:
    recommended = payload["recommended"]
    selected = payload["selected"]
    plan = payload["plan_candidate"]
    return [
        f"drafted plan candidate from {selected['pack']}/{selected['profile']}",
        f"recommended {recommended['pack']}/{recommended['profile']}; "
        f"selected {selected['pack']}/{selected['profile']} ({payload['selection_mode']})",
        f"steps: {len(plan['steps'])}",
        f"validation gates: {', '.join(plan['validation_gates'])}",
        "(not written; pass --write to materialize "
        ".agent/plan.lock.json and .agent/workflow.contract.json)",
    ]


def command_draft_plan(args: argparse.Namespace) -> int:
    if args.brief and args.stdin:
        return _draft_fail(
            "invalid_arguments", "--brief and --stdin are mutually exclusive",
            json_mode=args.json, exit_code=2,
        )
    if not args.brief and not args.stdin:
        return _draft_fail(
            "invalid_arguments", "one of --brief or --stdin is required",
            json_mode=args.json, exit_code=2,
        )

    root = Path(args.root).resolve()

    try:
        brief = (
            _load_brief_from_stdin()
            if args.stdin
            else _load_brief_from_path(Path(args.brief).resolve())
        )
    except FileNotFoundError as exc:
        return _draft_fail("input_not_found", str(exc), json_mode=args.json, exit_code=1)
    except ValueError as exc:
        code = "invalid_json" if isinstance(exc.__cause__, json.JSONDecodeError) else "invalid_brief_json"
        return _draft_fail(code, str(exc), json_mode=args.json, exit_code=1)

    try:
        pack = load_pack(Path(args.workflow))
    except PackError as exc:
        return _draft_fail("invalid_pack", str(exc), json_mode=args.json, exit_code=1)

    try:
        result = compile_draft_plan(
            brief,
            pack.manifest,
            objective=args.objective or "",
            root=root,
            profile_id=args.profile,
            reason=args.reason,
            allow_missing_candidates=args.allow_missing_candidates,
        )
    except DraftPlanError as exc:
        return _draft_fail(exc.code, str(exc), json_mode=args.json, exit_code=1)

    plan = result["plan"]
    profile = result["profile"]
    report = result["report"]
    mode = result["selection_mode"]
    warnings = result["warnings"]
    reason_text = selection_reason(report, profile, mode, args.reason)
    contract = profile_to_contract(pack.manifest, profile["id"], "draft-plan", reason_text)
    contract["validation_policy"]["required_gates"] = list(plan["validation_gates"])
    # Validate the contract before any write so a malformed custom pack fails
    # closed with a diagnostic instead of half-materializing .agent/.
    contract_errors = validate_workflow_contract(contract)
    if contract_errors:
        return _draft_fail(
            "contract_invalid", "; ".join(contract_errors), json_mode=args.json, exit_code=1
        )
    recommended = report["recommended"]
    selected = {"pack": pack.manifest["id"], "profile": profile["id"]}

    if args.write:
        plan_path = root / ".agent/plan.lock.json"
        contract_path = root / WORKFLOW_CONTRACT_PATH
        # A locked plan is sacred: lock-plan is the only authority for it, so
        # --force overrides an unlocked draft but never a locked plan.
        if plan_path.exists() and _existing_plan_is_locked(plan_path):
            return _draft_fail(
                "locked_plan_exists",
                "refusing to overwrite a locked .agent/plan.lock.json; "
                "unlock or remove it first",
                json_mode=args.json, exit_code=1,
            )
        if (plan_path.exists() or contract_path.exists()) and not args.force:
            return _draft_fail(
                "artifact_exists",
                "refusing to overwrite existing .agent/plan.lock.json or "
                ".agent/workflow.contract.json (use --force)",
                json_mode=args.json, exit_code=1,
            )
        write_json(plan_path, plan)
        write_workflow_contract(root, contract)
        if args.json:
            print(json.dumps({
                "schema_version": DRAFT_PLAN_SCHEMA_VERSION,
                "status": "drafted",
                "path": str(plan_path),
                "contract_path": str(contract_path),
                "recommended": recommended,
                "selected": selected,
                "selection_mode": mode,
                "warnings": warnings,
                "errors": [],
            }, indent=2, sort_keys=True))
        else:
            print(f"drafted .agent/plan.lock.json from {selected['pack']}/{selected['profile']}")
            print(f"wrote {WORKFLOW_CONTRACT_PATH}")
            _print_draft_warnings(warnings)
        return 0

    payload = {
        "schema_version": DRAFT_PLAN_SCHEMA_VERSION,
        "status": "draft",
        "plan_candidate": plan,
        "workflow_contract": contract,
        "recommended": recommended,
        "selected": selected,
        "selection_mode": mode,
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_lines(_render_draft_summary(payload))
        _print_draft_warnings(warnings)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create .agent scaffold")
    init.add_argument("--root", default=".")
    init.add_argument("--force", action="store_true")
    init.add_argument("--pack", help="initialize from a workflow pack manifest path")
    init.add_argument("--profile", help="pack profile id (required with --pack)")
    init.add_argument(
        "--reason", help="selection reason recorded in the workflow contract"
    )
    init.set_defaults(func=command_init)

    init_execution = subparsers.add_parser("init-execution", help="create execution contract and ledgers")
    init_execution.add_argument("--root", default=".")
    init_execution.add_argument("--force", action="store_true")
    init_execution.set_defaults(func=command_init_execution)

    pack = subparsers.add_parser("pack", help="inspect workflow packs")
    pack_sub = pack.add_subparsers(dest="pack_command", required=True)
    pack_inspect = pack_sub.add_parser(
        "inspect", help="validate and summarize a workflow pack"
    )
    pack_inspect.add_argument("path")
    pack_inspect.add_argument(
        "--json", action="store_true", help="emit a JSON summary including manifest_sha256"
    )
    pack_inspect.set_defaults(func=command_pack_inspect)

    doctor_parser = subparsers.add_parser("doctor", help="check shell runtime readiness")
    doctor_parser.add_argument("--root", default=".")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(func=command_doctor)

    intake = subparsers.add_parser("intake", help="create task intake from a prompt file")
    intake.add_argument("--root", default=".")
    intake.add_argument("--from", dest="from_file", required=True)
    intake.set_defaults(func=command_intake)

    validate = subparsers.add_parser("validate-plan", help="validate a plan lock")
    validate.add_argument("plan", nargs="?", default=".agent/plan.lock.json")
    validate.set_defaults(func=command_validate_plan)

    lock = subparsers.add_parser("lock-plan", help="validate and lock a plan")
    lock.add_argument("plan", nargs="?", default=".agent/plan.lock.json")
    lock.add_argument("--stdin", action="store_true", help="read plan JSON from standard input")
    lock.add_argument("--from-json", dest="from_json", help="read plan JSON from this file")
    lock.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    lock.set_defaults(func=command_lock_plan)

    recommend_parser = subparsers.add_parser(
        "recommend-workflow", help="recommend a workflow posture from a task brief"
    )
    recommend_parser.add_argument("--brief", help="read the task brief JSON from this file")
    recommend_parser.add_argument(
        "--stdin", action="store_true", help="read the task brief JSON from standard input"
    )
    recommend_parser.add_argument(
        "--json", action="store_true", help="emit the recommendation as JSON"
    )
    recommend_parser.add_argument(
        "--selected-profile", dest="selected_profile",
        help="operator-selected profile id (override)",
    )
    recommend_parser.add_argument(
        "--reason",
        help="rationale required when --selected-profile differs from the recommendation",
    )
    recommend_parser.set_defaults(func=command_recommend_workflow)

    draft = subparsers.add_parser(
        "draft-plan",
        help="compile a task brief and workflow pack into an unlocked draft plan",
    )
    draft.add_argument("--root", default=".")
    draft.add_argument("--brief", help="read the task brief JSON from this file")
    draft.add_argument(
        "--stdin", action="store_true", help="read the task brief JSON from standard input"
    )
    draft.add_argument(
        "--workflow", required=True,
        help="path to the workflow pack (a .agentflow-pack dir, its parent, or pack.json)",
    )
    draft.add_argument(
        "--objective", help="the plan objective: the human intent the brief does not carry",
    )
    draft.add_argument("--profile", help="explicitly select a pack profile (override)")
    draft.add_argument(
        "--reason", help="rationale required when --profile is weaker than the recommendation",
    )
    draft.add_argument(
        "--allow-missing-candidates", "--greenfield",
        dest="allow_missing_candidates", action="store_true",
        help="downgrade candidate_file_missing to a warning so a plan can be "
        "drafted for files that do not exist yet (greenfield planning)",
    )
    draft.add_argument(
        "--write", action="store_true",
        help="write .agent/plan.lock.json (unlocked) and .agent/workflow.contract.json",
    )
    draft.add_argument(
        "--force", action="store_true", help="overwrite existing plan/contract when writing",
    )
    draft.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    draft.set_defaults(func=command_draft_plan)

    workflow_contract = subparsers.add_parser(
        "workflow-contract",
        help="write or validate workflow contract artifact",
    )
    workflow_contract.add_argument("path", nargs="?", default=WORKFLOW_CONTRACT_PATH)
    workflow_contract.add_argument("--root", default=".")
    workflow_contract_mode = workflow_contract.add_mutually_exclusive_group()
    workflow_contract_mode.add_argument(
        "--from-json",
        dest="from_json",
        help=f"read a contract JSON object and write {WORKFLOW_CONTRACT_PATH}",
    )
    workflow_contract_mode.add_argument(
        "--validate",
        action="store_true",
        help="validate an existing workflow contract artifact (default)",
    )
    workflow_contract.set_defaults(func=command_workflow_contract)

    amend = subparsers.add_parser("amend-plan", help="record a plan amendment")
    amend.add_argument("--root", default=".")
    amend.add_argument("--id", required=True)
    amend.add_argument("--reason", required=True)
    amend.add_argument("--evidence", action="append")
    amend.add_argument("--changed-scope", action="append")
    amend.add_argument("--changed-steps", action="append")
    amend.add_argument("--changed-risk")
    amend.add_argument("--requires-approval", action="store_true")
    amend.set_defaults(func=command_amend_plan)

    evidence = subparsers.add_parser("record-evidence", help="append an evidence ledger entry")
    evidence.add_argument("--root", default=".")
    evidence.add_argument("--id", required=True)
    evidence.add_argument("--claim", required=True)
    evidence.add_argument("--source", required=True)
    evidence.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    evidence.add_argument("--kind", choices=list(EVIDENCE_KINDS))
    evidence.add_argument("--supports", action="append")
    evidence.set_defaults(func=command_record_evidence)

    review = subparsers.add_parser("record-review", help="record a review run from its manifest")
    review.add_argument("--root", default=".")
    review.add_argument("--manifest", dest="manifest", required=True)
    review.add_argument("--emit-evidence", action="store_true")
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=command_record_review)

    record_capability = subparsers.add_parser(
        "record-capability", help="record a used specialized capability"
    )
    record_capability.add_argument("--root", default=".")
    record_capability.add_argument("--id", required=True)
    record_capability.add_argument("--capability", required=True)
    record_capability.add_argument("--provider", required=True)
    record_capability.add_argument("--reason", required=True)
    record_capability.add_argument("--evidence", action="append")
    record_capability.set_defaults(func=command_record_capability)

    waive_capability = subparsers.add_parser(
        "waive-capability", help="record a knowingly waived specialized capability"
    )
    waive_capability.add_argument("--root", default=".")
    waive_capability.add_argument("--id", required=True)
    waive_capability.add_argument("--capability", required=True)
    waive_capability.add_argument("--reason", required=True)
    waive_capability.add_argument("--evidence", action="append")
    waive_capability.set_defaults(func=command_waive_capability)

    review_manifest = subparsers.add_parser(
        "review-manifest",
        help="produce review-manifest.json from a findings-final.json sidecar",
    )
    review_manifest.add_argument("--root", default=".")
    review_manifest.add_argument("--state-dir", dest="state_dir", required=True)
    review_manifest.add_argument("--branch", default=None)
    review_manifest.add_argument("--config", default="docs/ai/config.json")
    review_manifest.add_argument(
        "--findings-json",
        dest="findings_json",
        default=None,
        help="path to findings sidecar; relative to --state-dir unless absolute "
        "(default: findings-final.json)",
    )
    review_manifest.add_argument(
        "--depth-profile",
        dest="depth_profile",
        default="deep",
        choices=WORKFLOW_REVIEW_DEPTHS,
        help="review depth this recorded run satisfies; lighter depths require "
        "fewer artifacts (default: deep = full four-pass)",
    )
    review_manifest.add_argument("--write", action="store_true")
    review_manifest.add_argument("--json", action="store_true")
    review_manifest.add_argument("--fail-on-block", dest="fail_on_block", action="store_true")
    review_manifest.add_argument("--strict-exit", dest="strict_exit", action="store_true")
    review_manifest.set_defaults(func=command_review_manifest)

    context = subparsers.add_parser("record-context", help="append a context receipt")
    context.add_argument("--root", default=".")
    context.add_argument("--id", required=True)
    context.add_argument("--source", required=True)
    context.add_argument("--reason", required=True)
    context.add_argument("--used-for", action="append", dest="used_for")
    context.add_argument("--bytes", type=int)
    context.set_defaults(func=command_record_context)

    failure = subparsers.add_parser("record-failure", help="append a failure signature")
    failure.add_argument("--root", default=".")
    failure.add_argument("--command", required=True)
    failure.add_argument("--failure-type", required=True)
    failure.add_argument("--relevant-lines", action="append")
    failure.add_argument("--suspected-cause", required=True)
    failure.add_argument("--next-action", required=True)
    failure.set_defaults(func=command_record_failure)

    audit = subparsers.add_parser("audit-drift", help="compare git changes to plan scope")
    audit.add_argument("--root", default=".")
    audit.add_argument("--plan", default=".agent/plan.lock.json")
    audit.set_defaults(func=command_audit_drift)

    next_step_parser = subparsers.add_parser("next-step", help="show the next eligible plan step")
    next_step_parser.add_argument("--root", default=".")
    next_step_parser.add_argument("--json", action="store_true")
    next_step_parser.set_defaults(func=command_next_step)

    claim = subparsers.add_parser("claim-step", help="claim a plan step")
    claim.add_argument("step")
    claim.add_argument("--root", default=".")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--lease-minutes", type=int)
    claim.add_argument("--json", action="store_true")
    claim.set_defaults(func=command_claim_step)

    amend = subparsers.add_parser(
        "amend-step", help="open an auditable amendment attempt on a completed step"
    )
    amend.add_argument("step")
    amend.add_argument("--root", default=".")
    amend.add_argument("--agent", required=True)
    amend.add_argument("--reason", required=True)
    amend.add_argument("--reason-code", choices=sorted(AMENDMENT_REASON_CODES))
    amend.add_argument("--finding", action="append", help="review-run-scoped finding ref RR-...#ID (repeatable)")
    amend.add_argument("--lease-minutes", type=int, help="lease TTL for the amendment attempt under enforce")
    amend.add_argument("--json", action="store_true")
    amend.set_defaults(func=command_amend_step)

    complete = subparsers.add_parser("complete-step", help="complete a verified step")
    complete.add_argument("step")
    complete.add_argument("--root", default=".")
    complete.add_argument("--attempt")
    complete.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    complete.add_argument("--json", action="store_true")
    complete.set_defaults(func=command_complete_step)

    block = subparsers.add_parser("block-step", help="mark an attempt blocked")
    block.add_argument("step")
    block.add_argument("--root", default=".")
    block.add_argument("--attempt")
    block.add_argument("--reason", required=True)
    block.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    block.add_argument("--json", action="store_true")
    block.set_defaults(func=command_block_step)

    fail = subparsers.add_parser("fail-step", help="mark an attempt failed")
    fail.add_argument("step")
    fail.add_argument("--root", default=".")
    fail.add_argument("--attempt")
    fail.add_argument("--reason", required=True)
    fail.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"),
                      help="record who forced the failure (break-glass; no owner check)")
    fail.add_argument("--json", action="store_true")
    fail.set_defaults(func=command_fail_step)

    reclaim = subparsers.add_parser(
        "reclaim-step", help="abandon an expired attempt and open a fresh claim"
    )
    reclaim.add_argument("step")
    reclaim.add_argument("--root", default=".")
    reclaim.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"), required=False)
    reclaim.add_argument("--reason", required=True)
    reclaim.add_argument("--lease-minutes", type=int)
    reclaim.add_argument("--json", action="store_true")
    reclaim.set_defaults(func=command_reclaim_step)

    renew = subparsers.add_parser(
        "renew-lease", help="extend an attempt's lease (owner self-recovery)"
    )
    renew.add_argument("step")
    renew.add_argument("--root", default=".")
    renew.add_argument("--attempt")
    renew.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    renew.add_argument("--minutes", type=int)
    renew.add_argument("--json", action="store_true")
    renew.set_defaults(func=command_renew_lease)

    run = subparsers.add_parser("run", help="run a command and record an observed receipt")
    run.add_argument("--root", default=".")
    run.add_argument("--step", required=True)
    run.add_argument("--attempt")
    run.add_argument("--gate")
    run.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    run.add_argument("--confirm-risk", action="store_true")
    run.add_argument("--json", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=command_run)

    record_cmd = subparsers.add_parser("record-command", help="record an externally run command")
    record_cmd.add_argument("--root", default=".")
    record_cmd.add_argument("--step", required=True)
    record_cmd.add_argument("--attempt")
    record_cmd.add_argument("--exit-code", type=int, required=True)
    record_cmd.add_argument("--gate")
    record_cmd.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    record_cmd.add_argument("--json", action="store_true")
    record_cmd.add_argument("command", nargs=argparse.REMAINDER)
    record_cmd.set_defaults(func=command_record_command)

    record_file = subparsers.add_parser("record-file-change", help="record changed files for a step")
    record_file.add_argument("--root", default=".")
    record_file.add_argument("--step", required=True)
    record_file.add_argument("--attempt")
    record_file.add_argument("--path", action="append", required=True)
    record_file.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    record_file.add_argument("--json", action="store_true")
    record_file.set_defaults(func=command_record_file_change)

    verify_step_parser = subparsers.add_parser("verify-step", help="verify one step attempt")
    verify_step_parser.add_argument("step")
    verify_step_parser.add_argument("--root", default=".")
    verify_step_parser.add_argument("--attempt")
    verify_step_parser.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    verify_step_parser.add_argument("--strict", action="store_true")
    verify_step_parser.add_argument("--replay", action="store_true")
    verify_step_parser.add_argument("--json", action="store_true")
    verify_step_parser.set_defaults(func=command_verify_step)

    verify_run_parser = subparsers.add_parser("verify-run", help="verify execution run coverage")
    verify_run_parser.add_argument("--root", default=".")
    verify_run_parser.add_argument("--plan", default=".agent/plan.lock.json")
    verify_run_parser.add_argument("--strict", action="store_true")
    verify_run_parser.add_argument(
        "--no-record",
        action="store_false",
        dest="record",
        help="verify without appending to .agent/verification-runs.jsonl",
    )
    verify_run_parser.add_argument("--json", action="store_true")
    verify_run_parser.set_defaults(record=True)
    verify_run_parser.set_defaults(func=command_verify_run)

    aggregate_parser = subparsers.add_parser(
        "aggregate-ledgers",
        help="merge N worktree ledgers into one canonical .agent/ (pass --dry-run to preview collisions without writing)",
    )
    aggregate_parser.add_argument("--input", action="append")
    aggregate_parser.add_argument("--source-id", action="append", dest="source_id")
    aggregate_parser.add_argument("--label", action="append")
    aggregate_parser.add_argument("--output", default=".")
    aggregate_parser.add_argument("--base")
    aggregate_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    aggregate_parser.add_argument("--json", action="store_true")
    aggregate_parser.set_defaults(func=command_aggregate_ledgers)

    detect_stuck_parser = subparsers.add_parser(
        "detect-stuck",
        help="detect stuck agent loops from receipts and verification history",
    )
    detect_stuck_parser.add_argument("--root", default=".")
    detect_stuck_parser.add_argument("--plan", default=".agent/plan.lock.json")
    detect_stuck_parser.add_argument("--strict", action="store_true")
    detect_stuck_parser.add_argument("--json", action="store_true")
    detect_stuck_parser.add_argument("--min-command-failures", type=_positive_int, default=3)
    detect_stuck_parser.add_argument("--min-verify-failures", type=_positive_int, default=2)
    detect_stuck_parser.add_argument("--min-cycle-repeats", type=_positive_int, default=3)
    detect_stuck_parser.set_defaults(func=command_detect_stuck)

    export_handoff_parser = subparsers.add_parser("export-handoff", help="write a provider-neutral work packet")
    export_handoff_parser.add_argument("--root", default=".")
    export_handoff_parser.add_argument("--step", required=True)
    export_handoff_parser.add_argument("--format", choices=["json", "markdown"], default="json")
    export_handoff_parser.add_argument("--output")
    export_handoff_parser.set_defaults(func=command_export_handoff)

    lint_handoff_parser = subparsers.add_parser("lint-handoff", help="lint a handoff for provider-specific language")
    lint_handoff_parser.add_argument("--root", default=".")
    lint_handoff_parser.add_argument("--input", required=True)
    lint_handoff_parser.add_argument("--json", action="store_true")
    lint_handoff_parser.set_defaults(func=command_lint_handoff)

    replay = subparsers.add_parser("replay-gates", help="replay attested validation gates")
    replay.add_argument("--root", default=".")
    replay.add_argument("--step")
    replay.add_argument("--record", action="store_true")
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=command_replay_gates)

    runtime = subparsers.add_parser("runtime-status", help="summarize configured runtime readiness")
    runtime.add_argument("--root", default=".")
    runtime.add_argument("--config", default=".agent/runtime.config.json")
    runtime.add_argument("--record", action="store_true")
    runtime.add_argument("--json", action="store_true")
    runtime.add_argument("--probe", action="store_true")
    runtime.add_argument("--strict", action="store_true")
    runtime.set_defaults(func=command_runtime_status)

    proof = subparsers.add_parser("build-proof", help="generate proof pack markdown")
    proof.add_argument("--root", default=".")
    proof.add_argument("--plan", default=".agent/plan.lock.json")
    proof.add_argument("--output", default=".agent/proof-pack.md")
    proof.add_argument("--strict", action="store_true")
    proof.set_defaults(func=command_build_proof)

    verify = subparsers.add_parser("verify-proof", help="verify proof-pack source hashes")
    verify.add_argument("--root", default=".")
    verify.add_argument("--proof", default=".agent/proof-pack.json")
    verify.add_argument("--replay", action="store_true")
    verify.add_argument("--strict", action="store_true")
    verify.set_defaults(func=command_verify_proof)

    view = subparsers.add_parser(
        "view-proof",
        help="render a static HTML proof report (review aid; verify-proof is authoritative)",
    )
    view.add_argument("--html", action="store_true")
    view.add_argument("--root", default=".")
    view.add_argument("--proof", default=".agent/proof-pack.json")
    view.add_argument("--output", default=".agent/proof-report.html")
    view.set_defaults(func=command_view_proof)

    events_parser = subparsers.add_parser(
        "events", help="project a chronological event stream over .agent ledgers"
    )
    events_parser.add_argument("--root", default=".")
    events_parser.add_argument("--since")
    events_format = events_parser.add_mutually_exclusive_group()
    events_format.add_argument("--jsonl", action="store_true")
    events_format.add_argument("--json", action="store_true")
    events_parser.set_defaults(func=command_events)

    next_action_parser = subparsers.add_parser(
        "next-action", help="report the next required Agentflow action")
    next_action_parser.add_argument("--root", default=".")
    next_action_parser.add_argument("--json", action="store_true")
    next_action_parser.add_argument("--strict", action="store_true")
    next_action_parser.set_defaults(func=command_next_action)

    finish_step_parser = subparsers.add_parser(
        "finish-step", help="verify then complete a step")
    finish_step_parser.add_argument("step")
    finish_step_parser.add_argument("--attempt", default=None)
    finish_step_parser.add_argument("--root", default=".")
    finish_step_parser.add_argument("--agent", default=os.environ.get("AGENTFLOW_AGENT_ID"))
    finish_step_parser.add_argument("--json", action="store_true")
    finish_step_parser.add_argument("--strict", action="store_true")
    finish_step_parser.add_argument("--replay", action="store_true")
    finish_step_parser.set_defaults(func=command_finish_step)

    finish_run_parser = subparsers.add_parser(
        "finish-run", help="run audit/verify/proof gates in order")
    finish_run_parser.add_argument("--root", default=".")
    finish_run_parser.add_argument("--plan", default=".agent/plan.lock.json")
    finish_run_parser.add_argument("--json", action="store_true")
    finish_run_parser.add_argument("--strict", action="store_true")
    finish_run_parser.set_defaults(func=command_finish_run)

    status = subparsers.add_parser("status", help="summarize current workflow state")
    status.add_argument("--root", default=".")
    status.set_defaults(func=command_status)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
