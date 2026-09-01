from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
from typing import Any, Iterator

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.board.server import _native_operator_request_fields, make_server
from coordharness.coord import coord_db, native_cockpit
from coordharness.coord.config import connect


WORK_ID = "NATIVE-OPERATOR-WRITES-1"
TOKEN = "native-operator-test-token-0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]


def _seed(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", database.parent)
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="Native operator transfer fixture",
            assignee="claude",
            assigned_by="claude",
            module="coord",
            surface="task",
            done_signal="artifacts/native-operator-proof.json",
            acceptance_json=json.dumps(["proof exists"]),
            note="native operator endpoint fixture",
            intent_state="queued",
            # Ranked, because `/api/v1/actions` resolves its target against the
            # rows the snapshot carries, and the snapshot's operator surface
            # keeps queued work only where somebody gave it a priority. These
            # tests are about whether the served routes refuse to write, so the
            # fixture seeds a row that is on the board to be refused.
            priority=1,
        )
    finally:
        conn.close()


def _document(database: Path, *, operation_id: str = "native-action-0001") -> dict[str, Any]:
    conn = connect(database)
    try:
        row = conn.execute(
            "SELECT version,assignee FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        heads = coord_db._typed_handoff_head_state_unlocked(conn, WORK_ID)[
            "active_event_ids"
        ]
    finally:
        conn.close()
    return {
        "schema_version": 1,
        "action_id": operation_id,
        "source_face": "native_cockpit",
        "actor": "operator",
        "action": "work.reassign",
        "target": {
            "work_id": WORK_ID,
            "expected_version": int(row["version"]),
            "expected_assignee": str(row["assignee"]),
            "expected_head_event_ids": heads,
        },
        "payload": {
            "owner_lane": "codex",
            "target_intent": "queued",
            "task": "Continue the canonical row",
            "why": "The resident operator confirmed this transfer",
            "acceptance": "Preserve and satisfy the existing done signal",
            "refs": [f"coord://work/{WORK_ID}"],
            "constraints": ["preserve the declared done signal"],
            "release_held_claim": False,
            "confirmed": True,
        },
        "dry_run": False,
    }


def _request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    path = tmp_path / "coord.db"
    _seed(path, monkeypatch)
    return path


