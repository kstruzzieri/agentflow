# Use Agentflow in your own repository

This page turns Agentflow on for Claude Code, Codex CLI or GitHub Copilot in a
repository of your own, using the published package. Each runtime gets two
things:

- the **MCP server** (`agentflow-mcp`), which gives the agent the Agentflow
  tools ([MCP server](mcp.md));
- the **Stop hook** (`agentflow stop-gate`), which keeps the agent working
  until the proof chain passes ([Stop hook](stop-hook.md)).

Copy-ready files for each runtime are in
[`examples/integrations/`](../examples/integrations/).

## Before you start

Install Agentflow so its commands are on every PATH, not only inside one
virtual environment. A runtime starts the MCP server and the hook with its own
PATH, so a venv that is not active when the runtime starts is not seen.

```bash
pipx install agentflow-proof
```

or `uv tool install agentflow-proof`. Then check:

```bash
agentflow --version
```

It must print 1.9.0 or later; older versions have no `stop-gate` command.

The Stop hook only acts in a repository that has a locked Agentflow plan
(`.agent/plan.lock.json`). Anywhere else it lets the agent stop, so the same
config is safe in every repository. If the hook cannot run Agentflow at all, it
blocks while a plan is locked and tells the agent to install or upgrade
`agentflow-proof`.

## Claude Code

Copy the contents of `examples/integrations/claude-code/` into your repository
root:

- `.mcp.json` starts `agentflow-mcp`.
- `.claude/settings.json` runs `agentflow stop-gate` as the `Stop` hook.

If you already have these files, merge the `agentflow` entry and the `Stop`
hook into them. Claude Code asks you to approve the project's MCP server the
first time it starts in the repository.

To check it worked: run `/mcp` and see `agentflow` connected; then, with a
locked plan whose steps are not done, ask the agent to stop and see it continue
with a reason that starts `agentflow stop-gate: 'verify-run' failed`.

## Codex CLI

Copy `examples/integrations/codex/.codex/config.toml` into your repository's
`.codex/` folder, or merge its `[mcp_servers.agentflow]` and `[[hooks.Stop]]`
tables into an existing one.

Codex reads a project's `.codex/` config only once you trust the project. Each
hook must also be trusted: run `/hooks` in Codex and approve the Agentflow
`Stop` hook. Any edit to the hook needs trusting again; until then Codex skips
it.

Codex documents no limit on how many times a Stop hook can send the agent
back. Under Codex, a run that cannot finish keeps going until you press Esc.

## GitHub Copilot (CLI and VS Code)

Copy the contents of `examples/integrations/copilot/` into your repository
root:

- `.mcp.json` starts `agentflow-mcp`. It is the same file as Claude Code's.
- `.github/hooks/agentflow.json` runs `agentflow stop-gate` on `agentStop`,
  with a `bash` line and a `powershell` line.

The hook sets `timeoutSec` to 600. Keep it long: Copilot skips a hook that
times out, which lets the agent stop.

Copilot CLI also reads `.claude/settings.json`. If you copy both the Claude
Code and Copilot folders, the gate runs twice under Copilot CLI. That is
harmless, since the second run gives the same answer, but slower.

## GitHub Copilot cloud agent

The cloud agent works on github.com, so three pieces are set there:

1. Add the MCP server in the repository's **Settings → Copilot → Coding agent**
   MCP configuration:

   ```json
   {"mcpServers": {"agentflow": {"type": "local", "command": "agentflow-mcp", "args": [], "tools": ["*"]}}}
   ```

2. Commit `examples/integrations/copilot/.github/workflows/copilot-setup-steps.yml`
   as `.github/workflows/copilot-setup-steps.yml`. It installs Agentflow before
   the agent starts.
3. Commit `.github/hooks/agentflow.json` from the Copilot folder.

`.agent/` is normally not committed, so the cloud agent starts with no locked
plan and the Stop hook lets it stop. Enforcement begins only once the agent
itself runs `agentflow init`, `lock-plan` and `init-execution`. The hook
enforces finishing a locked plan; nothing forces the agent to lock one, and in
the cloud no one is there to lock it for the agent.

## Golem

Golem needs no file: it finds Agentflow on PATH itself
([go-llm#272](https://github.com/kstruzzieri/go-llm/issues/272)). See
[Golem integration](golem-integration.md).

## Windows

Windows is best effort ([platform support](stability.md#platform-support)).
`agentflow stop-gate` is Python and runs on Windows. The hook lines differ:

- Copilot's hook file has a `powershell` line with the same safety net as the
  `bash` line.
- Codex's hook has `command_windows = "agentflow stop-gate"`, which has no
  safety net: if `agentflow` is not on PATH, the hook fails and Codex lets the
  agent stop.
- Claude Code runs the single `command` line; see the table below for what was
  checked.

## Checked versions

Checked by hand from the 1.9.0 wheel: the runtime lists the Agentflow tools,
the Stop hook blocks an unfinished run, and it allows a finished one.

| Runtime | macOS | Windows 11 |
| --- | --- | --- |
| Claude Code | 2.1.319: tools, block and allow checked | not checked on Windows |
| Codex CLI | not checked yet | not checked on Windows |
| Copilot CLI | 1.0.106: tools, block and allow checked | not checked on Windows |
| VS Code Copilot | not checked yet | not checked on Windows |
| Copilot cloud agent | not checked | not checked |

A runtime marked "MCP tools only; Stop hook not enforcing" lists the tools but
its Stop hook did not hold the agent in the check.

## Troubleshooting

The hook lets the agent stop when it should block:

- **`agentflow` is not on the runtime's PATH.** Install with `pipx` or
  `uv tool`, or start the runtime from a shell where the command works.
- **Codex: the hook is not trusted.** Run `/hooks` and approve it again after
  any edit.
- **A timeout shorter than the gate.** Keep the hook timeout at 600 seconds.
- **No locked plan.** The hook only enforces a plan that is locked in
  `.agent/plan.lock.json` at the repository root or above the working
  directory.
