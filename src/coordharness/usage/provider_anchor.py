"""Anchor locally measured Codex cost to the provider's own daily token volume.

The official Codex CLI app-server answers ``account/usage/read`` with the
account's lifetime token count and one bucket per day. That count covers every
surface the account used, while this machine's transcripts cover only the
sessions that ran here and whose rollout files still exist. Measured on
2026-09-22 over 2026-07-17..2026-09-21 (UTC): the provider reported 56.27B
tokens against 39.30B in local transcripts, with the provider at or above local
on 62 of 63 days.

This module reconciles the two WITHOUT letting one pass for the other:

* ``measured`` is always the local transcripts' own priced cost, untouched.
* ``estimated_off_transcript`` is the provider volume local transcripts do not
  account for, priced at that same day's local model-and-component mix. It is
  an estimate of usage nobody on this machine observed, and every row and total
  says so in its ``basis``.

A flat blended rate would be wrong: cached reads are most of Codex volume and
bill at a tenth of fresh input, so the day's own mix is the least-assumption
price for tokens of unknown composition. Where a day has provider volume but no
priced local tokens, the mix of the nearest days that do is used, and the row
names the days it borrowed from.

DAY BASIS. Provider buckets are UTC calendar days: bucketing the same local
records by UTC puts the provider at or above local on 62 of 63 days and leaves
every day the provider omits empty locally too, whereas New York, Los Angeles
or Berlin days produce 11-19 days where local exceeds provider. The caller MUST
therefore pass local rows bucketed by UTC day. Rows bucketed by a local zone
misalign a slice of every day and inflate the estimate on each boundary.
``plan_codex_anchor`` does this from the store's UTC slots, then spreads each
UTC day's estimate over the reader's LOCAL days in proportion to where that UTC
day's measured volume fell (``_split_utc_day``).

PAYLOAD. ``apply_anchor_plan`` is the one place the result enters a dashboard
payload, shared verbatim by both harnesses so the field names cannot drift. The
headline ``costs.api_rate_estimate.amount_nanos`` is measured + estimated +
legacy, and ``measured_amount_nanos`` / ``estimated_amount_nanos`` /
``legacy_amount_nanos`` always travel beside it, so no reader can present the
estimate as measured. Claude has no provider-side daily token API, so a Claude
plan is ``not_applicable`` and its cost stays measured-only.

Pure: no I/O, no clock, integer nano-USD throughout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Final

from .local_history import SlotUsage, day_slots, slot_day, utc_slot_day
from .pricing import RateCard

# Basis labels. Only the first two leave the day's cost fully measured.
BASIS_ALIGNED: Final = "measured_matches_provider"
BASIS_LOCAL_EXCEEDS: Final = "measured_exceeds_provider"
BASIS_LOCAL_MIX: Final = "estimated_at_same_day_local_mix"
BASIS_NEAREST_MIX: Final = "estimated_at_nearest_window_local_mix"
BASIS_NO_PROVIDER: Final = "measured_only_no_provider_bucket"
BASIS_UNPRICEABLE: Final = "off_transcript_volume_unpriceable"

DEFAULT_MAX_WINDOW_DAYS: Final = 7

# Whether a provider's cost carries a provider-anchored estimate.
ESTIMATE_APPLIED: Final = "applied"
ESTIMATE_UNAVAILABLE: Final = "unavailable"
ESTIMATE_NOT_APPLICABLE: Final = "not_applicable"

# Machine-readable reasons an estimate is absent. Every one leaves the cost
# measured-only; none is ever silent.
REASON_NO_PROVIDER_TOKEN_API: Final = "provider_reports_no_daily_tokens"
REASON_SERIES_UNAVAILABLE: Final = "provider_series_unavailable"
REASON_SERIES_INVALID: Final = "provider_series_invalid"
REASON_SERIES_EXPIRED: Final = "provider_series_expired"
REASON_SERIES_DISABLED: Final = "provider_series_disabled"
REASON_SERIES_PENDING: Final = "provider_series_not_yet_fetched"
REASON_NO_MEASURED_USAGE: Final = "no_measured_local_usage"

# How the estimate was made, carried on the cost and on the anchor block.
# Kept under the dashboard proxy's 80-character token bound, or it is refused.
ESTIMATE_BASIS: Final = "provider_utc_day_gap_priced_at_same_utc_day_local_mix"
# How a UTC day's estimate is placed on local days.
ALLOCATION_BASIS: Final = "utc_day_estimate_split_by_that_days_measured_local_slot_volume"

# A provider series older than this is not applied. Past days never change, so
# an older series is still right about them, but beyond a week the recent days
# it lacks dominate what a reader looks at.
MAX_SERIES_AGE_SECONDS: Final = 7 * 24 * 3600

_NANOS_PER_USD: Final = 1_000_000_000


class ProviderSeriesError(ValueError):
    """An ``account/usage/read`` result is not a usable daily series."""


@dataclass(frozen=True)
class ProviderSeries:
    """The provider's own account-level token counts, one bucket per UTC day."""

    lifetime_tokens: int | None
    daily: Mapping[str, int]

    @property
    def bucket_total(self) -> int:
        return sum(self.daily.values())

    @property
    def reconciles_with_lifetime(self) -> bool | None:
        """True when the buckets sum exactly to the reported lifetime."""

        if self.lifetime_tokens is None:
            return None
        return self.bucket_total == self.lifetime_tokens


