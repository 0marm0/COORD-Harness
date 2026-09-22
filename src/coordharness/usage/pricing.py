"""Self-contained list-price computation for locally observed token counts.

This module is the only place COORD turns tokens into money. It depends on
nothing but a vendored rate card, so a stale or absent third-party cost cache
can no longer zero out the dashboard. Rates are API list prices: a subscription
plan is not billed this way, and every figure produced here is an
API-equivalent estimate rather than provider-billed spend.

An unknown model yields ``None`` rather than zero. A missing price and a real
zero are different facts, and collapsing them is what made a broken feed look
like a free day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final

_RATE_CARD_PATH: Final = Path(__file__).with_name("rate_card.json")

# A daily aggregate can legitimately be large; a rate cannot. These bounds keep
# a corrupt or hostile card from producing absurd figures.
_MAX_USD_PER_MILLION: Final = Decimal("10000")
_NANOS_PER_USD: Final = 1_000_000_000
_TOKENS_PER_UNIT: Final = 1_000_000
_MODEL_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:<>/-]{1,120}$")
_DATE_SUFFIX: Final = re.compile(r"-\d{8}$")

_COMPONENTS: Final = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read"),
    ("cache_create_5m_tokens", "cache_write_5m"),
    ("cache_create_1h_tokens", "cache_write_1h"),
    # An undifferentiated cache write is billed at the 5m rate, which is the
    # rate Anthropic publishes as `cache_write`.
    ("cache_create_other_tokens", "cache_write_5m"),
)


class RateCardError(ValueError):
    """The vendored rate card is unusable."""


@dataclass(frozen=True)
class ModelRate:
    """Nano-USD per million tokens for one model, by token component."""

    model: str
    provider: str
    input: int
    output: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int

    def nanos_per_million(self, component: str) -> int:
        return int(getattr(self, component))


@dataclass(frozen=True)
class PricedTokens:
    """The outcome of pricing one (day, model) aggregate."""

    model: str
    resolved_model: str | None
    amount_nanos: int | None
    priced_tokens: int
    unpriced_tokens: int
    reason: str | None = None

    @property
    def priced(self) -> bool:
        return self.amount_nanos is not None


@dataclass(frozen=True)
class RateCard:
    """An immutable, self-contained list-price table."""

    pricing_key: str
    currency: str
    rate_card_version: int
    source: Mapping[str, Any]
    digest: str
    rates: Mapping[str, ModelRate]
    aliases: Mapping[str, str]
    unpriced: Mapping[str, str]

    def resolve(self, model: object) -> tuple[str | None, str | None]:
        """Return ``(resolved_model, reason_when_unresolved)``.

        Resolution order is exact match, declared alias, then the same lookups
        with a trailing ``-YYYYMMDD`` build suffix removed. An explicitly
        declared unpriced label resolves to nothing and carries its reason.
        """

        if not isinstance(model, str):
            return None, "model_not_a_string"
        name = model.strip()
        if not name or not _MODEL_PATTERN.match(name):
            return None, "model_unrecognized"
        for candidate in self._candidates(name):
            declared = self.unpriced.get(candidate)
            if declared is not None:
                return None, declared
            if candidate in self.rates:
                return candidate, None
            alias = self.aliases.get(candidate)
            if alias is not None and alias in self.rates:
                return alias, None
        return None, "model_not_in_rate_card"

    def _candidates(self, name: str) -> tuple[str, ...]:
        lowered = name.casefold()
        ordered = [name, lowered]
        for value in (name, lowered):
            stripped = _DATE_SUFFIX.sub("", value)
            if stripped != value:
                ordered.append(stripped)
        seen: dict[str, None] = {}
        for value in ordered:
            seen.setdefault(value, None)
        return tuple(seen)

    def price(self, model: object, tokens: Mapping[str, object]) -> PricedTokens:
        """Price one aggregate exactly, in integer nano-USD.

        Products are summed before the single division, so no component is
        independently rounded.
        """

        counts = {name: _count(tokens.get(name)) for name, _ in _COMPONENTS}
        total_tokens = sum(counts.values())
        resolved, reason = self.resolve(model)
        label = model if isinstance(model, str) else ""
        if resolved is None:
            return PricedTokens(
                model=label,
                resolved_model=None,
                amount_nanos=None,
                priced_tokens=0,
                unpriced_tokens=total_tokens,
                reason=reason,
            )
        rate = self.rates[resolved]
        scaled = sum(
            counts[name] * rate.nanos_per_million(component) for name, component in _COMPONENTS
        )
        return PricedTokens(
            model=label,
            resolved_model=resolved,
            amount_nanos=scaled // _TOKENS_PER_UNIT,
            priced_tokens=total_tokens,
            unpriced_tokens=0,
        )

    def provenance(self) -> dict[str, Any]:
        """Describe this card for the payload, without leaking local paths."""

        return {
            "pricing_key": self.pricing_key,
            "rate_card_version": self.rate_card_version,
            "rate_card_digest": self.digest,
            "currency": self.currency,
            "source": dict(self.source),
            "canonical": False,
            "semantics": "self_computed_api_list_price_estimate",
            "warning": (
                "Tokens priced at API list rates by this harness; "
                "a subscription plan is not billed this way."
            ),
        }


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _rate_nanos(value: object, *, model: str, component: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RateCardError(f"{model}.{component}: rate must be a number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise RateCardError(f"{model}.{component}: rate is not a decimal") from error
    if not amount.is_finite() or amount < 0 or amount > _MAX_USD_PER_MILLION:
        raise RateCardError(f"{model}.{component}: rate out of range")
    scaled = amount * _NANOS_PER_USD
    if scaled != scaled.to_integral_value():
        raise RateCardError(f"{model}.{component}: rate finer than one nano-USD")
    return int(scaled)


def _model_rate(model: str, raw: object) -> ModelRate:
    if not isinstance(raw, Mapping):
        raise RateCardError(f"{model}: rate entry must be an object")
    provider = raw.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise RateCardError(f"{model}: missing provider")
    for required in ("input", "output"):
        if required not in raw:
            raise RateCardError(f"{model}: missing {required} rate")
    write_5m = raw.get("cache_write_5m", 0)
    return ModelRate(
        model=model,
        provider=provider.strip(),
        input=_rate_nanos(raw["input"], model=model, component="input"),
        output=_rate_nanos(raw["output"], model=model, component="output"),
        cache_read=_rate_nanos(raw.get("cache_read", 0), model=model, component="cache_read"),
        cache_write_5m=_rate_nanos(write_5m, model=model, component="cache_write_5m"),
        cache_write_1h=_rate_nanos(
            raw.get("cache_write_1h", write_5m), model=model, component="cache_write_1h"
        ),
    )


def _text_map(raw: object, *, field: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise RateCardError(f"{field} must be an object")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RateCardError(f"{field}: entries must be strings")
        result[key] = value
    return result


def parse_rate_card(payload: bytes | str) -> RateCard:
    """Validate a rate-card document and return an immutable card."""

    raw_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
    try:
        document = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RateCardError("rate card is not valid JSON") from error
    if not isinstance(document, Mapping):
        raise RateCardError("rate card root must be an object")
    version = document.get("rate_card_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RateCardError("unsupported rate_card_version")
    pricing_key = document.get("pricing_key")
    if not isinstance(pricing_key, str) or not pricing_key.strip():
        raise RateCardError("missing pricing_key")
    currency = document.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise RateCardError("missing currency")
    raw_models = document.get("models")
    if not isinstance(raw_models, Mapping) or not raw_models:
        raise RateCardError("rate card declares no models")
    rates = {
        model: _model_rate(model, entry)
        for model, entry in raw_models.items()
        if isinstance(model, str)
    }
    if len(rates) != len(raw_models):
        raise RateCardError("model keys must be strings")
    source = document.get("source")
    return RateCard(
        pricing_key=pricing_key.strip(),
        currency=currency.upper(),
        rate_card_version=version,
        source=dict(source) if isinstance(source, Mapping) else {},
        digest=hashlib.sha256(raw_bytes).hexdigest(),
        rates=rates,
        aliases=_text_map(document.get("aliases"), field="aliases"),
        unpriced=_text_map(document.get("unpriced"), field="unpriced"),
    )


@lru_cache(maxsize=4)
def _load_cached(path_text: str, mtime_ns: int, size: int) -> RateCard:
    del mtime_ns, size
    return parse_rate_card(Path(path_text).read_bytes())


def load_rate_card(path: Path | str | None = None) -> RateCard:
    """Load the vendored rate card, reparsing only when the file changes."""

    target = Path(path) if path is not None else _RATE_CARD_PATH
    try:
        stat = target.stat()
    except OSError as error:
        raise RateCardError(f"rate card unreadable: {target.name}") from error
    return _load_cached(str(target), stat.st_mtime_ns, stat.st_size)
