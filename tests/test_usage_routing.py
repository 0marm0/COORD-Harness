"""The routing advisor must refuse as clearly as it recommends.

Every test here is written so it fails against the obvious wrong
implementation: one that totals whatever rows it finds, ignores coverage, and
falls back to a default provider when it knows nothing.
"""

from __future__ import annotations

from datetime import date

import pytest

from coordharness.usage.routing import (
    RoutingError,
    advise,
    advise_from_ledger,
    render,
    summarize_rows,
    usage_window,
)

TODAY = date(2026, 8, 27)


def _row(provider, usage_date, *, tokens=1_000, coverage="complete", account="acct"):
    return {
        "provider": provider,
        "account_key": account,
        "usage_date": usage_date,
        "model": "m",
        "tier": "default",
        "coverage_state": coverage,
        "input_tokens": tokens,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_5m_tokens": 0,
        "cache_create_1h_tokens": 0,
        "cache_create_other_tokens": 0,
        "request_count": 1,
        "provider_native_cost_nanos": 10,
    }


def _week(provider, *, per_day=1_000, days=7, coverage="complete"):
    return [
        _row(provider, f"2026-08-{21 + offset:02d}", tokens=per_day, coverage=coverage)
        for offset in range(days)
    ]


class _StubLedger:
    def __init__(self, rows):
        self._rows = rows

    def corrected_rows(self):
        return list(self._rows)


def test_window_is_inclusive_and_ends_today():
    window = usage_window(7, today=TODAY)
    assert window.start_date == "2026-08-21"
    assert window.end_date == "2026-08-27"
    assert window.days == 7


def test_a_zero_day_window_is_refused_rather_than_silently_widened():
    with pytest.raises(RoutingError):
        usage_window(0, today=TODAY)


def test_rows_outside_the_window_are_not_counted():
    window = usage_window(7, today=TODAY)
    rows = _week("claude") + [_row("claude", "2026-08-01", tokens=999_999)]
    summaries = summarize_rows(rows, window)
    assert len(summaries) == 1
    # The stale row is 999,999 tokens. If the window were ignored it would
    # dominate; the total must be exactly the seven in-window days.
    assert summaries[0].total_tokens == 7_000


def test_no_usage_refuses_instead_of_defaulting_to_a_provider():
    window = usage_window(7, today=TODAY)
    advice = advise([], {"claude": 100_000, "codex": 100_000}, window)
    assert advice.recommended is None
    assert advice.confident is False
    assert "nothing to route on" in advice.reason
    # The failure mode this guards: a fallback that names a provider anyway.
    assert "claude" not in advice.reason
    assert "codex" not in advice.reason


def test_usage_without_declared_budgets_refuses_and_says_which_providers_it_saw():
    window = usage_window(7, today=TODAY)
    summaries = summarize_rows(_week("claude"), window)
    advice = advise(summaries, {}, window)
    assert advice.recommended is None
    assert "no budget is declared" in advice.reason
    assert [verdict.provider for verdict in advice.verdicts] == ["claude"]
    assert advice.verdicts[0].eligible is False


def test_the_provider_with_more_headroom_wins_and_the_margin_is_stated():
    window = usage_window(7, today=TODAY)
    rows = _week("claude", per_day=9_000) + _week("codex", per_day=1_000)
    advice = advise(summarize_rows(rows, window), {"claude": 100_000, "codex": 100_000}, window)
    assert advice.recommended == "codex"
    assert advice.confident is True
    assert "more than claude" in advice.reason
    remaining = {verdict.provider: verdict.remaining_tokens for verdict in advice.verdicts}
    assert remaining == {"claude": 37_000, "codex": 93_000}


def test_an_exhausted_provider_is_excluded_with_its_number_named():
    window = usage_window(7, today=TODAY)
    rows = _week("claude", per_day=20_000) + _week("codex", per_day=1_000)
    advice = advise(summarize_rows(rows, window), {"claude": 100_000, "codex": 100_000}, window)
    assert advice.recommended == "codex"
    exhausted = next(v for v in advice.verdicts if v.provider == "claude")
    assert exhausted.eligible is False
    assert "no headroom left" in exhausted.reason
    assert "140,000" in exhausted.reason


def test_every_provider_exhausted_yields_a_confident_refusal():
    window = usage_window(7, today=TODAY)
    rows = _week("claude", per_day=20_000) + _week("codex", per_day=20_000)
    advice = advise(summarize_rows(rows, window), {"claude": 10_000, "codex": 10_000}, window)
    assert advice.recommended is None
    # Confident: we know the answer is "nobody", which is different from not knowing.
    assert advice.confident is True
    assert all(verdict.eligible is False for verdict in advice.verdicts)


def test_missing_days_make_the_total_a_floor_and_the_advice_unconfident():
    window = usage_window(7, today=TODAY)
    # Five of seven days observed: the sum understates, and routing on an
    # understated total sends work to the lane that is most used.
    rows = _week("claude", per_day=1_000, days=5) + _week("codex", per_day=2_000)
    advice = advise(summarize_rows(rows, window), {"claude": 100_000, "codex": 100_000}, window)
    assert advice.confident is False
    assert any("floor, not a total" in caveat for caveat in advice.caveats)
    assert any("2 of 7 days have no observation" in caveat for caveat in advice.caveats)


def test_a_partial_coverage_state_is_not_counted_as_complete():
    window = usage_window(7, today=TODAY)
    summaries = summarize_rows(_week("claude", coverage="partial"), window)
    assert summaries[0].days_observed == 7
    # Every day present, but the observations themselves are partial.
    assert summaries[0].complete is False


