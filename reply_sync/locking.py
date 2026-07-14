from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


class ReplySyncAlreadyRunning(RuntimeError):
    pass


class ReplySyncLock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "ReplySyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ReplySyncAlreadyRunning("Reply synchronization is already running") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
        return self

    def __exit__(self, *_: object) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()
