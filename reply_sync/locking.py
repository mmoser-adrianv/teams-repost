from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class ReplySyncAlreadyRunning(RuntimeError):
    pass


class ReplySyncLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> "ReplySyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ReplySyncAlreadyRunning("Reply synchronization is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
