"""Pricing-engine tests.

These pin the three defects that made the dashboard read $0.00 for a busy day:
cost that existed only as a third-party passthrough, replayed transcript
records counted more than once, and Codex's cached prefix counted twice and
priced at the full input rate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.usage.local_history import discover_local_cli_history
from coordharness.usage.pricing import RateCardError, load_rate_card, parse_rate_card

pytestmark = pytest.mark.unit

_CARD = {
    "rate_card_version": 1,
    "pricing_key": "test-card-v1",
    "currency": "USD",
    "unit": "usd_per_million_tokens",
    "aliases": {"opus": "claude-opus-5"},
    "unpriced": {"codex-auto-review": "no published list price"},
    "models": {
        "claude-opus-5": {
            "provider": "anthropic",
            "input": 5,
            "output": 25,
            "cache_read": 0.5,
            "cache_write_5m": 6.25,
            "cache_write_1h": 10,
        }
    },
}


def _card(**overrides: object):
    return parse_rate_card(json.dumps({**_CARD, **overrides}))


def test_vendored_rate_card_loads_and_prices_a_known_rate() -> None:
    card = load_rate_card()

    assert card.currency == "USD"
    assert card.rates
    # One million output tokens on Opus 5 is $25.00 exactly.
    assert card.price("claude-opus-5", {"output_tokens": 1_000_000}).amount_nanos == 25_000_000_000


def test_each_token_component_is_priced_at_its_own_published_rate() -> None:
    card = _card()

    priced = card.price(
        "claude-opus-5",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "cache_create_5m_tokens": 1_000_000,
            "cache_create_1h_tokens": 1_000_000,
        },
    )

    # 5 + 25 + 0.5 + 6.25 + 10 = 46.75 USD
    assert priced.amount_nanos == 46_750_000_000
    assert priced.priced_tokens == 5_000_000
    assert priced.unpriced_tokens == 0


def test_an_undifferentiated_cache_write_uses_the_five_minute_rate() -> None:
    card = _card()

    priced = card.price("claude-opus-5", {"cache_create_other_tokens": 1_000_000})

    assert priced.amount_nanos == 6_250_000_000


def test_summing_happens_before_the_single_division() -> None:
    """No component is independently rounded, so small counts stay exact."""

    card = _card()

    # 3 input tokens at $5/Mtok is 15_000 nano-USD; a per-component floor at
    # nano granularity would still be exact, but a naive per-component divide by
    # a million would floor each to zero.
    assert card.price("claude-opus-5", {"input_tokens": 3}).amount_nanos == 15_000


def test_an_unknown_model_is_unpriced_rather_than_free() -> None:
    card = _card()

    priced = card.price("some-model-we-never-saw", {"input_tokens": 1_000})

    assert priced.amount_nanos is None
    assert priced.priced is False
    assert priced.priced_tokens == 0
    assert priced.unpriced_tokens == 1_000
    assert priced.reason == "model_not_in_rate_card"


def test_a_declared_unpriced_label_carries_its_reason() -> None:
    card = _card()

    priced = card.price("codex-auto-review", {"input_tokens": 7})

    assert priced.amount_nanos is None
    assert priced.reason == "no published list price"
    assert priced.unpriced_tokens == 7


def test_aliases_and_build_suffixes_resolve_to_the_published_model() -> None:
    card = _card()

    assert card.price("opus", {"input_tokens": 1_000_000}).resolved_model == "claude-opus-5"
    assert (
        card.price("claude-opus-5-20260101", {"input_tokens": 1}).resolved_model == "claude-opus-5"
    )


@pytest.mark.parametrize(
    "broken",
    [
        {"rate_card_version": 0},
        {"currency": "DOLLARS"},
        {"models": {}},
        {"models": {"m": {"provider": "x", "input": -1, "output": 1}}},
        {"models": {"m": {"provider": "x", "input": 1e9, "output": 1}}},
        {"models": {"m": {"provider": "x", "output": 1}}},
        {"models": {"m": {"provider": "", "input": 1, "output": 1}}},
    ],
)
def test_a_malformed_rate_card_is_refused(broken: dict) -> None:
    with pytest.raises(RateCardError):
        _card(**broken)


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _claude_record(message_id: str, day: str = "2026-09-03") -> dict:
    return {
        "timestamp": f"{day}T12:00:00+00:00",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    }


def test_a_replayed_claude_message_is_counted_once(tmp_path: Path) -> None:
    """A resumed session replays earlier turns; the usage is not new spend."""

    root = tmp_path / ".claude"
    _write(root / "projects" / "a" / "first.jsonl", [_claude_record("msg-1")])
    # The same message replayed into a resumed transcript, plus one new message.
    _write(
        root / "projects" / "b" / "resumed.jsonl",
        [_claude_record("msg-1"), _claude_record("msg-2")],
    )

    imported = discover_local_cli_history(root, provider="claude")

    assert sum(row.input_tokens for row in imported.rows) == 200
    assert imported.records_accepted == 2
    assert imported.records_deduplicated == 1


def test_codex_input_tokens_exclude_the_cached_prefix(tmp_path: Path) -> None:
    """Codex reports input inclusive of cache; counting both double-counts."""

    root = tmp_path / ".codex"
    _write(
        root / "sessions" / "s.jsonl",
        [
            {
                "timestamp": "2026-09-19T12:00:00+00:00",
                "ordinal": 1,
                "payload": {
                    "thread_id": "t-1",
                    "model": "gpt-5.6-sol",
                    "last_token_usage": {
                        "input_tokens": 1_000,
                        "cached_input_tokens": 900,
                        "output_tokens": 5,
                    },
                },
            }
        ],
    )

    imported = discover_local_cli_history(root, provider="codex")
    row = imported.rows[0]

    assert row.input_tokens == 100
    assert row.cache_read_tokens == 900
    # The day's volume is the reported input plus output, counted once.
    assert row.input_tokens + row.cache_read_tokens + row.output_tokens == 1_005


def test_a_cached_prefix_larger_than_reported_input_is_clamped(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    _write(
        root / "sessions" / "s.jsonl",
        [
            {
                "timestamp": "2026-09-19T12:00:00+00:00",
                "ordinal": 1,
                "payload": {
                    "thread_id": "t-1",
                    "model": "gpt-5.6-sol",
                    "last_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 999,
                        "output_tokens": 1,
                    },
                },
            }
        ],
    )

    row = discover_local_cli_history(root, provider="codex").rows[0]

    assert row.input_tokens == 0
    assert row.cache_read_tokens == 10


def test_priced_coverage_crosses_the_proxy_so_a_caption_need_not_guess() -> None:
    """`history.pricing_coverage` must survive sanitization.

    The dense card captions its cost with how much of the observed volume the
    rate card could price. When that field was stripped here, the caption fell
    back to our measured tokens over the provider's quota-reported tokens --
    two different accountings -- and reported "15% of today priced" for a Codex
    day priced in full.
    """

    from tests.usage.test_dashboard_proxy import _payload
    from coordharness.usage.dashboard_proxy import validate_usage_dashboard

    payload = _payload()
    history = payload["providers"]["claude"]["history"]
    history["pricing_coverage"] = {
        "priceable_tokens_today": 8_333_619,
        "unpriced_tokens_today": 0,
        "priced_coverage_percent": 100.0,
        "semantics": "rate_card_priced_vs_unpriceable_local_tokens",
        # Internal detail that must NOT cross: the rate-card identity and the
        # per-model reasons are not a caption's business.
        "rate_card_digest": "9" * 64,
        "unpriced_models": [{"label": "m", "reason": "unknown_model"}],
        "priceable_tokens_all_time": 38_863_095_998,
    }
    clean = validate_usage_dashboard(payload)["providers"]["claude"]["history"]

    coverage = clean["pricing_coverage"]
    assert coverage["priced_coverage_percent"] == 100.0
    assert coverage["priceable_tokens_today"] == 8_333_619
    assert coverage["unpriced_tokens_today"] == 0
    assert coverage["semantics"] == "rate_card_priced_vs_unpriceable_local_tokens"
    assert "rate_card_digest" not in coverage
    assert "unpriced_models" not in coverage
    assert "priceable_tokens_all_time" not in coverage


def test_a_dashboard_without_pricing_coverage_is_unchanged() -> None:
    from tests.usage.test_dashboard_proxy import _payload
    from coordharness.usage.dashboard_proxy import validate_usage_dashboard

    payload = _payload()
    payload["providers"]["claude"]["history"].pop("pricing_coverage", None)
    clean = validate_usage_dashboard(payload)["providers"]["claude"]["history"]
    assert "pricing_coverage" not in clean


def test_an_out_of_range_priced_coverage_percent_is_clamped_not_forwarded() -> None:
    from tests.usage.test_dashboard_proxy import _payload
    from coordharness.usage.dashboard_proxy import validate_usage_dashboard

    payload = _payload()
    payload["providers"]["claude"]["history"]["pricing_coverage"] = {
        "priced_coverage_percent": 143.7,
    }
    clean = validate_usage_dashboard(payload)["providers"]["claude"]["history"]
    assert clean["pricing_coverage"]["priced_coverage_percent"] == 100.0


def test_an_unpriceable_model_reaches_the_coverage_block_rather_than_reading_free(
    tmp_path: Path,
) -> None:
    """A model with no rate must show up as unpriced volume, not as $0 of spend.

    Codex left 440,774,839 tokens over 14 days under the label `unknown`,
    which resolves to no rate. The cost those tokens carry is genuinely not
    known -- and the honest report of that is a visible unpriced count beside
    a coverage percentage, not a rate imputed to make the figure look whole.
    """

    from datetime import datetime, timezone

    from coordharness.usage.local_service import _history

    root = tmp_path / ".codex"
    _write(
        root / "sessions" / "s.jsonl",
        [
            {
                "timestamp": "2026-09-21T12:00:00+00:00",
                "ordinal": ordinal,
                "payload": {
                    "thread_id": "t-1",
                    "model": model,
                    "last_token_usage": {
                        "input_tokens": 999,
                        "cached_input_tokens": 0,
                        "output_tokens": 1,
                    },
                },
            }
            for ordinal, model in ((1, "gpt-5.6-sol"), (2, "not-a-real-model"))
        ],
    )
    imported = discover_local_cli_history(root, provider="codex")

    history = _history(
        imported,
        datetime(2026, 9, 21, 18, tzinfo=timezone.utc),
        card=load_rate_card(),
    )
    coverage = history["pricing_coverage"]

    assert coverage["unpriced_tokens_today"] == 1_000
    assert coverage["priceable_tokens_today"] == 1_000
    assert coverage["priced_coverage_percent"] == 50.0
    assert coverage["unpriced_tokens_all_time"] == 1_000
    assert [entry["reason"] for entry in coverage["unpriced_models"]] == [
        "model_not_in_rate_card"
    ]
    # The priced half still carries its own cost; the unpriced half adds none.
    # 999 input at $4/M plus 1 output at $20/M, and nothing for the other 1,000.
    assert history["daily"][0]["api_rate_estimate_nanos"] == 4_016_000


def test_a_codex_cache_write_is_priced_at_the_cards_write_rate() -> None:
    """The card has carried a Codex cache-write rate all along; nothing read it."""

    card = load_rate_card()
    priced = card.price("gpt-5.6-sol", {"cache_create_5m_tokens": 1_000_000})

    assert priced.resolved_model == "gpt-5.6-sol"
    assert priced.amount_nanos == 5_000_000_000
