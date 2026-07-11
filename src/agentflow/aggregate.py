"""Cross-worktree ledger aggregation analysis (read-only dry-run surface).

#30 impl 1/3. This module never writes to disk. It parses input worktrees,
detects fail-closed collisions, and computes the planned id rewrites for a
dry-run report. The write path (#111) and provenance (#112) build on top.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import read_jsonl, try_read_json, write_json
from .contracts import AGGREGATION_SCHEMA_VERSION, ARTIFACT_PATHS, BASE_ARTIFACT_PATHS, EXECUTION_ARTIFACT_PATHS
from .git import run_git
from .packs import _safe_relative_path
from .receipts import sha256_path

SOURCE_ID_RE = re.compile(r"^[a-z0-9]{1,16}$")

_ALL_PATHS = {**BASE_ARTIFACT_PATHS, **EXECUTION_ARTIFACT_PATHS}

MUST_MATCH = (
    "plan-lock",
    "execution-contract",
    "workflow-contract",
    "assumptions",
    "runtime-config",
)
CONCAT_LEDGERS = (
    "evidence",
    "context-receipts",
    "failures",
    "amendments",
    "capability-receipts",
    "runtime-snapshot",
)

# Strict local counter-id formats for the rewrite-class ledgers. Inputs are raw
# per-worktree ledgers, so their ids must be plain (never namespaced, never
# containing path separators); a violation is a hostile/corrupt foreign ledger
# and fails closed before the write path rebuilds any receipt path from an id.
_LOCAL_ID_RULES = {
    "step-runs": (("attempt_id", "A"), ("amends_attempt", "A"), ("superseded_by", "A")),
    "command-receipts": (("id", "CR"), ("attempt_id", "A")),
    "file-receipts": (("id", "FR"), ("attempt_id", "A")),
    "verification-runs": (("id", "VR"), ("attempt_id", "A")),
}

# JSONL ledgers read by the detectors. A malformed line in any of these would
# otherwise crash a detector mid-analysis; analyze() pre-flights them so a
# corrupt foreign ledger fails closed as a `malformed_ledger` collision.
_JSONL_LEDGERS = (
    "step-runs",
    "file-receipts",
    "command-receipts",
    "verification-runs",
    "review-runs",
) + CONCAT_LEDGERS


@dataclass(frozen=True)
class Source:
    root: Path
    source_id: str
    label: str

    @property
    def prefix(self) -> str:
        return f"WT{self.source_id}-"


def parse_sources(
    inputs: List[str],
    source_ids: List[str],
    labels: Optional[List[str]],
) -> Tuple[List[Source], List[str]]:
    errors: List[str] = []
    if len(inputs) != len(source_ids):
        errors.append(
            f"--input count ({len(inputs)}) must equal --source-id count ({len(source_ids)})"
        )
        return [], errors
    labels = labels or []
    if labels and len(labels) != len(inputs):
        errors.append(
            f"--label count ({len(labels)}) must equal --input count ({len(inputs)})"
        )
        return [], errors
    seen: set = set()
    sources: List[Source] = []
    for index, (inp, sid) in enumerate(zip(inputs, source_ids)):
        if not SOURCE_ID_RE.fullmatch(sid):
            errors.append(f"invalid --source-id {sid!r}: must match [a-z0-9]{{1,16}}")
            continue
        if sid in seen:
            errors.append(f"duplicate --source-id {sid!r}")
            continue
        seen.add(sid)
        root = Path(inp).resolve()
        label = labels[index] if labels else root.name
        sources.append(Source(root=root, source_id=sid, label=label))
    return sources, errors


def _collision(kind: str, **detail: Any) -> Dict[str, Any]:
    return {"kind": kind, **detail}


def _artifact_rel(name: str) -> str:
    return _ALL_PATHS[name]


def _read_bytes(path: Path) -> bytes:
    if not path.exists():
        return b""
    return path.read_bytes()


def _contained_path(root: Path, rel_path: str) -> Optional[Path]:
    candidate = root / rel_path
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _must_match_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    first = sources[0]
    for name in MUST_MATCH:
        rel = _artifact_rel(name)
        base = _read_bytes(first.root / rel)
        for other in sources[1:]:
            if _read_bytes(other.root / rel) != base:
                collisions.append(
                    _collision(
                        "must_match_mismatch",
                        artifact=name,
                        sources=[first.source_id, other.source_id],
                    )
                )
    return collisions


def _step_rows_by_step(source: Source) -> Dict[str, List[str]]:
    rel = EXECUTION_ARTIFACT_PATHS["step-runs"]
    rows: Dict[str, List[str]] = {}
    for row in read_jsonl(source.root / rel):
        step_id = row.get("step_id")
        if isinstance(step_id, str):
            rows.setdefault(step_id, []).append(json.dumps(row, sort_keys=True))
    return rows


def _step_overlap_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    seen: Dict[str, Tuple[str, List[str]]] = {}
    for source in sources:
        for step_id, rows in sorted(_step_rows_by_step(source).items()):
            if step_id in seen:
                first_source, first_rows = seen[step_id]
                if rows != first_rows:
                    collisions.append(_collision("step_overlap", step_id=step_id, sources=[first_source, source.source_id]))
            else:
                seen[step_id] = (source.source_id, rows)
    return collisions


def _file_rows_by_path(source: Source) -> Dict[str, List[str]]:
    rel = EXECUTION_ARTIFACT_PATHS["file-receipts"]
    rows: Dict[str, List[str]] = {}
    for row in read_jsonl(source.root / rel):
        path = row.get("path")
        if isinstance(path, str):
            rows.setdefault(path, []).append(json.dumps(row, sort_keys=True))
    return rows


def _file_overlap_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    seen: Dict[str, Tuple[str, List[str]]] = {}
    for source in sources:
        for path, rows in sorted(_file_rows_by_path(source).items()):
            if path in seen:
                first_source, first_rows = seen[path]
                if rows != first_rows:  # differing rows => concurrent edit; identical => shared baseline
                    collisions.append(_collision("file_overlap", path=path, sources=[first_source, source.source_id]))
            else:
                seen[path] = (source.source_id, rows)
    return collisions


def _concat_dup_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    for name in CONCAT_LEDGERS:
        rel = _artifact_rel(name)
        canon_by_id: Dict[str, str] = {}
        src_by_id: Dict[str, str] = {}
        for source in sources:
            for row in read_jsonl(source.root / rel):
                rid = row.get("id")
                if not isinstance(rid, str):
                    continue
                canon = json.dumps(row, sort_keys=True)
                if rid in canon_by_id:
                    if canon_by_id[rid] != canon:
                        collisions.append(
                            _collision(
                                "concat_dup_mismatch",
                                ledger=name,
                                id=rid,
                                sources=[src_by_id[rid], source.source_id],
                            )
                        )
                else:
                    canon_by_id[rid] = canon
                    src_by_id[rid] = source.source_id
    return collisions


def _receipt_file_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    rel = EXECUTION_ARTIFACT_PATHS["command-receipts"]
    for source in sources:
        for row in read_jsonl(source.root / rel):
            for path_key, sha_key in (("stdout_path", "stdout_sha256"), ("stderr_path", "stderr_sha256")):
                rel_path = row.get(path_key)
                expected = row.get(sha_key)
                if not rel_path:
                    continue
                if not _safe_relative_path(rel_path):
                    collisions.append(_collision("unsafe_path", source=source.source_id, path=rel_path, field=path_key))
                    continue
                fpath = _contained_path(source.root, rel_path)
                if fpath is None:
                    collisions.append(_collision("unsafe_path", source=source.source_id, path=rel_path, field=path_key))
                    continue
                if not fpath.exists():
                    collisions.append(_collision("receipt_file_missing", source=source.source_id, path=rel_path))
                    continue
                if expected and sha256_path(fpath) != expected:
                    collisions.append(_collision("receipt_hash_mismatch", source=source.source_id, path=rel_path))
    return collisions


def _review_artifact_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    from .review import sha256_file

    collisions: List[Dict[str, Any]] = []
    rel = BASE_ARTIFACT_PATHS["review-runs"]
    for source in sources:
        for row in read_jsonl(source.root / rel):
            manifest = row.get("manifest_path")
            manifest_sha = row.get("manifest_sha256")
            if isinstance(manifest, str):
                if not _safe_relative_path(manifest):
                    collisions.append(_collision("unsafe_path", source=source.source_id, path=manifest, field="manifest_path"))
                else:
                    path = _contained_path(source.root, manifest)
                    if path is None:
                        collisions.append(_collision("unsafe_path", source=source.source_id, path=manifest, field="manifest_path"))
                        continue
                    if not path.exists():
                        collisions.append(_collision("review_manifest_missing", source=source.source_id, path=manifest))
                    elif isinstance(manifest_sha, str) and sha256_file(path) != manifest_sha:
                        collisions.append(_collision("review_manifest_hash_mismatch", source=source.source_id, path=manifest))
            for entry in row.get("artifacts", []) if isinstance(row.get("artifacts"), list) else []:
                art = entry.get("path") if isinstance(entry, dict) else None
                expected = entry.get("sha256") if isinstance(entry, dict) else None
                if not isinstance(art, str):
                    continue
                if not _safe_relative_path(art):
                    collisions.append(_collision("unsafe_path", source=source.source_id, path=art, field="artifact_path"))
                    continue
                path = _contained_path(source.root, art)
                if path is None:
                    collisions.append(_collision("unsafe_path", source=source.source_id, path=art, field="artifact_path"))
                    continue
                if not path.exists():
                    collisions.append(_collision("review_artifact_missing", source=source.source_id, path=art))
                elif isinstance(expected, str) and sha256_file(path) != expected:
                    collisions.append(_collision("review_artifact_hash_mismatch", source=source.source_id, path=art))
    return collisions


def _review_dup_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    rel = BASE_ARTIFACT_PATHS["review-runs"]
    seen: Dict[str, Tuple[str, str]] = {}
    for source in sources:
        for row in read_jsonl(source.root / rel):
            rid = row.get("review_run_id")
            if not isinstance(rid, str):
                continue
            canon = json.dumps(row, sort_keys=True)
            if rid in seen:
                first_source, first_canon = seen[rid]
                if canon != first_canon:  # differing dup shadows in review_summary()'s by-id map
                    collisions.append(_collision("review_dup_id", review_run_id=rid, sources=[first_source, source.source_id]))
            else:
                seen[rid] = (source.source_id, canon)
    return collisions


def _head(root: Path) -> Optional[str]:
    code, out, _ = run_git(root, ["rev-parse", "HEAD"])
    return out.strip() if code == 0 and out.strip() else None


def _rev_parse(root: Path, ref: str) -> Optional[str]:
    code, out, _ = run_git(root, ["rev-parse", ref])
    return out.strip() if code == 0 and out.strip() else None


def _base_commit(source: Source, resolved_base: Optional[str], output_root: Path) -> Optional[str]:
    src_head = _head(source.root)
    if not src_head:
        return None
    if resolved_base is None:
        out_head = _head(output_root)
        if not out_head:
            return None
        code, out, _ = run_git(source.root, ["merge-base", out_head, src_head])
        return out.strip() if code == 0 and out.strip() else None
    code, out, _ = run_git(source.root, ["merge-base", src_head, resolved_base])
    if code != 0 or out.strip() != resolved_base:
        return None
    return out.strip() if code == 0 and out.strip() else None


def _source_bases(sources: List[Source], base_ref: Optional[str], output_root: Path) -> Dict[str, Optional[str]]:
    resolved_base = _rev_parse(output_root, base_ref) if base_ref else None
    if base_ref and resolved_base is None:
        return {s.source_id: None for s in sources}
    return {s.source_id: _base_commit(s, resolved_base, output_root) for s in sources}


def _base_commit_collisions(bases: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    distinct = {b for b in bases.values() if b is not None}
    if len(distinct) > 1:
        collisions.append(_collision("base_commit_mismatch", bases=dict(bases)))
    if any(b is None for b in bases.values()):
        collisions.append(_collision("base_commit_unresolved", bases=dict(bases)))
    return collisions


def _latest_file_receipts(sources: List[Source]) -> Dict[str, Tuple[str, Optional[str], str, Optional[str]]]:
    latest: Dict[str, Tuple[str, Optional[str], str, Optional[str]]] = {}
    rel = EXECUTION_ARTIFACT_PATHS["file-receipts"]
    for source in sources:
        for row in read_jsonl(source.root / rel):
            path = row.get("path")
            if not isinstance(path, str):
                continue
            ts = row.get("recorded_at", "")
            if not isinstance(ts, str):
                ts = ""
            if path not in latest or ts >= latest[path][0]:
                latest[path] = (ts, row.get("after_sha256"), source.source_id, row.get("change_kind"))
    return latest


def _precondition_collisions(sources: List[Source], output_root: Path) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    for path, (_, after, sid, change_kind) in sorted(_latest_file_receipts(sources).items()):
        if not _safe_relative_path(path):
            collisions.append(_collision("unsafe_path", source=sid, path=path, field="file_receipt_path"))
            continue
        target = _contained_path(output_root, path)
        if target is None:
            collisions.append(_collision("unsafe_path", source=sid, path=path, field="file_receipt_path"))
            continue
        if change_kind == "deleted":
            if target.exists():
                collisions.append(_collision("precondition_deleted_present", path=path, source=sid))
            continue
        if after is None:
            # A non-deleted receipt must record an after_sha256; a null one is a
            # malformed/foreign row, so fail closed rather than skip the check.
            collisions.append(_collision("precondition_malformed", path=path, source=sid))
            continue
        if not target.exists():
            collisions.append(_collision("precondition_missing", path=path, source=sid))
            continue
        if sha256_path(target) != after:
            collisions.append(_collision("precondition_hash_mismatch", path=path, source=sid))
    return collisions


def _ids(source: Source, name: str, key: str) -> set:
    rel = _artifact_rel(name)
    return {row[key] for row in read_jsonl(source.root / rel) if isinstance(row.get(key), str)}


def _verification_ids(source: Source) -> set:
    rel = EXECUTION_ARTIFACT_PATHS["verification-runs"]
    return {
        row["id"]
        for row in read_jsonl(source.root / rel)
        if row.get("scope") == "step" and isinstance(row.get("id"), str)
    }


def _planned_rewrites(sources: List[Source], bases: Dict[str, Optional[str]]) -> Dict[str, Any]:
    rewrites: Dict[str, Any] = {}
    for source in sources:
        prefix = source.prefix
        rewrites[source.source_id] = {
            "attempt_ids": {a: prefix + a for a in sorted(_ids(source, "step-runs", "attempt_id"))},
            "command_receipt_ids": {c: prefix + c for c in sorted(_ids(source, "command-receipts", "id"))},
            "file_receipt_ids": {f: prefix + f for f in sorted(_ids(source, "file-receipts", "id"))},
            "verification_run_ids": {v: prefix + v for v in sorted(_verification_ids(source))},
        }
    return {"sources": _source_summaries(sources, bases), "rewrites": rewrites}


def _malformed_ledger_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    for source in sources:
        for name in _JSONL_LEDGERS:
            try:
                rows = read_jsonl(source.root / _artifact_rel(name))
            except ValueError as exc:
                collisions.append(_collision("malformed_ledger", source=source.source_id, ledger=name, error=str(exc)))
                continue
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    collisions.append(
                        _collision(
                            "malformed_ledger",
                            source=source.source_id,
                            ledger=name,
                            error=f"{_artifact_rel(name)}:{index}: row must be a JSON object",
                        )
                    )
                    continue
                if name == "file-receipts" and isinstance(row.get("path"), str) and not isinstance(row.get("recorded_at"), str):
                    collisions.append(
                        _collision(
                            "malformed_ledger",
                            source=source.source_id,
                            ledger=name,
                            error=f"{_artifact_rel(name)}:{index}: recorded_at must be a string",
                        )
                    )
    return collisions


def _malformed_id_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    collisions: List[Dict[str, Any]] = []
    for source in sources:
        for name, rules in _LOCAL_ID_RULES.items():
            rel = _artifact_rel(name)
            for row in read_jsonl(source.root / rel):
                for field, tag in rules:
                    value = row.get(field)
                    if isinstance(value, str) and not re.fullmatch(rf"{tag}[0-9]+", value):
                        collisions.append(
                            _collision("malformed_id", source=source.source_id, ledger=name, field=field, value=value)
                        )
    return collisions


def _intra_source_dup_id_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    """A repeated counter ``id`` within ONE source's own rewrite-class ledger.

    Well-formed ledgers mint monotonic ids, so a repeat means a corrupt ledger
    (a double-append or crash mid-write). After namespacing both rows collapse
    to the same id and, for ``by_attempt`` command receipts, the same rebuilt
    physical path — a silent overwrite that would leave a ledger row pointing at
    another row's file. Fail closed before the write path can relocate anything.
    """
    collisions: List[Dict[str, Any]] = []
    for source in sources:
        for name in ("command-receipts", "file-receipts", "verification-runs"):
            rel = _artifact_rel(name)
            seen: set = set()
            for row in read_jsonl(source.root / rel):
                rid = row.get("id")
                if not isinstance(rid, str):
                    continue
                if rid in seen:
                    collisions.append(_collision("intra_source_dup_id", source=source.source_id, ledger=name, id=rid))
                seen.add(rid)
    return collisions


def _malformed_contract_collisions(sources: List[Source]) -> List[Dict[str, Any]]:
    """A present-but-unparseable execution-contract.

    The contract's ``command_policy.receipt_store`` drives the physical receipt
    layout the write path emits, so a contract that exists but does not parse to
    a JSON object must fail closed rather than let ``_receipt_store`` silently
    fall back to ``by_attempt`` and mis-lay-out the canonical receipts tree.
    """
    collisions: List[Dict[str, Any]] = []
    rel = EXECUTION_ARTIFACT_PATHS["execution-contract"]
    for source in sources:
        path = source.root / rel
        if not path.exists():
            continue
        _data, error = try_read_json(path)
        if error is not None:
            collisions.append(_collision("malformed_contract", source=source.source_id, error=error))
    return collisions


def _source_summaries(sources: List[Source], bases: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    return [
        {
            "source_id": source.source_id,
            "root_label": source.label,
            "base_commit": bases.get(source.source_id),
            "head_commit": _head(source.root),
            "namespaced_prefix": source.prefix,
        }
        for source in sources
    ]


def analyze(sources: List[Source], output_root: Path, base_ref: Optional[str] = None) -> Dict[str, Any]:
    # Shared entry for analyze/plan_write/write_canonical; several helpers index
    # sources[0], so guard the empty case here rather than crash with IndexError.
    if not sources:
        return {"status": "collision", "sources": [], "collisions": [_collision("no_sources")], "planned": {}}
    bases = _source_bases(sources, base_ref, output_root)
    # Pre-flight: a malformed JSONL ledger would crash a detector mid-scan, so a
    # corrupt foreign input must fail closed before any detector reads rows.
    malformed = _malformed_ledger_collisions(sources)
    if malformed:
        return {
            "status": "collision",
            "sources": _source_summaries(sources, bases),
            "collisions": malformed,
            "planned": {},
        }
    collisions: List[Dict[str, Any]] = []
    collisions += _must_match_collisions(sources)
    collisions += _malformed_contract_collisions(sources)
    collisions += _malformed_id_collisions(sources)
    collisions += _intra_source_dup_id_collisions(sources)
    collisions += _base_commit_collisions(bases)
    collisions += _step_overlap_collisions(sources)
    collisions += _file_overlap_collisions(sources)
    collisions += _concat_dup_collisions(sources)
    collisions += _receipt_file_collisions(sources)
    collisions += _review_artifact_collisions(sources)
    collisions += _review_dup_collisions(sources)
    collisions += _precondition_collisions(sources, output_root)
    planned = _planned_rewrites(sources, bases)
    return {
        "status": "collision" if collisions else "ok",
        "sources": planned["sources"],
        "collisions": collisions,
        "planned": planned["rewrites"],
    }


# --- write path (#111) ---------------------------------------------------

REWRITE_LEDGERS = ("step-runs", "command-receipts", "file-receipts", "verification-runs")


def _canon(row: Dict[str, Any]) -> str:
    """Canonical row bytes — the same byte-identity discriminator the overlap
    detectors use to tell shared baseline from a concurrent edit."""
    return json.dumps(row, sort_keys=True)


def _row_timestamp(row: Dict[str, Any]) -> str:
    """The row's natural timestamp for deterministic ordering (§6).

    ``last_verified`` is the evidence ledger's natural timestamp; without it,
    evidence concat rows would all fall back to "" and order by position only.
    """
    for key in ("recorded_at", "created_at", "started_at", "last_verified"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def _baseline_canon(sources: List[Source], rel: str) -> set:
    """Canonical rows present in >=2 sources for ledger ``rel``.

    A same-key row that *differed* across sources already fired a collision in
    ``analyze()``; so any row whose bytes appear in >=2 sources is byte-identical
    shared baseline (§ shared-baseline). It is emitted once, un-namespaced.
    """
    counts: Counter = Counter()
    for source in sources:
        seen_in_source: set = set()
        for row in read_jsonl(source.root / rel):
            canon = _canon(row)
            if canon not in seen_in_source:
                seen_in_source.add(canon)
                counts[canon] += 1
    return {canon for canon, n in counts.items() if n >= 2}


def _attempt_map(source: Source, step_baseline: set) -> Dict[str, str]:
    """Map tree-local attempt ids of ``source`` to their namespaced form.

    An attempt is tree-local iff it owns a tree-local step-run row (its
    ``attempt_id`` on a row whose bytes are not shared baseline). Baseline
    attempts (the completed prerequisite inherited by every tree) are absent
    from the map, so ``.get(a, a)`` leaves them un-namespaced.
    """
    rel = EXECUTION_ARTIFACT_PATHS["step-runs"]
    local: set = set()
    for row in read_jsonl(source.root / rel):
        if _canon(row) in step_baseline:
            continue
        attempt = row.get("attempt_id")
        if isinstance(attempt, str):
            local.add(attempt)
    return {attempt: source.prefix + attempt for attempt in sorted(local)}


def _receipt_store(sources: List[Source]) -> str:
    """Resolve ``command_policy.receipt_store`` from the must-match contract.

    The execution-contract is byte-identical across all inputs (a collision
    otherwise), so the first source is representative.
    """
    contract_path = sources[0].root / EXECUTION_ARTIFACT_PATHS["execution-contract"]
    if not contract_path.exists():
        return "by_attempt"  # absent uniformly (else must-match fires); default policy
    contract, _ = try_read_json(contract_path)
    policy = (contract or {}).get("command_policy", {})
    store = policy.get("receipt_store") if isinstance(policy, dict) else None
    return store if store in ("by_attempt", "content_addressed") else "by_attempt"


def _emit_baseline_then_local(
    sources: List[Source],
    rel: str,
    rewrite_local: Any,
    keep_row: Any = None,
    baseline: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Shared shape for the rewrite ledgers.

    Emits shared-baseline rows once (first source's input order, un-namespaced),
    then each source's tree-local rows in source order and original input order
    — preserving per-source transition order (§6 exception). ``rewrite_local``
    namespaces a tree-local row; ``keep_row`` (optional) filters rows out
    entirely (used to drop ``scope="run"`` verification rows).
    """
    baseline = baseline if baseline is not None else _baseline_canon(sources, rel)
    emitted: set = set()
    out: List[Dict[str, Any]] = []
    for source in sources:
        for row in read_jsonl(source.root / rel):
            if keep_row is not None and not keep_row(row):
                continue
            canon = _canon(row)
            if canon in baseline and canon not in emitted:
                emitted.add(canon)
                out.append(row)
    for source in sources:
        for row in read_jsonl(source.root / rel):
            if keep_row is not None and not keep_row(row):
                continue
            if _canon(row) in baseline:
                continue
            out.append(rewrite_local(row, source))
    return out


