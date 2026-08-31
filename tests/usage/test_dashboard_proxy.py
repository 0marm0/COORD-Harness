from __future__ import annotations

from copy import deepcopy
import io
import json
import socket

import pytest

from coordharness.usage.dashboard_proxy import (
    USAGE_CONTRACT,
    UsageDashboardProxy,
    validate_usage_dashboard_url,
)

LOOPBACK_URL = "http://127.0.0.1:7870/api/usage/v1"


def _payload() -> dict:
    return {
        "schema": USAGE_CONTRACT,
        "generated_at": "2026-08-26T12:00:00Z",
        "stale_after": "2026-08-26T12:05:00Z",
        "refresh": {"state": "fresh", "generated_at": "2026-08-26T12:00:00Z"},
        "calendar": {
            "time_zone": "America/New_York",
            "local_date": "2026-08-26",
            "week_start_date": "2026-08-24",
            "week_starts_on": "monday",
            "semantics": "configured_local_calendar",
        },
        "providers": {
            "claude": {
                "source": {
                    "kind": "legacy_high_water",
                    "canonical": False,
                    "warning": "ever-observed custody envelope",
                },
                "quota_source": {
                    "kind": "codexbar_widget_snapshot",
                    "canonical": False,
                    "label": "Codex Bar widget quota snapshot · stale last-good",
                    "warning": "Stale last-good quota observation retained; values may lag until Codex Bar refreshes.",
                },
                "account": {"status": "authenticated", "plan": "max", "authenticated": True},
                "account_source": {"kind": "claude_code_auth_status", "canonical": True},
                "windows": [
                    {
                        "kind": "session",
                        "name": "5 hour",
                        "window_minutes": 300,
                        "used_percent": 25,
                        "remaining_percent": 75,
                        "resets_at": "2026-08-26T15:00:00Z",
                        "countdown_seconds": 10_800,
                    }
                ],
                "reset_credits": [],
                "runout": {
                    "kind": "current_window_linear",
                    "advisory": True,
                    "estimated_exhausts_at": None,
                    "seconds_to_exhaustion": None,
                    "basis": "current window",
                },
                "history": {
                    "daily": [{"date": "2026-08-26", "total_tokens": 123}],
                    "rolling_7d_total_tokens": 456,
                    "calendar_week_total_tokens": 456,
                    "all_time_total_tokens": None,
                    "semantics": "canonical_correctable",
                    "ever_observed_envelope": {"total_tokens": 789},
                },
                "costs": {
                    "provider_billed": {
                        "amount_nanos": None,
                        "currency": None,
                        "semantics": "unknown",
                    },
                    "provider_native": {"amount_nanos": None, "semantics": "unknown"},
                    "api_rate_estimate": {
                        "amount_nanos": 1000,
                        "semantics": "api_rate_estimate",
                    },
                },
                "active_sessions": {"status": "available", "count": 2, "providers": ["claude"]},
                "live_observation_state": "stale_last_good",
                "errors": [],
            }
        },
        "errors": [],
    }


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200, content_length: str | None = None):
        self.status = status
        self._stream = io.BytesIO(payload)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:7870/api/usage/v1",
        "http://example.com/api/usage/v1",
        "http://10." "0.0.1/api/usage/v1",
        "http://user:" "synthetic-password@127.0.0.1/api/usage/v1",
        "http://127.0.0.1:99999/api/usage/v1",
        "http://127.0.0.1/api/usage/v1#secret",
    ],
)
def test_url_security_rejects_non_loopback_tls_credentials_and_fragments(url: str) -> None:
    with pytest.raises(ValueError):
        validate_usage_dashboard_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:7870/api/usage/v1",
        "http://localhost:8780/api/usage/v1",
        "http://[::1]:8780/api/usage/v1",
    ],
)
def test_url_security_accepts_only_explicit_loopback_http(url: str) -> None:
    assert validate_usage_dashboard_url(url) == url


