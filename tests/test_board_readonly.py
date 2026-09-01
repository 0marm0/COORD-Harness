"""The board is a read-only projection. These tests prove it by driving it.

Two things are easy to get wrong here and both have a test each way.

The first is scope. A read-only proof that visits a hand-written list of routes
is scoped by the thing it checks: add a route, forget the list, and the proof
still passes while the new route is the one that writes. So the route list is
read out of `BoardHandler.do_GET` itself, and the enumeration is checked against
a floor that it must exceed -- if the AST walk ever returns nothing, the floor
assertion fails instead of the sweep passing vacuously.

The second is what "unchanged" means for SQLite. A file whose size and mtime are
identical can still have been written to and checkpointed back, and a sibling
`-wal` or `-shm` that appears and is cleaned up leaves no trace in the main
file's stat. So the receipt carries the page count as well as the stat, and it
carries every file in the database's directory by name, not just `coord.db`.
"""

from __future__ import annotations

import ast
import http.client
import inspect
import json
from pathlib import Path
import shutil
import threading
from typing import Any, Iterator

import pytest

from coordharness import demo
from coordharness.board import server as board_server
from coordharness.board import snapshot as snapshot_module
from coordharness.board.server import make_server
from coordharness.coord import coord_db
from coordharness.coord.config import connect, connect_ro

# A floor, not the list under test. Its only job is to fail when the AST walk
# below stops finding routes: an enumeration that silently returns the empty set
# would otherwise turn this whole file green.
ROUTE_FLOOR = {
    "/healthz",
    "/api/v1/snapshot",
    "/api/v1/graph",
    "/api/v1/context",
    "/api/v1/timeline",
    "/api/menubar",
    "/",
    "/cockpit",
    "/static/app.css",
}

# Paths the server deliberately does not answer. Two of them are native client
# probes that were considered and left at 404 (see the note in server.py); the
# rest prove the sweep is discriminating rather than accepting everything.
UNROUTED = (
    "/api/state/compact?profile=native&plane=all",
    "/api/capability_inventory",
    "/api/v1/events",
    "/static/timeline-does-not-exist.js",
    "/nonsense",
)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# This status route is a bounded proxy over a separately deployed local service.
# With no deployment-owned upstream configured, its correct fail-soft response is
# 503; a configured test/development environment may answer 200 instead.
FAIL_SOFT_ROUTE_STATUSES = {
    "/api/v1/usage-actions/status": {200, 503},
    "/api/v1/provider-management": {200, 503},
}
ACTION_ROUTES = {"/api/v1/usage-actions", "/api/v1/provider-management"}


def _string_literals(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        found: set[str] = set()
        for element in node.elts:
            found |= _string_literals(element)
        return found
    return set()


def _routed_paths() -> set[str]:
    """Every path `BoardHandler.do_GET` answers, read out of `do_GET`.

    Handles the two forms the handler uses: `path == "..."` / `path in {...}`,
    and `path.startswith("/static/")`, which is expanded over the static
    allowlist because that is what decides which names behind the prefix exist.
    """
    tree = ast.parse(inspect.getsource(board_server))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "BoardHandler"
    )
    do_get = next(
        node
        for node in handler.body
        if isinstance(node, ast.FunctionDef) and node.name == "do_GET"
    )
    paths: set[str] = set()
    for node in ast.walk(do_get):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "path"
        ):
            for operator, comparator in zip(node.ops, node.comparators):
                if isinstance(operator, (ast.Eq, ast.In)):
                    paths |= _string_literals(comparator)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "path"
            and node.args
        ):
            for prefix in _string_literals(node.args[0]):
                paths |= {prefix + name for name in board_server._STATIC_ALLOWLIST}
    return paths


