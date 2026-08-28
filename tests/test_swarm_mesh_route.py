from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server


REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "coordharness" / "board" / "static"


@pytest.fixture()
def mesh_board(tmp_path: Path):
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


def _request(port: int, method: str, path: str) -> tuple[int, str, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        connection.close()


def test_swarm_mesh_shell_and_assets_are_served_read_only(mesh_board) -> None:
    status, content_type, body = _request(mesh_board.server_port, "GET", "/mesh")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    markup = body.decode("utf-8")
    assert "COORD Swarm Mesh" in markup
    assert 'href="/static/swarm-mesh.css"' in markup
    assert 'src="/static/ops-atlas-model.js"' in markup
    assert 'src="/static/swarm-mesh-model.js"' in markup
    assert 'src="/static/swarm-mesh.js"' in markup
    assert "<style" not in markup
    assert "<script>" not in markup

    for asset, expected_type in (
        ("swarm-mesh.css", "text/css"),
        ("swarm-mesh-model.js", "text/javascript"),
        ("swarm-mesh.js", "text/javascript"),
    ):
        asset_status, asset_type, asset_body = _request(
            mesh_board.server_port, "GET", f"/static/{asset}"
        )
        assert asset_status == 200
        assert expected_type in asset_type
        assert asset_body

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        method_status, _, _ = _request(mesh_board.server_port, method, "/mesh")
        assert method_status == 405


def test_spatial_scene_is_deterministic_and_layouts_only_encode_declared_facets() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the standalone scene-model contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
global.window = global;
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

const nodes = [
  {id:"agent:codex:one", kind:"agent", label:"Codex one", status:"running", owner:"codex:one", actor:"codex", module:"", modules:["alpha"], missing:false},
  {id:"agent:claude:two", kind:"agent", label:"Claude two", status:"recorded", owner:"claude:two", actor:"claude", module:"", modules:["beta"], missing:false},
  {id:"work:A", kind:"work", label:"Alpha work", status:"running", owner:"codex:one", module:"alpha", modules:["alpha"], missing:false},
  {id:"work:B", kind:"work", label:"Beta work", status:"planned", owner:"claude:two", module:"beta", modules:["beta"], missing:false},
  {id:"job:J", kind:"job", label:"Recorded job", status:"running", owner:"local:runner", module:"", modules:[], missing:false},
  {id:"work:MISSING", kind:"missing_work", label:"Missing endpoint", status:"missing", owner:"", module:"", modules:[], missing:true},
];
const nodesById = new Map(nodes.map(value => [value.id, value]));
const edges = [
  {id:"owns:codex:A", source:"agent:codex:one", target:"work:A", kind:"owns", sourceField:"snapshot.rows.owner", missing:false},
  {id:"owns:claude:B", source:"agent:claude:two", target:"work:B", kind:"owns", sourceField:"snapshot.rows.owner", missing:false},
  {id:"depends:A:B", source:"work:A", target:"work:B", kind:"depends_on", sourceField:"work_items.depends_on", missing:false},
  {id:"evidence:A:J", source:"work:A", target:"job:J", kind:"runtime_evidence", sourceField:"job_progress.roadmap_id", missing:false},
  {id:"missing:B:X", source:"work:MISSING", target:"work:B", kind:"depends_on", sourceField:"work_items.depends_on", missing:true},
];
const model = {
  nodes, nodesById, edges,
  activity: [
    {at:"2026-08-25T12:00:00Z", id:"A", kind:"note", actor:"codex"},
    {at:"2026-08-25T12:01:00Z", id:"B", kind:"note", actor:"reviewer"},
  ],
  documents: {operations: {execution: {
    layers:[{depth:0, ids:["A"]}, {depth:1, ids:["B"]}],
    topology_metrics_status:"partial_unresolved",
    cycle_tainted:[], analysis_boundary_tainted:[],
    missing_dependency_tainted:["MISSING"], unresolved_tainted:["MISSING"],
  }}},
  receipt: {complete:false, emittedNodes:6, emittedEdges:5, omittedNodes:0, omittedEdges:0, unknownCount:0},
};

function digest(scene) {
  return {
    schemaVersion: scene.schemaVersion,
    revision: scene.layoutRevision,
    layout: scene.layout,
    clusters: scene.clusters.map(c => ({id:c.id, label:c.label, count:c.memberCount, center:c.center, ids:c.nodeIds})),
    nodes: scene.nodes.map(n => ({id:n.id, cluster:n.clusterId, depth:n.dependencyDepth, depthState:n.depthState, world:n.world})),
    edges: scene.edges.map(e => ({id:e.id, bend:e.bend, source:e.sourceWorld, target:e.targetWorld})),
    occurrences: scene.occurrences,
  };
}
const layouts = {};
for (const layout of ["swarm", "context", "critical"]) {
  const first = digest(CoordSwarmMeshModel.buildScene(model, {layout}));
  const second = digest(CoordSwarmMeshModel.buildScene(model, {layout}));
  layouts[layout] = {first, second};
}
process.stdout.write(JSON.stringify(layouts));
"""
    result = subprocess.run(
        [node, "-e", script, str(STATIC / "swarm-mesh-model.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    layouts = json.loads(result.stdout)

    for layout, pair in layouts.items():
        assert pair["first"] == pair["second"], f"{layout} layout drifted"
        assert pair["first"]["schemaVersion"] == "SpatialSceneV1"
        assert pair["first"]["revision"] == "coord-spatial-deterministic-v1"

    swarm = layouts["swarm"]["first"]
    context = layouts["context"]["first"]
    critical = layouts["critical"]["first"]
    assert {cluster["id"] for cluster in swarm["clusters"]} >= {
        "lane:codex",
        "lane:claude",
        "quarantine",
    }
    assert {cluster["id"] for cluster in context["clusters"]} >= {
        "module:alpha",
        "module:beta",
        "quarantine",
    }
    assert {cluster["id"] for cluster in critical["clusters"]} >= {
        "critical:depth:0",
        "critical:depth:1",
        "quarantine",
    }
    quarantined = next(node for node in critical["nodes"] if node["id"] == "work:MISSING")
    assert quarantined["depthState"] == "unresolved"

    occurrences = {item["actor"]: item for item in swarm["occurrences"]}
    assert occurrences["codex"]["motionState"] == "exact_hold"
    assert occurrences["codex"]["edgeId"] == "owns:codex:A"
    assert occurrences["reviewer"]["motionState"] == "actor_mismatch"
    assert occurrences["reviewer"]["edgeId"] == ""

    # Layout switches change only deterministic presentation. Identity and
    # source relationship membership remain the same in all three scenes.
    expected_nodes = {node["id"] for node in swarm["nodes"]}
    expected_edges = {edge["id"] for edge in swarm["edges"]}
    for scene in (context, critical):
        assert {node["id"] for node in scene["nodes"]} == expected_nodes
        assert {edge["id"] for edge in scene["edges"]} == expected_edges


def test_low_information_scene_centers_nodes_and_fits_projected_bounds() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the standalone scene-model contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
global.window = global;
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

const nodes = Array.from({length: 13}, (_, index) => ({
  id: `work:LOW-${String(index + 1).padStart(2, "0")}`,
  kind: "work",
  label: `Low information node ${index + 1}`,
  status: "recorded",
  owner: "",
  module: `module-${index + 1}`,
  modules: [`module-${index + 1}`],
  missing: false,
}));
const availability = {
  schema_version: "TopologyAvailabilityV1",
  state: "low_information",
  reason_code: "authoritative_relationships_absent",
  reason: "The authoritative graph publishes no relationships.",
  missing_prerequisite: "authoritative_graph_relationships",
  population: {work_items: 13, nodes: 13, edges: 0, events: 2},
  admitted: {work_items: 13, nodes: 13, edges: 0, events: 2},
  omitted: {work_items: 0, nodes: 0, edges: 0, events: 0},
  source: {graph_declared: "test", graph_schema_version: "GraphV1", graph_content_sha256: "a".repeat(64)},
  freshness: {state: "current", stale: false, generated_at: "2026-08-26T12:00:00Z", document_skew_seconds: 0},
};
const model = {
  nodes,
  nodesById: new Map(nodes.map(value => [value.id, value])),
  edges: [],
  activity: [],
  documents: {operations: {topology_availability: availability, execution: {layers: [], topology_metrics_status: "available"}}},
  receipt: {complete: true, emittedNodes: 13, emittedEdges: 0, omittedNodes: 0, omittedEdges: 0, unknownCount: 0},
};
const scene = CoordSwarmMeshModel.buildScene(model, {layout: "context"});
const oneClusterNodes = nodes.map(value => ({...value, module: "single", modules: ["single"]}));
const singleModel = {
  ...model,
  nodes: oneClusterNodes,
  nodesById: new Map(oneClusterNodes.map(value => [value.id, value])),
};
const single = CoordSwarmMeshModel.buildScene(singleModel, {layout: "context"});
const viewport = {width: 900, height: 500};
const camera = {yaw: .42, pitch: .35, zoom: 1, panX: 0, panY: 0};
const fit = CoordSwarmMeshModel.fitCameraToPoints(
  scene.nodes.map(value => value.world),
  camera,
  viewport,
  {padding: 72, minZoom: .25, maxZoom: 2.5},
);
const bounds = CoordSwarmMeshModel.projectedBounds(scene.nodes.map(value => value.world), fit, viewport);
process.stdout.write(JSON.stringify({
  lowInformation: scene.lowInformation,
  topologyState: scene.topologyAvailability.state,
  nodeCount: scene.nodes.length,
  edgeCount: scene.edges.length,
  maximumCenterRadius: Math.max(...scene.clusters.map(value => Math.hypot(value.center.x, value.center.y))),
  singleCenter: single.clusters[0].center,
  fit,
  bounds,
}));
"""
    result = subprocess.run(
        [node, "-e", script, str(STATIC / "swarm-mesh-model.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(result.stdout)

    assert receipt["lowInformation"] is True
    assert receipt["topologyState"] == "low_information"
    assert receipt["nodeCount"] == 13
    assert receipt["edgeCount"] == 0
    assert receipt["maximumCenterRadius"] <= 170
    assert receipt["singleCenter"] == {"x": 0, "y": 0, "z": 0}
    assert 0.25 <= receipt["fit"]["zoom"] <= 2.5
    assert receipt["bounds"]["minX"] >= 72 - 1e-6
    assert receipt["bounds"]["maxX"] <= 900 - 72 + 1e-6
    assert receipt["bounds"]["minY"] >= 72 - 1e-6
    assert receipt["bounds"]["maxY"] <= 500 - 72 + 1e-6


def test_perspective_projection_is_deterministic_and_motion_edges_are_admitted() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the standalone scene-model contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
global.window = global;
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

const camera = {yaw: 0, pitch: 0, zoom: 1, panX: 0, panY: 0};
const viewport = {width: 1000, height: 600};
const near = {x: 80, y: -25, z: 210};
const far = {x: 80, y: -25, z: -210};
const first = CoordSwarmMeshModel.projectPoint(near, camera, viewport, "perspective");
const second = CoordSwarmMeshModel.projectPoint(near, camera, viewport, "perspective");
const farProjected = CoordSwarmMeshModel.projectPoint(far, camera, viewport, "perspective");
const flatNear = CoordSwarmMeshModel.projectPoint(near, camera, viewport, "flat");
const flatFar = CoordSwarmMeshModel.projectPoint(far, camera, viewport, "flat");

const nodes = [
  {id:"work:A", kind:"work", label:"A", status:"running", owner:"codex:a", module:"alpha", modules:["alpha"], missing:false},
  {id:"work:B", kind:"work", label:"B", status:"planned", owner:"codex:a", module:"alpha", modules:["alpha"], missing:false},
];
const admitted = {id:"edge:admitted", source:"work:A", target:"work:B", kind:"depends_on"};
const synthetic = {id:"edge:synthetic", source:"work:A", target:"work:B", kind:"owns"};
const model = {
  nodes,
  nodesById:new Map(nodes.map(value => [value.id, value])),
  edges:[admitted, synthetic],
  activity:[],
  documents:{operations:{
    graph_envelope:{schema_version:"GraphEnvelopeV1", nodes, edges:[admitted]},
    execution:{layers:[], topology_metrics_status:"available"},
  }},
  receipt:{complete:true, emittedNodes:2, emittedEdges:1},
};
const scene = CoordSwarmMeshModel.buildScene(model, {layout:"context"});
process.stdout.write(JSON.stringify({
  deterministic: first,
  repeated: second,
  nearDepthScale: first.depthScale,
  farDepthScale: farProjected.depthScale,
  nearFog: first.fog,
  farFog: farProjected.fog,
  flatNear,
  flatFar,
  admitted: scene.admittedEdges.map(edge => edge.id),
  flags: Object.fromEntries(scene.edges.map(edge => [edge.id, edge.admitted])),
}));
"""
    result = subprocess.run(
        [node, "-e", script, str(STATIC / "swarm-mesh-model.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(result.stdout)

    assert receipt["deterministic"] == receipt["repeated"]
    assert receipt["nearDepthScale"] > receipt["farDepthScale"]
    assert receipt["nearFog"] > receipt["farFog"]
    assert receipt["flatNear"]["x"] == receipt["flatFar"]["x"]
    assert receipt["flatNear"]["y"] == receipt["flatFar"]["y"]
    assert receipt["flatNear"]["depthScale"] == 1
    assert receipt["flatFar"]["depthScale"] == 1
    assert receipt["admitted"] == ["edge:admitted"]
    assert receipt["flags"] == {"edge:admitted": True, "edge:synthetic": False}