def _merge_step_runs(
    sources: List[Source], step_baseline: set, attempt_maps: Dict[str, Dict[str, str]]
) -> List[Dict[str, Any]]:
    rel = EXECUTION_ARTIFACT_PATHS["step-runs"]

    def rewrite(row: Dict[str, Any], source: Source) -> Dict[str, Any]:
        amap = attempt_maps[source.source_id]
        out = dict(row)
        for key in ("attempt_id", "amends_attempt", "superseded_by"):
            value = out.get(key)
            if isinstance(value, str) and value in amap:
                out[key] = amap[value]
        return out

    return _emit_baseline_then_local(sources, rel, rewrite, baseline=step_baseline)


def _merge_file_receipts(
    sources: List[Source], attempt_maps: Dict[str, Dict[str, str]]
) -> List[Dict[str, Any]]:
    rel = EXECUTION_ARTIFACT_PATHS["file-receipts"]

    def rewrite(row: Dict[str, Any], source: Source) -> Dict[str, Any]:
        amap = attempt_maps[source.source_id]
        out = dict(row)
        rid = out.get("id")
        if isinstance(rid, str):
            out["id"] = source.prefix + rid
        attempt = out.get("attempt_id")
        if isinstance(attempt, str):
            out["attempt_id"] = amap.get(attempt, attempt)
        return out

    return _emit_baseline_then_local(sources, rel, rewrite)


