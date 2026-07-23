# PyPI publication packet (prepared, not executed)

This packet is a maintainer-only preparation record. Every external operation
below is unperformed: do not claim names, configure publishers or environments,
contact an owner or PyPI support, upload artifacts, create releases or tags, or
enable publication from contributor work.

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

## Prepared ownership and PEP 541 evidence

Record evidence before any maintainer-only owner contact or support request;
leave unknown fields blank rather than inventing them.

| Evidence placeholder/category | Prepared field |
| --- | --- |
| abandonment | PyPI project URL, last release/activity date, maintainer response status |
| notability | repository, release, documentation, and independent-usage URLs; check date |
| different-name workaround | compatibility, migration, and user-impact evidence for `agentflow-proof` |
| usage evidence | installs, downstream references, issue links, or other verifiable adoption evidence |
| owner contact | owner contact URL or address, date, message, response |
| requested outcome | transfer, collaborator access, or use of `agentflow-proof` |

Prepared owner-contact wording (unperformed; maintainer-only): “Hello, I
maintain `kstruzzieri/agentflow`. May we discuss the `agentflow` PyPI name? The
repository, release evidence, and requested outcome are recorded above.”

Draft PyPI support-request body (unperformed; maintainer-only): “Please review
the PEP 541 evidence for `agentflow`. Abandonment: [abandonment evidence].
Notability: [notability evidence]. Different-name workaround: [different-name
workaround evidence]. Usage: [usage evidence]. Owner contact: [owner-contact
evidence]. Requested outcome: [requested outcome]. No package upload or
publisher configuration has been performed.” Do not send this draft without
maintainer approval and completed evidence fields.

## First-publication checklist

Before a maintainer performs any external action:

1. Confirm Issue #5 is closed; keep `if: false` in place while completing the
   remaining prerequisites.
2. Confirm the distribution name, the repository `kstruzzieri/agentflow`, the
   workflow `release.yml`, environment `pypi`, and required reviewers.
3. Complete the ownership/PEP 541 evidence fields and decide whether owner
   contact or the support request is necessary; neither is performed by this
   packet.
4. As the maintainer, create the protected `pypi` environment in repository
   Settings and configure required reviewers; then configure the trusted
   publisher with no token and review the exact wheel and sdist already built
   by the workflow.
5. Only after steps 1-4, separately authorize removing `if: false`.
6. Confirm the PyPI stage contains exactly one wheel and one sdist, never a
   zipapp or placeholder upload; retain the `agentflow` and `agentflow-mcp`
   commands and imports.
