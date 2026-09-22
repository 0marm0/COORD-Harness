"""The user-local override card: layering, refusal, identity, and the refresher.

A newly shipped model must not depend on someone noticing it is unpriced, and a
bad catalog fetch must never price anything. Both halves are tested here with a
synthetic catalog; nothing touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

import pytest

from coordharness.usage.pricing import _RATE_CARD_PATH, load_rate_card
from coordharness.usage.rate_card_refresh import (
    RateCardRefresher,
    layer_rate_cards,
    load_effective_rate_card,
    refresh_override,
    transform_models_dev,
    unpriced_for_want_of_rate,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 22, 20, tzinfo=timezone.utc)


def _catalog_from_vendored(**extra: dict) -> dict:
    """A models.dev-shaped catalog that transforms back into the vendored card."""

    card = json.loads(_RATE_CARD_PATH.read_bytes())
    catalog: dict = {"anthropic": {"models": {}}, "openai": {"models": {}}, "other": {}}
    for model, entry in card["models"].items():
        cost = {"input": entry["input"], "output": entry["output"],
                "cache_read": entry["cache_read"]}
        if "cache_write_5m" in entry:
            cost["cache_write"] = entry["cache_write_5m"]
        catalog[entry["provider"]]["models"][model] = {
            "id": model,
            "modalities": {"input": ["text"], "output": ["text"]},
            "cost": cost,
        }
    for model, entry in extra.items():
        catalog["anthropic"]["models"][model] = entry
    return catalog


def _opus_next() -> dict:
    return {
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
    }


def test_the_transform_reproduces_the_vendored_models_exactly() -> None:
    vendored = json.loads(_RATE_CARD_PATH.read_bytes())["models"]
    assert transform_models_dev(_catalog_from_vendored()) == vendored


def test_the_transform_skips_non_text_and_unpriced_entries() -> None:
    catalog = _catalog_from_vendored(
        **{
            "image-only": {"modalities": {"input": ["image"]}, "cost": {"input": 1, "output": 1}},
            "no-output-rate": {"modalities": {"input": ["text"]}, "cost": {"input": 1}},
        }
    )
    models = transform_models_dev(catalog)
    assert "image-only" not in models and "no-output-rate" not in models


def test_an_identical_override_leaves_the_vendored_identity(tmp_path: Path) -> None:
    override = tmp_path / "rate_card.json"
    result = refresh_override(
        override, fetcher=lambda: json.dumps(_catalog_from_vendored()).encode(), now=lambda: _NOW
    )
    vendored = load_rate_card()
    effective = load_effective_rate_card(override)

    assert result.status == "updated"
    assert (effective.pricing_key, effective.digest) == (vendored.pricing_key, vendored.digest)


def test_a_new_model_is_priced_and_changes_the_identity_deterministically(
    tmp_path: Path,
) -> None:
    body = json.dumps(_catalog_from_vendored(**{"claude-opus-9": _opus_next()})).encode()
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    one = refresh_override(first, fetcher=lambda: body, now=lambda: _NOW)
    two = refresh_override(second, fetcher=lambda: body, now=lambda: _NOW + timedelta(days=3))
    card = load_effective_rate_card(first)

    assert one.added_models == ("claude-opus-9",)
    assert card.resolve("claude-opus-9") == ("claude-opus-9", None)
    # 1h write derived as 2x input, the documented Anthropic rule.
    assert card.rates["claude-opus-9"].cache_write_1h == 6 * 1_000_000_000
    assert card.pricing_key.startswith(load_rate_card().pricing_key + ".ovr-")
    # Fetched at different times, same prices: same identity.
    assert (card.pricing_key, card.digest) == (
        load_effective_rate_card(second).pricing_key,
        load_effective_rate_card(second).digest,
    )
    assert two.pricing_key == one.pricing_key


def test_vendored_aliases_and_unpriced_labels_always_stand(tmp_path: Path) -> None:
    override = tmp_path / "rate_card.json"
    body = json.dumps(_catalog_from_vendored(**{"claude-opus-9": _opus_next()})).encode()
    refresh_override(override, fetcher=lambda: body, now=lambda: _NOW)
    card = load_effective_rate_card(override)

    assert card.resolve("codex-auto-review") == ("gpt-5.6-sol", None)
    assert card.resolve("<synthetic>")[0] is None


def test_an_override_entry_wins_over_the_vendored_one() -> None:
    vendored_raw = _RATE_CARD_PATH.read_bytes()
    document = json.loads(vendored_raw)
    document["models"] = {"gpt-5.6-sol": {**document["models"]["gpt-5.6-sol"], "input": 5}}
    card = layer_rate_cards(vendored_raw, json.dumps(document).encode())
    assert card.rates["gpt-5.6-sol"].input == 5 * 1_000_000_000
    assert len(card.rates) == len(load_rate_card().rates)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c.pop("openai"), "catalog_unparseable"),
        (
            lambda c: c["anthropic"]["models"]["claude-opus-5"]["cost"].update(input=500),
            "catalog_rate_jump",
        ),
        (
            lambda c: c["openai"].update(
                models=dict(list(c["openai"]["models"].items())[:2])
            ) or c["anthropic"].update(models=dict(list(c["anthropic"]["models"].items())[:2])),
            "catalog_lost_most_models",
        ),
    ],
)
def test_a_suspicious_fetch_is_refused_and_the_last_good_card_stays(
    tmp_path: Path, mutate, reason: str
) -> None:
    override = tmp_path / "rate_card.json"
    good = json.dumps(_catalog_from_vendored(**{"claude-opus-9": _opus_next()})).encode()
    refresh_override(override, fetcher=lambda: good, now=lambda: _NOW)
    before = override.read_bytes()
    catalog = _catalog_from_vendored()
    mutate(catalog)

    result = refresh_override(override, fetcher=lambda: json.dumps(catalog).encode())

    assert (result.status, result.reason) == ("refused", reason)
    assert override.read_bytes() == before


def test_a_failed_fetch_is_non_fatal(tmp_path: Path) -> None:
    def boom() -> bytes:
        raise TimeoutError("offline")

    result = refresh_override(tmp_path / "rate_card.json", fetcher=boom)
    assert (result.status, result.reason) == ("failed", "fetch_failed")
    assert not (tmp_path / "rate_card.json").exists()


def test_a_damaged_override_file_falls_back_to_the_vendored_card(tmp_path: Path) -> None:
    override = tmp_path / "rate_card.json"
    override.write_text("{not json")
    assert load_effective_rate_card(override).digest == load_rate_card().digest


# -- the refresher ----------------------------------------------------------


class _Fetcher:
    def __init__(self) -> None:
        self.calls = 0
        self.body = json.dumps(_catalog_from_vendored(**{"claude-opus-9": _opus_next()})).encode()
        self.done = threading.Event()

    def __call__(self) -> bytes:
        self.calls += 1
        self.done.set()
        return self.body


def _refresher(tmp_path: Path, fetcher: _Fetcher, clock: list[float]) -> RateCardRefresher:
    return RateCardRefresher(
        override_path=tmp_path / "rate_card.json",
        fetcher=fetcher,
        now=lambda: _NOW,
        monotonic=lambda: clock[0],
    )


def test_an_unpriced_model_triggers_one_refresh_then_backs_off(tmp_path: Path) -> None:
    fetcher, clock = _Fetcher(), [0.0]
    refresher = _refresher(tmp_path, fetcher, clock)

    assert refresher.maybe_refresh(["claude-opus-9"]) is True
    assert refresher.wait(5)
    assert refresher.last_result is not None and refresher.last_result.status == "updated"
    # Just refreshed, same model: nothing is due.
    assert refresher.maybe_refresh(["claude-opus-9"]) is False
    # A different unknown model, but inside the retry window: still nothing.
    assert refresher.maybe_refresh(["claude-opus-10"]) is False
    clock[0] += 3601
    assert refresher.maybe_refresh(["claude-opus-10"]) is True
    assert refresher.wait(5)
    assert fetcher.calls == 2


def test_without_an_override_the_periodic_refresh_is_due(tmp_path: Path) -> None:
    fetcher, clock = _Fetcher(), [0.0]
    refresher = _refresher(tmp_path, fetcher, clock)
    assert refresher.maybe_refresh() is True
    assert refresher.wait(5)
    clock[0] += 3601
    # The override was fetched "now"; the 24h cadence is not due yet.
    assert refresher.maybe_refresh() is False


def test_the_opt_out_environment_prevents_every_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_USAGE_RATE_CARD_REFRESH", "0")
    fetcher, clock = _Fetcher(), [0.0]
    assert _refresher(tmp_path, fetcher, clock).maybe_refresh(["claude-opus-9"]) is False
    assert fetcher.calls == 0


def test_only_models_missing_a_rate_prompt_a_fetch() -> None:
    assert unpriced_for_want_of_rate(
        {
            "claude-opus-9": "model_not_in_rate_card",
            "<synthetic>": "Claude Code synthetic message; no model call",
            "bad name!": "model_unrecognized",
        }
    ) == ("claude-opus-9",)


def test_the_payload_reports_the_effective_card(tmp_path: Path) -> None:
    from coordharness.usage.local_service import ProviderProbe, _UncachedLocalUsageService
    from coordharness.usage.provider_series import SeriesObservation

    home = tmp_path / "home"
    transcript = home / ".claude" / "projects" / "p" / "c.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "timestamp": "2026-09-22T12:00:00Z",
        "message": {"id": "m", "model": "claude-opus-9",
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    }) + "\n")
    refresh_override(
        home / ".coordharness" / "rate_card.json",
        fetcher=lambda: json.dumps(_catalog_from_vendored(**{"claude-opus-9": _opus_next()})).encode(),
        now=lambda: _NOW,
    )

    def probe() -> ProviderProbe:
        return ProviderProbe(account={"status": "active", "plan": "max", "authenticated": True})

    document = _UncachedLocalUsageService(
        home=home,
        now=lambda: _NOW,
        claude_probe=probe,
        codex_probe=probe,
        cost_cache_root=home / "none",
        codex_series=lambda: SeriesObservation(series=None, reason="provider_series_disabled"),
    ).dashboard()
    cost = document["providers"]["claude"]["costs"]["api_rate_estimate"]

    assert cost["pricing_key"].startswith("coord-list-price-v4.ovr-")
    assert cost["rate_card_override_models"] == 1
    assert cost["unpriced_tokens"] == 0
    assert cost["measured_amount_nanos"] == 3_000_000_000
