"""Derived current base-rate prices over immutable legacy token aggregates.

Canonical input is already fresh-only. Original imported costs are reference
evidence; this module never mutates rows or infers request context or tiers.
Only exact model/rate pairs independently verified in official sources enter
the derived amount. A catalog's Spark price is preserved as reference only.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from .pricing import RateCard

COMPONENTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_create_5m_tokens", "cache_create_1h_tokens", "cache_create_other_tokens")
OFFICIAL_BASE_RATES = {
    "gpt-5.4": ("2.5", "15", ".25"),
    "gpt-5.4-mini": (".75", "4.5", ".075"),
    "gpt-5.5": ("5", "30", ".5"),
    "gpt-5.6-sol": ("4", "20", ".4"),
    "gpt-5.6-terra": ("2", "12", ".2"),
}
SOURCE_URLS = tuple(f"https://developers.openai.com/api/docs/models/{model}" for model in OFFICIAL_BASE_RATES)
BASIS = "repriced_legacy_current_api_estimate"


def _value(row: Any, field: str, default: Any = None) -> Any:
    return row.get(field, default) if isinstance(row, Mapping) else getattr(row, field, default)


def price_legacy_row(row: Any, card: RateCard | None, *, preserve_recorded: bool = False) -> dict[str, Any]:
    metrics = {key: _value(row, key, 0) for key in COMPONENTS}
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in metrics.values()):
        raise ValueError("legacy token counts must be nonnegative integers")
    model = _value(row, "model", "")
    original = _value(row, "api_rate_estimate_nanos")
    original = original if isinstance(original, int) and not isinstance(original, bool) and original >= 0 else None
    priced = card.price(model, metrics) if card is not None else None
    resolved, reason = card.resolve(model) if card is not None else (None, "rate_card_unavailable")
    authority_unknown = False
    if resolved is not None:
        expected = OFFICIAL_BASE_RATES.get(model)
        if resolved != model:
            reason, authority_unknown = "legacy_model_alias_not_independently_verified", True
        elif expected is None:
            reason, authority_unknown = "legacy_model_rate_authority_unverified", True
        else:
            rate = card.rates[resolved]
            expected_nanos = tuple(int(Decimal(value) * 1_000_000_000) for value in expected)
            if rate.provider != "openai" or card.currency != "USD":
                reason, authority_unknown = "legacy_provider_or_currency_not_verified", True
            elif (rate.input, rate.output, rate.cache_read) != expected_nanos:
                reason, authority_unknown = "legacy_effective_rate_differs_from_verified_official_rate", True
            elif any(metrics[key] for key in COMPONENTS if key.startswith("cache_create")):
                reason, authority_unknown = "legacy_cache_write_TTL_and_input_inclusion_unverified", True
            else:
                reason = None
    amount = priced.amount_nanos if priced is not None and reason is None else None
    # A recorded historical estimate may include the tariff and request
    # premiums captured then. Repricing it at today's cheaper base rates
    # erases that evidence. Unknown or unsupported model estimates remain
    # references, and this does not assert an invoice or verified old tariff.
    recorded = preserve_recorded and model in OFFICIAL_BASE_RATES and original is not None
    if recorded:
        amount, reason, authority_unknown = original, None, False
    if preserve_recorded and model in OFFICIAL_BASE_RATES and original is None:
        amount, reason = None, "legacy_recorded_estimate_missing_or_invalid"
    tokens = sum(metrics.values())
    return {"date": _value(row, "usage_date"), "model": model, "resolved_model": resolved,
        "amount_nanos": amount, "original_amount_nanos": original,
        "recorded_estimate_preserved": recorded,
        "card_reference_amount_nanos": priced.amount_nanos if priced is not None else None,
        "priced_tokens": tokens if amount is not None else 0,
        "unpriced_tokens": tokens if amount is None else 0,
        "rate_authority_unverified_tokens": tokens if authority_unknown else 0,
        "reason": reason}


def derive_legacy_pricing(rows: Iterable[Any], card: RateCard | None, *, preserve_recorded: bool = False) -> dict[str, Any]:
    prices = [price_legacy_row(row, card, preserve_recorded=preserve_recorded) for row in rows]
    known = [row["amount_nanos"] for row in prices if row["amount_nanos"] is not None]
    return {"schema": "legacy-recorded-pricing.v2" if preserve_recorded else "legacy-current-pricing.v1", "basis": "retained_recorded_legacy_api_estimate" if preserve_recorded else BASIS,
        "price_rule": "legacy-recorded-estimates-2026-10-22-v2" if preserve_recorded else "legacy-official-base-2026-10-22-v1",
        "amount_nanos": sum(known) if known else None,
        "original_amount_nanos": sum(row["original_amount_nanos"] or 0 for row in prices),
        "card_reference_amount_nanos": sum(row["card_reference_amount_nanos"] or 0 for row in prices),
        "unpriced_original_amount_nanos": sum(row["original_amount_nanos"] or 0 for row in prices if row["amount_nanos"] is None),
        "rows_count": len(prices), "priced_rows": len(known),
        "unpriced_rows": len(prices)-len(known),
        **{key: sum(row[key] for row in prices) for key in ("priced_tokens", "unpriced_tokens", "rate_authority_unverified_tokens")},
        "request_premiums_applied": False, "historical_effective_dates_verified": False,
        "pricing_key": card.pricing_key if card is not None else None,
        "rate_card_digest": card.digest if card is not None else None,
        "source_urls": list(SOURCE_URLS), "rows": prices,
        "warning": ("Recorded historical estimates retained for supported models. Current prices remain comparison evidence. Original tiers and billed charges are unverified." if preserve_recorded else "Legacy tokens repriced at verified current base API rates. Original costs remain reference evidence. Request context, tiers and historical billed rates are unknown.")}


def public_legacy_pricing(pricing: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in pricing.items() if key in {
        "schema", "basis", "price_rule", "amount_nanos", "original_amount_nanos",
        "card_reference_amount_nanos", "unpriced_original_amount_nanos", "rows_count", "priced_rows",
        "unpriced_rows", "priced_tokens", "unpriced_tokens", "rate_authority_unverified_tokens",
        "request_premiums_applied", "historical_effective_dates_verified", "pricing_key", "rate_card_digest",
        "source_urls", "warning",
    }}
