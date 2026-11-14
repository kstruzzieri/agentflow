<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/kstruzzieri/agentflow/main/docs/branding/agentflow-lockup-dark.svg">
    <img alt="agentflow" src="https://raw.githubusercontent.com/kstruzzieri/agentflow/main/docs/branding/agentflow-lockup-light.svg" width="440">
  </picture>
</p>

<h2 align="center">Gate the flow. Prove the work.</h2>

<p align="center">
  <img alt="agentflow — proof" src="https://raw.githubusercontent.com/kstruzzieri/agentflow/main/docs/branding/agentflow-badge.svg" height="34">
</p>

<p align="center">
  <a href="https://github.com/kstruzzieri/agentflow/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kstruzzieri/agentflow/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/agentflow-proof/"><img alt="PyPI" src="https://img.shields.io/pypi/v/agentflow-proof.svg"></a>
  <a href="https://github.com/kstruzzieri/agentflow/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
</p>

# Agentflow

An AI coding agent says the work is done. Agentflow makes it show the receipts.

Agentflow is a small command-line tool that runs beside any coding agent, such
as Claude Code, Codex, or a local model. The agent works from a locked plan,
runs its checks through Agentflow, and records each edit against a step of that
plan. Agentflow gathers those receipts into a proof pack: plain files under
`.agent/` that you, a reviewer, or CI can check without reading the chat.

It is for developers and teams who want reviewable evidence of what an agent
changed, not its word for it.

- **Runs locally.** Python 3.11+ standard library only; no service, no account.
- **Any agent that can run shell commands.** It is a CLI and an MCP server, not a framework.
- **Plain local files.** Plans, receipts, and proofs are JSON and Markdown you can read and diff.

## Install

```bash
python3 -m pip install agentflow-proof
```

The package is `agentflow-proof` on PyPI (plain `agentflow` there is an
unrelated project). It installs two commands, `agentflow` and `agentflow-mcp`.
If pip reports an externally managed environment, use
`pipx install agentflow-proof` or a virtual environment.
Linux and macOS are supported; Windows is best effort
([platform support][platform]).

**Next: run the [Quickstart][quickstart]**, a first proof in an empty
repository in about two minutes.

Single-file builds with checksums are on the [Releases][releases] page, covered
in [Packaging][packaging]. To work from a source checkout, see
[CONTRIBUTING][contributing].

## How a run works

The agent usually types these, not you. This is the lifecycle of a one-step
plan, with placeholders; the Quickstart has the runnable version.

```bash
agentflow init                            # create .agent/ with a blank plan
# fill in the plan: objective, scope, allowed files, steps, checks
agentflow lock-plan .agent/plan.lock.json # validate and lock it
agentflow init-execution                  # open the run's ledgers
agentflow claim-step P1 --agent "$USER"   # take a step
# ...the agent edits files here...
agentflow run --step P1 --gate "<check>" -- <command>   # run a check, keep the receipt
agentflow record-file-change --step P1 --path <file>    # tie an edit to the step
agentflow finish-step P1                  # verify the step, then complete it
agentflow finish-run                      # audit, verify, build the proof, verify the proof
```

A successful `finish-run` prints:

```text
audit-drift ok
verify-run ok
build-proof ok
verify-proof ok
```

`verify-run` and `audit-drift` compare your uncommitted changes with the last
commit, so run them before you commit. A changed file with no receipt fails
`verify-run`. An unrecorded edit inside a recorded file, or a file outside the
plan's scope, fails `audit-drift`.

Anyone with the proof files can check them again later with
`agentflow verify-proof`, which compares the ledgers and receipts with the
checksums the proof recorded. The proof stays local by default;
[Agent artifacts][artifacts] covers sharing it with reviewers and CI. The
[Agent workflow][workflow] guide explains each phase.

## Use it with your agent

- **Instructions.** Tell the agent to follow the [task loop][workflow]. This
  repository's own [AGENTS.md][agents] is a working example.
- **MCP server.** `agentflow-mcp` serves the workflow as tools over stdio or
  loopback HTTP. Point your MCP client at it and the agent sees the tools. It
  does not expose `run` or `record-command`; [MCP server][mcp] sets out the
  security boundary.
