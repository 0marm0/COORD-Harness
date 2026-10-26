"""Dated tariffs and retained estimates conserve the displayed calendar."""

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from coordharness.usage.local_history import SlotUsage
from coordharness.usage.ledger import DailyUsage
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.provider_anchor import plan_codex_anchor, ProviderSeries
from coordharness.usage.legacy_pricing import derive_legacy_pricing
from coordharness.usage.local_service import _history


def _local_priced_daily(imported, card, zone):
    h = _history(imported, datetime(2026, 10, 22, tzinfo=zone), card=card)
    daily = {r["date"]: r for r in h["daily"]}
    models = {}
    for r in h["daily"]:
        for m in r.get("model_breakdowns", []):
            models.setdefault(m["label"], {})[r["date"]] = m
    return daily, models


def data(day):
    slot = int(datetime.fromisoformat(day + "T00:15:00+00:00").timestamp())
    model = "gpt-5.6-sol"
    metrics = dict(input_tokens=100000, cache_read_tokens=100000, output_tokens=1000)
    local = datetime.fromtimestamp(slot, ZoneInfo("America/New_York")).date().isoformat()
    return SimpleNamespace(
        rows=(DailyUsage(usage_date=local, model=model, **metrics),),
        legacy_rows=(),
        slot_rows=(SlotUsage(usage_slot=slot, model=model, **metrics),),
        request_pricing_bands=(
            {"usage_slot": slot, "model": model, "context_long": False, **metrics},
        ),
    ), local


def test_cutover_uses_request_utc_not_local_display_day_and_prices_gap_once():
    card = load_rate_card()
    zone = ZoneInfo("America/New_York")
    for day, expected in [("2026-08-20", 580000000), ("2026-08-21", 460000000)]:
        imported, local = data(day)
        daily, models, *_ = _local_priced_daily(imported, card, zone)
        assert daily[local]["api_rate_estimate_nanos"] == expected
        assert models["gpt-5.6-sol"][local]["api_rate_estimate_nanos"] == expected
        plan = plan_codex_anchor(
            imported.slot_rows,
            card,
            ProviderSeries(None, {day: 402000}),
            tz=zone,
            request_pricing_bands=imported.request_pricing_bands,
        )
        assert plan.result.measured_cost_nanos == expected
        assert plan.estimated_cost_nanos == expected


def test_legacy_premiums_are_preserved_not_added_twice_or_erased():
    row = DailyUsage(
        usage_date="2026-07-16",
        model="gpt-5.6-sol",
        input_tokens=100000,
        api_rate_estimate_nanos=1234567890,
    )
    imported = SimpleNamespace(
        rows=(), legacy_rows=(row,), request_pricing_bands=(), legacy_provenance=None
    )
    daily, *_ = _local_priced_daily(imported, load_rate_card(), timezone.utc)
    summary = derive_legacy_pricing((row,), load_rate_card(), preserve_recorded=True)
    assert summary["amount_nanos"] == daily[row.usage_date]["api_rate_estimate_nanos"] == 1234567890
    assert summary["basis"] == "retained_recorded_legacy_api_estimate"
    assert summary["card_reference_amount_nanos"] == 400000000
    assert row.api_rate_estimate_nanos == 1234567890


def test_missing_recorded_legacy_cost_does_not_silently_use_today_price():
    row = DailyUsage(
        usage_date="2026-07-16",
        model="gpt-5.6-sol",
        input_tokens=100000,
        api_rate_estimate_nanos=None,
    )
    summary = derive_legacy_pricing((row,), load_rate_card(), preserve_recorded=True)
    assert summary["amount_nanos"] is None
    assert summary["unpriced_tokens"] == 100000
    assert summary["rows"][0]["reason"] == "legacy_recorded_estimate_missing_or_invalid"
