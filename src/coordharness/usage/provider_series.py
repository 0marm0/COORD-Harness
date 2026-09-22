"""The provider's own daily Codex token counts, fetched off the request path.

``account/usage/read`` on the official Codex CLI app-server answers with the
account's lifetime tokens and one bucket per UTC day. Past buckets never
change and today's moves slowly, so the series is fetched at most once per
``DEFAULT_TTL_SECONDS`` in a background thread, kept in memory, and written to
a small cache file beside the scan store so a restarted service anchors its
first payload from the last good series instead of waiting on a subprocess.

The request path only ever READS what is held. A missing, failed, expired or
opted-out series is reported as a machine-readable reason, and the payload then
carries measured cost only -- never silently.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
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

_logger = logging.getLogger("coordharness.usage.provider_series")

CACHE_SCHEMA: Final = "coordharness.codex-account-usage-series.v1"
OPT_OUT_ENV: Final = "COORD_USAGE_CODEX_PROVIDER_SERIES"
DEFAULT_TTL_SECONDS: Final = 900.0
RETRY_AFTER_FAILURE_SECONDS: Final = 300.0
_MAX_CACHE_BYTES: Final = 1024 * 1024
_OFF_VALUES: Final = frozenset({"0", "false", "off", "no", "disabled"})


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


def read_cache(path: Path) -> SeriesObservation | None:
    """The last good series on disk, re-validated; ``None`` if absent or bad."""

    try:
        if path.stat().st_size > _MAX_CACHE_BYTES:
            return None
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != CACHE_SCHEMA:
        return None
    observed = _parse_utc(document.get("observed_at"))
    try:
        series = parse_account_usage(document.get("result"))
    except ProviderSeriesError:
        return None
    if observed is None:
        return None
    return SeriesObservation(series=series, observed_at=_utc_iso(observed))


def write_cache(path: Path, result: Mapping[str, Any], observed_at: datetime) -> None:
    """Atomically keep the raw validated result; a reader never sees half of it."""

    document = {"schema": CACHE_SCHEMA, "observed_at": _utc_iso(observed_at), "result": result}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


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
    ) -> None:
        self._fetch = fetch
        self._cache_path = Path(cache_path)
        self._ttl = float(ttl_seconds)
        self._retry_after = float(retry_after_failure_seconds)
        self._opt_out_env = opt_out_env
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._on_update = on_update
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
        with self._lock:
            load_disk = not self._loaded_disk
            self._loaded_disk = True
        if load_disk:
            cached = read_cache(self._cache_path)
            with self._lock:
                if self._held is None and cached is not None:
                    self._held = cached
        self._maybe_start()
        with self._lock:
            if self._held is not None:
                return self._held
            return SeriesObservation(
                series=None, reason=self._last_error or REASON_SERIES_PENDING
            )

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
            if self._thread is not None or self._monotonic() < self._not_before:
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

        if self._fetch is None:
            return SeriesObservation(series=None, reason=REASON_SERIES_UNAVAILABLE)
        try:
            result = self._fetch()
            series = parse_account_usage(result)
        except ProviderSeriesUnavailable as error:
            return self._failed(error.code)
        except ProviderSeriesError:
            return self._failed(REASON_SERIES_INVALID)
        except Exception:  # noqa: BLE001 - any provider fault degrades to measured-only
            _logger.warning("codex usage series fetch failed", exc_info=True)
            return self._failed(REASON_SERIES_UNAVAILABLE)
        observed = self._now()
        try:
            write_cache(self._cache_path, result, observed)
        except OSError:
            _logger.warning("codex usage series cache not written", exc_info=True)
        observation = SeriesObservation(series=series, observed_at=_utc_iso(observed))
        with self._lock:
            self._held = observation
            self._last_error = None
        return observation

    def _failed(self, code: str) -> SeriesObservation:
        with self._lock:
            self._last_error = code
            self._not_before = self._monotonic() + self._retry_after
            held = self._held
        return held if held is not None else SeriesObservation(series=None, reason=code)

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
