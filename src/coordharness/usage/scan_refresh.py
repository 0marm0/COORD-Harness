"""Keep the usage scan store current from inside the process that serves it.

The scan store is only as fresh as the last scan that ran against it. Nothing
else refreshes it on a standalone install -- there is no scheduler to rely on,
and a dashboard whose store was last scanned at midnight reports every token
since then as never having been spent. So the service that reads the store also
keeps it current: when the last completed scan is older than a short threshold,
one incremental scan runs in the background while the request is answered from
the last good store.

Three properties matter more than the scan itself:

* Never on the request path. A refresh is started, not awaited.
* One walker. Single-flight inside a process (one thread at most) and across
  processes (a non-blocking OS file lock beside the store), so two dashboards on
  one machine cannot both stat tens of thousands of transcripts at once.
* Freshness is recorded by the scanner, not inferred from the rows. The store's
  own ``scanned_at`` only moves for a file that changed, so an idle machine would
  look hours stale a second after a scan. A completed pass writes a small marker
  beside the store instead, and the payload reports that.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX installs
    fcntl = None  # type: ignore[assignment]

from .scan_store import scan_providers

_logger = logging.getLogger("coordharness.usage.scan")

MARKER_SCHEMA = "usage-scan-refresh.v1"
DEFAULT_STALE_AFTER_SECONDS = 120
# After a failed scan, wait before trying again rather than rescanning on every
# request: a fault that failed once (a full disk, an unreadable store) will
# almost always fail again immediately.
RETRY_AFTER_FAILURE_SECONDS = 60.0
# Another process holds the scan lock, so it is doing this work already.
RETRY_AFTER_LOCKED_SECONDS = 15.0

Scanner = Callable[..., Sequence[Any]]


def marker_path(store: Path) -> Path:
    """The sidecar a completed scan pass writes: ``usage-scan.last-scan.json``."""

    return store.with_suffix(".last-scan.json")


def lock_path(store: Path) -> Path:
    """The cross-process scan lock: ``usage-scan.lock`` beside the store."""

    return store.with_suffix(".lock")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def write_marker(store: Path, providers: Sequence[dict[str, Any]], completed_at: datetime) -> None:
    """Record that a full incremental pass over every provider has completed.

    Written atomically so a reader never sees half a document, and only by a
    caller that still holds the scan lock, so the marker is never newer than
    the rows it vouches for.
    """

    marker = marker_path(store)
    payload = {
        "schema": MARKER_SCHEMA,
        "completed_at": _utc_iso(completed_at),
        "incremental": True,
        "providers": list(providers),
    }
    tmp = marker.with_name(f".{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, marker)


def _read_store(store: Path) -> tuple[bool, datetime | None]:
    """Whether the store holds any scanned file, and its newest ``scanned_at``.

    Read-only and lock-free: a dashboard read must never create or migrate the
    store. Any failure reads as "no history", which makes the caller fall back
    to a live scan rather than report an empty one.
    """

    if not store.is_file():
        return False, None
    try:
        with closing(
            sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=1.0)
        ) as conn:
            count, newest = conn.execute(
                "SELECT COUNT(*), MAX(scanned_at) FROM scanned_file"
            ).fetchone()
    except sqlite3.Error:
        return False, None
    return bool(count), _parse_utc(newest)


def last_completed_scan(store: Path) -> datetime | None:
    """When a scan pass last completed, or the best lower bound available.

    Before the first marker exists (a store built by an older release), the
    newest ``scanned_at`` stands in. It can report a quiet store as older than
    it is, never as newer, so the worst case is one redundant incremental scan.
    """

    try:
        document = json.loads(marker_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = None
    completed = _parse_utc(document.get("completed_at")) if isinstance(document, dict) else None
    if completed is not None:
        return completed
    return _read_store(store)[1]


@dataclass(frozen=True)
class StoreFreshness:
    """How current the history behind every token and dollar figure is.

    ``state`` is one of:

    * ``fresh`` -- the store's last completed scan is within the threshold.
    * ``stale`` -- the store is older than that; a refresh may be running.
    * ``building`` -- no store history yet and the first scan is running; the
      payload is serving a bounded live scan of the most recent files.
    * ``unavailable`` -- no store history and nothing is building one.
    """

    state: str
    last_scan_at: datetime | None
    age_seconds: int | None
    stale_after_seconds: int
    refreshing: bool
    serving: str
    error_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "state": self.state,
            "last_scan_at": _utc_iso(self.last_scan_at) if self.last_scan_at else None,
            "age_seconds": self.age_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "refreshing": self.refreshing,
            "serving": self.serving,
            "semantics": "local_transcript_scan_store_freshness",
        }
        if self.error_code is not None:
            document["error_code"] = self.error_code
        return document


def assess_store(
    store: Path,
    now: datetime,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    refreshing: bool = False,
    building: bool = False,
    error_code: str | None = None,
) -> StoreFreshness:
    """Describe the store's freshness without touching it."""

    has_history, _newest = _read_store(store)
    last_scan = last_completed_scan(store) if has_history else None
    age = (
        max(0, int((now.astimezone(timezone.utc) - last_scan).total_seconds()))
        if last_scan is not None
        else None
    )
    if building or not has_history:
        state = "building" if building else "unavailable"
        serving = "live_bounded_scan"
    else:
        state = "fresh" if age is not None and age <= stale_after_seconds else "stale"
        serving = "scan_store"
    return StoreFreshness(
        state=state,
        last_scan_at=last_scan,
        age_seconds=age,
        stale_after_seconds=int(stale_after_seconds),
        refreshing=refreshing,
        serving=serving,
        error_code=error_code,
    )


