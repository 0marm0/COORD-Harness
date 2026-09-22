"""Keep the rate card current without letting a bad fetch price anything.

The vendored ``rate_card.json`` is a snapshot. On 2026-09-22 a newly shipped
model (``claude-opus-5-5``) was missing from it, ~18% of that day's Claude
tokens were unpriced, and the gap was closed by hand only because someone
noticed. This module removes the noticing:

* A user-local OVERRIDE card is layered over the vendored one: an override
  entry wins, the vendored card fills every model the override lacks, and the
  vendored aliases and declared-unpriced labels always stand (the override
  never carries its own). ``load_effective_rate_card`` builds that layered card.
* The override is fetched from ``https://models.dev/api.json`` -- public, no
  credentials -- through the SAME transform the vendored card was built with:
  every Anthropic and OpenAI model with a text input and both an input and an
  output rate; a missing cache-read rate is 0; the published ``cache_write`` is
  the 5-minute write; Anthropic's 1-hour write is 2x the base input rate, which
  Anthropic documents; OpenAI publishes one write rate, used for both. That
  transform reproduces the vendored card's 61 models exactly from the
  2026-09-22 catalog.
* A fetch is refused -- and the last good override stays in force -- when it
  does not parse, fails ``parse_rate_card``, lost a provider, holds far fewer
  models than the vendored card, or moved any vendored model's rate by more
  than ``_MAX_RATE_FACTOR``. A refused fetch never touches the file.
* A refresh is started when an unpriced model shows up in the history, and
  otherwise at most once per ``REFRESH_INTERVAL_SECONDS``. It runs in a
  background thread with a bounded timeout, and every failure is logged and
  non-fatal. ``COORD_USAGE_RATE_CARD_REFRESH=0`` turns it off entirely, for an
  install that is offline or must not make network calls.

COMPARABILITY. The payload's ``pricing_key`` and ``rate_card_digest`` describe
the EFFECTIVE card. When the override changes nothing, the effective card IS
the vendored card: same key, same digest (the vendored file's sha256), so two
harnesses on the same vendored card compare equal regardless of their override
files. When it does change something, the key becomes
``<vendored key>.ovr-<12 hex>``, where the suffix hashes only the model rates that
differ from the vendored card, and the digest hashes the effective card with no
timestamps in it. Two harnesses whose overrides differ only in WHEN they were
fetched therefore still produce the same key and digest; two that genuinely
price differently cannot.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Final
from urllib.request import Request, urlopen

from .pricing import _RATE_CARD_PATH, RateCard, RateCardError, parse_rate_card

_logger = logging.getLogger("coordharness.usage.rate_card")

MODELS_DEV_URL: Final = "https://models.dev/api.json"
OPT_OUT_ENV: Final = "COORD_USAGE_RATE_CARD_REFRESH"
REFRESH_INTERVAL_SECONDS: Final = 24 * 3600
# After a failed or refused fetch, and between fetches triggered by the same
# unpriced model: a catalog that did not have the model an hour ago almost
# certainly still does not.
RETRY_AFTER_SECONDS: Final = 3600.0
FETCH_TIMEOUT_SECONDS: Final = 10.0
# The 2026-09-22 catalog is 4.9 MB. Far above that, well below "unbounded".
MAX_FETCH_BYTES: Final = 32 * 1024 * 1024
CATALOG_PROVIDERS: Final = ("anthropic", "openai")

# Refusal thresholds. A real catalog update adds models and occasionally
# re-prices one; it does not lose half the models or move a rate tenfold.
_MIN_MODEL_FRACTION: Final = 0.5
_MAX_RATE_FACTOR: Final = 10.0

_OFF_VALUES: Final = frozenset({"0", "false", "off", "no", "disabled"})


def default_override_path(home: Path | str | None = None) -> Path:
    """COORD's user-local override card: ``~/.coordharness/rate_card.json``."""

    base = Path(home) if home is not None else Path.home()
    return base / ".coordharness" / "rate_card.json"


def refresh_enabled(env: str = OPT_OUT_ENV) -> bool:
    """False when the operator opted out of rate-card network fetches."""

    return os.environ.get(env, "").strip().lower() not in _OFF_VALUES


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- the transform ----------------------------------------------------------


