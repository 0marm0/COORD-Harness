"""Thread-safe bounded snapshot caching for the standalone local usage service."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
from typing import Any

from .local_service import ProviderProbe, _UncachedLocalUsageService


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LocalUsageService(_UncachedLocalUsageService):
    """Coalesce refreshes and serve bounded fresh, stale, or warming snapshots."""

    def __init__(
        self,
        *args: Any,
        cache_ttl_seconds: float = 30.0,
        first_read_wait_seconds: float = 0.2,
        monotonic: Any = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        ttl = float(cache_ttl_seconds)
        wait = float(first_read_wait_seconds)
        if not 0 <= ttl <= 300:
            raise ValueError("local usage cache TTL must be in [0, 300] seconds")
        if not 0 <= wait <= 1:
            raise ValueError("local usage first-read wait must be in [0, 1] seconds")
        self._cache_ttl = ttl
        self._first_read_wait = wait
        self._monotonic = monotonic
        self._cache_condition = threading.Condition(threading.RLock())
        self._cached_document: dict[str, Any] | None = None
        self._cached_at: float | None = None
        self._refreshing = False
        self._refresh_generation = 0
        self._last_refresh_error: str | None = None

    def dashboard(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return quickly while at most one background refresh does local I/O.

        A cold caller waits only ``first_read_wait_seconds``. If discovery is
        still running it gets an explicit warming document. Expired snapshots
        are returned as stale while one background refresh replaces them.
        """

        with self._cache_condition:
            observed = self._monotonic()
            if self._is_fresh(observed) and not force_refresh:
                return deepcopy(self._cached_document)
            generation_before = self._refresh_generation
            if not self._refreshing:
                self._refreshing = True
                threading.Thread(
                    target=self._refresh_worker,
                    name="coord-local-usage-refresh",
                    daemon=True,
                ).start()
            cached = deepcopy(self._cached_document)
            should_wait = cached is None or force_refresh
            if should_wait and self._first_read_wait:
                deadline = time.monotonic() + self._first_read_wait
                while self._refreshing and time.monotonic() < deadline:
                    self._cache_condition.wait(timeout=max(0.0, deadline - time.monotonic()))
                if self._refresh_generation != generation_before and self._cached_document:
                    return deepcopy(self._cached_document)
                cached = deepcopy(self._cached_document)
            if cached is not None:
                return self._stale(cached, "local_refresh_in_progress")
            if self._last_refresh_error and not self._refreshing:
                return self._empty("error", "local_refresh_failed")
            return self._empty("warming", "local_refresh_in_progress")

    def account_status(self, *, force_refresh: bool = False) -> dict[str, ProviderProbe]:
        """Project cached public account state without rerunning provider probes."""

        dashboard = self.dashboard(force_refresh=force_refresh)
        providers = dashboard.get("providers")
        result: dict[str, ProviderProbe] = {}
        for provider in ("claude", "codex"):
            item = providers.get(provider) if isinstance(providers, dict) else None
            if not isinstance(item, dict):
                result[provider] = self._unavailable_probe(provider)
                continue
            account = item.get("account") if isinstance(item.get("account"), dict) else {}
            windows = item.get("windows") if isinstance(item.get("windows"), list) else []
            account_source = item.get("account_source")
            quota_source = item.get("quota_source")
            errors = item.get("errors") if isinstance(item.get("errors"), list) else []
            result[provider] = ProviderProbe(
                account=dict(account),
                windows=tuple(dict(window) for window in windows if isinstance(window, dict)),
                observed_at=item.get("live_observed_at")
                if isinstance(item.get("live_observed_at"), str)
                else None,
                account_source=account_source.get("kind")
                if isinstance(account_source, dict) and isinstance(account_source.get("kind"), str)
                else "official_cli_status",
                quota_source=quota_source.get("kind")
                if isinstance(quota_source, dict) and isinstance(quota_source.get("kind"), str)
                else None,
                errors=tuple(
                    row["code"]
                    for row in errors
                    if isinstance(row, dict) and isinstance(row.get("code"), str)
                ),
            )
        return result

    def _refresh_worker(self) -> None:
        document: dict[str, Any] | None = None
        error: str | None = None
        try:
            document = super().dashboard()
        except Exception:
            error = "local_refresh_failed"
        with self._cache_condition:
            if document is not None:
                self._cached_document = deepcopy(document)
                self._cached_at = self._monotonic()
                self._refresh_generation += 1
                self._last_refresh_error = None
            else:
                self._last_refresh_error = error
            self._refreshing = False
            self._cache_condition.notify_all()

    def _is_fresh(self, observed: float) -> bool:
        return (
            self._cached_document is not None
            and self._cached_at is not None
            and observed - self._cached_at <= self._cache_ttl
        )

    def _empty(self, state: str, code: str) -> dict[str, Any]:
        generated = _iso(self._now())
        return {
            "schema": "coordharness.usage-intelligence.v1",
            "generated_at": generated,
            "stale_after": None,
            "refresh": {
                "state": state,
                "generated_at": generated,
                "error_code": code,
            },
            "providers": {},
            "errors": [{"code": code}],
        }

    def _stale(self, document: dict[str, Any], code: str) -> dict[str, Any]:
        generated = _iso(self._now())
        last_good = document.get("generated_at")
        document["refresh"] = {
            "state": "stale",
            "generated_at": generated,
            "error_code": code,
            **({"last_good_generated_at": last_good} if isinstance(last_good, str) else {}),
        }
        errors = list(document.get("errors") or [])
        errors.append({"code": code})
        document["errors"] = errors[-64:]
        return document

    @staticmethod
    def _unavailable_probe(provider: str) -> ProviderProbe:
        return ProviderProbe(
            account={
                "status": "unavailable",
                "plan": "unknown",
                "authenticated": None,
            },
            errors=(f"{provider}_local_refresh_in_progress",),
        )
