"""Side-effect-free recommendation over local account, quota, and history data."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def recommend_local_dashboard(
    *,
    usage: Mapping[str, Any],
    catalog: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    providers = usage.get("providers") if isinstance(usage.get("providers"), Mapping) else {}
    required = set(policy.get("required_capabilities") or [])
    candidates = []
    for definition in catalog:
        provider = definition.get("id")
        if provider not in {"claude", "codex"}:
            continue
        reasons: list[str] = []
        eligible = definition.get("enabled") is True and definition.get("routable") is True
        missing = required - set(definition.get("capabilities") or [])
        if missing:
            eligible = False
            reasons.append("missing " + ", ".join(sorted(missing)))
        live = providers.get(provider) if isinstance(providers, Mapping) else None
        account = (
            live.get("account")
            if isinstance(live, Mapping) and isinstance(live.get("account"), Mapping)
            else {}
        )
        if account.get("authenticated") is not True:
            eligible = False
            reasons.append("provider sign in not verified")
        profile_group = profiles.get(provider) if isinstance(profiles, Mapping) else None
        active_id = profile_group.get("active") if isinstance(profile_group, Mapping) else "default"
        profile_rows = profile_group.get("profiles") if isinstance(profile_group, Mapping) else []
        active = next(
            (
                row
                for row in profile_rows
                if isinstance(row, Mapping) and row.get("id") == active_id
            ),
            None,
        )
        if not isinstance(active, Mapping) or active.get("enabled") is False:
            eligible = False
            reasons.append("no enabled account profile")
        session = weekly = None
        windows = live.get("windows") if isinstance(live, Mapping) else None
        for window in windows if isinstance(windows, list) else []:
            if not isinstance(window, Mapping):
                continue
            remaining = window.get("remaining_percent")
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
                continue
            if window.get("kind") == "session":
                session = float(remaining)
            elif window.get("kind") == "weekly":
                weekly = float(remaining)
        if session is None and weekly is None:
            eligible = False
            reasons.append("current provider quota unavailable")
        if session is not None and session < policy.get("min_session_remaining", 15):
            eligible = False
            reasons.append("session reserve reached")
        if weekly is not None and weekly < policy.get("min_weekly_remaining", 20):
            eligible = False
            reasons.append("weekly reserve reached")
        history = (
            live.get("history")
            if isinstance(live, Mapping) and isinstance(live.get("history"), Mapping)
            else {}
        )
        local_tokens = history.get("rolling_7d_total_tokens")
        if isinstance(local_tokens, int) and not isinstance(local_tokens, bool):
            reasons.append(f"local seven day history {local_tokens} tokens with partial coverage")
        score = int(definition.get("priority") or 50)
        score += int(active.get("priority") or 50) if isinstance(active, Mapping) else 0
        score += int(
            (session if session is not None else weekly if weekly is not None else 0) * 0.4
        )
        score += int(
            (weekly if weekly is not None else session if session is not None else 0) * 0.5
        )
        if isinstance(local_tokens, int):
            score -= min(20, local_tokens // 1_000_000)
        if not eligible:
            score = -1
        candidates.append(
            {
                "provider": provider,
                "account": active_id,
                "display_name": definition.get("display_name") or provider.title(),
                "eligible": eligible,
                "score": score,
                "session_remaining": session,
                "weekly_remaining": weekly,
                "runway_minutes": None,
                "metered": False,
                "reasons": reasons or ["meets routing policy"],
            }
        )
    candidates.sort(key=lambda row: (not row["eligible"], -row["score"], row["provider"]))
    selected = next((row for row in candidates if row["eligible"]), None)
    return {
        "schema": "coordharness.provider-routing.v1",
        "mode": policy.get("mode", "advisory"),
        "selected": selected,
        "candidates": candidates,
        "automatic_execution": False,
        "decision": "recommendation_available" if selected else "usage_unavailable",
    }
