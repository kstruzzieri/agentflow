# Packaging and Releases

Agentflow ships as source plus two Python distribution artifacts and two
single-file zipapp artifacts. There are no runtime dependencies.

## Build-once distribution handoff

Build these four artifacts once from the tagged source, then pass those exact
bytes to every inspection, clean-install, GitHub Release, and (when separately
authorized) PyPI stage:

- `dist/agentflow.pyz` — CLI zipapp
- `dist/agentflow-mcp.pyz` — MCP zipapp
- `dist/agentflow_proof-*.whl` — provisional `agentflow-proof` wheel
- `dist/agentflow_proof-*.tar.gz` — provisional `agentflow-proof` sdist

```bash
python3 -m pip install build==1.5.0 twine==6.2.0
python3 scripts/build_zipapp.py --output-dir dist
python3 -m build --sdist --wheel --outdir dist
python3 scripts/check_distribution.py --dist-dir dist
python3 -m twine check dist/*.whl dist/*.tar.gz
```

`check_distribution.py` verifies the wheel/sdist metadata, files, and console
scripts. The release workflow uploads this one-artifact handoff and creates
`SHA256SUMS` for all four artifacts; consumers verify it with
`sha256sum -c SHA256SUMS`.

The wheel clean-installs on Python 3.11, 3.12, and 3.13. The sdist clean-installs
on Python 3.11 with the pinned backend. The installed wheel must also preserve
the released-v0.4.0 proof contract:

```bash
agentflow verify-proof --root tests/fixtures/compatibility/released-v0.4.0
```

### Python 3.11 sdist seam

Use the pinned backend and the built local sdist; `--no-index` and
`--no-build-isolation` prevent an accidental rebuild or dependency lookup:

```bash
python3.11 -m venv /tmp/agentflow-sdist
/tmp/agentflow-sdist/bin/python -m pip install setuptools==83.0.0
/tmp/agentflow-sdist/bin/python -m pip install --no-index --no-build-isolation dist/agentflow_proof-*.tar.gz
/tmp/agentflow-sdist/bin/agentflow --version
```

`publish-pypi` in `.github/workflows/release.yml` remains `if: false` while
Issue #5's compatibility freeze is incomplete. It stages only the wheel and
sdist; the zipapps remain release assets.

## Single-file builds (zipapp)

Build both artifacts from a checkout:

```bash
python3 scripts/build_zipapp.py
```

This produces:

- `dist/agentflow.pyz` — the CLI (`agentflow ...`)
- `dist/agentflow-mcp.pyz` — the MCP server (stdio/http transports)

Build one artifact with `--only agentflow` or `--only agentflow-mcp`, and
choose a different destination with `--output-dir`.

Run them directly:

```bash
./dist/agentflow.pyz --version
python3 dist/agentflow.pyz status --json
python3 dist/agentflow-mcp.pyz --transport stdio
```

On Windows, use `python dist\agentflow.pyz ...` (the `.pyz` extension is
associated with the launcher by the standard Python installer).

### How it works

A `.pyz` is a zip archive with a `#!/usr/bin/env python3` shebang. The build
script stages `src/agentflow/`, adds a `__main__.py` that checks for
Python >= 3.11 before importing anything, and archives it with the stdlib
`zipapp` module.

### Limitations

- A system Python >= 3.11 must be on `PATH`. Older interpreters get a clear
  error message instead of a traceback.
- The artifact is a snapshot: rebuild after pulling changes.
- Builds are not byte-for-byte reproducible (zip entries carry file mtimes).

## Release checklist

The release guard requires Python 3.11 or newer because it reads
`pyproject.toml` with the standard-library `tomllib` module.

1. On `main`, update the version in `pyproject.toml`.
2. Update the same version in `src/agentflow/__init__.py`.
3. Move the relevant notes under `CHANGELOG.md`'s `Unreleased` heading into
   a dated `## [X.Y.Z] - YYYY-MM-DD` release heading, leaving an empty
   `Unreleased` heading for future changes. Move the notes rather than copying
   them: the guard fails the release if the `Unreleased` section is not empty at
   tag time. In the same edit, update the CHANGELOG footer reference links —
   point the `[Unreleased]` link at `vX.Y.Z...HEAD` and add a `[X.Y.Z]` link for
   the new release tag.
4. Run the version check and full suite:

   ```bash
   python3 scripts/check_release.py
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   ```

5. Commit the version and CHANGELOG changes through a normal pull request.
6. After that commit reaches `main`, validate the intended tag before creating
   it:

   ```bash
   python3 scripts/check_release.py --tag vX.Y.Z
   ```

7. Create and push the exact validated tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

8. The `Release` workflow validates the tag, both version declarations, and
   CHANGELOG before it runs tests or builds artifacts. It builds and smokes the
   zipapps, generates `SHA256SUMS`, and creates the GitHub release using the
   matching CHANGELOG section as its notes.
9. Download `agentflow.pyz`, `agentflow-mcp.pyz`, and `SHA256SUMS` from the
   release; run `sha256sum -c SHA256SUMS`, then
   `python3 agentflow.pyz --version`.

> **Do not bump the version ahead of a release.** Ordinary CI checks only that
> the version declarations agree and permits `## [Unreleased]` to contain
> pending notes. The `check_release.py --tag` guard and tag-triggered `Release`
> workflow additionally require the matching dated CHANGELOG heading and an
> empty `Unreleased` section. Change the version only in the release pull
> request, together with the CHANGELOG move in step 3.

## Phase 2: standalone binaries (PyInstaller)

The zipapp still requires a system Python. Phase 2 packages the interpreter
too (PyInstaller one-file mode), which needs a per-OS build matrix
(macOS/Linux/Windows) and a signing/notarization story.

Trigger criteria for starting phase 2:

- A consumer needs to run Agentflow on machines without Python 3.11, or
- Install friction reports from zipapp users.

Until then, zipapp is the supported single-file distribution.
