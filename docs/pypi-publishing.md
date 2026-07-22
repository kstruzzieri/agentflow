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

## Prepared publisher values

The following values are prepared only; a maintainer must verify them in PyPI
before configuring anything.

| Field | Prepared value |
| --- | --- |
| Distribution publisher | `agentflow-proof` |
| Reserved name to assess, not upload | `agentflow-mcp` |
| GitHub owner/repository | `kstruzzieri/agentflow` |
| Workflow | `.github/workflows/release.yml` |
| GitHub environment | `pypi` |
| required reviewers | `kstruzzieri` (confirm current maintainer access) |
| Credentials | no token; trusted-publisher OIDC only after authorization |

Only `agentflow-proof` produces the wheel and sdist. `agentflow-mcp` remains a
console command, not a second Python distribution: empty `agentflow-mcp`
placeholder uploads are forbidden.

## Prepared ownership and PEP 541 evidence

Record evidence before any maintainer-only owner contact or support request;
leave unknown fields blank rather than inventing them.

| Evidence | Prepared field |
| --- | --- |
| Project-name availability | URL, check date, result |
| Repository identity | `https://github.com/kstruzzieri/agentflow`, release and commit URLs |
| Ownership/contact evidence | owner contact URL or address, date, message, response |
| PEP 541 activity evidence | PyPI project URL, last release/activity date, maintainer response status |
| Requested outcome | transfer, collaborator access, or use of `agentflow-proof` |

Prepared owner-contact wording (unperformed; maintainer-only): “Hello, I
maintain `kstruzzieri/agentflow`. May we discuss the `agentflow` PyPI name? The
repository, release evidence, and requested outcome are recorded above.”

Draft PyPI support-request body (unperformed; maintainer-only): “Please review
the attached PEP 541 evidence for `agentflow`: project activity, owner-contact
attempts, the public repository, and the requested outcome. No package upload
or publisher configuration has been performed.” Do not send this draft without
maintainer approval and complete evidence fields.

## First-publication checklist

Before a maintainer performs any external action:

1. Confirm Issue #5 is closed and separately authorize removing `if: false`.
2. Confirm the distribution name, the repository `kstruzzieri/agentflow`, the
   workflow `release.yml`, environment `pypi`, and required reviewers.
3. Complete the ownership/PEP 541 evidence fields and decide whether owner
   contact or the support request is necessary; neither is performed by this
   packet.
4. Configure the trusted publisher and protected environment only as the
   maintainer, with no token, then review the exact wheel and sdist already
   built by the workflow.
5. Confirm the PyPI stage contains exactly one wheel and one sdist, never a
   zipapp or placeholder upload; retain the `agentflow` and `agentflow-mcp`
   commands and imports.
