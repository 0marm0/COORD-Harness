"""Provider-volume anchoring: measured cost stays measured, the gap is an estimate.

The provider's daily account volume (Codex app-server ``account/usage/read``)
exceeds local transcripts on 62 of 63 UTC days measured on 2026-09-22. These
tests pin that the anchoring never presents the estimate as measured, prices
the gap at the day's own mix rather than a flat blend, and never deducts
measured cost when local exceeds the provider.
"""

from __future__ import annotations

import pytest

from coordharness.usage.provider_anchor import (
    BASIS_ALIGNED,
    BASIS_LOCAL_EXCEEDS,
    BASIS_LOCAL_MIX,
    BASIS_NEAREST_MIX,
    BASIS_NO_PROVIDER,
    BASIS_UNPRICEABLE,
    LocalDayCost,
    ProviderSeriesError,
    anchor_to_provider,
    parse_account_usage,
)

pytestmark = pytest.mark.unit

_USD = 1_000_000_000


def _by_day(result):
    return {day.usage_date: day for day in result.days}


def test_parse_account_usage_reads_lifetime_and_buckets() -> None:
    series = parse_account_usage(
        {
            "summary": {"lifetimeTokens": 30, "peakDailyTokens": 20},
            "dailyUsageBuckets": [
                {"startDate": "2026-09-02", "tokens": 20},
                {"startDate": "2026-09-01", "tokens": 10},
            ],
        }
    )
    assert series.lifetime_tokens == 30
    assert list(series.daily.items()) == [("2026-09-01", 10), ("2026-09-02", 20)]
    assert series.reconciles_with_lifetime is True


def test_parse_account_usage_flags_buckets_that_do_not_sum_to_lifetime() -> None:
    series = parse_account_usage(
        {"summary": {"lifetimeTokens": 99}, "dailyUsageBuckets": [{"startDate": "2026-09-01", "tokens": 1}]}
    )
    assert series.reconciles_with_lifetime is False
    assert parse_account_usage({"dailyUsageBuckets": []}).reconciles_with_lifetime is None


@pytest.mark.parametrize(
    "buckets",
    [
        [{"startDate": "2026-09-01", "tokens": 1}, {"startDate": "2026-09-01", "tokens": 2}],
        [{"startDate": "2026-09-01", "tokens": -1}],
        [{"startDate": "2026-09-01", "tokens": True}],
        [{"startDate": "2026-9-1", "tokens": 1}],
        [{"startDate": "2026-09-31", "tokens": 1}],
        ["2026-09-01"],
    ],
)
def test_parse_account_usage_refuses_malformed_buckets(buckets) -> None:
    with pytest.raises(ProviderSeriesError):
        parse_account_usage({"dailyUsageBuckets": buckets})


def test_parse_account_usage_refuses_missing_series_and_oversized_series() -> None:
    with pytest.raises(ProviderSeriesError):
        parse_account_usage({"summary": {"lifetimeTokens": 1}})
    with pytest.raises(ProviderSeriesError):
        parse_account_usage(
            {"dailyUsageBuckets": [{"startDate": "2026-09-01", "tokens": 1}] * 3}, max_buckets=2
        )