def _request(
    port: int, path: str, *, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def _page_count(db: Path, workdir: Path) -> tuple[int, int]:
    """Page count and page size, read from a private copy.

    Reading them from the live file would mean opening it, and an open is the
    thing under test: this measurement must not be able to create the `-shm`
    that a later assertion is looking for.
    """
    copied = workdir / "receipt.db"
    shutil.copyfile(db, copied)
    wal = Path(f"{db}-wal")
    if wal.exists():
        shutil.copyfile(wal, Path(f"{copied}-wal"))
    conn = connect_ro(copied)
    try:
        return (
            int(conn.execute("PRAGMA page_count").fetchone()[0]),
            int(conn.execute("PRAGMA page_size").fetchone()[0]),
        )
    finally:
        conn.close()
        copied.unlink(missing_ok=True)
        Path(f"{copied}-wal").unlink(missing_ok=True)


def _receipt(db: Path, workdir: Path) -> dict[str, Any]:
    # Directory first, so the copy taken for the page count cannot show up in it.
    directory = {
        entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns)
        for entry in sorted(db.parent.iterdir())
    }
    stamp = db.stat()
    pages, page_size = _page_count(db, workdir)
    return {
        "size": stamp.st_size,
        "mtime_ns": stamp.st_mtime_ns,
        "ctime_ns": stamp.st_ctime_ns,
        "page_count": pages,
        "page_size": page_size,
        "directory": directory,
    }


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[tuple[Any, Path, Path]]:
    state = tmp_path / "state"
    state.mkdir()
    db = state / "coord.db"
    demo.seed(db, quiet=True)
    workdir = tmp_path / "receipts"
    workdir.mkdir()
    # A refresh interval long enough that nothing rebuilds mid-sweep on its own;
    # the refresh path is driven explicitly in the test that covers it.
    server = make_server(port=0, db_path=str(db), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, db, workdir
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_every_route_is_enumerated_from_the_handler_not_from_a_list() -> None:
    routes = _routed_paths()
    missing = ROUTE_FLOOR - routes
    assert not missing, f"do_GET no longer answers {sorted(missing)}"
    # Strictly larger than the floor: the walk found routes nobody wrote down
    # here, which is the only evidence that it is reading the handler at all.
    assert len(routes) > len(ROUTE_FLOOR)
    assert all(path.startswith("/") for path in routes)


def test_coherent_bundle_receipt_honors_reproducible_build_clock(
    board, monkeypatch
) -> None:
    server, _db, _workdir = board
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1772442000")

    bundle = server.operations_bundle()

    assert bundle["generated_at"] == "2026-03-02T09:00:00Z"
    assert bundle["read_status"]["generated_at"] == bundle["generated_at"]


def test_background_refresh_never_blocks_the_request_accept_loop(
    board, monkeypatch
) -> None:
    server, _db, _workdir = board
    started = threading.Event()
    release = threading.Event()
    real_build_documents = board_server.build_documents

    def slow_build(db_path):
        started.set()
        assert release.wait(5), "test did not release the synthetic slow refresh"
        return real_build_documents(db_path)

    monkeypatch.setattr(board_server, "build_documents", slow_build)
    server._next_refresh = 0.0
    assert started.wait(2), "background refresh did not start"

    try:
        status, _headers, body = _request(server.server_port, "/healthz")
        assert status == 200
        assert body
    finally:
        release.set()


def test_driving_every_route_leaves_the_database_byte_identical(board) -> None:
    server, db, workdir = board
    routes = sorted(_routed_paths())
    assert ROUTE_FLOOR <= set(routes)

    before = _receipt(db, workdir)
    for path in routes:
        status, headers, body = _request(server.server_port, path)
        expected_statuses = FAIL_SOFT_ROUTE_STATUSES.get(path, {200})
        assert status in expected_statuses, f"GET {path} -> {status}"
        assert body, f"GET {path} returned an empty body"

        head_status, head_headers, head_body = _request(
            server.server_port, path, method="HEAD"
        )
        assert head_status == status, f"HEAD {path} -> {head_status}; GET was {status}"
        assert head_body == b"", f"HEAD {path} returned a body"
        # The point of HEAD is that the length is truthful without the payload.
        assert head_headers["Content-Length"] == headers["Content-Length"]
        assert int(head_headers["Content-Length"]) == len(body)

    after = _receipt(db, workdir)
    assert after == before
    # Named individually so a failure says which one appeared: either is proof
    # that something opened the live database for writing.
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_write_methods_are_refused_everywhere(board) -> None:
    server, db, workdir = board
    before = _receipt(db, workdir)
    for path in sorted(_routed_paths()):
        for method in WRITE_METHODS:
            status, headers, _body = _request(
                server.server_port, path, method=method
            )
            if path in ACTION_ROUTES and method == "POST":
                assert status == 403, f"{method} {path} -> {status}"
                continue
            assert status == 405, f"{method} {path} -> {status}"
            assert headers["Allow"] == "GET, HEAD, OPTIONS"
    assert _receipt(db, workdir) == before


def test_unrouted_paths_are_not_served(board) -> None:
    # Without this the sweep above proves nothing: a handler that answered
    # everything with 200 would pass it.
    server, _db, _workdir = board
    for path in UNROUTED:
        status, _headers, _body = _request(server.server_port, path)
        assert status == 404, f"GET {path} -> {status}"


def test_timeline_is_served_and_refreshes_with_the_other_documents(board) -> None:
    server, _db, _workdir = board
    status, headers, body = _request(server.server_port, "/api/v1/timeline")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    document = json.loads(body)
    assert document["schema_version"] == "TimelineV1"
    assert document == server.timeline()
    assert document["items"], "the seeded board posts events, so this is not vacuous"

    # Redaction, at the boundary rather than at the builder: whatever the
    # document holds, the withheld column names must not be in the served bytes.
    served = body.decode("utf-8")
    for column in (
        "title",
        "body",
        "refs_json",
        "payload_json",
        "session_id",
        "to_selector",
        "severity",
        "verdict",
        "trust",
    ):
        assert f'"{column}"' not in served


def test_a_failed_rebuild_swaps_no_document_at_all(board, monkeypatch) -> None:
    """The four documents move together or not at all.

    A refresh that built three documents and then failed on the fourth would
    leave the board serving a snapshot and a timeline from different states --
    a drawer opened on a row whose history is missing, or showing history for a
    row that is no longer in the list.
    """
    server, _db, _workdir = board
    originals = (
        server.snapshot(),
        server.graph(),
        server.context(),
        server.timeline(),
    )

    def _explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("timeline rebuild failed")

    monkeypatch.setattr(board_server, "build_timeline", _explode)
    server._next_refresh = 0.0
    server.service_actions()

    assert (
        server.snapshot(),
        server.graph(),
        server.context(),
        server.timeline(),
    ) == originals
    # And the ablation moved something: with the failure removed, the same call
    # does replace the documents, so the assertion above is not passing because
    # service_actions is inert.
    monkeypatch.undo()
    server._next_refresh = 0.0
    server.service_actions()
    assert server.timeline() is not originals[3]
    assert server.snapshot() is not originals[0]


def test_menubar_projection_carries_only_snapshot_facts(board) -> None:
    server, _db, _workdir = board
    status, _headers, body = _request(server.server_port, "/api/menubar")
    assert status == 200
    document = json.loads(body)
    snapshot = server.snapshot()

    assert document["source"] == snapshot["source"]
    assert document["stale"] == snapshot["stale"]
    work_model = document["work_model"]
    assert work_model["summary"] == {
        key: snapshot["summary"][key]
        for key in ("running", "attention", "next", "done", "total")
    }

    # The counts and the buckets come from one classifier, so they agree about
    # every row the snapshot carries. They are no longer the same number: the
    # counts are the census of the whole board while the rows are the operator
    # surface `_serves_operator_surface` selected, so a count may stand above a
    # shorter list -- that is how the menubar says what it is not showing. What
    # must never happen is the reverse. A list longer than its own count is a
    # bucket and a count that disagree, and then neither number explains the
    # other.
    carried = {"running": 0, "attention": 0, "next": 0}
    for row in snapshot["rows"]:
        status = (str(row.get("status") or "").strip().lower()) or "planned"
        if status in snapshot_module._RUNNING:
            carried["running"] += 1
        elif status in snapshot_module._ATTENTION:
            carried["attention"] += 1
        elif status not in snapshot_module._DONE:
            carried["next"] += 1
    for bucket, key in (
        ("running_rows", "running"),
        ("attention_rows", "attention"),
        ("next_rows", "next"),
    ):
        assert len(work_model[bucket]) == carried[key], bucket
        assert len(work_model[bucket]) <= snapshot["summary"][key], bucket
    assert work_model["running_rows"], "the seeded board has running work"

    rows_by_id = {row["id"]: row for row in snapshot["rows"]}
    allowed = {
        "id",
        "display",
        "status",
        "stale",
        "owner",
        "module",
        "current_step",
        "pct",
        "eta_s",
    }
    seen: set[str] = set()
    for bucket in ("running_rows", "attention_rows", "next_rows"):
        for row in work_model[bucket]:
            assert set(row) <= allowed, f"unexpected field in {bucket}: {set(row) - allowed}"
            source = rows_by_id[row["id"]]
            # Every value is the snapshot's own, so this route discloses nothing
            # that /api/v1/snapshot did not already publish.
            assert row["display"] == source["title"]
            assert row["status"] == source["status"]
            assert row["stale"] == source["stale"]
            if "pct" in row:
                assert row["pct"] == pytest.approx(source["progress_fraction"] * 100.0)
            seen.add(row["id"])

    # Finished rows are counted, never filed as work still to do.
    assert len(seen) == len(snapshot["rows"]) - snapshot["summary"]["done"]


def test_native_probes_that_were_left_unanswered_stay_unanswered(board) -> None:
    """`/api/state/compact` and `/api/capability_inventory` are decisions.

    Both were read against their Swift decoders and deliberately not served --
    the reasoning is in server.py. This pins the decision so that serving them
    later is a change someone makes on purpose rather than one that arrives with
    a stray route.
    """
    server, _db, _workdir = board
    for path in ("/api/state/compact", "/api/capability_inventory"):
        for method in ("GET", "HEAD"):
            status, _headers, _body = _request(
                server.server_port, path, method=method
            )
            assert status == 404


def test_do_get_dispatches_only_in_forms_the_walk_can_see() -> None:
    """The enumeration reads literals. Assert the handler only writes literals.

    `_routed_paths` understands two shapes: `path == "..."` / `path in {...}`,
    and `path.startswith("...")`. A route added as `path == ROUTES[name]`, or as
    a lookup in a dict, is invisible to it -- and invisible in the quiet
    direction, because the floor is a subset check and would still hold. The
    sweep would go on passing while the new route is the one that writes.

    So this fails on the *form* rather than on the count: every comparison
    against `path` in `do_GET` must carry at least one string literal, and
    `path` must never appear on the right-hand side of one, where the walk does
    not look.
    """
    tree = ast.parse(inspect.getsource(board_server))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "BoardHandler"
    )
    do_get = next(
        node
        for node in handler.body
        if isinstance(node, ast.FunctionDef) and node.name == "do_GET"
    )

    opaque: list[str] = []
    for node in ast.walk(do_get):
        if isinstance(node, ast.Compare):
            for operator, comparator in zip(node.ops, node.comparators):
                if isinstance(comparator, ast.Name) and comparator.id == "path":
                    opaque.append(f"line {node.lineno}: `path` on the right of a comparison")
                if not (isinstance(node.left, ast.Name) and node.left.id == "path"):
                    continue
                if isinstance(operator, (ast.Eq, ast.In)) and not _string_literals(comparator):
                    opaque.append(f"line {node.lineno}: compared against a non-literal")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "path"
        ):
            if not node.args or not _string_literals(node.args[0]):
                opaque.append(f"line {node.lineno}: startswith on a non-literal prefix")

    assert not opaque, (
        "do_GET dispatches in a form _routed_paths cannot read, so the read-only "
        "sweep below would skip those routes silently: " + "; ".join(opaque)
    )
    # Anti-vacuity: the walk this test guards must actually be finding routes.
    assert len(_routed_paths()) > len(ROUTE_FLOOR)


