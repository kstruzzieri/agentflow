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

Installing an sdist still builds it. `--no-build-isolation` reuses the preinstalled pinned backend and, with `--no-index`, avoids isolated-build dependency resolution/index fallback. The backend pin lives only in `pyproject.toml` `[build-system].requires`; the commands read it from there:

```bash
python3.11 -m venv /tmp/agentflow-sdist
/tmp/agentflow-sdist/bin/python -m pip install $(python3.11 -c 'import tomllib; print(*tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"])')
/tmp/agentflow-sdist/bin/python -m pip install --no-index --no-build-isolation dist/agentflow_proof-*.tar.gz
/tmp/agentflow-sdist/bin/agentflow --version
```

`publish-pypi` in `.github/workflows/release.yml` runs on every `v*` tag once
a required reviewer approves the protected `pypi` environment; the Issue #5
`if: false` gate was removed after the 1.0.0 schema freeze completed. It stages
only the wheel and sdist; the zipapps remain release assets.

### Release inputs that are not pinned by content

Check the build job in the revision being released. Revisions that install
`build` and `twine` by version also resolve build dependencies without hash
checks. [PR #169](https://github.com/kstruzzieri/agentflow/pull/169) pins the
build and metadata-check tools by hash. These two inputs remain outside those
hash checks:

- `clean-install-sdist` installs the build backend by version, which it reads
  from the sdist under test. The job runs after the artifacts are uploaded for
  the later jobs and holds no publishing permission.
- `pypa/gh-action-pypi-publish` is pinned to a commit, but that commit runs a
  container image chosen by a registry tag named after the commit. Whoever
  controls the registry namespace can repoint a tag, so the image contents are
  trusted to the PyPA project.

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

5. Look at the README the way PyPI will show it. `twine check` only confirms
   that the long description parses. Render the built wheel's description with
   PyPI's own renderer, in a throwaway virtual environment outside the
   repository:

   ```bash
   python3 -m venv /tmp/agentflow-render
   /tmp/agentflow-render/bin/pip install build==1.5.0 'readme_renderer[md]'
   /tmp/agentflow-render/bin/python -m build --wheel --outdir /tmp/agentflow-render/dist
   /tmp/agentflow-render/bin/python - <<'EOF'
   import email, glob, zipfile
   from readme_renderer.markdown import render
   wheel = glob.glob("/tmp/agentflow-render/dist/agentflow_proof-*.whl")[0]
   with zipfile.ZipFile(wheel) as z:
       name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
       msg = email.message_from_bytes(z.read(name))
   html = render(msg.get_payload(decode=True).decode("utf-8"))
   open("/tmp/agentflow-render/readme.html", "w", encoding="utf-8").write(html)
   EOF
   ```

   Open `/tmp/agentflow-render/readme.html` in a browser and check that the
   logo, the brand badge and the four status badges load, that the header
   block is centered, and that every link opens the page it names. PyPI drops
   the `<source>` inside `<picture>`, so only the light logo appears there.

6. Commit the version and CHANGELOG changes through a normal pull request.
7. After that commit reaches `main`, validate the intended tag before creating
   it:

   ```bash
   python3 scripts/check_release.py --tag vX.Y.Z
   ```

8. Create and push the exact validated tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

9. The `Release` workflow validates the tag, both version declarations, and
   CHANGELOG before it runs tests or builds artifacts. It builds and smokes the
   zipapps, generates `SHA256SUMS`, and creates the GitHub release using the
   matching CHANGELOG section as its notes.
10. Download `agentflow.pyz`, `agentflow-mcp.pyz`,
    `agentflow_proof-*.whl`, `agentflow_proof-*.tar.gz`, and `SHA256SUMS` from
    the release; run `sha256sum -c SHA256SUMS`, then
    `python3 agentflow.pyz --version`.
11. After a reviewer approves the `pypi` environment and the upload finishes,
    open https://pypi.org/project/agentflow-proof/ and confirm that the
    project page shows the new version with the logo, badges and working links
    seen in step 5.

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
