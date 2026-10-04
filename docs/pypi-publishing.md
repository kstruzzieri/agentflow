# PyPI publication packet (prepared, not executed)

This packet is a maintainer-only preparation record. Publisher and environment
configuration, uploads, releases, tags, and enabling publication remain
unperformed and must not be done from contributor work. The two owner-facing
actions, direct owner contact and the PEP 541 request, have been performed by
the maintainer and are recorded under "Ownership and PEP 541 evidence" below;
do not repeat them.

## Current gate

`.github/workflows/release.yml` contains `if: false # Issue #5 compatibility
freeze` on `publish-pypi`. Issue #5 remains open, so the compatibility freeze is
incomplete and the job must stay disabled until a separate maintainer approval.
It uses trusted publishing with no token; do not create or store a PyPI token.

## Prepared trusted-publisher values

The following values are prepared only; a maintainer must verify them in PyPI
before configuring anything. Both pending trusted-publisher rows are
maintainer-only and unperformed.

| Pending project / trusted publisher | GitHub owner | Repository | Workflow filename | Environment |
| --- | --- | --- | --- | --- |
| `agentflow-proof` | `kstruzzieri` | `agentflow` | `release.yml` | `pypi` |
| `agentflow-mcp` | `kstruzzieri` | `agentflow` | `release.yml` | `pypi` |

| Field | Prepared value |
| --- | --- |
| required reviewers | `kstruzzieri` (confirm current maintainer access) |
| Credentials | no token; trusted-publisher OIDC only after authorization |

Before removing `if: false`, a maintainer must create the `pypi` environment in
repository Settings and configure its required reviewers. The workflow's
`environment: pypi` reference does not create protection rules; if the named
environment is absent, GitHub can create it without those protections.

Only `agentflow-proof` produces the wheel and sdist. `agentflow-mcp` remains a
console command, not a second Python distribution. It may be configured or
claimed only with a separately approved legitimate companion distribution,
never an empty placeholder; empty `agentflow-mcp` placeholder uploads are
forbidden.

## Ownership and PEP 541 evidence

Owner contact and the PEP 541 request are the two external actions the
maintainer has performed. Both are recorded here and on
[issue #6](https://github.com/kstruzzieri/agentflow/issues/6); keep this table
current and leave unknown fields blank rather than inventing them.

| Evidence placeholder/category | Recorded value (checked 2026-09-12) |
| --- | --- |
| abandonment | https://pypi.org/project/agentflow/ has one release, 0.0.2, uploaded 2023-05-29, and none since; its listed homepage `github.com/stoyan-stoyanov/llmflow` no longer resolves; the author email on PyPI is blank; no owner response to the contact below |
| notability | https://github.com/kstruzzieri/agentflow, releases from `v0.4.0` (2026-07-11) with checksummed zipapps, `docs/` and the stability contract in `docs/stability.md`; no independent-usage URLs recorded yet |
| different-name workaround | the import package and console scripts are already `agentflow` and `agentflow-mcp`, so a distribution named `agentflow-proof` permanently differs from the command users type; `docs/stability.md` allows the rename with only `project.name` changing |
| usage evidence | none recorded yet; add installs, downstream references, or issue links as they appear |
| owner contact | 2026-07-20, https://github.com/stoyan-stoyanov/llmflows/discussions/58 (the successor repository of the dead homepage); no reply or reaction as of 2026-09-12; the owner was active on GitHub elsewhere during that window |
| requested outcome | transfer of the `agentflow` name; `agentflow-proof` remains the shipping distribution until then |

PEP 541 request filed 2026-09-12 by the maintainer:
https://github.com/pypi/support/issues/12242. Track it independently of the
release; moderators contact the owner themselves and the queue is measured in
months. If the transfer completes, only `project.name` in `pyproject.toml`
changes.

## First-publication checklist

Before a maintainer performs any remaining publication action (owner contact
and the PEP 541 request are already performed and recorded above; they were
never gated on Issue #5 and do not gate a release):

1. Confirm Issue #5 is closed; keep `if: false` in place while completing the
   remaining prerequisites.
2. Confirm the distribution name, the repository `kstruzzieri/agentflow`, the
   workflow `release.yml`, environment `pypi`, and required reviewers.
3. Keep the ownership/PEP 541 evidence table current. Owner contact
   (2026-07-20) and the PEP 541 request (pypi/support#12242, 2026-09-12) are
   done; the transfer outcome is tracked there and never gates a release.
4. As the maintainer, create the protected `pypi` environment in repository
   Settings and configure required reviewers; then configure the trusted
   publisher with no token and review the exact wheel and sdist already built
   by the workflow.
5. Only after steps 1-4, separately authorize removing `if: false`.
6. Confirm the PyPI stage contains exactly one wheel and one sdist, never a
   zipapp or placeholder upload; retain the `agentflow` and `agentflow-mcp`
   commands and imports.