def test_fresh_payload_is_semantically_preserved_and_request_has_no_credentials() -> None:
    expected = _payload()
    seen = {}

    def open_request(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["headers"] = dict(request.header_items())
        return _Response(json.dumps(expected).encode("utf-8"))

    proxy = UsageDashboardProxy(url=LOOPBACK_URL, opener=open_request, timeout_seconds=0.25)
    actual = proxy.get()

    assert actual == expected
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert seen["url"] == LOOPBACK_URL
    assert seen["timeout"] == 0.25
    assert seen["headers"]["Accept"] == "application/json"
    assert not any(name.lower() in {"authorization", "cookie"} for name in seen["headers"])


def test_unknown_live_observation_state_fails_closed() -> None:
    payload = _payload()
    payload["providers"]["claude"]["live_observation_state"] = "private_debug_state"

    actual = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    ).get()

    assert actual["refresh"]["error_code"] == "invalid_contract"


def test_legacy_top_level_windows_are_reduced_to_safe_fields() -> None:
    payload = _payload()
    payload["providers"]["claude"]["windows"][0].update(
        {
            "pace": {
                "state": "reserve",
                "delta_percent": 12,
                "expected_used_percent": 37,
                "will_last_to_reset": True,
                "summary": "must-not-cross",
            },
            "account_id": "must-not-cross",
        }
    )

    actual = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    ).get()
    window = actual["providers"]["claude"]["windows"][0]

    assert window["pace"] == {
        "state": "reserve",
        "delta_percent": 12,
        "expected_used_percent": 37,
        "will_last_to_reset": True,
    }
    assert "account_id" not in window
    assert "summary" not in window["pace"]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (socket.timeout(), "upstream_timeout"),
        (OSError("refused"), "upstream_unavailable"),
    ],
)
def test_upstream_failure_returns_explicit_error_without_invented_providers(failure, code) -> None:
    def explode(_request, _timeout):
        raise failure

    payload = UsageDashboardProxy(url=LOOPBACK_URL, opener=explode).get()
    assert payload["schema"] == USAGE_CONTRACT
    assert payload["refresh"]["state"] == "error"
    assert payload["refresh"]["error_code"] == code
    assert payload["providers"] == {}
    assert payload["errors"] == [{"code": f"coord_proxy_{code}"}]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json", "malformed_json"),
        (json.dumps({"schema": "other"}).encode(), "invalid_contract"),
    ],
)
def test_malformed_or_wrong_contract_is_fail_soft(raw: bytes, code: str) -> None:
    proxy = UsageDashboardProxy(url=LOOPBACK_URL, opener=lambda _request, _timeout: _Response(raw))
    payload = proxy.get()
    assert payload["refresh"] == {
        "state": "error",
        "generated_at": payload["generated_at"],
        "error_code": code,
    }


def test_daily_model_breakdowns_cross_proxy_as_bounded_safe_fields() -> None:
    payload = _payload()
    payload["providers"]["claude"]["history"]["daily"][0].update({
        "cache_create_5m_tokens": 3,
        "cache_create_1h_tokens": 4,
        "model_breakdowns": [{
            "key": "model-a1",
            "label": "claude-test-model",
            "total_tokens": 123,
            "input_tokens": 5,
            "output_tokens": 6,
            "cache_read_tokens": 7,
            "cache_create_5m_tokens": 8,
            "cache_create_1h_tokens": 9,
            "cache_create_other_tokens": 10,
            "provider_native_cost_nanos": None,
            "api_rate_estimate_nanos": 1_250_000_000,
            "private_debug": "must-not-cross",
        }],
    })
    proxy = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    )

    day = proxy.get()["providers"]["claude"]["history"]["daily"][0]
    assert day["cache_create_5m_tokens"] == 3
    assert day["cache_create_1h_tokens"] == 4
    assert day["model_breakdowns"] == [{
        "key": "model-a1",
        "label": "claude-test-model",
        "total_tokens": 123,
        "input_tokens": 5,
        "output_tokens": 6,
        "cache_read_tokens": 7,
        "cache_create_5m_tokens": 8,
        "cache_create_1h_tokens": 9,
        "cache_create_other_tokens": 10,
        "provider_native_cost_nanos": None,
        "api_rate_estimate_nanos": 1_250_000_000,
    }]


