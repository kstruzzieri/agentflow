"""Create two independent Agentflow writers, aggregate them, and verify proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": str(REPO / "src")}


def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, env=ENV, check=True, text=True)


def agentflow(cwd: Path, *args: str) -> None:
    run(cwd, sys.executable, "-m", "agentflow", *args)


def writer(root: Path, name: str, step: str, plan_source: Path | None = None) -> None:
    path = f"src/{name}.txt"
    agentflow(root, "init")
    plan_path = root / ".agent/plan.lock.json"
    if plan_source is not None:
        plan_path.write_text(plan_source.read_text())
    else:
        plan = json.loads(plan_path.read_text())
        plan.update({
        "objective": "Two-writer aggregation example",
        "scope": ["Create each writer output"],
        "invariants": ["One writer owns one worktree."],
        "allowed_files": ["src/", ".agent/"],
        "validation_gates": ["python3 -c pass"],
        "rollback_plan": f"Delete {path}.",
        "steps": [
            {"id": "P1", "action": "Create src/writer-a.txt.", "files": ["src/writer-a.txt"],
             "preconditions": ["worktree initialized"], "expected_diff": ["src/writer-a.txt"],
             "validation": ["python3 -c pass"], "evidence_ids": []},
            {"id": "P2", "action": "Create src/writer-b.txt.", "files": ["src/writer-b.txt"],
             "preconditions": ["worktree initialized"], "expected_diff": ["src/writer-b.txt"],
             "validation": ["python3 -c pass"], "evidence_ids": []},
        ],
        })
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")
        agentflow(root, "lock-plan", ".agent/plan.lock.json")
    agentflow(root, "init-execution")
    agentflow(root, "claim-step", step, "--agent", name)
    (root / "src").mkdir(exist_ok=True)
    (root / path).write_text(f"{name}\n")
    agentflow(root, "record-file-change", "--step", step, "--path", path)
    agentflow(root, "run", "--step", step, "--gate", "python3 -c pass", "--", "python3", "-c", "pass")
    agentflow(root, "verify-step", step)
    agentflow(root, "complete-step", step)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agentflow-aggregation-") as temp:
        base = Path(temp) / "canonical"
        base.mkdir()
        run(base, "git", "init", "-q")
        run(base, "git", "config", "user.email", "example@agentflow.invalid")
        run(base, "git", "config", "user.name", "agentflow-example")
        (base / "README.md").write_text("aggregation example\n")
        run(base, "git", "add", "README.md")
        run(base, "git", "commit", "-qm", "seed")
        a, b = Path(temp) / "writer-a", Path(temp) / "writer-b"
        run(base, "git", "worktree", "add", "-q", "-b", "writer-a", str(a), "HEAD")
        run(base, "git", "worktree", "add", "-q", "-b", "writer-b", str(b), "HEAD")
        (base / "src").mkdir()
        (base / "src/writer-a.txt").write_text("writer-a\n")
        (base / "src/writer-b.txt").write_text("writer-b\n")
        writer(a, "writer-a", "P1")
        writer(b, "writer-b", "P2", a / ".agent/plan.lock.json")
        common = ("aggregate-ledgers", "--input", str(a), "--source-id", "writera",
                  "--input", str(b), "--source-id", "writerb", "--output", str(base), "--base", "HEAD")
        agentflow(base, *common, "--dry-run")
        agentflow(base, *common)
        agentflow(base, "verify-run")
        agentflow(base, "build-proof")
        agentflow(base, "verify-proof")
    print("aggregation example passed")


if __name__ == "__main__":
    main()
