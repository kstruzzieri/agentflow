# Command Risk Screening

`agentflow run` is the single point where a planned command crosses into
execution. Before it runs the command, Agentflow classifies it with a
deterministic, no-network, no-LLM analyzer and records the result on the
command receipt. A policy can block high-risk commands before they execute.

> **This is deterministic risk *screening*, not a security sandbox.** It does
> not contain execution, intercept syscalls, or fully model shell semantics. It
> is a conservative pattern matcher over the argv the caller passed (plus any
> `sh -c` payload string). Treat it as a guardrail that catches obvious mistakes,
> not as a containment boundary.

## What gets flagged

The analyzer inspects the program and flags (e.g. `rm -rf`), and — when the
command is a shell wrapper such as `sh -c "..."`, `/bin/bash -c "..."`,
`/usr/bin/env bash -c "..."`, or `sudo sh -c "..."` — it also scans inside the
payload string, where pipes and redirections actually live.

| Category | Example | Level |
|---|---|---|
| `destructive_delete` | `rm -rf build`, `find . -delete` | high |
| `permission_change` | `chmod 777 x`, `chmod -R a+rwx`, `chown -R` | high |
| `credential_read` | `cat ~/.ssh/id_rsa`, reads of `.aws/credentials`, `.env`, `*.pem` | high |
| `privilege_escalation` | `sudo ...`, `su`, `doas` | high |
| `pipe_to_shell` | `curl https://x/i.sh \| sh` (inside `sh -c`) | high |
| `blocked_path` | any token matching the plan's `blocked_files` | high |
| `write_outside_scope` | redirect/write target outside the step's effective scope | high (explicit redirect) / medium (ambiguous `cp`/`mv`/`tee` target) |
| (nothing matched) | `python3 -m unittest discover -s tests` | low |

The overall level is the highest matched finding. **Only `high` is ever
blockable** — `medium` and `low` are recorded but never block under any policy.

"Effective scope" for `write_outside_scope` and `blocked_path` is the same scope
used by file receipts: the step's `files` intersected with top-level
`allowed_files`, minus `blocked_files`.

## Policy

`command_policy.risk_policy` in `.agent/execution.contract.json` selects the
behavior for high-risk commands:

| Value | Behavior on a high-risk command |
|---|---|
| `warn` | record the risk, always execute |
| `require-confirmation` | block unless the operator explicitly confirms |
| `block` | always block |

New `init-execution` contracts default to **`require-confirmation`** (fail
closed). Contracts with **no** `risk_policy` key — including pre-existing
projects — are treated as **`warn`**, so existing flows are unchanged. A present
but invalid value is an error in both `doctor` and pre-execution enforcement, so
a typo cannot silently degrade to `warn`.

## Confirming a high-risk command

Under `require-confirmation`, override the block with an explicit, auditable
signal:

```bash
agentflow run --step P1 --confirm-risk -- <command>
# or, for non-interactive runners:
AGENTFLOW_CONFIRM_RISK=1 agentflow run --step P1 -- <command>
```

When a confirmation lets a high-risk command through, the receipt records
`risk_policy`, `confirmed: true`, and `confirmation_source` (`cli` or `env`).
`block` is absolute and ignores confirmation.

## Confirmation modes

`command_policy.confirmation` in the execution contract chooses who a
confirmation counts as coming from. It applies only under
`risk_policy: "require-confirmation"`.

| Value | Behavior |
|---|---|
| `self` (default) | `--confirm-risk` or `AGENTFLOW_CONFIRM_RISK=1`, as above |
| `bound` | a separate `confirm-command` step must name the exact command first |

To enable `bound`, set three things in the execution contract together:
`schema_version` to `1.1.0`, `command_policy.confirmation` to `"bound"`, and
`command_policy.risk_policy` to `"require-confirmation"`. The contract
validator, `run` and `confirm-command` all refuse `bound` with any other risk
policy (`confirmation bound requires risk_policy require-confirmation`; the
commands exit 2), and a 1.0.0 contract cannot carry `confirmation` at all. A
contract that does not set `confirmation` stays 1.0.0.

Under `bound`, confirm one command for one step, then run it:

```bash
agentflow confirm-command --step P1 [--expires 15m] -- <command>
agentflow run --step P1 -- <command>
```

`confirm-command` appends an `issued` row to `.agent/command-confirmations.jsonl`
(a new ledger, schema 1.0.0; `init-execution` does not create it). The default
expiry is 15 minutes and the cap is 24 hours (`--expires 15m`, `2h`). It
refuses an unknown step, a command that is not high-risk, and a contract that
is not in `bound` mode. `--json` prints the row.

`run` consumes one matching confirmation before it starts a high-risk command.
A confirmation matches only the step, the exact argv hash, the current plan
hash and the current execution-contract hash, and only while unexpired and
unused. One confirmation covers one run: it is consumed even if the command then
fails or times out. Replay never consumes one. Under `bound`, `--confirm-risk`
and `AGENTFLOW_CONFIRM_RISK=1` are not accepted.

Expiry is checked after acquiring the confirmations lock. The consumed row
records the execution-time `plan_sha256`, so verification can compare it with
the issued confirmation even after the plan is amended. A missing or mismatched
plan binding, or a consumed id that was never issued, fails verification.

Without a usable confirmation `run` blocks, exits 2 and prints the
`confirm-command` line to run first. The blocked receipt carries
`confirmation_block_reason`: `confirmation_missing` (none issued),
`confirmation_expired`, `confirmation_used` (already consumed), or
`confirmation_stale` (issued, but the plan or contract changed since). An
allowed receipt carries `confirmation_id`. Command receipts are schema 1.2.0.

