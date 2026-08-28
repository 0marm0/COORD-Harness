"""Route work by measured usage instead of by assumption.

A fleet with two lanes tends to route by habit: this kind of work goes to that
agent. Habit is fine until one lane is near a limit and the other is idle, and
nothing in the system knows which is which.

This module answers one question — *given what has actually been recorded, which
provider has room* — and it answers it as **advice with its evidence attached**,
never as a switch it performs itself. Nothing here writes to the ledger, the
board, or any client configuration.

Three properties matter more than the recommendation:

* **It reads the ledger through the ledger's own public API.** No new tables, no
  second accounting store, no duplicated arithmetic that could disagree with the
  usage dashboard about the same week.
* **Incomplete coverage is never silently totalled.** The ledger records a
  ``coverage_state`` per observation. A week missing two days sums to a smaller
  number than the truth, and a router that believed that number would send work
  to the lane that is *most* used. Every window therefore carries how many days
  it expected, how many it observed, and what states those observations were in.
* **No data is an answer, not a default.** With nothing recorded, or with no
  declared budget to measure against, the advice is an explicit refusal naming
  what is missing. It never falls back to "use the usual one", because that is
  indistinguishable from a real recommendation at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .local_routing import recommend_local_dashboard as recommend_local_dashboard

# Observation coverage states the ledger can report. Anything outside this set
# is unknown to this module and is treated as NOT complete: a state we cannot
# interpret must never be optimistically counted as full coverage.
COMPLETE_COVERAGE = "complete"

# Token fields that make up "how much was used". Cache reads are counted because
# they are billed usage; they are also reported separately so a caller can see
# a lane whose weight is mostly cache.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_5m_tokens",
    "cache_create_1h_tokens",
    "cache_create_other_tokens",
)


class RoutingError(ValueError):
    """A routing input that cannot be interpreted rather than merely empty."""


@dataclass(frozen=True)
class UsageWindow:
    """The exact span a total was computed over, carried with every total."""

    days: int
    start_date: str
    end_date: str

    def as_dict(self) -> dict[str, Any]:
        return {"days": self.days, "start_date": self.start_date, "end_date": self.end_date}


@dataclass(frozen=True)
class ProviderUsage:
    """One provider's measured usage across one window, with its coverage."""

    provider: str
    account_key: str
    window: UsageWindow
    total_tokens: int
    tokens_by_field: Mapping[str, int]
    request_count: int | None
    native_cost_nanos: int | None
    days_expected: int
    days_observed: int
    coverage_states: Mapping[str, int]

    @property
    def complete(self) -> bool:
        """True only when every day is present and every row reads complete.

        Deliberately strict. A window that is 6/7 days present is not "roughly
        right" for routing: the missing day is exactly the one that could put a
        lane over its limit.
        """
        if self.days_observed < self.days_expected:
            return False
        if not self.coverage_states:
            return False
        return all(state == COMPLETE_COVERAGE for state in self.coverage_states)

    @property
    def missing_days(self) -> int:
        return max(0, self.days_expected - self.days_observed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account_key": self.account_key,
            "window": self.window.as_dict(),
            "total_tokens": self.total_tokens,
            "tokens_by_field": dict(self.tokens_by_field),
            "request_count": self.request_count,
            "native_cost_nanos": self.native_cost_nanos,
            "days_expected": self.days_expected,
            "days_observed": self.days_observed,
            "missing_days": self.missing_days,
            "coverage_states": dict(self.coverage_states),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class ProviderVerdict:
    """What the advisor concluded about one provider, and why."""

    provider: str
    usage: ProviderUsage
    budget_tokens: int | None
    remaining_tokens: int | None
    used_fraction: float | None
    eligible: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "usage": self.usage.as_dict(),
            "budget_tokens": self.budget_tokens,
            "remaining_tokens": self.remaining_tokens,
            "used_fraction": self.used_fraction,
            "eligible": self.eligible,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RoutingAdvice:
    """Advice, its confidence, and the evidence it was computed from."""

    recommended: str | None
    reason: str
    confident: bool
    window: UsageWindow
    verdicts: Sequence[ProviderVerdict] = field(default_factory=tuple)
    caveats: Sequence[str] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "UsageRoutingAdviceV1",
            "recommended": self.recommended,
            "reason": self.reason,
            "confident": self.confident,
            "window": self.window.as_dict(),
            "caveats": list(self.caveats),
            "verdicts": [verdict.as_dict() for verdict in self.verdicts],
        }


def _iso(day: date) -> str:
    return day.isoformat()


def usage_window(days: int, *, today: date | None = None) -> UsageWindow:
    """The inclusive window of the last ``days`` calendar days ending today."""
    if days < 1:
        raise RoutingError("a usage window must cover at least one day")
    end = today or date.today()
    start = end - timedelta(days=days - 1)
    return UsageWindow(days=days, start_date=_iso(start), end_date=_iso(end))


