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

Agentflow keeps the active root `.agent/` directory local and ignored by default. It can build proof metadata with hashes and lets users choose reviewed local-only, CI-uploaded, PR-attached, or committed-proof workflows.

### Non-guarantee

`.agent/` is not a secret store and publishing it is not automatically safe. Command receipts can reveal command strings, paths, environment-variable names, and risk findings. Captured stdout and stderr can contain secrets, proprietary logs, stack traces, or source snippets. Runtime, context, failure, review, and handoff records can also disclose internal services or task context.

### Threat assumptions

Anyone who can read a published repository, CI artifact, PR attachment, or shared proof bundle can read the files included in it. Receipt content is only as safe as the commands and services that produced it.

### Residual user responsibility

Review and, when necessary, redact every `.agent/` file before publishing it. Prefer the smallest proof subset that satisfies the receiving workflow, use bounded artifact retention, and never place secrets in commands or logs that could be captured. See [Agent Artifact Policy](agent-artifacts.md) for the specific files to review.

## Loopback HTTP MCP transport

### Guarantee

Agentflow's MCP server uses stdio by default. Its optional HTTP transport binds to `127.0.0.1` by default, warns when asked to bind a non-loopback host, rejects non-localhost browser `Origin` headers, limits request bodies, and exposes only read/query and workflow state-transition tools—not arbitrary command execution.

### Non-guarantee

The HTTP transport is unauthenticated. Its `Origin` check is a browser defense, not client authentication: non-browser clients can omit that header. Stdio is a local parent-process transport and does not create an HTTP listener; loopback HTTP is a listening service on the local machine. Neither transport makes an untrusted client trustworthy, and the HTTP controls do not authorize exposure to an untrusted LAN, the internet, or another user's local processes.

### Threat assumptions

The local host and the process that starts the server are trusted, the selected bind address is reachable only by intended clients, and any reverse proxy, tunnel, browser extension, or network configuration has been evaluated by the operator.

### Residual user responsibility

Keep HTTP bound to loopback unless every reachable client and network path is trusted. Do not publish or tunnel the endpoint without adding controls suitable for that deployment. Treat browser integrations, local-network exposure, reverse proxies, and untrusted clients as separate security decisions. See [MCP Server](mcp.md) for transport setup.

## Proof integrity

### Guarantee

`build-proof` records SHA-256 hashes for declared proof inputs and a canonical proof core; `verify-proof` recomputes them and reports changes to declared, hash-bound content. This gives checksum-based tamper evidence for a proof bundle that is retained and verified as built.

### Non-guarantee

Proof integrity is **not cryptographic signing**. Agentflow does not establish the identity of a proof producer, protect an operator's private key, prove that a bundle is complete, or prevent a party who can rewrite a bundle from recomputing its checksums. Proof signing (for example, HMAC or Sigstore) is out of scope for v1.0.

### Threat assumptions

The verifier obtains the intended proof bundle from a trusted channel and can compare it against an independently retained reference, such as a CI artifact, commit, or release attachment.

### Residual user responsibility

Protect the repository, CI system, and proof distribution channel; retain provenance outside the bundle when producer identity matters; and use an external signing or attestation system if authenticity or non-repudiation is a requirement.