def transform_models_dev(document: object) -> dict[str, dict[str, Any]]:
    """The vendored card's ``models`` table, built from a models.dev catalog."""

    if not isinstance(document, Mapping):
        raise RateCardError("catalog root must be an object")
    models: dict[str, dict[str, Any]] = {}
    for provider in CATALOG_PROVIDERS:
        section = document.get(provider)
        catalog = section.get("models") if isinstance(section, Mapping) else None
        if not isinstance(catalog, Mapping):
            raise RateCardError(f"catalog has no {provider} models")
        for model_id, model in catalog.items():
            if not isinstance(model_id, str) or not isinstance(model, Mapping):
                continue
            cost = model.get("cost")
            modalities = model.get("modalities")
            inputs = modalities.get("input") if isinstance(modalities, Mapping) else None
            if not isinstance(cost, Mapping) or "input" not in cost or "output" not in cost:
                continue
            if not isinstance(inputs, list) or "text" not in inputs:
                continue
            entry: dict[str, Any] = {
                "provider": provider,
                "input": cost["input"],
                "output": cost["output"],
                "cache_read": cost.get("cache_read", 0),
            }
            if "cache_write" in cost:
                entry["cache_write_5m"] = cost["cache_write"]
                entry["cache_write_1h"] = (
                    _double(cost["input"]) if provider == "anthropic" else cost["cache_write"]
                )
            models[model_id] = entry
    return dict(sorted(models.items()))


def _double(value: object) -> object:
    """Twice a rate, exactly: via Decimal text, never binary float."""

    from decimal import Decimal, InvalidOperation

    try:
        doubled = Decimal(str(value)) * 2
    except (InvalidOperation, ValueError):
        return value
    return int(doubled) if doubled == doubled.to_integral_value() else float(doubled)


# -- layering ---------------------------------------------------------------


def _vendored_document(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise RateCardError("vendored rate card root must be an object")
    return raw, document


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _rate_tuple(card: RateCard, model: str) -> list[Any]:
    rate = card.rates[model]
    return [
        rate.provider,
        rate.input,
        rate.output,
        rate.cache_read,
        rate.cache_write_5m,
        rate.cache_write_1h,
    ]


def layer_rate_cards(vendored_raw: bytes, override_raw: bytes | None) -> RateCard:
    """The effective card: override entries over the vendored card.

    Only override MODELS are taken. Aliases, declared-unpriced labels,
    currency and version stay the vendored card's: those are decisions this
    project made (``codex-auto-review -> gpt-5.6-sol`` among them), not facts a
    catalog can revise.
    """

    vendored = parse_rate_card(vendored_raw)
    if override_raw is None:
        return vendored
    override = parse_rate_card(override_raw)
    if override.currency != vendored.currency:
        raise RateCardError("override currency differs from the vendored card")
    delta = sorted(
        model
        for model, rate in override.rates.items()
        if vendored.rates.get(model) != rate
    )
    if not delta:
        return vendored
    document = json.loads(vendored_raw)
    raw_override = json.loads(override_raw)
    models = dict(document["models"])
    for model in delta:
        models[model] = raw_override["models"][model]
    delta_digest = hashlib.sha256(
        _canonical({model: _rate_tuple(override, model) for model in delta})
    ).hexdigest()
    # No timestamps anywhere in what is hashed: the digest must depend on the
    # prices in force, not on when an override happened to be fetched.
    effective = {
        "rate_card_version": document["rate_card_version"],
        # "." rather than "+": the key must stay inside the dashboard proxy's
        # token alphabet, or a payload priced with an override is refused.
        "pricing_key": f"{vendored.pricing_key}.ovr-{delta_digest[:12]}",
        "currency": document["currency"],
        "unit": document.get("unit"),
        "source": {
            "kind": "vendored_with_models_dev_override",
            "vendored_pricing_key": vendored.pricing_key,
            "vendored_digest": vendored.digest,
            "override_models": len(delta),
        },
        "aliases": document.get("aliases") or {},
        "unpriced": document.get("unpriced") or {},
        "models": dict(sorted(models.items())),
    }
    return parse_rate_card(_canonical(effective))


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=8)
def _layered_cached(
    vendored_text: str,
    vendored_key: tuple[int, int],
    override_text: str,
    override_key: tuple[int, int] | None,
) -> RateCard:
    del vendored_key, override_key
    vendored_raw = Path(vendored_text).read_bytes()
    override_raw: bytes | None = None
    if override_text:
        try:
            override_raw = Path(override_text).read_bytes()
            return layer_rate_cards(vendored_raw, override_raw)
        except (OSError, RateCardError, ValueError, KeyError) as error:
            # A damaged override must never take pricing down with it: the
            # vendored card is always a valid answer on its own.
            _logger.warning("rate card override ignored: %s", error)
    return layer_rate_cards(vendored_raw, None)


def load_effective_rate_card(
    override_path: Path | str | None = None,
    *,
    vendored_path: Path | str | None = None,
) -> RateCard:
    """The card to price with: the override layered over the vendored card.

    Reparsed only when either file changes. With no override file, this is
    exactly ``pricing.load_rate_card()``.
    """

    vendored = Path(vendored_path) if vendored_path is not None else _RATE_CARD_PATH
    vendored_key = _stat_key(vendored)
    if vendored_key is None:
        raise RateCardError(f"rate card unreadable: {vendored.name}")
    override = Path(override_path) if override_path is not None else None
    override_key = _stat_key(override) if override is not None else None
    return _layered_cached(
        str(vendored),
        vendored_key,
        str(override) if override is not None and override_key is not None else "",
        override_key,
    )


