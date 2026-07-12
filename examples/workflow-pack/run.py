"""Run the shipped docs-only pack from recommendation through proof."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": str(REPO / "src")}

def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, env=ENV, check=True, text=True)

def af(cwd: Path, *args: str) -> None:
    run(cwd, sys.executable, "-m", "agentflow", *args)

def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agentflow-workflow-pack-") as temp:
        root = Path(temp) / "checkout"
        shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(".git", ".agent", ".claude", "__pycache__"))
        run(root, "git", "init", "-q")
        run(root, "git", "config", "user.email", "example@agentflow.invalid")
        run(root, "git", "config", "user.name", "agentflow-example")
        run(root, "git", "add", "-A")
        run(root, "git", "commit", "-qm", "baseline")
        af(root, "recommend-workflow", "--brief", "examples/briefs/docs-only.brief.json")
        af(root, "draft-plan", "--brief", "examples/briefs/docs-only.brief.json", "--workflow", "examples/packs/agentflow-draft-demo", "--objective", "Document the workflow-pack walkthrough", "--write")
        af(root, "lock-plan", ".agent/plan.lock.json")
        af(root, "init-execution")
        af(root, "claim-step", "P1", "--agent", "workflow-example")
        with (root / "docs/roadmap.md").open("a") as handle:
            handle.write("\nWorkflow-pack example receipt.\n")
        af(root, "record-file-change", "--step", "P1", "--path", "docs/roadmap.md")
        af(root, "run", "--step", "P1", "--gate", "docs-build", "--", "python3", "-c", "pass")
        af(root, "verify-step", "P1")
        af(root, "complete-step", "P1")
        af(root, "audit-drift")
        af(root, "verify-run")
        af(root, "build-proof")
        af(root, "verify-proof")
    print("workflow-pack example passed")

if __name__ == "__main__":
    main()
