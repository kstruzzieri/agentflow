# Contributing to Agentflow

Thank you for helping improve Agentflow.

## Development Setup

Agentflow requires Python 3.11 or newer and has no runtime dependencies. Clone
the repository, then either run directly from the source tree:

```bash
PYTHONPATH=src python3 -m agentflow --version
```

or install the checkout into an isolated tool environment:

```bash
uv tool install --editable /path/to/agentflow
```

## Making a Change

1. Open an issue for behavior changes or other work that would benefit from
   agreement before implementation.
2. Use the Agentflow task loop in [docs/agent-workflow.md](docs/agent-workflow.md)
   for planned changes in this repository.
3. Keep changes focused, preserve backward compatibility unless the change is
   explicitly breaking, follow [docs/stability.md](docs/stability.md), and
   update documentation with behavior changes.
4. Add focused tests and run the complete suite:

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   ```

5. For packaging or entry-point changes, build the wheel, sdist, and both
   zipapps once; inspect them; then run the clean-install checks. Do not publish
   from contributor work.

   ```bash
   python3 scripts/build_zipapp.py --output-dir dist
   python3 -m build --sdist --wheel --outdir dist
   python3 scripts/check_distribution.py --dist-dir dist
   python3 -m twine check dist/*.whl dist/*.tar.gz
   python3 -m venv /tmp/agentflow-wheel
   /tmp/agentflow-wheel/bin/python -m pip install --no-index dist/agentflow_proof-*.whl
   /tmp/agentflow-wheel/bin/agentflow --version
   /tmp/agentflow-wheel/bin/agentflow-mcp --help
   python3.11 -m venv /tmp/agentflow-sdist
   /tmp/agentflow-sdist/bin/python -m pip install setuptools==83.0.0
   /tmp/agentflow-sdist/bin/python -m pip install --no-index --no-build-isolation dist/agentflow_proof-*.tar.gz
   /tmp/agentflow-sdist/bin/agentflow --version
   ```

## Pull Requests

Describe the problem, the chosen approach, and the validation performed. Keep
generated root `.agent/` state out of commits unless the contribution is a
deliberately reviewed proof fixture. See
[docs/agent-artifacts.md](docs/agent-artifacts.md) before publishing any proof
artifacts or command output.

Report security issues privately according to [SECURITY.md](SECURITY.md), not in
a public issue.

All participants are expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). For the repository's trust boundaries and
residual operator responsibilities, see
[docs/security-model.md](docs/security-model.md).