# -- fetching ---------------------------------------------------------------


def fetch_models_dev(
    url: str = MODELS_DEV_URL,
    *,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    max_bytes: int = MAX_FETCH_BYTES,
) -> bytes:
    """GET the public catalog, bounded in time and size. No credentials."""

    request = Request(url, headers={"Accept": "application/json", "User-Agent": "coord-usage"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise RateCardError("catalog exceeds the size bound")
    return body


def _suspicion(models: Mapping[str, Mapping[str, Any]], vendored: RateCard) -> str | None:
    """Why a fetched table must not be trusted, or ``None``."""

    providers = {entry.get("provider") for entry in models.values()}
    for provider in CATALOG_PROVIDERS:
        if provider not in providers:
            return f"catalog_lost_provider_{provider}"
    if len(models) < max(10, int(len(vendored.rates) * _MIN_MODEL_FRACTION)):
        return "catalog_lost_most_models"
    candidate = parse_rate_card(
        _canonical(
            {
                "rate_card_version": vendored.rate_card_version,
                "pricing_key": "candidate",
                "currency": vendored.currency,
                "models": dict(models),
            }
        )
    )
    for model, rate in candidate.rates.items():
        known = vendored.rates.get(model)
        if known is None:
            continue
        for old, new in ((known.input, rate.input), (known.output, rate.output)):
            if old and new and max(old, new) / min(old, new) > _MAX_RATE_FACTOR:
                return "catalog_rate_jump"
            if bool(old) != bool(new):
                return "catalog_rate_jump"
    return None


@dataclass(frozen=True)
class RefreshResult:
    """What one refresh attempt did. ``status`` is updated|refused|failed|disabled."""

    status: str
    reason: str | None = None
    models: int = 0
    added_models: tuple[str, ...] = ()
    changed_models: tuple[str, ...] = ()
    pricing_key: str | None = None
    rate_card_digest: str | None = None
    fetched_at: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "models": self.models,
            "added_models": list(self.added_models),
            "changed_models": list(self.changed_models),
            "pricing_key": self.pricing_key,
            "rate_card_digest": self.rate_card_digest,
            "fetched_at": self.fetched_at,
        }


def override_fetched_at(path: Path) -> datetime | None:
    """When the override in force was fetched, from its own ``source``."""

    try:
        document = json.loads(path.read_bytes())
        stamp = document["source"]["fetched_at"]
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def refresh_override(
    override_path: Path | str,
    *,
    vendored_path: Path | str | None = None,
    fetcher: Callable[[], bytes] = fetch_models_dev,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    dry_run: bool = False,
) -> RefreshResult:
    """Fetch, validate and install one override card; the last good one survives.

    Synchronous and bounded by the fetcher's timeout. Never raises for a
    network or catalog fault -- those come back as ``failed`` / ``refused``.
    """

    target = Path(override_path)
    vendored_file = Path(vendored_path) if vendored_path is not None else _RATE_CARD_PATH
    try:
        vendored_raw, vendored_document = _vendored_document(vendored_file)
        vendored = parse_rate_card(vendored_raw)
    except (OSError, ValueError, RateCardError) as error:
        return RefreshResult(status="failed", reason=f"vendored_card_unusable: {error}")
    try:
        body = fetcher()
    except Exception as error:  # noqa: BLE001 - any fetch fault is non-fatal by contract
        _logger.warning("rate card fetch failed: %s", error)
        return RefreshResult(status="failed", reason="fetch_failed")
    try:
        models = transform_models_dev(json.loads(body))
        reason = _suspicion(models, vendored)
    except (ValueError, RateCardError, RecursionError) as error:
        _logger.warning("rate card fetch refused: %s", error)
        return RefreshResult(status="refused", reason="catalog_unparseable")
    if reason is not None:
        _logger.warning("rate card fetch refused: %s", reason)
        return RefreshResult(status="refused", reason=reason, models=len(models))
    fetched_at = _utc_iso(now().replace(microsecond=0))
    document = {
        "rate_card_version": vendored_document["rate_card_version"],
        "pricing_key": vendored.pricing_key,
        "currency": vendored.currency,
        "unit": vendored_document.get("unit", "usd_per_million_tokens"),
        "source": {"kind": "models.dev", "url": MODELS_DEV_URL, "fetched_at": fetched_at},
        "notes": [
            "User-local override fetched from models.dev and layered over the vendored "
            "card: entries here win, the vendored card fills the rest. Aliases and "
            "declared-unpriced labels are always the vendored card's.",
        ],
        "models": models,
    }
    payload = json.dumps(document, indent=2).encode("utf-8") + b"\n"
    try:
        effective = layer_rate_cards(vendored_raw, payload)
    except (RateCardError, ValueError, KeyError) as error:
        _logger.warning("rate card fetch refused: %s", error)
        return RefreshResult(status="refused", reason="override_fails_validation")
    added = tuple(sorted(set(models) - set(vendored.rates)))
    changed = tuple(
        sorted(
            model
            for model in set(models) & set(vendored.rates)
            if parse_rate_card(payload).rates[model] != vendored.rates[model]
        )
    )
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    _logger.info(
        "rate card override %s: %d models, %d added, %d re-priced",
        "checked" if dry_run else "installed",
        len(models),
        len(added),
        len(changed),
    )
    return RefreshResult(
        status="updated",
        models=len(models),
        added_models=added,
        changed_models=changed,
        pricing_key=effective.pricing_key,
        rate_card_digest=effective.digest,
        fetched_at=fetched_at,
    )


class RateCardRefresher:
    """Start at most one background override refresh, when one is due.

    Due means: a model in the history is unpriced for want of a rate and has
    not already prompted a fetch in this process, or the override in force is
    older than ``interval_seconds`` (or absent). Either way, never more often
    than ``retry_after_seconds`` after the previous attempt.
    """

    def __init__(
        self,
        *,
        override_path: Path | str,
        vendored_path: Path | str | None = None,
        opt_out_env: str = OPT_OUT_ENV,
        fetcher: Callable[[], bytes] = fetch_models_dev,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        interval_seconds: float = REFRESH_INTERVAL_SECONDS,
        retry_after_seconds: float = RETRY_AFTER_SECONDS,
        on_complete: Callable[[RefreshResult], None] | None = None,
    ) -> None:
        self._path = Path(override_path)
        self._vendored = vendored_path
        self._opt_out_env = opt_out_env
        self._fetcher = fetcher
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._interval = float(interval_seconds)
        self._retry_after = float(retry_after_seconds)
        self._on_complete = on_complete
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._not_before = float("-inf")
        self._prompted: set[str] = set()
        self.last_result: RefreshResult | None = None

    @property
    def override_path(self) -> Path:
        return self._path

    def enabled(self) -> bool:
        return refresh_enabled(self._opt_out_env)

    def _periodic_due(self) -> bool:
        fetched = override_fetched_at(self._path)
        if fetched is None:
            return True
        return (self._now().astimezone(timezone.utc) - fetched).total_seconds() > self._interval

    def maybe_refresh(self, unpriced_models: Iterable[str] = ()) -> bool:
        """Start a background refresh if one is due. Never waits for it."""

        if not self.enabled():
            return False
        fresh = {model for model in unpriced_models if isinstance(model, str) and model}
        with self._lock:
            if self._thread is not None or self._monotonic() < self._not_before:
                return False
            new_unpriced = fresh - self._prompted
        if not new_unpriced and not self._periodic_due():
            return False
        with self._lock:
            if self._thread is not None:
                return False
            self._prompted |= new_unpriced
            self._not_before = self._monotonic() + self._retry_after
            thread = threading.Thread(
                target=self._run, name="coord-usage-rate-card-refresh", daemon=True
            )
            self._thread = thread
        thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        result: RefreshResult | None = None
        try:
            result = refresh_override(
                self._path, vendored_path=self._vendored, fetcher=self._fetcher, now=self._now
            )
        except Exception:  # noqa: BLE001 - a refresh fault must never reach a reader
            _logger.warning("rate card refresh failed", exc_info=True)
            result = RefreshResult(status="failed", reason="refresh_raised")
        finally:
            with self._lock:
                self._thread = None
                self.last_result = result
        if result is not None and result.status == "updated" and self._on_complete is not None:
            try:
                self._on_complete(result)
            except Exception:  # noqa: BLE001 - a listener fault must not poison the refresher
                _logger.warning("rate card refresh listener failed", exc_info=True)


def unpriced_for_want_of_rate(unpriced: Mapping[str, str]) -> tuple[str, ...]:
    """The unpriced models a catalog fetch could fix.

    A label the card DECLARES unpriced (``<synthetic>``) or a malformed name is
    not a missing rate, and fetching for it would only burn the retry budget.
    """

    return tuple(
        sorted(model for model, reason in unpriced.items() if reason == "model_not_in_rate_card")
    )


__all__ = [
    "MODELS_DEV_URL",
    "OPT_OUT_ENV",
    "REFRESH_INTERVAL_SECONDS",
    "RateCardRefresher",
    "RefreshResult",
    "default_override_path",
    "fetch_models_dev",
    "layer_rate_cards",
    "load_effective_rate_card",
    "override_fetched_at",
    "refresh_enabled",
    "refresh_override",
    "transform_models_dev",
    "unpriced_for_want_of_rate",
]
