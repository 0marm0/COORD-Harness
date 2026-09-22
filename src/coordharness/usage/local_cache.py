"""Thread-safe bounded snapshot caching for the standalone local usage service."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
from typing import Any

from .local_service import ProviderProbe, _UncachedLocalUsageService
from .rate_card_refresh import RateCardRefresher, RefreshResult, unpriced_for_want_of_rate
from .scan_refresh import DEFAULT_STALE_AFTER_SECONDS, ScanStoreRefresher, StoreFreshness


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
        store_auto_refresh: bool = True,
        store_stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        rate_card_auto_refresh: bool | None = None,
        **kwargs: Any,
    ) -> None:
        # An injected history loader means the scan store is not the history
        # source, so there is nothing for a refresher to keep current.
        store_backed = kwargs.get("history_loader") is None
        card_backed = kwargs.get("rate_card_loader") is None
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
        # The only thing that keeps a standalone install's scan store current.
        # It is created here, but walks nothing until the first dashboard read.
        self._store_refresher: ScanStoreRefresher | None = (
            ScanStoreRefresher(
                home=self.home,
                store_path=self._scan_store_path,
                stale_after_seconds=store_stale_after_seconds,
                now=self._now,
                on_complete=self._invalidate_cached_document,
            )
            if store_auto_refresh and store_backed
            else None
        )
        # Keeps the user-local override card current (see rate_card_refresh).
        # Off unless this service serves the real user with the default card
        # loader: a fixture home must never trigger a network fetch. The
        # operator opt-out is the COORD_USAGE_RATE_CARD_REFRESH=0 environment.
        auto_card = (
            rate_card_auto_refresh
            if rate_card_auto_refresh is not None
            else self._serves_real_user
        )
        self._rate_card_refresher: RateCardRefresher | None = (
            RateCardRefresher(
                override_path=self._rate_card_override_path,
                on_complete=self._on_rate_card_refreshed,
            )
            if auto_card and card_backed
            else None
        )

    def dashboard(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return quickly while at most one background refresh does local I/O.

        A cold caller waits only ``first_read_wait_seconds``. If discovery is
        still running it gets an explicit warming document. Expired snapshots
        are returned as stale while one background refresh replaces them.
        """

        if self._store_refresher is not None:
            # Starts a background incremental scan when the store is older than
            # its threshold; never waits for one. This request is answered from
            # whatever the store holds now.
            self._store_refresher.request_refresh()
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

    def _store_is_building(self) -> bool:
        return self._store_refresher is not None and self._store_refresher.building

    def _after_pricing(self, unpriced_models: Mapping[str, str]) -> None:
        """Start a background rate-card refresh when a model went unpriced."""

        if self._rate_card_refresher is not None:
            self._rate_card_refresher.maybe_refresh(unpriced_for_want_of_rate(unpriced_models))

    def _on_rate_card_refreshed(self, _result: RefreshResult) -> None:
        self._invalidate_cached_document()

    def _on_provider_series_update(self) -> None:
        self._invalidate_cached_document()

    def _history_store_freshness(self, observed: datetime) -> StoreFreshness:
        if self._store_refresher is None:
            return super()._history_store_freshness(observed)
        return self._store_refresher.freshness()

    def _invalidate_cached_document(self) -> None:
        """A scan just completed: the next read rebuilds from the new store."""

        condition = getattr(self, "_cache_condition", None)
        if condition is None:
            # A background source finished before construction completed;
            # there is no snapshot yet to invalidate.
            return
        with condition:
            self._cached_at = None

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
