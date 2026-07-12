# Security Policy

## Supported Versions

Security fixes are applied to the latest released version and the `main`
branch. Older releases may not receive backports.

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting form under the repository's
**Security** tab. Include affected versions, reproduction steps, impact, and
any suggested mitigation. Do not open a public issue or include live secrets
in a report.

If private vulnerability reporting is unavailable, contact the repository
owner through their GitHub profile to arrange a private channel before sharing
technical details.

Agentflow records commands, paths, and command output in local proof artifacts.
Review [docs/agent-artifacts.md](docs/agent-artifacts.md) before publishing
anything under `.agent/`. Command-risk screening is a policy aid, not a process
sandbox or substitute for host-level isolation.

For the supported guarantees, non-guarantees, threat assumptions, and residual
operator responsibilities across command execution, artifact publication, MCP
transport, and proof integrity, see
[docs/security-model.md](docs/security-model.md).
