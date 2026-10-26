"""The Codex provider series is fetched off the request path, cached, and never silent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading

import pytest

from coordharness.usage.local_service import fetch_codex_account_usage
from coordharness.usage.provider_anchor import (
    REASON_SERIES_DISABLED,
    REASON_SERIES_INVALID,
    REASON_SERIES_PENDING,
    ProviderSeries,
)
from coordharness.usage.provider_series import (
    CodexUsageSeriesSource,
    ProviderSeriesUnavailable,
    read_cache,
)
from coordharness.usage.series_identity import codex_source_identity
from coordharness.usage.series_retention import SeriesRetention


def test_missing_advisory_lock_keeps_memory_without_unsafe_persistence(tmp_path, monkeypatch):
    from coordharness.usage import series_retention

    monkeypatch.setattr(series_retention, "fcntl", None)
    path = tmp_path / "receipt.json"
    retention = SeriesRetention(path)
    series = ProviderSeries(5, {"2026-10-22": 5})
    held, persisted = retention.observe("a" * 64, series, "2026-10-22T12:00:00Z")
    assert persisted is False
    assert held.series == series
    assert retention.recall("a" * 64).series == series
    assert not path.exists()
    assert not path.with_name(path.name + ".lock").exists()

pytestmark = pytest.mark.unit

_RESULT = {
    "summary": {"lifetimeTokens": 30},
    "dailyUsageBuckets": [
        {"startDate": "2026-09-20", "tokens": 10},
        {"startDate": "2026-09-21", "tokens": 20},
    ],
}
_IDENTITY = "a" * 64


def _source(tmp_path: Path, fetch, clock: list[datetime]) -> CodexUsageSeriesSource:
    return CodexUsageSeriesSource(
        fetch=fetch, cache_path=tmp_path / "series.json", now=lambda: clock[0],
        identity=lambda: _IDENTITY,
    )


def test_the_first_read_is_pending_and_never_waits_for_the_fetch(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    release = threading.Event()

    def fetch():
        assert release.wait(5)
        return _RESULT

    source = _source(tmp_path, fetch, clock)

    first = source.current()
    assert first.series is None and first.reason == REASON_SERIES_PENDING
    release.set()
    assert source.wait(5)
    second = source.current()
    assert second.series is not None and second.series.daily["2026-09-21"] == 20
    assert second.observed_at == "2026-09-22T00:00:00Z"


def test_a_restarted_source_serves_the_cached_series_immediately(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()

    restarted = _source(tmp_path, None, clock)
    observation = restarted.current()
    assert observation.series is not None
    assert observation.series.reconciles_with_lifetime is True
    assert read_cache(tmp_path / "series.json", source_identity=_IDENTITY) == observation


def test_a_failed_fetch_keeps_the_last_good_series(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    results = [_RESULT]

    def fetch():
        if not results:
            raise ProviderSeriesUnavailable("codex_app_server_timeout")
        return results.pop()

    source = _source(tmp_path, fetch, clock)
    source.refresh_now()
    clock[0] += timedelta(hours=1)
    kept = source.refresh_now()
    assert kept.series is not None and kept.observed_at == "2026-09-22T00:00:00Z"
    assert kept.series_state == "stale_last_good"
    assert kept.series_reason == "codex_app_server_timeout"


def test_restart_timeout_and_expiry_never_remove_covered_days(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    first = _source(tmp_path, lambda: _RESULT, clock).refresh_now()
    clock[0] += timedelta(days=30)

    def timeout():
        raise ProviderSeriesUnavailable("codex_app_server_timeout")

    restarted = _source(tmp_path, timeout, clock)
    observation = restarted.refresh_now()
    assert observation.series == first.series
    assert observation.series_reason == "codex_app_server_timeout"
    assert observation.observed_at == first.observed_at


def test_partial_success_unions_history_and_accepts_explicit_corrections(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    _source(tmp_path, lambda: _RESULT, clock).refresh_now()
    clock[0] += timedelta(days=1)
    partial = {"summary": {"lifetimeTokens": 45}, "dailyUsageBuckets": [
        {"startDate": "2026-09-21", "tokens": 15},
        {"startDate": "2026-09-22", "tokens": 20},
    ]}
    observation = _source(tmp_path, lambda: partial, clock).refresh_now()
    assert observation.series.daily == {
        "2026-09-20": 10, "2026-09-21": 15, "2026-09-22": 20,
    }
    assert observation.series_reason == "provider_series_partial_response"
    assert observation.series_state == "stale_last_good"
    restarted = _source(tmp_path, None, clock)
    assert restarted.current() == observation


def test_partial_response_preserves_known_lifetime_but_explicit_correction_replaces_it(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-22", "tokens": 3}]}
    clock[0] += timedelta(hours=1)
    assert source.refresh_now().series.lifetime_tokens == 30
    source._fetch = lambda: {"summary": {"lifetimeTokens": 15}, "dailyUsageBuckets": [
        {"startDate": "2026-09-22", "tokens": 3},
    ]}
    clock[0] += timedelta(hours=1)
    assert source.refresh_now().series.lifetime_tokens == 15


def test_full_daily_response_without_lifetime_discloses_retained_summary(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    source._fetch = lambda: {"dailyUsageBuckets": _RESULT["dailyUsageBuckets"]}
    clock[0] += timedelta(hours=1)
    result = source.refresh_now()
    assert result.series.lifetime_tokens == 30
    assert result.series_state == "stale_last_good"
    assert result.series_reason == "provider_series_partial_response"


def test_account_switch_and_missing_identity_cannot_reuse_a_receipt(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    _source(tmp_path, lambda: _RESULT, clock).refresh_now()
    identity = [_IDENTITY]
    source = CodexUsageSeriesSource(
        fetch=None, cache_path=tmp_path / "series.json", now=lambda: clock[0],
        identity=lambda: identity[0],
    )
    assert source.current().series is not None
    identity[0] = "b" * 64
    assert source.current().series is None
    identity[0] = None
    assert source.current().series is None
    assert read_cache(tmp_path / "series.json") is None


def test_switch_during_fetch_rejects_the_response(tmp_path: Path) -> None:
    identity = [_IDENTITY]

    def switched():
        identity[0] = "b" * 64
        return _RESULT

    source = CodexUsageSeriesSource(fetch=switched, cache_path=tmp_path / "series.json",
                                   identity=lambda: identity[0])
    assert source.refresh_now().reason == "provider_series_identity_changed"
    assert not (tmp_path / "series.json").exists()


def test_switch_during_observation_clock_cannot_publish_the_previous_account(tmp_path: Path) -> None:
    identity = [_IDENTITY]
    source = None

    def now():
        identity[0] = "b" * 64
        source._load_identity()
        return datetime(2026, 9, 22, tzinfo=timezone.utc)

    source = CodexUsageSeriesSource(
        fetch=lambda: _RESULT, cache_path=tmp_path / "series.json",
        identity=lambda: identity[0], now=now,
    )
    assert source.refresh_now().reason == "provider_series_identity_changed"
    assert source._held is None
    assert not (tmp_path / "series.json").exists()


def test_cache_is_normalized_private_and_tampering_is_rejected(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    raw = {**_RESULT, "email": "synthetic-secret", "access_token": "synthetic-token"}
    _source(tmp_path, lambda: raw, clock).refresh_now()
    path = tmp_path / "series.json"
    content = path.read_text()
    assert "synthetic-secret" not in content and "synthetic-token" not in content
    assert os.stat(path).st_mode & 0o777 == 0o600
    document = json.loads(content)
    document["result"]["dailyUsageBuckets"][0]["tokens"] = 999
    path.write_text(json.dumps(document))
    recovered = read_cache(path, source_identity=_IDENTITY)
    assert recovered.series.daily["2026-09-20"] == 10
    assert recovered.series_state == "stale_last_good"
    assert recovered.series_reason == "provider_series_cache_recovered"
    assert read_cache(path, source_identity="b" * 64) is None
    path.with_name(path.name + ".backup").write_text("corrupt")
    assert read_cache(path, source_identity=_IDENTITY) is None


def test_unknown_identity_never_fetches_or_reuses_retained_account_data(tmp_path: Path) -> None:
    calls = []
    source = CodexUsageSeriesSource(
        fetch=lambda: calls.append(1) or _RESULT, cache_path=tmp_path / "series.json",
        identity=lambda: None,
    )
    assert source.current().reason == "provider_series_identity_unavailable"
    assert source.refresh_now().reason == "provider_series_identity_unavailable"
    assert calls == []


def test_account_scope_ignores_token_rotation_and_fails_closed_on_signout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    root = tmp_path / ".codex"
    root.mkdir()
    auth = root / "auth.json"
    auth.write_text(json.dumps({"tokens": {"account_id": "synthetic-account", "access_token": "one"}}))
    first = codex_source_identity(tmp_path)
    assert first is not None and len(first) == 64
    auth.write_text(json.dumps({"tokens": {"account_id": "synthetic-account", "access_token": "two"}}))
    assert codex_source_identity(tmp_path) == first
    auth.write_text(json.dumps({"tokens": {"account_id": "other-account"}}))
    assert codex_source_identity(tmp_path) != first
    auth.unlink()
    assert codex_source_identity(tmp_path) is None


def test_failed_persistence_then_recovery_keeps_memory_and_disk_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    original_write = SeriesRetention._write

    def fail(*args):
        raise OSError("synthetic read-only cache")

    monkeypatch.setattr(SeriesRetention, "_write", fail)
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-22", "tokens": 3}]}
    clock[0] += timedelta(hours=1)
    failed = source.refresh_now()
    assert failed.series_reason == "provider_series_cache_write_failed"
    assert set(failed.series.daily) == {"2026-09-20", "2026-09-21", "2026-09-22"}
    monkeypatch.setattr(SeriesRetention, "_write", original_write)
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-23", "tokens": 4}]}
    clock[0] += timedelta(hours=1)
    restored = source.refresh_now()
    assert set(restored.series.daily) == {
        "2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23",
    }
    assert _source(tmp_path, None, clock).current().series == restored.series


def test_concurrent_collectors_union_the_latest_disk_receipts(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    peer = _source(tmp_path, None, clock)
    assert peer.current().series is not None
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-22", "tokens": 3}]}
    clock[0] += timedelta(hours=1)
    source.refresh_now()
    peer._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-23", "tokens": 4}]}
    clock[0] += timedelta(hours=1)
    result = peer.refresh_now()
    assert set(result.series.daily) == {
        "2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23",
    }


def test_equal_clock_peer_correction_wins_over_older_memory(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    corrected = {"dailyUsageBuckets": [{"startDate": "2026-09-21", "tokens": 15}]}
    _source(tmp_path, lambda: corrected, clock).refresh_now()
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-22", "tokens": 3}]}
    observation = source.refresh_now()
    assert observation.series.daily == {
        "2026-09-20": 10, "2026-09-21": 15, "2026-09-22": 3,
    }


def test_lock_open_failure_still_retains_disk_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    _source(tmp_path, lambda: _RESULT, clock).refresh_now()
    original_open = os.open

    def readonly_lock(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError("synthetic lock permission failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", readonly_lock)
    store = SeriesRetention(tmp_path / "series.json")
    observation, persisted = store.observe(
        _IDENTITY, ProviderSeries(None, {"2026-09-22": 3}), "2026-09-22T01:00:00Z",
    )
    assert persisted is False
    assert observation.series.daily == {
        "2026-09-20": 10, "2026-09-21": 20, "2026-09-22": 3,
    }


def test_memory_only_partial_receipt_cannot_revert_a_peer_day_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    original_write = SeriesRetention._write

    def fail(*args):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(SeriesRetention, "_write", fail)
    clock[0] += timedelta(hours=2)
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-22", "tokens": 3}]}
    source.refresh_now()
    monkeypatch.setattr(SeriesRetention, "_write", original_write)
    clock[0] -= timedelta(hours=1)
    correction = {"dailyUsageBuckets": [{"startDate": "2026-09-20", "tokens": 5}]}
    _source(tmp_path, lambda: correction, clock).refresh_now()
    clock[0] += timedelta(hours=2)
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-23", "tokens": 4}]}
    value = source.refresh_now()
    assert value.series.daily["2026-09-20"] == 5
    assert value.series.daily["2026-09-22"] == 3
    assert value.series.daily["2026-09-23"] == 4


def test_delayed_response_can_correct_an_older_day_without_reverting_newer_days(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)
    source.refresh_now()
    clock[0] += timedelta(hours=2)
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-22", "tokens": 3}]}
    source.refresh_now()
    clock[0] -= timedelta(hours=1)
    source._fetch = lambda: {"dailyUsageBuckets": [{"startDate": "2026-09-20", "tokens": 5}]}
    result = source.refresh_now()
    assert result.series.daily == {
        "2026-09-20": 5, "2026-09-21": 20, "2026-09-22": 3,
    }
    assert result.series.lifetime_tokens == 30
    assert result.observed_at == "2026-09-22T02:00:00Z"


def test_failures_carry_their_reason(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]

    def timeout():
        raise ProviderSeriesUnavailable("codex_app_server_timeout")

    assert _source(tmp_path, timeout, clock).refresh_now().reason == "codex_app_server_timeout"
    bad = _source(tmp_path / "b", lambda: {"dailyUsageBuckets": "nope"}, clock)
    assert bad.refresh_now().reason == REASON_SERIES_INVALID


def test_the_opt_out_environment_disables_the_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_USAGE_CODEX_PROVIDER_SERIES", "off")
    calls: list[int] = []
    source = _source(tmp_path, lambda: calls.append(1) or _RESULT, [datetime.now(timezone.utc)])
    assert source.current().reason == REASON_SERIES_DISABLED
    assert calls == []


def test_the_fetch_asks_the_app_server_for_exactly_account_usage() -> None:
    seen: list[list[dict]] = []

    def runner(command, requests, timeout):
        seen.append(list(requests))
        return [{"id": 1, "result": {}}, {"id": 2, "result": _RESULT}]

    assert fetch_codex_account_usage(Path("/nonexistent"), runner=runner) == _RESULT
    assert [request.get("method") for request in seen[0]] == [
        "initialize",
        "initialized",
        "account/usage/read",
    ]


@pytest.mark.parametrize(
    ("responses", "code"),
    [
        ([{"id": 2, "error": {"code": -1}}], "codex_account_usage_rpc_error"),
        ([], "codex_account_usage_timeout"),
    ],
)
def test_the_fetch_names_what_went_wrong(responses, code: str) -> None:
    with pytest.raises(ProviderSeriesUnavailable) as raised:
        fetch_codex_account_usage(Path("/nonexistent"), runner=lambda *_a: responses)
    assert raised.value.code == code
