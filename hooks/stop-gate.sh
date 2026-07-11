#!/bin/sh
# Agentflow stop-gate hook — runtime-agnostic completion gate.
#
# Blocks an agent session from finishing until the Agentflow proof chain passes:
#   verify-run  ->  build-proof  ->  verify-proof
#
# Universal contract (interpreted per runtime — see docs/stop-hook.md):
#   exit 0        proof satisfied (or repo ungoverned) -> allow stop
#   exit non-zero proof not satisfied                  -> block / keep working
#
# The script is the *enforcement* twin of the Agentflow MCP server: the MCP
# server gives an agent voluntary access to the tools; this hook makes finishing
# conditional on the proof actually passing. It is POSIX sh, has no third-party
# dependencies (matching Agentflow's stdlib-only invariant), parses no stdin, and
# keeps stdout clean (all human-facing notes go to stderr) so runtimes that read
# stdout JSON on exit 0 see an empty allow.
set -u

# Project root: explicit override > Claude Code's $CLAUDE_PROJECT_DIR > cwd.
if [ -n "${AGENTFLOW_ROOT:-}" ]; then
  ROOT="$AGENTFLOW_ROOT"
elif [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  ROOT="$CLAUDE_PROJECT_DIR"
else
  ROOT="$(pwd)"
fi

# Pass-through when Agentflow is not governing this repo. A locked plan is the
# signal that the task is under Agentflow governance; with no plan there is no
# proof contract to enforce, so the hook must not block unrelated sessions.
if [ ! -f "$ROOT/.agent/plan.lock.json" ]; then
  echo "agentflow stop-gate: no locked plan at $ROOT/.agent/plan.lock.json; nothing to enforce." >&2
  exit 0
fi

# Resolve the agentflow command. $AGENTFLOW_CMD overrides (word-split on purpose
# so "python3 -m agentflow" works); otherwise prefer an installed console script
# and fall back to running the module from a source checkout.
if [ -n "${AGENTFLOW_CMD:-}" ]; then
  AGENTFLOW="$AGENTFLOW_CMD"
elif command -v agentflow >/dev/null 2>&1; then
  AGENTFLOW="agentflow"
else
  AGENTFLOW="python3 -m agentflow"
  if [ -d "$ROOT/src/agentflow" ]; then
    PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
    export PYTHONPATH
  fi
fi

# Preflight: the resolved CLI must actually be runnable. Otherwise a missing or
# broken install would surface as a "verify-run failed" misattribution and trap
# the agent in a loop it cannot fix. Fail closed (block) with a distinct,
# actionable message — an enforcement gate must not silently allow when its
# enforcer is absent.
# shellcheck disable=SC2086  # intentional word-split of $AGENTFLOW
if ! $AGENTFLOW --version >/dev/null 2>&1; then
  echo "agentflow stop-gate: '$AGENTFLOW' is not runnable; install Agentflow or set \$AGENTFLOW_CMD. Blocking." >&2
  exit 1
fi

run_gate() {
  # shellcheck disable=SC2086  # intentional word-split of $AGENTFLOW
  if ! $AGENTFLOW "$@" --root "$ROOT" >&2; then
    echo "agentflow stop-gate: '$1' failed; blocking completion until the Agentflow proof chain passes." >&2
    exit 1
  fi
}

# Run the chain in order, fail-fast. build-proof is part of the chain so the
# common "proof not generated yet" case self-heals here and verify-proof then
# passes — the hook only blocks on genuine "work not done" states. Agentflow
# stdout is redirected to stderr (inside run_gate) to keep the hook's own stdout
# empty. verify-run uses --no-record so repeated stop attempts stay idempotent
# and do not bloat .agent/verification-runs.jsonl; the agent's explicit loop run
# is the one that records.
run_gate verify-run --no-record
run_gate build-proof
run_gate verify-proof

echo "agentflow stop-gate: proof chain verified; safe to stop." >&2
exit 0
