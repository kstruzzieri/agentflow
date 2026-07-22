<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/branding/agentflow-lockup-dark.svg">
    <img alt="agentflow" src="docs/branding/agentflow-lockup-light.svg" width="440">
  </picture>
</p>

<h2 align="center">Gate the flow. Prove the work.</h2>

<p align="center">
  <img alt="agentflow — proof" src="docs/branding/agentflow-badge.svg" height="34">
</p>

<p align="center">
  <a href="https://github.com/kstruzzieri/agentflow/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kstruzzieri/agentflow/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
</p>

# Agentflow

Agentflow is a lightweight, model-agnostic workflow runner that makes agentic software work auditable. It records the plan,
execution steps, validations, reviews, file-change evidence, drift checks, and final proof as local files, so a run can be
inspected, resumed, and verified outside any LLM chat.

The current implementation is intentionally lightweight:

- no runtime dependencies
- local file artifacts under `.agent/`
- JSON schemas and templates for the workflow and execution contracts
- a Python CLI for planning, runtime checks, execution receipts, drift audit,
  proof-pack generation, and verification

The active task `.agent/` directory is local-only by default. See
[docs/agent-artifacts.md](docs/agent-artifacts.md) for when to keep it ignored,
upload proof as CI artifacts, attach proof to PRs, or commit a deliberate proof
bundle.
Supported interfaces and compatibility promises are defined in
[docs/stability.md](docs/stability.md).

## Quick Start

