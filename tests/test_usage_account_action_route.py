from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server
from coordharness.usage.account_actions import UsageAccountActionForwarder
from coordharness.usage.dashboard_proxy import UsageDashboardProxy


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, method: str, body: bytes | None) -> tuple[int, bytes]:
        self.calls.append((method, body))
        action = json.loads(body)["action"] if body is not None else None
        is_codex_start = action == "codex_login_start"
        is_claude_connect = action == "claude_connect_open"
        document = {
            "schema": "coordharness.usage-account-actions.v1",
            "codex": {
                "state": "waiting_browser" if is_codex_start else "idle",
                "can_start": not is_codex_start,
                "can_cancel": is_codex_start,
                "auth_url": "https://must-not-cross.invalid/private",
                "login_id": "must-not-cross",
            },
            "claude": {
                "state": "manual_connect_required",
                "connect_available": True,
                **({"opened": True} if is_claude_connect else {}),
            },
        }
        if is_codex_start:
            document.update({"ok": True, "result": "browser_opened"})
        elif is_claude_connect:
            document.update({"ok": True, "result": "connect_window_opened"})
        return 202 if is_codex_start else 200, json.dumps(document).encode("utf-8")


@pytest.fixture()
def usage_server(tmp_path: Path):
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    transport = _Transport()
    server = make_server(
        port=0,
        db_path=str(database),
        refresh_interval=3600,
        usage_account_forwarder=UsageAccountActionForwarder(transport=transport),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, transport
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        connection.close()


def _headers(port: int) -> dict[str, str]:
    return {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
        "X-Coord-Usage-Action": "v1",
    }


def test_status_and_valid_action_return_only_sanitized_state(usage_server) -> None:
    port, transport = usage_server
    status, content_type, body = _request(port, "GET", "/api/v1/usage-actions/status")
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    status_document = json.loads(body)
    assert status_document["codex"] == {
        "state": "idle",
        "can_start": True,
        "can_cancel": False,
    }

    payload = b'{"action":"codex_login_start"}'
    status, content_type, body = _request(
        port,
        "POST",
        "/api/v1/usage-actions",
        body=payload,
        headers=_headers(port),
    )
    assert status == 202
    assert content_type == "application/json; charset=utf-8"
    document = json.loads(body)
    assert document["codex"] == {
        "state": "waiting_browser",
        "can_start": False,
        "can_cancel": True,
    }
    assert document["result"] == "browser_opened"
    assert "must-not-cross" not in body.decode("utf-8")

    claude_payload = b'{"action":"claude_connect_open"}'
    status, _, body = _request(
        port,
        "POST",
        "/api/v1/usage-actions",
        body=claude_payload,
        headers=_headers(port),
    )
    assert status == 200
    claude_document = json.loads(body)
    assert claude_document["result"] == "connect_window_opened"
    assert claude_document["claude"] == {
        "state": "manual_connect_required",
        "connect_available": True,
        "opened": True,
    }
    assert transport.calls == [
        ("GET", None),
        ("POST", payload),
        ("POST", claude_payload),
    ]


@pytest.mark.parametrize(
    ("path", "body", "header_changes", "expected"),
    [
        ("/api/v1/usage-actions?debug=1", b'{"action":"codex_login_start"}', {}, 400),
        ("/api/v1/usage-actions", b'{"action":"codex_login_start"}', {"Origin": ""}, 403),
        (
            "/api/v1/usage-actions",
            b'{"action":"codex_login_start"}',
            {"Origin": "http://attacker.invalid"},
            403,
        ),
        (
            "/api/v1/usage-actions",
            b'{"action":"codex_login_start"}',
            {"Content-Type": "text/plain"},
            415,
        ),
        (
            "/api/v1/usage-actions",
            b'{"action":"codex_login_start"}',
            {"X-Coord-Usage-Action": "wrong"},
            403,
        ),
        ("/api/v1/usage-actions", b'{"action":"codex_login_start","extra":true}', {}, 400),
        ("/api/v1/usage-actions", b'{"action":NaN}', {}, 400),
        ("/api/v1/usage-actions", b'{"action":"open_url"}', {}, 400),
        ("/api/v1/usage-actions", b'{"action":"codex_login_start","action":"open_url"}', {}, 400),
    ],
)
def test_action_route_rejects_unsafe_requests(
    usage_server,
    path: str,
    body: bytes,
    header_changes: dict[str, str],
    expected: int,
) -> None:
    port, transport = usage_server
    headers = _headers(port)
    headers.update(header_changes)
    before = len(transport.calls)

    status, _content_type, _response = _request(port, "POST", path, body=body, headers=headers)

    assert status == expected
    assert len(transport.calls) == before


def test_action_route_rejects_oversized_body_before_reading_it(usage_server) -> None:
    port, transport = usage_server
    status, _content_type, _body = _request(
        port,
        "POST",
        "/api/v1/usage-actions",
        body=b"x" * 4_097,
        headers=_headers(port),
    )
    assert status == 413
    assert transport.calls == []


def test_other_post_routes_remain_read_only(usage_server) -> None:
    port, transport = usage_server
    status, _content_type, _body = _request(
        port, "POST", "/api/v1/snapshot", body=b"{}", headers=_headers(port)
    )
    assert status == 405
    assert transport.calls == []


def test_explicitly_disabled_usage_proxy_also_disables_account_upstream(
    tmp_path: Path,
) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(
        port=0,
        db_path=str(database),
        refresh_interval=3600,
        usage_dashboard_proxy=UsageDashboardProxy(url=""),
    )
    try:
        assert server._usage_dashboard_proxy.url is None
        assert server._usage_account_forwarder._action_url is None
    finally:
        server.server_close()
