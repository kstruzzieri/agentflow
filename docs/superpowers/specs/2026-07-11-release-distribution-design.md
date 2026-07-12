# Release Discipline and Distribution Design

## Purpose

Prepare Agentflow for a trustworthy v1.0 release without making the release
depend on transfer of the occupied `agentflow` name on PyPI. The work is split
into two serial pull requests because Issue #3 must land before Issue #6 changes
the same release workflow.

The first pull request establishes release discipline. The second, created only
after the first is merged and rebased into `main`, adds Python distribution
artifacts and disabled trusted publishing. No package is uploaded, no PyPI name
is claimed, and no owner or PyPI support message is sent by either pull request.

## Sources and constraints

This design implements Issues #3 and #6 under the v1.0 umbrella in Issue #11.
It follows PEP 541 for the prepared name-transfer evidence and uses
`agentflow-archive` as the source for the 0.3.0 CHANGELOG backfill because the
current public repository begins with the v0.4.0 import commit.

Repository-wide constraints:

- Python 3.11 is the minimum supported version; clean-install coverage is
  Python 3.11, 3.12, and 3.13.
- Runtime code and the release guard remain standard-library-only.
- Tests use `unittest`, matching the existing suite.
- GitHub Actions are pinned to immutable commit SHAs and remain eligible for
  Dependabot updates.
- The literal versions in `pyproject.toml` and
  `src/agentflow/__init__.py` remain separate and must agree. Dynamic version
  metadata is deliberately not introduced because Issue #3 requires both
  declarations to be checked before a build backend resolves metadata.
- The PR-time consistency check compares only the two version declarations.
  It never constrains the distribution name, so the second pull request can
  change `project.name` from `agentflow` to `agentflow-proof`.
- Clean-install jobs install explicit local artifact paths with `--no-index`.
  They never ask an index to resolve `agentflow`, which currently identifies an
  unrelated distribution.
- README and CONTRIBUTING installation changes are isolated in the final commit
  of the second pull request for shared-document integration.

## Delivery sequence

### Pull request 1: release discipline (Issue #3)

PR1 contains only the release guard, CHANGELOG, release-procedure documentation,
workflow wiring, and focused tests. It is fully validated through a fresh
Agentflow plan and proof chain, then merged before PR2 begins.

PR1 changes:

- Add `CHANGELOG.md` in Keep a Changelog form with `Unreleased`, `0.4.0`, and
  `0.3.0` sections. Dates and entries are reconstructed from the tagged public
  release and `agentflow-archive` history.
- Add `scripts/check_release.py`, a standard-library CLI. With no tag argument,
  it checks that `project.version` and `agentflow.__version__` agree. With
  `--tag`, it additionally requires the exact `vMAJOR.MINOR.PATCH` form, equality
  with both declarations, and a matching released CHANGELOG heading. An optional
  notes-output argument extracts that release's CHANGELOG body for GitHub
  release notes.
- Add focused `unittest` coverage for valid and invalid tags, either version
  mismatch, missing CHANGELOG headings, and exact release-note extraction.
- Add the version-only invocation to `.github/workflows/ci.yml`, so version
  drift fails on pull requests rather than waiting for a tag.
- Split `.github/workflows/release.yml` into an early read-only guard job and the
  existing release job. The release job depends on the guard, repeats the
  deterministic validation after checkout to create its notes file, and passes
  that file to `gh release create --notes-file` instead of generating a second
  competing set of notes.
- Update `docs/packaging.md` with the exact release order: update both literal
  versions, move `Unreleased` entries into the dated release heading, commit,
  tag, push, validate, and post-release smoke.

The release workflow still builds only the existing zipapps in PR1. Wheel,
sdist, artifact handoff, and PyPI permissions are intentionally absent until
PR1 has landed.

### Pull request 2: release distribution (Issue #6 Track B)

PR2 starts from the merged PR1 commit on current `main`, uses a new isolated
worktree and fresh `.agent/` execution state, and changes the distribution
metadata to the provisional `agentflow-proof` name. Imports remain `agentflow`,
and the installed scripts remain `agentflow` and `agentflow-mcp`. Switching to
the permanent `agentflow` name later is a documented one-line `project.name`
change, not a templating or dynamic-build system.

PR2 restructures the release workflow into this data flow:

1. `guard` validates the tag and CHANGELOG before tests or builds.
2. `build` depends on `guard`, runs the full source suite, builds both zipapps,
   one wheel, and one sdist from the tagged checkout, inspects their contents and
   metadata, and uploads exactly those bytes as one workflow artifact.
3. `clean-install-wheel` downloads that artifact and installs the wheel by
   explicit path with `--no-index` on Python 3.11, 3.12, and 3.13. Each matrix
   leg runs `agentflow --version`, `agentflow --help`, and an MCP `initialize`
   request over stdio.
4. `clean-install-sdist` downloads the same artifact, installs the sdist by
   explicit path with `--no-index` on Python 3.11, and runs the CLI version
   smoke. This catches missing source files independently of wheel coverage.
5. `github-release` depends on the guard and both install jobs, downloads the
   original workflow artifact, generates `SHA256SUMS` covering both zipapps,
   the wheel, and the sdist, and attaches those exact files with the extracted
   CHANGELOG notes.