@dataclass(frozen=True)
class LocalDayCost:
    """One UTC day of locally MEASURED usage, already priced.

    ``tokens`` is every token observed (input inclusive of cached input, plus
    output), matching what the provider counts. ``priced_tokens`` is the part
    the rate card could price and ``cost_nanos`` its cost; the mix rate is
    ``cost_nanos / priced_tokens`` so unpriced tokens never dilute it.
    """

    usage_date: str
    tokens: int
    priced_tokens: int
    cost_nanos: int


@dataclass(frozen=True)
class AnchoredDay:
    usage_date: str
    provider_tokens: int | None
    local_tokens: int
    measured_cost_nanos: int
    off_transcript_tokens: int
    estimated_off_transcript_cost_nanos: int | None
    basis: str
    mix_source_dates: tuple[str, ...] = ()

    @property
    def overlap_excess_tokens(self) -> int:
        """Local tokens above the provider's count; never subtracted from cost."""

        if self.provider_tokens is None:
            return 0
        return max(0, self.local_tokens - self.provider_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.usage_date,
            "provider_tokens": self.provider_tokens,
            "local_tokens": self.local_tokens,
            "off_transcript_tokens": self.off_transcript_tokens,
            "overlap_excess_tokens": self.overlap_excess_tokens,
            "measured_cost_usd": _usd(self.measured_cost_nanos),
            "estimated_off_transcript_cost_usd": (
                None
                if self.estimated_off_transcript_cost_nanos is None
                else _usd(self.estimated_off_transcript_cost_nanos)
            ),
            "basis": self.basis,
            "mix_source_dates": list(self.mix_source_dates),
        }


@dataclass(frozen=True)
class AnchorResult:
    days: tuple[AnchoredDay, ...]

    @property
    def measured_cost_nanos(self) -> int:
        return sum(day.measured_cost_nanos for day in self.days)

    @property
    def estimated_off_transcript_cost_nanos(self) -> int:
        return sum(day.estimated_off_transcript_cost_nanos or 0 for day in self.days)

    @property
    def anchored_cost_nanos(self) -> int:
        return self.measured_cost_nanos + self.estimated_off_transcript_cost_nanos

    @property
    def unpriceable_off_transcript_tokens(self) -> int:
        return sum(
            day.off_transcript_tokens
            for day in self.days
            if day.estimated_off_transcript_cost_nanos is None
        )

    def totals(self) -> dict[str, Any]:
        provider = [day.provider_tokens for day in self.days if day.provider_tokens is not None]
        return {
            "semantics": "provider_volume_anchored_codex_cost",
            "measured_semantics": "priced_from_local_transcripts",
            "estimated_semantics": (
                "provider_reported_volume_absent_from_local_transcripts_"
                "priced_at_local_mix_estimate"
            ),
            "provider_tokens": sum(provider),
            "local_tokens": sum(day.local_tokens for day in self.days),
            "off_transcript_tokens": sum(day.off_transcript_tokens for day in self.days),
            "overlap_excess_tokens": sum(day.overlap_excess_tokens for day in self.days),
            "unpriceable_off_transcript_tokens": self.unpriceable_off_transcript_tokens,
            "days": len(self.days),
            "days_provider_above_local": sum(
                1 for day in self.days if day.off_transcript_tokens > 0
            ),
            "days_local_above_provider": sum(
                1 for day in self.days if day.overlap_excess_tokens > 0
            ),
            "measured_cost_usd": _usd(self.measured_cost_nanos),
            "estimated_off_transcript_cost_usd": _usd(self.estimated_off_transcript_cost_nanos),
            "anchored_cost_usd": _usd(self.anchored_cost_nanos),
        }


