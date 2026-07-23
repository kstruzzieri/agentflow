# PyPI Distribution Track B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one wheel, one sdist, and both zipapps once; inspect and clean-install those exact bytes; exercise the installed compatibility seam; and prepare a statically disabled OIDC publishing path without publishing or configuring anything.

**Architecture:** Keep packaging policy in `pyproject.toml`, verify archives with one standard-library script, and express release orchestration in GitHub Actions. The build job owns all artifact creation; clean-install, GitHub release, and disabled PyPI jobs only download its named artifact. Documentation is the last task and begins only after rebasing onto the merged #8/#10 lane.

**Tech Stack:** Python 3.11 standard library, `unittest`, PEP 517 via `build==1.5.0`, `setuptools==83.0.0`, `twine==6.2.0`, and SHA-pinned GitHub Actions.

## Global Constraints

- Minimum Python: 3.11; wheel clean-install matrix: 3.11, 3.12, 3.13.
- Distribution metadata name: `agentflow-proof`; import package: `agentflow`.
- Installed scripts remain `agentflow` and `agentflow-mcp`.
- Runtime dependencies remain empty; no runtime dependency or dynamic naming layer may be added.
- Build backend: `setuptools==83.0.0`; CI build frontend: `build==1.5.0`; metadata checker: `twine==6.2.0`.
- `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` (`v7.0.1`).
- `actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` (`v8.0.1`).
- `pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247` (`v1.14.1`).
- Root workflow permission stays `contents: read`; only `github-release` gets `contents: write`; only `publish-pypi` gets `id-token: write`.
- `publish-pypi` has literal `if: false`, uses the `pypi` environment, and remains disabled until Issue #5 completes and a maintainer separately authorizes enablement.
- Never contact an owner or PyPI support; never create a publisher, project, token, release, tag, or upload.
- README and CONTRIBUTING installation edits are the final codebase edits and start only after #8/#10 has merged and this branch has rebased onto current `origin/main`.

## File Structure

- `pyproject.toml`: provisional distribution name, exact build backend, unchanged entry points and runtime dependency contract.
- `MANIFEST.in`: include `CHANGELOG.md` in the sdist.
- `scripts/check_distribution.py`: standard-library archive and metadata inspector; no extraction and no network.
- `tests/test_distribution.py`: synthetic wheel/sdist contract tests plus repository metadata assertions.
- `.github/workflows/release.yml`: build-once artifact graph, install gates, GitHub release, and disabled trusted publishing.
- `tests/test_release.py`: static workflow and release-document contract tests.
- `README.md`, `CONTRIBUTING.md`, `docs/packaging.md`: user and contributor installation/release guidance.
- `docs/pypi-publishing.md`: prepared maintainer packet and exact manual configuration values; documentation only.

---

### Task 1: Distribution Metadata and Artifact Inspector

**Files:**

- Create: `MANIFEST.in`
- Create: `scripts/check_distribution.py`
- Create: `tests/test_distribution.py`
- Create: `docs/superpowers/plans/2026-07-19-pypi-distribution-track-b.md`
- Modify: `pyproject.toml`

**Interfaces:**

- Consumes: `[project].name`, `[project].version`, `[project].dependencies`, and `[project.scripts]` from a root `pyproject.toml`.
- Produces: `python3 scripts/check_distribution.py --root <repo> --dist-dir <dir>`, returning `0` only when exactly one wheel and one sdist match the declared contract.
- Produces artifact names `agentflow_proof-0.4.0-py3-none-any.whl` and `agentflow_proof-0.4.0.tar.gz` for the current version.

