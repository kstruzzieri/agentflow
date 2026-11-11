# Security Model

Agentflow records and verifies evidence for a workflow. It does not replace host security controls, code review, secret handling, or an operator's judgment. This document describes the trust boundaries that matter when using or publishing Agentflow artifacts.

## Command execution

### Guarantee

`agentflow run` deterministically classifies the argv it receives (and selected `sh -c` payloads), records the classification on a receipt, and can apply a configured policy to block high-risk commands before it starts them. New execution contracts default to explicit confirmation for high-risk commands.

### Non-guarantee

Command-risk screening is **not a sandbox**. It does not isolate a process, intercept system calls, restrict network access, authenticate commands, or model all shell syntax and dynamically constructed paths. A command that screening allows still runs with the permissions, files, credentials, and network access of the invoking environment.

### Threat assumptions

The operator can inspect the plan and command before execution, the host and its credentials are already appropriately protected, and an attacker has not replaced the local Agentflow executable or altered the working directory after the operator's review.

### Residual user responsibility

Run Agentflow in the isolation appropriate to the task (for example, a container, VM, restricted account, or dedicated worktree), review commands and their outputs, restrict credentials, and treat risk-policy confirmation as an explicit decision to run the command—not as a safety override.

## Agent artifact publication

### Guarantee

Agentflow writes workflow artifacts to the active root `.agent/` directory and does not publish them automatically. It can build proof metadata with hashes for reviewed local-only, CI-uploaded, PR-attached, or committed-proof workflows. The repository and operator—not Agentflow—control whether `.agent/` is ignored or published.

### Non-guarantee

`.agent/` is not a secret store and publishing it is not automatically safe. Command receipts can reveal command strings, paths, environment-variable names, and risk findings. Captured stdout and stderr can contain secrets, proprietary logs, stack traces, or source snippets. Runtime, context, failure, review, and handoff records can also disclose internal services or task context.

### Threat assumptions

Anyone who can read a published repository, CI artifact, PR attachment, or shared proof bundle can read the files included in it. Receipt content is only as safe as the commands and services that produced it.

### Residual user responsibility

Keep `/.agent/` ignored unless deliberately publishing a reviewed proof bundle. Review and, when necessary, redact every `.agent/` file before publishing it. Prefer the smallest proof subset that satisfies the receiving workflow, use bounded artifact retention, and never place secrets in commands or logs that could be captured. See [Agent Artifact Policy](agent-artifacts.md) for the specific files to review.

## Loopback HTTP MCP transport

### Guarantee

Agentflow's MCP server uses stdio by default. Its optional HTTP transport binds to `127.0.0.1` by default, warns when asked to bind a non-loopback host, rejects non-localhost browser `Origin` headers, and limits request bodies. The server does not expose `run` or `record-command`, so a client cannot submit a new arbitrary argv directly. However, `verify_step`, `verify_proof`, and `finish_step` accept `replay`; when requested, Agentflow executes attested command gates already present under the client-selected root.

### Non-guarantee

The HTTP transport is unauthenticated. Its `Origin` check is a browser defense, not client authentication: non-browser clients can omit that header. Replay runs with the MCP server process's permissions, so the surface is not categorically non-executing even though it rejects client-supplied argv. Stdio is a local parent-process transport and does not create an HTTP listener; loopback HTTP is a listening service on the local machine. Neither transport makes an untrusted client trustworthy, and the HTTP controls do not authorize exposure to an untrusted LAN, the internet, or another user's local processes.

### Threat assumptions

The local host, the process that starts the server, and each client-selected root and attested receipt ledger are trusted; the selected bind address is reachable only by intended clients; and any reverse proxy, tunnel, browser extension, or network configuration has been evaluated by the operator.

### Residual user responsibility

Keep HTTP bound to loopback unless every reachable client and network path is trusted. Treat a client-selected root and its attested command receipts as executable input: review them before allowing replay, and do not let untrusted clients select them. Do not publish or tunnel the endpoint without adding controls suitable for that deployment. Treat browser integrations, local-network exposure, reverse proxies, and untrusted clients as separate security decisions. See [MCP Server](mcp.md) for transport setup.

