"""The Codex provider series is fetched off the request path, cached, and never silent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordharness.usage.local_service import fetch_codex_account_usage
from coordharness.usage.provider_anchor import (
    REASON_SERIES_DISABLED,
    REASON_SERIES_INVALID,
    REASON_SERIES_PENDING,
)
from coordharness.usage.provider_series import (
    CodexUsageSeriesSource,
    ProviderSeriesUnavailable,
    read_cache,
)

pytestmark = pytest.mark.unit

_RESULT = {
    "summary": {"lifetimeTokens": 30},
    "dailyUsageBuckets": [
        {"startDate": "2026-09-20", "tokens": 10},
        {"startDate": "2026-09-21", "tokens": 20},
    ],
}


def _source(tmp_path: Path, fetch, clock: list[datetime]) -> CodexUsageSeriesSource:
    return CodexUsageSeriesSource(
        fetch=fetch, cache_path=tmp_path / "series.json", now=lambda: clock[0]
    )


def test_the_first_read_is_pending_and_never_waits_for_the_fetch(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 22, tzinfo=timezone.utc)]
    source = _source(tmp_path, lambda: _RESULT, clock)

    first = source.current()
    assert first.series is None and first.reason == REASON_SERIES_PENDING
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
    assert read_cache(tmp_path / "series.json") == observation


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
