"""Sanitized loopback proxy for provider accounts and routing policy."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from coordharness.usage.dashboard_proxy import USAGE_DASHBOARD_URL_ENV, validate_usage_dashboard_url
from coordharness.usage.local_provider_management import LocalProviderManagementService


SCHEMA_ID = "coord.provider-management.v1"
_UPSTREAM_SCHEMA_ID = "coordharness.provider-management.v1"
_UPSTREAM_SCHEMA_ENV = "COORD_PROVIDER_UPSTREAM_SCHEMA"
_ACTION_HEADER_ENV = "COORD_USAGE_ACTION_HEADER"
UPSTREAM_PATH = "/api/usage/v2/providers"
MAX_BODY_BYTES = 20 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_PROFILE = re.compile(r"^(?:default|p_[0-9a-f]{12})$")
_AUTH = {"cli", "api_key", "oauth", "local", "gateway", "none"}
_CAPABILITIES = {"chat", "code", "reasoning", "vision", "audio", "embeddings", "tools", "local"}
Transport = Callable[[str, bytes | None], tuple[int, bytes]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _text(value: object, maximum: int = 80) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError("unsafe_text")
    return value


def _number(value: object, low: int = 0, high: int = 10080) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError("unsafe_number")
    return value


def _safe_response(raw: object, *, upstream_schema: str = _UPSTREAM_SCHEMA_ID) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != upstream_schema:
        raise ValueError("invalid_schema")
    catalog = []
    for row in raw.get("catalog", [])[:48]:
        if not isinstance(row, dict) or not _ID.fullmatch(str(row.get("id") or "")):
            raise ValueError("invalid_catalog")
        modes = row.get("auth_modes")
        caps = row.get("capabilities")
        if not isinstance(modes, list) or any(mode not in _AUTH for mode in modes) or not isinstance(caps, list) or any(cap not in _CAPABILITIES for cap in caps):
            raise ValueError("invalid_catalog")
        catalog.append({"id": row["id"], "display_name": _text(row.get("display_name"), 48),
            "short_name": _text(row.get("short_name"), 48), "accent": _text(row.get("accent"), 7),
            "auth_modes": modes[:6], "default_auth_mode": row.get("default_auth_mode") if row.get("default_auth_mode") in modes else modes[0],
            "capabilities": caps[:8], "routable": row.get("routable") is True,
            "builtin": row.get("builtin") is True, "enabled": row.get("enabled") is True,
            "priority": _number(row.get("priority"), 0, 100)})
    profiles = {}
    source_profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    for provider in {row["id"] for row in catalog}:
        source = source_profiles.get(provider)
        if not isinstance(source, dict) or not _PROFILE.fullmatch(str(source.get("active") or "")):
            continue
        rows = []
        for row in source.get("profiles", [])[:13]:
            if not isinstance(row, dict) or not _PROFILE.fullmatch(str(row.get("id") or "")) or row.get("auth_mode") not in _AUTH:
                continue
            rows.append({"id": row["id"], "label": _text(row.get("label"), 40),
                "active": row.get("active") is True, "isolated": row.get("isolated") is True,
                "auth_mode": row["auth_mode"], "enabled": row.get("enabled") is not False,
                "priority": _number(row.get("priority"), 0, 100),
                "credential_set": row.get("credential_set") is True})
        profiles[provider] = {"active": source["active"], "profiles": rows}
    policy = raw.get("routing_policy")
    if not isinstance(policy, dict):
        raise ValueError("invalid_policy")
    clean_policy = {"version": 1, "mode": policy.get("mode") if policy.get("mode") in {"advisory", "automatic"} else "advisory",
        "min_session_remaining": _number(policy.get("min_session_remaining"), 0, 100),
        "min_weekly_remaining": _number(policy.get("min_weekly_remaining"), 0, 100),
        "min_runway_minutes": _number(policy.get("min_runway_minutes"), 0, 10080),
        "allow_metered_api": policy.get("allow_metered_api") is True,
        "prefer_subscription": policy.get("prefer_subscription") is True,
        "prefer_local": policy.get("prefer_local") is True,
        "required_capabilities": [cap for cap in policy.get("required_capabilities", []) if cap in _CAPABILITIES][:8]}
    recommendation = raw.get("recommendation") if isinstance(raw.get("recommendation"), dict) else {}
    candidates = []
    for row in recommendation.get("candidates", [])[:48]:
        if not isinstance(row, dict) or not _ID.fullmatch(str(row.get("provider") or "")):
            continue
        candidates.append({"provider": row["provider"], "account": row.get("account") if _PROFILE.fullmatch(str(row.get("account") or "")) else None,
            "display_name": _text(row.get("display_name"), 48), "eligible": row.get("eligible") is True,
            "score": int(row.get("score")) if isinstance(row.get("score"), (int, float)) else -1,
            "session_remaining": row.get("session_remaining") if isinstance(row.get("session_remaining"), (int, float)) else None,
            "weekly_remaining": row.get("weekly_remaining") if isinstance(row.get("weekly_remaining"), (int, float)) else None,
            "runway_minutes": row.get("runway_minutes") if isinstance(row.get("runway_minutes"), (int, float)) else None,
            "metered": row.get("metered") is True,
            "reasons": [_text(reason, 100) for reason in row.get("reasons", [])[:8]]})
    selected_provider = recommendation.get("selected", {}).get("provider") if isinstance(recommendation.get("selected"), dict) else None
    selected = next((row for row in candidates if row["provider"] == selected_provider and row["eligible"]), None)
    return {"schema": SCHEMA_ID, "catalog": catalog, "profiles": profiles, "routing_policy": clean_policy,
        "recommendation": {"schema": "coord.provider-routing.v1", "mode": clean_policy["mode"], "selected": selected,
            "candidates": candidates, "automatic_execution": False,
            "decision": _text(recommendation.get("decision", "usage_unavailable"), 80)}}


class ProviderManagementForwarder:
    def __init__(self, *, transport: Transport | None = None, timeout_seconds: float = 4.0,
                 dashboard_url: str | None = None, local_service: LocalProviderManagementService | None = None) -> None:
        self._transport = transport or self._default_transport
        self._local_service = local_service or LocalProviderManagementService()
        self._timeout = max(.2, min(float(timeout_seconds), 10.0))
        self._upstream_schema = os.environ.get(_UPSTREAM_SCHEMA_ENV, _UPSTREAM_SCHEMA_ID)
        self._action_header = os.environ.get(_ACTION_HEADER_ENV, "X-Coordharness-Usage-Action")
        if not self._upstream_schema or not self._upstream_schema.replace(".", "").replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid provider upstream schema")
        if not self._action_header or not self._action_header.replace("-", "").isalnum():
            raise ValueError("invalid usage action header")
        configured = dashboard_url if dashboard_url is not None else os.environ.get(USAGE_DASHBOARD_URL_ENV)
        if configured:
            parts = urlsplit(validate_usage_dashboard_url(configured))
            self._url = urlunsplit((parts.scheme, parts.netloc, UPSTREAM_PATH, "", ""))
            self._origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        else:
            self._url = self._origin = None

    def status(self) -> tuple[int, dict[str, Any]]:
        return self._call("GET", None)

    def forward(self, document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > MAX_BODY_BYTES:
            return 413, {"schema": SCHEMA_ID, "ok": False, "error": "body_too_large"}
        return self._call("POST", body)

    def _call(self, method: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            status, payload = self._transport(method, body)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            clean = _safe_response(json.loads(payload, parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("constant"))), upstream_schema=self._upstream_schema)
            clean["ok"] = status < 400
            return status if status in {200, 400, 403, 404, 409, 413, 501, 503} else 502, clean
        except (HTTPError, URLError, OSError, TimeoutError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return 503, {"schema": SCHEMA_ID, "ok": False, "catalog": [], "profiles": {}, "error": "upstream_unavailable"}

    def _default_transport(self, method: str, body: bytes | None) -> tuple[int, bytes]:
        if not self._url:
            if method == "GET":
                status, document = 200, self._local_service.status()
            else:
                try:
                    request = json.loads(body or b"null", parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("constant")))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                    request = None
                status, document = self._local_service.dispatch(request)
            return status, json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": self._origin}
        if method == "POST":
            headers.update({"Content-Type": "application/json", self._action_header: "v1"})
        request = Request(self._url, data=body, headers=headers, method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self._timeout) as response:
                return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ValueError("redirect_rejected") from exc
            return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)
