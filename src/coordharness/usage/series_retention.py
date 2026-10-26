"""Durable normalized provider receipts, scoped to an opaque account identity."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # non-POSIX hosts retain observations in memory only
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .provider_anchor import ProviderSeries, ProviderSeriesError, parse_account_usage

SCHEMA = "codex-provider-receipts.v3"
MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RetainedSeries:
    series: ProviderSeries
    observed_at: str
    state: str = "fresh"
    reason: str | None = None
    day_observed_at: dict[str, str] = field(default_factory=dict)
    lifetime_observed_at: str | None = None


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def _document(scope: str, value: RetainedSeries) -> dict:
    body = {"schema": SCHEMA, "scope": scope, "observed_at": value.observed_at,
            "state": value.state, "reason": value.reason,
            "day_observed_at": {day: value.day_observed_at.get(day, value.observed_at) for day in value.series.daily},
            "lifetime_observed_at": value.lifetime_observed_at,
            "result": {"summary": {"lifetimeTokens": value.series.lifetime_tokens},
                       "dailyUsageBuckets": [{"startDate": day, "tokens": tokens}
                                             for day, tokens in value.series.daily.items()]}}
    body["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


class SeriesRetention:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._held: dict[str, RetainedSeries] = {}

    def _read(self, scope: str) -> RetainedSeries | None:
        if self.path is None:
            return None
        primary = self._read_path(scope, self.path)
        if primary is not None:
            return primary
        backup = self._read_path(scope, self.path.with_name(self.path.name + ".backup"))
        if backup:
            return RetainedSeries(backup.series, backup.observed_at, "stale_last_good", "provider_series_cache_recovered", backup.day_observed_at, backup.lifetime_observed_at)
        return None

    def _read_path(self, scope: str, path: Path) -> RetainedSeries | None:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                return None
            doc = json.loads(path.read_bytes())
            digest = doc.pop("digest", None)
            if (doc.get("schema") != SCHEMA or doc.get("scope") != scope or
                digest != hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()):
                return None
            stamp = doc["observed_at"]
            if (_instant(stamp) - datetime.now(timezone.utc)).total_seconds() > 300:
                return None
            series = parse_account_usage(doc["result"])
            if (any(value > 2**63-1 for value in series.daily.values()) or
                (series.lifetime_tokens is not None and series.lifetime_tokens > 2**63-1)):
                return None
            state = doc.get("state")
            reason = doc.get("reason")
            if state not in {"fresh", "stale_last_good"}:
                return None
            if reason not in {None, "provider_series_partial_response", "provider_series_cache_recovered"}:
                return None
            day_stamps = doc.get("day_observed_at")
            if not isinstance(day_stamps, dict) or set(day_stamps) != set(series.daily):
                return None
            if any(_instant(value) > _instant(stamp) for value in day_stamps.values()):
                return None
            lifetime_stamp = doc.get("lifetime_observed_at")
            if ((series.lifetime_tokens is None and lifetime_stamp is not None) or
                (series.lifetime_tokens is not None and (lifetime_stamp is None or _instant(lifetime_stamp) > _instant(stamp)))):
                return None
            return RetainedSeries(series, stamp, state, reason, day_stamps, lifetime_stamp)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, ProviderSeriesError):
            return None

    def recall(self, scope: str) -> RetainedSeries | None:
        with self._lock:
            if scope not in self._held:
                disk = self._read(scope)
                if disk:
                    self._held[scope] = disk
            return self._held.get(scope)

    def _prior(self, scope: str) -> RetainedSeries | None:
        disk, held = self._read(scope), self._held.get(scope)
        if not disk or not held:
            return held or disk
        # Compare each day's actual observation; a newer partial fetch cannot
        # re-stamp carried history and revert a peer's explicit correction.
        daily, stamps = dict(disk.series.daily), dict(disk.day_observed_at)
        for day, tokens in held.series.daily.items():
            held_stamp = held.day_observed_at.get(day, held.observed_at)
            if day not in daily or _instant(held_stamp) > _instant(stamps.get(day, disk.observed_at)):
                daily[day], stamps[day] = tokens, held_stamp
        newest = held if _instant(held.observed_at) > _instant(disk.observed_at) else disk
        summary = disk
        if held.series.lifetime_tokens is not None and (disk.series.lifetime_tokens is None or _instant(held.lifetime_observed_at or held.observed_at) > _instant(disk.lifetime_observed_at or disk.observed_at)):
            summary = held
        return RetainedSeries(ProviderSeries(summary.series.lifetime_tokens, daily), newest.observed_at, newest.state, newest.reason, stamps, summary.lifetime_observed_at)

    @staticmethod
    def _merge(old: RetainedSeries | None, series: ProviderSeries, stamp: str) -> RetainedSeries:
        daily = dict(old.series.daily) if old else {}
        stamps = dict(old.day_observed_at) if old else {}
        missing = bool(set(daily) - set(series.daily)) or bool(old and old.series.lifetime_tokens is not None and series.lifetime_tokens is None)
        for day, tokens in series.daily.items():
            if day not in daily or _instant(stamp) >= _instant(stamps.get(day, old.observed_at if old else stamp)):
                daily[day], stamps[day] = tokens, stamp
        if len(daily) > 4096:
            raise ProviderSeriesError("retained series exceeds day bound")
        lifetime = old.series.lifetime_tokens if old else None
        lifetime_stamp = old.lifetime_observed_at if old else None
        if series.lifetime_tokens is not None and (lifetime_stamp is None or _instant(stamp) >= _instant(lifetime_stamp)):
            lifetime, lifetime_stamp = series.lifetime_tokens, stamp
        observed = old.observed_at if old and _instant(old.observed_at) > _instant(stamp) else stamp
        return RetainedSeries(ProviderSeries(lifetime, dict(sorted(daily.items()))), observed,
                              "stale_last_good" if missing else "fresh",
                              "provider_series_partial_response" if missing else None,
                              stamps, lifetime_stamp)

    def observe(self, scope: str, series: ProviderSeries, stamp: str) -> tuple[RetainedSeries, bool]:
        """Merge actual per-day observations, never re-stamp carried history.

        Explicit corrections replace older observations, including downward
        corrections. A delayed response can correct an old day without replacing
        newer days. Atomic normalized replicas survive refresh and restart.
        """
        _instant(stamp)
        with self._lock:
            lock_fd = None
            try:
                if self.path:
                    if fcntl is None:
                        raise OSError("provider receipt persistence requires advisory file locking")
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                value = self._merge(self._prior(scope), series, stamp)
                self._held[scope] = value
                if self.path:
                    self._write(scope, value)
                return value, True
            except OSError:
                # Readable disk receipts remain useful even when locking/writing
                # fails. Per-day merge also protects memory-only observations.
                value = self._merge(self._prior(scope), series, stamp)
                self._held[scope] = value
                return value, False
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)

    def _write(self, scope: str, value: RetainedSeries) -> None:
        assert self.path is not None
        data = json.dumps(_document(scope, value), sort_keys=True).encode()
        if len(data) > MAX_BYTES:
            raise OSError("oversized retention cache")
        self._atomic(self.path, data)
        # Independent normalized replica permits recovery from a torn/corrupt
        # primary file; neither copy contains RPC account details or credentials.
        self._atomic(self.path.with_name(self.path.name + ".backup"), data)

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        if path.is_symlink():
            raise OSError("unsafe retention cache")
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
