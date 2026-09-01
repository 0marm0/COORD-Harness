"""Bounded, read-only proxy for a configured usage dashboard.

The proxy is deliberately a transport boundary, not another usage model.  A
fresh, valid document is returned unchanged.  On a transient failure a bounded
last-good document may be returned with only the contract's refresh/error
fields updated to disclose that it is stale.  No token or cost value is ever
calculated here.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


USAGE_CONTRACT = "coordharness.usage-intelligence.v1"
USAGE_DASHBOARD_URL_ENV = "COORD_USAGE_DASHBOARD_URL"
UPSTREAM_CONTRACT_ENV = "COORD_USAGE_UPSTREAM_SCHEMA"
DEFAULT_TIMEOUT_SECONDS = 0.8
DEFAULT_LAST_GOOD_TTL_SECONDS = 300.0
MAX_RESPONSE_BYTES = 1_048_576
MAX_DAILY_ROWS = 400
MAX_WINDOWS = 32
MAX_QUOTA_GROUPS = 16
MAX_GROUP_WINDOWS = 32
MAX_RESET_CREDITS = 32
MAX_ACTIVE_SESSIONS = 50
MAX_BREAKDOWN_ITEMS = 50
MAX_ERRORS = 64
_PROVIDERS = frozenset({"claude", "codex"})
_LIVE_OBSERVATION_STATES = frozenset(
    {
        "fresh",
        "stale_last_good",
        "stale_last_good_no_current_windows",
        "quota_observation_expired",
        "quota_observation_unavailable",
    }
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:+()'%,;!?&$#=·—-]{0,239}$")
_SAFE_CURRENCY = re.compile(r"^(?:[A-Z]{3}|unknown)$")
_SAFE_TIME_ZONE = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_MAX_INTEGER = 9_223_372_036_854_775_807
_SENSITIVE_TEXT = re.compile(
    r"(?:bearer|password|credential|cookie|keychain|api[ _-]?key|token=|secret|private)",
    re.IGNORECASE,
)


class UsageDashboardError(RuntimeError):
    """A stable, presentation-safe proxy failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect outside the URL that passed loopback checks."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_usage_dashboard_url(url: str, *, allow_non_loopback: bool = False) -> str:
    """Validate the configured upstream without DNS or credential handling."""

    if not isinstance(url, str) or not url.strip() or url != url.strip():
        raise ValueError("usage dashboard URL must be a nonempty trimmed string")
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise ValueError("usage dashboard URL must use http")
    if parts.username is not None or parts.password is not None:
        raise ValueError("usage dashboard URL must not contain credentials")
    if parts.fragment:
        raise ValueError("usage dashboard URL must not contain a fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("usage dashboard URL must contain a host")
    try:
        # Accessing .port performs urllib's strict numeric/range validation.
        parts.port
    except ValueError as exc:
        raise ValueError("usage dashboard URL has an invalid port") from exc
    if not allow_non_loopback:
        if host != "localhost":
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError("usage dashboard URL must target loopback") from exc
            if not address.is_loopback:
                raise ValueError("usage dashboard URL must target loopback")
    return url


def _bounded_list(value: Any, *, field: str, limit: int) -> list[Any]:
    del field
    if not isinstance(value, list) or len(value) > limit:
        raise UsageDashboardError("invalid_contract")
    return value


def _safe_string(value: Any, *, pattern: re.Pattern[str], maximum: int = 80) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise UsageDashboardError("invalid_contract")
    if "@" in value or "/" in value or "\\" in value or ".." in value:
        raise UsageDashboardError("invalid_contract")
    if not pattern.fullmatch(value):
        raise UsageDashboardError("invalid_contract")
    return value


def _safe_text(value: Any, *, maximum: int = 240) -> str:
    text_value = _safe_string(value, pattern=_SAFE_TEXT, maximum=maximum)
    if _SENSITIVE_TEXT.search(text_value):
        raise UsageDashboardError("invalid_contract")
    return text_value


def _safe_number(value: Any, *, minimum: float = 0, maximum: float | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageDashboardError("invalid_contract")
    if (
        not math.isfinite(float(value))
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise UsageDashboardError("invalid_contract")
    return value


def _safe_int(value: Any, *, maximum: int = _MAX_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise UsageDashboardError("invalid_contract")
    return value


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not 20 <= len(value) <= 40 or "T" not in value:
        raise UsageDashboardError("invalid_contract")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise UsageDashboardError("invalid_contract") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UsageDashboardError("invalid_contract")
    return value


def _safe_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise UsageDashboardError("invalid_contract")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise UsageDashboardError("invalid_contract") from exc
    return value


def _optional_number(
    source: dict[str, Any], target: dict[str, Any], field: str, *, maximum: float
) -> None:
    if field in source:
        target[field] = (
            None if source[field] is None else _safe_number(source[field], maximum=maximum)
        )


def _optional_int(source: dict[str, Any], target: dict[str, Any], field: str) -> None:
    if field in source:
        target[field] = None if source[field] is None else _safe_int(source[field])


def _optional_timestamp(source: dict[str, Any], target: dict[str, Any], field: str) -> None:
    if field in source:
        target[field] = None if source[field] is None else _safe_timestamp(source[field])


def _sanitize_error_list(value: Any, *, field: str) -> list[dict[str, str]]:
    errors = _bounded_list(value, field=field, limit=MAX_ERRORS)
    clean: list[dict[str, str]] = []
    for error in errors:
        if not isinstance(error, dict):
            raise UsageDashboardError("invalid_contract")
        clean.append({"code": _safe_string(error.get("code"), pattern=_SAFE_TOKEN)})
    return clean


def _sanitize_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {"kind": _safe_string(value.get("kind"), pattern=_SAFE_TOKEN)}
    if not isinstance(value.get("canonical"), bool):
        raise UsageDashboardError("invalid_contract")
    clean["canonical"] = value["canonical"]
    if value.get("label") is not None:
        clean["label"] = _safe_text(value["label"], maximum=80)
    if value.get("warning") is not None:
        clean["warning"] = _safe_text(value["warning"])
    if value.get("schema") is not None:
        _safe_string(value["schema"], pattern=_SAFE_TOKEN)
        clean["schema"] = "coordharness.usage-ledger.v2"
    return clean


def _sanitize_account(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    authenticated = value.get("authenticated")
    if authenticated is not None and not isinstance(authenticated, bool):
        raise UsageDashboardError("invalid_contract")
    return {
        "status": _safe_string(value.get("status"), pattern=_SAFE_TOKEN),
        "plan": _safe_text(value.get("plan"), maximum=80),
        "authenticated": authenticated,
    }


def _sanitize_quota_pace(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    state = _safe_string(value.get("state"), pattern=_SAFE_TOKEN)
    if state not in {"reserve", "deficit", "on_pace"}:
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {
        "state": state,
        "delta_percent": _safe_number(value.get("delta_percent"), maximum=100),
        "expected_used_percent": _safe_number(value.get("expected_used_percent"), maximum=100),
    }
    if not isinstance(value.get("will_last_to_reset"), bool):
        raise UsageDashboardError("invalid_contract")
    clean["will_last_to_reset"] = value["will_last_to_reset"]
    _optional_number(value, clean, "seconds_to_exhaustion", maximum=31_536_000)
    if value.get("advisory") is not None:
        if not isinstance(value["advisory"], bool):
            raise UsageDashboardError("invalid_contract")
        clean["advisory"] = value["advisory"]
    for field in ("basis", "source"):
        if value.get(field) is not None:
            clean[field] = _safe_string(value[field], pattern=_SAFE_TOKEN)
    if value.get("marker_remaining_percent") is not None:
        clean["marker_remaining_percent"] = _safe_number(
            value["marker_remaining_percent"], maximum=100
        )
    if value.get("marker_kind") is not None:
        marker_kind = _safe_string(value["marker_kind"], pattern=_SAFE_TOKEN)
        if marker_kind not in {"reserve", "on_pace", "deficit"}:
            raise UsageDashboardError("invalid_contract")
        clean["marker_kind"] = marker_kind
    return clean


def _sanitize_quota_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {}
    if value.get("kind") is not None:
        clean["kind"] = _safe_string(value["kind"], pattern=_SAFE_TOKEN)
    if value.get("name") is not None:
        clean["name"] = _safe_text(value["name"], maximum=80)
    for field, maximum in (
        ("window_minutes", 525_600),
        ("used_percent", 100),
        ("remaining_percent", 100),
        ("countdown_seconds", 31_536_000),
    ):
        _optional_number(value, clean, field, maximum=maximum)
    _optional_timestamp(value, clean, "resets_at")
    if value.get("pace") is not None:
        clean["pace"] = _sanitize_quota_pace(value["pace"])
    return clean


def _sanitize_runout(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {}
    if value.get("kind") is not None:
        clean["kind"] = _safe_string(value["kind"], pattern=_SAFE_TOKEN)
    if value.get("advisory") is not None:
        if not isinstance(value["advisory"], bool):
            raise UsageDashboardError("invalid_contract")
        clean["advisory"] = value["advisory"]
    _optional_timestamp(value, clean, "estimated_exhausts_at")
    _optional_number(value, clean, "seconds_to_exhaustion", maximum=31_536_000)
    if value.get("basis") is not None:
        clean["basis"] = _safe_text(value["basis"], maximum=80)
    return clean


def _sanitize_quota_groups(value: Any) -> list[dict[str, Any]]:
    groups = _bounded_list(value, field="quota_groups", limit=MAX_QUOTA_GROUPS)
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise UsageDashboardError("invalid_contract")
        key = _safe_string(group.get("key"), pattern=_SAFE_TOKEN)
        if key in seen:
            raise UsageDashboardError("invalid_contract")
        seen.add(key)
        windows = _bounded_list(
            group.get("windows"), field="quota_groups.windows", limit=MAX_GROUP_WINDOWS
        )
        clean.append(
            {
                "key": key,
                "label": _safe_text(group.get("label"), maximum=80),
                "semantics": _safe_string(group.get("semantics"), pattern=_SAFE_TOKEN),
                "windows": [_sanitize_quota_window(window) for window in windows],
                "runout": _sanitize_runout(group.get("runout")),
            }
        )
    return clean


def _sanitize_reset_credits(value: Any) -> list[dict[str, Any]]:
    rows = _bounded_list(value, field="reset_credits", limit=MAX_RESET_CREDITS)
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise UsageDashboardError("invalid_contract")
        item: dict[str, Any] = {"status": _safe_string(row.get("status"), pattern=_SAFE_TOKEN)}
        _optional_int(row, item, "count")
        _optional_timestamp(row, item, "expires_at")
        if "semantics" in row:
            item["semantics"] = _safe_string(row["semantics"], pattern=_SAFE_TOKEN)
        clean.append(item)
    return clean


_DAILY_INTEGER_FIELDS = (
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_5m_tokens",
    "cache_create_1h_tokens",
    "cache_create_other_tokens",
    "provider_native_cost_nanos",
    "api_rate_estimate_nanos",
)
_HISTORY_INTEGER_FIELDS = (
    "today_total_tokens",
    "rolling_7d_total_tokens",
    "calendar_week_total_tokens",
    "all_time_total_tokens",
)


def _sanitize_daily(value: Any) -> list[dict[str, Any]]:
    rows = _bounded_list(value, field="history.daily", limit=MAX_DAILY_ROWS)
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise UsageDashboardError("invalid_contract")
        item: dict[str, Any] = {"date": _safe_date(row.get("date"))}
        for field in _DAILY_INTEGER_FIELDS:
            _optional_int(row, item, field)
        if item.get("total_tokens") is None:
            raise UsageDashboardError("invalid_contract")
        if "model_breakdowns" in row:
            models = _bounded_list(
                row["model_breakdowns"],
                field="history.daily.model_breakdowns",
                limit=MAX_BREAKDOWN_ITEMS,
            )
            item["model_breakdowns"] = [_sanitize_daily_model_breakdown(model) for model in models]
        clean.append(item)
    return clean


def _sanitize_daily_model_breakdown(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {
        "key": _safe_string(value.get("key"), pattern=_SAFE_TOKEN),
        "label": _safe_text(value.get("label"), maximum=80),
    }
    for field in _DAILY_INTEGER_FIELDS:
        _optional_int(value, clean, field)
    if clean.get("total_tokens") is None:
        raise UsageDashboardError("invalid_contract")
    return clean


def _sanitize_history(value: Any, *, allow_extensions: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {"daily": _sanitize_daily(value.get("daily"))}
    for field in _HISTORY_INTEGER_FIELDS:
        _optional_int(value, clean, field)
    if value.get("semantics") is not None:
        clean["semantics"] = _safe_string(value["semantics"], pattern=_SAFE_TOKEN)
    if allow_extensions and value.get("ever_observed_envelope") is not None:
        envelope = value["ever_observed_envelope"]
        if not isinstance(envelope, dict):
            raise UsageDashboardError("invalid_contract")
        clean["ever_observed_envelope"] = {"total_tokens": _safe_int(envelope.get("total_tokens"))}
    if allow_extensions and value.get("provider_reported_account") is not None:
        clean["provider_reported_account"] = _sanitize_history(
            value["provider_reported_account"], allow_extensions=False
        )
    return clean


def _sanitize_cost_component(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {}
    _optional_int(value, clean, "amount_nanos")
    if "currency" in value:
        clean["currency"] = (
            None
            if value["currency"] is None
            else _safe_string(value["currency"], pattern=_SAFE_CURRENCY, maximum=7)
        )
    if value.get("semantics") is not None:
        clean["semantics"] = _safe_string(value["semantics"], pattern=_SAFE_TOKEN)
    if value.get("source") is not None:
        clean["source"] = _sanitize_source(value["source"])
    _optional_timestamp(value, clean, "observed_at")
    for field in ("coverage_start", "coverage_end"):
        if value.get(field) is not None:
            clean[field] = _safe_date(value[field])
    if value.get("by_currency") is not None:
        currencies = value["by_currency"]
        if not isinstance(currencies, dict) or len(currencies) > 16:
            raise UsageDashboardError("invalid_contract")
        clean["by_currency"] = {
            _safe_string(currency, pattern=_SAFE_CURRENCY, maximum=7): _safe_int(amount)
            for currency, amount in currencies.items()
        }
    return clean


def _sanitize_costs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    return {
        field: _sanitize_cost_component(value[field])
        for field in ("provider_billed", "provider_native", "api_rate_estimate")
        if value.get(field) is not None
    }


_BREAKDOWN_INTEGER_FIELDS = (
    "total_tokens",
    "today_total_tokens",
    "rolling_7d_total_tokens",
    "calendar_week_total_tokens",
    "provider_native_cost_nanos",
    "api_rate_estimate_nanos",
)


def _sanitize_breakdown_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {
        "key": _safe_string(value.get("key"), pattern=_SAFE_TOKEN),
        "label": _safe_text(value.get("label"), maximum=80),
    }
    for field in _BREAKDOWN_INTEGER_FIELDS:
        _optional_int(value, clean, field)
    if "top_model" in value:
        clean["top_model"] = (
            None if value["top_model"] is None else _safe_text(value["top_model"], maximum=80)
        )
    return clean


def _sanitize_breakdown_group(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    items = _bounded_list(value.get("items", []), field=field, limit=MAX_BREAKDOWN_ITEMS)
    if not isinstance(value.get("canonical"), bool):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {
        "status": _safe_string(value.get("status"), pattern=_SAFE_TOKEN),
        "semantics": (
            None
            if value.get("semantics") is None
            else _safe_string(value["semantics"], pattern=_SAFE_TOKEN)
        ),
        "canonical": value["canonical"],
        "coverage_start": (
            None if value.get("coverage_start") is None else _safe_date(value["coverage_start"])
        ),
        "coverage_end": (
            None if value.get("coverage_end") is None else _safe_date(value["coverage_end"])
        ),
        "observed_at": (
            None if value.get("observed_at") is None else _safe_timestamp(value["observed_at"])
        ),
        "items": [_sanitize_breakdown_item(item) for item in items],
        "omitted_count": _safe_int(value.get("omitted_count", 0), maximum=1_000_000),
    }
    if value.get("reason_code") is not None:
        clean["reason_code"] = _safe_string(value["reason_code"], pattern=_SAFE_TOKEN)
    if value.get("warning") is not None:
        clean["warning"] = _safe_text(value["warning"])
    return clean


def _sanitize_breakdowns(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    return {
        field: _sanitize_breakdown_group(value[field], field=f"breakdowns.{field}")
        for field in ("models", "projects")
        if value.get(field) is not None
    }


def _sanitize_active_sessions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    providers = _bounded_list(value.get("providers"), field="active_sessions.providers", limit=2)
    if any(provider not in _PROVIDERS for provider in providers) or len(set(providers)) != len(
        providers
    ):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {
        "status": _safe_string(value.get("status"), pattern=_SAFE_TOKEN),
        "count": (
            None
            if value.get("count") is None
            else _safe_int(value["count"], maximum=MAX_ACTIVE_SESSIONS)
        ),
        "providers": list(providers),
    }
    if "items" in value:
        items = _bounded_list(
            value["items"], field="active_sessions.items", limit=MAX_ACTIVE_SESSIONS
        )
        safe_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("provider") not in _PROVIDERS:
                raise UsageDashboardError("invalid_contract")
            safe_item: dict[str, Any] = {
                "provider": item["provider"],
                "state": _safe_string(item.get("state"), pattern=_SAFE_TOKEN),
            }
            _optional_timestamp(item, safe_item, "started_at")
            _optional_timestamp(item, safe_item, "last_activity_at")
            for field in ("duration_seconds", "idle_seconds"):
                _optional_number(item, safe_item, field, maximum=31_536_000)
            safe_items.append(safe_item)
        clean["items"] = safe_items
    return clean


def _sanitize_provider(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    windows = _bounded_list(value.get("windows"), field="windows", limit=MAX_WINDOWS)
    clean: dict[str, Any] = {
        "source": _sanitize_source(value.get("source")),
        "account": _sanitize_account(value.get("account")),
        "windows": [_sanitize_quota_window(window) for window in windows],
        "reset_credits": _sanitize_reset_credits(value.get("reset_credits")),
        "runout": _sanitize_runout(value.get("runout")),
        "history": _sanitize_history(value.get("history")),
        "costs": _sanitize_costs(value.get("costs")),
        "active_sessions": _sanitize_active_sessions(value.get("active_sessions")),
        "errors": _sanitize_error_list(value.get("errors"), field="provider.errors"),
    }
    if value.get("account_source") is not None:
        clean["account_source"] = _sanitize_source(value["account_source"])
    if value.get("quota_source") is not None:
        clean["quota_source"] = _sanitize_source(value["quota_source"])
    if value.get("quota_groups") is not None:
        clean["quota_groups"] = _sanitize_quota_groups(value["quota_groups"])
    if value.get("breakdowns") is not None:
        clean["breakdowns"] = _sanitize_breakdowns(value["breakdowns"])
    if value.get("live_observed_at") is not None:
        clean["live_observed_at"] = _safe_timestamp(value["live_observed_at"])
    if value.get("live_snapshot_source") is not None:
        clean["live_snapshot_source"] = _safe_string(
            value["live_snapshot_source"], pattern=_SAFE_TOKEN
        )
    if value.get("live_observation_state") is not None:
        observation_state = _safe_string(value["live_observation_state"], pattern=_SAFE_TOKEN)
        if observation_state not in _LIVE_OBSERVATION_STATES:
            raise UsageDashboardError("invalid_contract")
        clean["live_observation_state"] = observation_state
    return clean


def _sanitize_refresh(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    clean: dict[str, Any] = {
        "state": _safe_string(value.get("state"), pattern=_SAFE_TOKEN),
        "generated_at": _safe_timestamp(value.get("generated_at")),
    }
    for field in ("error", "error_code"):
        if value.get(field) is not None:
            clean[field] = _safe_string(value[field], pattern=_SAFE_TOKEN)
    if value.get("last_good_generated_at") is not None:
        clean["last_good_generated_at"] = _safe_timestamp(value["last_good_generated_at"])
    return clean


def _safe_time_zone(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or ".." in value
        or not _SAFE_TIME_ZONE.fullmatch(value)
    ):
        raise UsageDashboardError("invalid_contract")
    return value


def _sanitize_calendar(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageDashboardError("invalid_contract")
    week_starts_on = _safe_string(value.get("week_starts_on"), pattern=_SAFE_TOKEN)
    if week_starts_on != "monday":
        raise UsageDashboardError("invalid_contract")
    return {
        "time_zone": _safe_time_zone(value.get("time_zone")),
        "local_date": _safe_date(value.get("local_date")),
        "week_start_date": _safe_date(value.get("week_start_date")),
        "week_starts_on": week_starts_on,
        "semantics": _safe_string(value.get("semantics"), pattern=_SAFE_TOKEN),
    }


def validate_usage_dashboard(
    payload: Any, *, expected_contract: str = USAGE_CONTRACT
) -> dict[str, Any]:
    """Rebuild the public document from a strict presentation-field allowlist."""

    if not isinstance(payload, dict) or payload.get("schema") != expected_contract:
        raise UsageDashboardError("invalid_contract")
    providers = payload.get("providers")
    if not isinstance(providers, dict) or not set(providers).issubset(_PROVIDERS):
        raise UsageDashboardError("invalid_contract")
    return {
        "schema": USAGE_CONTRACT,
        "generated_at": _safe_timestamp(payload.get("generated_at")),
        "stale_after": (
            None if payload.get("stale_after") is None else _safe_timestamp(payload["stale_after"])
        ),
        "refresh": _sanitize_refresh(payload.get("refresh")),
        **(
            {"calendar": _sanitize_calendar(payload["calendar"])}
            if payload.get("calendar") is not None
            else {}
        ),
        "providers": {
            provider_key: _sanitize_provider(provider)
            for provider_key, provider in providers.items()
        },
        "errors": _sanitize_error_list(payload.get("errors"), field="errors"),
    }


def _default_open(request: Request, timeout: float):
    opener = build_opener(_NoRedirect())
    return opener.open(request, timeout=timeout)


class UsageDashboardProxy:
    """Fetch and cache the canonical usage document without any local writes."""

    def __init__(
        self,
        *,
        url: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        last_good_ttl_seconds: float = DEFAULT_LAST_GOOD_TTL_SECONDS,
        opener: Callable[[Request, float], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = _utc_now,
        local_provider: Callable[[], Any] | None = None,
        allow_non_loopback: bool = False,
    ):
        configured = url if url is not None else os.environ.get(USAGE_DASHBOARD_URL_ENV)
        upstream_contract = os.environ.get(UPSTREAM_CONTRACT_ENV, USAGE_CONTRACT)
        if not _SAFE_TOKEN.fullmatch(upstream_contract):
            raise ValueError("invalid usage upstream schema")
        self.upstream_contract = upstream_contract
        self.url = (
            validate_usage_dashboard_url(configured, allow_non_loopback=allow_non_loopback)
            if configured
            else None
        )
        if not 0 < float(timeout_seconds) <= 5:
            raise ValueError("usage dashboard timeout must be in (0, 5] seconds")
        if not 0 <= float(last_good_ttl_seconds) <= 3600:
            raise ValueError("usage dashboard last-good TTL must be in [0, 3600] seconds")
        self.timeout_seconds = float(timeout_seconds)
        self.last_good_ttl_seconds = float(last_good_ttl_seconds)
        self._opener = opener or _default_open
        self._monotonic = monotonic
        self._now = now
        if local_provider is None:
            from coordharness.usage.local_service import LocalUsageService

            local_provider = LocalUsageService().dashboard
        self._local_provider = local_provider
        self._lock = threading.Lock()
        self._last_good: dict[str, Any] | None = None
        self._last_good_at: float | None = None

    def _fetch(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if self.url is None:
            try:
                payload = (
                    self._local_provider(force_refresh=True)
                    if force_refresh
                    else self._local_provider()
                )
                return validate_usage_dashboard(payload)
            except UsageDashboardError:
                raise
            except Exception as exc:
                raise UsageDashboardError("local_service_unavailable") from exc
        request = Request(
            self.url,
            headers={
                "Accept": "application/json",
                "User-Agent": "coord-usage-dashboard-proxy/1",
            },
            method="GET",
        )
        try:
            response = self._opener(request, self.timeout_seconds)
            with response:
                status = getattr(response, "status", None) or response.getcode()
                if int(status) != 200:
                    raise UsageDashboardError("upstream_http_error")
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if int(length) > MAX_RESPONSE_BYTES:
                            raise UsageDashboardError("response_too_large")
                    except ValueError as exc:
                        raise UsageDashboardError("malformed_response") from exc
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except UsageDashboardError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise UsageDashboardError("upstream_timeout") from exc
        except HTTPError as exc:
            raise UsageDashboardError("upstream_http_error") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise UsageDashboardError("upstream_timeout") from exc
            raise UsageDashboardError("upstream_unavailable") from exc
        except OSError as exc:
            raise UsageDashboardError("upstream_unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UsageDashboardError("response_too_large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise UsageDashboardError("malformed_json") from exc
        return validate_usage_dashboard(payload, expected_contract=self.upstream_contract)

    def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a fresh document, bounded last-good fallback, or error envelope."""

        try:
            payload = self._fetch(force_refresh=force_refresh)
        except UsageDashboardError as exc:
            return self._fallback(exc.code)
        with self._lock:
            self._last_good = deepcopy(payload)
            self._last_good_at = self._monotonic()
        # A successful proxy read is semantically transparent.
        return deepcopy(payload)

    def _fallback(self, code: str) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            cached = deepcopy(self._last_good)
            cached_at = self._last_good_at
        if (
            cached is not None
            and cached_at is not None
            and self._monotonic() - cached_at <= self.last_good_ttl_seconds
        ):
            refresh = dict(cached.get("refresh") or {})
            refresh.update(
                {
                    "state": "stale",
                    "generated_at": now,
                    "error_code": code,
                    "last_good_generated_at": cached.get("generated_at"),
                }
            )
            cached["refresh"] = refresh
            errors = list(cached.get("errors") or [])
            errors.append({"code": f"coord_proxy_{code}"})
            cached["errors"] = errors[-MAX_ERRORS:]
            return cached
        return {
            "schema": USAGE_CONTRACT,
            "generated_at": now,
            "stale_after": None,
            "refresh": {
                "state": "error",
                "generated_at": now,
                "error_code": code,
            },
            "providers": {},
            "errors": [{"code": f"coord_proxy_{code}"}],
        }


__all__ = [
    "USAGE_CONTRACT",
    "USAGE_DASHBOARD_URL_ENV",
    "UPSTREAM_CONTRACT_ENV",
    "UsageDashboardError",
    "UsageDashboardProxy",
    "validate_usage_dashboard",
    "validate_usage_dashboard_url",
]
