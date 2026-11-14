#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
root="${1:-$repo/tests/fixtures/proof-bundle}"
# Optional: compare with this commit, committed changes included (#139).
base_ref="${AGENTFLOW_BASE_REF:-}"
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
  if [ -n "$base_ref" ]; then
    echo "smoke.sh: AGENTFLOW_BASE_REF needs a proof root at the top of a Git repository;" \
      "$root is checked as a copy with no history" >&2
    exit 2
  fi
  bundle=$(mktemp -d "${TMPDIR:-/tmp}/agentflow-proof.XXXXXX")
  trap 'rm -rf "$bundle"' EXIT HUP INT TERM
  cp -R "$root/." "$bundle"
  rm -rf "$bundle/.git"
  git -C "$bundle" init -q
  git -C "$bundle" add -A
  git -C "$bundle" -c user.email=example@agentflow.invalid -c user.name=agentflow-example commit -qm baseline
  root=$bundle
  # The copy's one commit is its whole history: compare with it, not with a
  # run baseline the copy does not contain.
  base_ref=HEAD
fi
cd "$root"
if [ -n "$base_ref" ]; then
  "${PYTHON:-python3}" -m agentflow verify-run --no-record --root . --base-ref "$base_ref"
else
  "${PYTHON:-python3}" -m agentflow verify-run --no-record --root .
fi
"${PYTHON:-python3}" -m agentflow verify-proof --root .
"${PYTHON:-python3}" -m agentflow verify-proof --strict --root .
