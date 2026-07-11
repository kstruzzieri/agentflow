"""Deterministic, no-network, no-LLM command risk screening.

Classifies a command's argv (and any ``sh -c`` payload) into the existing
low/medium/high vocabulary BEFORE ``agentflow run`` executes it. This is
screening, not a sandbox: it does not contain execution or fully model shell
semantics. It is a conservative pattern matcher over the argv the caller passed.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import RISK_POLICIES
from .validation import effective_scope, matches_path

_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}

_SHELL_PROGRAMS = {"sh", "bash", "zsh", "dash", "ksh"}
_WRAPPER_PROGRAMS = {"env", "command"}
_PRIVILEGE_PROGRAMS = {"sudo", "doas", "su"}
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&]")
_CREDENTIAL_GLOBS = ("*.pem", "*.key")
_CREDENTIAL_BASENAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", ".netrc", ".env",
}
_SHELL_LONG_OPTIONS_WITH_VALUES = {"--init-file", "--rcfile"}
_SHELL_SHORT_OPTIONS_WITH_VALUES = {"o", "O"}


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _finding(category: str, level: str, detail: str) -> Dict[str, str]:
    return {"category": category, "level": level, "detail": detail}


def _extract_shell_payload(argv: List[str]) -> Optional[str]:
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if token == "-c":
            payload_index = index + 1
            return argv[payload_index] if payload_index < len(argv) else None
        if token in _SHELL_LONG_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(
            token.startswith(f"{option}=")
            for option in _SHELL_LONG_OPTIONS_WITH_VALUES
        ):
            index += 1
            continue
        if (
            (token.startswith("-") and not token.startswith("--"))
            or token.startswith("+")
        ):
            if token.startswith("-") and "c" in token[1:]:
                payload_index = index + 1
                return argv[payload_index] if payload_index < len(argv) else None
            if any(option in token[1:] for option in _SHELL_SHORT_OPTIONS_WITH_VALUES):
                index += 2
            else:
                index += 1
            continue
        if token.startswith("--"):
            index += 1
            continue
        return None
    return None


def _max_level(findings: List[Dict[str, str]]) -> str:
    level = "low"
    for finding in findings:
        if _LEVEL_RANK[finding["level"]] > _LEVEL_RANK[level]:
            level = finding["level"]
    return level


def normalize_command(command: List[str]) -> Dict[str, Any]:
    """Peel known wrappers, identify the real program and any shell payload."""
    tokens = [str(item) for item in command]
    privilege: List[str] = []
    index = 0
    while index < len(tokens):
        base = _basename(tokens[index])
        if base in _PRIVILEGE_PROGRAMS:
            privilege.append(base)
            index += 1
            continue
        if base in _WRAPPER_PROGRAMS:
            index += 1
            # env may carry NAME=VALUE assignments and short flags
            while index < len(tokens) and (
                "=" in tokens[index] or tokens[index].startswith("-")
            ):
                index += 1
            continue
        break
    rest = tokens[index:]
    program = _basename(rest[0]) if rest else ""
    shell_payload: Optional[str] = None
    if program in _SHELL_PROGRAMS:
        shell_payload = _extract_shell_payload(rest)
    return {
        "program": program,
        "argv": rest,
        "shell_payload": shell_payload,
        "privilege_wrappers": privilege,
    }


def _safe_split(text: str) -> List[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _scan_program(program: str, argv: List[str]) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    flags = _option_flags(argv)
    if program == "rm":
        recursive = any(_rm_flag_has(flag, {"r", "R"}, {"--recursive"}) for flag in flags)
        force = any(_rm_flag_has(flag, {"f"}, {"--force"}) for flag in flags)
        if recursive and force:
            findings.append(_finding("destructive_delete", "high", "rm recursive force"))
    if program == "find" and "-delete" in argv:
        findings.append(_finding("destructive_delete", "high", "find -delete"))
    if program == "chmod":
        if any(_is_broad_mode(token) for token in argv[1:]):
            findings.append(_finding("permission_change", "high", "chmod broad mode"))
    if program == "chown":
        if any(flag in ("-R", "--recursive") for flag in flags):
            findings.append(_finding("permission_change", "high", "chown -R"))
    if program in _PRIVILEGE_PROGRAMS:
        findings.append(_finding("privilege_escalation", "high", program))
    return findings


def _option_flags(argv: List[str]) -> List[str]:
    flags: List[str] = []
    for token in argv[1:]:
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            flags.append(token)
    return flags


def _rm_flag_has(flag: str, short_names: set[str], long_names: set[str]) -> bool:
    if flag in long_names:
        return True
    if flag.startswith("--"):
        return False
    if flag.startswith("-") and flag != "-":
        return any(name in flag[1:] for name in short_names)
    return False


def _is_broad_mode(token: str) -> bool:
    candidate = token.lower()
    if candidate in {"777", "0777", "a+rwx", "a=rwx", "o+rwx", "o+w", "+w"}:
        return True
    return candidate.endswith("777")


def _looks_like_credential(token: str) -> bool:
    base = _basename(token)
    if base in _CREDENTIAL_BASENAMES:
        return True
    if base.startswith("id_") and "/.ssh/" in "/" + token:
        return True
    if any(fnmatch(base, pattern) for pattern in _CREDENTIAL_GLOBS):
        return True
    if "/.aws/credentials" in "/" + token:
        return True
    return False


def _path_tokens(argv: List[str]) -> List[str]:
    return [token for token in argv[1:] if not token.startswith("-")]


def _normalize_repo_path(token: str, root: Optional[Path]) -> str:
    cleaned = token
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if root is not None and cleaned.startswith("/"):
        root_text = root.as_posix().rstrip("/")
        if cleaned == root_text:
            return "."
        if cleaned.startswith(f"{root_text}/"):
            cleaned = cleaned[len(root_text) + 1 :]
    return posixpath.normpath(cleaned)


def _scan_credentials(tokens: List[str]) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    for token in tokens:
        if _looks_like_credential(token):
            findings.append(_finding("credential_read", "high", _basename(token)))
    return findings


def _scan_payload(payload: str) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    if re.search(r"\b(curl|wget)\b", payload) and re.search(
        r"\|\s*(sudo\s+)?(sh|bash|zsh|dash|ksh)\b", payload
    ):
        findings.append(_finding("pipe_to_shell", "high", "pipe to shell"))
    for segment in _SEGMENT_SPLIT.split(payload):
        argv = _safe_split(segment)
        if not argv:
            continue
        program = _basename(argv[0])
        findings.extend(_scan_program(program, argv))
        findings.extend(_scan_credentials(_path_tokens(argv)))
    return findings


def _redirect_targets(payload: str) -> List[str]:
    return re.findall(r">>?\s*([^\s;|&>]+)", payload)


def _ambiguous_write_targets(argv: List[str]) -> List[str]:
    program = _basename(argv[0]) if argv else ""
    operands = [token for token in argv[1:] if not token.startswith("-")]
    if program in {"cp", "mv", "install"} and len(operands) >= 2:
        return [operands[-1]]
    if program == "tee" and operands:
        return operands
    return []


def _scan_plan_relative(
    model: Dict[str, Any],
    plan: Dict[str, Any],
    step_id: str,
    root: Optional[Path],
) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    effective_allowed, blocked = effective_scope(plan, step_id)

    payload = model["shell_payload"]
    payload_argv: List[str] = _safe_split(payload) if payload else []
    all_tokens = _path_tokens(model["argv"]) + _path_tokens(payload_argv)

    for token in all_tokens:
        norm = _normalize_repo_path(token, root)
        if blocked and matches_path(norm, blocked):
            findings.append(_finding("blocked_path", "high", norm))

    explicit_writes = _redirect_targets(payload) if payload else []
    for token in explicit_writes:
        norm = _normalize_repo_path(token, root)
        if not matches_path(norm, effective_allowed):
            findings.append(_finding("write_outside_scope", "high", norm))

    ambiguous = _ambiguous_write_targets(model["argv"])
    if payload_argv:
        ambiguous += _ambiguous_write_targets(payload_argv)
    for token in ambiguous:
        norm = _normalize_repo_path(token, root)
        if not matches_path(norm, effective_allowed):
            findings.append(_finding("write_outside_scope", "medium", norm))
    return findings


def _dedupe_sorted(findings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    unique: List[Dict[str, str]] = []
    for finding in findings:
        key = (finding["category"], finding["level"], finding["detail"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda f: (f["category"], f["level"], f["detail"]))
    return unique


def evaluate_policy(level: str, policy: Optional[str], confirmed: bool) -> str:
    """Return "allow" or "block".

    Missing policy (None) maps to "warn" for legacy contracts. A present but
    unknown value raises ValueError so enforcement fails closed instead of
    silently degrading to "warn".
    """
    effective = "warn" if policy is None else policy
    if effective not in RISK_POLICIES:
        raise ValueError(f"unknown risk_policy {policy!r}")
    if effective == "warn" or level != "high":
        return "allow"
    if effective == "block":
        return "block"
    return "allow" if confirmed else "block"


def classify_command(
    command: List[str],
    plan: Dict[str, Any],
    step_id: str,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return {"level": "low|medium|high", "findings": [...]}."""
    if not command:
        return {"level": "low", "findings": []}
    model = normalize_command(command)
    findings: List[Dict[str, str]] = []
    for wrapper in model["privilege_wrappers"]:
        findings.append(_finding("privilege_escalation", "high", wrapper))
    findings.extend(_scan_program(model["program"], model["argv"]))
    findings.extend(_scan_credentials(_path_tokens(model["argv"])))
    if model["shell_payload"] is not None:
        findings.extend(_scan_payload(model["shell_payload"]))
    findings.extend(_scan_plan_relative(model, plan, step_id, root))
    findings = _dedupe_sorted(findings)
    return {"level": _max_level(findings), "findings": findings}
