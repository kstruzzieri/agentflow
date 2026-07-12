#!/usr/bin/env python3
"""Validate Agentflow version declarations, release tags, and CHANGELOG notes."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
)
VERSION_RE = re.compile(rf"^{VERSION_PATTERN}$")
TAG_RE = re.compile(rf"^v(?P<version>{VERSION_PATTERN})$")
PACKAGE_VERSION_RE = re.compile(
    r"^__version__\s*=\s*(?P<quote>['\"])(?P<version>[^'\"]+)(?P=quote)\s*$",
    re.MULTILINE,
)
RELEASE_HEADING_RE = re.compile(
    rf"^## \[(?P<version>{VERSION_PATTERN})\] - "
    rf"[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\s*$",
    re.MULTILINE,
)
SECTION_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
FOOTER_LINK_RE = re.compile(
    rf"^\[(?:Unreleased|{VERSION_PATTERN})\]:[ \t]+", re.MULTILINE
)


class ReleaseCheckError(ValueError):
    """A release input is missing, malformed, or inconsistent."""


def _project_version(root: Path) -> str:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseCheckError(
            "pyproject.toml project.version must be a literal MAJOR.MINOR.PATCH"
        )
    return version


def _package_version(root: Path) -> str:
    path = root / "src" / "agentflow" / "__init__.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    matches = list(PACKAGE_VERSION_RE.finditer(text))
    if len(matches) != 1:
        raise ReleaseCheckError(
            "src/agentflow/__init__.py must contain exactly one literal "
            "__version__ assignment"
        )
    version = matches[0].group("version")
    if not VERSION_RE.fullmatch(version):
        raise ReleaseCheckError(
            "agentflow.__version__ must be a literal MAJOR.MINOR.PATCH"
        )
    return version


def check_versions(root: Path) -> str:
    project = _project_version(root)
    package = _package_version(root)
    if project != package:
        raise ReleaseCheckError(
            f"version mismatch: pyproject.toml has {project}; "
            f"agentflow.__version__ has {package}"
        )
    return project


def _release_notes(root: Path, version: str) -> str:
    path = root / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    matches = [
        match
        for match in RELEASE_HEADING_RE.finditer(text)
        if match.group("version") == version
    ]
    if len(matches) != 1:
        raise ReleaseCheckError(
            f"found {len(matches)} release headings for {version}; expected 1"
        )
    start = matches[0].end()
    next_heading = SECTION_HEADING_RE.search(text, start)
    footer = FOOTER_LINK_RE.search(text, start)
    boundaries = [len(text)]
    if next_heading:
        boundaries.append(next_heading.start())
    if footer:
        boundaries.append(footer.start())
    end = min(boundaries)
    notes = text[start:end].strip()
    if not notes:
        raise ReleaseCheckError(f"CHANGELOG release {version} has no notes")
    return notes + "\n"


def check_release(root: Path, tag: str) -> tuple[str, str]:
    tag_match = TAG_RE.fullmatch(tag)
    if not tag_match:
        raise ReleaseCheckError(
            f"invalid release tag {tag!r}; expected vMAJOR.MINOR.PATCH"
        )
    declared = check_versions(root)
    tagged = tag_match.group("version")
    if tagged != declared:
        raise ReleaseCheckError(
            f"version mismatch: tag has {tagged}; declarations have {declared}"
        )
    return declared, _release_notes(root, declared)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--tag")
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args(argv)
    if args.notes_file is not None and args.tag is None:
        parser.error("--notes-file requires --tag")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.tag is None:
            version = check_versions(args.root)
            message = f"version declarations agree: {version}"
        else:
            version, notes = check_release(args.root, args.tag)
            if args.notes_file is not None:
                try:
                    args.notes_file.write_text(notes, encoding="utf-8")
                except OSError as exc:
                    raise ReleaseCheckError(
                        f"cannot write {args.notes_file}: {exc}"
                    ) from exc
            message = f"release guard passed: v{version}"
    except ReleaseCheckError as exc:
        sys.stderr.write(f"release check failed: {exc}\n")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
