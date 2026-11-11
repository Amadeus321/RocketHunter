"""Small cross-process file lock used for local shared quota and memory updates."""
from __future__ import annotations

import os
import time
import json
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked_file(path: Path, poll_seconds: float = 0.1):
    """Hold an exclusive lock on a one-byte file across threads and processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not read the locked byte before acquiring it: Windows rejects reads
    # from another process while that byte range is locked.
    path.touch(exist_ok=True)
    handle = path.open("r+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(poll_seconds)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def shared_rate_limit(state_path: Path, minimum_interval_seconds: float) -> None:
    """Reserve a process-wide request start while keeping calls at the given pace."""
    state_path = Path(state_path)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with locked_file(lock_path):
        try:
            next_allowed = float(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            next_allowed = 0.0
        wait = max(0.0, next_allowed - time.time())
        if wait:
            time.sleep(wait)
        next_allowed = time.time() + max(0.0, float(minimum_interval_seconds))
        temp = state_path.with_suffix(state_path.suffix + ".tmp")
        temp.write_text(json.dumps(next_allowed), encoding="utf-8")
        temp.replace(state_path)
