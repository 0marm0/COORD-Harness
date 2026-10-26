"""UTC boundaries and provenance for date-effective token tariffs."""
from datetime import datetime

import pytest

from coordharness.usage.historical_tariffs import select_historical_rate
from coordharness.usage.pricing import load_rate_card


def slot(text):
    return int(datetime.fromisoformat(text).timestamp())


@pytest.mark.parametrize("model,boundary,old,new", [
    ("gpt-5.6-sol", "2026-08-21", (5,30,.5), (4,20,.4)),
    ("gpt-5.6-terra", "2026-07-30", (2.5,15,.25), (2,12,.2)),
    ("gpt-5.6-luna", "2026-07-30", (1,6,.1), (.2,1.2,.02)),
])
def test_tariff_changes_at_utc_midnight(model,boundary,old,new):
    card=load_rate_card();moment=slot(boundary+"T00:00:00+00:00")
    for when,expected in ((moment-1,old),(moment,new)):
        rate,proof=select_historical_rate(card,model,when)
        assert (rate.input,rate.output,rate.cache_read)==tuple(round(x*1_000_000_000) for x in expected)
        assert proof['historical_effective_dates_verified'] is True
    assert card.rates[model].input==round(new[0]*1_000_000_000)


def test_slot_day_does_not_use_local_calendar_day():
    # Aug20 in New York, but already the Aug21 promotional interval in UTC.
    rate,proof=select_historical_rate(load_rate_card(),'gpt-5.6-sol',slot('2026-08-20T20:01:00-04:00'))
    assert rate.input==4_000_000_000
    assert proof['usage_date_utc']=='2026-08-21'


def test_pre_public_dates_preserve_disclosed_numeric_assumption():
    rate,proof=select_historical_rate(load_rate_card(),'gpt-5.6-sol',usage_date='2026-07-01')
    assert rate.input==5_000_000_000
    assert proof['historical_effective_dates_verified'] is False
    assert 'pre_public_release_model_date_rate_assumed' in proof['assumptions']


def test_old_cache_and_ttl_authority_is_never_invented():
    rate,proof=select_historical_rate(load_rate_card(),'gpt-5.6-sol',usage_date='2026-08-20')
    assert rate.cache_read==500_000_000
    assert rate.cache_write_5m==6_250_000_000
    assert proof['historical_components_verified'] is False
    assert proof['component_authority']['cache_read']=='historical_schedule_assumption'


def test_unknown_or_undated_models_do_not_infer_alias_or_history():
    card=load_rate_card()
    rate,proof=select_historical_rate(card,'codex-auto-review',usage_date='2026-08-20')
    assert rate is None and proof['reason']=='exact_model_rate_unavailable'
    rate,proof=select_historical_rate(card,'gpt-5.6-sol')
    assert rate is card.rates['gpt-5.6-sol']
    assert proof['basis']=='current_rate_api_equivalent_estimate'
    rate,proof=select_historical_rate(card,'gpt-6.1-sol',usage_date='2026-10-20')
    assert rate is card.rates['gpt-6.1-sol']
    assert proof['historical_effective_dates_verified'] is False


@pytest.mark.parametrize('kwargs', [
    {'usage_slot':True}, {'usage_slot':-1}, {'usage_date':'20260730'},
    {'usage_date':'2026-07-31','usage_slot':slot('2026-07-30T23:59:00+00:00')},
])
def test_invalid_or_conflicting_dates_fail_closed(kwargs):
    with pytest.raises(ValueError):select_historical_rate(load_rate_card(),'gpt-5.6-sol',**kwargs)


def test_future_rates_are_scenarios_not_verified_history():
    _,proof=select_historical_rate(load_rate_card(),'gpt-5.6-sol',usage_date='2027-01-01')
    assert proof['historical_effective_dates_verified'] is False
    assert 'future_tariff_interval_not_verified' in proof['assumptions']
