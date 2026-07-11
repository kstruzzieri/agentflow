"""Cross-platform advisory file locking for serializing ledger writes.

Standard library only, to preserve Agentflow's no-runtime-dependency invariant.
Uses ``fcntl`` on POSIX and ``msvcrt`` on Windows. If neither is available (no
known platform), the lock degrades to a no-op, which preserves prior behavior.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Iterator

# Poll interval for the Windows retry loop (POSIX flock blocks in the kernel and
# needs no polling).
_MSVCRT_RETRY_SECONDS = 0.05

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - non-Windows
    _HAVE_MSVCRT = False


@contextlib.contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` for the block's duration.

    The lock file is created if needed and never truncated; only its kernel lock
    state is used. The lock is released and the descriptor closed on exit, even if
    the body raises. ``fcntl`` locks are released by the kernel if the process
    dies, so a crash cannot leave a stale lock behind.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    acquired = False
    try:
        if _HAVE_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
        elif _HAVE_MSVCRT:  # pragma: no cover - Windows only
            # LK_LOCK gives up with OSError after ~10 retries, so a long-held
            # lock would make concurrent writers fail. Poll a non-blocking lock
            # until acquired to match POSIX flock's indefinite blocking.
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(_MSVCRT_RETRY_SECONDS)
        yield
    finally:
        try:
            if acquired and _HAVE_FCNTL:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif acquired and _HAVE_MSVCRT:  # pragma: no cover - Windows only
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
