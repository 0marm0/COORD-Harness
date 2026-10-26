"""Dated OpenAI base tariffs, separate from current-card price scenarios.

Published changes establish dates, not invoices or private/pre-release rates.
The pre-cut cache and write values remain explicit historical assumptions;
their current ratios cannot prove that the same ratios applied in the past.
No alias or model-family lookup happens here.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .pricing import ModelRate, RateCard

RULE = "openai-dated-base-tariffs-2026-10-22-v1"
CHANGELOG = "https://developers.openai.com/api/docs/changelog"
VERIFIED_ON = "2026-10-22"
# input, output, cache read; USD per million tokens. Cache write TTLs are
# compatibility fields only and are never evidence of an observed write TTL.
_SCHEDULE = {
    "gpt-5.6-sol": ("2026-07-09", "2026-08-21", ("5", "30", ".5"), ("4", "20", ".4")),
    "gpt-5.6-terra": ("2026-07-09", "2026-07-30", ("2.5", "15", ".25"), ("2", "12", ".2")),
    "gpt-5.6-luna": ("2026-07-09", "2026-07-30", ("1", "6", ".1"), (".2", "1.2", ".02")),
}


def _utc_day(usage_slot: int | None, usage_date: str | None) -> str | None:
    slot_day = None
    if usage_slot is not None:
        if isinstance(usage_slot, bool) or not isinstance(usage_slot, int) or usage_slot < 0:
            raise ValueError("usage_slot must be a nonnegative integer UTC timestamp")
        try:
            slot_day = datetime.fromtimestamp(usage_slot, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("usage_slot is outside the supported date range") from exc
    if usage_date is not None:
        try:
            if not isinstance(usage_date, str) or date.fromisoformat(usage_date).isoformat() != usage_date:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("usage_date must be an ISO UTC day") from exc
        if slot_day is not None and slot_day != usage_date:
            raise ValueError("usage_date conflicts with the usage_slot UTC day")
    return slot_day or usage_date


def select_historical_rate(card: RateCard, model: str, usage_slot: int | None = None,
                           *, usage_date: str | None = None) -> tuple[ModelRate | None, dict[str, Any]]:
    """Select an exact model's dated base rate and bounded provenance.

    Missing dates preserve a current-rate scenario. Known historical dates
    apply the dated schedule numerically, with component-specific authority.
    The caller must disclose its own aliases and tier/context assumptions.
    """
    day = _utc_day(usage_slot, usage_date)
    rate = card.rates.get(model)
    provenance: dict[str, Any] = {
        "rule_id": RULE, "model": model, "usage_date_utc": day,
        "basis": "current_rate_api_equivalent_estimate",
        "historical_effective_dates_verified": False,
        "interval_start": None, "interval_end_exclusive": None,
        "source_urls": [], "assumptions": [], "component_authority": {},
        "verified_on": VERIFIED_ON,
    }
    if rate is None:
        provenance["reason"] = "exact_model_rate_unavailable"
        return None, provenance
    if day is None:
        provenance["assumptions"] = ["missing_usage_time_current_rate_scenario"]
        return rate, provenance
    schedule = _SCHEDULE.get(model)
    if schedule is None:
        provenance["assumptions"] = ["historical_interval_unestablished_current_rate_scenario"]
        return rate, provenance
    release, cut, before, after = schedule
    old = day < cut
    values = before if old else after
    nano = tuple(int(Decimal(value) * 1_000_000_000) for value in values)
    # Current model docs establish a 1.25x standard cache-write rate. Applying
    # that ratio before the cut is a scenario, never historical TTL authority.
    write = nano[0] * 5 // 4
    selected = replace(rate, input=nano[0], output=nano[1], cache_read=nano[2],
                       cache_write_5m=write, cache_write_1h=write)
    assumptions = []
    if day < release:
        assumptions.append("pre_public_release_model_date_rate_assumed")
    if day > VERIFIED_ON:
        assumptions.append("future_tariff_interval_not_verified")
    if old:
        assumptions.extend(("historical_cached_input_rate_assumed_from_retained_schedule",
                            "historical_cache_write_ratio_assumed_from_current_rule"))
    else:
        assumptions.append("compatibility_cache_write_buckets_do_not_establish_TTL")
    authority = {
        "input": "dated_official_price_change" if day >= release else "pre_release_rate_assumption",
        "output": "dated_official_price_change" if day >= release else "pre_release_rate_assumption",
        "cache_read": "historical_schedule_assumption" if old else "official_current_model_rate",
        "cache_write_5m": "historical_ratio_and_TTL_assumption" if old else "official_current_ratio_TTL_unknown",
        "cache_write_1h": "historical_ratio_and_TTL_assumption" if old else "official_current_ratio_TTL_unknown",
    }
    provenance.update(
        basis="historical_api_equivalent_estimate_with_assumptions",
        historical_effective_dates_verified=release <= day <= VERIFIED_ON,
        interval_start=release if old else cut,
        interval_end_exclusive=cut if old else None,
        source_urls=[CHANGELOG, f"https://developers.openai.com/api/docs/models/{model}"],
        assumptions=assumptions, component_authority=authority,
        historical_components_verified=not old and release <= day <= VERIFIED_ON,
        current_card_rates_changed=selected != rate,
    )
    return selected, provenance
