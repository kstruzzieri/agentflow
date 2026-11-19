#!/usr/bin/env python3
"""Compare every file PyPI serves for agentflow-proof with the GitHub releases.

For each version on PyPI, the SHA-256 of each file must equal the entry for
that filename in the ``SHA256SUMS`` asset of the GitHub release tagged
``v<version>``. A version with no such release, a file ``SHA256SUMS`` does not
list, or a different hash is reported.

The GitHub release is the reference. This finds a file that reached PyPI some
other way than the release workflow; it cannot find a compromised repository.

Exit status: 0 everything matches, 1 a discrepancy was confirmed (even if
other releases could not be checked), 2 an incomplete scan with no confirmed
discrepancy.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional, Sequence

PROJECT = "agentflow-proof"
REPOSITORY = "kstruzzieri/agentflow"
PYPI_URL = "https://pypi.org/pypi/{project}/json"
SUMS_URL = (
    "https://github.com/{repository}/releases/download/v{version}/SHA256SUMS"
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
SUMS_LINE = re.compile(r"([0-9a-f]{64})[ \t]+\*?(\S+)")

# Returns the body, or None when the URL does not exist (HTTP 404).
Fetch = Callable[[str], Optional[bytes]]


class ReleaseWatchError(ValueError):
    """The data needed for the comparison is missing or unreadable."""

    def __init__(self, message: str, *, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)


def _fetch(url: str) -> Optional[bytes]:
    # ponytail: no retry; a network blip fails one scheduled run. Add retries
    # if the schedule turns noisy.
    request = urllib.request.Request(
        url, headers={"User-Agent": "agentflow-release-watch"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if len(body) > MAX_RESPONSE_BYTES:
        raise ReleaseWatchError(f"response is too large: {url}")
    return body


def parse_sums(text: str) -> dict[str, str]:
    """Return ``{filename: sha256}`` from ``sha256sum`` output."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = SUMS_LINE.fullmatch(line.strip())
        if match is None:
            raise ReleaseWatchError(f"unreadable SHA256SUMS line: {line!r}")
        sums[match.group(2)] = match.group(1)
    return sums


def _file_digest(entry: object) -> tuple[str, str]:
    if not isinstance(entry, dict):
        raise ReleaseWatchError("expected a PyPI file object")
    name = entry.get("filename")
    if not isinstance(name, str) or not name.strip():
        raise ReleaseWatchError("expected a nonempty PyPI filename")
    digests = entry.get("digests")
    digest = digests.get("sha256") if isinstance(digests, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReleaseWatchError(f"invalid PyPI SHA-256 for {name}")
    return name, digest


def find_problems(
    fetch: Fetch = _fetch,
    project: str = PROJECT,
    repository: str = REPOSITORY,
) -> tuple[list[str], int, int]:
    """Return ``(problems, versions checked, files checked)``."""
    body = fetch(PYPI_URL.format(project=project))
    if body is None:
        raise ReleaseWatchError(f"PyPI has no project named {project}")
    project_data = json.loads(body)
    releases = project_data.get("releases") if isinstance(project_data, dict) else None
    if not isinstance(releases, dict):
        raise ReleaseWatchError("expected a PyPI releases object")
    problems: list[str] = []
    errors: list[str] = []
    versions = checked = 0
    for version, entries in sorted(releases.items()):
        if not version.strip() or not isinstance(entries, list):
            errors.append(f"{version!r}: expected a named release with a file list")
            continue
        files = []
        for entry in entries:
            try:
                files.append(_file_digest(entry))
            except ReleaseWatchError as exc:
                errors.append(f"{version}: {exc}")
        if not files:
            continue
        versions += 1
        try:
            sums_body = fetch(SUMS_URL.format(repository=repository, version=version))
            sums = None if sums_body is None else parse_sums(sums_body.decode("utf-8"))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            errors.append(f"{version}: {exc}")
            continue
        if sums is None:
            problems.append(
                f"{version}: PyPI has this version, but GitHub release "
                f"v{version} has no SHA256SUMS"
            )
            continue
        for name, digest in files:
            checked += 1
            recorded = sums.get(name)
            if recorded is None:
                problems.append(
                    f"{version}: {name} is on PyPI, but the release's "
                    "SHA256SUMS does not list it"
                )
            elif recorded != digest:
                problems.append(
                    f"{version}: {name} on PyPI has sha256 {digest}; the "
                    f"release recorded {recorded}"
                )
    if errors:
        raise ReleaseWatchError("\n".join(errors), problems=problems)
    return problems, versions, checked


def main(argv: Optional[Sequence[str]] = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        problems, versions, checked = find_problems(_fetch)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        sys.stderr.write(f"could not check PyPI against the releases: {exc}\n")
        if not isinstance(exc, ReleaseWatchError) or not exc.problems:
            return 2
        problems = exc.problems
    if problems:
        sys.stderr.write(
            "PyPI serves files that the GitHub releases did not record:\n"
        )
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        return 1
    plural = "" if versions == 1 else "s"
    print(
        f"PyPI files match the GitHub releases: {versions} version{plural}, "
        f"{checked} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
