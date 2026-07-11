"""Regression tests for receipt id allocation under parallel writes (issue #33).

`agentflow record-file-change` / `record-command` allocated receipt ids with a
non-atomic read-count-then-append (`FR{len(ledger)+1}`), so two concurrent
invocations could mint the same id (observed: duplicate `FR12`). These tests
spawn real OS processes that hammer the same ledger simultaneously and assert
that every id is unique.
"""

from __future__ import annotations

import multiprocessing
import subprocess
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from agentflow.artifacts import create_initial_artifacts, write_json
from agentflow.execution import claim_step, init_execution_artifacts

WORKERS = 8
WRITES_PER_WORKER = 20
# Bound the barrier so a worker dying before the rendezvous surfaces as a test
# error (BrokenBarrierError) instead of deadlocking the whole run.
BARRIER_TIMEOUT = 30


def _plan() -> dict:
    return {
        "schema_version": "0.3.0",
        "objective": "Exercise parallel receipt writes.",
        "scope": ["Hammer the receipt ledgers concurrently."],
        "non_goals": [],
        "invariants": ["Receipt ids are unique."],
        "allowed_files": ["*.txt", ".agent/"],
        "blocked_files": [],
        "validation_gates": ["python3 -c \"print('ok')\""],
        "rollback_plan": "Discard the temp worktree.",
        "risk_level": "low",
        "drift_budget": {
            "unrelated_edits": 0,
            "new_dependencies": 0,
            "formatting_drift": "minimal",
            "architecture_drift": "requires_approval",
            "test_weakening": 0,
        },
        "steps": [
            {
                "id": "P1",
                "action": "Record concurrent receipts.",
                "files": ["*.txt"],
                "preconditions": ["Claimed."],
                "expected_diff": ["Receipts recorded."],
                "validation": ["python3 -c \"print('ok')\""],
                "evidence_ids": [],
            }
        ],
        "evidence_ids": [],
        "locked": True,
        "locked_at": "2026-06-01T00:00:00+00:00",
    }


def _command_worker(args) -> list:
    root_str, barrier = args
    from agentflow.receipts import record_command

    root = Path(root_str)
    ids = []
    barrier.wait(timeout=BARRIER_TIMEOUT)
    for _ in range(WRITES_PER_WORKER):
        receipt = record_command(root, "P1", "A1", ["true"], 0)
        ids.append(receipt["id"])
    return ids


def _file_worker(args) -> list:
    root_str, index, barrier = args
    from agentflow.receipts import record_file_change

    root = Path(root_str)
    path = f"f{index}.txt"
    (root / path).write_text(f"worker {index}\n", encoding="utf-8")
    plan = _plan()
    ids = []
    barrier.wait(timeout=BARRIER_TIMEOUT)
    for _ in range(WRITES_PER_WORKER):
        receipt = record_file_change(root, plan, "P1", "A1", path)
        ids.append(receipt["id"])
    return ids


class ReceiptConcurrencyTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        subprocess.run(
            ["git", "init"],
            cwd=str(root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        create_initial_artifacts(root)
        init_execution_artifacts(root)
        write_json(root / ".agent/plan.lock.json", _plan())
        claim_step(root, _plan(), "P1", "agent-a")
        return root

    def test_parallel_record_command_ids_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            with multiprocessing.Manager() as manager:
                barrier = manager.Barrier(WORKERS)
                args = [(str(root), barrier) for _ in range(WORKERS)]
                with ProcessPoolExecutor(max_workers=WORKERS) as pool:
                    results = list(pool.map(_command_worker, args))

            ids = [rid for batch in results for rid in batch]
            self.assertEqual(len(ids), WORKERS * WRITES_PER_WORKER)
            self.assertEqual(
                len(set(ids)),
                len(ids),
                f"duplicate command-receipt ids minted: {len(ids) - len(set(ids))} dupes",
            )

    def test_lock_sidecar_not_flagged_as_dependency_drift(self) -> None:
        # The receipt lock sidecar must not be classified as a dependency
        # lockfile by audit_drift; otherwise every record-command/run pollutes
        # the drift notes for projects that track .agent/ (PR #36 review).
        from agentflow.receipts import record_command
        from agentflow.validation import audit_drift

        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            record_command(root, "P1", "A1", ["true"], 0)

            report = audit_drift(root, _plan())

            self.assertEqual(
                report["dependency_changes"],
                [],
                f"lock sidecar wrongly flagged as dependency drift: "
                f"{report['dependency_changes']}",
            )

    def test_parallel_record_file_change_ids_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            with multiprocessing.Manager() as manager:
                barrier = manager.Barrier(WORKERS)
                args = [(str(root), index, barrier) for index in range(WORKERS)]
                with ProcessPoolExecutor(max_workers=WORKERS) as pool:
                    results = list(pool.map(_file_worker, args))

            ids = [rid for batch in results for rid in batch]
            self.assertEqual(len(ids), WORKERS * WRITES_PER_WORKER)
            self.assertEqual(
                len(set(ids)),
                len(ids),
                f"duplicate file-receipt ids minted: {len(ids) - len(set(ids))} dupes",
            )


class _FakeMsvcrt:
    """Minimal msvcrt stand-in: lock contended for the first N attempts."""

    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self, fail_attempts: int) -> None:
        self.fail_attempts = fail_attempts
        self.lock_attempts = 0
        self.unlocks = 0

    def locking(self, fileno, mode, nbytes):  # noqa: ANN001 - mirrors msvcrt
        if mode in (self.LK_LOCK, self.LK_NBLCK):
            self.lock_attempts += 1
            if self.lock_attempts <= self.fail_attempts:
                raise OSError("Resource temporarily unavailable")
        elif mode == self.LK_UNLCK:
            self.unlocks += 1


class FileLockWindowsTests(unittest.TestCase):
    """The msvcrt path must block (retry) like POSIX flock, not give up.

    `msvcrt.locking(.., LK_LOCK, ..)` raises OSError after ~10 retries, so a
    long-held receipt lock would make concurrent writers fail on Windows. The
    lock must retry until acquired and only unlock once acquisition succeeds.
    """

    def _run_with_fake_msvcrt(self, fake: "_FakeMsvcrt") -> None:
        from unittest import mock

        from agentflow import locks

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "ledger.jsonl.lockfile"
            with mock.patch.object(locks, "_HAVE_FCNTL", False), mock.patch.object(
                locks, "_HAVE_MSVCRT", True
            ), mock.patch.object(locks, "msvcrt", fake, create=True), mock.patch(
                "time.sleep"
            ):
                with locks.file_lock(lock_path):
                    pass

    def test_msvcrt_retries_until_lock_acquired(self) -> None:
        fake = _FakeMsvcrt(fail_attempts=3)

        self._run_with_fake_msvcrt(fake)

        self.assertEqual(fake.lock_attempts, 4, "should retry until acquired")
        self.assertEqual(fake.unlocks, 1, "should unlock exactly once after acquiring")


if __name__ == "__main__":
    unittest.main()
