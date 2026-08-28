from __future__ import annotations

import json

import pytest

from coordharness.usage import account_actions
from coordharness.usage.account_actions import SCHEMA_ID, UsageAccountActionForwarder


def test_default_transport_uses_shared_provider_account_path_for_status_and_actions(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "COORD_USAGE_DASHBOARD_URL",
        "http://127.0.0.1:8998/configured/usage",
    )
    requests = []

    class Response:
        def __init__(self, method: str):
            self.method = method
            self.status = 202 if method == "POST" else 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            document = {
                "schema": "coordharness.usage-account-actions.v1",
                "codex": {
                    "state": "waiting_browser" if self.method == "POST" else "idle",
                    "can_start": self.method != "POST",
                    "can_cancel": self.method == "POST",
                },
                "claude": {
                    "state": "manual_connect_required",
                    "connect_available": True,
                },
            }
            if self.method == "POST":
                document.update({"ok": True, "result": "browser_opened"})
            return json.dumps(document).encode("utf-8")

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response(request.method)

    monkeypatch.setattr(account_actions, "build_opener", lambda *_handlers: Opener())
    forwarder = UsageAccountActionForwarder(timeout_seconds=2)
    configured = UsageAccountActionForwarder(
        timeout_seconds=2,
        dashboard_url="http://127.0.0.1:8999/custom/usage",
    )

    assert forwarder.status()[0] == 200
    assert forwarder.forward("codex_login_start")[0] == 202
    assert configured.status()[0] == 200

    assert [request.full_url for request, _timeout in requests] == [
        "http://127.0.0.1:8998/api/usage/v1/account-actions",
        "http://127.0.0.1:8998/api/usage/v1/account-actions",
        "http://127.0.0.1:8999/api/usage/v1/account-actions",
    ]
    assert [request.method for request, _timeout in requests] == ["GET", "POST", "GET"]
    assert [request.get_header("Origin") for request, _timeout in requests] == [
        "http://127.0.0.1:8998",
        "http://127.0.0.1:8998",
        "http://127.0.0.1:8999",
    ]
    assert requests[1][0].get_header("X-coordharness-usage-action") == "v1"


def test_missing_upstream_configuration_uses_local_service(monkeypatch) -> None:
    monkeypatch.delenv("COORD_USAGE_DASHBOARD_URL", raising=False)

    class LocalService:
        def status(self):
            return {"schema": "coordharness.usage-account-actions.v1", "codex": {"state": "idle", "can_start": False, "can_cancel": False}, "claude": {"state": "unavailable", "connect_available": False}}

    status, document = UsageAccountActionForwarder(local_service=LocalService()).status()

    assert status == 200
    assert document["codex"]["can_start"] is False
    assert document["claude"]["state"] == "unavailable"


@pytest.mark.parametrize(
    "dashboard_url",
    [
        "https://127.0.0.1:7870/api/usage/v1",
        "http://example.invalid/api/usage/v1",
        "http://user:" "synthetic-password@127.0.0.1:7870/api/usage/v1",
    ],
)
def test_account_upstream_rejects_non_loopback_tls_and_credentials(
    dashboard_url: str,
) -> None:
    with pytest.raises(ValueError):
        UsageAccountActionForwarder(dashboard_url=dashboard_url)


def test_forwarder_exposes_only_fixed_safe_account_state() -> None:
    seen: list[tuple[str, bytes | None]] = []

    def transport(method: str, body: bytes | None) -> tuple[int, bytes]:
        seen.append((method, body))
        return 202, json.dumps(
            {
                "schema": "coordharness.usage-account-actions.v1",
                "ok": True,
                "result": "browser_opened",
                "codex": {
                    "state": "waiting_browser",
                    "can_start": False,
                    "can_cancel": True,
                    "auth_url": "https://secret.invalid/login?token=private",
                    "login_id": "private-login-id",
                    "email": "private@example.invalid",
                },
                "claude": {
                    "state": "manual_connect_required",
                    "connect_available": True,
                    "opened": False,
                    "path": "/Applications/Private.app",
                },
                "raw_error": "/redacted/token-store",
            }
        ).encode("utf-8")

    status, document = UsageAccountActionForwarder(transport=transport).forward("codex_login_start")

    assert status == 202
    assert document == {
        "schema": SCHEMA_ID,
        "ok": True,
        "result": "browser_opened",
        "codex": {
            "state": "waiting_browser",
            "can_start": False,
            "can_cancel": True,
        },
        "claude": {
            "state": "manual_connect_required",
            "connect_available": True,
            "opened": False,
        },
    }
    assert seen == [("POST", b'{"action":"codex_login_start"}')]
    serialized = json.dumps(document)
    for secret in (
        "secret.invalid",
        "private-login-id",
        "private@example.invalid",
        "/Applications/Private.app",
        "/redacted",
    ):
        assert secret not in serialized


