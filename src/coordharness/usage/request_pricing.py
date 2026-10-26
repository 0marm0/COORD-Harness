"""Current API list-price adjustments for preclassified request bands.

The effective RateCard remains the base-price authority. Bands classify actual
requests before aggregation: an aggregate token total is never a context size.
Unknown tier, speed, region and historical effective dates stay disclosed.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone, tzinfo
from typing import Any

from .pricing import RateCard
from .historical_tariffs import select_historical_rate

PRICE_RULE = "official-dated-with-assumptions-2026-10-22-v2"
SOURCE_URLS = (
    "https://developers.openai.com/api/docs/pricing",
    "https://developers.openai.com/api/docs/changelog",
    "https://platform.claude.com/docs/en/about-claude/pricing",
    "https://platform.claude.com/docs/en/build-with-claude/fast-mode",
)
COMPONENTS = {
    "input_tokens": "input", "output_tokens": "output",
    "cache_read_tokens": "cache_read", "cache_create_5m_tokens": "cache_write_5m",
    "cache_create_1h_tokens": "cache_write_1h", "cache_create_other_tokens": "cache_write_5m",
}
OPENAI_CONTEXT = frozenset({"gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"})
CLAUDE_FAST = frozenset({"claude-opus-4-8", "claude-opus-5", "claude-opus-5-5"})
CLAUDE_REGIONAL = frozenset({"claude-fable-5", "claude-fable-5-1", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5", "claude-sonnet-4-6", "claude-sonnet-5", "claude-sonnet-5-5", "claude-haiku-5-5"})


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def price_band(model: str, tokens: Mapping[str, Any], *, card: RateCard,
               context_long: bool | None, request_count: int = 1,
               service_tier: str | None = None, inference_speed: str | None = None,
               region: str | None = None, usage_slot: int | None = None) -> dict[str, Any]:
    """Return integer cost numerators and adjustment provenance for one band.

    Divide numerators by 1,000,000 for nano-USD after summing disjoint bands.
    Nonzero OpenAI writes retain the card's base price and suppress unsupported
    adjustments because TTL and inclusion in input are not yet established.
    """
    count = _count(request_count, "request_count")
    values = {key: _count(tokens.get(key, 0), key) for key in COMPONENTS}
    resolved, reason = card.resolve(model)
    coverage = {key: 0 for key in (
        "requests", "priced_requests", "unpriced_requests", "context_adjusted_requests",
        "speed_adjusted_requests", "unknown_service_tier_requests", "unknown_inference_speed_requests",
        "unknown_region_requests", "unsupported_cache_write_requests", "assumed_alias_requests",
        "unknown_context_requests", "unsupported_adjustment_requests",
        "historical_rate_adjusted_requests", "historical_rate_unverified_requests",
    )}
    coverage["requests"] = count
    assumptions: set[str] = set()
    result: dict[str, Any] = {"model": model, "resolved_model": resolved, "reason": reason,
        "base_numerator": None, "adjusted_numerator": None, "addition_numerator": None,
        "coverage": coverage, "assumptions": []}
    if resolved is None:
        # A synthetic non-call with zero tokens is separate from unpriced work.
        coverage["unpriced_requests"] = count if any(values.values()) else 0
        return result
    coverage["priced_requests"] = count
    if resolved != model:
        coverage["assumed_alias_requests"] = count
        assumptions.add("resolved_model_uses_declared_alias_or_family_rate")
    rate = card.rates[resolved]
    base = sum(values[key] * rate.nanos_per_million(component) for key, component in COMPONENTS.items())
    # Keep the current-card base so existing daily base costs can add the
    # single delta below. Only request-slot UTC time selects a dated tariff;
    # local daily aggregates cannot safely resolve a midnight cutover.
    if usage_slot is not None and model == resolved:
        dated_rate, proof = select_historical_rate(card, model, usage_slot)
        assumptions.update(proof["assumptions"])
        if dated_rate is not None:
            if dated_rate != rate:
                coverage["historical_rate_adjusted_requests"] = count
            rate = dated_rate
        if not proof.get("historical_components_verified", False):
            coverage["historical_rate_unverified_requests"] = count
    adjusted = sum(values[key] * rate.nanos_per_million(component) for key, component in COMPONENTS.items())
    unsupported = False
    is_openai = rate.provider == "openai"
    context_supported = resolved in OPENAI_CONTEXT or resolved == "claude-haiku-5-5"
    # SQLite CASE/grouping expressions expose booleans as integer 0/1.
    context_flag = context_long if isinstance(context_long, bool) else (bool(context_long) if isinstance(context_long, int) and context_long in (0, 1) else None)
    if context_supported and context_flag is None:
        coverage["unknown_context_requests"] = count
        assumptions.add("unknown_request_context_standard_context_scenario")
    elif context_flag is not None:
        coverage["unknown_context_requests"] = 0
        assumptions.discard("unknown_request_context_standard_context_scenario")
    long = context_flag is True and context_supported
    if long:
        coverage["context_adjusted_requests"] = count
        if resolved == "claude-haiku-5-5":
            adjusted *= 5
        else:
            scaled = sum(values[key] * rate.nanos_per_million(component) * (3 if component == "output" else 4) for key, component in COMPONENTS.items())
            if scaled % 2:
                unsupported = True
                assumptions.add("effective_rate_precision_cannot_represent_adjustment")
            else:
                adjusted = scaled // 2
    tier = service_tier
    tiers = {"standard": (1, 1), "batch": (1, 2)}
    if is_openai:
        tier = {"default": "standard", "priority": "fast"}.get(tier, tier)
        if resolved in OPENAI_CONTEXT:
            tiers.update({"fast": (2, 1), "flex": (1, 2)})
        if resolved in {"gpt-6-astra", "gpt-6.1-sol"}:
            tiers["ultrafast"] = (6, 1)
    if tier not in tiers:
        coverage["unknown_service_tier_requests"] = count
        assumptions.add("unknown_service_tier_standard_scenario")
    tier_num, tier_den = tiers.get(tier, (1, 1))
    if not is_openai and resolved in CLAUDE_FAST:
        if inference_speed == "fast":
            if tier == "batch":
                unsupported = True
                assumptions.add("claude_fast_batch_combination_unsupported")
            else:
                tier_num, tier_den = 2, 1
                coverage["speed_adjusted_requests"] = count
        elif inference_speed != "standard" and tier != "batch":
            coverage["unknown_inference_speed_requests"] = count
            assumptions.add("unknown_inference_speed_standard_scenario")
    if is_openai and tier in {"fast", "ultrafast"}:
        coverage["speed_adjusted_requests"] = count
    premium_region = (resolved in OPENAI_CONTEXT and region in {"us", "us_only", "eu", "regional", "fedramp"}) if is_openai else region in {"us", "us_only"} and resolved in CLAUDE_REGIONAL
    if region != "global" and not premium_region:
        coverage["unknown_region_requests"] = count
        assumptions.add("unknown_region_global_scenario")
    region_num, region_den = (11, 10) if premium_region else (1, 1)
    adjusted = adjusted * tier_num * region_num
    divisor = tier_den * region_den
    if adjusted % divisor:
        unsupported = True
        assumptions.add("effective_rate_precision_cannot_represent_adjustment")
    else:
        adjusted //= divisor
    if is_openai and any(values[key] for key in COMPONENTS if key.startswith("cache_create")):
        coverage["unsupported_cache_write_requests"] = count
        unsupported = True
        assumptions.add("openai_cache_write_TTL_and_input_inclusion_unverified_base_retained")
    if unsupported:
        coverage["unsupported_adjustment_requests"] = count
        adjusted = base
        result["reason"] = "request_adjustment_unsupported_base_retained"
        coverage["context_adjusted_requests"] = coverage["speed_adjusted_requests"] = 0
    result.update(base_numerator=base, adjusted_numerator=adjusted,
                  addition_numerator=adjusted-base, assumptions=sorted(assumptions))
    return result


def request_adjustments(imported: Any, card: RateCard, tz: tzinfo | None = None) -> dict[str, Any]:
    """Summarize already classified request bands for local and UTC days."""
    bands = imported.get("request_pricing_bands", ()) if isinstance(imported, Mapping) else getattr(imported, "request_pricing_bands", ())
    coverage: dict[str, int] = defaultdict(int)
    assumptions: set[str] = set()
    local: dict[tuple[str, str], dict[str, int]] = {}
    utc: dict[tuple[str, str], dict[str, int]] = {}
    totals = {"base_numerator": 0, "adjusted_numerator": 0, "addition_numerator": 0}
    for band in bands:
        if not isinstance(band, Mapping):
            raise ValueError("request pricing band must be a mapping")
        price = price_band(str(band.get("model", "")), band, card=card,
            context_long=band.get("context_long"), request_count=band.get("request_count", 1),
            service_tier=band.get("service_tier"), inference_speed=band.get("inference_speed"), region=band.get("region"), usage_slot=band.get("usage_slot"))
        for key, value in price["coverage"].items():
            coverage[key] += value
        assumptions.update(price["assumptions"])
        if price["base_numerator"] is None:
            continue
        moment = datetime.fromtimestamp(_count(band.get("usage_slot"), "usage_slot"), timezone.utc)
        local_day = (moment.astimezone(tz) if tz is not None else moment.astimezone()).date().isoformat()
        for table, day in [(local, local_day), (utc, moment.date().isoformat())]:
            row = table.setdefault((day, price["model"]), {"base_numerator": 0, "adjusted_numerator": 0, "addition_numerator": 0, "request_count": 0})
            for key in totals:
                row[key] += price[key]
            row["request_count"] += price["coverage"]["requests"]
        for key in totals:
            totals[key] += price[key]

    def serialize(table: dict) -> list[dict[str, Any]]:
        return [{"date": day, "model": model, "request_count": row["request_count"],
                 **{key.replace("_numerator", "_nanos"): row[key] // 1_000_000 for key in totals}}
                for (day, model), row in sorted(table.items())]

    return {"schema": "request-pricing-adjustments.v1", "basis": "date_aware_api_equivalent_estimate_with_assumptions",
        "price_rule": PRICE_RULE, "historical_effective_dates_verified": False,
        "source_urls": list(SOURCE_URLS), "pricing_key": card.pricing_key,
        "rate_card_digest": card.digest, "currency": card.currency,
        **{key.replace("_numerator", "_nanos"): value // 1_000_000 for key, value in totals.items()},
        "by_day_model": serialize(local), "by_day_model_utc": serialize(utc),
        "coverage": dict(coverage), "assumptions": sorted(assumptions)}


def public_request_pricing(adjustments: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded public provenance; never forward request or day/model rows."""
    return {key: adjustments[key] for key in (
        "schema", "basis", "price_rule", "historical_effective_dates_verified",
        "source_urls", "pricing_key", "rate_card_digest", "currency",
        "base_nanos", "adjusted_nanos", "addition_nanos", "coverage", "assumptions",
    ) if key in adjustments}
