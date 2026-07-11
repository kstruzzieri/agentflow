# Single-Writer Leases

Agentflow's execution contract can enforce single-writer ownership of a plan
step: only the agent that holds a live lease on an attempt may write to it
(record commands, record file changes, run gated commands) or close it out
(verify, complete, block). Ownership is a wall-clock lease enforced with the
local `file_lock` inside one shared worktree — no daemon, no network, no
distributed lock service.

This is opt-in. It is gated entirely on `concurrency.lease_policy`, which
defaults to `advisory`. Existing plans and contracts see no behavior change.

## Advisory vs enforce

- `advisory` (default, and the behavior when the field is absent): leases are
  recorded but never block anything. A second `claim-step` on an open step is
  allowed, foreign writers are allowed, expired leases are informational only.
  This is exactly the pre-lease runtime behavior; the only invisible change is
  that all `step-runs.jsonl` mutations are serialized under a lock so concurrent
  claims can never mint a duplicate attempt id.
- `enforce`: a step may have at most one live open attempt. Claiming, writing
  to, or closing an attempt requires the owning agent's identity and a lease
  that has not expired. Recovery from a crashed or abandoned owner is explicit
  (`reclaim-step`, `renew-lease`, or the `fail-step` break-glass).

## Contract fields

Set these under the `concurrency` block of `.agent/execution.contract.json`:

| Field | Default | Meaning |
| --- | --- | --- |
| `lease_policy` | `advisory` | `advisory` or `enforce`. Any other value is rejected. |
| `lease_ttl_minutes` | `30` | Default lease length stamped at claim/amend/renew when no explicit value is given. Must be a positive integer. |
| `lease_grace_seconds` | `30` | Slack added to a deadline before an attempt is treated as expired. Must be a non-negative integer. |

All three are optional and additive — a contract with none of them reads as
`advisory` with the defaults above, so there is no contract schema bump and no
migration required to keep advisory behavior.

Turning enforcement on is a one-line edit:

```json
"concurrency": { "writer_model": "single_writer", "lease_policy": "enforce" }
```

## The enforce command loop

Every write and lifecycle command needs the acting agent's identity under
`enforce`. For the write and lifecycle commands — `run`, `record-command`,
`record-file-change`, `verify-step`, `complete-step`, `block-step`,
`finish-step`, `reclaim-step`, `renew-lease`, and the optional `--agent` on
`fail-step` — `--agent` defaults to `AGENTFLOW_AGENT_ID`, so setting it in the
environment once lets those commands run without repeating the flag.
`claim-step` and `amend-step` still require an explicit `--agent`: they open a
new attempt and must name its owner outright rather than inherit one.

```bash
export AGENTFLOW_AGENT_ID=agent-a

# 1. Claim the step. Under enforce this stamps a finite lease (lease_ttl_minutes,
#    or override with --lease-minutes for a longer step).
agentflow claim-step P1 --agent agent-a --lease-minutes 60

# 2. Do the work as the owner. Every write carries the agent identity.
agentflow record-file-change --step P1 --path src/foo.py --agent agent-a
agentflow run --step P1 --agent agent-a --gate "pytest -q" -- pytest -q

# 3. Long step? Extend your own lease before it lapses. `agentflow run`
#    auto-renews when a command's timeout would outlast the remaining lease,
#    but renew-lease is the explicit control for long manual work.
agentflow renew-lease P1 --agent agent-a --minutes 120

# 4. Close it out as the owner.
agentflow verify-step P1 --agent agent-a
agentflow complete-step P1 --agent agent-a
```

### Recovering a crashed owner

If the owner dies and its lease lapses, another agent recovers the step by
abandoning the expired attempt and opening a fresh one:

```bash
# Only works once agent-a's lease has actually expired (deadline + grace).
agentflow reclaim-step P1 --agent agent-b --reason "agent-a crashed" --lease-minutes 60
```

`reclaim-step` records an `abandoned` event on the old attempt (with
`abandoned_by`, `reason`, and `superseded_by`) and a fresh `claimed` event for
the new owner, atomically under the step-run lock. It refuses to run while the
old lease is still live — coordinate or wait instead.

### Owner self-recovery of an expired lease

The owning agent may `renew-lease` even after its own finite lease has expired —
that is a valid recovery path and does not require reclaim. Foreign recovery
always goes through `reclaim-step`.

### Break-glass: fail-step

`fail-step` is the escape hatch. It performs no owner or expiry check and closes
the attempt as `failed`; pass `--agent` only to record `failed_by`. Use it when
an attempt must be forced closed and neither renew nor reclaim applies.

```bash
agentflow fail-step P1 --reason "unrecoverable" --agent operator
```

## Migration to enforce

Attempts claimed before enforcement (or claimed under `advisory`) may carry no
lease deadline (`lease_expires_at: null`). Under `enforce` a no-deadline open
attempt is a special case: it is **owner-renewable but not reclaimable**, because
there is no expiry to prove abandonment against.

Before switching a live run to `enforce`:

- Finish or `fail-step` any open no-deadline attempts, **or**
- Have the owner `renew-lease` to convert the legacy attempt into a finite lease.

`verify-run` and `audit-drift` surface no-deadline open attempts and expired
leases so they are easy to find before flipping the policy.

## Representation in coverage, proof, and drift

- `verify-run` (under `enforce`) errors on an unrecovered expired lease and warns
  on a no-deadline open attempt.
- The proof pack's `coverage` block carries `expired_leases`,
  `no_deadline_open_attempts`, and `abandoned_attempts` inside the hash-bound
  `canonical_core`.
- `audit-drift` emits an advisory `stale_attempts` note for expired open leases.
  It is informational only and never changes the drift verdict.

## Non-goals

Single-writer lease enforcement deliberately does **not**:

- Enable `multi_writer`. `WRITER_MODELS` stays `("single_writer",)`; the contract
  still rejects any other writer model.
- Provide remote or cloud agent orchestration or routing.
- Do cross-worktree / cross-tree attempt-id aggregation or ledger merge — that is
  [#30](https://github.com/kstruzzieri/agentflow/issues/30) and stays a separate,
  deferred item. Leases coordinate agents inside **one** shared worktree only.
- Run any distributed lock service, daemon, or network coordinator.