## Proof integrity

### Guarantee

`build-proof` records SHA-256 hashes for declared proof inputs and a canonical proof core; `verify-proof` recomputes them and reports changes to declared, hash-bound content. This gives checksum-based tamper evidence for a proof bundle that is retained and verified as built.

### Non-guarantee

Proof integrity is **not cryptographic signing**. Agentflow does not establish the identity of a proof producer, protect an operator's private key, prove that a bundle is complete, or prevent a party who can rewrite a bundle from recomputing its checksums. Proof signing (for example, HMAC or Sigstore) is out of scope for v1.0. Likewise, a command receipt's `provenance` records which Agentflow command wrote it, not who ran the command: anyone who can write `.agent/` can write any value (see [Command Receipt Provenance](agent-workflow.md#command-receipt-provenance)).

### Threat assumptions

The verifier obtains the intended proof bundle from a trusted channel and can compare it against an independently retained reference, such as a CI artifact, commit, or release attachment.

### Residual user responsibility

Protect the repository, CI system, and proof distribution channel; retain provenance outside the bundle when producer identity matters; and use an external signing or attestation system if authenticity or non-repudiation is a requirement.

Decide the minimum policy yourself. The execution contract that sets hunk attribution, the command risk policy, the review gate, leases, and receipt requirements is written by whoever can write `.agent/`, which is usually the agent being checked. A proof built under a weak contract is a valid proof of a weak run. Pass `verify-proof --require KEY=MIN` from CI configuration the agent cannot edit so that the verifier, not the contract, sets the floor.

A floor on `risk_policy`, `lease_policy`, or `hunk_attribution` also checks the contract the work ran under, because these settings act while the work runs (#117). Every step event, command and file receipt, verification run, and the drift report record `execution_contract_sha256`: the SHA-256 of the contract bytes the command that wrote them read its policy from. Each command reads the contract once, when it starts, so a command that `run` launches or a gate that `replay-gates` replays cannot rewrite the contract and have its records name the restored one. Under such a floor, `verify-proof` fails with `policy_below_floor` unless every record in the hashed step-runs, command-receipts, file-receipts, and verification-runs ledgers, and a hashed drift report written by `audit-drift`, carry the hash of the verified contract. A ledger or drift report missing from the proof fails, as does an untouched drift report and records written before this change, which carry no pin. `aggregate-ledgers` writes the four ledgers even when they are empty, as `init-execution` does, so a missing one always means it was removed. Floors on the other keys are judged again at build and verify time and do not need the pin.

Changing the contract mid-run therefore fails such a floor even if the change is reverted before `build-proof`. Redoing steps in place does not recover, because amended and failed attempts keep their records in the ledgers: archive `.agent/`, start a new execution history under the final contract, and redo the work. On an aggregated root, run `audit-drift` on the root after `aggregate-ledgers`; the pin covers the rows that survive aggregation, not source diagnostics it drops.

The pin records what Agentflow wrote, not who wrote it. Like receipt provenance, it does not stop a party who can write `.agent/` from writing forged lines, it covers only the retained history (not runs dropped by `init-execution --force`), and it does not cover commands Agentflow did not run: an attested receipt records a command run elsewhere, without a risk check. Replaying that gate applies the risk policy and command timeout `run` would apply, so `replay-gates --record` cannot turn a command the policy blocks into a trusted receipt (#119).

Receipt requirements can also be tightened after a step completes without invalidating its completion. For example, a step completed with an attested receipt while `require_managed_receipts_for_validation` is `false` can still pass `verify-proof --require require_managed_receipts_for_validation=true` if the setting is raised before `build-proof`. The proof records that gate as `failed`, but verification can still exit 0 and print `proof verified`. A receipt-policy floor therefore proves the setting at build time, not that completed gates met that policy.