- [ ] **Step 1: Write the failing artifact-contract tests**

  Add synthetic archives in `tests/test_distribution.py` using `zipfile.ZipFile` and `tarfile.open`. A valid wheel fixture contains every `src/agentflow/*.py` path mapped under `agentflow/`, one `agentflow_proof-0.4.0.dist-info/METADATA`, one `entry_points.txt`, `WHEEL`, and `licenses/LICENSE`. A valid sdist fixture contains the same sources below `agentflow_proof-0.4.0/src/agentflow/`, root `PKG-INFO`, `pyproject.toml`, `README.md`, `LICENSE`, and `CHANGELOG.md`.

  Add these concrete cases:

  ```python
  def test_valid_wheel_and_sdist_pass(self):
      result = self.run_inspector()
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertIn("distribution artifacts passed", result.stdout)

  def test_requires_exactly_one_wheel_and_sdist(self):
      self.write_valid_wheel("extra-0.4.0-py3-none-any.whl")
      result = self.run_inspector()
      self.assertEqual(result.returncode, 1)
      self.assertIn("expected exactly one wheel", result.stderr)

  def test_rejects_wrong_metadata_name_or_version(self):
      self.write_valid_wheel(metadata_name="agentflow", metadata_version="9.9.9")
      result = self.run_inspector()
      self.assertEqual(result.returncode, 1)
      self.assertIn("metadata", result.stderr)

  def test_rejects_changed_console_entry_points(self):
      self.write_valid_wheel(entry_points={"agentflow": "other:main"})
      result = self.run_inspector()
      self.assertEqual(result.returncode, 1)
      self.assertIn("console entry points", result.stderr)

  def test_rejects_missing_package_source(self):
      self.write_valid_wheel(omit="agentflow/cli.py")
      result = self.run_inspector()
      self.assertEqual(result.returncode, 1)
      self.assertIn("agentflow/cli.py", result.stderr)

  def test_rejects_wheel_repository_leakage(self):
      self.write_valid_wheel(extra={"tests/test_cli.py": b"pass\n"})
      result = self.run_inspector()
      self.assertEqual(result.returncode, 1)
      self.assertIn("unexpected wheel path", result.stderr)

  def test_rejects_missing_sdist_document_or_source(self):
      self.write_valid_sdist(omit="CHANGELOG.md")
      result = self.run_inspector()
      self.assertEqual(result.returncode, 1)
      self.assertIn("CHANGELOG.md", result.stderr)
  ```

- [ ] **Step 2: Run the tests and prove RED**

  Run through Agentflow with an inverted shell exit so the receipt succeeds only when the new tests fail because `scripts/check_distribution.py` is absent:

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P1 --agent codex-ticket-6 \
    --gate "TDD red: distribution inspector absent" -- \
    /bin/zsh -lc '! PYTHONPATH=src python3.11 -m unittest tests.test_distribution -v'
  ```

  Expected: outer command exits `0`; unittest output shows failure caused by the missing inspector, not a fixture syntax error.

- [ ] **Step 3: Implement the minimum metadata and inspection contract**

  Change `pyproject.toml` to:

  ```toml
  [build-system]
  requires = ["setuptools==83.0.0"]
  build-backend = "setuptools.build_meta"

  [project]
  name = "agentflow-proof"
  dependencies = []

  [project.scripts]
  agentflow = "agentflow.cli:main"
  agentflow-mcp = "agentflow.mcp_server:main"
  ```

  Add `MANIFEST.in`:

  ```text
  include CHANGELOG.md
  ```

  Implement `scripts/check_distribution.py` with these exact public functions:

  ```python
  class DistributionCheckError(ValueError):
      """A built wheel or sdist violates the repository packaging contract."""

  def inspect_distribution(root: Path, dist_dir: Path) -> tuple[Path, Path]:
      """Validate and return the sole wheel and sdist in dist_dir."""

  def main(argv: Sequence[str] | None = None) -> int:
      """Print one concise success/error line and return a process status."""
  ```

  Use only `argparse`, `configparser`, `email.parser`, `re`, `tarfile`, `tomllib`, `zipfile`, and `pathlib`. Read archives in place; never extract them. Normalize distribution names with `re.sub(r"[-_.]+", "_", name).lower()`. Require exact filenames, exact `Name`/`Version`, no `Requires-Dist`, exact console-script mapping, all checked-in package `.py` files, wheel top-level paths limited to `agentflow/` and its sole `.dist-info/`, wheel license inclusion, and sdist documents/source inclusion. Catch `OSError`, invalid TOML, bad ZIP/TAR, and `DistributionCheckError`; print `distribution check failed: ...` without a traceback.

- [ ] **Step 4: Run the focused tests and prove GREEN**

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P1 --agent codex-ticket-6 \
    --gate "focused distribution contract tests" -- \
    env PYTHONPATH=src python3.11 -m unittest tests.test_distribution -v
  ```

  Expected: all `tests.test_distribution` cases pass.

