"""Schema-version parsing and compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


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


def validate_schema_version(actual: object, supported: str, artifact: str) -> List[str]:
    if not isinstance(actual, str):
        return [f"{artifact} schema_version must be MAJOR.MINOR.PATCH"]
    try:
        parse_schema_version(actual)
    except ValueError:
        return [f"{artifact} schema_version must be MAJOR.MINOR.PATCH"]
    if not is_schema_version_compatible(actual, supported):
        return [
            f"{artifact} schema_version {actual} is incompatible with supported {supported}"
        ]
    return []