def _merge_verification_runs(
    sources: List[Source], attempt_maps: Dict[str, Dict[str, str]]
) -> List[Dict[str, Any]]:
    rel = EXECUTION_ARTIFACT_PATHS["verification-runs"]

    def keep(row: Dict[str, Any]) -> bool:
        return row.get("scope") == "step"  # drop run-level rows (§ledger-merge-rules)

    def rewrite(row: Dict[str, Any], source: Source) -> Dict[str, Any]:
        amap = attempt_maps[source.source_id]
        out = dict(row)
        rid = out.get("id")
        if isinstance(rid, str):
            out["id"] = source.prefix + rid
        attempt = out.get("attempt_id")
        if isinstance(attempt, str):
            out["attempt_id"] = amap.get(attempt, attempt)
        return out

    return _emit_baseline_then_local(sources, rel, rewrite, keep_row=keep)


def _cr_copies(
    source: Source, old_row: Dict[str, Any], new_row: Dict[str, Any]
) -> List[Tuple[Path, str, Optional[str]]]:
    """Physical files to copy for one command-receipt row.

    ``by_attempt`` copies each stream file to its rebuilt canonical path;
    ``content_addressed`` copies the content-hashed file to the same rel path.
    Only rows already validated by ``analyze()`` reach here, so ``old_row``'s
    stored paths are known-contained.
    """
    copies: List[Tuple[Path, str, Optional[str]]] = []
    for path_key, sha_key in (("stdout_path", "stdout_sha256"), ("stderr_path", "stderr_sha256")):
        old_rel = old_row.get(path_key)
        if not isinstance(old_rel, str) or not old_rel:
            continue
        expected = old_row.get(sha_key)
        expected = expected if isinstance(expected, str) else None
        dst_rel = new_row.get(path_key)
        if not isinstance(dst_rel, str):
            continue
        copies.append((source.root / old_rel, dst_rel, expected))
    return copies


