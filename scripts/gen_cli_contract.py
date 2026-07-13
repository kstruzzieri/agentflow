#!/usr/bin/env python3
"""Regenerate docs/cli-contract.json from the argparse parser.

Run from the repo root after any CLI surface change:

    python3 scripts/gen_cli_contract.py

The stability test (tests/test_stability_policy.py) fails CI whenever the
committed manifest no longer matches the parser; this script is the one
supported way to bring it back in sync.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentflow.cli_contract import build_cli_contract  # noqa: E402


def main() -> int:
    target = ROOT / "docs/cli-contract.json"
    target.write_text(
        json.dumps(build_cli_contract(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
