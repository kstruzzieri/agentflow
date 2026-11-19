#!/usr/bin/env python3
"""Compare every file PyPI serves for agentflow-proof with the GitHub releases.

For each version on PyPI, the SHA-256 of each file must equal the entry for
that filename in the ``SHA256SUMS`` asset of the GitHub release tagged
``v<version>``. A version with no such release, a file ``SHA256SUMS`` does not
list, or a different hash is reported.

The GitHub release is the reference. This finds a file that reached PyPI some
other way than the release workflow; it cannot find a compromised repository.

Exit status: 0 everything matches, 1 a problem was found, 2 the check could
not run (network, unreadable data).
"""

from __future__ import annotations

import argparse
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


def find_problems(
    fetch: Fetch = _fetch,
    project: str = PROJECT,
    repository: str = REPOSITORY,
) -> tuple[list[str], int, int]:
    """Return ``(problems, versions checked, files checked)``."""
    body = fetch(PYPI_URL.format(project=project))
    if body is None:
        raise ReleaseWatchError(f"PyPI has no project named {project}")
    releases = json.loads(body)["releases"]
    problems: list[str] = []
    versions = checked = 0
    for version in sorted(releases):
        files = releases[version]
        if not files:
            continue
        versions += 1
        sums_body = fetch(SUMS_URL.format(repository=repository, version=version))
        if sums_body is None:
            problems.append(
                f"{version}: PyPI has this version, but GitHub release "
                f"v{version} has no SHA256SUMS"
            )
            continue
        sums = parse_sums(sums_body.decode("utf-8"))
        for entry in files:
            name, digest = entry["filename"], entry["digests"]["sha256"]
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
    return problems, versions, checked


def main(argv: Optional[Sequence[str]] = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        problems, versions, checked = find_problems(_fetch)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"could not check PyPI against the releases: {exc}\n")
        return 2
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
