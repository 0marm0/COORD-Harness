"""Current request adjustments reach measured rows and the anchor exactly once."""
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from coordharness.usage.dashboard_proxy import (
    UsageDashboardError, _sanitize_cost_component, _sanitize_history,
)
from coordharness.usage.ledger import DailyUsage
from coordharness.usage.local_history import SlotUsage
from coordharness.usage.local_service import _cost_component, _history
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.provider_anchor import (
    ProviderSeries, apply_anchor_plan, plan_codex_anchor, utc_day_costs, unavailable_plan,
)


def imported(*, tier=None, long=True, local_day="2026-10-22", legacy=()):
    slot = int(datetime(2026, 10, 22, 1, tzinfo=timezone.utc).timestamp())
    metrics = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_tokens": 0,
               "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0, "cache_create_other_tokens": 0}
    model = "gpt-6.1-sol"
    return SimpleNamespace(rows=(DailyUsage(usage_date=local_day, model=model, **metrics),),
        slot_rows=(SlotUsage(usage_slot=slot, model=model, **metrics),),
        request_pricing_bands=({"usage_slot": slot, "model": model, "context_long": long,
            "service_tier": tier, "request_count": 1, **metrics},),
        legacy_rows=legacy, legacy_provenance=None, coverage_state="complete")


def test_measured_headline_daily_and_model_include_adjustment_once():
    data, card = imported(), load_rate_card()
    cost = _cost_component(data, card, None)
    history = _history(data, datetime(2026, 10, 22, tzinfo=timezone.utc), card=card)
    assert cost["base_amount_nanos"] == 2_000_000_000
    assert cost["request_adjustment_nanos"] == 2_000_000_000
    assert cost["amount_nanos"] == 4_000_000_000
    assert history["daily"][0]["api_rate_estimate_nanos"] == cost["amount_nanos"]
    assert history["daily"][0]["model_breakdowns"][0]["api_rate_estimate_nanos"] == cost["amount_nanos"]
    assert cost["request_pricing"]["coverage"]["unknown_service_tier_requests"] == 1


def test_signed_batch_discount_reaches_proxy_and_model_rows():
    data, card = imported(tier="batch", long=False), load_rate_card()
    cost = _cost_component(data, card, None)
    history = _history(data, datetime(2026, 10, 22, tzinfo=timezone.utc), card=card)
    assert cost["request_adjustment_nanos"] == -1_000_000_000
    assert _sanitize_cost_component(cost)["request_adjustment_nanos"] == -1_000_000_000
    clean = _sanitize_history(history)
    assert clean["daily"][0]["request_adjustment_nanos"] == -1_000_000_000
    assert clean["daily"][0]["model_breakdowns"][0]["api_rate_estimate_nanos"] == 1_000_000_000


def test_anchor_uses_adjusted_utc_mix_without_double_applying_premium():
    data, card = imported(), load_rate_card()
    local = utc_day_costs(data.slot_rows, card, request_pricing_bands=data.request_pricing_bands)
    assert local[0].cost_nanos == 4_000_000_000
    plan = plan_codex_anchor(data.slot_rows, card,
        ProviderSeries(2_000_000, {"2026-10-22": 2_000_000}), tz=timezone.utc,
        request_pricing_bands=data.request_pricing_bands)
    assert plan.result.measured_cost_nanos == 4_000_000_000
    assert plan.result.estimated_off_transcript_cost_nanos == 4_000_000_000
    cost = _cost_component(data, card, None)
    history = _history(data, datetime(2026, 10, 22, tzinfo=timezone.utc), card=card)
    apply_anchor_plan(history, cost, plan, measured_nanos=cost["amount_nanos"], legacy_nanos=0)
    assert cost["amount_nanos"] == 8_000_000_000
    assert history["daily"][0]["measured_api_rate_estimate_nanos"] == 4_000_000_000


def test_utc_anchor_and_local_history_use_correct_day_basis():
    zone = ZoneInfo("America/New_York")
    data, card = imported(local_day="2026-10-21"), load_rate_card()
    history = _history(data, datetime(2026, 10, 21, 21, tzinfo=zone), card=card)
    plan = plan_codex_anchor(data.slot_rows, card,
        ProviderSeries(2_000_000, {"2026-10-22": 2_000_000}), tz=zone,
        request_pricing_bands=data.request_pricing_bands)
    assert history["daily"][0]["api_rate_estimate_nanos"] == 4_000_000_000
    assert plan.estimated_nanos_by_day == {"2026-10-21": 4_000_000_000}