def summarize_rows(
    rows: Iterable[Mapping[str, Any]],
    window: UsageWindow,
) -> list[ProviderUsage]:
    """Fold ledger daily rows into one summary per provider and account.

    ``rows`` is whatever ``UsageLedger.corrected_rows()`` returns: this function
    filters to the window itself rather than asking the ledger for a narrower
    read, so the arithmetic stays in one place and the ledger keeps its public
    surface unchanged.
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        usage_date = str(row.get("usage_date") or "")
        if not (window.start_date <= usage_date <= window.end_date):
            continue
        provider = str(row.get("provider") or "").strip()
        account = str(row.get("account_key") or "").strip()
        if not provider:
            continue
        key = (provider, account)
        bucket = buckets.setdefault(
            key,
            {
                "tokens": {name: 0 for name in _TOKEN_FIELDS},
                "requests": None,
                "cost": None,
                "days": set(),
                "states": {},
            },
        )
        for name in _TOKEN_FIELDS:
            value = row.get(name)
            if isinstance(value, int):
                bucket["tokens"][name] += value
        requests = row.get("request_count")
        if isinstance(requests, int):
            bucket["requests"] = (bucket["requests"] or 0) + requests
        cost = row.get("provider_native_cost_nanos")
        if isinstance(cost, int):
            bucket["cost"] = (bucket["cost"] or 0) + cost
        bucket["days"].add(usage_date)
        # An absent coverage_state is recorded as "unknown" rather than assumed
        # complete: the strictness lives in one place, here.
        state = str(row.get("coverage_state") or "unknown").strip().lower() or "unknown"
        bucket["states"][state] = bucket["states"].get(state, 0) + 1

    summaries: list[ProviderUsage] = []
    for (provider, account), bucket in sorted(buckets.items()):
        tokens = bucket["tokens"]
        summaries.append(
            ProviderUsage(
                provider=provider,
                account_key=account,
                window=window,
                total_tokens=sum(tokens.values()),
                tokens_by_field=dict(tokens),
                request_count=bucket["requests"],
                native_cost_nanos=bucket["cost"],
                days_expected=window.days,
                days_observed=len(bucket["days"]),
                coverage_states=dict(sorted(bucket["states"].items())),
            )
        )
    return summaries


def _fmt(value: int) -> str:
    return f"{value:,}"


def advise(
    summaries: Sequence[ProviderUsage],
    budgets: Mapping[str, int] | None,
    window: UsageWindow,
    *,
    require_complete: bool = False,
) -> RoutingAdvice:
    """Recommend the provider with the most headroom, or refuse and say why.

    ``budgets`` maps provider name to a token allowance for this window. A
    provider with no declared budget cannot be compared -- headroom is
    meaningless without a limit -- so it is reported and excluded rather than
    guessed at.

    ``require_complete`` turns incomplete coverage from a caveat into a refusal.
    The default discloses instead of refusing, because a partial week is still
    worth acting on when the caller knows it is partial; what is never
    acceptable is acting on it without being told.
    """
    caveats: list[str] = []

    if not summaries:
        return RoutingAdvice(
            recommended=None,
            reason=(
                "No usage was recorded in this window, so there is nothing to route on. "
                "This is not a recommendation to use any particular provider."
            ),
            confident=False,
            window=window,
            caveats=("no observations in window",),
        )

    if not budgets:
        return RoutingAdvice(
            recommended=None,
            reason=(
                "Usage was recorded but no budget is declared for any provider, so "
                "headroom cannot be computed. Declare a per-provider allowance for "
                "this window to get a recommendation."
            ),
            confident=False,
            window=window,
            verdicts=tuple(
                ProviderVerdict(
                    provider=summary.provider,
                    usage=summary,
                    budget_tokens=None,
                    remaining_tokens=None,
                    used_fraction=None,
                    eligible=False,
                    reason="no declared budget for this provider",
                )
                for summary in summaries
            ),
            caveats=("no declared budgets",),
        )

    # A budgeted provider with no rows in the window is not absent from the
    # fleet -- it is the lane that used nothing, which is the lane with the most
    # headroom. Omitting it because the ledger has no row would hide exactly the
    # provider the caller should hear about. It is added with a zero total and
    # zero observed days, so `complete` is False and the caller is told the
    # difference between "measured zero" and "never observed".
    seen = {summary.provider for summary in summaries}
    unobserved = [
        ProviderUsage(
            provider=provider,
            account_key="",
            window=window,
            total_tokens=0,
            tokens_by_field={name: 0 for name in _TOKEN_FIELDS},
            request_count=None,
            native_cost_nanos=None,
            days_expected=window.days,
            days_observed=0,
            coverage_states={},
        )
        for provider in sorted(set(budgets) - seen)
    ]
    for summary in unobserved:
        caveats.append(
            f"{summary.provider}: no observation in this window at all — treated as "
            "unused, but that is absence of evidence, not evidence of zero"
        )

    verdicts: list[ProviderVerdict] = []
    for summary in [*summaries, *unobserved]:
        budget = budgets.get(summary.provider)
        if budget is None:
            verdicts.append(
                ProviderVerdict(
                    provider=summary.provider,
                    usage=summary,
                    budget_tokens=None,
                    remaining_tokens=None,
                    used_fraction=None,
                    eligible=False,
                    reason="no declared budget for this provider",
                )
            )
            continue
        if budget <= 0:
            raise RoutingError(f"budget for {summary.provider!r} must be positive")

        remaining = budget - summary.total_tokens
        fraction = summary.total_tokens / budget
        if not summary.complete:
            missing = summary.missing_days
            detail = (
                f"{missing} of {summary.days_expected} days have no observation"
                if missing
                else "an observation in this window is not marked complete"
            )
            caveats.append(
                f"{summary.provider}: {detail}, so {_fmt(summary.total_tokens)} tokens "
                "is a floor, not a total"
            )

        if require_complete and not summary.complete:
            verdicts.append(
                ProviderVerdict(
                    provider=summary.provider,
                    usage=summary,
                    budget_tokens=budget,
                    remaining_tokens=remaining,
                    used_fraction=fraction,
                    eligible=False,
                    reason="coverage is incomplete and complete coverage was required",
                )
            )
            continue

        if remaining <= 0:
            verdicts.append(
                ProviderVerdict(
                    provider=summary.provider,
                    usage=summary,
                    budget_tokens=budget,
                    remaining_tokens=remaining,
                    used_fraction=fraction,
                    eligible=False,
                    reason=(
                        f"used {_fmt(summary.total_tokens)} of {_fmt(budget)} tokens "
                        f"({fraction:.0%}) — no headroom left in this window"
                    ),
                )
            )
            continue

        verdicts.append(
            ProviderVerdict(
                provider=summary.provider,
                usage=summary,
                budget_tokens=budget,
                remaining_tokens=remaining,
                used_fraction=fraction,
                eligible=True,
                reason=(
                    f"used {_fmt(summary.total_tokens)} of {_fmt(budget)} tokens "
                    f"({fraction:.0%}), {_fmt(remaining)} remaining"
                ),
            )
        )

    eligible = [verdict for verdict in verdicts if verdict.eligible]
    if not eligible:
        return RoutingAdvice(
            recommended=None,
            reason=(
                "Every provider with a declared budget is out of headroom in this "
                "window, or was excluded. Nothing is recommended; the per-provider "
                "reasons are attached."
            ),
            confident=True,
            window=window,
            verdicts=tuple(verdicts),
            caveats=tuple(caveats),
        )

    # Most headroom first; ties broken by provider name so the same inputs always
    # produce the same advice.
    eligible.sort(key=lambda verdict: (-(verdict.remaining_tokens or 0), verdict.provider))
    winner = eligible[0]
    confident = all(verdict.usage.complete for verdict in eligible)
    runner_up = eligible[1] if len(eligible) > 1 else None
    margin = (
        f" — {_fmt((winner.remaining_tokens or 0) - (runner_up.remaining_tokens or 0))} "
        f"more than {runner_up.provider}"
        if runner_up
        else " — the only provider with headroom"
    )
    return RoutingAdvice(
        recommended=winner.provider,
        reason=f"{winner.provider} has the most headroom{margin}.",
        confident=confident,
        window=window,
        verdicts=tuple(verdicts),
        caveats=tuple(caveats),
    )


def advise_from_ledger(
    ledger: Any,
    budgets: Mapping[str, int] | None,
    *,
    days: int = 7,
    today: date | None = None,
    require_complete: bool = False,
) -> RoutingAdvice:
    """Convenience path: read the ledger's corrected rows and advise over them.

    ``ledger`` is any object exposing ``corrected_rows()`` — the real
    ``UsageLedger`` in production, a stub in tests. Nothing is written.
    """
    window = usage_window(days, today=today)
    rows = ledger.corrected_rows()
    return advise(summarize_rows(rows, window), budgets, window, require_complete=require_complete)


def render(advice: RoutingAdvice) -> str:
    """A short human rendering for the CLI, evidence included."""
    lines = [
        f"window   {advice.window.start_date} .. {advice.window.end_date} "
        f"({advice.window.days} days)",
    ]
    if advice.recommended:
        lines.append(f"route to {advice.recommended}")
    else:
        lines.append("route to (no recommendation)")
    lines.append(f"because  {advice.reason}")
    if not advice.confident:
        lines.append("         this advice is NOT confident; see caveats")
    for verdict in advice.verdicts:
        mark = "+" if verdict.eligible else "-"
        lines.append(f"  {mark} {verdict.provider}: {verdict.reason}")
    for caveat in advice.caveats:
        lines.append(f"  ! {caveat}")
    return "\n".join(lines)
