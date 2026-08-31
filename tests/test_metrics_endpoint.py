"""`/metrics` exports counts the board's own projection already derives.

Nothing this route reports is a new business fact: the status buckets are
`snapshot.summary`, the lease figure is `context.items[].lease_remaining_s`,
and the run/heartbeat figures are `runs.state` / `agent_sessions.last_heartbeat`
read through the same `stable_copy` + `connect_ro` pattern every other board
document already uses. This file proves the route is reachable, guarded by
the same Host/Origin middleware as every other route, never caches, and that
every line it emits parses as Prometheus text exposition format.
"""

from __future__ import annotations

import http.client
import re
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from coordharness import demo
from coordharness.board.server import make_server

_METRIC_LINE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<labels>[^}]*)\})? (?P<value>\S+)$'
)


def _request(
    port: int, path: str, *, method: str = "GET", headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def _parse_prometheus_text(body: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """A minimal parser: enough to check names, labels and value types.

    Not a Prometheus client -- this repo has no dependency on one -- but
    strict about the exposition grammar (`# HELP` before `# TYPE` before the
    samples, every non-comment line matching name{labels} value) so a
    malformed line fails the test rather than being silently skipped.
    """
    series: dict[str, list[tuple[dict[str, str], float]]] = {}
    help_seen: set[str] = set()
    type_seen: set[str] = set()
    for line in body.splitlines():
        if not line:
            continue
        if line.startswith("# HELP "):
            help_seen.add(line.split()[2])
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            type_seen.add(name)
            assert kind == "gauge", f"{name} is not exported as a gauge: {kind!r}"
            continue
        assert not line.startswith("#"), f"unrecognized comment line: {line!r}"
        match = _METRIC_LINE.match(line)
        assert match, f"line does not parse as Prometheus exposition text: {line!r}"
        name = match.group("name")
        assert name in type_seen, f"{name} sample appears with no preceding # TYPE"
        assert name in help_seen, f"{name} sample appears with no preceding # HELP"
        labels: dict[str, str] = {}
        raw_labels = match.group("labels")
        if raw_labels:
            for pair in raw_labels.split(","):
                key, _, value = pair.partition("=")
                assert value.startswith('"') and value.endswith('"'), (
                    f"unquoted label value in {line!r}"
                )
                labels[key] = value[1:-1]
        series.setdefault(name, []).append((labels, float(match.group("value"))))
    return series


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[tuple[Any, Path]]:
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    server = make_server(port=0, db_path=str(db), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_metrics_returns_200_text_with_the_expected_gauges(board) -> None:
    server, _db = board
    status, headers, body = _request(server.server_port, "/metrics")

    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert headers["Cache-Control"] == "no-store"
    text = body.decode("utf-8")

    series = _parse_prometheus_text(text)
    assert set(series) == {
        "coordharness_board_rows_by_status",
        "coordharness_expired_leases_total",
        "coordharness_runs_by_state",
        "coordharness_session_heartbeat_age_seconds",
    }


def test_status_buckets_agree_with_the_cached_snapshot_summary(board) -> None:
    server, _db = board
    _status, _headers, body = _request(server.server_port, "/metrics")
    series = _parse_prometheus_text(body.decode("utf-8"))
    summary = server.snapshot()["summary"]

    by_status = {labels["status"]: value for labels, value in series["coordharness_board_rows_by_status"]}
    assert by_status == {
        bucket: float(summary[bucket]) for bucket in ("running", "attention", "next", "done")
    }
    assert sum(by_status.values()) == summary["total"]


def test_run_states_are_reported_for_both_tracked_states(board) -> None:
    server, _db = board
    _status, _headers, body = _request(server.server_port, "/metrics")
    series = _parse_prometheus_text(body.decode("utf-8"))

    by_state = {labels["state"]: value for labels, value in series["coordharness_runs_by_state"]}
    assert set(by_state) == {"live", "orphaned"}
    assert all(value >= 0 for value in by_state.values())


def test_every_active_session_gets_a_heartbeat_age_sample(board) -> None:
    server, db = board
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        active_sessions = {
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM agent_sessions WHERE state='active'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert active_sessions, "the seeded demo board has no active sessions; nothing was tested"

    _status, _headers, body = _request(server.server_port, "/metrics")
    series = _parse_prometheus_text(body.decode("utf-8"))
    heartbeat_series = series["coordharness_session_heartbeat_age_seconds"]

    seen = {labels["session_id"]: value for labels, value in heartbeat_series}
    assert set(seen) == active_sessions
    for session_id, age in seen.items():
        assert age >= 0, f"{session_id} reported a negative heartbeat age: {age}"


def test_expired_lease_count_is_a_nonnegative_integer_gauge(board) -> None:
    server, _db = board
    _status, _headers, body = _request(server.server_port, "/metrics")
    series = _parse_prometheus_text(body.decode("utf-8"))

    (labels, value), = series["coordharness_expired_leases_total"]
    assert labels == {}
    assert value >= 0
    assert value == int(value)


def test_metrics_never_writes_to_the_live_database(board) -> None:
    server, db = board
    before = (db.stat().st_size, db.stat().st_mtime_ns)

    for _ in range(3):
        status, _headers, _body = _request(server.server_port, "/metrics")
        assert status == 200

    after = (db.stat().st_size, db.stat().st_mtime_ns)
    assert after == before
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_metrics_obeys_the_same_host_middleware_as_every_other_route(board) -> None:
    server, _db = board
    status, _headers, body = _request(
        server.server_port, "/metrics", headers={"Host": "evil.example"}
    )
    assert status == 403
    assert b"forbidden host" in body


def test_metrics_rejects_write_methods_like_every_other_route(board) -> None:
    server, _db = board
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        status, headers, _body = _request(server.server_port, "/metrics", method=method)
        assert status == 405, f"{method} /metrics -> {status}"
        assert headers["Allow"] == "GET, HEAD, OPTIONS"


def test_head_matches_get_content_length(board) -> None:
    server, _db = board
    get_status, get_headers, get_body = _request(server.server_port, "/metrics")
    head_status, head_headers, head_body = _request(
        server.server_port, "/metrics", method="HEAD"
    )
    assert head_status == get_status
    assert head_body == b""
    assert head_headers["Content-Length"] == get_headers["Content-Length"]
    assert int(get_headers["Content-Length"]) == len(get_body)