def _merge_command_receipts(
    sources: List[Source], attempt_maps: Dict[str, Dict[str, str]], store: str
) -> Tuple[List[Dict[str, Any]], List[Tuple[Path, str, Optional[str]]]]:
    rel = EXECUTION_ARTIFACT_PATHS["command-receipts"]
    baseline = _baseline_canon(sources, rel)
    emitted: set = set()
    rows: List[Dict[str, Any]] = []
    copies: List[Tuple[Path, str, Optional[str]]] = []

    for source in sources:
        for row in read_jsonl(source.root / rel):
            canon = _canon(row)
            if canon in baseline and canon not in emitted:
                emitted.add(canon)
                rows.append(row)
                copies += _cr_copies(source, row, row)

    for source in sources:
        amap = attempt_maps[source.source_id]
        for row in read_jsonl(source.root / rel):
            canon = _canon(row)
            if canon in baseline:
                continue
            out = dict(row)
            rid = out.get("id")
            if isinstance(rid, str):
                out["id"] = source.prefix + rid
            attempt = out.get("attempt_id")
            if isinstance(attempt, str):
                out["attempt_id"] = amap.get(attempt, attempt)
            if store == "by_attempt":
                for path_key, stream in (("stdout_path", "stdout"), ("stderr_path", "stderr")):
                    if isinstance(out.get(path_key), str) and isinstance(out.get("id"), str) and isinstance(out.get("attempt_id"), str):
                        out[path_key] = f".agent/receipts/{out['attempt_id']}/{out['id']}.{stream}.txt"
            rows.append(out)
            copies += _cr_copies(source, row, out)
    return rows, copies


