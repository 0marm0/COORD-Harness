"""The provider's own daily Codex token counts, fetched off the request path.

``account/usage/read`` on the official Codex CLI app-server answers with the
account's lifetime tokens and one bucket per UTC day. The series is fetched at most once per
``DEFAULT_TTL_SECONDS`` in a background thread, kept in memory, and written to
a small cache file beside the scan store so a restarted service anchors its
first payload from the last good series instead of waiting on a subprocess.

The request path only reads what is held. Failed or old observations retain
covered history with explicit stale status. Unknown or changed account identity
cannot reuse another account's cache. Omitted days survive partial successful
responses; explicitly reported corrections replace their previous values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Final

from .provider_anchor import (
    REASON_SERIES_DISABLED,
    REASON_SERIES_INVALID,
    REASON_SERIES_PENDING,
    REASON_SERIES_UNAVAILABLE,
    ProviderSeries,
    ProviderSeriesError,
    parse_account_usage,
)
from .series_retention import SCHEMA, SeriesRetention

_logger = logging.getLogger("coordharness.usage.provider_series")

CACHE_SCHEMA: Final = SCHEMA
OPT_OUT_ENV: Final = "COORD_USAGE_CODEX_PROVIDER_SERIES"
DEFAULT_TTL_SECONDS: Final = 900.0
RETRY_AFTER_FAILURE_SECONDS: Final = 300.0
_OFF_VALUES: Final = frozenset({"0", "false", "off", "no", "disabled"})
_IDENTITY = re.compile(r"[a-f0-9]{64}\Z")


class ProviderSeriesUnavailable(RuntimeError):
    """The provider did not return a usable series; ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SeriesObservation:
    """What the request path may use: a series with its fetch time, or a reason."""

    series: ProviderSeries | None
    observed_at: str | None = None
    reason: str | None = None
    series_state: str = "fresh"
    series_reason: str | None = None


def default_cache_path(store_path: Path) -> Path:
    """``codex-usage-series.json`` beside the scan store."""

    return store_path.with_name("codex-usage-series.json")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def read_cache(path: Path, *, source_identity: str | None = None) -> SeriesObservation | None:
    """The last good series on disk, re-validated; ``None`` if absent or bad."""

    if source_identity is None or not _IDENTITY.fullmatch(source_identity):
        return None
    retained = SeriesRetention(path).recall(source_identity)
    if retained is None:
        return None
    return SeriesObservation(
        series=retained.series, observed_at=retained.observed_at,
        series_state=retained.state, series_reason=retained.reason,
    )


def write_cache(
    path: Path, series: ProviderSeries, observed_at: datetime, *, source_identity: str,
) -> None:
    """Keep only normalized counts and opaque identity, atomically and privately."""

    if not _IDENTITY.fullmatch(source_identity):
        raise ValueError("unverified source identity")
    _, persisted = SeriesRetention(path).observe(source_identity, series, _utc_iso(observed_at))
    if not persisted:
        raise OSError("provider series cache not written")


