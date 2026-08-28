from __future__ import annotations

from contextlib import contextmanager
import http.client
from pathlib import Path
import threading

from coordharness import demo
from coordharness.board.server import make_server


@contextmanager
def _board(tmp_path: Path):
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    server = make_server(
        host="127.0.0.1",
        port=0,
        db_path=str(db),
        refresh_interval=3600,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=b"{}" if method == "POST" else None)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_options_advertises_post_only_for_scoped_usage_actions(tmp_path: Path) -> None:
    with _board(tmp_path) as port:
        for path in (
            "/",
            "/api/v1/snapshot",
            "/api/v1/query",
            "/api/v1/actions",
            "/mesh",
            "/map",
            "/ops",
        ):
            status, headers, body = _request(port, "OPTIONS", path)
            assert status == 204
            assert headers["Allow"] == "GET, HEAD, OPTIONS"
            assert body == b""

        status, headers, body = _request(port, "OPTIONS", "/api/v1/usage-actions")
        assert status == 204
        assert headers["Allow"] == "GET, HEAD, OPTIONS, POST"
        assert body == b""


def test_unrelated_post_retains_empty_read_only_405_contract(tmp_path: Path) -> None:
    with _board(tmp_path) as port:
        status, headers, body = _request(port, "POST", "/api/v1/query")
        assert status == 405
        assert headers["Allow"] == "GET, HEAD, OPTIONS"
        assert headers["Content-Length"] == "0"
        assert body == b""
