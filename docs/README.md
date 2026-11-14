# Agentflow documentation

New here? Start with the [Quickstart](quickstart.md).

## Start here

- [Quickstart](quickstart.md): an empty repository to a verified proof in
  about two minutes.
- [Agent workflow](agent-workflow.md): the full task loop, phase by phase, and
  the files Agentflow writes.

## Reference

- [Agent artifacts](agent-artifacts.md): what lives under `.agent/` and what to
  commit, attach, or keep local.
- [Command risk screening](command-risk.md): how commands are classified
  before they run, and the policies that flag, hold, or block them.
- [Single-writer leases](single-writer-leases.md): letting only one agent
  write to a plan step at a time.
- [Workflow packs](workflow-packs.md): reusable policy and templates in a
  local data file.
- [Recommend workflow](recommend-workflow.md): map a task brief to a suggested
  level of process.
- [Draft plan](draft-plan.md): turn a task brief and a workflow pack into a
  draft plan.
- [detect-stuck](detect-stuck.md): spot an agent repeating a failing command.
- [Review checklist](review-checklist.md): what to check before calling a task
  complete.
- [Verification matrix](verification-matrix.md): which check fits which kind
  of change.

## Integrations

- [MCP server](mcp.md): expose the workflow to an agent as MCP tools.
- [Stop hook](stop-hook.md): block a session from finishing until the proof
  passes.
- [CI proof gate](ci-proof-gate.md): verify proofs in GitHub Actions and set
  the minimum policy there.
- [Golem integration](golem-integration.md): the contract for the Golem
  adapter, useful for anyone writing their own.

## Trust and compatibility

- [Security model](security-model.md): what Agentflow guarantees and what it
  leaves to you.
- [Stability policy](stability.md): which commands, flags, and outputs are
  stable within a major version, and platform support.
- [Artifact compatibility](compatibility.md): which older proofs and files a
  newer version can read.
- [Schema-freeze audit](schema-freeze-audit.md): the record behind the 1.0
  file-format freeze.

## Project

- [Roadmap](roadmap.md): what shipped and what comes next.
- [Packaging and releases](packaging.md): the published artifacts, checksums,
  and the release checklist.
- [PyPI publication packet](pypi-publishing.md): maintainer record for PyPI
  publishing.
- [Changelog](../CHANGELOG.md): every release.
- [Examples](../examples/): copyable CI, MCP client, workflow pack, and
  aggregation setups.
- [Branding](branding/README.md): logo files and usage, with a visual
  [brand guide](branding/brand-guide.html).
