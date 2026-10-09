"""Bundled Astra/Sol Standard prices and honest request-adjustment scope."""
from __future__ import annotations

from coordharness.usage.pricing import load_rate_card

import pytest


@pytest.mark.parametrize(
    "component,astra_nanos,sol_nanos",
    [
        ("input_tokens", 10_000_000_000, 2_000_000_000),
        ("output_tokens", 50_000_000_000, 10_000_000_000),
        ("cache_read_tokens", 1_000_000_000, 100_000_000),
        ("cache_create_5m_tokens", 12_500_000_000, 2_500_000_000),
        ("cache_create_1h_tokens", 12_500_000_000, 2_500_000_000),
        ("cache_create_other_tokens", 12_500_000_000, 2_500_000_000),
    ],
)
def test_current_codex_models_have_documented_bundled_standard_rates(
    component: str, astra_nanos: int, sol_nanos: int
) -> None:
    # Official model docs, verified 2026-10-09. Loading the bundled card,
    # without a network refresh/override, is the offline-install regression.
    card = load_rate_card()
    for model, expected in (("gpt-6-astra", astra_nanos), ("gpt-6.1-sol", sol_nanos)):
        priced = card.price(model, {component: 1_000_000})
        assert priced.resolved_model == model
        assert priced.amount_nanos == expected
        assert priced.priced_tokens == 1_000_000
        assert priced.unpriced_tokens == 0


@pytest.mark.parametrize("model,expected", [("gpt-6-astra", 2_150_000), ("gpt-6.1-sol", 340_000)])
def test_cached_codex_prefix_is_priced_separately(model: str, expected: int) -> None:
    # A receipt with inclusive input 1000, cached input 900, and output 5
    # becomes fresh input 100 and cache-read 900 before reaching pricing.
    priced = load_rate_card().price(
        model, {"input_tokens": 100, "cache_read_tokens": 900, "output_tokens": 5}
    )
    assert priced.amount_nanos == expected
    assert priced.priced_tokens == 1005


def test_new_sol_is_distinct_from_prior_sol_cached_rate() -> None:
    card = load_rate_card()
    assert card.rates["gpt-6.1-sol"].cache_read == 100_000_000
    assert card.rates["gpt-6-sol"].cache_read == 200_000_000
    assert "gpt-6.1-sol" not in card.aliases


def test_no_undocumented_astra_alias_is_assumed() -> None:
    priced = load_rate_card().price("astra", {"input_tokens": 3})
    assert priced.amount_nanos is None
    assert priced.unpriced_tokens == 3


def test_provenance_discloses_request_pricing_limits() -> None:
    provenance = load_rate_card().provenance()
    assert provenance["canonical"] is False
    assert provenance["semantics"] == "self_computed_api_list_price_estimate"
    assert "Standard rates" in provenance["warning"]
    assert "service-tier" in provenance["warning"]
    assert "long-context" in provenance["warning"]
    assert "subscription plan is not billed this way" in provenance["warning"]