Agentflow is not yet published as a PyPI package. The provisional Python
distribution agentflow-proof is not yet published; it is built and validated.
It keeps the `agentflow` imports and the `agentflow` and `agentflow-mcp`
commands. From `v0.4.0` on, prebuilt single-file artifacts (`agentflow.pyz`,
`agentflow-mcp.pyz`) are published on the
[GitHub Releases](https://github.com/kstruzzieri/agentflow/releases) page; for
development you install from this source repository.

**Prerequisite:** Python 3.11 or newer (see `requires-python` in
`pyproject.toml`). Agentflow has no runtime dependencies — only the standard
library.

### Platform support

Linux and macOS are supported and tested in release-blocking CI on Python 3.11,
3.12, and 3.13. Windows is **best effort**: the Python CLI may work, but Windows
has no release-blocking CI leg and no compatibility guarantee until this support
tier changes. The POSIX `sh` stop hook (`hooks/stop-gate.sh`) and shell-dependent
examples are POSIX-only; use a POSIX shell on Windows or do not use those parts.
A future supported Windows tier requires Windows CI plus adapted or replaced
POSIX-only hooks and tests.

### Install from source

Use an editable install when you want command-line access to the local checkout.
Replace `/path/to/agentflow` with this repository's absolute path.
The `uv tool` and `pipx` options make `agentflow` available outside the repo
when their bin directory is on `PATH`. The `pip` option makes it available while
that Python environment is active.

```bash
uv tool install --editable /path/to/agentflow
```

```bash
pipx install --editable /path/to/agentflow
```

For a virtual environment or the current Python environment:

```bash
python3 -m pip install -e .
```

To test the locally built provisional wheel without publishing it:

```bash
python3 -m pip install dist/agentflow_proof-*.whl
```

If the permanent PyPI name becomes available later, only `project.name` changes;
imports and the `agentflow` and `agentflow-mcp` command names stay the same.

### Smoke tests

After any editable install that exposes the console script:

```bash
agentflow --version
agentflow --help
```

For no-install usage from the repository root:

```bash
PYTHONPATH=src python3 -m agentflow --version
PYTHONPATH=src python3 -m agentflow --help
```

For no-install usage from another directory:

```bash
PYTHONPATH=/path/to/agentflow/src python3 -m agentflow --version
PYTHONPATH=/path/to/agentflow/src python3 -m agentflow --help
```

### Single-file build (zipapp)

Build self-contained single-file artifacts (requires only a system
Python 3.11+ to run):

```bash
python3 scripts/build_zipapp.py
./dist/agentflow.pyz --version
python3 dist/agentflow-mcp.pyz --transport stdio
```

To use a published build instead of building locally, download `agentflow.pyz`,
`agentflow-mcp.pyz`, and `SHA256SUMS` from the
[Releases](https://github.com/kstruzzieri/agentflow/releases) page, verify the
checksums, and run:

```bash
sha256sum -c SHA256SUMS
python3 agentflow.pyz --version
```

See `docs/packaging.md` for details, limitations, and the release checklist.

### Basic workflow

After installing Agentflow, initialize a repository and inspect the generated
plan before locking or executing it:

```bash
agentflow init
agentflow status
agentflow validate-plan .agent/plan.lock.json
agentflow audit-drift
agentflow runtime-status --json
agentflow build-proof --strict
agentflow verify-proof
```

For no-install usage, replace `agentflow` with
`PYTHONPATH=src python3 -m agentflow` from the repository root.

### Agent task loop

Codex, Claude Code, and other agents should use the same Agentflow task loop:

```bash
agentflow init
# Populate .agent/plan.lock.json with the required plan contract:
# objective, scope, invariants, allowed_files, validation_gates, rollback_plan,
# and real steps. Include task files and .agent/ in allowed_files; then lock it.
agentflow lock-plan .agent/plan.lock.json
agentflow init-execution
STEP_ID=P1
VALIDATION_GATE="<matching step.validation entry>"
agentflow claim-step "$STEP_ID" --agent "$USER"
agentflow run --step "$STEP_ID" --gate "$VALIDATION_GATE" -- <validation-or-work-command>
agentflow record-file-change --step "$STEP_ID" --path <changed-path>
agentflow verify-step "$STEP_ID"
agentflow complete-step "$STEP_ID"
agentflow verify-run
agentflow audit-drift
agentflow build-proof
agentflow verify-proof
```

The loop keeps each task tied to a plan step. `init` creates the planning
artifacts. Populate the full `.agent/plan.lock.json` contract before claiming
work; `init` starts with placeholder fields and an empty `steps` list.
`STEP_ID=P1` assumes the locked plan contains a step with id `P1`.
`VALIDATION_GATE` should match the step's legacy `validation` entry unless the
step uses structured command gates. Include `.agent/` in `allowed_files` when
the task uses Agentflow artifacts in the same worktree; `audit-drift` compares
all changed files from git status. `init-execution` creates execution ledgers,
`claim-step` assigns a step attempt, `run` records command receipts,
`record-file-change` maps edits to the step, `verify-step` and `complete-step`
close the step, and
`verify-run`, `audit-drift`, `build-proof`, and `verify-proof` prove the whole
run. Run `audit-drift` before `build-proof` so `.agent/proof-pack.md` includes
the current drift result from `.agent/drift-report.json`. To attach a follow-up
edit to an already-completed step (for example a review-feedback fix), use
`amend-step` — it opens an auditable new attempt linked to the prior completed
attempt rather than silently reusing it.

Adapter-backed workflows can provide the same plan contract as structured JSON
without hand-editing `.agent/plan.lock.json`:

```bash
agentflow lock-plan --stdin --json < plan.json
agentflow lock-plan --from-json plan.json --json
```

Both commands validate the supplied JSON through the normal plan validator, set
`locked` and `locked_at`, and write the canonical artifact to
`.agent/plan.lock.json` by default. Invalid input exits nonzero and emits a
machine-readable payload with `status: "invalid"` and stable diagnostic entries
under `errors`. The existing file-based form remains unchanged:
`agentflow lock-plan .agent/plan.lock.json` locks that file in place.

Adaptive workflows can also record the selected workflow policy as a durable
contract:

```bash
agentflow workflow-contract --from-json workflow-contract.json
agentflow workflow-contract --validate
```

This writes `.agent/workflow.contract.json` after stdlib validation. The
artifact records the selected workflow pack/profile, rationale, required
capabilities, review depth, validation policy, and proof policy. When present,
`build-proof` includes a concise workflow summary and hashes the contract, so
`verify-proof` detects stale or tampered workflow metadata.

The adaptive workflow track can go from a task brief to a draft plan
deterministically. `recommend-workflow` maps a machine-authored task brief to a
workflow posture, and `draft-plan` compiles that brief plus a workflow pack into
an unlocked, `validate-plan`-valid `.agent/plan.lock.json` draft:

```bash
agentflow recommend-workflow --brief examples/briefs/small-bugfix.brief.json
agentflow draft-plan --brief examples/briefs/medium-feature.brief.json \
  --workflow examples/packs/agentflow-draft-demo \
  --objective "Add an events projection cache" --write
```

`draft-plan` treats the recommendation as a lower-bound posture, selects the
least-strict pack profile that satisfies it, and hydrates that profile's plan
template with the brief (objective, unioned gates, `allowed_files` including
`.agent/`, stricter risk). It never locks a plan and fails closed on vague
briefs, missing candidate files, or unsatisfiable postures. See
[`docs/draft-plan.md`](docs/draft-plan.md) and
[`docs/recommend-workflow.md`](docs/recommend-workflow.md).

Agentflow v0.2 adds optional runtime metadata, runtime snapshots, structured
proof metadata at `.agent/proof-pack.json`, and `verify-proof` hash checks.
Runtime status is safe by default: configured commands are not spawned unless
`runtime-status --probe` is used.

`agentflow view-proof --html` renders the proof pack and execution ledgers
into a self-contained static HTML report (default
`.agent/proof-report.html`) for human review and sharing: no JavaScript, no
external references, all recorded content escaped, with links to receipt
stdout/stderr files where available. The report is a review aid only;
`verify-proof` remains the authoritative check.

## Agentflow v0.3 Portable Runtime Contract

Agentflow v0.3 adds an optional execution layer under `.agent/`:

```bash
agentflow init-execution
agentflow doctor
agentflow next-step --json
STEP_ID=P1
VALIDATION_GATE="python3 -m unittest discover -s tests"
agentflow claim-step "$STEP_ID" --agent "$USER"
agentflow run --step "$STEP_ID" --gate "$VALIDATION_GATE" -- python3 -m unittest discover -s tests
agentflow record-file-change --step "$STEP_ID" --path src/agentflow/example.py
agentflow verify-step "$STEP_ID"
agentflow complete-step "$STEP_ID"
agentflow verify-run
agentflow audit-drift
agentflow build-proof
agentflow verify-proof
```

Execution ledgers are additive. Existing v0.2 proof artifacts remain valid when
no execution contract exists.

## Agent Artifact Policy

Root `.agent/` is task-local by default. Commit or publish it only when the
workflow explicitly calls for a reviewed proof bundle. The policy in
[docs/agent-artifacts.md](docs/agent-artifacts.md) explains:

- local-only, CI-uploaded, PR-attached, and committed proof workflows
- which `.agent/` files are useful proof versus sensitive command output
- when plans should include `.agent/` in `allowed_files`
- how adapter-backed workflows such as Golem should retain and publish proof

## MCP Server

Agentflow ships a dependency-free MCP server that exposes the core workflow
commands as MCP tools, so any MCP-capable front-end discovers them
automatically. It speaks JSON-RPC 2.0 over stdio (Claude Code, Codex CLI) and
Streamable HTTP (local LLMs such as Ollama/Open WebUI and llama.cpp). The server
is a thin wrapper that runs `agentflow.cli.main` in-process — no business logic
is duplicated and no new runtime dependency is added.

```bash
# stdio (Claude Code reads .mcp.json, Codex reads .codex/config.toml)
PYTHONPATH=src python3 -m agentflow.mcp_server

# Streamable HTTP for local LLM front-ends
PYTHONPATH=src python3 -m agentflow.mcp_server --transport http --port 8765
```

Exposed tools: `status`, `doctor`, `next_step`, `next_action`, `claim_step`,
`amend_step`, `reclaim_step`, `renew_lease`, `record_review`, `complete_step`,
`verify_step`, `finish_step`, `verify_run`, `finish_run`, `audit_drift`,
`build_proof`, `verify_proof`. The arbitrary-command tools (`run`,
`record-command`) are intentionally not exposed.
See [docs/mcp.md](docs/mcp.md) for transports, configs, and the security
boundary.

## Stop Hook (Enforcement Gate)

Where the MCP server gives an agent *voluntary* access to the tools, the stop
hook (`hooks/stop-gate.sh`) is the *enforcement* twin: it blocks a session from
finishing until the proof chain passes (`verify-run` -> `build-proof` ->
`verify-proof`). It is runtime-agnostic — a POSIX `sh` script with no
third-party dependencies that signals through a universal exit-code contract
(`0` = proof satisfied or repo ungoverned, non-zero = block).

```bash
# Bare invocation (CI, git pre-commit, OpenHands, Codex):
sh hooks/stop-gate.sh
```

The hook is Agentflow-specific: with no locked `.agent/plan.lock.json` it exits
`0` and never runs the CLI, so it is safe to install in any repo. Example
runtime configs ship for Claude Code (`.claude/settings.json`, with a
`|| exit 2` adapter since Claude Code blocks only on exit 2) and OpenHands
([`.openhands/hooks.json`](.openhands/hooks.json)). None auto-activate — copy the
snippet to turn the gate on. See [docs/stop-hook.md](docs/stop-hook.md) for
per-runtime wiring, idempotency, and the loop-safety notes.

## Command Risk Screening

Before `agentflow run` executes a command, a deterministic, no-network, no-LLM
analyzer classifies it (`low`/`medium`/`high`) and records the result on the
command receipt. A `command_policy.risk_policy` of `warn`, `require-confirmation`
(the new-contract default), or `block` decides whether a high-risk command —
`rm -rf`, `chmod 777`, credential reads, `sudo`, `curl | sh`, writes outside
scope, blocked-path references — is blocked before it runs. This is deterministic
risk *screening*, not a security sandbox. See
[docs/command-risk.md](docs/command-risk.md) for the category catalog, the
`--confirm-risk` / `AGENTFLOW_CONFIRM_RISK` override, and detection limits.

## Hunk-Level Attribution

When `record-file-change` records an edit, Agentflow captures a per-hunk
fingerprint of the diff against `HEAD` (sha256 of the changed lines, with
span-independent identity so unrelated line-number shifts do not invalidate it).
`audit-drift` and `verify-run` then flag any current diff hunk inside an allowed
file that matches no recorded receipt hunk as an `unmapped_hunk`. This
distinguishes the edits a step actually attested from stray or unrelated edits
in the same allowed file. The `proof_policy.hunk_attribution` knob
(`enforce` / `observe` / `off`) governs severity; the default is `enforce` when
an execution contract exists and `off` otherwise. Receipts store only hashes,
spans, and counts — never raw changed-line text. The workflow contract and
receipt behavior are documented in
[docs/agent-workflow.md](docs/agent-workflow.md).

## Single-Writer Leases

The execution contract can enforce single-writer ownership of a plan step so
that only the agent holding a live lease may write to or close an attempt. It is
opt-in via `concurrency.lease_policy` (`advisory` by default, so existing plans
are unchanged); set it to `enforce` to gate claims, writes, and lifecycle
transitions on agent identity and a wall-clock lease. `reclaim-step` recovers a
crashed owner after its lease expires, `renew-lease` extends an owner's own
lease, and `fail-step` is the break-glass. Pass `--agent` on write and lifecycle
commands (or set `AGENTFLOW_AGENT_ID`). `multi_writer` remains rejected;
cross-worktree aggregation now ships separately via `aggregate-ledgers`
([#30](https://github.com/kstruzzieri/agentflow/issues/30)), which merges
one-writer-per-worktree runs into a single canonical proof. See
[docs/single-writer-leases.md](docs/single-writer-leases.md) for the contract
fields, the full enforce loop, and the migration note.

## GitHub Actions Proof Gate

This repository includes a dependency-free GitHub Actions gate in
`.github/workflows/ci.yml`. The workflow runs the unit test suite from the source
checkout, stages a configured proof root into a temporary git repository, then
runs:

```bash
PYTHONPATH=src python3 -m agentflow verify-run --no-record --root "$PROOF_ROOT"
PYTHONPATH=src python3 -m agentflow verify-proof --root "$PROOF_ROOT"
```

`verify-run --no-record` is for CI and other read-only checks: it validates the
execution ledger without appending a new entry to `.agent/verification-runs.jsonl`,
so `verify-proof` can still validate the committed proof hashes afterward.

The workflow uses `AGENTFLOW_PROOF_ROOT` to select the committed proof bundle.
This repository points it at `tests/fixtures/proof-bundle` so CI can regression
test the verifier itself without committing task-local root `.agent/` artifacts.
Repositories that commit the active task proof bundle should set
`AGENTFLOW_PROOF_ROOT` to `.`. On pull requests, the workflow uses the PR base
commit as the temporary git baseline before overlaying the current proof root, so
`verify-run` still sees source edits that lack file receipts. Set
`AGENTFLOW_PROOF_BASE_REF` explicitly when the baseline should be a different
commit:

```yaml
env:
  AGENTFLOW_PROOF_ROOT: .
  AGENTFLOW_PROOF_BASE_REF: ${{ github.event.pull_request.base.sha }}
```

Expected failure behavior:

- missing `.agent/proof-pack.json`: CI fails before verification starts
- stale proof metadata, hash mismatches, or modified receipt output files:
  `verify-proof` exits non-zero
- incomplete execution ledgers, drift failures, or unmapped changed files:
  `verify-run` exits non-zero

## Project Status

Agentflow is at version `0.4.0`. Prebuilt single-file
artifacts (`agentflow.pyz`, `agentflow-mcp.pyz`, `SHA256SUMS`) are published on
the [Releases](https://github.com/kstruzzieri/agentflow/releases) page. The
implemented surface includes:

- v0.1 local planning, evidence, drift, and proof artifacts.
- v0.2 runtime metadata, runtime snapshots, structured proof metadata, and
  reproducible proof verification.
- v0.3 portable execution contracts, step claims, command and file receipts,
  step/run verification, replay support, provider-neutral handoffs, and
  execution summaries in proof packs.
- Increments on the v0.3 line: command-risk screening, a stdlib-only MCP server,
  a Stop-hook enforcement gate, a CI proof gate with a strict review gate, a
  deterministic `review-manifest` producer, and hunk-level attribution (drift
  audit distinguishes receipt-covered edits from unrelated edits inside the same
  allowed file), plus durable adaptive workflow contract artifacts.
- Concurrency and interop: single-writer lease enforcement, cross-worktree
  ledger aggregation via `aggregate-ledgers`, read-only MCP/runtime status
  evidence in proofs, a static `view-proof --html` report, and a Golem adapter
  integration guide.
- Distribution: a stdlib-only `zipapp` single-file build with a tag-triggered
  release workflow (packaging phase 1).

The next priorities are tracked in [docs/roadmap.md](docs/roadmap.md).

## Documentation

- [Agent workflow](docs/agent-workflow.md) — plan, execute, verify, and prove a run
- [Artifact policy](docs/agent-artifacts.md) — decide what stays local and what is safe to publish
- [Security model](docs/security-model.md) — trust boundaries, guarantees, and operator responsibilities
- [Command risk screening](docs/command-risk.md) — deterministic command classification and limits
- [MCP server](docs/mcp.md) — stdio and local HTTP transports
- [Workflow packs](docs/workflow-packs.md) — reusable workflow policy and planning profiles
- [Executable examples](examples/) — CI proof, MCP, workflow-pack, and aggregation walkthroughs
- [Packaging and releases](docs/packaging.md) — build and verify single-file artifacts
- [Roadmap](docs/roadmap.md) — current status and near-term priorities

## Contributing and Security

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the local
development and pull-request workflow. Please report vulnerabilities privately
as described in [SECURITY.md](SECURITY.md).

## Branding

The agentflow brand kit lives in [`docs/branding/`](docs/branding/); open
[`brand-guide.html`](docs/branding/brand-guide.html) for the full guide.

The mark is a **gate / ledger**: plan-step rails passing through a validation
gate — pending (gray) in, verified (teal) out — reusing the vertical bar in the
`agent│flow` wordmark. Teal `#00D4AA` is the primary accent; mint `#86EFAC` is
reserved for `PASS`/success states.

| asset | file |
|-------|------|
| primary mark | [`agentflow-mark.svg`](docs/branding/agentflow-mark.svg) |
| favicon (≤16px) | [`favicon.svg`](docs/branding/favicon.svg) |
| lockups | [`dark`](docs/branding/agentflow-lockup-dark.svg) · [`light`](docs/branding/agentflow-lockup-light.svg) |
| status badge | [`agentflow-badge.svg`](docs/branding/agentflow-badge.svg) |
| social card | [`agentflow-social.svg`](docs/branding/agentflow-social.svg) |

## Core Rule

```text
No fact without evidence.
No edit without a plan step.
No plan change without an amendment.
No completion without verification.
No context unless it pays rent.
```

## License

Agentflow is available under the [MIT License](LICENSE).