@pytest.mark.parametrize(
    "field", ["sessions", "provider_history", "models", "projects", "quota_groups"]
)
def test_nested_arrays_are_bounded_at_the_proxy_boundary(field: str) -> None:
    payload = _payload()
    if field == "sessions":
        payload["providers"]["claude"]["active_sessions"]["items"] = [{}] * 51
    elif field == "provider_history":
        payload["providers"]["claude"]["history"]["provider_reported_account"] = {
            "daily": [{}] * 401
        }
    elif field == "quota_groups":
        payload["providers"]["claude"]["quota_groups"] = [{}] * 17
    else:
        payload["providers"]["claude"]["breakdowns"] = {field: {"items": [{}] * 51}}
    proxy = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    )
    assert proxy.get()["refresh"]["error_code"] == "invalid_contract"


def test_quota_groups_are_bounded_and_reduced_to_safe_presentation_fields() -> None:
    payload = _payload()
    payload["providers"]["claude"]["quota_source"] = {
        "kind": "codexbar_cli_live",
        "canonical": True,
        "label": "Codex Bar live Claude quota",
        "debug": "must-not-cross",
    }
    payload["providers"]["claude"]["quota_groups"] = [
        {
            "key": "meter-a1",
            "label": "GPT-5.3-Codex-Spark",
            "semantics": "provider_rate_limit_group",
            "windows": [
                {
                    "kind": "session",
                    "name": "Session",
                    "window_minutes": 300,
                    "used_percent": 12.5,
                    "remaining_percent": 87.5,
                    "resets_at": "2026-08-26T15:00:00Z",
                    "countdown_seconds": 10_800,
                    "pace": {
                        "state": "reserve",
                        "delta_percent": 37,
                        "expected_used_percent": 41,
                        "will_last_to_reset": True,
                        "seconds_to_exhaustion": None,
                        "summary": "must-not-cross",
                    },
                    "raw_limit_id": "must-not-cross",
                }
            ],
            "runout": {
                "kind": "current_window_linear",
                "advisory": True,
                "estimated_exhausts_at": None,
                "seconds_to_exhaustion": 7200,
                "basis": "current window",
                "debug": "must-not-cross",
            },
            "raw_limit_id": "must-not-cross",
        }
    ]

    actual = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    ).get()
    group = actual["providers"]["claude"]["quota_groups"][0]

    assert group == {
        "key": "meter-a1",
        "label": "GPT-5.3-Codex-Spark",
        "semantics": "provider_rate_limit_group",
        "windows": [
            {
                "kind": "session",
                "name": "Session",
                "window_minutes": 300,
                "used_percent": 12.5,
                "remaining_percent": 87.5,
                "resets_at": "2026-08-26T15:00:00Z",
                "countdown_seconds": 10_800,
                "pace": {
                    "state": "reserve",
                    "delta_percent": 37,
                    "expected_used_percent": 41,
                    "will_last_to_reset": True,
                    "seconds_to_exhaustion": None,
                },
            }
        ],
        "runout": {
            "kind": "current_window_linear",
            "advisory": True,
            "estimated_exhausts_at": None,
            "seconds_to_exhaustion": 7200,
            "basis": "current window",
        },
    }
    assert actual["providers"]["claude"]["quota_source"] == {
        "kind": "codexbar_cli_live",
        "canonical": True,
        "label": "Codex Bar live Claude quota",
    }
    assert "raw_limit_id" not in json.dumps(group)
    assert "summary" not in json.dumps(group)
    assert "debug" not in json.dumps(actual["providers"]["claude"]["quota_source"])


