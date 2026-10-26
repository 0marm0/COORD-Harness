from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from coordharness.usage.provider_anchor import ProviderSeries, apply_anchor_plan, plan_codex_anchor
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.local_history import SlotUsage
from coordharness.usage.token_reconciliation import reconcile_provider_tokens


def test_legacy_and_measured_local_boundary_keeps_estimated_gap_cost():
    card = load_rate_card()
    slot = SlotUsage(
        usage_slot=int(datetime(2026, 7, 17, 2, tzinfo=timezone.utc).timestamp()),
        model="gpt-5.6-sol", input_tokens=100,
    )
    anchor = plan_codex_anchor(
        [slot], card, ProviderSeries(700, {"2026-07-16": 200, "2026-07-17": 500}),
        excluded_utc_days={"2026-07-16"}, tz=ZoneInfo("America/New_York"),
    )
    measured_cost = card.price(slot.model, {"input_tokens": 100}).amount_nanos
    legacy_cost = 700_000
    doc = {"history": {
        "daily": [{
            "date": "2026-07-16", "provenance": "legacy_import", "total_tokens": 300,
            "api_rate_estimate_nanos": measured_cost + legacy_cost,
            "model_breakdowns": [
                {"key": "legacy", "label": "legacy", "total_tokens": 200,
                 "api_rate_estimate_nanos": legacy_cost},
                {"key": "sol", "label": slot.model, "total_tokens": 100,
                 "api_rate_estimate_nanos": measured_cost},
            ],
        }],
        "today_total_tokens": 300, "rolling_7d_total_tokens": 300,
        "calendar_week_total_tokens": 300, "all_time_total_tokens": 300,
    }}
    cost = {}
    apply_anchor_plan(doc["history"], cost, anchor,
                      measured_nanos=measured_cost, legacy_nanos=legacy_cost)
    reconcile_provider_tokens(doc, anchor, today=date(2026, 7, 16))
    row = doc["history"]["daily"][0]
    assert row["total_tokens"] == 700
    assert row["provenance"] == "legacy_import"
    assert row["measured_api_rate_estimate_nanos"] is None
    assert row["estimated_tokens"] == 400
    assert row["api_rate_estimate_nanos"] - row["estimated_api_rate_estimate_nanos"] == measured_cost + legacy_cost
    assert sum(m["total_tokens"] for m in row["model_breakdowns"]) == 700
    assert row["api_rate_estimate_nanos"] == cost["amount_nanos"]
    assert sum(m["api_rate_estimate_nanos"] or 0 for m in row["model_breakdowns"]) == row["api_rate_estimate_nanos"]


def test_unpriceable_provider_gap_keeps_unknown_model_cost_unknown():
    slot = SlotUsage(
        usage_slot=int(datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp()),
        model="unknown", input_tokens=100,
    )
    anchor = plan_codex_anchor([slot], load_rate_card(),
                               ProviderSeries(500, {"2026-09-21": 500}), tz=timezone.utc)
    doc = {"history": {
        "daily": [{"date": "2026-09-21", "total_tokens": 100, "api_rate_estimate_nanos": None,
                   "model_breakdowns": []}],
        "today_total_tokens": 100, "rolling_7d_total_tokens": 100,
        "calendar_week_total_tokens": 100, "all_time_total_tokens": 100,
    }, "breakdowns": {"models": {"items": []}}}
    apply_anchor_plan(doc["history"], {}, anchor, measured_nanos=None, legacy_nanos=0)
    reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 21))
    assert doc["history"]["daily"][0]["total_tokens"] == 500
    assert doc["history"]["daily"][0]["model_breakdowns"][0]["api_rate_estimate_nanos"] is None
    assert doc["breakdowns"]["models"]["items"][0]["api_rate_estimate_nanos"] is None


def test_fifty_measured_models_and_provider_gap_survive_proxy():
    from coordharness.usage.dashboard_proxy import _sanitize_history
    from coordharness.usage.provider_anchor import AnchorPlan

    items = [{"key": f"model-{i}", "label": f"Model {i}", "total_tokens": 1}
             for i in range(50)]
    doc = {"history": {"daily": [{"date": "2026-09-21", "total_tokens": 50,
                                  "model_breakdowns": items}],
                       "all_time_total_tokens": 50}}
    anchor = AnchorPlan(state="applied", estimated_tokens_by_day={"2026-09-21": 10})
    reconcile_provider_tokens(doc, anchor, today=date(2026, 9, 21))
    clean = _sanitize_history(doc["history"])
    assert len(clean["daily"][0]["model_breakdowns"]) == 51
    assert sum(m["total_tokens"] for m in clean["daily"][0]["model_breakdowns"]) == 60