def _merge_concat(sources: List[Source], rel: str) -> List[Dict[str, Any]]:
    """Union concat-ledger rows, dedupe byte-identical duplicates (shared
    baseline emits once), order by ``(timestamp, source_order, input_index)``."""
    entries: List[Tuple[str, int, int, Dict[str, Any]]] = []
    seen: set = set()
    for order, source in enumerate(sources):
        for index, row in enumerate(read_jsonl(source.root / rel)):
            canon = _canon(row)
            if canon in seen:
                continue
            seen.add(canon)
            entries.append((_row_timestamp(row), order, index, row))
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in entries]


def _review_copy_path(canonical_state: str, state_dir: Any, path: str) -> str:
    if isinstance(state_dir, str) and path.startswith(state_dir + "/"):
        return f"{canonical_state}/{path[len(state_dir) + 1:]}"
    return f"{canonical_state}/{path}"


def _merge_review_runs(
    sources: List[Source],
) -> Tuple[List[Dict[str, Any]], List[Tuple[Path, str, Optional[str]]]]:
    rel = BASE_ARTIFACT_PATHS["review-runs"]
    entries: List[Tuple[str, int, int, Dict[str, Any], Source]] = []
    seen: set = set()
    for order, source in enumerate(sources):
        for index, row in enumerate(read_jsonl(source.root / rel)):
            canon = _canon(row)
            if canon in seen:  # byte-identical review_run_id row emits once (differing => collision)
                continue
            seen.add(canon)
            entries.append((_row_timestamp(row), order, index, row, source))
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    rows: List[Dict[str, Any]] = []
    copies: List[Tuple[Path, str, Optional[str]]] = []
    for _, _, _, row, source in entries:
        out = dict(row)
        state_dir = row.get("state_dir")
        review_run_id = row.get("review_run_id")
        canonical_state = f".agent/reviews/{review_run_id}" if _safe_relative_path(review_run_id) else ".agent/reviews/review"
        out["state_dir"] = canonical_state
        manifest = row.get("manifest_path")
        if isinstance(manifest, str):
            sha = row.get("manifest_sha256")
            new_manifest = _review_copy_path(canonical_state, state_dir, manifest)
            out["manifest_path"] = new_manifest
            copies.append((source.root / manifest, new_manifest, sha if isinstance(sha, str) else None))
        artifacts = row.get("artifacts")
        out_artifacts: List[Any] = []
        for entry in artifacts if isinstance(artifacts, list) else []:
            art = entry.get("path") if isinstance(entry, dict) else None
            sha = entry.get("sha256") if isinstance(entry, dict) else None
            if isinstance(art, str):
                new_art = _review_copy_path(canonical_state, state_dir, art)
                entry = dict(entry)
                entry["path"] = new_art
                copies.append((source.root / art, new_art, sha if isinstance(sha, str) else None))
            out_artifacts.append(entry)
        if isinstance(artifacts, list):
            out["artifacts"] = out_artifacts
        rows.append(out)
    return rows, copies