6. `publish-pypi` is present but statically disabled with `if: false` and a
   comment naming the Issue #5 compatibility freeze as the enablement gate. It
   downloads the same workflow artifact and uses PyPI trusted publishing. Its
   `needs:` list includes `guard`, `clean-install-wheel`, and
   `clean-install-sdist`, so enabling it cannot bypass a failed guard or install
   test.

There is no rebuild in either publication job. Tests, GitHub release creation,
and eventual PyPI publication all consume the bytes produced by `build`.

## Build inputs and artifact inspection

The CI-only `build`, `setuptools`, and metadata-checking tool versions are
pinned. The build-system setuptools requirement is also pinned so isolated
build environments cannot silently change backend behavior. Runtime dependency
metadata remains empty.

`SOURCE_DATE_EPOCH` is the tag commit timestamp for release builds. Non-tag
PR/CI builds use the checked-out `HEAD` commit timestamp. This controls known
timestamp inputs but is not presented as proof that every backend output is
universally byte-reproducible. The stronger provenance guarantee is that the
workflow builds once, tests those artifacts, and publishes those same bytes.

A standard-library artifact inspector opens the wheel and sdist rather than
trusting the build exit code. It verifies:

- normalized wheel/sdist filenames and version;
- `Name: agentflow-proof` and the expected version in package metadata;
- both console entry points and their target modules;
- inclusion of the `agentflow` package and required packaging documents;
- exclusion of tests, caches, root `.agent/` state, and unrelated repository
  files from the wheel;
- expected source files in the sdist.

The normal packaging metadata checker also runs as a pinned CI-only tool.

## Permissions and publishing gates

Workflow permissions default to `contents: read`. The GitHub release job alone
receives `contents: write`. The disabled PyPI job alone receives
`id-token: write`, has no password or API-token input, and targets the protected
`pypi` GitHub environment.

The static `if: false` gate and environment protection serve different roles:
the former prevents all execution before Issue #5 completes, while the latter
requires the configured environment policy after the static gate is removed.
No long-lived PyPI token is created or stored.

The manual setup documentation covers PyPI pending trusted publishers for the
not-yet-existing `agentflow-proof` and `agentflow-mcp` projects. It states that
pending-publisher setup is configuration, not proof that a name has been
claimed or a distribution published. `agentflow-mcp` may only be claimed with
a legitimate companion release; an empty placeholder upload is prohibited.

## Compatibility seam

Issues #4 and #5 have not selected and implemented the frozen cross-version
command surface. PR2 therefore does not claim compatibility acceptance. The
workflow and documentation identify the exact post-install location where the
selected command must run against the tagged-v0.4.0 fixture before publication
can be enabled. The disabled publish job depends on the install jobs that will
own that check.

When #4/#5 supply the fixture and command, integration replaces the documented
blocked seam with the real invocation; it does not require a new artifact build
or a second publication path.

## Name-transfer and manual-operation packet

PR2 adds a document containing:

- respectful current-owner contact text;
- contact-attempt evidence fields and dates required by PEP 541;
- the abandonment, notability, different-name-workaround, and usage evidence
  expected for a transfer request;
- a draft PyPI support issue that maps each claim to evidence;
- exact pending-publisher configuration values for the GitHub owner,
  repository, release workflow filename, and `pypi` environment;
- a manual approval checklist for legitimate first publication of
  `agentflow-proof` and the `agentflow-mcp` companion.

These are prepared payloads only. Posting owner contact, opening a support
case, configuring PyPI, uploading a distribution, or otherwise claiming a name
requires explicit authorization in the active session.

## Error handling

The release guard fails closed with a concise nonzero error for malformed tags,
unreadable or invalid version declarations, version disagreement, duplicate or
missing release headings, and missing release-note bodies. It performs no build
or import before validation completes.

Workflow dependency edges prevent downstream jobs from running after a guard,
build, inspection, or clean-install failure. Artifact installers use isolated
environments and explicit local paths. Missing or ambiguous artifact globs are
treated as errors rather than allowing index fallback.

## Validation and evidence

Each pull request has its own locked Agentflow plan, claimed steps, recorded
file changes, command receipts, focused tests, full suite, drift audit, proof
build, and proof verification.

PR1 validation includes:

- red/green unit tests for the release guard;
- version-only PR check behavior;
- full `unittest` suite;
- release-workflow inspection proving the guard runs before the release job and
  CHANGELOG notes replace generated notes.

PR2 validation includes:

- red/green tests for artifact inspection and distribution metadata;
- wheel and sdist builds using pinned tools;
- archive and metadata inspection;
- local clean installs on every available Python 3.11-3.13 interpreter, with
  GitHub Actions providing the authoritative matrix;
- wheel CLI and MCP stdio smokes plus the sdist CLI smoke;
- focused workflow tests proving permissions, static publication disablement,
  complete `needs:` dependencies, artifact handoff, and no index-based install;
- full suite and Agentflow proof chain.

Before either pull request is integrated, its branch is updated against current
`main`, conflicts are limited to that branch's owned files, validation and the
proof chain are rerun, and the final commit SHA is reported.

## Explicit non-goals

- Publishing any artifact or claiming any PyPI name.
- Contacting the current `agentflow` owner or PyPI support.
- Enabling publication before Issue #5 freezes the compatibility contract.
- Changing Python import paths or installed console-script names.
- Adding build-time name templating, a custom release orchestrator, signing,
  PyInstaller binaries, or a broader cross-OS release matrix.
