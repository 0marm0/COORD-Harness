"""Conservation checks before a self-computed usage snapshot is published.

Only explicit self-computed totals are checked. Provider/native or missing
histories retain their existing semantics. Errors carry no private source data.
"""
from collections.abc import Mapping


def accounting_errors(document):
    errors = []
    providers = document.get("providers", {})
    if not isinstance(providers, Mapping):
        return ()
    for provider in ("claude", "codex"):
        item = providers.get(provider, {})
        if not isinstance(item, Mapping):
            continue
        history = item.get("history", {})
        cost = item.get("costs", {}).get("api_rate_estimate", {})
        if not isinstance(history, Mapping) or not isinstance(cost, Mapping):
            continue
        if cost.get("semantics") != "self_computed_api_list_price_estimate":
            continue
        daily = history.get("daily")
        total = history.get("all_time_total_tokens")
        if type(total) is not int or not isinstance(daily, list):
            continue
        rows_valid = all(isinstance(row, Mapping) and type(row.get("total_tokens")) is int and row["total_tokens"] >= 0 for row in daily)
        if not rows_valid:
            errors.append(provider + "_daily_tokens_invalid")
            continue
        dates = [row.get("date") for row in daily]
        if any(not isinstance(day, str) for day in dates) or len(set(dates)) != len(dates):
            errors.append(provider + "_daily_dates_invalid")
        omitted_tokens = omitted_cost = 0
        omitted = history.get("omitted_daily_totals")
        if omitted is not None:
            # The local builder validates the complete history before bounding
            # the public chart. Retain only its omitted prefix's numeric sums;
            # the public proxy exposes only these bounded numeric totals.
            valid = (
                isinstance(omitted, Mapping)
                and type(omitted.get("row_count")) is int and omitted["row_count"] > 0
                and type(omitted.get("total_tokens")) is int and omitted["total_tokens"] >= 0
                and type(omitted.get("api_rate_estimate_nanos")) is int
                and omitted["api_rate_estimate_nanos"] >= 0
                and isinstance(omitted.get("last_date"), str)
                and bool(daily)
                and all(isinstance(day, str) and day > omitted["last_date"] for day in dates)
            )
            if not valid:
                errors.append(provider + "_omitted_daily_invalid")
                continue
            omitted_tokens = omitted["total_tokens"]
            omitted_cost = omitted["api_rate_estimate_nanos"]
        if omitted_tokens + sum(row["total_tokens"] for row in daily) != total:
            errors.append(provider + "_daily_tokens_do_not_conserve")
        amount = cost.get("amount_nanos")
        if type(amount) is int:
            values = [row.get("api_rate_estimate_nanos") for row in daily]
            if any(value is not None and type(value) is not int for value in values) or omitted_cost + sum(value or 0 for value in values) != amount:
                errors.append(provider + "_daily_cost_does_not_conserve")
        pending = history.get("unplaced_usage")
        if isinstance(pending, Mapping) and pending.get("charged") is not False:
            errors.append(provider + "_unplaced_usage_charged")
    return tuple(errors)


def assert_accounting(document):
    errors = accounting_errors(document)
    if errors:
        raise ValueError("usage_accounting_inconsistent:" + ",".join(errors))


def bound_history(provider, item, *, limit=400):
    """Validate full conservation, then retain a bounded omitted-prefix proof."""
    assert_accounting({"providers": {provider: item}})
    history = item["history"]
    daily = history["daily"]
    if len(daily) > limit:
        omitted = daily[:-limit]
        history["omitted_daily_totals"] = {
            "row_count": len(omitted),
            "total_tokens": sum(row["total_tokens"] for row in omitted),
            "api_rate_estimate_nanos": sum(row.get("api_rate_estimate_nanos") or 0 for row in omitted),
            "last_date": omitted[-1]["date"],
        }
        history["daily"] = daily[-limit:]