def test_an_unrecognised_coverage_state_is_treated_as_not_complete():
    window = usage_window(7, today=TODAY)
    summaries = summarize_rows(_week("claude", coverage="reconciling"), window)
    assert summaries[0].complete is False
    assert "reconciling" in summaries[0].coverage_states


def test_a_missing_coverage_state_is_unknown_rather_than_assumed_complete():
    window = usage_window(7, today=TODAY)
    rows = _week("claude")
    for row in rows:
        row.pop("coverage_state")
    summaries = summarize_rows(rows, window)
    assert summaries[0].complete is False
    assert summaries[0].coverage_states == {"unknown": 7}


def test_require_complete_turns_a_caveat_into_an_exclusion():
    window = usage_window(7, today=TODAY)
    rows = _week("claude", per_day=1_000, days=4) + _week("codex", per_day=9_000)
    budgets = {"claude": 100_000, "codex": 100_000}
    lenient = advise(summarize_rows(rows, window), budgets, window)
    strict = advise(summarize_rows(rows, window), budgets, window, require_complete=True)
    # Lenient prefers claude on a total it has disclosed is a floor.
    assert lenient.recommended == "claude"
    assert lenient.confident is False
    # Strict refuses to use that floor at all.
    assert strict.recommended == "codex"
    excluded = next(v for v in strict.verdicts if v.provider == "claude")
    assert "complete coverage was required" in excluded.reason


def test_ties_break_deterministically_so_the_same_inputs_advise_the_same_way():
    window = usage_window(7, today=TODAY)
    rows = _week("zeta", per_day=1_000) + _week("alpha", per_day=1_000)
    budgets = {"zeta": 100_000, "alpha": 100_000}
    first = advise(summarize_rows(rows, window), budgets, window)
    second = advise(summarize_rows(list(reversed(rows)), window), budgets, window)
    assert first.recommended == second.recommended == "alpha"


def test_a_nonpositive_budget_is_an_error_not_a_division():
    window = usage_window(7, today=TODAY)
    summaries = summarize_rows(_week("claude"), window)
    with pytest.raises(RoutingError):
        advise(summaries, {"claude": 0}, window)


def test_the_ledger_path_reads_without_writing():
    ledger = _StubLedger(_week("claude", per_day=1_000) + _week("codex", per_day=5_000))
    advice = advise_from_ledger(ledger, {"claude": 100_000, "codex": 100_000},
                                days=7, today=TODAY)
    assert advice.recommended == "claude"
    # corrected_rows() returns a copy each call; the advisor must not mutate it.
    assert len(ledger.corrected_rows()) == 14


def test_the_rendering_always_shows_the_window_and_the_evidence():
    window = usage_window(7, today=TODAY)
    rows = _week("claude", per_day=9_000) + _week("codex", per_day=1_000)
    text = render(advise(summarize_rows(rows, window), {"claude": 100_000, "codex": 100_000}, window))
    assert "2026-08-21 .. 2026-08-27" in text
    assert "route to codex" in text
    # Both providers appear, not just the winner: the evidence is the point,
    # and the lane that was NOT chosen must still show its number.
    assert "+ codex" in text
    assert "claude" in text and "63,000 of 100,000" in text


def test_the_rendering_of_a_refusal_does_not_read_like_a_recommendation():
    window = usage_window(7, today=TODAY)
    text = render(advise([], {"claude": 1}, window))
    assert "route to (no recommendation)" in text
    assert "NOT confident" in text


def test_serialisation_carries_the_window_on_every_total():
    window = usage_window(7, today=TODAY)
    summaries = summarize_rows(_week("claude"), window)
    document = advise(summaries, {"claude": 100_000}, window).as_dict()
    assert document["schema_version"] == "UsageRoutingAdviceV1"
    assert document["window"]["days"] == 7
    # A total detached from its window is the thing that goes stale silently.
    assert document["verdicts"][0]["usage"]["window"]["start_date"] == "2026-08-21"


def test_a_budgeted_lane_with_no_rows_is_considered_not_silently_dropped():
    """Absence of evidence is not evidence of zero — but it is also not absence
    of the lane. A provider that recorded nothing in the window has the MOST
    headroom, and dropping it because the ledger has no row would hide exactly
    the lane the caller should hear about."""
    window = usage_window(1, today=TODAY)
    rows = _row("claude", "2026-08-27", tokens=9_000)
    advice = advise(summarize_rows([rows], window),
                    {"claude": 100_000, "codex": 100_000}, window)
    assert advice.recommended == "codex"
    codex = next(v for v in advice.verdicts if v.provider == "codex")
    assert codex.eligible is True
    assert codex.remaining_tokens == 100_000
    assert codex.usage.days_observed == 0
    # It must say the zero is unobserved, not measured.
    assert any("absence of evidence, not evidence of zero" in c for c in advice.caveats)
    assert advice.confident is False


def test_an_unobserved_lane_is_still_excluded_under_require_complete():
    window = usage_window(1, today=TODAY)
    rows = _row("claude", "2026-08-27", tokens=9_000)
    advice = advise(summarize_rows([rows], window),
                    {"claude": 100_000, "codex": 100_000}, window,
                    require_complete=True)
    # With proof required, a lane we have never observed cannot be chosen.
    assert advice.recommended == "claude"
    codex = next(v for v in advice.verdicts if v.provider == "codex")
    assert codex.eligible is False