- **Stop hook.** Once wired into your agent runtime, it blocks a session from
  finishing until the proof passes. See [Stop hook][stop-hook].
- **CI gate.** Verify the proof in GitHub Actions and let CI, not the agent,
  set the minimum policy. See [CI proof gate][ci-gate].

## What it covers

- [Locked plans and amendments][workflow]: scope, allowed files, and steps are
  set up front, and `amend-plan` records a change to them.
- [Receipts down to the diff hunk][workflow]: each edit in an allowed file must
  trace to a step.
- [Command risk screening][command-risk]: commands like `rm -rf` or `curl | sh`
  are flagged, held for confirmation, or blocked before they run.
- [Proof packs][artifacts] with a static HTML report (`agentflow view-proof --html`).
- [Single-writer leases][leases] and [proof aggregation][aggregation] across worktrees.
- [Workflow packs][packs]: reusable policy, workflow recommendations, and draft plans.

Agentflow records and checks; it is not a sandbox, and its proofs are checksum
based tamper evidence, not signatures. The [Security model][security] says
what is guaranteed and what is left to you.

## Documentation

[Quickstart][quickstart] · [Agent workflow][workflow] ·
[Security model][security] · [Stability and compatibility][stability] ·
[Examples][examples] · [All docs][docs]

## Project status

Agentflow is past 1.0. Its commands and file formats follow a written
[stability policy][stability]. See the [Roadmap][roadmap] for what comes next
and the [Changelog][changelog] for each release.

## Contributing, security, and license

Contributions are welcome; start with [CONTRIBUTING][contributing]. Report
vulnerabilities privately as described in [SECURITY.md][security-policy].
Agentflow is released under the [MIT license][license].

[releases]: https://github.com/kstruzzieri/agentflow/releases
[packaging]: https://github.com/kstruzzieri/agentflow/blob/main/docs/packaging.md
[contributing]: https://github.com/kstruzzieri/agentflow/blob/main/CONTRIBUTING.md
[quickstart]: https://github.com/kstruzzieri/agentflow/blob/main/docs/quickstart.md
[workflow]: https://github.com/kstruzzieri/agentflow/blob/main/docs/agent-workflow.md
[agents]: https://github.com/kstruzzieri/agentflow/blob/main/AGENTS.md
[mcp]: https://github.com/kstruzzieri/agentflow/blob/main/docs/mcp.md
[stop-hook]: https://github.com/kstruzzieri/agentflow/blob/main/docs/stop-hook.md
[ci-gate]: https://github.com/kstruzzieri/agentflow/blob/main/docs/ci-proof-gate.md
[command-risk]: https://github.com/kstruzzieri/agentflow/blob/main/docs/command-risk.md
[artifacts]: https://github.com/kstruzzieri/agentflow/blob/main/docs/agent-artifacts.md
[leases]: https://github.com/kstruzzieri/agentflow/blob/main/docs/single-writer-leases.md
[aggregation]: https://github.com/kstruzzieri/agentflow/tree/main/examples/aggregation
[packs]: https://github.com/kstruzzieri/agentflow/blob/main/docs/workflow-packs.md
[security]: https://github.com/kstruzzieri/agentflow/blob/main/docs/security-model.md
[stability]: https://github.com/kstruzzieri/agentflow/blob/main/docs/stability.md
[platform]: https://github.com/kstruzzieri/agentflow/blob/main/docs/stability.md#platform-support
[examples]: https://github.com/kstruzzieri/agentflow/tree/main/examples
[docs]: https://github.com/kstruzzieri/agentflow/blob/main/docs/README.md
[roadmap]: https://github.com/kstruzzieri/agentflow/blob/main/docs/roadmap.md
[changelog]: https://github.com/kstruzzieri/agentflow/blob/main/CHANGELOG.md
[security-policy]: https://github.com/kstruzzieri/agentflow/blob/main/SECURITY.md
[license]: https://github.com/kstruzzieri/agentflow/blob/main/LICENSE