def test_unknown_legacy_cost_is_reference_only_on_local_day_collision():
    legacy = DailyUsage(usage_date="2026-10-21", model="legacy", input_tokens=10, api_rate_estimate_nanos=123)
    data, card = imported(local_day="2026-10-21", legacy=(legacy,)), load_rate_card()
    history = _history(data, datetime(2026, 10, 21, 21, tzinfo=ZoneInfo("America/New_York")), card=card)
    assert history["daily"][0]["api_rate_estimate_nanos"] == 4_000_000_000
    assert history["daily"][0]["provenance"] == "legacy_import"
    assert history["legacy_pricing"]["original_amount_nanos"] == 123
    assert history["legacy_pricing"]["amount_nanos"] is None
    assert history["legacy_pricing"]["unpriced_tokens"] == 10


def test_private_pricing_rows_are_never_forwarded():
    cost = _cost_component(imported(), load_rate_card(), None)
    cost["request_pricing"]["request_pricing_bands"] = [{"private_path": "/private/secret"}]
    cost["request_pricing"]["by_day_model"] = [{"private_path": "/private/secret"}]
    clean = _sanitize_cost_component(cost)
    assert "request_pricing_bands" not in clean["request_pricing"]
    assert "by_day_model" not in clean["request_pricing"]
    assert clean["request_pricing"]["historical_effective_dates_verified"] is False


def test_unapproved_pricing_source_is_rejected():
    cost = _cost_component(imported(), load_rate_card(), None)
    cost["request_pricing"]["source_urls"] = ["https://private.example/account"]
    with pytest.raises(UsageDashboardError):
        _sanitize_cost_component(cost)


def test_legacy_unknown_and_unverified_spark_enter_global_cost_coverage():
    legacy = (
        DailyUsage(usage_date="2026-07-01", model="unknown", input_tokens=100, api_rate_estimate_nanos=7777),
        DailyUsage(usage_date="2026-07-01", model="gpt-5.3-codex-spark", input_tokens=100, api_rate_estimate_nanos=2222),
    )
    data, card = imported(legacy=legacy), load_rate_card()
    cost = _cost_component(data, card, None)
    assert cost["amount_nanos"] == 4_000_000_000
    assert cost["unpriced_tokens"] == cost["legacy_unpriced_tokens"] == 200
    assert cost["legacy_pricing"]["original_amount_nanos"] == 9999
    assert cost["legacy_pricing"]["amount_nanos"] is None
    assert cost["legacy_pricing"]["rate_authority_unverified_tokens"] == 100
    clean = _sanitize_cost_component(cost)
    assert clean["unpriced_tokens"] == 200
    assert clean["legacy_pricing"]["unpriced_tokens"] == 200
    assert "rows" not in clean["legacy_pricing"]


def test_legacy_public_rows_are_stripped_and_original_reference_survives():
    legacy = DailyUsage(usage_date="2026-07-01", model="gpt-5.6-sol", input_tokens=10, api_rate_estimate_nanos=123)
    cost = _cost_component(imported(legacy=(legacy,)), load_rate_card(), None)
    cost["legacy_pricing"]["rows"] = [{"private_path": "/private/secret"}]
    clean = _sanitize_cost_component(cost)
    assert "rows" not in clean["legacy_pricing"]
    assert clean["legacy_pricing"]["original_amount_nanos"] == 123
    assert clean["legacy_pricing"]["amount_nanos"] == 123


def test_only_legacy_known_and_unknown_keep_measured_semantics_separate():
    legacy = (
        DailyUsage(usage_date="2026-07-01", model="gpt-5.6-sol", input_tokens=10, api_rate_estimate_nanos=123),
        DailyUsage(usage_date="2026-07-01", model="unknown", input_tokens=100, api_rate_estimate_nanos=7777),
    )
    data = imported(legacy=legacy)
    data.rows, data.slot_rows, data.request_pricing_bands = (), (), ()
    cost = _cost_component(data, load_rate_card(), None)
    assert cost["amount_nanos"] is None
    assert cost["semantics"] == "measured_cost_unavailable"
    assert cost["currency"] == "USD"
    assert cost["priced_tokens"] == 10 and cost["unpriced_tokens"] == 100
    assert cost["coverage_state"] == "partial"
    history = _history(data, datetime(2026, 10, 22, tzinfo=timezone.utc), card=load_rate_card())
    apply_anchor_plan(history, cost, unavailable_plan("no_measured_local_usage"),
        measured_nanos=None, legacy_nanos=cost["legacy_pricing"]["amount_nanos"])
    assert cost["amount_nanos"] == 123
    assert cost["measured_amount_nanos"] is None
    assert cost["legacy_pricing"]["original_amount_nanos"] == 7900
