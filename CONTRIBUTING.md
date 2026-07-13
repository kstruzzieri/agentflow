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

5. Build the zipapps when changing packaging or entry-point behavior:

   ```bash
   python3 scripts/build_zipapp.py
   ./dist/agentflow.pyz --version
   ```

## Pull Requests

Describe the problem, the chosen approach, and the validation performed. Keep
generated root `.agent/` state out of commits unless the contribution is a
deliberately reviewed proof fixture. See
[docs/agent-artifacts.md](docs/agent-artifacts.md) before publishing any proof
artifacts or command output.

Report security issues privately according to [SECURITY.md](SECURITY.md), not in
a public issue.