def test_the_four_documents_are_built_from_one_read_of_the_database(
    board, monkeypatch
) -> None:
    """Swapping together is not the same as agreeing.

    Each builder materializes its own copy of the database, so four calls read
    four different instants. A row or an event written between the snapshot
    build and the timeline build lands in one document and not the other, and
    the lock publishes that disagreement atomically instead of preventing it --
    the drawer opens a row from the snapshot and asks for history the row list
    does not explain, or the other way around.

    The write here is timed to land in exactly that window, and the control is
    a fresh read of the live database proving the write really happened. Absent
    from the served set, present in the database: that is the coherence claim,
    and it cannot pass by the write simply failing.
    """
    server, db, _workdir = board
    real_build_snapshot = board_server.build_snapshot
    written = {"done": False}

    def build_then_write(db_path: Any = None, **kwargs: Any) -> dict[str, Any]:
        document = real_build_snapshot(db_path, **kwargs)
        if not written["done"]:
            written["done"] = True
            conn = connect(db)
            try:
                coord_db.post_event(
                    conn,
                    kind="coherence_window_probe",
                    actor="claude",
                    work_id="ML-202",
                    idempotency_key="coherence-window",
                )
                conn.commit()
            finally:
                conn.close()
        return document

    monkeypatch.setattr(board_server, "build_snapshot", build_then_write)
    server._next_refresh = 0.0
    server.service_actions()
    monkeypatch.undo()

    assert written["done"], "the interleaved write never ran; nothing was tested"
    # Control: the event is in the live database, so its absence below is the
    # frozen read at work rather than a write that silently failed.
    fresh = {
        item["id"]: item["events"]
        for item in board_server.build_timeline(str(db))["items"]
    }
    assert any(event["kind"] == "coherence_window_probe" for event in fresh["ML-202"])

    served = {
        item["id"]: item["events"] for item in server.timeline()["items"]
    }
    snapshot_ids = {row["id"] for row in server.snapshot()["rows"]}
    assert set(served) <= snapshot_ids
    assert not any(
        event["kind"] == "coherence_window_probe"
        for event in served.get("ML-202", [])
    ), "the timeline carries an event the snapshot beside it could not have seen"