def _serve(database: Path) -> Iterator[Any]:
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_default_public_server_keeps_native_and_browser_actions_read_only(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_NATIVE_OPERATOR_WRITES", raising=False)
    monkeypatch.delenv("COORD_NATIVE_OPERATOR_TOKEN", raising=False)
    before = database.read_bytes()
    for server in _serve(database):
        raw = json.dumps(_document(database)).encode()
        status, headers, body = _request(
            server.server_port,
            "/api/native/action",
            method="POST",
            body=raw,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        )
        assert status == 405
        assert headers["Allow"] == "GET, HEAD, OPTIONS"
        assert body == b""
        status, _headers, body = _request(server.server_port, "/api/native/action")
        assert status == 404
        assert body == b"not found"
        status, _headers, body = _request(
            server.server_port, f"/api/v1/actions?target={WORK_ID}"
        )
        assert status == 200
        registry = json.loads(body)
        assert registry["source"]["read_only"] is True
        assert registry["counts"]["available_mutations"] == 0
    assert database.read_bytes() == before


def test_authenticated_loopback_transfer_is_fenced_and_replay_safe(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_NATIVE_OPERATOR_WRITES", "1")
    monkeypatch.setenv("COORD_NATIVE_OPERATOR_TOKEN", TOKEN)
    document = _document(database)
    assert "_authority_capability" not in _native_operator_request_fields(document)
    seen_capabilities: list[object] = []
    real_post = coord_db.post_operator_reassignment

    def observed_post(conn: Any, **fields: Any) -> dict[str, Any]:
        seen_capabilities.append(fields.get("_authority_capability"))
        return real_post(conn, **fields)

    monkeypatch.setattr(coord_db, "post_operator_reassignment", observed_post)
    raw = json.dumps(document, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    for server in _serve(database):
        unauthenticated, _headers, body = _request(
            server.server_port,
            "/api/native/action",
            method="POST",
            body=raw,
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"},
        )
        assert unauthenticated == 401
        assert TOKEN.encode() not in body
        assert seen_capabilities == []

        status, _headers, body = _request(
            server.server_port, "/api/native/action", method="POST", body=raw, headers=headers
        )
        assert status == 200
        receipt = json.loads(body)
        assert receipt["status"] == "applied"
        assert receipt["owner_lane"] == "codex"
        assert receipt["work_version"] == document["target"]["expected_version"] + 1
        assert receipt["released_claim_ids"] == []
        assert receipt["replayed"] is False

        status, _headers, body = _request(
            server.server_port, "/api/native/action", method="POST", body=raw, headers=headers
        )
        assert status == 200
        replay = json.loads(body)
        assert replay["status"] == "replayed"
        assert replay["event_id"] == receipt["event_id"]
        assert replay["replayed"] is True

        stale = {**document, "action_id": "native-action-stale-0002"}
        status, _headers, body = _request(
            server.server_port,
            "/api/native/action",
            method="POST",
            body=json.dumps(stale).encode(),
            headers=headers,
        )
        assert status == 409
        conflict = json.loads(body)
        assert conflict["status"] == "stale"
        assert conflict["error"]["code"] == "stale_fence"
        assert WORK_ID not in body.decode()
        assert seen_capabilities
        assert all(
            capability is coord_db._OPERATOR_REASSIGNMENT_CAPABILITY
            for capability in seen_capabilities
        )


def test_native_route_refuses_ambiguous_json_methods_and_claim_conflicts(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_NATIVE_OPERATOR_WRITES", "1")
    monkeypatch.setenv("COORD_NATIVE_OPERATOR_TOKEN", TOKEN)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    conn = connect(database)
    try:
        coord_db.register_session(conn, "claude:native-holder", "claude")
        claim_id = coord_db.claim_work(
            conn, "claude:native-holder", WORK_ID, step="still working", lease_s=600
        )
    finally:
        conn.close()
    document = _document(database)
    for server in _serve(database):
        duplicate = json.dumps(document).replace(
            "\"action\": \"work.reassign\",",
            "\"action\": \"work.reassign\", \"action\": \"handoff.create\",",
        ).encode()
        for raw in (duplicate, b"{\"x\":NaN}"):
            status, _headers, body = _request(
                server.server_port,
                "/api/native/action",
                method="POST",
                body=raw,
                headers=headers,
            )
            assert status == 400
            assert json.loads(body)["error"]["code"] == "invalid_json"
        status, response_headers, body = _request(
            server.server_port, "/api/native/action", method="PUT", body=b"{}", headers=headers
        )
        assert status == 405
        assert response_headers["Allow"] == "POST, OPTIONS"
        assert body == b""
        status, _headers, body = _request(
            server.server_port,
            "/api/native/action",
            method="POST",
            body=json.dumps(document).encode(),
            headers=headers,
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "claim_conflict"
    conn = connect(database)
    try:
        claim = conn.execute(
            "SELECT status FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        assert claim["status"] == "running"
        assert conn.execute(
            "SELECT assignee FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()[0] == "claude"
    finally:
        conn.close()


def test_native_projection_carries_fences_and_truthful_action_reasons(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_NATIVE_OPERATOR_WRITES", raising=False)
    monkeypatch.delenv("COORD_NATIVE_OPERATOR_TOKEN", raising=False)
    conn = connect(database)
    try:
        result = native_cockpit.refresh(conn, source_version="native-writes-default-test")
        row = dict(conn.execute(
            "SELECT * FROM native_cockpit_rows WHERE writer_seq=? AND coord_work_id=?",
            (result["writer_seq"], WORK_ID),
        ).fetchone())
        actions = [dict(item) for item in conn.execute(
            "SELECT * FROM native_cockpit_row_actions WHERE writer_seq=? AND work_id=? ORDER BY sort_order",
            (result["writer_seq"], WORK_ID),
        )]
        assert row["work_version"] == 0
        assert row["current_assignee"] == "claude"
        assert json.loads(row["assignment_head_event_ids"]) == []
        assert json.loads(row["active_claim_ids"]) == []
        assert row["claim_live"] == 0
        assert row["live_run_count"] == 0
        assert row["native_operator_writes_enabled"] == 0
        assert len(actions) == 3
        assert all(action["enabled"] == 0 for action in actions)
        assert all("COORD_NATIVE_OPERATOR_WRITES" in action["disabled_reason"] for action in actions)

        monkeypatch.setenv("COORD_NATIVE_OPERATOR_WRITES", "1")
        monkeypatch.setenv("COORD_NATIVE_OPERATOR_TOKEN", TOKEN)
        result = native_cockpit.refresh(conn, source_version="native-writes-enabled-test")
        actions = [dict(item) for item in conn.execute(
            "SELECT * FROM native_cockpit_row_actions WHERE writer_seq=? AND work_id=? ORDER BY sort_order",
            (result["writer_seq"], WORK_ID),
        )]
        by_action = {action["action"]: action for action in actions}
        assert by_action["task.assign.claude"]["enabled"] == 0
        assert "already" in by_action["task.assign.claude"]["disabled_reason"]
        assert by_action["task.assign.codex"]["enabled"] == 1
        assert by_action["handoff.create"]["enabled"] == 1
        assert all(action["requires_confirmation"] == 1 for action in actions)
    finally:
        conn.close()


def test_installer_keeps_writes_default_off_and_offers_explicit_opt_in() -> None:
    installer = (ROOT / "apps/install.sh").read_text(encoding="utf-8")
    assert "--enable-native-operator-writes" in installer
    assert "ENABLE_NATIVE_OPERATOR_WRITES=0" in installer
    assert '"COORD_NATIVE_OPERATOR_WRITES": "1"' in installer
    assert '"COORD_NATIVE_OPERATOR_TOKEN_FILE": native_operator_token_path' in installer
    assert "secrets.token_urlsafe(48)" in installer
    assert "0o600" in installer
    assert "for _attempt in {1..10}" in installer
    assert "bootstrap_ok" in installer
    assert "could not be bootstrapped after a bounded retry" in installer
