"""Strict fixed-loopback forwarding for user-triggered usage account actions."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from coordharness.usage.dashboard_proxy import (
    USAGE_DASHBOARD_URL_ENV,
    validate_usage_dashboard_url,
)
from coordharness.usage.local_account_actions import LocalAccountActionService


SCHEMA_ID = "coord.usage-account-actions.v1"
_UPSTREAM_SCHEMA_ID = "coordharness.usage-account-actions.v1"
_UPSTREAM_SCHEMA_ENV = "COORD_ACCOUNT_ACTION_UPSTREAM_SCHEMA"
_ACTION_HEADER_ENV = "COORD_USAGE_ACTION_HEADER"
_UPSTREAM_ACTION_PATH = "/api/usage/v1/account-actions"
_MAX_RESPONSE_BYTES = 16 * 1024
_ALLOWED_ACTIONS = {
    "codex_login_start",
    "codex_login_cancel",
    "claude_connect_open",
    # Backward-compatible input only. Native clients use claude_connect_open.
    "claude_recovery_open",
    "profile_add",
    "profile_select",
    "profile_remove",
}
_SAFE_CODEX_STATES = {
    "idle",
    "starting",
    "waiting_browser",
    "completed",
    "failed",
    "cancelled",
    "expired",
    "unavailable",
}
_SAFE_CODEX_REASONS = {
    "login_expired",
    "login_start_failed",
    "login_failed",
    "login_interrupted",
}
_SAFE_CLAUDE_STATES = {
    "connected",
    "sign_in_required",
    "waiting_user",
    "manual_connect_required",
    "unavailable",
}
_SAFE_RESULTS = {
    "browser_opened",
    "login_already_active",
    "login_start_failed",
    "cancelled",
    "no_active_login",
    "connect_window_opened",
    "connect_already_connected",
    "connect_already_active",
    "connect_unavailable",
    "profile_add",
    "profile_select",
    "profile_remove",
    "unsupported_local_action",
}
_UPSTREAM_RESULT_MAP = {
    "browser_opened": "browser_opened",
    "login_already_active": "login_already_active",
    "login_start_failed": "login_start_failed",
    "cancelled": "cancelled",
    "no_active_login": "no_active_login",
    "connect_window_opened": "connect_window_opened",
    "connect_unavailable": "connect_unavailable",
    "claude_code_login_opened": "connect_window_opened",
    "claude_code_already_connected": "connect_already_connected",
    "claude_code_login_already_active": "connect_already_active",
    "claude_code_unavailable": "connect_unavailable",
    "claude_code_login_unavailable": "connect_unavailable",
}
_RESULT_ACTIONS = {
    "browser_opened": {"codex_login_start"},
    "login_already_active": {"codex_login_start"},
    "login_start_failed": {"codex_login_start"},
    "cancelled": {"codex_login_cancel"},
    "no_active_login": {"codex_login_cancel"},
    "connect_window_opened": {"claude_connect_open", "claude_recovery_open"},
    "connect_already_connected": {"claude_connect_open", "claude_recovery_open"},
    "connect_already_active": {"claude_connect_open", "claude_recovery_open"},
    "connect_unavailable": {"claude_connect_open", "claude_recovery_open"},
    "profile_add": {"profile_add"},
    "profile_select": {"profile_select"},
    "profile_remove": {"profile_remove"},
    "unsupported_local_action": {"codex_login_start", "claude_connect_open", "claude_recovery_open"},
}
_RESULT_STATUSES = {
    "browser_opened": {202},
    "login_already_active": {409},
    "login_start_failed": {503},
    "cancelled": {200},
    "no_active_login": {409},
    "connect_window_opened": {200, 202},
    "connect_already_connected": {200},
    "connect_already_active": {409},
    "connect_unavailable": {404, 503},
    "profile_add": {200},
    "profile_select": {200},
    "profile_remove": {200},
    "unsupported_local_action": {501},
}
Transport = Callable[[str, bytes | None], tuple[int, bytes]]



class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _reject_constant(_value: str) -> None:
    raise ValueError("nonstandard JSON constant")


def _unavailable_document() -> dict[str, Any]:
    return {
        "schema": SCHEMA_ID,
        "codex": {"state": "unavailable", "can_start": False, "can_cancel": False},
        "claude": {
            "state": "unavailable",
            "connect_available": False,
        },
    }


def _result_is_coherent(
    *,
    result: str,
    ok: bool,
    action: str,
    status: int,
    codex: dict[str, Any],
    claude: dict[str, Any],
) -> bool:
    if action not in _RESULT_ACTIONS[result] or status not in _RESULT_STATUSES[result]:
        return False
    if result in {"profile_add", "profile_select", "profile_remove"}:
        return ok
    if result == "unsupported_local_action":
        return not ok and codex["state"] in {"idle", "unavailable"} and claude["connect_available"] is False
    if result == "browser_opened":
        return (
            ok
            and codex["state"] == "waiting_browser"
            and not codex["can_start"]
            and codex["can_cancel"]
        )
    if result == "login_already_active":
        return (
            not ok
            and codex["state"] in {"starting", "waiting_browser"}
            and not codex["can_start"]
            and codex["can_cancel"]
        )
    if result == "login_start_failed":
        return (
            not ok
            and codex["state"] == "failed"
            and codex["can_start"]
            and not codex["can_cancel"]
        )
    if result == "cancelled":
        return (
            ok
            and codex["state"] == "cancelled"
            and codex["can_start"]
            and not codex["can_cancel"]
        )
    if result == "no_active_login":
        return (
            not ok
            and codex["state"] not in {"starting", "waiting_browser"}
            and codex["can_start"]
            and not codex["can_cancel"]
        )
    if result == "connect_window_opened":
        return (
            ok
            and claude["state"] != "unavailable"
            and claude["connect_available"]
            and claude.get("opened") is True
        )
    if result == "connect_already_connected":
        return (
            ok
            and claude["state"] == "connected"
            and claude["connect_available"]
            and claude.get("opened") is not True
        )
    if result == "connect_already_active":
        return (
            not ok
            and claude["state"] == "waiting_user"
            and claude["connect_available"]
            and claude.get("opened") is not True
        )
    return (
        result == "connect_unavailable"
        and not ok
        and claude.get("opened") is not True
    )


def _safe_document(
    raw: object,
    *,
    method: str,
    action: str | None,
    status: int,
    upstream_schema: str = _UPSTREAM_SCHEMA_ID,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != upstream_schema:
        raise ValueError("upstream schema rejected")
    codex_source = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    claude_source = raw.get("claude") if isinstance(raw.get("claude"), dict) else {}
    codex: dict[str, Any] = {
        "state": codex_source.get("state")
        if codex_source.get("state") in _SAFE_CODEX_STATES
        else "unavailable",
        "can_start": codex_source.get("can_start") is True,
        "can_cancel": codex_source.get("can_cancel") is True,
    }
    if codex_source.get("reason_code") in _SAFE_CODEX_REASONS:
        codex["reason_code"] = codex_source["reason_code"]
    claude: dict[str, Any] = {
        "state": claude_source.get("state")
        if claude_source.get("state") in _SAFE_CLAUDE_STATES
        else "unavailable",
        "connect_available": claude_source.get("connect_available") is True,
    }
    if isinstance(claude_source.get("opened"), bool):
        claude["opened"] = claude_source["opened"]
    document: dict[str, Any] = {"schema": SCHEMA_ID, "codex": codex, "claude": claude}
    profiles_source = raw.get("profiles")
    if isinstance(profiles_source, dict):
        profiles: dict[str, Any] = {}
        for provider in ("claude", "codex"):
            source = profiles_source.get(provider)
            if not isinstance(source, dict):
                continue
            active = source.get("active")
            rows = source.get("profiles")
            if not isinstance(active, str) or not isinstance(rows, list):
                continue
            safe_rows = []
            for row in rows[:13]:
                if not isinstance(row, dict):
                    continue
                profile_id, label = row.get("id"), row.get("label")
                if (
                    isinstance(profile_id, str)
                    and isinstance(label, str)
                    and len(profile_id) <= 32
                    and len(label) <= 40
                    and type(row.get("active")) is bool
                    and type(row.get("isolated")) is bool
                ):
                    safe_rows.append({
                        "id": profile_id,
                        "label": label,
                        "active": row["active"],
                        "isolated": row["isolated"],
                    })
            profiles[provider] = {"active": active, "profiles": safe_rows}
        if set(profiles) == {"claude", "codex"}:
            document["profiles"] = profiles

    ok_value = raw.get("ok")
    upstream_result = raw.get("result")
    result_value = (
        _UPSTREAM_RESULT_MAP.get(upstream_result, upstream_result)
        if isinstance(upstream_result, str)
        else upstream_result
    )
    if method == "GET":
        if action is not None or ok_value is not None or result_value is not None:
            raise ValueError("status response shape rejected")
        return document
    if (
        action is None
        or not isinstance(ok_value, bool)
        or not isinstance(result_value, str)
        or result_value not in _SAFE_RESULTS
        or not _result_is_coherent(
            result=result_value,
            ok=ok_value,
            action=action,
            status=status,
            codex=codex,
            claude=claude,
        )
    ):
        raise ValueError("action response coherence rejected")
    document["ok"] = ok_value
    document["result"] = result_value
    return document



class UsageAccountActionForwarder:
    """Forward a fixed action vocabulary to the configured local service."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = 4.0,
        dashboard_url: str | None = None,
        local_service: LocalAccountActionService | None = None,
    ) -> None:
        self._transport = transport or self._default_transport
        self._local_service = local_service or LocalAccountActionService()
        self._timeout_seconds = max(0.2, min(float(timeout_seconds), 10.0))
        self._upstream_schema = os.environ.get(_UPSTREAM_SCHEMA_ENV, _UPSTREAM_SCHEMA_ID)
        self._action_header = os.environ.get(_ACTION_HEADER_ENV, "X-Coordharness-Usage-Action")
        if not self._upstream_schema or not self._upstream_schema.replace(".", "").replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid account action upstream schema")
        if not self._action_header or not self._action_header.replace("-", "").isalnum():
            raise ValueError("invalid usage action header")
        configured = (
            dashboard_url
            if dashboard_url is not None
            else os.environ.get(USAGE_DASHBOARD_URL_ENV)
        )
        self._action_url = self._derive_action_url(configured) if configured else None
        if self._action_url is None:
            self._origin = None
        else:
            parts = urlsplit(self._action_url)
            self._origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    @staticmethod
    def _derive_action_url(dashboard_url: str) -> str:
        validated = validate_usage_dashboard_url(dashboard_url)
        parts = urlsplit(validated)
        return urlunsplit((parts.scheme, parts.netloc, _UPSTREAM_ACTION_PATH, "", ""))

    def status(self) -> tuple[int, dict[str, Any]]:
        return self._call("GET", None, action=None)

    def forward(self, request: str | dict[str, str]) -> tuple[int, dict[str, Any]]:
        action = request if isinstance(request, str) else request.get("action", "")
        if action not in _ALLOWED_ACTIONS:
            document = _unavailable_document()
            document["ok"] = False
            return 400, document
        document = {"action": action} if isinstance(request, str) else request
        body = json.dumps(document, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        return self._call("POST", body, action=action)

    def _call(
        self, method: str, body: bytes | None, *, action: str | None
    ) -> tuple[int, dict[str, Any]]:
        try:
            status, payload = self._transport(method, body)
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise ValueError("upstream response too large")
            decoded = json.loads(payload, parse_constant=_reject_constant)
            safe = _safe_document(
                decoded, method=method, action=action, status=status,
                upstream_schema=self._upstream_schema,
            )
        except (
            HTTPError,
            URLError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            document = _unavailable_document()
            document["ok"] = False
            return 503, document
        allowed_status = status if status in {200, 202, 400, 404, 409, 501, 503} else 502
        if allowed_status >= 400:
            safe["ok"] = False
        return allowed_status, safe

    def _default_transport(self, method: str, body: bytes | None) -> tuple[int, bytes]:
        if self._action_url is None:
            if method == "GET":
                status, document = 200, self._local_service.status()
            else:
                try:
                    request_value = json.loads(body or b"null", parse_constant=_reject_constant)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                    request_value = None
                status, document = self._local_service.dispatch(request_value)
            return status, json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Origin": self._origin,
        }
        if method == "POST":
            headers.update(
                {
                    "Content-Type": "application/json",
                    self._action_header: "v1",
                }
            )
        request = Request(self._action_url, data=body, headers=headers, method=method)
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                return int(response.status), payload
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ValueError("upstream redirect rejected") from exc
            payload = exc.read(_MAX_RESPONSE_BYTES + 1)
            return int(exc.code), payload
