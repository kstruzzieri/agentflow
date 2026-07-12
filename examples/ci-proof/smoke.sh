#!/bin/sh
set -eu

root="${1:-tests/fixtures/proof-bundle}"
"${PYTHON:-python3}" -m agentflow verify-run --no-record --root "$root"
"${PYTHON:-python3}" -m agentflow verify-proof --root "$root"
"${PYTHON:-python3}" -m agentflow verify-proof --strict --root "$root"