@contextmanager
def scan_lock(store: Path, *, blocking: bool) -> Iterator[bool]:
    """Hold the cross-process scan lock; yields whether it was acquired.

    Released on exit and by the kernel if the process dies, so a crashed
    scanner can never wedge the next one. On a platform without ``fcntl`` this
    degrades to process-local single flight, which the caller still provides.
    """

    if fcntl is None:  # pragma: no cover - non-POSIX
        yield True
        return
    path = lock_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ScanStoreRefresher:
    """Start at most one background incremental scan when the store is stale."""

    def __init__(
        self,
        *,
        home: Path,
        store_path: Path,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        scanner: Scanner = scan_providers,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        if not 1 <= float(stale_after_seconds) <= 3600:
            raise ValueError("scan store stale threshold must be in [1, 3600] seconds")
        self._home = Path(home)
        self._store = Path(store_path)
        self._stale_after = int(stale_after_seconds)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._scanner = scanner
        self._on_complete = on_complete
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._building = False
        self._not_before = 0.0
        self._error_code: str | None = None

    @property
    def store_path(self) -> Path:
        return self._store

    @property
    def refreshing(self) -> bool:
        with self._lock:
            return self._thread is not None

    @property
    def building(self) -> bool:
        """True while the first scan of a store with no history is running."""

        with self._lock:
            return self._thread is not None and self._building

    def freshness(self) -> StoreFreshness:
        with self._lock:
            refreshing = self._thread is not None
            building = refreshing and self._building
            error_code = self._error_code
        return assess_store(
            self._store,
            self._now(),
            stale_after_seconds=self._stale_after,
            refreshing=refreshing,
            building=building,
            error_code=error_code,
        )

    def _is_due(self) -> bool:
        last = last_completed_scan(self._store)
        if last is None:
            return True
        age = (self._now().astimezone(timezone.utc) - last).total_seconds()
        return age > self._stale_after

    def request_refresh(self) -> bool:
        """Start a background scan if one is due; never waits for it.

        Returns whether a scan thread was started by this call. There is no
        "force": the threshold is two minutes, and a scan the store does not
        need would only compete with the request for the same disk.
        """

        with self._lock:
            if self._thread is not None:
                return False
            if self._monotonic() < self._not_before:
                return False
        # Outside the lock: this reads a marker file and possibly the store.
        if not self._is_due():
            return False
        has_history = _read_store(self._store)[0]
        with self._lock:
            if self._thread is not None:
                return False
            self._building = not has_history
            thread = threading.Thread(
                target=self._run, name="coord-usage-scan-refresh", daemon=True
            )
            self._thread = thread
        thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until any running scan finishes; for tests and shutdown."""

        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        completed = False
        retry_after = 0.0
        error_code: str | None = None
        try:
            with scan_lock(self._store, blocking=False) as acquired:
                if not acquired:
                    retry_after = RETRY_AFTER_LOCKED_SECONDS
                elif self._is_due():
                    # Re-checked under the lock: another process may have
                    # finished a scan between our decision and our lock.
                    started = self._monotonic()
                    results = self._scanner(self._home, store_path=self._store)
                    write_marker(
                        self._store,
                        [result.summary() for result in results],
                        self._now(),
                    )
                    completed = True
                    _logger.info(
                        "usage scan refresh: completed in %.1fs", self._monotonic() - started
                    )
        except Exception:  # noqa: BLE001 - reported in the payload, never raised to a reader
            _logger.warning("usage scan refresh failed", exc_info=True)
            error_code = "scan_refresh_failed"
            retry_after = RETRY_AFTER_FAILURE_SECONDS
        with self._lock:
            self._thread = None
            self._building = False
            self._error_code = error_code
            self._not_before = self._monotonic() + retry_after
        if completed and self._on_complete is not None:
            try:
                self._on_complete()
            except Exception:  # noqa: BLE001 - a listener fault must not poison the refresher
                _logger.warning("usage scan refresh listener failed", exc_info=True)


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "MARKER_SCHEMA",
    "ScanStoreRefresher",
    "StoreFreshness",
    "assess_store",
    "last_completed_scan",
    "lock_path",
    "marker_path",
    "scan_lock",
    "write_marker",
]
