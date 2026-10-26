"""Lifetime conservation must survive the bounded public daily chart."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from coordharness.usage.accounting_guard import accounting_errors, assert_accounting
from coordharness.usage.dashboard_proxy import UsageDashboardError, validate_usage_dashboard
from coordharness.usage.ledger import DailyUsage
from coordharness.usage.local_history import LocalHistoryImport
from coordharness.usage.local_service import LocalUsageService, ProviderProbe, _UncachedLocalUsageService


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def _history(_root, *, provider):
    rows = tuple(
        DailyUsage(usage_date=(NOW - timedelta(days=i)).date().isoformat(),
                   model="claude-opus-5", input_tokens=1000)
        for i in range(401)
    ) if provider == "claude" else ()
    return LocalHistoryImport(
        provider=provider, rows=rows, coverage_state="complete",
        root_identity_digest="1" * 64, manifest_digest="2" * 64,
        files_scanned=1, records_scanned=len(rows), records_accepted=len(rows),
        records_rejected=0, parse_error_count=0,
    )


def _probe():
    return ProviderProbe(
        account={"status": "active", "plan": "plus", "authenticated": True},
        windows=(), observed_at="2026-10-26T12:00:00Z",
        account_source="official_cli_status", quota_source="official_cli_quota",
    )


def _arguments(tmp_path):
    return dict(home=tmp_path, now=lambda: NOW, history_loader=_history,
                claude_probe=_probe, codex_probe=_probe)


def test_complete_history_is_checked_before_chart_is_bounded(tmp_path):
    document = _UncachedLocalUsageService(**_arguments(tmp_path)).dashboard()
    history = document["providers"]["claude"]["history"]
    assert len(history["daily"]) == 400
    assert sum(row["total_tokens"] for row in history["daily"]) == 400000
    assert history["all_time_total_tokens"] == 401000
    assert history["omitted_daily_totals"]["row_count"] == 1
    assert_accounting(document)
    public = validate_usage_dashboard(document)
    assert public["providers"]["claude"]["history"]["omitted_daily_totals"] == history["omitted_daily_totals"]
    assert len(public["providers"]["claude"]["history"]["daily"]) == 400
    assert_accounting(public)


@pytest.mark.parametrize("mutation", ["visible_tokens", "omitted_tokens", "omitted_cost", "overlap"])
def test_bounded_history_still_rejects_inconsistent_conservation(tmp_path, mutation):
    document = validate_usage_dashboard(_UncachedLocalUsageService(**_arguments(tmp_path)).dashboard())
    history = document["providers"]["claude"]["history"]
    if mutation == "visible_tokens":
        history["daily"][0]["total_tokens"] += 1
    elif mutation == "overlap":
        history["omitted_daily_totals"]["last_date"] = history["daily"][0]["date"]
    else:
        field = "total_tokens" if mutation == "omitted_tokens" else "api_rate_estimate_nanos"
        history["omitted_daily_totals"][field] += 1
    before = deepcopy(document)
    assert accounting_errors(document)
    with pytest.raises(ValueError, match="usage_accounting_inconsistent"):
        assert_accounting(document)
    assert document == before


@pytest.mark.parametrize("field,value", [
    ("row_count", True), ("row_count", 0), ("row_count", -1),
    ("total_tokens", True), ("total_tokens", -1),
    ("api_rate_estimate_nanos", False), ("api_rate_estimate_nanos", -1),
    ("last_date", "2026-02-30"), ("last_date", "/private/path"),
])
def test_proxy_refuses_invalid_omitted_history_proof(tmp_path, field, value):
    document = _UncachedLocalUsageService(**_arguments(tmp_path)).dashboard()
    document["providers"]["claude"]["history"]["omitted_daily_totals"][field] = value
    with pytest.raises(UsageDashboardError, match="invalid_contract"):
        validate_usage_dashboard(document)


def test_proxy_strips_unknown_omitted_history_fields(tmp_path):
    document = _UncachedLocalUsageService(**_arguments(tmp_path)).dashboard()
    proof = document["providers"]["claude"]["history"]["omitted_daily_totals"]
    proof.update(source_path="/private/synthetic", account_id="synthetic-secret")
    public = validate_usage_dashboard(document)
    sanitized = public["providers"]["claude"]["history"]["omitted_daily_totals"]
    assert set(sanitized) == {"row_count", "total_tokens", "api_rate_estimate_nanos", "last_date"}
    assert_accounting(public)


def test_cached_service_publishes_long_history(tmp_path):
    service = LocalUsageService(**_arguments(tmp_path), store_auto_refresh=False,
                                rate_card_auto_refresh=False, first_read_wait_seconds=1)
    service.dashboard()
    with service._cache_condition:
        assert service._cache_condition.wait_for(lambda: not service._refreshing, timeout=3)
    assert service._last_refresh_error is None
    assert service._cached_document is not None
    assert service.dashboard()["refresh"]["state"] == "fresh"


def test_inconsistent_full_history_is_not_hidden_in_omitted_prefix(tmp_path, monkeypatch):
    from coordharness.usage import local_service

    original = local_service._history

    def broken_history(*args, **kwargs):
        history = original(*args, **kwargs)
        if history["daily"]:
            history["daily"][0]["total_tokens"] += 1
        return history

    monkeypatch.setattr(local_service, "_history", broken_history)
    with pytest.raises(ValueError, match="usage_accounting_inconsistent"):
        _UncachedLocalUsageService(**_arguments(tmp_path)).dashboard()
