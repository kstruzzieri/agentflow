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

It is **POSIX-only** `sh` (including on Windows), pulls in **no third-party tools** (matching Agentflow's
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
or a change with no receipt since the run's baseline, committed or not
(`verify-run`), or proof files that no longer match their recorded checksums
(`verify-proof`). Runs started by Agentflow 1.6 or older have no baseline, and
for them `verify-run` sees uncommitted changes only (#139). Re-running the hook with a satisfied contract is idempotent and
stays green: it calls `verify-run --no-record`, so repeated stop attempts do not
append to `.agent/verification-runs.jsonl` (the agent's own loop run is the one
that records).

### Fail-closed when the CLI is unavailable

Before running the chain, the hook probes `agentflow --version`. If the resolved
command is missing or broken it blocks with a distinct *"not runnable"* message
rather than misreporting it as a proof failure. This is deliberate: an
enforcement gate must not silently allow completion just because its enforcer is
absent. Install Agentflow or set `$AGENTFLOW_CMD` to recover.

## Blocked agents

A hook that runs inside the workspace is not a security boundary. A workspace
writer can record a blocker with any reason, rewrite ledgers and checksums, or
delete `.agent/plan.lock.json`, which makes the hook pass through as ungoverned.
Agentflow therefore adds no escape hatch: exit `0` keeps meaning "proof
satisfied (or repo ungoverned)", and a recorded blocker does not satisfy the
gate. That includes a blocked amendment whose recorded edit is still in the
tree: the gate keeps failing until a replacement attempt completes. In
non-strict mode, a blocked amendment that left no edit does not hold the gate.

Operator interruption is the supported way out (Esc in Claude Code). The
operator then completes or re-plans the step, or disables the hook. By default,
Claude Code allows eight consecutive Stop-hook continuations and ends the turn
on the next block. Each tool call resets the counter. Operators can raise the
cap with `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`. See the
[Claude Code Stop hook reference](https://code.claude.com/docs/en/hooks#stop).
GitHub Copilot also caps consecutive continuations at eight. In Codex CLI,
interrupt the session if the run cannot finish.

On a refusal, the shell script writes the failing gate's output and the
`'<gate>' failed` line to stderr. Then `agentflow next-action` prints the
next action and one `recorded blocker:` line per current blocker. The last
line is the fixed operator message:

```
agentflow stop-gate: a recorded blocker does not satisfy this gate. If the work is genuinely blocked, an operator must interrupt the session (see docs/stop-hook.md).
```

Stdout stays empty and the exit code is unchanged, even if `next-action` itself
fails.

Retry, time or cost budgets and operator-approved pauses belong in the host (for
example Golem), where approval and budget state live outside the agent's write
access.

## Installed package: `agentflow stop-gate`

The script above lives in this repository and is not part of the published
package. After `pip install agentflow-proof`, point the runtime's Stop hook at
the `agentflow stop-gate` command instead. It runs the same chain, in process,
and answers in the one format Claude Code, Codex CLI and GitHub Copilot all read
from a Stop hook:

| Outcome | Exit | Stdout |
| --- | --- | --- |
| proof satisfied, or repo not under Agentflow governance | `0` | `{}` |
| root lookup error or failed proof gate | `0` | `{"decision": "block", "reason": "..."}` |

- **Root:** `--root PATH`, else `$AGENTFLOW_ROOT`, else `$CLAUDE_PROJECT_DIR`,
  else the current directory. Only the current directory is searched upward
  for the nearest `.agent/plan.lock.json`, because Codex and Copilot run hooks
  in the session's working directory, which may be a subdirectory. The search
  stops at the repository root (the first directory holding `.git`), so a
  repository nested inside a governed one is never held by the outer plan.
  A permission or I/O error while checking the plan or repository boundary
  blocks completion and puts the error in the reason.
- **Reason:** the `'<gate>' failed` line, at most 20 lines of that gate's
  output, the `next-action` output with any recorded blockers, and the fixed
  operator line last. The runtime gives it to the agent as its next prompt.
- **Always exit 0.** A non-zero exit is not a block in every runtime (Copilot
  treats it as a warning and lets the agent stop), so the answer is carried by
  stdout alone. An internal error while governed is reported as a block.
- **Stdout holds one JSON answer.** On failure, the command captures the
  gate's Python output and includes its diagnostics in the JSON reason.
  Hook status messages and output written directly to file descriptor 1 by
  a gate or its subprocess go to stderr.
- **stdin** (the runtime's hook payload) is not read, as the script never
  read it. `stop_hook_active` does not change the answer; see
  [Blocked agents](#blocked-agents).

`hooks/stop-gate.sh` stays for source checkouts, CI and git hooks, and
`agentflow finish-run` remains the exit-status gate for CI.

## Per-runtime wiring

All deliverables here are **examples**. None auto-activate in this repository;
copy the relevant snippet into your runtime config to turn the gate on.

### Claude Code

With an installed package, the hook runs `agentflow stop-gate`. Do not use the
bare command as the whole hook line: if `agentflow` is not on the hook's PATH
(for example a virtual environment that is not active when Claude Code
starts), the shell exits 127, which Claude Code treats as non-blocking, and the
agent stops. The hook line must fall back to a block while a plan is locked;
the copy-ready configs for each runtime do this
([#156](https://github.com/kstruzzieri/agentflow/issues/156)).

In a source checkout of this repository, use the script. Claude Code only
treats exit code **2** as a blocking Stop error (any other non-zero is surfaced
as a non-blocking warning). Wrap the script so a failure maps to exit 2. Add to
`.claude/settings.json`:

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
  failing gate) and continues. Claude Code also sends `stop_hook_active: true`
  on stdin when it is already continuing because of a stop hook, but this
  script parses no stdin, and that flag alone must not bypass the gate: a
  repeated stop attempt is not evidence that the work is blocked. So a contract
  the agent genuinely cannot satisfy re-prompts it on every stop attempt.
- Recording a blocker on a step that has never completed
  (`agentflow block-step` / `fail-step`) does not satisfy the gate: `verify-run`
  fails while any step is open or has never completed, or when its latest
  attempt failed. A genuinely blocked agent should record the blocker, so the
  ledger says why, and name it in its reply. The operator then interrupts the
  session (Esc in Claude Code) and either completes or re-plans the step, or
  disables the hook. See [Blocked agents](#blocked-agents).
- The self-healing `build-proof` step keeps the common case from blocking at all.

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
pass-through when ungoverned, fail-fast gate ordering, next-action and operator
guidance on refusal, and a clean exit when the whole chain passes.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p test_stop_gate.py
```