def test_claude_connect_action_forwards_exact_new_vocabulary() -> None:
    seen: list[tuple[str, bytes | None]] = []

    def transport(method: str, body: bytes | None) -> tuple[int, bytes]:
        seen.append((method, body))
        return 200, json.dumps(
            {
                "schema": "coordharness.usage-account-actions.v1",
                "ok": True,
                "result": "connect_window_opened",
                "codex": {"state": "idle", "can_start": True, "can_cancel": False},
                "claude": {
                    "state": "manual_connect_required",
                    "connect_available": True,
                    "opened": True,
                },
            }
        ).encode("utf-8")

    status, document = UsageAccountActionForwarder(transport=transport).forward(
        "claude_connect_open"
    )

    assert status == 200
    assert document["ok"] is True
    assert document["result"] == "connect_window_opened"
    assert document["claude"] == {
        "state": "manual_connect_required",
        "connect_available": True,
        "opened": True,
    }
    assert seen == [("POST", b"{\"action\":\"claude_connect_open\"}")]


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"{\"schema\":\"wrong.v1\"}",
    ],
)
def test_forwarder_rejects_missing_or_wrong_upstream_schema(payload: bytes) -> None:
    status, document = UsageAccountActionForwarder(
        transport=lambda _method, _body: (200, payload)
    ).status()

    assert status == 503
    assert document["ok"] is False
    assert document["schema"] == SCHEMA_ID
    assert document["claude"]["connect_available"] is False


def test_forwarder_rejects_incoherent_action_result_and_state() -> None:
    cases = [
        (
            "codex_login_start",
            202,
            {
                "schema": "coordharness.usage-account-actions.v1",
                "ok": True,
                "result": "browser_opened",
                "codex": {"state": "idle", "can_start": True, "can_cancel": False},
                "claude": {"state": "manual_connect_required", "connect_available": True},
            },
        ),
        (
            "claude_connect_open",
            200,
            {
                "schema": "coordharness.usage-account-actions.v1",
                "ok": True,
                "result": "connect_window_opened",
                "codex": {"state": "idle", "can_start": True, "can_cancel": False},
                "claude": {
                    "state": "manual_connect_required",
                    "connect_available": True,
                    "opened": False,
                },
            },
        ),
        (
            "claude_connect_open",
            200,
            {
                "schema": "coordharness.usage-account-actions.v1",
                "ok": True,
                "result": "cancelled",
                "codex": {"state": "cancelled", "can_start": True, "can_cancel": False},
                "claude": {"state": "manual_connect_required", "connect_available": True},
            },
        ),
    ]

    for action, upstream_status, payload in cases:
        status, document = UsageAccountActionForwarder(
            transport=lambda _method, _body, code=upstream_status, value=payload: (
                code,
                json.dumps(value).encode("utf-8"),
            )
        ).forward(action)
        assert status == 503
        assert document["ok"] is False
        assert document["codex"]["state"] == "unavailable"


@pytest.mark.parametrize(
    "action",
    ["", "codex_login_complete", "open_url", "../../token", "claude_login_start"],
)
def test_forwarder_rejects_unknown_actions_without_transport(action: str) -> None:
    calls = 0

    def transport(_method: str, _body: bytes | None) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        return 200, b"{}"

    status, document = UsageAccountActionForwarder(transport=transport).forward(action)

    assert status == 400
    assert document["ok"] is False
    assert document["codex"] == {
        "state": "unavailable",
        "can_start": False,
        "can_cancel": False,
    }
    assert document["claude"] == {
        "state": "unavailable",
        "connect_available": False,
    }
    assert calls == 0


def test_forwarder_fails_closed_on_large_or_nonstandard_json() -> None:
    payloads = [
        b"{" + (b" " * (16 * 1024)) + b"}",
        b'{"ok":NaN}',
        b"not-json",
    ]

    for payload in payloads:
        status, document = UsageAccountActionForwarder(
            transport=lambda _method, _body, value=payload: (200, value)
        ).status()
        assert status == 503
        assert document["ok"] is False
        assert document["codex"]["state"] == "unavailable"
        assert document["claude"]["state"] == "unavailable"


def test_forwarder_normalizes_untrusted_status_and_fields() -> None:
    status, document = UsageAccountActionForwarder(
        transport=lambda _method, _body: (
            418,
            b'{"schema":"coordharness.usage-account-actions.v1",'
            b'"codex":{"state":"secret","can_start":"yes","can_cancel":1},'
            b'"claude":{"state":"secret","connect_available":"yes"}}',
        )
    ).status()

    assert status == 502
    assert document == {
        "schema": SCHEMA_ID,
        "ok": False,
        "codex": {
            "state": "unavailable",
            "can_start": False,
            "can_cancel": False,
        },
        "claude": {
            "state": "unavailable",
            "connect_available": False,
        },
    }

@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("connected", "connected"),
        ("sign_in_required", "sign_in_required"),
        ("manual_connect_required", "manual_connect_required"),
    ],
)
def test_forwarder_preserves_only_finite_claude_connection_states(
    state: str,
    expected: str,
) -> None:
    payload = {
        "schema": "coordharness.usage-account-actions.v1",
        "codex": {"state": "idle", "can_start": True, "can_cancel": False},
        "claude": {"state": state, "connect_available": True},
    }
    status, document = UsageAccountActionForwarder(
        transport=lambda _method, _body: (200, json.dumps(payload).encode("utf-8"))
    ).status()
    assert status == 200
    assert document["claude"] == {
        "state": expected,
        "connect_available": True,
    }


def test_connected_claude_can_open_settings_without_false_auth_transition() -> None:
    payload = {
        "schema": "coordharness.usage-account-actions.v1",
        "ok": True,
        "result": "connect_window_opened",
        "codex": {"state": "idle", "can_start": True, "can_cancel": False},
        "claude": {
            "state": "connected",
            "connect_available": True,
            "opened": True,
        },
    }
    status, document = UsageAccountActionForwarder(
        transport=lambda _method, _body: (200, json.dumps(payload).encode("utf-8"))
    ).forward("claude_connect_open")
    assert status == 200
    assert document["claude"]["state"] == "connected"
    assert document["result"] == "connect_window_opened"
