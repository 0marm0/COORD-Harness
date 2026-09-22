"""Provider-anchored Codex cost, from the scan store's slots to the proxied payload.

The contract under test: MEASURED and ESTIMATED travel separately per day and
in total, the headline is their sum plus legacy and says so, a missing provider
series falls back to measured-only with a machine-readable reason, Claude is
never anchored, and every one of those fields survives the dashboard proxy.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coordharness.usage.dashboard_proxy import UsageDashboardProxy
from coordharness.usage.ledger import DailyUsage
from coordharness.usage.local_history import SlotUsage
from coordharness.usage.local_service import ProviderProbe, _UncachedLocalUsageService
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.provider_anchor import (
    ESTIMATE_APPLIED,
    ESTIMATE_NOT_APPLICABLE,
    ESTIMATE_UNAVAILABLE,
    REASON_NO_PROVIDER_TOKEN_API,
    REASON_SERIES_EXPIRED,
    REASON_SERIES_PENDING,
    ProviderSeries,
    _apportion,
    apply_anchor_plan,
    not_applicable_plan,
    plan_codex_anchor,
    utc_day_costs,
)
from coordharness.usage.provider_series import SeriesObservation
from coordharness.usage.scan_store import UsageScanStore

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def _slot(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def _row(stamp: str, *, fresh: int, cached: int = 0, output: int = 0) -> SlotUsage:
    return SlotUsage(
        usage_slot=_slot(stamp),
        model="gpt-5.6-sol",
        input_tokens=fresh,
        cache_read_tokens=cached,
        output_tokens=output,
        request_count=1,
    )


def test_apportion_is_exact_and_deterministic() -> None:
    assert _apportion(10, {"a": 1, "b": 1, "c": 1}) == {"a": 4, "b": 3, "c": 3}
    assert sum(_apportion(1_000_003, {"x": 7, "y": 5, "z": 1}).values()) == 1_000_003
    assert _apportion(0, {"a": 1}) == {}
    assert _apportion(5, {"a": 0}) == {}


def test_utc_day_costs_price_each_utc_day_at_its_own_mix() -> None:
    card = load_rate_card()
    rows = [
        _row("2026-09-20T23:45:00Z", fresh=1_000_000),
        _row("2026-09-21T00:00:00Z", fresh=0, cached=1_000_000),
    ]
    days = utc_day_costs(rows, card)
    assert [day.usage_date for day in days] == ["2026-09-20", "2026-09-21"]
    # gpt-5.6-sol: $4/M fresh input, $0.40/M cached.
    assert [day.cost_nanos for day in days] == [4_000_000_000, 400_000_000]
    assert [day.tokens for day in days] == [1_000_000, 1_000_000]


def test_a_utc_days_estimate_follows_its_measured_volume_onto_local_days() -> None:
    card = load_rate_card()
    # UTC 09-21: 3M tokens at 02:00Z (New York 09-20 evening), 1M at 15:00Z.
    rows = [
        _row("2026-09-21T02:00:00Z", fresh=3_000_000),
        _row("2026-09-21T15:00:00Z", fresh=1_000_000),
    ]
    series = ProviderSeries(lifetime_tokens=None, daily={"2026-09-21": 8_000_000})
    plan = plan_codex_anchor(
        rows, card, series, tz=ZoneInfo("America/New_York"), series_observed_at="2026-09-22T00:00:00Z",
        now=_NOW,
    )

    assert plan.state == ESTIMATE_APPLIED
    # 4M off-transcript tokens at the day's own $4/M mix = $16, split 3:1.
    assert plan.estimated_tokens_by_day == {"2026-09-20": 3_000_000, "2026-09-21": 1_000_000}
    assert plan.estimated_nanos_by_day == {
        "2026-09-20": 12_000_000_000,
        "2026-09-21": 4_000_000_000,
    }
    assert plan.estimated_cost_nanos == 16_000_000_000


def test_legacy_days_are_never_anchored_again_and_pre_window_days_are_reported() -> None:
    card = load_rate_card()
    rows = [_row("2026-07-17T12:00:00Z", fresh=1_000)]
    series = ProviderSeries(
        lifetime_tokens=10_000,
        daily={"2026-07-10": 4_000, "2026-07-16": 5_000, "2026-07-17": 1_000},
    )
    plan = plan_codex_anchor(
        rows, card, series, tz=timezone.utc, excluded_utc_days={"2026-07-16"}, now=_NOW
    )
    assert plan.window_start == "2026-07-17"
    assert plan.pre_window_provider_tokens == 4_000
    assert plan.estimated_cost_nanos == 0
    assert plan.anchor_block()["provider_tokens"] == 1_000


def test_an_expired_series_is_not_applied() -> None:
    plan = plan_codex_anchor(
        [_row("2026-09-01T12:00:00Z", fresh=1)],
        load_rate_card(),
        ProviderSeries(lifetime_tokens=None, daily={"2026-09-01": 5}),
        series_observed_at="2026-09-01T00:00:00Z",
        now=_NOW,
    )
    assert (plan.state, plan.reason) == (ESTIMATE_UNAVAILABLE, REASON_SERIES_EXPIRED)


def test_apply_writes_both_parts_per_day_and_in_total() -> None:
    card = load_rate_card()
    rows = [_row("2026-09-21T12:00:00Z", fresh=1_000_000)]
    plan = plan_codex_anchor(
        rows,
        card,
        ProviderSeries(lifetime_tokens=None, daily={"2026-09-20": 500_000, "2026-09-21": 2_000_000}),
        tz=timezone.utc,
        now=_NOW,
    )
    history = {
        "daily": [
            {"date": "2026-07-01", "total_tokens": 9, "api_rate_estimate_nanos": 7,
             "provenance": "legacy_import"},
            {"date": "2026-09-21", "total_tokens": 1_000_000,
             "api_rate_estimate_nanos": 4_000_000_000, "provenance": "self_computed"},
        ]
    }
    cost = {"amount_nanos": 4_000_000_000}
    apply_anchor_plan(history, cost, plan, measured_nanos=4_000_000_000, legacy_nanos=7)

    legacy, measured = history["daily"]
    assert legacy["measured_api_rate_estimate_nanos"] is None
    assert legacy["estimated_api_rate_estimate_nanos"] is None
    assert measured["measured_api_rate_estimate_nanos"] == 4_000_000_000
    assert measured["estimated_api_rate_estimate_nanos"] == 4_000_000_000
    assert measured["estimated_tokens"] == 1_000_000
    assert measured["api_rate_estimate_nanos"] == 8_000_000_000
    assert cost == {
        "amount_nanos": 8_000_000_007,
        "measured_amount_nanos": 4_000_000_000,
        "estimated_amount_nanos": 4_000_000_000,
        "legacy_amount_nanos": 7,
        "estimate_state": ESTIMATE_APPLIED,
        "estimate_reason": None,
        "estimate_basis": "provider_utc_day_gap_priced_at_same_utc_day_local_mix",
        "provider_series_observed_at": None,
        "amount_semantics": "measured_plus_estimated_plus_legacy",
    }
    # 09-20 was below the local window, so it is reported, not estimated.
    assert history["provider_anchor"]["pre_window_provider_tokens"] == 500_000


def test_a_day_seen_only_by_the_provider_gets_an_estimate_only_row() -> None:
    card = load_rate_card()
    rows = [_row("2026-09-20T12:00:00Z", fresh=1_000_000)]
    plan = plan_codex_anchor(
        rows,
        card,
        ProviderSeries(lifetime_tokens=None, daily={"2026-09-20": 1_000_000, "2026-09-21": 250_000}),
        tz=timezone.utc,
        now=_NOW,
    )
    history = {"daily": [{"date": "2026-09-20", "total_tokens": 1_000_000,
                          "api_rate_estimate_nanos": 4_000_000_000,
                          "provenance": "self_computed"}]}
    apply_anchor_plan(history, {}, plan, measured_nanos=4_000_000_000, legacy_nanos=0)

    added = history["daily"][-1]
    assert added["date"] == "2026-09-21"
    assert added["provenance"] == "provider_anchor_estimate"
    assert added["total_tokens"] == 0
    assert added["measured_api_rate_estimate_nanos"] == 0
    assert added["estimated_api_rate_estimate_nanos"] == 1_000_000_000


def test_claude_is_measured_only_and_says_why() -> None:
    history = {"daily": [{"date": "2026-09-21", "total_tokens": 1,
                          "api_rate_estimate_nanos": 5, "provenance": "self_computed"}]}
    cost: dict = {"amount_nanos": 5}
    apply_anchor_plan(history, cost, not_applicable_plan(), measured_nanos=5, legacy_nanos=0)
    assert cost["estimated_amount_nanos"] is None
    assert cost["estimate_state"] == ESTIMATE_NOT_APPLICABLE
    assert cost["estimate_reason"] == REASON_NO_PROVIDER_TOKEN_API
    assert cost["amount_nanos"] == 5
    assert history["daily"][0]["estimated_api_rate_estimate_nanos"] is None


# -- the service and the proxy ---------------------------------------------


def _codex_line(stamp: str, ordinal: int, running: int) -> dict:
    return {
        "timestamp": stamp,
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "model": "gpt-5.6-sol",
            "info": {
                # Output 0 would drop the record: the parser reads `x or y`
                # counts, so a zero output falls through to a missing key.
                "total_token_usage": {"input_tokens": running, "cached_input_tokens": 0,
                                      "output_tokens": running // 1_000_000},
                "last_token_usage": {"input_tokens": 999_999, "cached_input_tokens": 0,
                                     "output_tokens": 1},
            },
        },
    }


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    rollout = home / ".codex" / "sessions" / "rollout-2026-09-21T12-00-00-01a0-a.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in (
                {"timestamp": "2026-09-21T12:00:00Z", "ordinal": 0, "type": "session_meta",
                 "payload": {"id": "01a0-a"}},
                _codex_line("2026-09-21T12:00:00Z", 1, 1_000_000),
                _codex_line("2026-09-21T12:00:01Z", 2, 2_000_000),
            )
        )
    )
    claude = home / ".claude" / "projects" / "p" / "c.jsonl"
    claude.parent.mkdir(parents=True)
    claude.write_text(json.dumps({
        "timestamp": "2026-09-21T12:00:00Z",
        "message": {"id": "m", "model": "claude-opus-5",
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    }) + "\n")
    with UsageScanStore(home / ".coordharness" / "usage-scan.sqlite") as store:
        store.scan(home / ".codex", provider="codex")
        store.scan(home / ".claude", provider="claude")
        store.import_legacy(
            "codex",
            source="codexbar_high_water_v1",
            rows=[DailyUsage(usage_date="2026-07-01", model="gpt-5.6-sol",
                             input_tokens=10, api_rate_estimate_nanos=123)],
        )
    return home


def _service(home: Path, observation: SeriesObservation) -> _UncachedLocalUsageService:
    def probe() -> ProviderProbe:
        return ProviderProbe(account={"status": "active", "plan": "pro", "authenticated": True})

    return _UncachedLocalUsageService(
        home=home,
        now=lambda: _NOW,
        claude_probe=probe,
        codex_probe=probe,
        cost_cache_root=home / "none",
        codex_series=lambda: observation,
    )


def _series() -> SeriesObservation:
    return SeriesObservation(
        series=ProviderSeries(
            lifetime_tokens=None,
            daily={"2026-07-01": 10, "2026-09-21": 3_000_000},
        ),
        observed_at="2026-09-22T11:00:00Z",
    )


def test_the_service_splits_codex_cost_into_measured_estimated_and_legacy(
    tmp_path: Path,
) -> None:
    document = _service(_home(tmp_path), _series()).dashboard()
    codex = document["providers"]["codex"]
    cost = codex["costs"]["api_rate_estimate"]

    # 2M measured (~$8.000032 at gpt-5.6-sol list); the 1M off-transcript
    # tokens at that same day's mix is exactly half of it; plus legacy.
    assert cost["measured_amount_nanos"] == 8_000_032_000
    assert cost["estimated_amount_nanos"] == 4_000_016_000
    assert cost["legacy_amount_nanos"] == 123
    assert cost["amount_nanos"] == 12_000_048_123
    assert cost["estimate_state"] == "applied"
    assert cost["provider_series_observed_at"] == "2026-09-22T11:00:00Z"
    day = next(row for row in codex["history"]["daily"] if row["date"] == "2026-09-21")
    assert (day["measured_api_rate_estimate_nanos"], day["estimated_api_rate_estimate_nanos"]) == (
        8_000_032_000,
        4_000_016_000,
    )
    assert day["api_rate_estimate_nanos"] == 12_000_048_000
    # The legacy day was excluded from anchoring even though the provider
    # reports it: it already equals the provider's bucket.
    assert codex["history"]["provider_anchor"]["window_start"] == "2026-09-21"
    claude = document["providers"]["claude"]["costs"]["api_rate_estimate"]
    assert claude["estimate_state"] == "not_applicable"
    assert claude["estimated_amount_nanos"] is None


def test_without_a_provider_series_codex_is_measured_only_with_a_reason(tmp_path: Path) -> None:
    document = _service(
        _home(tmp_path), SeriesObservation(series=None, reason=REASON_SERIES_PENDING)
    ).dashboard()
    codex = document["providers"]["codex"]
    cost = codex["costs"]["api_rate_estimate"]

    assert cost["estimate_state"] == "unavailable"
    assert cost["estimate_reason"] == REASON_SERIES_PENDING
    assert cost["estimated_amount_nanos"] is None
    assert cost["amount_nanos"] == 8_000_032_123
    assert {"code": "codex_provider_anchor_unavailable"} in codex["errors"]


def test_every_anchor_field_survives_the_dashboard_proxy(tmp_path: Path) -> None:
    """Fields have been silently stripped at this boundary before; not these."""

    document = _service(_home(tmp_path), _series()).dashboard()
    proxied = UsageDashboardProxy(url=None, local_provider=lambda: document).get()

    for provider in ("codex", "claude"):
        before = document["providers"][provider]
        after = proxied["providers"][provider]
        cost_fields = (
            "amount_nanos",
            "measured_amount_nanos",
            "estimated_amount_nanos",
            "legacy_amount_nanos",
            "estimate_state",
            "estimate_reason",
            "estimate_basis",
            "provider_series_observed_at",
            "amount_semantics",
            "pricing_key",
            "rate_card_digest",
            "rate_card_override_models",
        )
        for field in cost_fields:
            assert field in before["costs"]["api_rate_estimate"], field
            # A null token field may be dropped; a stated value never may.
            assert (
                after["costs"]["api_rate_estimate"].get(field)
                == before["costs"]["api_rate_estimate"][field]
            ), (provider, field)
        anchor_before = before["history"]["provider_anchor"]
        anchor_after = after["history"]["provider_anchor"]
        for key, value in anchor_before.items():
            if value is not None:
                assert anchor_after.get(key) == value, (provider, key)
        for before_day, after_day in zip(before["history"]["daily"], after["history"]["daily"]):
            for field in (
                "api_rate_estimate_nanos",
                "measured_api_rate_estimate_nanos",
                "estimated_api_rate_estimate_nanos",
                "estimated_tokens",
                "provenance",
            ):
                assert after_day.get(field) == before_day.get(field), (provider, field)
    codex_days = proxied["providers"]["codex"]["history"]["daily"]
    assert any(day.get("estimated_api_rate_estimate_nanos") for day in codex_days)
