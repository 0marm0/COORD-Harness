"""Recovery contracts for delayed catalog publication and fetch diagnostics."""
from datetime import datetime, timezone
import json
from urllib.error import HTTPError

from coordharness.usage.pricing import _RATE_CARD_PATH
from coordharness.usage.rate_card_refresh import (
    RateCardRefresher, refresh_override, load_effective_rate_card,
)

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def catalog(extra=None):
    """Synthetic catalog keeps known prices exact; new prices are fixtures only."""
    models = json.loads(_RATE_CARD_PATH.read_bytes())["models"]
    if extra:
        models.update(extra)
    doc = {provider: {"models": {}} for provider in ("anthropic", "openai")}
    for model, entry in models.items():
        cost = {key: entry[key] for key in ("input", "output", "cache_read")}
        if "cache_write_5m" in entry:
            cost["cache_write"] = entry["cache_write_5m"]
        doc[entry["provider"]]["models"][model] = {
            "modalities": {"input": ["text"]}, "cost": cost,
        }
    return json.dumps(doc).encode()


def test_unpublished_model_is_retried_after_cooldown(tmp_path):
    clock = [0.0]
    bodies = [catalog(), catalog({
        "fixture-new-model": {"provider": "openai", "input": 1, "output": 2, "cache_read": 0},
    })]
    path = tmp_path / "rate_card.json"
    refresher = RateCardRefresher(
        override_path=path, fetcher=lambda: bodies.pop(0), now=lambda: NOW,
        monotonic=lambda: clock[0], retry_after_seconds=60,
    )
    assert refresher.maybe_refresh(["fixture-new-model"])
    assert refresher.wait(2)
    assert "fixture-new-model" not in load_effective_rate_card(path).rates
    clock[0] = 59
    assert not refresher.maybe_refresh(["fixture-new-model"])
    clock[0] = 60
    assert refresher.maybe_refresh(["fixture-new-model"])
    assert refresher.wait(2)
    assert "fixture-new-model" in load_effective_rate_card(path).rates
    assert not bodies


def test_failed_missing_model_fetch_recovers_with_fresh_override(tmp_path):
    path = tmp_path / "rate_card.json"
    assert refresh_override(path, fetcher=catalog, now=lambda: NOW).status == "updated"
    prior = path.read_bytes()
    clock = [0.0]
    calls = [0]

    def fetch():
        calls[0] += 1
        if calls[0] == 1:
            raise TimeoutError("test timeout")
        return catalog()

    refresher = RateCardRefresher(
        override_path=path, fetcher=fetch, now=lambda: NOW,
        monotonic=lambda: clock[0], retry_after_seconds=60,
    )
    assert refresher.maybe_refresh(["fixture-new-model"])
    assert refresher.wait(2)
    assert refresher.last_result.status == "failed"
    assert path.read_bytes() == prior
    clock[0] = 60
    assert refresher.maybe_refresh(["fixture-new-model"])
    assert refresher.wait(2)
    assert refresher.last_result.status == "updated"
    assert calls[0] == 2


def test_fetch_failure_reports_http_status_without_changing_last_good(tmp_path):
    path = tmp_path / "rate_card.json"
    assert refresh_override(path, fetcher=catalog, now=lambda: NOW).status == "updated"
    prior = path.read_bytes()

    def fail():
        raise HTTPError("https://models.dev/api.json", 503, "Unavailable", {}, None)

    result = refresh_override(path, fetcher=fail)
    assert result.summary()["http_status"] == 503
    assert result.summary()["error_type"] == "HTTPError"
    assert result.reason == "fetch_failed"
    assert path.read_bytes() == prior


def test_fresh_override_without_missing_rates_does_not_refetch(tmp_path):
    path = tmp_path / "rate_card.json"
    assert refresh_override(path, fetcher=catalog, now=lambda: NOW).status == "updated"
    refresher = RateCardRefresher(override_path=path, now=lambda: NOW)
    assert not refresher.maybe_refresh()