class CodexUsageSeriesSource:
    """Serve the last good provider series; refresh it in the background."""

    def __init__(
        self,
        *,
        fetch: Callable[[], Mapping[str, Any]] | None,
        cache_path: Path,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        retry_after_failure_seconds: float = RETRY_AFTER_FAILURE_SECONDS,
        opt_out_env: str = OPT_OUT_ENV,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_update: Callable[[], None] | None = None,
        identity: Callable[[], str | None] | None = None,
    ) -> None:
        self._fetch = fetch
        self._cache_path = Path(cache_path)
        self._retention = SeriesRetention(self._cache_path)
        self._ttl = float(ttl_seconds)
        self._retry_after = float(retry_after_failure_seconds)
        self._opt_out_env = opt_out_env
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._on_update = on_update
        self._identity = identity or (lambda: None)
        self._source_identity: str | None = None
        self._lock = threading.Lock()
        self._held: SeriesObservation | None = None
        self._loaded_disk = False
        self._thread: threading.Thread | None = None
        self._not_before = float("-inf")
        self._last_error: str | None = None

    def enabled(self) -> bool:
        return os.environ.get(self._opt_out_env, "").strip().lower() not in _OFF_VALUES

    def current(self) -> SeriesObservation:
        """Never blocks on the provider. Starts a refresh when one is due."""

        if not self.enabled():
            return SeriesObservation(series=None, reason=REASON_SERIES_DISABLED)
        self._load_identity()
        self._maybe_start()
        with self._lock:
            if self._held is not None:
                if self._due() and self._held.series_state == "fresh":
                    return replace(self._held, series_state="stale_last_good",
                                   series_reason="provider_series_expired")
                return self._held
            return SeriesObservation(
                series=None, reason=self._last_error or (
                    REASON_SERIES_PENDING if self._source_identity is not None
                    else "provider_series_identity_unavailable"
                )
            )

    def _load_identity(self) -> str | None:
        """Verify disk-only auth scope before serving or merging account history."""

        try:
            identity = self._identity()
        except (OSError, ValueError):
            identity = None
        if identity is not None and not _IDENTITY.fullmatch(identity):
            identity = None
        with self._lock:
            if identity != self._source_identity:
                self._held = None
                self._source_identity = identity
                self._loaded_disk = False
                self._last_error = None
                self._not_before = float("-inf")
            if self._loaded_disk:
                return identity
            self._loaded_disk = True
            cached = self._retention.recall(identity) if identity is not None else None
            if cached is not None:
                self._held = SeriesObservation(
                    series=cached.series, observed_at=cached.observed_at,
                    series_state=cached.state, series_reason=cached.reason,
                )
        return identity

    def _due(self) -> bool:
        held = self._held
        observed = _parse_utc(held.observed_at) if held is not None else None
        if observed is None:
            return True
        return (self._now().astimezone(timezone.utc) - observed).total_seconds() > self._ttl

    def _maybe_start(self) -> None:
        if self._fetch is None:
            return
        with self._lock:
            if (self._source_identity is None or self._thread is not None
                    or self._monotonic() < self._not_before):
                return
            if not self._due():
                return
            thread = threading.Thread(
                target=self._run, name="coord-codex-usage-series", daemon=True
            )
            self._thread = thread
        thread.start()

    def refresh_now(self) -> SeriesObservation:
        """Fetch synchronously (bounded by the fetcher); for the CLI and tests."""

        if not self.enabled():
            return SeriesObservation(series=None, reason=REASON_SERIES_DISABLED)
        if self._fetch is None:
            self._load_identity()
            return self._failed(REASON_SERIES_UNAVAILABLE)
        identity = self._load_identity()
        if identity is None:
            return self._failed("provider_series_identity_unavailable")
        try:
            result = self._fetch()
            series = parse_account_usage(result)
        except ProviderSeriesUnavailable as error:
            return self._failed(error.code)
        except ProviderSeriesError:
            return self._failed(REASON_SERIES_INVALID)
        except Exception:  # noqa: BLE001 - any provider fault degrades to measured-only
            _logger.warning("codex usage series fetch failed")
            return self._failed(REASON_SERIES_UNAVAILABLE)
        if self._load_identity() != identity:
            return self._failed("provider_series_identity_changed")
        observed = self._now()
        if self._load_identity() != identity:
            return self._failed("provider_series_identity_changed")
        with self._lock:
            if self._source_identity != identity:
                return SeriesObservation(series=None, reason="provider_series_identity_changed")
            if identity is not None:
                try:
                    retained, persisted = self._retention.observe(identity, series, _utc_iso(observed))
                except (OSError, ValueError):
                    self._last_error = "provider_series_cache_capacity"
                    if self._held is not None:
                        self._held = replace(self._held, series_state="stale_last_good",
                                             series_reason=self._last_error)
                        return self._held
                    return SeriesObservation(series=None, reason=self._last_error)
                observation = SeriesObservation(
                    series=retained.series, observed_at=retained.observed_at,
                    series_state=retained.state if persisted else "stale_last_good",
                    series_reason=retained.reason if persisted else "provider_series_cache_write_failed",
                )
            else:
                observation = SeriesObservation(
                    series=series, observed_at=_utc_iso(observed),
                    series_state="stale_last_good", series_reason="provider_series_identity_unavailable",
                )
            self._held = observation
            self._last_error = None
            self._not_before = float("-inf")
        return observation

    def _failed(self, code: str) -> SeriesObservation:
        self._load_identity()
        with self._lock:
            self._last_error = code
            self._not_before = self._monotonic() + self._retry_after
            held = self._held
            if held is not None:
                self._held = replace(held, series_state="stale_last_good", series_reason=code)
                return self._held
        return SeriesObservation(series=None, reason=code)

    def _run(self) -> None:
        updated = False
        try:
            updated = self.refresh_now().series is not None
        finally:
            with self._lock:
                self._thread = None
        if updated and self._on_update is not None:
            try:
                self._on_update()
            except Exception:  # noqa: BLE001 - a listener fault must not poison the source
                _logger.warning("codex usage series listener failed", exc_info=True)

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()


__all__ = [
    "CACHE_SCHEMA",
    "OPT_OUT_ENV",
    "CodexUsageSeriesSource",
    "ProviderSeriesUnavailable",
    "SeriesObservation",
    "default_cache_path",
    "read_cache",
    "write_cache",
]