def plan_write(sources: List[Source]) -> Dict[str, Any]:
    """Pure planning pass: compute every canonical ledger row + every physical
    file to copy, without writing. Assumes ``analyze()`` already passed."""
    step_rel = EXECUTION_ARTIFACT_PATHS["step-runs"]
    step_baseline = _baseline_canon(sources, step_rel)
    attempt_maps = {source.source_id: _attempt_map(source, step_baseline) for source in sources}
    store = _receipt_store(sources)

    ledgers: Dict[str, List[Dict[str, Any]]] = {}
    ledgers[step_rel] = _merge_step_runs(sources, step_baseline, attempt_maps)
    ledgers[EXECUTION_ARTIFACT_PATHS["file-receipts"]] = _merge_file_receipts(sources, attempt_maps)
    ledgers[EXECUTION_ARTIFACT_PATHS["verification-runs"]] = _merge_verification_runs(sources, attempt_maps)
    cr_rows, receipt_copies = _merge_command_receipts(sources, attempt_maps, store)
    ledgers[EXECUTION_ARTIFACT_PATHS["command-receipts"]] = cr_rows
    for name in CONCAT_LEDGERS:
        ledgers[_artifact_rel(name)] = _merge_concat(sources, _artifact_rel(name))
    review_rows, review_copies = _merge_review_runs(sources)
    ledgers[BASE_ARTIFACT_PATHS["review-runs"]] = review_rows

    must_match: Dict[str, bytes] = {}
    for name in MUST_MATCH:
        rel = _artifact_rel(name)
        path = sources[0].root / rel
        if path.exists():
            must_match[rel] = path.read_bytes()

    return {
        "ledgers": ledgers,
        "receipt_copies": receipt_copies,
        "review_copies": review_copies,
        "must_match": must_match,
    }


