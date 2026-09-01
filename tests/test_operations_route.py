from __future__ import annotations

from copy import deepcopy
import http.client
import json
from pathlib import Path
import threading

import pytest
from jsonschema import Draft202012Validator

from coordharness import demo
from coordharness.board import server as board_server
from coordharness.board.server import make_server


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def operations_board(tmp_path: Path):
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    server = make_server(port=0, db_path=str(db), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, path: str) -> tuple[int, str, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        connection.close()


def test_operations_document_and_shell_are_served(operations_board) -> None:
    server = operations_board
    status, content_type, body = _request(server.server_port, "/api/v1/operations")
    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    document = json.loads(body)
    assert document == server.operations()
    assert document["schema_version"] == "OpsAtlasV1"
    assert document["generated_at"] == server.snapshot()["generated_at"]
    assert document["graph_envelope"]["schema_version"] == "GraphEnvelopeV1"
    assert document["graph_envelope"]["source"]["content_sha256"]

    page_status, page_type, page = _request(server.server_port, "/ops")
    assert page_status == 200
    assert page_type == "text/html; charset=utf-8"
    markup = page.decode("utf-8")
    assert "COORD Operations Atlas" in markup
    assert 'src="/static/ops-atlas-model.js"' in markup
    assert 'src="/static/ops-atlas.js"' in markup
    assert "<style" not in markup
    assert "<script>" not in markup

    status_code, status_type, status_body = _request(
        server.server_port, "/api/v1/read-status"
    )
    assert status_code == 200
    assert status_type == "application/json; charset=utf-8"
    read_status = json.loads(status_body)
    read_status_schema = json.loads(
        (REPO / "src/coordharness/board/read_status_v1.schema.json").read_text()
    )
    Draft202012Validator.check_schema(read_status_schema)
    Draft202012Validator(read_status_schema).validate(read_status)
    assert read_status["schema_version"] == "ReadStatusV1"
    assert read_status["read_only"] is True
    assert read_status["degraded"] is False
    assert read_status["cache_generation"] == 1

    bundle_code, bundle_type, bundle_body = _request(
        server.server_port, "/api/v1/operations-bundle"
    )
    assert bundle_code == 200
    assert bundle_type == "application/json; charset=utf-8"
    bundle = json.loads(bundle_body)
    assert bundle["schema_version"] == "OpsAtlasBundleV1"
    assert bundle["cache_generation"] == 1
    assert bundle["read_status"]["cache_generation"] == bundle["cache_generation"]
    assert bundle["read_status"]["generated_at"] == bundle["generated_at"]
    for name, getter in (
        ("snapshot", server.snapshot),
        ("graph", server.graph),
        ("context", server.context),
        ("timeline", server.timeline),
        ("operations", server.operations),
    ):
        assert bundle[name] == getter()


def test_operations_bundle_v2_includes_coherent_public_pulse(operations_board) -> None:
    server = operations_board
    status, content_type, body = _request(server.server_port, "/api/v2/operations-bundle")

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    bundle = json.loads(body)
    assert bundle["schema_version"] == "OpsAtlasBundleV2"
    assert bundle["cache_generation"] == bundle["read_status"]["cache_generation"]
    assert bundle["pulse"] == server.pulse()
    for name, getter in (
        ("snapshot", server.snapshot),
        ("graph", server.graph),
        ("context", server.context),
        ("timeline", server.timeline),
        ("operations", server.operations),
    ):
        assert bundle[name] == getter()
    encoded = json.dumps(bundle["pulse"], sort_keys=True)
    for withheld in ("body", "payload_json", "refs_json", "session_id", "severity", "verdict", "trust"):
        assert f"\"{withheld}\"" not in encoded


def test_operations_builder_failure_preserves_the_whole_cached_set(
    operations_board, monkeypatch
) -> None:
    server = operations_board
    originals = (
        server.snapshot(),
        server.graph(),
        server.context(),
        server.timeline(),
        server.pulse(),
        server.operations(),
    )

    def _explode(*_args, **_kwargs):
        raise RuntimeError("derived operations failed")

    monkeypatch.setattr(board_server, "build_operations", _explode)
    server._next_refresh = 0.0
    server.service_actions()

    assert (
        server.snapshot(),
        server.graph(),
        server.context(),
        server.timeline(),
        server.pulse(),
        server.operations(),
    ) == originals
    failed_status = server.read_status()
    assert failed_status["degraded"] is True
    assert failed_status["consecutive_refresh_failures"] == 1
    assert failed_status["last_failure_class"] == "RuntimeError"
    assert failed_status["cache_generation"] == 1

    monkeypatch.undo()
    server._next_refresh = 0.0
    server.service_actions()
    assert server.snapshot() is not originals[0]
    assert server.pulse() is not originals[4]
    assert server.operations() is not originals[5]
    recovered_status = server.read_status()
    assert recovered_status["degraded"] is False
    assert recovered_status["consecutive_refresh_failures"] == 0
    assert recovered_status["last_failure_class"] == ""
    assert recovered_status["cache_generation"] == 2


def test_operations_bundle_cannot_straddle_a_refresh_boundary(
    operations_board, monkeypatch
) -> None:
    server = operations_board
    before = server.operations_bundle()
    built = threading.Event()
    release = threading.Event()
    new_documents = tuple(
        {**deepcopy(document), "_test_generation": 2}
        for document in (
            server.snapshot(),
            server.graph(),
            server.context(),
            server.timeline(),
            server.pulse(),
            server.operations(),
        )
    ) + (dict(server._status_census),)

    def _delayed_build(_db_path):
        built.set()
        assert release.wait(timeout=5)
        return new_documents

    monkeypatch.setattr(board_server, "build_documents", _delayed_build)
    server._next_refresh = 0.0
    refresh = threading.Thread(target=server.service_actions, daemon=True)
    refresh.start()
    assert built.wait(timeout=5)

    # A bundle read while the next generation is being built sees the complete
    # old set, never a mix of documents selected by five independent requests.
    during = server.operations_bundle()
    assert during["cache_generation"] == before["cache_generation"] == 1
    assert {
        during[name].get("_test_generation", 1)
        for name in ("snapshot", "graph", "context", "timeline", "operations")
    } == {1}

    release.set()
    refresh.join(timeout=5)
    assert not refresh.is_alive()
    # The server's background refresher can win the non-blocking refresh lock
    # before the explicit service thread does. Wait for whichever caller won;
    # joining only the explicit thread can otherwise observe the old generation.
    assert server._refresh_run_lock.acquire(timeout=5)
    server._refresh_run_lock.release()
    after = server.operations_bundle()
    assert after["cache_generation"] == 2
    assert after["read_status"]["cache_generation"] == 2
    assert {
        after[name].get("_test_generation", 1)
        for name in ("snapshot", "graph", "context", "timeline", "operations")
    } == {2}


def test_operations_document_reselects_public_fields(operations_board) -> None:
    payload = json.dumps(operations_board.operations(), sort_keys=True)
    for withheld in (
        "body",
        "payload_json",
        "refs_json",
        "decision_text",
        "knowledge_text",
        "credential",
        "api_key",
    ):
        assert f'"{withheld}"' not in payload
