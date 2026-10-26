"""Reject inconsistent snapshots without discarding retained receipt evidence."""
from copy import deepcopy
import pytest
from coordharness.usage.accounting_guard import accounting_errors, assert_accounting


def payload():
    return {"providers":{"codex":{"costs":{"api_rate_estimate":{"semantics":"self_computed_api_list_price_estimate","amount_nanos":12}},"history":{"all_time_total_tokens":100,"daily":[{"date":"2026-10-22","total_tokens":100,"api_rate_estimate_nanos":12}],"unplaced_usage":{"charged":False}}}}}


def test_conserved_payload_and_partial_unpriced_days():
    d=payload();assert not accounting_errors(d)
    d["providers"]["codex"]["history"]["daily"].append({"date":"2026-10-21","total_tokens":0,"api_rate_estimate_nanos":None});assert_accounting(d)


@pytest.mark.parametrize("mutation",["tokens","cost","dates","pending"])
def test_rejects_inconsistent_totals_and_never_mutates(mutation):
    d=payload();h=d["providers"]["codex"]["history"]
    if mutation=="tokens":h["all_time_total_tokens"]=101
    if mutation=="cost":h["daily"][0]["api_rate_estimate_nanos"]=13
    if mutation=="dates":h["daily"].append(dict(h["daily"][0]))
    if mutation=="pending":h["unplaced_usage"]["charged"]=True
    before=deepcopy(d)
    with pytest.raises(ValueError,match="usage_accounting_inconsistent"):assert_accounting(d)
    assert d==before


def test_native_and_unknown_histories_are_not_reinterpreted():
    d=payload();d["providers"]["codex"]["costs"]["api_rate_estimate"]["semantics"]="provider_native";d["providers"]["codex"]["history"]["all_time_total_tokens"]=999;assert not accounting_errors(d)