def _write_row_file(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _stage_canonical(plan: Dict[str, Any], agent_dir: Path) -> None:
    """Materialise the canonical .agent/ under ``agent_dir`` (a fresh dir).

    Raises on any unsafe destination, copy hash mismatch, or missing source so
    the caller aborts before swapping anything into place (fail closed, no
    partial output).
    """
    staging_root = agent_dir.parent
    for rel, data in plan["must_match"].items():
        target = staging_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for rel, rows in plan["ledgers"].items():
        if not rows:
            continue  # an empty ledger is simply absent (matches single-tree runs)
        _write_row_file(staging_root / rel, rows)
    for src_abs, dst_rel, expected in plan["receipt_copies"] + plan["review_copies"]:
        # Defense in depth: analyze() already fails closed on malformed input ids
        # (so a rebuilt receipt dst_rel cannot contain a traversal), but the write
        # consumer must never join an escaping relative path to the output either.
        if not _safe_relative_path(dst_rel) or _contained_path(staging_root, dst_rel) is None:
            raise ValueError(f"aggregate: unsafe canonical destination path: {dst_rel}")
        dst = staging_root / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not src_abs.exists():
            raise FileNotFoundError(f"aggregate: source artifact vanished mid-write: {src_abs}")
        shutil.copyfile(src_abs, dst)
        if expected is not None and sha256_path(dst) != expected:
            raise ValueError(f"aggregate: copied artifact hash mismatch: {dst_rel}")


def write_canonical(
    sources: List[Source], output_root: Path, base_ref: Optional[str] = None
) -> Dict[str, Any]:
    """Validate, then atomically emit one canonical .agent/ into ``output_root``.

    Returns the ``analyze()`` collision report (nothing written) when validation
    fails, else ``{"status": "ok", "sources": [...], "written": {...}}``.

    Precondition (design "Immutable inputs"): the caller guarantees the input
    trees are quiescent for the duration of the call. ``analyze()`` and the
    ``plan_write()`` pass each read the source ledgers independently; a source
    mutated between them would bypass the validation gate. Aggregation never
    writes to inputs, but it does not lock them — the orchestrator must only
    aggregate completed, no-longer-active worktrees.
    """
    report = analyze(sources, output_root, base_ref=base_ref)
    if report["status"] != "ok":
        return report

    plan = plan_write(sources)
    output_root = Path(output_root)
    staging = Path(tempfile.mkdtemp(prefix=".agent.aggregate.", dir=output_root))
    final = output_root / ".agent"
    backup: Optional[Path] = None
    try:
        agent_dir = staging / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        _stage_canonical(plan, agent_dir)
        # Provenance is generated fresh each run, never read/merged from inputs
        # (contracts keeps "aggregation" out of the merge-path lists).
        provenance = {
            "schema_version": AGGREGATION_SCHEMA_VERSION,
            "mode": "cross_worktree",
            "source_count": len(sources),
            "sources": report["sources"],
        }
        write_json(staging / ARTIFACT_PATHS["aggregation"], provenance)
        if final.exists():
            backup = output_root / (staging.name + ".bak")
            os.replace(final, backup)
        os.replace(agent_dir, final)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)  # success: drop the superseded tree
            backup = None
    except BaseException:
        if backup is not None and not final.exists():
            try:
                os.replace(backup, final)  # restore the pre-existing tree
                backup = None
            except OSError:
                pass  # restore failed: leave the .bak on disk for manual recovery, never delete it
        raise
    finally:
        # Only the staging dir is unconditionally removed. A surviving ``backup``
        # here means a restore failed, so it is the operator's last copy of the
        # pre-existing tree and must be preserved.
        shutil.rmtree(staging, ignore_errors=True)

    written = {rel: len(rows) for rel, rows in plan["ledgers"].items() if rows}
    written["receipts"] = len(plan["receipt_copies"])
    written["review_files"] = len(plan["review_copies"])
    return {"status": "ok", "sources": report["sources"], "written": written}
