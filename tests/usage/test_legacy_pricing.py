"""Immutable legacy aggregates get a separately derived, partial current price."""
from dataclasses import asdict
import json
from pathlib import Path

from coordharness.usage.ledger import DailyUsage
from coordharness.usage.legacy_pricing import derive_legacy_pricing, price_legacy_row, public_legacy_pricing
from coordharness.usage.pricing import load_rate_card, parse_rate_card


def test_canonical_fresh_input_is_not_subtracted_or_cache_counted_twice():
    row = DailyUsage(usage_date="2026-07-01", model="gpt-5.6-sol", input_tokens=100,
                     cache_read_tokens=900, output_tokens=10, api_rate_estimate_nanos=9999)
    original = asdict(row)
    price = price_legacy_row(row, load_rate_card())
    assert price["amount_nanos"] == 960000
    assert price["original_amount_nanos"] == 9999
    assert asdict(row) == original


def test_unknown_original_cost_is_reference_not_a_verified_charge():
    row = DailyUsage(usage_date="2026-07-01", model="unknown", input_tokens=100,
                     api_rate_estimate_nanos=7777)
    result = derive_legacy_pricing([row], load_rate_card())
    assert result["amount_nanos"] is None
    assert result["original_amount_nanos"] == 7777
    assert result["unpriced_original_amount_nanos"] == 7777
    assert result["unpriced_tokens"] == 100


def test_spark_rate_is_quarantined_even_when_catalog_has_an_exact_entry():
    row = DailyUsage(usage_date="2026-04-01", model="gpt-5.3-codex-spark", input_tokens=100,
                     api_rate_estimate_nanos=123)
    price = price_legacy_row(row, load_rate_card())
    assert price["amount_nanos"] is None
    assert price["card_reference_amount_nanos"] == 175000
    assert price["rate_authority_unverified_tokens"] == 100


def test_effective_rate_change_requires_fresh_official_verification():
    document = json.loads(Path(__file__).resolve().parents[2].joinpath("src/coordharness/usage/rate_card.json").read_text())
    document["models"]["gpt-5.6-sol"]["input"] = 5
    row = DailyUsage(usage_date="2026-07-01", model="gpt-5.6-sol", input_tokens=100)
    result = price_legacy_row(row, parse_rate_card(json.dumps(document)))
    assert result["amount_nanos"] is None
    assert result["reason"] == "legacy_effective_rate_differs_from_verified_official_rate"


def test_no_request_premium_is_inferred_from_a_daily_total():
    row = DailyUsage(usage_date="2026-07-01", model="gpt-5.6-sol", input_tokens=1_000_000)
    result = derive_legacy_pricing([row], load_rate_card())
    assert result["amount_nanos"] == 4_000_000_000
    assert result["request_premiums_applied"] is False
    assert result["historical_effective_dates_verified"] is False
    assert "rows" not in public_legacy_pricing(result)


def test_declared_alias_is_not_silently_used_for_legacy_identity():
    row = DailyUsage(usage_date="2026-07-01", model="codex-auto-review", input_tokens=100)
    result = price_legacy_row(row, load_rate_card())
    assert result["amount_nanos"] is None
    assert result["reason"] == "legacy_model_alias_not_independently_verified"
