"""Schema-version parsing and compatibility checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .contracts import POLICY_EXACT, POLICY_NONE, POLICY_SAME_MAJOR


@dataclass(frozen=True)
class SchemaVersion:
    major: int
    minor: int
    patch: int


# Strict dotted triple: no whitespace, signs, leading zeros, or pre-release
# suffixes (int() would silently accept " 3"; "01" would alias "1").
_SCHEMA_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def parse_schema_version(value: str) -> SchemaVersion:
    match = _SCHEMA_VERSION_RE.fullmatch(value)
    if match is None:
        raise ValueError("schema_version must be MAJOR.MINOR.PATCH")
    return SchemaVersion(*(int(part) for part in match.groups()))


def is_schema_version_compatible(actual: str, supported: str) -> bool:
    try:
        actual_version = parse_schema_version(actual)
        supported_version = parse_schema_version(supported)
    except ValueError:
        return False
    return (
        actual_version.major == supported_version.major
        and actual_version.minor <= supported_version.minor
    )


def validate_schema_version(
    actual: object, supported: str, artifact: str, policy: Optional[str] = None
) -> List[str]:
    if policy is None:
        from .contracts import (
            ARTIFACT_COMPATIBILITY_POLICIES,
            DEFAULT_COMPATIBILITY_POLICY,
        )

        policy = ARTIFACT_COMPATIBILITY_POLICIES.get(
            artifact, DEFAULT_COMPATIBILITY_POLICY
        )
    return validate_schema_version_policy(actual, supported, artifact, policy)


def validate_schema_version_policy(
    actual: object, supported: str, artifact: str, policy: str
) -> List[str]:
    if policy == POLICY_NONE:
        return []
    if policy not in {POLICY_EXACT, POLICY_SAME_MAJOR}:
        raise ValueError(f"unknown schema compatibility policy: {policy}")
    if not isinstance(actual, str):
        return [f"{artifact} schema_version must be MAJOR.MINOR.PATCH"]
    try:
        parse_schema_version(actual)
    except ValueError:
        return [f"{artifact} schema_version must be MAJOR.MINOR.PATCH"]
    try:
        parse_schema_version(supported)
    except ValueError as exc:
        # A malformed *supported* version is a broken constant (developer
        # error), not bad user data; never blame the artifact for it.
        raise ValueError(
            f"invalid supported schema_version for {artifact}: {supported!r}"
        ) from exc
    compatible = (
        actual == supported
        if policy == POLICY_EXACT
        else is_schema_version_compatible(actual, supported)
    )
    if not compatible:
        return [
            f"{artifact} schema_version {actual} is incompatible with supported {supported}"
        ]
    return []


def validate_historical_proof_schema_version(
    actual: object, supported: str
) -> List[str]:
    """Validate the bounded verify-proof historical promise."""
    if isinstance(actual, str):
        try:
            actual_version = parse_schema_version(actual)
        except ValueError:
            return ["proof-pack schema_version must be MAJOR.MINOR.PATCH"]
        else:
            # supported comes from a constant; a parse failure there is a
            # developer error and must not be blamed on the proof.
            supported_version = parse_schema_version(supported)
            if (
                supported_version.major == 1
                and actual_version.major == 0
                and (actual_version.minor, actual_version.patch) >= (4, 0)
            ):
                return []
            if (actual_version.major, actual_version.minor) > (
                supported_version.major,
                supported_version.minor,
            ):
                return [
                    f"proof-pack schema_version {actual} is a newer schema than "
                    f"supported {supported}; upgrade Agentflow"
                ]
    return validate_schema_version_policy(
        actual, supported, "proof-pack", POLICY_SAME_MAJOR
    )