def test_hostile_event_prose_never_reaches_the_wire(board) -> None:
    """Redaction proven on the served bytes, with prose that fights back.

    The builder-level test covers the same ground; this one closes the boundary,
    because what is published is the response body and not the dict. The
    sentinel goes into every withheld column at once -- including the ones a
    naive escape would mangle rather than remove -- and the assertion is on the
    decoded response text, so an HTML-escaped or unicode-escaped copy of it
    would still be caught.
    """
    server, db, _workdir = board
    sentinel = "CANARY-9f31b2-withheld"
    hostile = f'<script>alert("x")</script> {sentinel} </title> \\u0000'

    conn = connect(db)
    try:
        coord_db.post_event(
            conn,
            kind="note",
            actor="claude",
            work_id="ML-202",
            title=hostile,
            body=hostile,
            refs_json=json.dumps([hostile]),
            payload_json=json.dumps({"plan": hostile}),
            session_id=hostile,
            to_selector=hostile,
            severity="high",
            verdict="pass",
        )
        conn.commit()
    finally:
        conn.close()

    server._next_refresh = 0.0
    server.service_actions()

    status, _headers, body = _request(server.server_port, "/api/v1/timeline")
    assert status == 200
    served = body.decode("utf-8")
    document = json.loads(served)

    # Not vacuous: the hostile event is in the document, as occurrence.
    events = {item["id"]: item["events"] for item in document["items"]}["ML-202"]
    assert any(event["kind"] == "note" for event in events)

    assert sentinel not in served
    assert "<script" not in served
    assert "alert(" not in served
    # Matched as JSON keys, not as bare substrings. `audit_verdict` is a
    # legitimate event *kind* on the seeded board, so a substring test for
    # "verdict" now fails on a value the document is entitled to carry while
    # saying nothing about the column it is meant to defend.
    for column in ("severity", "verdict", "trust", "session_id", "to_selector"):
        assert f'"{column}"' not in served
