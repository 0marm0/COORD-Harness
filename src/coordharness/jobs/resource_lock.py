"""Process-verifiable file locks for exclusive local resources.

Environment variables are only assertions.  This module keeps the operating
system lock descriptor open for the lifetime of the authority object and
verifies its inode and random ownership token before a protected operation.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX installs
    fcntl = None
import json
import os
from pathlib import Path
import secrets
from typing import Any

from coordharness import config


class ResourceLockError(RuntimeError):
    """Raised when an exclusive local resource lock cannot be verified."""


class ResourceLock:
    def __init__(self, resource: str, *, path: str | Path | None = None) -> None:
        clean = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in resource).strip(".-")
        if not clean:
            raise ValueError("resource lock name must not be empty")
        self.resource = clean
        self.path = Path(path) if path is not None else config.state_dir() / "locks" / f"{clean}.lock"
        self._token = secrets.token_hex(16)
        self._file = None

    @property
    def held(self) -> bool:
        return self._file is not None and not self._file.closed

    def acquire(self, *, blocking: bool = False) -> "ResourceLock":
        if fcntl is None:
            raise ResourceLockError("operating-system file locks are unavailable on this platform")
        if self.held:
            raise ResourceLockError(f"resource {self.resource!r} is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            handle.close()
            raise ResourceLockError(f"resource {self.resource!r} is already locked") from exc
        self._file = handle
        payload = {
            "schema_version": 1,
            "resource": self.resource,
            "pid": os.getpid(),
            "token": self._token,
        }
        handle.seek(0)
        handle.truncate()
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        return self

    def verify(self) -> dict[str, Any]:
        if fcntl is None:
            raise ResourceLockError("operating-system file locks are unavailable on this platform")
        if not self.held:
            raise ResourceLockError(f"resource {self.resource!r} is not held")
        assert self._file is not None
        try:
            path_stat = self.path.stat()
            descriptor_stat = os.fstat(self._file.fileno())
        except OSError as exc:
            raise ResourceLockError(f"resource {self.resource!r} lock file is unavailable") from exc
        if (path_stat.st_dev, path_stat.st_ino) != (descriptor_stat.st_dev, descriptor_stat.st_ino):
            raise ResourceLockError(f"resource {self.resource!r} lock inode changed")
        self._file.seek(0)
        try:
            payload = json.load(self._file)
        except (OSError, json.JSONDecodeError) as exc:
            raise ResourceLockError(f"resource {self.resource!r} lock receipt is invalid") from exc
        if payload.get("pid") != os.getpid() or payload.get("token") != self._token:
            raise ResourceLockError(f"resource {self.resource!r} lock receipt is not owned by this process")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResourceLockError(f"resource {self.resource!r} operating-system lock was lost") from exc
        return {
            "resource": self.resource,
            "authority": "os_file_lock",
            "verified": True,
            "pid": os.getpid(),
        }

    def release(self) -> None:
        if fcntl is None:
            self._file = None
            return
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ResourceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