- [ ] **Step 5: Build and inspect real artifacts with pinned tools**

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P1 --agent codex-ticket-6 \
    --gate "build and inspect wheel and sdist" -- /bin/zsh -lc '
      python3.11 scripts/build_zipapp.py --output-dir dist
      uv run --no-project --with build==1.5.0 python -m build --sdist --wheel --outdir dist
      python3.11 scripts/check_distribution.py --dist-dir dist
      uv run --no-project --with twine==6.2.0 python -m twine check dist/*.whl dist/*.tar.gz
    '
  ```

  Expected: exactly one wheel and one sdist pass both inspectors; both zipapps also exist in `dist/`.

- [ ] **Step 6: Record files, verify P1, complete it, and commit**

  ```bash
  for path in MANIFEST.in docs/superpowers/plans/2026-07-19-pypi-distribution-track-b.md pyproject.toml scripts/check_distribution.py tests/test_distribution.py; do
    PYTHONPATH=src python3 -m agentflow record-file-change --step P1 --agent codex-ticket-6 --path "$path"
  done
  PYTHONPATH=src python3 -m agentflow verify-step P1 --agent codex-ticket-6
  PYTHONPATH=src python3 -m agentflow complete-step P1 --agent codex-ticket-6
  git add MANIFEST.in docs/superpowers/plans/2026-07-19-pypi-distribution-track-b.md pyproject.toml scripts/check_distribution.py tests/test_distribution.py
  git commit -m "Add Python distribution artifact checks"
  ```

---

### Task 2: Build-Once Release Workflow and Installed Smokes

**Files:**

- Modify: `.github/workflows/release.yml`
- Modify: `tests/test_release.py`

**Interfaces:**

- Consumes: the wheel, sdist, and zipapps produced in Task 1.
- Produces: one workflow artifact named `release-distributions` containing exactly those four files.
- Produces jobs `guard`, `build`, `clean-install-wheel`, `clean-install-sdist`, `github-release`, and `publish-pypi`.

- [ ] **Step 1: Claim P2 and write failing workflow-contract tests**

  ```bash
  PYTHONPATH=src python3 -m agentflow claim-step P2 --agent codex-ticket-6
  ```

  Extend `RepositoryReleaseDisciplineTests` with assertions that slice each job block and prove:

  ```python
  self.assertIn("\n  build:", workflow)
  self.assertIn("\n  clean-install-wheel:", workflow)
  self.assertIn('python-version: ["3.11", "3.12", "3.13"]', wheel_job)
  self.assertIn("--no-index", wheel_job)
  self.assertIn("agentflow-mcp", wheel_job)
  self.assertIn("tests/fixtures/compatibility/released-v0.4.0", wheel_job)
  self.assertIn("--no-build-isolation", sdist_job)
  self.assertIn("setuptools==83.0.0", sdist_job)
  self.assertIn("if: false", publish_job)
  self.assertIn("id-token: write", publish_job)
  self.assertIn("environment: pypi", publish_job)
  self.assertIn("github-release", publish_job)
  self.assertNotIn("password:", publish_job)
  self.assertNotIn("*.pyz pypi-dist", publish_job)
  ```

  Also assert exact action SHAs, one upload in `build`, downloads in every consumer, four-file checksum coverage, `SOURCE_DATE_EPOCH`, `build==1.5.0`, `twine==6.2.0`, and that publication stages only `*.whl` and `*.tar.gz`.

- [ ] **Step 2: Run the focused workflow test and prove RED**

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P2 --agent codex-ticket-6 \
    --gate "TDD red: release workflow is still monolithic" -- \
    /bin/zsh -lc '! PYTHONPATH=src python3.11 -m unittest tests.test_release.RepositoryReleaseDisciplineTests -v'
  ```

  Expected: outer command exits `0`; failures name missing Track B jobs or gates.

- [ ] **Step 3: Replace the monolithic release job with the exact data flow**

  Keep `guard`. Add:

  - `build` needing `guard`, checking out the tag, setting `SOURCE_DATE_EPOCH` from `git show -s --format=%ct HEAD`, installing pinned CI tools, running the full source suite, building zipapps plus `python -m build --sdist --wheel`, running `check_distribution.py` and `twine check`, preserving zipapp CLI/MCP smokes, and uploading exactly four paths as `release-distributions`.
  - `clean-install-wheel` needing `build`, with Ubuntu/Python 3.11-3.13 matrix, checkout for the compatibility fixture, a fresh `$RUNNER_TEMP/venv`, artifact download, an unambiguous wheel array, `pip install --no-index`, `agentflow --version`, `agentflow --help`, MCP `initialize` over `agentflow-mcp` stdio, and installed `agentflow verify-proof --root tests/fixtures/compatibility/released-v0.4.0`.
  - `clean-install-sdist` needing `build`, Python 3.11, a fresh venv, exact `setuptools==83.0.0` preinstall, unambiguous sdist array, `pip install --no-index --no-build-isolation`, and CLI version smoke.
  - `github-release` needing `guard`, both install jobs, downloading the original bundle, extracting CHANGELOG notes, hashing the four distributables, and attaching the same files plus `SHA256SUMS`; only this job has `contents: write`.
  - `publish-pypi` needing `guard`, both install jobs, and `github-release`; literal `if: false` with an Issue #5 comment; `environment: pypi`; only `id-token: write`; download of the same bundle; copy only wheel/sdist to `pypi-dist/`; and the pinned trusted-publishing action with no password.

- [ ] **Step 4: Run the focused workflow tests and prove GREEN**

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P2 --agent codex-ticket-6 \
    --gate "focused release workflow tests" -- \
    env PYTHONPATH=src python3.11 -m unittest tests.test_release -v
  ```

  Expected: all release guard and Track B workflow assertions pass.

- [ ] **Step 5: Clean-install the wheel on each supported Python**

  For each executable `python3.11`, `python3.12`, and `python3.13`, run this through the matching Agentflow gate after substituting the executable and gate label:

  ```bash
  install_root=$(mktemp -d /private/tmp/agentflow-wheel-311.XXXXXX)
  python3.11 -m venv "$install_root"
  "$install_root/bin/python" -m pip install --no-index dist/agentflow_proof-0.4.0-py3-none-any.whl
  "$install_root/bin/agentflow" --version
  "$install_root/bin/agentflow" --help >/dev/null
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    | "$install_root/bin/agentflow-mcp" | grep -q '"serverInfo"'
  "$install_root/bin/agentflow" verify-proof \
    --root tests/fixtures/compatibility/released-v0.4.0
  ```

  Expected for every interpreter: install uses only the explicit wheel, CLI and MCP smokes succeed, and the installed verifier accepts the released-v0.4.0 fixture.

- [ ] **Step 6: Clean-install the sdist on Python 3.11**

  ```bash
  install_root=$(mktemp -d /private/tmp/agentflow-sdist-311.XXXXXX)
  python3.11 -m venv "$install_root"
  "$install_root/bin/python" -m pip install setuptools==83.0.0
  "$install_root/bin/python" -m pip install --no-index --no-build-isolation dist/agentflow_proof-0.4.0.tar.gz
  "$install_root/bin/agentflow" --version
  ```

  Expected: explicit local sdist installs without build isolation or index fallback for Agentflow.

- [ ] **Step 7: Record files, verify P2, complete it, and commit**

  ```bash
  PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --agent codex-ticket-6 --path .github/workflows/release.yml
  PYTHONPATH=src python3 -m agentflow record-file-change --step P2 --agent codex-ticket-6 --path tests/test_release.py
  PYTHONPATH=src python3 -m agentflow verify-step P2 --agent codex-ticket-6
  PYTHONPATH=src python3 -m agentflow complete-step P2 --agent codex-ticket-6
  git add .github/workflows/release.yml tests/test_release.py
  git commit -m "Build and verify release artifacts once"
  ```

---

### Task 3: Post-#8/#10 Documentation Integration and Final Proof

**Files:**

- Create: `docs/pypi-publishing.md`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/packaging.md`
- Modify: `tests/test_release.py`

**Interfaces:**

- Consumes: merged #8/#10 README/CONTRIBUTING state from current `origin/main`.
- Produces: accurate installation guidance and a non-executing maintainer operation packet.

- [ ] **Step 1: Confirm the dependency lane, fetch, and rebase before editing docs**

  Verify GitHub Issues #8 and #10 are closed by the merged public-project PR and confirm their merge commit is an ancestor of `origin/main`. Then:

  ```bash
  git fetch origin main
  git rebase origin/main
  git status --short --branch
  ```

  Expected: rebase succeeds, working tree is clean, and README/CONTRIBUTING include the merged #8/#10 changes.

- [ ] **Step 2: Claim P3 and write failing documentation-contract tests**

  ```bash
  PYTHONPATH=src python3 -m agentflow claim-step P3 --agent codex-ticket-6
  ```

  Add assertions that README names `agentflow-proof` without saying it is already published, README and CONTRIBUTING preserve `agentflow`/`agentflow-mcp`, packaging docs name all four artifacts and the installed v0.4.0 compatibility command, and `docs/pypi-publishing.md` contains `if: false`, Issue #5, both pending publisher names, `kstruzzieri`, `agentflow`, `release.yml`, `pypi`, required reviewers, no-token guidance, owner-contact evidence fields, and explicit prohibitions on placeholder uploads/external action.

  Prove RED with:

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P3 --agent codex-ticket-6 \
    --gate "TDD red: distribution docs absent" -- \
    /bin/zsh -lc '! PYTHONPATH=src python3.11 -m unittest tests.test_release.RepositoryReleaseDisciplineTests -v'
  ```

- [ ] **Step 3: Integrate the minimum accurate user and maintainer documentation**

  - README: keep “not yet published” until an authorized first upload; name `agentflow-proof` as the built/provisional distribution; show local wheel installation; state a later permanent-name switch changes only `project.name`; keep both command names.
  - CONTRIBUTING: require wheel/sdist plus zipapp build, inspector, clean-install checks, and no publication from contributor work.
  - `docs/packaging.md`: describe all four artifacts, exact build/inspect commands, one-artifact handoff, wheel matrix, sdist backend seam, checksums, installed `verify-proof` compatibility command, and static Issue #5 gate.
  - `docs/pypi-publishing.md`: include prepared contact wording and evidence fields, PEP 541 evidence mapping, a draft support-request body, exact pending-publisher and environment values, protected-environment reviewers, and a first-publication checklist. Mark every external operation maintainer-only and unperformed; forbid empty `agentflow-mcp` placeholder releases.

- [ ] **Step 4: Run documentation, full-suite, and final artifact gates**

  ```bash
  PYTHONPATH=src python3 -m agentflow run --step P3 --agent codex-ticket-6 \
    --gate "release documentation contract tests" -- \
    env PYTHONPATH=src python3.11 -m unittest tests.test_release -v

  PYTHONPATH=src python3 -m agentflow run --step P3 --agent codex-ticket-6 \
    --gate "full repository test suite" -- \
    env PYTHONPATH=src python3.11 -m unittest discover -s tests -v

  PYTHONPATH=src python3 -m agentflow run --step P3 --agent codex-ticket-6 \
    --gate "final distribution artifact validation" -- /bin/zsh -lc '
      python3.11 scripts/check_distribution.py --dist-dir dist
      uv run --no-project --with twine==6.2.0 python -m twine check dist/*.whl dist/*.tar.gz
    '
  ```

  Expected: focused docs checks, the full suite, archive inspection, and metadata inspection all pass on the rebased branch.

- [ ] **Step 5: Record files, verify P3, complete it, and commit**

  ```bash
  for path in README.md CONTRIBUTING.md docs/packaging.md docs/pypi-publishing.md tests/test_release.py; do
    PYTHONPATH=src python3 -m agentflow record-file-change --step P3 --agent codex-ticket-6 --path "$path"
  done
  PYTHONPATH=src python3 -m agentflow verify-step P3 --agent codex-ticket-6
  PYTHONPATH=src python3 -m agentflow complete-step P3 --agent codex-ticket-6
  git add README.md CONTRIBUTING.md docs/packaging.md docs/pypi-publishing.md tests/test_release.py
  git commit -m "Document the fallback distribution path"
  ```

- [ ] **Step 6: Run the full Agentflow closeout and independent review**

  ```bash
  PYTHONPATH=src python3 -m agentflow verify-run
  PYTHONPATH=src python3 -m agentflow audit-drift
  PYTHONPATH=src python3 -m agentflow build-proof
  PYTHONPATH=src python3 -m agentflow verify-proof
  ```

  Dispatch a whole-branch reviewer against `git merge-base origin/main HEAD..HEAD`, fix every Critical/Important finding through an Agentflow amendment, rerun affected tests, then rerun the four closeout commands.

- [ ] **Step 7: Publish only the branch and draft PR**

  Push `codex/ticket-6-pypi-distribution` and create a draft PR into `main`. The PR body must reference #6, summarize build-once handoff, installed smokes, compatibility seam, disabled OIDC gate, and validation. It must explicitly say no PyPI/project/publisher/release/tag/contact/support mutation occurred.
