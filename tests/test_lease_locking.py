from __future__ import annotations

import multiprocessing
import subprocess
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from agentflow.artifacts import create_initial_artifacts, write_json
from agentflow.execution import claim_step, init_execution_artifacts, read_step_state

WORKERS = 8


def _plan() -> dict:
    return {
        "schema_version": "0.3.0",
        "objective": "Exercise concurrent claims.",
        "scope": ["Claim the same step concurrently."],
        "non_goals": [],
        "invariants": ["Attempt ids are unique."],
        "allowed_files": [".agent/"],
        "blocked_files": [],
        "validation_gates": ["python3 -c \"print('ok')\""],
        "rollback_plan": "Discard temp worktree.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0, "new_dependencies": 0,
            "formatting_drift": "minimal", "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [{
            "id": "P1", "action": "Claim.", "files": [".agent/"],
            "preconditions": ["Locked."], "expected_diff": ["Claim recorded."],
            "validation": ["python3 -c \"print('ok')\""], "evidence_ids": [],
        }],
        "evidence_ids": [], "locked": True, "locked_at": "2026-06-01T00:00:00+00:00",
    }


def _claim_worker(args) -> str | None:
    root_str, index, barrier = args
    root = Path(root_str)
    barrier.wait(timeout=30)
    try:
        return claim_step(root, _plan(), "P1", f"agent-{index}")["attempt_id"]
    except ValueError:
        return None  # advisory allows all; this path is for the enforce test in Task 2


class LedgerSafetyTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        subprocess.run(["git", "init"], cwd=str(root), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", _plan())
        return root

    def test_concurrent_advisory_claims_never_share_an_attempt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            with multiprocessing.Manager() as manager:
                barrier = manager.Barrier(WORKERS)
                args = [(str(root), i, barrier) for i in range(WORKERS)]
                with ProcessPoolExecutor(max_workers=WORKERS) as pool:
                    results = [r for r in pool.map(_claim_worker, args) if r]
            # Advisory: all claims succeed, but every attempt id is distinct.
            self.assertEqual(len(results), WORKERS)
            self.assertEqual(len(set(results)), len(results),
                             f"duplicate attempt ids under concurrent claim: {results}")
            state = read_step_state(root)
            self.assertEqual(len(state["attempts"]), WORKERS)


if __name__ == "__main__":
    unittest.main()