def _usd(nanos: int) -> str:
    """Exact decimal text; a float would round the nano-USD it came from."""

    sign = "-" if nanos < 0 else ""
    whole, fraction = divmod(abs(nanos), _NANOS_PER_USD)
    return f"{sign}{whole}.{fraction:09d}"


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _iso_day(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def parse_account_usage(result: Mapping[str, Any], *, max_buckets: int = 4096) -> ProviderSeries:
    """Validate an ``account/usage/read`` result into a daily series.

    The result carries ``summary.lifetimeTokens`` and ``dailyUsageBuckets``,
    each ``{"startDate": "YYYY-MM-DD", "tokens": int}``. A bucket that repeats a
    day is refused rather than summed or overwritten: either would silently
    change a figure the provider stated once.
    """

    if not isinstance(result, Mapping):
        raise ProviderSeriesError("usage result must be an object")
    summary = result.get("summary")
    lifetime = (
        _count(summary.get("lifetimeTokens")) if isinstance(summary, Mapping) else None
    )
    buckets = result.get("dailyUsageBuckets")
    if not isinstance(buckets, list):
        raise ProviderSeriesError("usage result has no dailyUsageBuckets list")
    if len(buckets) > max_buckets:
        raise ProviderSeriesError("too many daily buckets")
    daily: dict[str, int] = {}
    for bucket in buckets:
        if not isinstance(bucket, Mapping):
            raise ProviderSeriesError("bucket must be an object")
        day, tokens = _iso_day(bucket.get("startDate")), _count(bucket.get("tokens"))
        if day is None or tokens is None:
            raise ProviderSeriesError("bucket needs an ISO startDate and non-negative tokens")
        if day in daily:
            raise ProviderSeriesError(f"duplicate bucket for {day}")
        daily[day] = tokens
    return ProviderSeries(lifetime_tokens=lifetime, daily=dict(sorted(daily.items())))


def _nearest_mix(
    target: str,
    priced: Mapping[str, LocalDayCost],
    max_window_days: int,
) -> tuple[int, int, tuple[str, ...]] | None:
    """The token-weighted mix of the closest days with priced local tokens.

    The window grows one day at a time, symmetrically, and stops at the first
    radius that reaches any priced day, so a quiet stretch borrows from its
    immediate neighbours rather than from a distant, differently shaped week.
    """

    origin = date.fromisoformat(target)
    for radius in range(1, max_window_days + 1):
        chosen = [
            row
            for row in priced.values()
            if 0 < abs((date.fromisoformat(row.usage_date) - origin).days) <= radius
        ]
        if chosen:
            cost = sum(row.cost_nanos for row in chosen)
            tokens = sum(row.priced_tokens for row in chosen)
            return cost, tokens, tuple(sorted(row.usage_date for row in chosen))
    return None


def anchor_to_provider(
    local: Iterable[LocalDayCost],
    provider_daily: Mapping[str, int],
    *,
    max_window_days: int = DEFAULT_MAX_WINDOW_DAYS,
) -> AnchorResult:
    """Reconcile measured local cost with the provider's daily token volume.

    Per UTC day:

    * provider == local: fully measured.
    * provider > local: measured cost stands; the difference is priced at the
      day's own local mix, or at the nearest priced window when the day has no
      priced local tokens, or left unpriced (``None``) when nothing lies within
      ``max_window_days``.
    * provider < local: measured cost stands and nothing is estimated. The
      excess is reported, never deducted -- a transcript record is direct
      evidence, a missing provider token is not evidence against it.
    * no provider bucket: measured only.
    """

    if max_window_days < 0:
        raise ValueError("max_window_days must be non-negative")
    rows: dict[str, LocalDayCost] = {}
    for row in local:
        if _iso_day(row.usage_date) != row.usage_date:
            raise ValueError(f"local row has no ISO day: {row.usage_date!r}")
        if row.usage_date in rows:
            raise ValueError(f"duplicate local row for {row.usage_date}")
        if min(row.tokens, row.priced_tokens, row.cost_nanos) < 0 or row.priced_tokens > row.tokens:
            raise ValueError(f"inconsistent local row for {row.usage_date}")
        rows[row.usage_date] = row
    provider: dict[str, int] = {}
    for day, tokens in provider_daily.items():
        if _iso_day(day) != day or _count(tokens) is None:
            raise ValueError(f"invalid provider bucket {day!r}")
        provider[day] = tokens
    priced = {day: row for day, row in rows.items() if row.priced_tokens > 0}
    days: list[AnchoredDay] = []
    for day in sorted(set(rows) | set(provider)):
        row = rows.get(day) or LocalDayCost(day, 0, 0, 0)
        reported = provider.get(day)
        if reported is None:
            days.append(_day(row, None, 0, 0, BASIS_NO_PROVIDER))
            continue
        gap = reported - row.tokens
        if gap == 0:
            days.append(_day(row, reported, 0, 0, BASIS_ALIGNED))
        elif gap < 0:
            days.append(_day(row, reported, 0, 0, BASIS_LOCAL_EXCEEDS))
        elif row.priced_tokens > 0:
            estimate = gap * row.cost_nanos // row.priced_tokens
            days.append(_day(row, reported, gap, estimate, BASIS_LOCAL_MIX))
        else:
            window = _nearest_mix(day, priced, max_window_days)
            if window is None:
                days.append(_day(row, reported, gap, None, BASIS_UNPRICEABLE))
            else:
                cost, tokens, sources = window
                days.append(
                    _day(row, reported, gap, gap * cost // tokens, BASIS_NEAREST_MIX, sources)
                )
    return AnchorResult(days=tuple(days))


def _day(
    row: LocalDayCost,
    reported: int | None,
    gap: int,
    estimate: int | None,
    basis: str,
    sources: Sequence[str] = (),
) -> AnchoredDay:
    return AnchoredDay(
        usage_date=row.usage_date,
        provider_tokens=reported,
        local_tokens=row.tokens,
        measured_cost_nanos=row.cost_nanos,
        off_transcript_tokens=gap,
        estimated_off_transcript_cost_nanos=estimate,
        basis=basis,
        mix_source_dates=tuple(sources),
    )


# -- from the scan store to a payload -------------------------------------


def utc_day_costs(slot_rows: Iterable[SlotUsage], card: RateCard) -> tuple[LocalDayCost, ...]:
    """Price measured local usage per UTC day, at each (day, model)'s own rate.

    ``tokens`` counts every component, cached input included, which is what
    the provider's daily bucket counts: Codex ``input_tokens`` is inclusive of
    the cached prefix, and the parser splits that prefix into
    ``cache_read_tokens`` without dropping it.
    """

    by_day_model: dict[tuple[str, str], list[int]] = {}
    for row in slot_rows:
        key = (utc_slot_day(row.usage_slot), row.model)
        bucket = by_day_model.setdefault(key, [0, 0, 0, 0, 0, 0])
        bucket[0] += row.input_tokens
        bucket[1] += row.output_tokens
        bucket[2] += row.cache_read_tokens
        bucket[3] += row.cache_create_5m_tokens
        bucket[4] += row.cache_create_1h_tokens
        bucket[5] += row.cache_create_other_tokens
    per_day: dict[str, list[int]] = {}
    for (day, model), amounts in by_day_model.items():
        priced = card.price(
            model,
            {
                "input_tokens": amounts[0],
                "output_tokens": amounts[1],
                "cache_read_tokens": amounts[2],
                "cache_create_5m_tokens": amounts[3],
                "cache_create_1h_tokens": amounts[4],
                "cache_create_other_tokens": amounts[5],
            },
        )
        row_total = per_day.setdefault(day, [0, 0, 0])
        row_total[0] += sum(amounts)
        row_total[1] += priced.priced_tokens
        row_total[2] += priced.amount_nanos or 0
    return tuple(
        LocalDayCost(usage_date=day, tokens=tokens, priced_tokens=priced, cost_nanos=cost)
        for day, (tokens, priced, cost) in sorted(per_day.items())
    )


def _apportion(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Split an integer exactly, largest remainder first; the parts sum to it."""

    positive = {key: weight for key, weight in weights.items() if weight > 0}
    if total <= 0 or not positive:
        return {}
    scale = sum(positive.values())
    shares = {key: total * weight // scale for key, weight in positive.items()}
    remainder = total - sum(shares.values())
    # Ties broken by key so the split is deterministic across runs and repos.
    ranked = sorted(
        positive, key=lambda key: (-(total * positive[key] % scale), key)
    )
    for key in ranked[:remainder]:
        shares[key] += 1
    return {key: value for key, value in shares.items() if value}


def _split_utc_day(
    utc_day: str,
    measured: Mapping[str, int] | None,
    tz: tzinfo | None,
) -> dict[str, int]:
    """Weights for placing one UTC day's estimate on the reader's local days.

    WHY measured volume: the estimate is usage the provider saw and this
    machine did not, and the least-assumption guess for WHEN in the UTC day it
    happened is when the measured usage of that same day happened. A UTC day
    with no measured volume falls back to wall-clock time -- each local day
    gets the share of the UTC day's 96 slots that fall on it -- rather than to
    one arbitrary side of midnight.
    """

    if measured and any(weight > 0 for weight in measured.values()):
        return dict(measured)
    weights: dict[str, int] = {}
    for slot in day_slots(utc_day, timezone.utc):
        local = slot_day(slot, tz)
        weights[local] = weights.get(local, 0) + 1
    return weights


@dataclass(frozen=True)
class AnchorPlan:
    """What anchoring decided for one provider, ready to merge into a payload."""

    state: str
    reason: str | None = None
    result: AnchorResult | None = None
    estimated_nanos_by_day: Mapping[str, int] = field(default_factory=dict)
    estimated_tokens_by_day: Mapping[str, int] = field(default_factory=dict)
    window_start: str | None = None
    window_end: str | None = None
    series_observed_at: str | None = None
    pre_window_provider_tokens: int = 0
    lifetime_tokens: int | None = None

    @property
    def estimated_cost_nanos(self) -> int | None:
        if self.state != ESTIMATE_APPLIED or self.result is None:
            return None
        return self.result.estimated_off_transcript_cost_nanos

    def anchor_block(self) -> dict[str, Any]:
        """The ``history.provider_anchor`` document; bounded scalars only."""

        document: dict[str, Any] = {
            "semantics": "provider_volume_anchored_cost",
            "state": self.state,
            "reason": self.reason,
            "estimate_basis": ESTIMATE_BASIS if self.state == ESTIMATE_APPLIED else None,
            "allocation_basis": ALLOCATION_BASIS if self.state == ESTIMATE_APPLIED else None,
            "day_basis": "utc",
            "series_observed_at": self.series_observed_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "lifetime_tokens": self.lifetime_tokens,
            "pre_window_provider_tokens": self.pre_window_provider_tokens,
        }
        if self.result is not None and self.state == ESTIMATE_APPLIED:
            totals = self.result.totals()
            document.update(
                {
                    "provider_tokens": totals["provider_tokens"],
                    "local_tokens": totals["local_tokens"],
                    "off_transcript_tokens": totals["off_transcript_tokens"],
                    "overlap_excess_tokens": totals["overlap_excess_tokens"],
                    "unpriceable_off_transcript_tokens": totals[
                        "unpriceable_off_transcript_tokens"
                    ],
                    "days": totals["days"],
                    "days_provider_above_local": totals["days_provider_above_local"],
                    "days_local_above_provider": totals["days_local_above_provider"],
                    "measured_cost_nanos": self.result.measured_cost_nanos,
                    "estimated_cost_nanos": self.result.estimated_off_transcript_cost_nanos,
                }
            )
        return document


def unavailable_plan(reason: str, *, observed_at: str | None = None) -> AnchorPlan:
    """No estimate, and exactly why. The cost stays measured-only."""

    return AnchorPlan(state=ESTIMATE_UNAVAILABLE, reason=reason, series_observed_at=observed_at)


def not_applicable_plan(reason: str = REASON_NO_PROVIDER_TOKEN_API) -> AnchorPlan:
    """A provider that publishes no daily token count, so nothing to anchor to."""

    return AnchorPlan(state=ESTIMATE_NOT_APPLICABLE, reason=reason)


def _parse_instant(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def plan_codex_anchor(
    slot_rows: Sequence[SlotUsage],
    card: RateCard,
    series: ProviderSeries,
    *,
    tz: tzinfo | None = None,
    excluded_utc_days: Iterable[str] = (),
    series_observed_at: str | None = None,
    now: datetime | None = None,
    max_window_days: int = DEFAULT_MAX_WINDOW_DAYS,
    max_series_age_seconds: int = MAX_SERIES_AGE_SECONDS,
) -> AnchorPlan:
    """Anchor measured Codex usage to the provider's daily series.

    ``excluded_utc_days`` are days another source already accounts for -- the
    frozen legacy rows, which equal the provider's buckets exactly -- and are
    never anchored again. The window starts at the first UTC day this machine
    has measured usage for: a provider day before any local transcript has no
    local mix to price at and is reported as ``pre_window_provider_tokens``
    instead of estimated.
    """

    observed = _parse_instant(series_observed_at)
    if observed is not None and now is not None:
        if (now.astimezone(timezone.utc) - observed).total_seconds() > max_series_age_seconds:
            return unavailable_plan(REASON_SERIES_EXPIRED, observed_at=series_observed_at)
    excluded = set(excluded_utc_days)
    rows = [row for row in slot_rows if utc_slot_day(row.usage_slot) not in excluded]
    local = utc_day_costs(rows, card)
    if not local:
        return unavailable_plan(REASON_NO_MEASURED_USAGE, observed_at=series_observed_at)
    window_start = local[0].usage_date
    provider = {
        day: tokens
        for day, tokens in series.daily.items()
        if day >= window_start and day not in excluded
    }
    pre_window = sum(
        tokens
        for day, tokens in series.daily.items()
        if day < window_start and day not in excluded
    )
    result = anchor_to_provider(local, provider, max_window_days=max_window_days)
    measured_split: dict[str, dict[str, int]] = {}
    for row in rows:
        per_local = measured_split.setdefault(utc_slot_day(row.usage_slot), {})
        local_day = slot_day(row.usage_slot, tz)
        per_local[local_day] = per_local.get(local_day, 0) + row.total_tokens
    nanos: dict[str, int] = {}
    tokens: dict[str, int] = {}
    for day in result.days:
        if day.off_transcript_tokens <= 0:
            continue
        weights = _split_utc_day(day.usage_date, measured_split.get(day.usage_date), tz)
        for local_day, amount in _apportion(day.off_transcript_tokens, weights).items():
            tokens[local_day] = tokens.get(local_day, 0) + amount
        if day.estimated_off_transcript_cost_nanos:
            for local_day, amount in _apportion(
                day.estimated_off_transcript_cost_nanos, weights
            ).items():
                nanos[local_day] = nanos.get(local_day, 0) + amount
    return AnchorPlan(
        state=ESTIMATE_APPLIED,
        result=result,
        estimated_nanos_by_day=nanos,
        estimated_tokens_by_day=tokens,
        window_start=window_start,
        window_end=max((day.usage_date for day in result.days), default=window_start),
        series_observed_at=series_observed_at,
        pre_window_provider_tokens=pre_window,
        lifetime_tokens=series.lifetime_tokens,
    )


# Per-day fields the plan writes. `api_rate_estimate_nanos` keeps meaning "the
# cost this day's bar shows", so it becomes measured + estimated; the two parts
# always travel beside it so a chart can shade the estimate and a hover split it.
DAILY_MEASURED_FIELD: Final = "measured_api_rate_estimate_nanos"
DAILY_ESTIMATED_FIELD: Final = "estimated_api_rate_estimate_nanos"
DAILY_ESTIMATED_TOKENS_FIELD: Final = "estimated_tokens"
ESTIMATE_ONLY_PROVENANCE: Final = "provider_anchor_estimate"


def apply_anchor_plan(
    history: MutableMapping[str, Any],
    cost: MutableMapping[str, Any],
    plan: AnchorPlan,
    *,
    measured_nanos: int | None,
    legacy_nanos: int,
) -> None:
    """Merge a plan into one provider's ``history`` and ``api_rate_estimate``.

    ``measured_nanos`` is the provider's whole self-computed priced cost and
    ``legacy_nanos`` its frozen imported cost, both over the FULL history
    rather than the served (row-capped) days. The headline becomes
    ``measured + estimated + legacy`` -- every dollar the daily chart draws --
    and each part is stated beside it. ``estimated_amount_nanos`` is ``None``
    whenever no estimate was made, never 0 standing in for "unknown".
    """

    applied = plan.state == ESTIMATE_APPLIED
    estimated_total = plan.estimated_cost_nanos if applied else None
    daily = history.get("daily")
    rows: list[dict[str, Any]] = daily if isinstance(daily, list) else []
    dated = {row.get("date"): row for row in rows if isinstance(row, dict)}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("provenance") == "legacy_import":
            row[DAILY_MEASURED_FIELD] = None
            row[DAILY_ESTIMATED_FIELD] = None
            row[DAILY_ESTIMATED_TOKENS_FIELD] = None
            continue
        measured = row.get("api_rate_estimate_nanos")
        row[DAILY_MEASURED_FIELD] = measured
        if not applied:
            row[DAILY_ESTIMATED_FIELD] = None
            row[DAILY_ESTIMATED_TOKENS_FIELD] = None
            continue
        estimate = plan.estimated_nanos_by_day.get(row.get("date"), 0)
        row[DAILY_ESTIMATED_FIELD] = estimate
        row[DAILY_ESTIMATED_TOKENS_FIELD] = plan.estimated_tokens_by_day.get(row.get("date"), 0)
        if estimate:
            row["api_rate_estimate_nanos"] = (measured or 0) + estimate
    if applied:
        # A local day the provider saw usage on and this machine did not gets
        # a row of its own, carrying no measured token at all -- labelled so
        # it can never be read as a day this machine observed.
        for day in sorted(set(plan.estimated_nanos_by_day) | set(plan.estimated_tokens_by_day)):
            if day in dated:
                continue
            estimate = plan.estimated_nanos_by_day.get(day, 0)
            rows.append(
                {
                    "date": day,
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_create_other_tokens": 0,
                    "api_rate_estimate_nanos": estimate,
                    DAILY_MEASURED_FIELD: 0,
                    DAILY_ESTIMATED_FIELD: estimate,
                    DAILY_ESTIMATED_TOKENS_FIELD: plan.estimated_tokens_by_day.get(day, 0),
                    "provenance": ESTIMATE_ONLY_PROVENANCE,
                    "model_breakdowns": [],
                }
            )
        rows.sort(key=lambda row: str(row.get("date")))
        if isinstance(daily, list):
            history["daily"] = rows
    history["provider_anchor"] = plan.anchor_block()
    headline_parts = [part for part in (measured_nanos, estimated_total) if part is not None]
    if legacy_nanos:
        headline_parts.append(legacy_nanos)
    cost["measured_amount_nanos"] = measured_nanos
    cost["estimated_amount_nanos"] = estimated_total
    cost["legacy_amount_nanos"] = legacy_nanos
    cost["estimate_state"] = plan.state
    cost["estimate_reason"] = plan.reason
    cost["estimate_basis"] = ESTIMATE_BASIS if applied else None
    cost["provider_series_observed_at"] = plan.series_observed_at
    cost["amount_semantics"] = "measured_plus_estimated_plus_legacy"
    if headline_parts:
        cost["amount_nanos"] = sum(headline_parts)