@pytest.mark.parametrize(
    "quota_groups",
    [
        [
            {
                "key": "meter-a",
                "label": "user@example.com",
                "semantics": "provider_rate_limit_group",
                "windows": [],
                "runout": {},
            }
        ],
        [
            {
                "key": "meter-a",
                "label": "/private/account",
                "semantics": "provider_rate_limit_group",
                "windows": [],
                "runout": {},
            }
        ],
        [
            {
                "key": "meter-a",
                "label": "Account quota",
                "semantics": "provider_rate_limit_group",
                "windows": [{}] * 33,
                "runout": {},
            }
        ],
        [
            {
                "key": "meter-a",
                "label": "Account quota",
                "semantics": "provider_rate_limit_group",
                "windows": [],
                "runout": {},
            },
            {
                "key": "meter-a",
                "label": "Second quota",
                "semantics": "provider_rate_limit_group",
                "windows": [],
                "runout": {},
            },
        ],
    ],
)
def test_quota_group_boundary_rejects_leaks_overflow_and_duplicate_keys(
    quota_groups: list[dict],
) -> None:
    payload = _payload()
    payload["providers"]["claude"]["quota_groups"] = quota_groups

    actual = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    ).get()

    assert actual["refresh"]["error_code"] == "invalid_contract"


@pytest.mark.parametrize(
    "quota_source,pace",
    [
        (
            {
                "kind": "codexbar_cli_live",
                "canonical": True,
                "label": "user@example.com",
            },
            None,
        ),
        (
            {
                "kind": "codexbar_cli_live",
                "canonical": True,
                "label": "Live quota",
            },
            {
                "state": "reserve",
                "delta_percent": 1,
                "expected_used_percent": 2,
                "will_last_to_reset": "yes",
            },
        ),
        (
            {
                "kind": "codexbar_cli_live",
                "canonical": True,
                "label": "Live quota",
            },
            {
                "state": "invented",
                "delta_percent": 1,
                "expected_used_percent": 2,
                "will_last_to_reset": True,
            },
        ),
    ],
)
def test_quota_source_and_pace_reject_private_or_invalid_values(
    quota_source: dict, pace: dict | None
) -> None:
    payload = _payload()
    payload["providers"]["claude"]["quota_source"] = quota_source
    if pace is not None:
        payload["providers"]["claude"]["quota_groups"] = [
            {
                "key": "account",
                "label": "Account quota",
                "semantics": "provider_rate_limit_group",
                "windows": [
                    {
                        "kind": "session",
                        "name": "Session",
                        "used_percent": 10,
                        "remaining_percent": 90,
                        "pace": pace,
                    }
                ],
                "runout": {},
            }
        ]

    actual = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    ).get()

    assert actual["refresh"]["error_code"] == "invalid_contract"


def test_last_good_is_bounded_and_marks_stale_without_changing_provider_values() -> None:
    expected = _payload()
    calls = [
        _Response(json.dumps(expected).encode("utf-8")),
        socket.timeout(),
        socket.timeout(),
    ]
    clock = [10.0]

    def open_request(_request, _timeout):
        value = calls.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    proxy = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=open_request,
        monotonic=lambda: clock[0],
        now=lambda: "2026-08-26T12:01:00Z",
        last_good_ttl_seconds=30,
    )
    assert proxy.get() == expected

    clock[0] = 25.0
    stale = proxy.get()
    assert stale["providers"] == expected["providers"]
    assert stale["generated_at"] == expected["generated_at"]
    assert stale["refresh"]["state"] == "stale"
    assert stale["refresh"]["error_code"] == "upstream_timeout"
    assert stale["errors"][-1] == {"code": "coord_proxy_upstream_timeout"}

    clock[0] = 41.0
    expired = proxy.get()
    assert expired["refresh"]["state"] == "error"
    assert expired["providers"] == {}
    assert expired["errors"] == [{"code": "coord_proxy_upstream_timeout"}]


