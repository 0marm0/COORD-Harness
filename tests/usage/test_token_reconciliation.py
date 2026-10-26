from copy import deepcopy
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from coordharness.usage.local_history import SlotUsage
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.provider_anchor import (
    AnchorPlan, ProviderSeries, apply_anchor_plan, not_applicable_plan, plan_codex_anchor,
)
from coordharness.usage.token_reconciliation import reconcile_provider_tokens


def provider():
    return {
        "history": {
            "daily": [{"date": "2026-09-21", "total_tokens": 100,
                       "input_tokens": 100, "api_rate_estimate_nanos": 400_000,
                       "model_breakdowns": [{"key": "sol", "label": "gpt-5.6-sol", "total_tokens": 100}]}],
            "today_total_tokens": 100, "rolling_7d_total_tokens": 100,
            "calendar_week_total_tokens": 100, "all_time_total_tokens": 100,
        },
        "breakdowns": {"models": {"items": [{"key": "sol", "total_tokens": 100}]}},
    }


def plan():
    return plan_codex_anchor(
        [SlotUsage(usage_slot=int(datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp()),
                   model="gpt-5.6-sol", input_tokens=100)],
        load_rate_card(), ProviderSeries(500, {"2026-09-21": 500}), tz=timezone.utc,
    )


def test_token_and_cost_volume_reconcile_without_double_pricing():
    doc, anchor, cost = provider(), plan(), {}
    apply_anchor_plan(doc["history"], cost, anchor, measured_nanos=400_000, legacy_nanos=0)
    dollars = deepcopy(cost)
    reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 21))
    row = doc["history"]["daily"][0]
    assert row["total_tokens"] == 500
    assert row["measured_total_tokens"] == row["input_tokens"] == 100
    assert row["unattributed_provider_tokens"] == 400
    assert sum(m["total_tokens"] for m in row["model_breakdowns"]) == 500
    assert row["model_breakdowns"][-1]["label"] == "Provider usage (model unknown)"
    assert doc["history"]["all_time_total_tokens"] == 500
    assert sum(m["total_tokens"] for m in doc["breakdowns"]["models"]["items"]) == 500
    assert cost == dollars
    once = deepcopy(doc)
    reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 21))
    assert doc == once


def test_claude_and_unavailable_provider_are_unchanged():
    for anchor in [not_applicable_plan(), AnchorPlan(state="unavailable")]:
        doc = provider()
        before = deepcopy(doc)
        reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 21))
        assert doc == before


def test_provider_only_day_is_visible_and_summary_keeps_uncapped_history():
    doc, cost = provider(), {}
    doc["history"]["all_time_total_tokens"] = 1000
    anchor = plan_codex_anchor(
        [SlotUsage(usage_slot=int(datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp()),
                   model="gpt-5.6-sol", input_tokens=100)],
        load_rate_card(), ProviderSeries(1000, {"2026-09-21": 500, "2026-09-22": 500}),
        tz=timezone.utc,
    )
    apply_anchor_plan(doc["history"], cost, anchor, measured_nanos=400_000, legacy_nanos=0)
    reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 22))
    assert doc["history"]["daily"][-1]["total_tokens"] == 500
    assert doc["history"]["daily"][-1]["measured_total_tokens"] == 0
    assert doc["history"]["all_time_total_tokens"] == 1900


def test_provider_utc_volume_is_conserved_across_local_day_allocation():
    rows = [SlotUsage(usage_slot=int(datetime(2026, 9, 21, hour, tzinfo=timezone.utc).timestamp()),
                     model="gpt-5.6-sol", input_tokens=count) for hour, count in [(2, 300), (15, 100)]]
    anchor = plan_codex_anchor(rows, load_rate_card(), ProviderSeries(800, {"2026-09-21": 800}),
                               tz=ZoneInfo("America/New_York"))
    doc = provider()
    doc["history"]["daily"].insert(0, {"date": "2026-09-20", "total_tokens": 300})
    cost = {}
    apply_anchor_plan(doc["history"], cost, anchor, measured_nanos=1_600_000, legacy_nanos=0)
    reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 21))
    assert [r["total_tokens"] for r in doc["history"]["daily"]] == [600, 200]
    assert sum(r["unattributed_provider_tokens"] for r in doc["history"]["daily"]) == 400
