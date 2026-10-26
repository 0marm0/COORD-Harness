"""Request-band pricing must conserve base costs and expose missing metadata."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from coordharness.usage.pricing import load_rate_card, parse_rate_card
from coordharness.usage.request_pricing import price_band, request_adjustments


def card():
    base = json.loads(Path(__file__).resolve().parents[1].joinpath("src/coordharness/usage/rate_card.json").read_text())
    base["models"]["claude-haiku-5-5"] = {"provider": "anthropic", "input": .1, "output": .5, "cache_read": .01, "cache_write_5m": .125, "cache_write_1h": .2}
    return parse_rate_card(json.dumps(base))


def test_context_classification_is_supplied_not_derived_from_aggregate():
    rate = card()
    short = price_band("gpt-6.1-sol", {"input_tokens": 400000}, card=rate, context_long=False, request_count=2)
    long = price_band("gpt-6.1-sol", {"input_tokens": 400000}, card=rate, context_long=True)
    assert short["addition_numerator"] == 0
    assert long["adjusted_numerator"] == short["base_numerator"] * 2
    assert price_band("gpt-6.1-sol", {"input_tokens": 400000}, card=rate, context_long=1)["adjusted_numerator"] == long["adjusted_numerator"]


def test_haiku_all_components_are_multiplied_including_output():
    tokens = {"input_tokens": 1, "cache_read_tokens": 80000, "cache_create_1h_tokens": 20000, "output_tokens": 42}
    price = price_band("claude-haiku-5-5", tokens, card=card(), context_long=True)
    assert price["adjusted_numerator"] == price["base_numerator"] * 5


def test_effective_card_and_alias_are_respected():
    rate = card()
    result = price_band("codex-auto-review", {"input_tokens": 300000}, card=rate, context_long=True)
    assert result["resolved_model"] == "gpt-5.6-sol"
    assert result["base_numerator"] // 1_000_000 == rate.price("codex-auto-review", {"input_tokens": 300000}).amount_nanos
    assert result["coverage"]["assumed_alias_requests"] == 1


def test_reasoning_is_not_an_additional_output_component():
    price = price_band("gpt-6.1-sol", {"output_tokens": 100, "reasoning_output_tokens": 90}, card=card(), context_long=False)
    assert price["base_numerator"] == 100 * 10_000_000_000


def test_unknown_tier_region_and_speed_are_disclosed():
    price = price_band("claude-opus-4-8", {"input_tokens": 1}, card=card(), context_long=False, request_count=7)
    for key in ["unknown_service_tier_requests", "unknown_region_requests", "unknown_inference_speed_requests"]:
        assert price["coverage"][key] == 7


def test_claude_speed_is_separate_from_capacity_tier():
    price = price_band("claude-opus-4-8", {"input_tokens": 100}, card=card(), context_long=False, service_tier="standard", inference_speed="fast", region="global")
    assert price["adjusted_numerator"] == price["base_numerator"] * 2
    assert price["coverage"]["unknown_service_tier_requests"] == 0


def test_openai_tier_context_and_region_stack():
    price = price_band("gpt-6.1-sol", {"input_tokens": 300000, "output_tokens": 100}, card=card(), context_long=True, service_tier="priority", region="us")
    expected_context = 300000 * 4_000_000_000 + 100 * 15_000_000_000
    assert price["adjusted_numerator"] == expected_context * 22 // 10


def test_unknown_model_preserves_none():
    price = price_band("unknown", {"input_tokens": 100}, card=card(), context_long=True)
    assert price["base_numerator"] is None
    assert price["addition_numerator"] is None
    assert price["coverage"]["unpriced_requests"] == 1


def test_openai_nonzero_writes_retain_base_and_refuse_adjustment():
    price = price_band("gpt-6.1-sol", {"input_tokens": 300000, "cache_create_5m_tokens": 1}, card=card(), context_long=True)
    assert price["adjusted_numerator"] == price["base_numerator"]
    assert price["coverage"]["unsupported_cache_write_requests"] == 1
    assert price["reason"] == "request_adjustment_unsupported_base_retained"


def test_local_utc_rollups_and_delta_contract():
    slot = int(datetime(2026, 10, 22, 1, tzinfo=timezone.utc).timestamp())
    imported = SimpleNamespace(request_pricing_bands=({"usage_slot": slot, "model": "gpt-6.1-sol", "input_tokens": 300000, "output_tokens": 100, "context_long": 1, "request_count": 1},))
    result = request_adjustments(imported, card(), ZoneInfo("America/New_York"))
    assert result["by_day_model"][0]["date"] == "2026-10-21"
    assert result["by_day_model_utc"][0]["date"] == "2026-10-22"
    assert result["adjusted_nanos"] == result["base_nanos"] + result["addition_nanos"]
    assert result["basis"] == "date_aware_api_equivalent_estimate_with_assumptions"
    assert result["historical_effective_dates_verified"] is False


def test_no_bands_means_no_adjustment():
    assert request_adjustments(SimpleNamespace(), load_rate_card())["addition_nanos"] == 0


def test_fractional_multiplier_is_applied_after_token_product():
    raw = json.loads(Path(__file__).resolve().parents[1].joinpath("src/coordharness/usage/rate_card.json").read_text())
    raw["models"]["gpt-6.1-sol"]["output"] = .000000001
    rate = parse_rate_card(json.dumps(raw))
    price = price_band("gpt-6.1-sol", {"output_tokens": 2}, card=rate, context_long=True)
    assert price["adjusted_numerator"] == 3
