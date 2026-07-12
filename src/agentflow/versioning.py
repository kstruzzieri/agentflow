"""Schema-version parsing and compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class SchemaVersion:
    major: int
    minor: int
    patch: int


def parse_schema_version(value: str) -> SchemaVersion:
    parts = value.split(".")
    if len(parts) != 3:
        raise ValueError("schema_version must be MAJOR.MINOR.PATCH")
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("schema_version must be MAJOR.MINOR.PATCH") from exc
    if major < 0 or minor < 0 or patch < 0:
        raise ValueError("schema_version must be MAJOR.MINOR.PATCH")
    return SchemaVersion(major, minor, patch)


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
        from .contracts import ARTIFACT_COMPATIBILITY_POLICIES

        policy = ARTIFACT_COMPATIBILITY_POLICIES.get(artifact, "same_major")
    return validate_schema_version_policy(actual, supported, artifact, policy)


def validate_schema_version_policy(
    actual: object, supported: str, artifact: str, policy: str
) -> List[str]:
    if policy == "none":
        return []
    if policy not in {"exact", "same_major"}:
        raise ValueError(f"unknown schema compatibility policy: {policy}")
    if not isinstance(actual, str):
        return [f"{artifact} schema_version must be MAJOR.MINOR.PATCH"]
    try:
        parse_schema_version(actual)
        parse_schema_version(supported)
    except ValueError:
        return [f"{artifact} schema_version must be MAJOR.MINOR.PATCH"]
    compatible = (
        actual == supported
        if policy == "exact"
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
            supported_version = parse_schema_version(supported)
        except ValueError:
            # An unreadable proof version cannot establish an expected version
            # mismatch; let integrity verification classify the artifact.
            return []
        else:
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
    return validate_schema_version_policy(actual, supported, "proof-pack", "same_major")
