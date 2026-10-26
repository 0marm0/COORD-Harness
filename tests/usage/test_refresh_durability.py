"""Refresh scheduling must preserve observation age, source updates and failures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

from coordharness.usage.local_cache import LocalUsageService
from coordharness.usage.local_service import _UncachedLocalUsageService


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def _document(observed: datetime, amount: int = 1) -> dict:
    stamp = observed.isoformat().replace("+00:00", "Z")
    return {
        "generated_at": stamp,
        "stale_after": stamp,
        "refresh": {"state": "fresh", "generated_at": stamp},
        "providers": {"codex": {
            "amount": amount,
            "errors": [{"code": "provider_series_stale_last_good"}],
            "account": {"status": "active", "authenticated": True},
        }},
        "errors": [{"code": "provider_series_stale_last_good"}],
    }


def _service(tmp_path, clock: list[float], **kwargs) -> LocalUsageService:
    return LocalUsageService(
        home=tmp_path,
        history_loader=lambda *_args, **_kwargs: None,
        now=lambda: NOW + timedelta(seconds=clock[0]),
        monotonic=lambda: clock[0],
        first_read_wait_seconds=0.01,
        rate_card_auto_refresh=False,
        **kwargs,
    )


def _idle(service: LocalUsageService) -> None:
    with service._cache_condition:
        assert service._cache_condition.wait_for(lambda: not service._refreshing, timeout=2)


def test_proactive_refresh_has_bounded_grace_and_original_stamp(monkeypatch, tmp_path):
    clock = [0.0]
    entered, release = threading.Event(), threading.Event()
    calls = []

    def build(_service):
        calls.append(clock[0])
        observed = _service._now()
        if len(calls) > 1:
            entered.set()
            assert release.wait(timeout=2)
        return _document(observed)

    monkeypatch.setattr(_UncachedLocalUsageService, "dashboard", build)
    service = _service(tmp_path, clock)
    service.dashboard()
    _idle(service)
    clock[0] = 15
    try:
        grace = service.dashboard()
        assert entered.wait(timeout=1)
        assert grace["refresh"]["state"] == "fresh"
        assert grace["generated_at"] == "2026-10-22T12:00:00Z"
        assert grace["stale_after"] == "2026-10-22T12:01:00Z"
        clock[0] = 59
        assert service.dashboard()["refresh"]["state"] == "fresh"
        clock[0] = 61
        stale = service.dashboard()
        assert stale["refresh"]["state"] == "stale"
        assert stale["refresh"]["last_good_generated_at"] == grace["generated_at"]
        assert stale["providers"]["codex"]["errors"] == grace["providers"]["codex"]["errors"]
        assert calls == [0, 15]
    finally:
        release.set()
        _idle(service)
    # Completion cannot reset the age of the observation made at second 15.
    clock[0] = 76
    assert service.dashboard()["refresh"]["state"] == "stale"
    _idle(service)


def test_invalidation_during_build_runs_one_followup_without_waiting_for_poll(monkeypatch, tmp_path):
    clock = [0.0]
    entered, release = threading.Event(), threading.Event()
    calls = []
    source = [1]

    def build(service):
        captured = source[0]
        calls.append(captured)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=2)
        return _document(service._now(), captured)

    monkeypatch.setattr(_UncachedLocalUsageService, "dashboard", build)
    service = _service(tmp_path, clock)
    assert service.dashboard()["refresh"]["state"] == "warming"
    assert entered.wait(timeout=1)
    source[0] = 2
    service._invalidate_cached_document()
    release.set()
    _idle(service)
    assert calls == [1, 2]
    assert service.dashboard()["providers"]["codex"]["amount"] == 2
    assert service.dashboard()["refresh"]["state"] == "fresh"


def test_repeated_invalidation_is_bounded_and_never_cleared_by_old_build(monkeypatch, tmp_path):
    clock = [0.0]
    calls = []

    def build(service):
        calls.append(1)
        if len(calls) <= 2:
            service._invalidate_cached_document()
        return _document(service._now(), len(calls))

    monkeypatch.setattr(_UncachedLocalUsageService, "dashboard", build)
    service = _service(tmp_path, clock)
    result = service.dashboard()
    _idle(service)
    assert result["refresh"]["state"] == "stale"
    assert len(calls) == 2
    with service._cache_condition:
        assert not service._is_fresh(clock[0])
    service.dashboard()
    _idle(service)
    assert service.dashboard()["providers"]["codex"]["amount"] == 3


def test_invalidation_and_force_bypass_freshness_grace(monkeypatch, tmp_path):
    clock = [0.0]
    entered, release = threading.Event(), threading.Event()
    calls = []

    def build(service):
        calls.append(1)
        if len(calls) > 1:
            entered.set()
            assert release.wait(timeout=2)
        return _document(service._now())

    monkeypatch.setattr(_UncachedLocalUsageService, "dashboard", build)
    service = _service(tmp_path, clock)
    service.dashboard()
    _idle(service)
    try:
        forced = service.dashboard(force_refresh=True)
        assert entered.wait(timeout=1)
        assert forced["refresh"]["state"] == "stale"
        service._invalidate_cached_document()
        invalidated = service.dashboard()
        assert invalidated["refresh"]["state"] == "stale"
    finally:
        release.set()
        _idle(service)


@pytest.mark.parametrize("warm", [False, True])
def test_refresh_failure_is_visible_and_backed_off_then_force_can_retry(monkeypatch, tmp_path, warm):
    clock = [0.0]
    calls = []
    failing = [not warm]

    def build(service):
        calls.append(1)
        if failing[0]:
            raise RuntimeError("provider failure")
        return _document(service._now(), 7)

    monkeypatch.setattr(_UncachedLocalUsageService, "dashboard", build)
    service = _service(tmp_path, clock)
    service.dashboard()
    _idle(service)
    if warm:
        failing[0] = True
        clock[0] = 15
        service.dashboard()
        _idle(service)
    count = len(calls)
    failed = service.dashboard()
    assert failed["refresh"]["state"] == ("stale" if warm else "error")
    assert failed["refresh"]["error_code"] == "local_refresh_failed"
    if warm:
        assert failed["providers"]["codex"]["amount"] == 7
    for _ in range(10):
        service.dashboard()
    assert len(calls) == count
    clock[0] += 5
    service.dashboard()
    _idle(service)
    assert len(calls) == count + 1
    failing[0] = False
    service.dashboard(force_refresh=True)
    _idle(service)
    assert service.dashboard()["refresh"]["state"] == "fresh"


def test_inconsistent_build_retains_previous_totals(monkeypatch,tmp_path):
    clock=[0.0];calls=[]
    def build(service):
        calls.append(1)
        return {"generated_at":"2026-10-22T12:00:00Z","providers":{"codex":{"costs":{"api_rate_estimate":{"semantics":"self_computed_api_list_price_estimate","amount_nanos":12}},"history":{"all_time_total_tokens":100 if len(calls)==1 else 1,"daily":[{"date":"2026-10-22","total_tokens":100,"api_rate_estimate_nanos":12}]}}}}
    monkeypatch.setattr(_UncachedLocalUsageService,"dashboard",build)
    service=_service(tmp_path,clock);service.dashboard();_idle(service)
    clock[0]=16;service.dashboard();_idle(service)
    d=service.dashboard();assert d["providers"]["codex"]["history"]["all_time_total_tokens"]==100
    assert d["refresh"]["error_code"]=="local_refresh_failed"
