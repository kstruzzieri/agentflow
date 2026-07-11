#!/usr/bin/env bash
# Reference hook template. Agentflow never executes this file; copy it into the
# host project and wire it up yourself.
set -euo pipefail
PYTHONPATH=src python3 -m unittest discover -s tests