def test_caller_cannot_mutate_cached_last_good() -> None:
    expected = _payload()
    calls = [
        _Response(json.dumps(expected).encode("utf-8")),
        socket.timeout(),
    ]

    def open_request(_request, _timeout):
        value = calls.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    proxy = UsageDashboardProxy(url=LOOPBACK_URL, opener=open_request)
    first = proxy.get()
    first["providers"]["claude"]["history"]["rolling_7d_total_tokens"] = -1
    fallback = proxy.get()
    assert fallback["providers"] == deepcopy(expected["providers"])


@pytest.mark.parametrize("explicit_url", [None, ""])
def test_unconfigured_proxy_uses_local_provider(monkeypatch, explicit_url) -> None:
    monkeypatch.delenv("COORD_USAGE_DASHBOARD_URL", raising=False)
    expected = _payload()
    payload = UsageDashboardProxy(url=explicit_url, local_provider=lambda: expected).get()

    assert payload == expected
    assert payload["refresh"]["state"] == "fresh"


def test_environment_configures_proxy(monkeypatch) -> None:
    monkeypatch.setenv("COORD_USAGE_DASHBOARD_URL", LOOPBACK_URL)

    assert UsageDashboardProxy().url == LOOPBACK_URL


def test_allowlist_strips_sensitive_unknown_fields_at_every_nested_level() -> None:
    payload = _payload()
    provider = payload["providers"]["claude"]
    payload["debug"] = {"deep": {"value": "must-not-cross"}}
    provider["source"]["opaque_authorization"] = "Bearer must-not-cross"
    provider["account"]["email"] = "private@example.invalid"
    provider["reset_credits"] = [
        {
            "status": "inventory",
            "count": 1,
            "expires_at": "2026-08-27T12:00:00Z",
            "semantics": "earned_credit_inventory_not_current_reset_eligibility",
            "credit_id": "private-credit-id",
        }
    ]
    provider["history"]["daily"][0]["source_path"] = "/redacted/history.json"
    provider["errors"] = [{"code": "SAFE_CODE", "detail": "Bearer must-not-cross"}]

    actual = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(payload).encode("utf-8")),
    ).get()
    serialized = json.dumps(actual)

    assert actual["refresh"]["state"] == "fresh"
    assert actual["providers"]["claude"]["reset_credits"] == [
        {
            "status": "inventory",
            "count": 1,
            "expires_at": "2026-08-27T12:00:00Z",
            "semantics": "earned_credit_inventory_not_current_reset_eligibility",
        }
    ]
    assert actual["providers"]["claude"]["errors"] == [{"code": "SAFE_CODE"}]
    for secret in (
        "must-not-cross",
        "private@example.invalid",
        "private-credit-id",
        "/redacted",
    ):
        assert secret not in serialized


def test_invalid_timestamp_and_deep_json_fail_soft_without_handler_abort() -> None:
    invalid = _payload()
    invalid["generated_at"] = "not-a-time"
    invalid_result = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(json.dumps(invalid).encode("utf-8")),
    ).get()
    assert invalid_result["refresh"]["error_code"] == "invalid_contract"

    base = json.dumps(_payload())
    deeply_nested = (
        base[:-1].encode("utf-8")
        + b',"unknown":'
        + (b"[" * 2_000)
        + b"0"
        + (b"]" * 2_000)
        + b"}"
    )
    deep_result = UsageDashboardProxy(
        url=LOOPBACK_URL,
        opener=lambda _request, _timeout: _Response(deeply_nested),
    ).get()
    assert deep_result["schema"] == USAGE_CONTRACT
    assert deep_result["refresh"]["state"] in {"fresh", "error"}
    if deep_result["refresh"]["state"] == "error":
        assert deep_result["refresh"]["error_code"] == "malformed_json"
