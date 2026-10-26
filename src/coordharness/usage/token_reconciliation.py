"""Reconcile displayed token totals with retained provider volume.

The provider anchor already estimates the cost of off-transcript volume. Its
tokens must travel into the same daily bars and summaries without assigning an
unknown model or component mix to an observed model. Local-day placement uses
the anchor's explicit allocation; only the UTC provider buckets are observed.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, MutableMapping

from .provider_anchor import AnchorPlan, ESTIMATE_APPLIED, ALLOCATION_BASIS

UNKNOWN_KEY = "provider-unattributed"
UNKNOWN_LABEL = "Provider usage (model unknown)"


def reconcile_provider_tokens(
    provider: MutableMapping[str, Any], plan: AnchorPlan, *, today: date
) -> None:
    """Apply the anchor's volume once, preserving measured tokens separately."""
    if plan.state != ESTIMATE_APPLIED:
        return
    history = provider["history"]
    daily = history.get("daily", [])
    seven = today - timedelta(days=6)
    week = today - timedelta(days=today.weekday())
    gaps = dict(plan.estimated_tokens_by_day)
    for row in daily:
        if not isinstance(row, dict):
            continue
        measured = row.get("measured_total_tokens", row.get("total_tokens", 0))
        row["measured_total_tokens"] = measured
        gap = gaps.get(row.get("date"), 0)
        row["unattributed_provider_tokens"] = gap
        row["total_tokens"] = int(measured or 0) + gap
        models = [m for m in row.get("model_breakdowns", []) if m.get("key") != UNKNOWN_KEY]
        if gap:
            models.append({
                "key": UNKNOWN_KEY,
                "label": UNKNOWN_LABEL,
                "total_tokens": gap,
                "api_rate_estimate_nanos": (
                    plan.estimated_nanos_by_day.get(row["date"])
                ),
                "provider_native_cost_nanos": None,
            })
        row["model_breakdowns"] = models

    def in_period(day: str, start: date) -> bool:
        return start.isoformat() <= day <= today.isoformat()

    additions = {
        "today_total_tokens": gaps.get(today.isoformat(), 0),
        "rolling_7d_total_tokens": sum(n for d, n in gaps.items() if in_period(d, seven)),
        "calendar_week_total_tokens": sum(n for d, n in gaps.items() if in_period(d, week)),
        "all_time_total_tokens": sum(gaps.values()),
    }
    for field, addition in additions.items():
        measured_key = "measured_" + field
        if measured_key not in history:
            history[measured_key] = history.get(field)
        base = history[measured_key]
        history[field] = None if base is None else int(base) + addition

    history["token_reconciliation"] = {
        "semantics": "local_observed_plus_provider_only_volume",
        "provider_only_tokens": sum(gaps.values()),
        "provider_day_basis": "utc",
        "allocation_basis": ALLOCATION_BASIS,
        "model_attribution": "provider_only_volume_unattributed",
        "pre_window_unreconciled_tokens": plan.pre_window_provider_tokens,
    }
    # Some producers provide lifetime model totals separately; add the same
    # unknown-volume row so those totals don't silently omit account usage.
    group = provider.get("breakdowns", {}).get("models")
    if isinstance(group, dict):
        items = [item for item in group.get("items", []) if item.get("key") != UNKNOWN_KEY]
        if sum(gaps.values()):
            items.append({
                "key": UNKNOWN_KEY,
                "label": UNKNOWN_LABEL,
                "total_tokens": sum(gaps.values()),
                "today_total_tokens": additions["today_total_tokens"],
                "rolling_7d_total_tokens": additions["rolling_7d_total_tokens"],
                "calendar_week_total_tokens": additions["calendar_week_total_tokens"],
                "api_rate_estimate_nanos": (
                    plan.estimated_cost_nanos if plan.estimated_nanos_by_day else None
                ),
                "provider_native_cost_nanos": None,
            })
        group["items"] = sorted(items, key=lambda item: -int(item.get("total_tokens") or 0))
