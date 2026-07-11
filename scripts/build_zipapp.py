#!/usr/bin/env python3
"""Build single-file zipapp artifacts for agentflow.

Stdlib only. Produces dist/agentflow.pyz (CLI) and dist/agentflow-mcp.pyz
(MCP server). The artifacts require a system Python >= 3.11 on PATH; see
docs/packaging.md.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "src" / "agentflow"

TARGETS = {
    "agentflow": "agentflow.cli",
    "agentflow-mcp": "agentflow.mcp_server",
}

# Parses on ancient interpreters so the version guard runs before any
# agentflow import can hit a SyntaxError.
MAIN_TEMPLATE = """\
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "agentflow requires Python 3.11 or newer; found %s.%s\\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    sys.exit(1)

from {module} import main

sys.exit(main())
"""


def build_target(name: str, module: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{name}.pyz"
    with tempfile.TemporaryDirectory() as staging_root:
        staging = Path(staging_root)
        shutil.copytree(
            PACKAGE_DIR,
            staging / "agentflow",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (staging / "__main__.py").write_text(
            MAIN_TEMPLATE.format(module=module), encoding="utf-8"
        )
        zipapp.create_archive(
            staging,
            target=target,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    return target


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build agentflow single-file zipapp artifacts"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist",
        help="directory for the built .pyz files (default: dist/)",
    )
    parser.add_argument(
        "--only",
        choices=sorted(TARGETS),
        help="build a single artifact instead of all of them",
    )
    args = parser.parse_args(argv)
    names = [args.only] if args.only else sorted(TARGETS)
    for name in names:
        artifact = build_target(name, TARGETS[name], args.output_dir)
        print(f"built {artifact} ({artifact.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
