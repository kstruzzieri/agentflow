#!/usr/bin/env python3
"""Validate the PR_TITLE environment variable against the commit convention."""

import os
import re
import sys


TITLE_RE = re.compile(
    r"(?:feat|fix|docs|refactor|test|chore|ci|build|perf|revert)"
    r"(?:\([^\s()]+\))?!?: \S[^\r\n]*"
)


def main() -> int:
    if not TITLE_RE.fullmatch(os.environ.get("PR_TITLE", "")):
        print(
            "PR title must use type(scope): description (scope is optional; "
            "use ! before : for breaking changes). Allowed types: "
            "feat, fix, docs, refactor, test, chore, ci, build, perf, revert.",
            file=sys.stderr,
        )
        return 1
    print("PR title follows the commit convention")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