def test_gap_is_priced_at_the_days_own_mix_and_kept_apart_from_measured() -> None:
    # Day mix: 1M priced tokens cost $0.50 -> $0.50/M. Provider saw 3M.
    local = [LocalDayCost("2026-09-01", 1_000_000, 1_000_000, _USD // 2)]
    result = anchor_to_provider(local, {"2026-09-01": 3_000_000})
    day = _by_day(result)["2026-09-01"]
    assert day.basis == BASIS_LOCAL_MIX
    assert day.measured_cost_nanos == _USD // 2
    assert day.off_transcript_tokens == 2_000_000
    assert day.estimated_off_transcript_cost_nanos == _USD
    totals = result.totals()
    assert totals["measured_cost_usd"] == "0.500000000"
    assert totals["estimated_off_transcript_cost_usd"] == "1.000000000"
    assert totals["anchored_cost_usd"] == "1.500000000"
    assert totals["measured_semantics"] != totals["estimated_semantics"]


def test_mix_is_per_day_not_a_flat_blend() -> None:
    # A cheap cache-heavy day and an expensive fresh-input day; the gap sits
    # entirely on the expensive day, so a flat blend would underprice it.
    local = [
        LocalDayCost("2026-09-01", 10_000_000, 10_000_000, _USD),  # $0.10/M
        LocalDayCost("2026-09-02", 1_000_000, 1_000_000, 5 * _USD),  # $5/M
    ]
    result = anchor_to_provider(local, {"2026-09-01": 10_000_000, "2026-09-02": 2_000_000})
    days = _by_day(result)
    assert days["2026-09-01"].basis == BASIS_ALIGNED
    assert days["2026-09-01"].estimated_off_transcript_cost_nanos == 0
    assert days["2026-09-02"].estimated_off_transcript_cost_nanos == 5 * _USD


def test_unpriced_local_tokens_do_not_dilute_the_mix() -> None:
    local = [LocalDayCost("2026-09-01", 2_000_000, 1_000_000, _USD)]
    result = anchor_to_provider(local, {"2026-09-01": 3_000_000})
    assert _by_day(result)["2026-09-01"].estimated_off_transcript_cost_nanos == _USD


def test_local_above_provider_is_reported_never_deducted() -> None:
    local = [LocalDayCost("2026-09-01", 5_000_000, 5_000_000, 3 * _USD)]
    result = anchor_to_provider(local, {"2026-09-01": 4_000_000})
    day = _by_day(result)["2026-09-01"]
    assert day.basis == BASIS_LOCAL_EXCEEDS
    assert day.measured_cost_nanos == 3 * _USD
    assert day.off_transcript_tokens == 0
    assert day.overlap_excess_tokens == 1_000_000
    assert result.totals()["days_local_above_provider"] == 1
    assert result.anchored_cost_nanos == 3 * _USD


def test_day_without_local_tokens_borrows_the_nearest_priced_mix() -> None:
    local = [
        LocalDayCost("2026-09-01", 1_000_000, 1_000_000, _USD),  # $1/M
        LocalDayCost("2026-09-03", 1_000_000, 1_000_000, 3 * _USD),  # $3/M
        LocalDayCost("2026-09-09", 1_000_000, 1_000_000, 100 * _USD),  # far away
    ]
    result = anchor_to_provider(
        local,
        {"2026-09-01": 1_000_000, "2026-09-02": 1_000_000, "2026-09-03": 1_000_000},
    )
    day = _by_day(result)["2026-09-02"]
    assert day.basis == BASIS_NEAREST_MIX
    assert day.mix_source_dates == ("2026-09-01", "2026-09-03")
    # Token-weighted over the radius-1 window: $4 / 2M tokens = $2/M.
    assert day.estimated_off_transcript_cost_nanos == 2 * _USD
    assert day.measured_cost_nanos == 0


def test_gap_with_no_priced_neighbour_in_window_stays_unpriced_not_zero() -> None:
    local = [LocalDayCost("2026-09-20", 1_000_000, 1_000_000, _USD)]
    result = anchor_to_provider(local, {"2026-09-01": 7}, max_window_days=7)
    day = _by_day(result)["2026-09-01"]
    assert day.basis == BASIS_UNPRICEABLE
    assert day.estimated_off_transcript_cost_nanos is None
    assert result.unpriceable_off_transcript_tokens == 7
    assert result.totals()["unpriceable_off_transcript_tokens"] == 7


def test_local_day_without_provider_bucket_is_measured_only() -> None:
    local = [LocalDayCost("2026-09-01", 10, 10, 10)]
    day = _by_day(anchor_to_provider(local, {}))["2026-09-01"]
    assert day.basis == BASIS_NO_PROVIDER
    assert day.provider_tokens is None
    assert day.estimated_off_transcript_cost_nanos == 0


@pytest.mark.parametrize(
    "rows",
    [
        [LocalDayCost("2026-09-01", 1, 1, 1), LocalDayCost("2026-09-01", 1, 1, 1)],
        [LocalDayCost("2026-09-01", 1, 2, 1)],
        [LocalDayCost("2026-09-01", -1, 0, 0)],
        [LocalDayCost("09/01/2026", 1, 1, 1)],
    ],
)
def test_inconsistent_local_rows_are_refused(rows) -> None:
    with pytest.raises(ValueError):
        anchor_to_provider(rows, {})


def test_invalid_provider_bucket_and_window_are_refused() -> None:
    with pytest.raises(ValueError):
        anchor_to_provider([], {"2026-09-01": -5})
    with pytest.raises(ValueError):
        anchor_to_provider([], {}, max_window_days=-1)


def test_as_dict_labels_every_figure() -> None:
    local = [LocalDayCost("2026-09-01", 1_000_000, 1_000_000, _USD)]
    row = anchor_to_provider(local, {"2026-09-01": 2_000_000}).days[0].as_dict()
    assert row == {
        "date": "2026-09-01",
        "provider_tokens": 2_000_000,
        "local_tokens": 1_000_000,
        "off_transcript_tokens": 1_000_000,
        "overlap_excess_tokens": 0,
        "measured_cost_usd": "1.000000000",
        "estimated_off_transcript_cost_usd": "1.000000000",
        "basis": BASIS_LOCAL_MIX,
        "mix_source_dates": [],
    }
