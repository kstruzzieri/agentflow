#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
root="${1:-$repo/tests/fixtures/proof-bundle}"
case "${PYTHONPATH:-}" in
  "") PYTHONPATH="$repo/src" ;;
  /*) ;;
  *) PYTHONPATH="$repo/$PYTHONPATH" ;;
esac
export PYTHONPATH
# Physical path (-P) so the comparison with git's resolved toplevel holds
# where TMPDIR is a symlink (macOS /var -> /private/var).
root=$(CDPATH= cd -- "$root" && pwd -P)
git_root=$(git -C "$root" rev-parse --show-toplevel 2>/dev/null || true)
if [ "$git_root" != "$root" ]; then
  bundle=$(mktemp -d "${TMPDIR:-/tmp}/agentflow-proof.XXXXXX")
  trap 'rm -rf "$bundle"' EXIT HUP INT TERM
  cp -R "$root/." "$bundle"
  rm -rf "$bundle/.git"
  git -C "$bundle" init -q
  git -C "$bundle" add -A
  git -C "$bundle" -c user.email=example@agentflow.invalid -c user.name=agentflow-example commit -qm baseline
  root=$bundle
fi
cd "$root"
"${PYTHON:-python3}" -m agentflow verify-run --no-record --root .
"${PYTHON:-python3}" -m agentflow verify-proof --root .
"${PYTHON:-python3}" -m agentflow verify-proof --strict --root .
