from __future__ import annotations

from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import pytest

from types import SimpleNamespace

from coordharness.bootstrap import bootstrap_database
from coordharness import demo
from coordharness import entry
from coordharness.board.server import make_server
from coordharness.board import snapshot as snapshot_module
from coordharness.board.snapshot import build_snapshot, load_schema, validate_snapshot
from coordharness.coord import coord_db
from coordharness.coord.config import connect


REPO = Path(__file__).resolve().parents[1]
TOP_KEYS = {
    "schema_version", "generated_at", "source", "stale", "summary", "rows", "sessions",
}
ROW_KEYS = {
    "id", "title", "status", "bucket", "owner", "module", "group", "priority",
    "progress_fraction", "eta_seconds", "stale", "current_step",
}


def _request(port: int, path: str, *, method: str = "GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers=headers or {})
    response = conn.getresponse()
    body = response.read()
    result = (response.status, dict(response.getheaders()), body)
    conn.close()
    return result


def _tree_receipt(root: Path) -> dict[str, tuple]:
    receipt: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        value = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISREG(value.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            kind = "file"
        elif stat.S_ISDIR(value.st_mode):
            digest = None
            kind = "dir"
        elif stat.S_ISLNK(value.st_mode):
            digest = os.readlink(path)
            kind = "link"
        else:
            digest = None
            kind = "other"
        receipt[relative] = (
            kind,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_ino,
            digest,
        )
    return receipt


def test_native_snapshot_matches_packaged_schema_and_swift_fixture(
    tmp_path: Path,
    monkeypatch,
):
    state = tmp_path / "state"
    db = state / "coord.db"
    monkeypatch.setenv("COORD_HOME", str(state))
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn,
            "WORK-1",
            title="Portable contract row",
            assignee="local",
            module="runtime",
            priority=2,
        )
    finally:
        conn.close()

    schema = load_schema()
    fixture = json.loads((REPO / "apps" / "Fixtures" / "snapshot-v1.json").read_text())
    validate_snapshot(fixture, schema)
    assert schema["properties"]["schema_version"]["const"] == "1"
    assert set(fixture) == TOP_KEYS
    assert set(fixture["rows"][0]) == ROW_KEYS

    snapshot = build_snapshot(db)
    validate_snapshot(snapshot, schema)
    assert set(snapshot) == TOP_KEYS
    assert snapshot["schema_version"] == "1"
    assert all(set(row) == ROW_KEYS for row in snapshot["rows"])
    parsed = datetime.fromisoformat(snapshot["generated_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert not any(
        key in json.dumps(snapshot)
        for key in ("done_signal", "depends_on", "event_id", "command_sha256")
    )


def test_seeded_snapshots_are_byte_equivalent_with_source_date_epoch(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1772442000")
    snapshots = []
    for name in ("first", "second"):
        state = tmp_path / name
        monkeypatch.setenv("COORD_HOME", str(state))
        db = state / "coord.db"
        demo.seed(db, quiet=True)
        snapshots.append(
            json.dumps(build_snapshot(db), sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
    assert snapshots[0] == snapshots[1]
    payload = json.loads(snapshots[0])
    assert payload["generated_at"] == "2026-03-02T09:00:00.000Z"


def test_distinct_jobs_for_one_work_remain_distinct_rows(
    tmp_path: Path,
    monkeypatch,
):
    state = tmp_path / "state"
    db = state / "coord.db"
    monkeypatch.setenv("COORD_HOME", str(state))
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(conn, "SHARED-1", title="Shared work")
    finally:
        conn.close()
    monkeypatch.setattr(
        snapshot_module,
        "load_snapshot",
        lambda _path: SimpleNamespace(
            items=(
                {"job_id": "attempt-done", "roadmap_id": "SHARED-1", "state": "done"},
                {"job_id": "attempt-failed", "roadmap_id": "SHARED-1", "state": "failed"},
            )
        ),
    )
    snapshot = build_snapshot(db)
    jobs = [row for row in snapshot["rows"] if row["id"].startswith("job:")]
    assert [(row["id"], row["status"]) for row in jobs] == [
        ("job:attempt-done", "done"),
        ("job:attempt-failed", "failed"),
    ]
    assert snapshot["summary"] == {
        "running": 0,
        "attention": 1,
        "next": 1,
        "done": 1,
        "total": 3,
    }


def test_snapshot_get_is_strictly_side_effect_free(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    db = state / "coord.db"
    monkeypatch.setenv("COORD_HOME", str(state))
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(conn, "WORK-1", title="Read-only board row", assignee="local")
    finally:
        conn.close()
    assert not (state / "job_progress").exists()

    before_tree = _tree_receipt(state)
    before_db_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    before_db_stat = db.stat()
    server = make_server(port=0, db_path=str(db), refresh_interval=3600)
    server._next_refresh = 0
    server.service_actions()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for method in ("GET", "GET", "HEAD"):
            status, _headers, body = _request(
                server.server_port, "/api/v1/snapshot", method=method
            )
            assert status == 200
            if method == "GET":
                validate_snapshot(json.loads(body))

        after_db_stat = db.stat()
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before_db_hash
        assert (
            after_db_stat.st_size,
            after_db_stat.st_mtime_ns,
            after_db_stat.st_ctime_ns,
            after_db_stat.st_ino,
            after_db_stat.st_mode,
        ) == (
            before_db_stat.st_size,
            before_db_stat.st_mtime_ns,
            before_db_stat.st_ctime_ns,
            before_db_stat.st_ino,
            before_db_stat.st_mode,
        )
        assert _tree_receipt(state) == before_tree
        assert not (state / "job_progress").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_coord_board_is_byte_for_byte_read_only(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    state = tmp_path / "state"
    db = state / "coord.db"
    monkeypatch.setenv("COORD_HOME", str(state))
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(conn, "CLI-READ-1", title="CLI read-only row")
    finally:
        conn.close()
    before_tree = _tree_receipt(state)
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    before_stat = db.stat()
    assert entry.main(["--db", str(db), "board"]) == 0
    first = capsys.readouterr().out
    assert entry.main(["--db", str(db), "board"]) == 0
    second = capsys.readouterr().out
    after_stat = db.stat()
    assert first == second
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    assert _tree_receipt(state) == before_tree
    assert (
        after_stat.st_size,
        after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
        after_stat.st_ino,
        after_stat.st_mode,
    ) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
        before_stat.st_ino,
        before_stat.st_mode,
    )


def test_board_http_ui_api_and_security(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    db = state / "coord.db"
    monkeypatch.setenv("COORD_HOME", str(state))
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn,
            "GRAPH-1",
            title="Graph source",
            depends_on='["MISSING-GRAPH"]',
        )
    finally:
        conn.close()
    server = make_server(port=0, db_path=str(db))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        status, headers, body = _request(port, "/")
        assert status == 200
        html = body.decode("utf-8")
        # The destinations are rendered by the client from one list now, so the
        # served document carries the panels rather than the labels. Assert on
        # what the server actually guarantees: every destination has a mount
        # point, including Attention, which used to be a card on Overview
        # showing eight of several hundred rows.
        for panel in ("attention", "overview", "work", "jobs", "graph", "comms", "usage"):
            assert f'id="{panel}" class="panel' in html, f"no mount point for {panel}"
        assert 'id="detail"' in html
        # The rail and the `activity` panel were removed: the rail shipped
        # `hidden` while a second render function kept painting it, and nothing
        # ever wrote to `#activity`. Assert their absence rather than dropping
        # the check, so a re-added dead destination has to argue for itself.
        assert 'id="rail"' not in html
        assert 'id="activity"' not in html
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert headers["X-Frame-Options"] == "DENY"

        status, _headers, body = _request(port, "/api/v1/snapshot")
        assert status == 200
        payload = json.loads(body)
        validate_snapshot(payload)
        assert payload["schema_version"] == "1"
        assert set(payload) == TOP_KEYS

        status, _headers, body = _request(port, "/api/v1/graph")
        assert status == 200
        graph = json.loads(body)
        missing_edges = [
            edge for edge in graph["edges"]
            if edge["relationship_state"] == "missing_target"
        ]
        assert missing_edges == [{
            "id": "depends_on:work:GRAPH-1:work:MISSING-GRAPH",
            "kind": "depends_on",
            "relationship_state": "missing_target",
            "source": "work:GRAPH-1",
            "source_field": "work_items.depends_on",
            "target": "work:MISSING-GRAPH",
        }]

        status, _headers, body = _request(port, "/api/v1/schema")
        assert status == 200
        assert json.loads(body)["additionalProperties"] is False

        status, _headers, body = _request(port, "/healthz")
        assert status == 200 and json.loads(body)["ok"] is True

        status, _headers, _body = _request(port, "/dashboard.html")
        assert status == 404
        status, _headers, _body = _request(port, "/api/v1/snapshot", method="POST")
        assert status == 405
        status, _headers, _body = _request(
            port, "/api/v1/snapshot", headers={"Host": "attacker.invalid"}
        )
        assert status == 403
        status, _headers, _body = _request(
            port, "/api/v1/snapshot", headers={"Origin": "http://attacker.invalid"}
        )
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_make_server_enforces_remote_opt_in_at_library_boundary(tmp_path: Path):
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    remote_host = ".".join(("0", "0", "0", "0"))

    with pytest.raises(ValueError, match="allow_remote=True"):
        make_server(host=remote_host, port=0, db_path=str(db))

    with pytest.raises(ValueError, match="explicit allowed host"):
        make_server(
            host=remote_host,
            port=0,
            db_path=str(db),
            allow_remote=True,
        )


def test_board_graph_renderer_static_contract():
    script = (REPO / "src" / "coordharness" / "board" / "static" / "app.js").read_text()
    styles = (REPO / "src" / "coordharness" / "board" / "static" / "app.css").read_text()

    for token in (
        "function normalizeGraph(graph)",
        "function layoutGraph(graph)",
        "function graphEdgePath(source,target,index)",
        '<svg viewBox="0 0 ${layout.width} ${layout.height}" role="img"',
        '<path class="gedge${missing?" missing":""}"',
        'marker-end="url(#gtip)"',
        'aria-labelledby="graph-svg-title graph-svg-description"',
        'aria-label="Source-bound relationship edge provenance"',
        "No graph nodes or relationships.",
        "No recorded relationships.",
    ):
        assert token in script
    assert ".gedge.missing" in styles
    assert ".gnode.missing circle" in styles
    assert "stroke-dasharray" in styles


def test_board_graph_renderer_draws_truthful_stable_accessible_svg():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the dependency-free board JavaScript unit test")

    source = REPO / "src" / "coordharness" / "board" / "static" / "app.js"
    javascript = r'''
const fs=require("fs");
const vm=require("vm");
const context={};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);

function deepFreeze(value){
  if(value&&typeof value==="object"&&!Object.isFrozen(value)){
    Object.freeze(value);
    Object.values(value).forEach(deepFreeze);
  }
  return value;
}
const graph={
  nodes:[
    {id:"work:B",kind:"work",label:"B",missing:false},
    {id:"work:A",kind:"work",label:"A",missing:false},
    {id:"work:M",kind:"missing_work",label:"Missing work: M",missing:true},
  ],
  edges:[
    {
      id:"edge:missing",source:"work:B",target:"work:M",kind:"depends_on",
      relationship_state:"missing_target",source_field:"work_items.depends_on",
    },
    {
      id:"edge:bound",source:"work:A",target:"work:B",kind:"depends_on",
      relationship_state:"source_bound",source_field:"work_items.depends_on",
    },
  ],
};
const before=JSON.stringify(graph);
deepFreeze(graph);
const first=context.layoutGraph(graph);
const reversed=context.layoutGraph({
  nodes:[...graph.nodes].reverse(),
  edges:[...graph.edges].reverse(),
});
const positions=layout=>Object.fromEntries(
  layout.nodes.map(item=>[item.id,{x:item.x,y:item.y,rank:item.rank}]),
);
const html=context.renderGraph(graph);

const missingEndpointGraph={
  nodes:[{id:"work:A",kind:"work",label:"A",missing:false}],
  edges:[{
    id:"edge:absent",source:"work:A",target:"work:ABSENT",kind:"depends_on",
    relationship_state:"missing_target",source_field:"work_items.depends_on",
  }],
};
const missingBefore=JSON.stringify(missingEndpointGraph);
deepFreeze(missingEndpointGraph);
const missingHtml=context.renderGraph(missingEndpointGraph);
const emptyHtml=context.renderGraph({nodes:[],edges:[]});

console.log(JSON.stringify({
  positions:positions(first),
  stable:JSON.stringify(positions(first))===JSON.stringify(positions(reversed)),
  unchanged:before===JSON.stringify(graph),
  edgePaths:(html.match(/<path class="gedge/g)||[]).length,
  boundPath:html.includes('data-edge-id="edge:bound"')&&
    html.includes('data-source="work:A" data-target="work:B"'),
  missingPath:html.includes('class="gedge missing"')&&
    html.includes('data-edge-id="edge:missing"'),
  missingNode:html.includes('class="gnode missing"')&&
    html.includes('data-node-id="work:M"'),
  tablePreserved:html.includes(">source_bound</td>")&&
    html.includes(">missing_target</td>")&&
    html.includes("work_items.depends_on"),
  svgLabelled:html.includes(
    'role="img" aria-labelledby="graph-svg-title graph-svg-description"',
  )&&html.includes('<title id="graph-svg-title">Source-bound dependency graph</title>')&&
    html.includes('<desc id="graph-svg-description">'),
  edgeLabelled:html.includes(
    'aria-label="work:A depends_on work:B; source_bound; derived from work_items.depends_on"',
  ),
  tableLabelled:html.includes(
    'aria-label="Source-bound relationship edge provenance"',
  ),
  synthesizedMissing:missingHtml.includes('data-node-id="work:ABSENT"')&&
    missingHtml.includes('class="gnode missing"')&&
    missingHtml.includes('class="gedge missing"')&&
    missingHtml.includes("missing endpoint"),
  synthesizedUnchanged:missingBefore===JSON.stringify(missingEndpointGraph),
  emptyState:emptyHtml.includes("No graph nodes or relationships.")&&
    emptyHtml.includes("No recorded relationships.")&&!emptyHtml.includes("<svg"),
}));
'''
    completed = subprocess.run(
        [node, "-e", javascript, str(source)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(completed.stdout)
    assert receipt == {
        "positions": {
            # Geometry tightened so a board with tens of rows per rank still
            # fits: margins 200/60 and a 520 column gap, down from 240/70/720.
            "work:A": {"x": 200, "y": 60, "rank": 0},
            "work:B": {"x": 720, "y": 60, "rank": 1},
            "work:M": {"x": 1240, "y": 60, "rank": 2},
        },
        "stable": True,
        "unchanged": True,
        "edgePaths": 2,
        "boundPath": True,
        "missingPath": True,
        "missingNode": True,
        "tablePreserved": True,
        "svgLabelled": True,
        "edgeLabelled": True,
        "tableLabelled": True,
        "synthesizedMissing": True,
        "synthesizedUnchanged": True,
        "emptyState": True,
    }


def test_context_document_carries_structure_and_no_prose(tmp_path: Path) -> None:
    """ContextV1 makes the graph navigable without widening what is disclosed.

    The board is read-only and unauthenticated, and has never carried event
    bodies, decisions or knowledge text. This asserts both halves: the relations
    that make navigation possible are present, and the prose fields that were
    deliberately withheld stay withheld. Without the second half the endpoint
    could grow a leak and still pass.
    """
    from coordharness import demo
    from coordharness.board.snapshot import build_context

    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    document = build_context(db)

    assert document["schema_version"] == "ContextV1"
    items = {item["id"]: item for item in document["items"]}
    assert items, "the demo board should produce context"

    # Structure that navigation needs, both directions.
    assert "ML-202" in items["ML-203"]["depends_on"]
    assert "ML-203" in items["ML-202"]["dependents"]
    assert items["ML-201"]["parent"] == "INIT-MODEL"
    assert "ML-201" in items["INIT-MODEL"]["children"]
    assert "ML-202" in items["ML-201"]["siblings"]
    assert items["ML-201"]["done_signal"].endswith("ml-201.md")

    # Prose the public surface withholds. `note` is the body of what an agent
    # wrote; the snapshot exposes a current step and nothing more.
    withheld = {"note", "note_text", "why_text", "events", "decisions", "knowledge"}
    for item in document["items"]:
        assert not (withheld & set(item)), f"{item['id']} carries withheld fields"


def test_cockpit_neighbourhood_and_global_search_are_deterministic() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the dependency-free cockpit unit test")
    static = REPO / "src" / "coordharness" / "board" / "static"
    neighbourhood = static / "neighbourhood.js"
    search = static / "search.js"
    javascript = r'''
const fs = require("fs");
const vm = require("vm");
const context = { fetch: undefined };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const nodes = ["A", "B", "C", "D", "I"].map(id => ({ id }));
const edges = [
  { source: "A", target: "B", kind: "depends_on" },
  { source: "B", target: "C", kind: "parent" },
  { source: "C", target: "D", kind: "runtime_evidence" },
  { source: "D", target: "missing", kind: "evidence" },
];
const before = JSON.stringify({ nodes, edges });
const cut = (id, hops) => {
  const value = context.coordNeighbourhood(nodes, edges, id, hops);
  return {
    nodes: value.nodes.map(item => item.id),
    edges: value.edges.map(item => `${item.source}:${item.target}`),
    center: value.root,
    hops: value.hops,
  };
};
const rows = [
  { id: "UI-200", title: "Ordinary row", owner: "codex", current_step: "refine launch" },
  { id: "UI-100", title: "Launch controls", owner: "claude", current_step: "ordinary work" },
  { id: "OPS-1", title: "Another launch", owner: "local", current_step: "idle" },
];
const rank = query => context.coordSearchRank(rows, query).map(item => [item.row.id, item.field]);
console.log(JSON.stringify({
  zero: cut("A", 0),
  one: cut("A", 1),
  two: cut("A", 2),
  isolated: cut("I", 3),
  full: cut(null, 2),
  unchanged: before === JSON.stringify({ nodes, edges }),
  exact: rank("ui-200"),
  fieldOrder: rank("launch"),
  caseInsensitive: rank("CLAUDE"),
  empty: rank(""),
}));
'''
    completed = subprocess.run(
        [node, "-e", javascript, str(neighbourhood), str(search)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(completed.stdout)
    assert receipt == {
        "zero": {"nodes": ["A"], "edges": [], "center": "A", "hops": 0},
        "one": {"nodes": ["A", "B"], "edges": ["A:B"], "center": "A", "hops": 1},
        "two": {
            "nodes": ["A", "B", "C"],
            "edges": ["A:B", "B:C"],
            "center": "A",
            "hops": 2,
        },
        "isolated": {"nodes": ["I"], "edges": [], "center": "I", "hops": 3},
        "full": {
            "nodes": ["A", "B", "C", "D", "I"],
            "edges": ["A:B", "B:C", "C:D", "D:missing"],
            "center": None,
            "hops": None,
        },
        "unchanged": True,
        "exact": [["UI-200", "exact id"]],
        "fieldOrder": [["OPS-1", "title"], ["UI-100", "title"], ["UI-200", "step"]],
        "caseInsensitive": [["UI-100", "owner"]],
        "empty": [],
    }

    script = neighbourhood.read_text(encoding="utf-8")
    page = static.joinpath("cockpit.html").read_text(encoding="utf-8")
    assert "showing ${safe(shown)} of ${safe(total)} nodes" in script
    assert 'src="/static/neighbourhood.js"' in page
    assert 'src="/static/search.js"' in page
    assert 'id="global-search"' in page


def test_no_surface_relies_on_inline_style(tmp_path: Path) -> None:
    """The board forbids inline style, so nothing served may depend on it.

    This is a regression test for a silent, total failure. Two view modules
    injected their stylesheets as `<style>` elements and set bar widths through
    `style=` attributes. The board sends `style-src 'self'` with no
    unsafe-inline, so the browser dropped both: `styleElement.sheet` was null
    and every rule was lost. Nothing errored, nothing logged, and the views
    rendered as unstyled markup that still looked deliberate.

    Setting styles through the CSSOM from script is permitted and is how the
    accent switch works; parsed inline style is not.
    """
    import re
    from coordharness.board.security import SECURITY_HEADERS

    policy = SECURITY_HEADERS["Content-Security-Policy"]
    assert "unsafe-inline" not in policy, "the policy this test defends has been weakened"

    static = Path(__file__).resolve().parents[1] / "src/coordharness/board/static"
    served = sorted(static.glob("*.js")) + sorted(static.glob("*.html"))
    assert served, "no static assets found to check"

    offenders: list[str] = []
    for path in served:
        text = path.read_text(encoding="utf-8")
        # A style attribute written into markup, as opposed to `.style.foo = ...`
        # which is CSSOM and allowed.
        for match in re.finditer(r'style\s*=\s*"[^"]*"', text):
            if "stylesheet" in match.group(0):
                continue
            offenders.append(f"{path.name}: {match.group(0)[:60]}")
        if re.search(r'createElement\(\s*["\']style["\']\s*\)', text):
            offenders.append(f"{path.name}: injects a <style> element")

    assert not offenders, "inline style is dropped by the policy: " + "; ".join(offenders)


def test_timeline_document_carries_occurrence_and_redacts_prose(tmp_path: Path) -> None:
    """TimelineV1 publishes when a row moved, and nothing about what was said.

    Two halves, and the second is the one that matters. Asserting only that the
    required keys are present would pass a document that also carried a `body`
    under some other name -- a renamed leak reads as a new field, not as a
    regression. So this also plants a sentinel in the `title` and `body` of a
    real event and asserts the string appears nowhere in the serialised
    document, which catches a leak regardless of what key it arrives under.

    The board is read-only and unauthenticated: occurrence and kind are public,
    prose is not.
    """
    from coordharness.board.snapshot import build_timeline

    sentinel = "CANARY-a2f4c1-do-not-serve"
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    conn = connect(db)
    try:
        coord_db.post_event(
            conn,
            kind="note",
            actor="codex",
            work_id="ML-202",
            title=f"title {sentinel}",
            body=f"body {sentinel}",
        )
        # An estate-wide event with no work item. A timeline is per row, so this
        # must be dropped rather than pooled under a placeholder id.
        coord_db.post_event(
            conn,
            kind="note",
            actor="service",
            title="sweep",
            body=f"unattached {sentinel}",
        )
    finally:
        conn.close()

    document = build_timeline(db)

    assert document["schema_version"] == "TimelineV1"
    assert document["source"] == "coord.db"
    datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))

    items = {item["id"]: item for item in document["items"]}
    assert items, "the demo board should produce a timeline"
    assert "ML-202" in items

    # Half one: the occurrence facts the surface exists to carry.
    for item in document["items"]:
        assert set(item) == {"id", "events"}
        assert isinstance(item["id"], str) and item["id"]
        assert item["events"], f"{item['id']} has no events"
        for event in item["events"]:
            assert set(event) == {"at", "kind", "actor"}
            assert isinstance(event["kind"], str) and event["kind"]
            assert isinstance(event["actor"], str)
            datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
        stamps = [event["at"] for event in item["events"]]
        assert stamps == sorted(stamps), f"{item['id']} events are not ascending"

    # Deterministic ordering of the items themselves, so two reads of an
    # unchanged board serialise identically. `generated_at` is the read clock
    # and moves by design, so the comparison is over the payload.
    ids = [item["id"] for item in document["items"]]
    assert ids == sorted(ids)
    assert json.dumps(document["items"]) == json.dumps(build_timeline(db)["items"])

    # The seeded board attributes by lane, and the posted event is present as an
    # occurrence -- proving the redaction below is not just an empty document.
    # Counted rather than pinned to a literal: the seeder writes a recorded
    # history, and a test that pinned its length would fail on every honest
    # change to the fixture instead of on a leak.
    assert len(items["ML-202"]["events"]) >= 2
    assert {"claude", "codex"} <= {
        event["actor"] for event in items["ML-202"]["events"]
    }
    # The vocabulary is varied, so a kind-aware reader of this document is not
    # looking at one colour. This is the seeder's contract, asserted here
    # because this is the document that publishes kinds.
    served_kinds = {
        event["kind"] for item in document["items"] for event in item["events"]
    }
    assert {"handoff", "audit_request", "audit_verdict", "note"} <= served_kinds

    # Half two: the leak canary. The sentinel was written into `title` and
    # `body` of an event that IS in the document, so any path that carries
    # prose -- under any key name, at any depth -- fails here.
    serialised = json.dumps(document)
    assert sentinel not in serialised, "TimelineV1 leaked event prose"

    # The unattached event is excluded outright, not rehomed.
    assert "" not in items
    # Every item names a row the board actually carries. Pinned as a subset of
    # the snapshot rather than as a fixed list of ids, because the seeded
    # history now touches most of the board and the property under test was
    # never "these five rows" -- it was "no id that is not a row".
    carried = {row["id"] for row in build_snapshot(db)["rows"]}
    for item in document["items"]:
        assert item["id"] in carried, f"unexpected timeline row {item['id']}"

    # And the withheld columns are absent by name as well, so a future edit that
    # adds one is caught even before it happens to carry a sentinel value.
    withheld = {
        "title", "body", "refs_json", "payload_json", "session_id",
        "to_selector", "severity", "verdict", "trust", "refs", "payload",
    }
    for item in document["items"]:
        assert not (withheld & set(item))
        for event in item["events"]:
            assert not (withheld & set(event))


def test_timeline_reads_without_writing_beside_the_source(tmp_path: Path) -> None:
    """The timeline is a read. It must not touch the database it reads.

    build_snapshot and build_graph go through the materialised copy for this
    reason; a sibling that opened the live file directly would still return the
    right answer while leaving a -wal beside a database someone else owns.
    """
    from coordharness.board.snapshot import build_timeline

    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    before = {path.name: path.stat().st_mtime_ns for path in sorted(tmp_path.iterdir())}

    build_timeline(db)

    after = {path.name: path.stat().st_mtime_ns for path in sorted(tmp_path.iterdir())}
    assert after == before, "build_timeline mutated the source directory"

    with pytest.raises(FileNotFoundError):
        build_timeline(tmp_path / "absent.db")


def test_timeline_names_only_rows_the_board_actually_has(tmp_path: Path) -> None:
    """A timeline item id is a claim that the board carries that row.

    `events.work_id` has no foreign key, so an event can name a work item that
    does not exist -- a mistyped selector, an id copied from another board, a
    work item deleted after its events were written. Grouping by `work_id`
    alone publishes those as items, and an item is an assertion: the drawer
    opens rows by id, so a client is told to expect a row that is not in the
    row list and opens an empty one.

    The control matters as much as the assertion. A test that only checked the
    subset relation would also pass on a timeline that dropped everything, so
    this pins the ghost into the database first, proves it is really there, and
    proves a real row's events survive the same filter.
    """
    db = tmp_path / "state" / "coord.db"
    db.parent.mkdir(parents=True)
    demo.seed(db, quiet=True)

    conn = connect(db)
    coord_db.post_event(conn, kind="note", actor="claude", work_id="GHOST-NOT-A-ROW")
    coord_db.post_event(conn, kind="note", actor="codex", work_id="ML-202")
    conn.commit()
    conn.close()

    # The ghost is in the events table -- the exclusion below is the builder's
    # doing, not an event that never got written.
    check = connect(db)
    try:
        stored = check.execute(
            "SELECT COUNT(*) FROM events WHERE work_id = 'GHOST-NOT-A-ROW'"
        ).fetchone()[0]
        rows_named = check.execute(
            "SELECT COUNT(*) FROM work_items WHERE work_id = 'GHOST-NOT-A-ROW'"
        ).fetchone()[0]
    finally:
        check.close()
    assert stored == 1
    assert rows_named == 0

    timeline = snapshot_module.build_timeline(db)
    snapshot = build_snapshot(db)
    timeline_ids = {item["id"] for item in timeline["items"]}
    snapshot_ids = {row["id"] for row in snapshot["rows"]}

    assert "GHOST-NOT-A-ROW" not in timeline_ids
    assert timeline_ids <= snapshot_ids, sorted(timeline_ids - snapshot_ids)
    # Not vacuous: the same filter left a real row's history in place.
    carried = {item["id"]: item["events"] for item in timeline["items"]}
    assert "ML-202" in carried
    assert any(event["actor"] == "codex" for event in carried["ML-202"])


def test_timeline_is_bounded_and_survives_unrepresentable_times(tmp_path: Path) -> None:
    """Volume, impossible timestamps, and a board with no events at all.

    `ts` is a REAL with no range check, so a corrupt or synthetic row can hold a
    value no calendar can express. Rendering is not the question -- dropping
    without raising is, because one such row would otherwise take the whole
    document down and the board would serve nothing rather than serve less.
    """
    db = tmp_path / "state" / "coord.db"
    db.parent.mkdir(parents=True)
    demo.seed(db, quiet=True)

    conn = connect(db)
    for index in range(1000):
        coord_db.post_event(
            conn,
            kind="heartbeat",
            actor="codex",
            work_id="ML-202",
            idempotency_key=f"volume-{index}",
        )
    conn.commit()
    conn.close()

    raw = connect(db)
    try:
        for value in (-1.0e18, 1.0e18, float("inf")):
            raw.execute(
                "INSERT INTO events(ts,kind,actor,work_id,refs_json,payload_json)"
                " VALUES(?,?,?,?,'[]','{}')",
                (value, "impossible", "claude", "ML-202"),
            )
        raw.commit()
    finally:
        raw.close()

    timeline = snapshot_module.build_timeline(db)
    events = {item["id"]: item["events"] for item in timeline["items"]}["ML-202"]
    assert len(events) >= 1000
    assert all(
        event["kind"] != "impossible" for event in events
    ), "a time no calendar can express must be dropped, not rendered"
    assert all(
        earlier["at"] <= later["at"] for earlier, later in zip(events, events[1:])
    )
    assert set(events[0]) == {"at", "kind", "actor"}

    # A board whose events are all gone is an empty list, not a missing key and
    # not a placeholder item.
    empty = connect(db)
    try:
        # `request_consumption` carries a foreign key onto the event that
        # opened a handoff or an audit request, and the seeded board records
        # both. Draining the record means dropping what points at it first;
        # a bare delete raises rather than emptying anything.
        empty.execute("DELETE FROM request_consumption")
        empty.execute("DELETE FROM events")
        empty.commit()
    finally:
        empty.close()
    drained = snapshot_module.build_timeline(db)
    assert drained["items"] == []
    assert drained["schema_version"] == "TimelineV1"
    assert build_snapshot(db)["rows"], "the rows are still there; only the events went"


def _pulse_document(db: Path) -> dict:
    from coordharness.board.snapshot import build_pulse

    return build_pulse(db)


def test_pulse_document_publishes_structure_and_nothing_else(tmp_path: Path) -> None:
    """PulseV1 carries kinds, lanes, counts, instants and routing lanes.

    Three facts live here that the other four documents cannot carry.
    Direction: TimelineV1's event tuple is sealed at (at, kind, actor) by the
    test above, so a reader could see that a handoff happened and never see who
    it was to. Vocabulary: `events.kind` is unconstrained TEXT with no
    registry, so the only honest way to say what kinds a board has is to count
    the ones on it. Shape: how many distinct instants, across how many UTC
    days.

    Every assertion here is on the shape and on the arithmetic between the
    sections, so a section that silently stopped counting cannot pass by being
    internally consistent with itself.
    """
    from coordharness.board.snapshot import build_timeline

    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    document = _pulse_document(db)

    assert set(document) == {
        "schema_version", "generated_at", "source", "counts", "kinds",
        "lanes", "days", "traffic", "traffic_undirected", "recent",
    }
    assert document["schema_version"] == "PulseV1"
    assert document["source"] == "coord.db"
    datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))

    counts = document["counts"]
    assert set(counts) == {
        "events", "events_without_row", "events_unrepresentable_time",
        "events_unattributed",
        "rows", "rows_with_events", "distinct_instants", "days", "lanes",
        "sessions", "sessions_live",
        "sessions_unattributed", "sessions_live_unattributed",
    }
    for key, value in counts.items():
        assert isinstance(value, int) and not isinstance(value, bool), key
        assert value >= 0, key

    # Every list is a list of one fixed record shape. A new key here is a wire
    # change, and this is where it is caught.
    for record in document["kinds"]:
        assert set(record) == {"kind", "count"}
    for record in document["lanes"]:
        assert set(record) == {"lane", "events", "kinds", "sessions", "sessions_live"}
        for inner in record["kinds"]:
            assert set(inner) == {"kind", "count"}
    for record in document["days"]:
        assert set(record) == {"date", "events", "first_at", "last_at"}
        datetime.fromisoformat(record["first_at"].replace("Z", "+00:00"))
        datetime.fromisoformat(record["last_at"].replace("Z", "+00:00"))
        assert record["first_at"] <= record["last_at"]
    for record in document["traffic"]:
        assert set(record) == {"kind", "from", "to", "count"}
        assert record["kind"] in {"audit_request", "audit_verdict", "handoff"}
    for record in document["traffic_undirected"]:
        assert set(record) == {"kind", "from", "count"}
    for record in document["recent"]:
        assert set(record) == {"at", "kind", "actor", "to", "row"}
        datetime.fromisoformat(record["at"].replace("Z", "+00:00"))

    # The arithmetic between sections. Each of these is a different path to the
    # same number, so a section that stopped counting disagrees with the rest
    # instead of quietly shrinking.
    assert counts["events"] == sum(record["count"] for record in document["kinds"])
    assert counts["events"] == sum(record["events"] for record in document["days"])
    # The lanes section carries only acts whose actor names a lane, so the
    # unattributed term is what makes this a closed sum on any board rather
    # than only on one where every actor happens to be set.
    assert counts["events"] == (
        sum(record["events"] for record in document["lanes"])
        + counts["events_unattributed"]
    )
    assert counts["sessions"] == (
        sum(record["sessions"] for record in document["lanes"])
        + counts["sessions_unattributed"]
    )
    assert counts["sessions_live"] == (
        sum(record["sessions_live"] for record in document["lanes"])
        + counts["sessions_live_unattributed"]
    )
    # No lane-pair row may name a lane the roster does not contain.
    roster = {record["lane"] for record in document["lanes"]}
    for record in document["traffic"]:
        assert record["from"] in roster, record
    for record in document["traffic_undirected"]:
        assert record["from"] in roster, record
    assert counts["days"] == len(document["days"])
    assert counts["lanes"] == len(document["lanes"])
    for record in document["lanes"]:
        assert record["events"] == sum(inner["count"] for inner in record["kinds"])
        assert record["sessions_live"] <= record["sessions"]

    # Population agrees with TimelineV1 by construction: both carry exactly the
    # events attached to a row this board has. A count beside a list the reader
    # can enumerate must not be a different number.
    timeline = build_timeline(db)
    assert counts["events"] == sum(len(item["events"]) for item in timeline["items"])
    assert counts["rows_with_events"] == len(timeline["items"])

    # Newest first, and capped. The cap is what makes this a recency list and
    # not a second copy of the timeline.
    assert len(document["recent"]) <= 12
    stamps = [record["at"] for record in document["recent"]]
    assert stamps == sorted(stamps, reverse=True)

    # Not vacuous: the seeded board records typed coordination acts in both
    # directions, which is the fact this endpoint exists to publish.
    assert counts["events"] > 0
    assert len(document["kinds"]) >= 4, "a one-kind board proves nothing about kinds"
    assert document["traffic"], "no routed coordination act was recorded"
    pairs = {(record["from"], record["to"]) for record in document["traffic"]}
    assert ("claude", "codex") in pairs and ("codex", "claude") in pairs

    # Deterministic: two reads of an unchanged board serialise identically.
    # `generated_at` is the read clock and moves by design.
    again = _pulse_document(db)
    del again["generated_at"], document["generated_at"]
    assert json.dumps(again, sort_keys=True) == json.dumps(document, sort_keys=True)


def test_pulse_serves_routing_lanes_and_never_the_selector_itself(tmp_path: Path) -> None:
    """`to` is a lane name or null, and the raw column never reaches the wire.

    `events.to_selector` is TEXT with no constraint. The typed handoff writer
    puts `actor:<lane>` in it, but nothing stops another writer putting a
    sentence there, so the match is a strict server-side grammar and anything
    failing it serialises as null. This proves both halves: a well-formed
    selector arrives as its lane, and a hostile one arrives as nothing at all.
    """
    import re

    sentinel = "CANARY-7c02e8-selector"
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    conn = connect(db)
    try:
        coord_db.post_event(
            conn,
            kind="handoff",
            actor="claude",
            work_id="ML-202",
            to_selector=f"actor:codex {sentinel} <script>",
            title="hostile selector",
            body="hostile selector",
        )
        conn.commit()
    finally:
        conn.close()

    document = _pulse_document(db)
    serialised = json.dumps(document)
    assert sentinel not in serialised, "PulseV1 leaked a routing selector"

    lane = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
    for record in document["recent"]:
        assert record["to"] is None or lane.match(record["to"]), record
    for record in document["traffic"]:
        assert lane.match(record["from"]) and lane.match(record["to"]), record

    # The hostile event is counted, not dropped -- it lands in the undirected
    # bucket, which is the honest place for "we do not know where this went".
    undirected = {
        (record["kind"], record["from"]): record["count"]
        for record in document["traffic_undirected"]
    }
    assert undirected.get(("handoff", "claude"), 0) >= 1
    # And the well-formed selectors the seeder wrote still arrive as lanes.
    assert any(record["to"] == "codex" for record in document["traffic"])


def test_pulse_counts_acts_whose_actor_names_no_lane(tmp_path: Path) -> None:
    """An unattributed act is counted, never filed under a lane called "".

    `events.actor` is nullable TEXT with no registry, so an actor can be absent,
    blank, or a bare `:suffix`. Each reduces to an empty lane, and the lanes
    section carries only real lanes. Without an explicit term for the remainder
    two things went wrong at once: `sum(lanes[].events)` silently disagreed with
    `counts.events`, and a `handoff` from such an actor was published in
    `traffic` as `from: ""` -- a lane no roster contains.

    The demo seed sets every actor, so this is invisible on a seeded board; it
    is reachable on any real one.
    """
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    conn = connect(db)
    try:
        for actor in (None, "   ", ":orphan"):
            coord_db.post_event(
                conn,
                kind="handoff",
                actor=actor,
                work_id="ML-202",
                to_selector="actor:codex",
                title="unattributed",
                body="unattributed",
            )
        conn.commit()
    finally:
        conn.close()

    document = _pulse_document(db)
    counts = document["counts"]

    assert counts["events_unattributed"] == 3
    # Counted in the totals and in every section that does not name a lane.
    assert counts["events"] == sum(record["count"] for record in document["kinds"])
    assert counts["events"] == sum(record["events"] for record in document["days"])
    # And the lanes section closes only with the unattributed term beside it.
    assert counts["events"] == (
        sum(record["events"] for record in document["lanes"])
        + counts["events_unattributed"]
    )

    # No phantom lane anywhere on the wire.
    roster = {record["lane"] for record in document["lanes"]}
    assert "" not in roster
    for record in document["traffic"] + document["traffic_undirected"]:
        assert record["from"] in roster, record


def test_pulse_and_context_carry_no_event_prose(tmp_path: Path) -> None:
    """The leak canary for both new surfaces, planted in every prose column.

    The shape tests above would pass a document that also carried a `body`
    under a different key: a renamed leak reads as a new field, not as a
    regression. So a sentinel goes into `title`, `body`, `refs_json`,
    `payload_json` and `to_selector` of an event that IS counted by both new
    surfaces, and the assertion is that the string appears nowhere in either
    serialisation, whatever key it might have arrived under.
    """
    from coordharness.board.snapshot import build_context

    sentinel = "CANARY-51ab99-do-not-serve"
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    conn = connect(db)
    try:
        coord_db.post_event(
            conn,
            kind="note",
            actor="codex",
            work_id="ML-202",
            to_selector=f"actor:{sentinel}",
            severity="high",
            title=f"title {sentinel}",
            body=f"body {sentinel}",
            refs_json=json.dumps([f"ref {sentinel}"]),
            payload_json=json.dumps({"why": f"payload {sentinel}"}),
        )
        conn.commit()
    finally:
        conn.close()

    pulse = _pulse_document(db)
    context = build_context(db)

    # Control: the event is really in the record, so the absences below are the
    # redaction at work rather than an event that never got written.
    assert pulse["counts"]["events"] >= 1
    assert any(record["row"] == "ML-202" for record in pulse["recent"]) or any(
        record["kind"] == "note" for record in pulse["kinds"]
    )

    # Withheld by key name as well as by value, walked to every depth, so an
    # edit that adds a prose column is caught before it happens to carry a
    # sentinel. Matched on keys rather than on the serialised text because
    # `note` and `audit_verdict` are legitimate event *kinds* and a substring
    # test would fail on a value the document is entitled to publish.
    withheld = {
        "title", "body", "refs_json", "payload_json", "session_id",
        "to_selector", "severity", "verdict", "trust", "note",
        "note_text", "why_text", "decisions", "knowledge",
    }

    def _keys(value: object) -> set[str]:
        if isinstance(value, dict):
            found = set(value)
            for item in value.values():
                found |= _keys(item)
            return found
        if isinstance(value, list):
            found: set[str] = set()
            for item in value:
                found |= _keys(item)
            return found
        return set()

    for name, document in (("PulseV1", pulse), ("ContextV1", context)):
        serialised = json.dumps(document)
        assert sentinel not in serialised, f"{name} leaked event prose"
        assert not (withheld & _keys(document)), (
            f"{name} carries {sorted(withheld & _keys(document))}"
        )

    # `events` is the one name that appears on both sides of the line: the
    # withheld column is an array of bodies, PulseV1's is a count. Pinned as a
    # count so a later edit cannot smuggle the array back under the same name.
    def _events_values(value: object) -> list[object]:
        if isinstance(value, dict):
            found = [value[key] for key in value if key == "events"]
            for item in value.values():
                found += _events_values(item)
            return found
        if isinstance(value, list):
            found = []
            for item in value:
                found += _events_values(item)
            return found
        return []

    seen = _events_values(pulse)
    assert seen, "no events count found; the assertion below is vacuous"
    assert all(
        isinstance(value, int) and not isinstance(value, bool) for value in seen
    ), "PulseV1 carries an events array, not an events count"
    assert "events" not in _keys(context)


def test_context_publishes_custody_and_pins_its_item_keys(tmp_path: Path) -> None:
    """ContextV1 items gain `claim_present` and `lease_remaining_s`, pinned here.

    The key set is asserted exactly, in one place, so the two new fields cannot
    be removed by a later edit and a third cannot arrive unannounced.

    The remainder may be negative and is not clamped. An expired lease is a
    fact, and folding "expired two hours ago" into zero would file a dead claim
    with the ones that just lapsed -- which is the exact confusion the field
    exists to remove.
    """
    from coordharness.board.snapshot import build_context

    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    document = build_context(db)
    items = {item["id"]: item for item in document["items"]}
    assert items

    expected = {
        "id", "parent", "children", "depends_on", "dependents", "siblings",
        "done_signal", "artifact_recorded", "blocked_reason_class",
        "resume_when", "next_step", "claim_present", "lease_remaining_s",
    }
    for item in document["items"]:
        assert set(item) == expected, item["id"]
        assert isinstance(item["claim_present"], bool)
        if item["claim_present"]:
            assert isinstance(item["lease_remaining_s"], int)
            assert not isinstance(item["lease_remaining_s"], bool)
        else:
            assert item["lease_remaining_s"] is None

    # Not vacuous in either direction: the seeded board holds claims, and it
    # also carries rows nobody has claimed.
    claimed = [item for item in document["items"] if item["claim_present"]]
    unclaimed = [item for item in document["items"] if not item["claim_present"]]
    assert claimed and unclaimed
    assert all(item["lease_remaining_s"] > 0 for item in claimed), (
        "a freshly seeded board's leases should not already be expired"
    )

    # An expired lease reports a negative remainder rather than a zero.
    raw = connect(db)
    try:
        raw.execute(
            "UPDATE claims SET expires_at = expires_at - 7200"
            " WHERE work_id = ?",
            (claimed[0]["id"],),
        )
        raw.commit()
    finally:
        raw.close()
    after = {item["id"]: item for item in build_context(db)["items"]}[claimed[0]["id"]]
    assert after["claim_present"] is True
    assert after["lease_remaining_s"] < 0


def test_pulse_is_served_read_only_and_refuses_every_write(tmp_path: Path) -> None:
    """The new route answers GET and HEAD, and nothing else.

    The route sweep in test_board_readonly.py finds this endpoint on its own by
    reading do_GET, so it is already covered there. This states the contract
    directly at the new path anyway: a read-only projection that grew a write
    verb would be a different kind of surface, and that should fail by name.
    """
    db = tmp_path / "state" / "coord.db"
    db.parent.mkdir(parents=True)
    demo.seed(db, quiet=True)
    server = make_server(port=0, db_path=str(db), refresh_interval=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _request(server.server_port, "/api/v1/pulse")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        document = json.loads(body)
        assert document["schema_version"] == "PulseV1"
        assert document == server.pulse()
        assert document["counts"]["events"] > 0

        head_status, head_headers, head_body = _request(
            server.server_port, "/api/v1/pulse", method="HEAD"
        )
        assert head_status == 200
        assert head_body == b""
        assert head_headers["Content-Length"] == headers["Content-Length"]

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            write_status, write_headers, _ = _request(
                server.server_port, "/api/v1/pulse", method=method
            )
            assert write_status == 405, f"{method} /api/v1/pulse -> {write_status}"
            assert write_headers["Allow"] == "GET, HEAD, OPTIONS"

        # The host check is the same one every other route runs.
        forbidden, _headers, _body = _request(
            server.server_port,
            "/api/v1/pulse",
            headers={"Host": "board.example.com"},
        )
        assert forbidden == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_demo_records_a_history_across_days_lanes_and_kinds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The seeded board is a record, not one instant with everything on it.

    The seeder used to pin every write to a single clock value, so the whole
    fictional history happened in one microsecond: nothing could be ordered,
    no view could group by day, and a recency list was indistinguishable from
    insertion order. It also wrote one kind, `note`, so every kind-aware view
    rendered as one colour and a reader could not tell a collapsed view from a
    uniform board.

    Determinism is asserted on the same clock the rest of the seeder uses:
    pinned by SOURCE_DATE_EPOCH, two seeds are byte-identical.
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1772442000")
    payloads = []
    for name in ("first", "second"):
        state = tmp_path / name
        monkeypatch.setenv("COORD_HOME", str(state))
        db = state / "coord.db"
        demo.seed(db, quiet=True)
        document = _pulse_document(db)
        assert document["generated_at"] == "2026-03-02T09:00:00.000Z"
        del document["generated_at"]
        payloads.append(json.dumps(document, sort_keys=True))
    assert payloads[0] == payloads[1]

    document = json.loads(payloads[0])
    counts = document["counts"]

    # Many instants, not one. Every seeded event has its own age, so the record
    # has as many instants as it has events.
    assert counts["events"] >= 40
    assert counts["distinct_instants"] == counts["events"]
    # Several fictional days, so a view that groups by UTC date finds groups.
    assert counts["days"] >= 3
    assert len(document["days"]) == counts["days"]
    assert sum(day["events"] for day in document["days"]) == counts["events"]

    # More than one lane writes, and more than one kind is written.
    lanes = {record["lane"] for record in document["lanes"] if record["events"]}
    assert len(lanes) >= 3, sorted(lanes)
    kinds = {record["kind"] for record in document["kinds"]}
    assert {"note", "handoff", "audit_request", "audit_verdict"} <= kinds
    assert len(kinds) >= 8, sorted(kinds)

    # And the coordination acts have a recorded destination, in both
    # directions, which is what makes the traffic section non-empty.
    assert {record["kind"] for record in document["traffic"]} == {
        "audit_request", "audit_verdict", "handoff",
    }
    assert {(record["from"], record["to"]) for record in document["traffic"]} >= {
        ("claude", "codex"), ("codex", "claude"),
    }