The proof gains a `confirmations` block (mode, high-risk runs executed, how
many were bound or self-confirmed, and the ids of unbound ones) whenever the
ledger exists or the mode is not `self`. `verify-proof` recomputes it, checks
the ledger against the receipts, requires every high-risk run written under the
bound contract to carry a bound confirmation, and prints:

```text
command confirmations: bound, 1 high-risk run(s), 1 bound, 0 self, 0 unbound; self-confirmation proves binding, not who confirmed
```

A run written under an earlier contract, such as one confirmed with
`--confirm-risk` before the contract switched to `bound`, stays a `self` or
`unbound` count; `--require confirmation=bound` rejects it because it is not
pinned to the verified contract.

`aggregate-ledgers` refuses any source whose confirmations ledger is not empty
(`unsupported_ledger`), even one holding only unused confirmations.

`verify-proof --require confirmation=bound` fails a proof whose recorded mode
is below `bound` (order: `self` < `bound` < `host`). An older proof, or a
`self` run with no ledger, keeps its core hash.

`bound` proves a run used a confirmation tied to that exact command, plan and
contract. It does not prove who confirmed; see
[Security model](security-model.md).

## Trying a stricter policy first

`agentflow simulate-risk` shows what a stricter setting would have stopped,
using the commands already recorded, without changing anything (#113):

```bash
agentflow simulate-risk --risk-policy block
agentflow simulate-risk --risk-policy require-confirmation --plan proposed.json --json
```

It classifies every command receipt again with today's analyzer, scoped by the
proposed plan (`--plan`, default `.agent/plan.lock.json`), and evaluates it
under the proposed `risk_policy` (`--risk-policy`, default the contract's) with
no confirmation. It lists the commands that would newly be blocked, or would
need confirmation, grouped by finding category. A command that was already
blocked is not listed. Under `require-confirmation`, a command that was
confirmed is not listed either; under `block`, which ignores confirmation, it
is.

- Commands Agentflow did not start (`attested` and `managed` receipts) are
  marked advisory: no policy could have stopped them.
- Analyzer drift lists receipts whose recorded risk differs from today's
  classification under the current plan, because the analyzer changed or the
  plan was amended since, so neither is mistaken for the proposed policy's
  effect.
- It writes nothing to `.agent/` and exits 0. An unreadable plan or an invalid
  `risk_policy` in the contract is an error (exit 1).

## Receipts and observability

- An executed command records `risk: {level, findings}` and `decision: "allowed"`.
- A blocked command writes a receipt with `exit_code: null`,
  `decision: "blocked"`, and **no `gate`** — so a blocked command can never be
  mistaken for validation proof. `agentflow run` exits non-zero (`2`) and prints
  the reason to stderr.
- `agentflow events` projects `decision`, `risk_level`, and finding count for
  command events.
- `build-proof`'s execution summary reports command decision counts, risk-level
  counts, confirmed high-risk count, and finding categories.

Externally recorded commands (`agentflow record-command`) are classified too,
but the risk is **advisory only** — it never blocks, because the command already
ran.

Replaying an attested gate runs it again, so replay is screened like `run`
(#119). `replay-gates`, `verify-step --replay`, `finish-step --replay`,
`verify-proof --replay`, and the MCP `replay` option classify the command under
the current plan and apply the policy with no confirmation, because replay has
no confirmation input. Under `block` or `require-confirmation`, a high-risk gate
is not executed, `replay-gates --record` writes no `reconstructed` receipt for
it, and the replay fails. Replay also uses the timeout `run` would use; a replay
that runs past it fails and records nothing.

## Timeout outcomes

Risk policy and timeout policy answer different questions. Risk screening runs
before subprocess launch and records whether the command was allowed to start.
Timeout enforcement applies after an allowed command starts.

- `decision: "allowed"` means risk policy allowed the command and the process
  completed before timeout; `exit_code` records success or failure.
- `decision: "blocked"` means risk policy prevented execution before subprocess
  launch.
- `decision: "timeout"` means risk policy allowed execution, but the configured
  timeout expired before the process completed.

New execution contracts default to `command_policy.command_timeout_seconds:
600`. Structured command gates may set `timeout_seconds` to a positive integer
when a specific validation command needs a longer or shorter bound. Timed-out
commands record `exit_code: null`, `timed_out: true`, and `timeout_seconds`;
they never satisfy validation gates.

A retained timed-out receipt also fails `verify-run`, in both modes, unless a
later attempt superseded it: the receipt's attempt ended `blocked`, `failed` or
`abandoned`, and a later attempt of the same step (by ledger order) opened and
completed. The step must be declared in the locked plan; retained history for an
undeclared step cannot supersede a timeout. `verify-run` then reports a valid
supersession as a `superseded_timeout` warning, and
the proof still counts it in `execution.command_timed_out`. A passing rerun
inside the timed-out attempt does not supersede it, and `complete-step` and
`finish-step` refuse to complete an attempt that holds a timed-out receipt.
They also refuse while a command is still running for that attempt (exit 1, no
step events written); wait for its receipt before attempting completion. Commands
in the same attempt are serialized from the open-state check through receipt
append, so a timeout cannot arrive after completion. `block-step`,
`fail-step`, and `reclaim-step` use that same lock and refuse an active command
with exit 1 and no step events written. Retry recovery after its receipt arrives.

## Detection limits

- Only argv tokens and `sh -c` payload strings are scanned. Commands that build
  paths dynamically, encode them, or use shell features the analyzer does not
  model can evade screening.
- Absolute paths are normalized against the repo root when known; paths outside
  the repo are compared as-is.
- `write_outside_scope` is best-effort: explicit redirections are treated as
  high, while ambiguous write-looking operands (`cp`/`mv`/`tee` destinations)
  are medium.
