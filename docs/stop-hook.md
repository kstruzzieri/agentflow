# Agentflow Stop Hook (Enforcement Gate)

The stop hook ([`hooks/stop-gate.sh`](../hooks/stop-gate.sh)) blocks an agent
session from finishing until the Agentflow proof chain passes:

```
verify-run  ->  build-proof  ->  verify-proof
```

It is the **enforcement** twin of the [MCP server](mcp.md). The MCP server gives
an agent *voluntary* access to the Agentflow tools; nothing forces it to use
them before declaring the task done. A Stop hook is the lever that does: most
agent runtimes fire a configurable command when the agent tries to stop, and a
non-zero exit keeps the agent working.

## Universal contract

The script speaks one runtime-neutral contract:

| Exit | Meaning | Runtime should |
| --- | --- | --- |
| `0` | proof satisfied, **or** repo is not under Agentflow governance | allow the agent to stop |
| non-zero | a proof gate failed | block completion / keep the agent working |

It is POSIX `sh`, pulls in **no third-party tools** (matching Agentflow's
stdlib-only invariant — no `jq`, no stdin parsing), and writes all human-facing
notes to **stderr** so stdout stays empty for runtimes that parse stdout JSON.

### Pass-through when ungoverned

The hook is Agentflow-specific. A locked plan (`.agent/plan.lock.json`) is the
signal that a task is under Agentflow governance. **With no locked plan the hook
exits `0` and never invokes the CLI** — so the same script can be installed in
any repo without bricking sessions that do not use Agentflow. Enforcement turns
on the moment a plan is locked.

### Self-healing chain

`build-proof` is part of the chain on purpose: the common "proof not generated
yet" state is fixed *inside the hook run*, and `verify-proof` then passes. The
hook only blocks on genuine "work not done" states — incomplete step coverage
(`verify-run`) or source that changed since the receipts were recorded
(`verify-proof`). Re-running the hook with a satisfied contract is idempotent and
stays green: it calls `verify-run --no-record`, so repeated stop attempts do not
append to `.agent/verification-runs.jsonl` (the agent's own loop run is the one
that records).

### Fail-closed when the CLI is unavailable

Before running the chain, the hook probes `agentflow --version`. If the resolved
command is missing or broken it blocks with a distinct *"not runnable"* message
rather than misreporting it as a proof failure. This is deliberate: an
enforcement gate must not silently allow completion just because its enforcer is
absent. Install Agentflow or set `$AGENTFLOW_CMD` to recover.

## Per-runtime wiring

All deliverables here are **examples**. None auto-activate in this repository;
copy the relevant snippet into your runtime config to turn the gate on.

### Claude Code

Claude Code only treats exit code **2** as a blocking Stop error (any other
non-zero is surfaced as a non-blocking warning). Wrap the script so a failure
maps to exit 2. Add to `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "sh \"$CLAUDE_PROJECT_DIR/hooks/stop-gate.sh\" || exit 2",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

- `Stop` ignores the matcher value; `"*"` is the conventional placeholder.
- `$CLAUDE_PROJECT_DIR` is exported for hooks and also read by the script itself
  to locate the repo root.
- When the gate fails, the agent receives the script's stderr (which names the
  failing gate) and continues. Claude Code has no `stop_hook_active` payload
  flag, so a contract the agent genuinely cannot satisfy will re-prompt it; the
  agent should then record an explicit blocker (`agentflow block-step` /
  `fail-step`) rather than loop. The self-healing `build-proof` step keeps the
  common case from blocking at all.

### OpenHands

OpenHands reads [`.openhands/hooks.json`](../.openhands/hooks.json) (shipped here
as an example). It consumes the universal non-zero-blocks contract directly, so
no exit-code adapter is needed:

```json
{
  "hooks": {
    "stop": [
      {
        "name": "agentflow-proof-gate",
        "command": "sh hooks/stop-gate.sh",
        "blocking": true,
        "description": "Block completion until the Agentflow proof chain passes."
      }
    ]
  }
}
```

Key names vary across OpenHands versions; adapt them as needed. The invariant is
unchanged: **run `sh hooks/stop-gate.sh`, treat non-zero as block.**

### Codex CLI and other runtimes

Codex CLI and similar tools expose pre-completion / pre-commit hooks. Point the
hook at the same script; if the runtime requires a specific blocking exit code,
use the same `|| exit N` adapter shown for Claude Code. For CI or a git
`pre-commit`, invoke it bare — any non-zero already fails the step:

```bash
sh hooks/stop-gate.sh
```

## Configuration

The script needs no configuration in the common case. Environment overrides:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTFLOW_ROOT` | `$CLAUDE_PROJECT_DIR`, else current directory from `pwd` | Repo root to gate |
| `CLAUDE_PROJECT_DIR` | (set by Claude Code) | Fallback repo root |
| `AGENTFLOW_CMD` | `agentflow` if installed, else `python3 -m agentflow` | Override the CLI invocation (word-split, so `"python3 -m agentflow"` works) |

From a source checkout with no installed console script, the hook auto-prepends
`<root>/src` to `PYTHONPATH` and runs `python3 -m agentflow`.

## Testing

[`tests/test_stop_gate.py`](../tests/test_stop_gate.py) injects a fake
`agentflow` via `$AGENTFLOW_CMD` and asserts the hook's orchestration:
pass-through when ungoverned, fail-fast gate ordering, and a clean exit when the
whole chain passes.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p test_stop_gate.py
```
