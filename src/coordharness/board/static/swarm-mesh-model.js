(function (root) {
  "use strict";

  const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
  const EVIDENCE_KINDS = new Set(["job", "artifact"]);
  const ATTENTION = new Set(["blocked", "attention", "failed", "stuck", "missing"]);

  const list = value => Array.isArray(value) ? value : [];
  const record = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const text = (value, fallback) => {
    const result = value === null || value === undefined ? "" : String(value).trim();
    return result || (fallback || "");
  };
  const compare = (left, right) => String(left).localeCompare(String(right));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const lane = owner => text(owner, "unowned").split(":", 1)[0].toLowerCase();

  function stableHash(value) {
    let hash = 2166136261;
    const source = String(value);
    for (let index = 0; index < source.length; index += 1) {
      hash ^= source.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function unit(value, salt) {
    return stableHash(`${salt}:${value}`) / 4294967295;
  }

  function sourceWorkId(nodeId) {
    const value = text(nodeId);
    return value.startsWith("work:") ? value.slice(5) : value;
  }

  function measuredDepths(model) {
    const execution = record(record(model.documents).operations).execution || {};
    const depths = new Map();
    list(execution.layers).forEach(layerRow => {
      const row = record(layerRow);
      const depth = Number(row.depth);
      if (!Number.isInteger(depth) || depth < 0) return;
      list(row.ids).forEach(id => depths.set(text(id), depth));
    });
    const unresolved = new Set([
      ...list(execution.cycle_tainted),
      ...list(execution.analysis_boundary_tainted),
      ...list(execution.missing_dependency_tainted),
      ...list(execution.unresolved_tainted),
    ].map(text).filter(Boolean));
    return { depths, unresolved, available: text(execution.topology_metrics_status) === "available" };
  }

  function topologyAvailability(model) {
    const receipt = record(record(record(model).documents).operations).topology_availability;
    return text(record(receipt).schema_version) === "TopologyAvailabilityV1"
      ? record(receipt)
      : {};
  }

  function lowInformationState(model) {
    const availability = topologyAvailability(model);
    const operations = record(record(record(model).documents).operations);
    const envelope = record(operations.graph_envelope);
    const envelopePresent = text(envelope.schema_version) === "GraphEnvelopeV1" ||
      Object.prototype.hasOwnProperty.call(envelope, "nodes");
    const authoritativeNodes = envelopePresent ? list(envelope.nodes).length : list(record(model).nodes).length;
    const authoritativeEdges = envelopePresent ? list(envelope.edges).length : list(record(model).edges).length;
    return text(availability.state) === "low_information" ||
      (authoritativeNodes > 0 && authoritativeEdges === 0);
  }

  function admittedSceneModel(model) {
    if (!lowInformationState(model)) return model;
    const operations = record(record(model.documents).operations);
    const envelopeNodes = list(record(operations.graph_envelope).nodes);
    const admittedIds = new Set(envelopeNodes.map(node => text(record(node).id)).filter(Boolean));
    const nodes = admittedIds.size
      ? model.nodes.filter(node => admittedIds.has(node.id))
      : model.nodes;
    return {
      ...model,
      nodes,
      nodesById: new Map(nodes.map(node => [node.id, node])),
      edges: [],
    };
  }

  function inheritedFacet(model, facet) {
    const values = new Map();
    model.nodes.forEach(node => {
      if (facet === "owner") values.set(node.id, text(node.owner));
      else values.set(node.id, text(node.module, list(node.modules)[0]));
    });
    // Evidence inherits the public facet of its recorded source work. This is
    // display grouping only and is never written back to the source document.
    model.edges.forEach(edge => {
      if (!EVIDENCE_KINDS.has(text(model.nodesById.get(edge.target) && model.nodesById.get(edge.target).kind)) &&
          !["evidence", "runtime_evidence"].includes(edge.kind)) return;
      const source = text(values.get(edge.source));
      if (source) values.set(edge.target, source);
    });
    return values;
  }

  function clusterAssignments(model, layout) {
    const owners = inheritedFacet(model, "owner");
    const modules = inheritedFacet(model, "module");
    const topology = measuredDepths(model);
    const assignments = new Map();

    model.nodes.forEach(node => {
      let key = "unassigned";
      let label = "Unassigned recorded work";
      let depthState = "not_applicable";
      let dependencyDepth = null;
      const rowId = sourceWorkId(node.id);

      if (node.missing) {
        key = "quarantine";
        label = "Missing / unresolved";
        depthState = "unresolved";
      } else if (layout === "swarm") {
        const owner = text(owners.get(node.id), text(node.owner));
        const ownerLane = owner ? lane(owner) : "unowned";
        key = `lane:${ownerLane}`;
        label = ownerLane === "unowned" ? "Unowned recorded work" : ownerLane;
      } else if (layout === "context") {
        const moduleName = text(modules.get(node.id), text(node.module));
        key = moduleName ? `module:${moduleName}` : "module:unassigned";
        label = moduleName || "Unassigned module";
      } else {
        if (node.kind === "agent") {
          key = "critical:owners";
          label = "Recorded owners";
        } else if (EVIDENCE_KINDS.has(node.kind)) {
          key = "critical:evidence";
          label = "Execution evidence";
        } else if (topology.unresolved.has(rowId)) {
          key = "critical:unresolved";
          label = "Depth withheld / unresolved";
          depthState = "unresolved";
        } else if (topology.depths.has(rowId)) {
          dependencyDepth = topology.depths.get(rowId);
          key = `critical:depth:${dependencyDepth}`;
          label = `Dependency layer ${dependencyDepth}`;
          depthState = "measured";
        } else {
          key = "critical:outside";
          label = "Outside measured population";
          depthState = "outside_population";
        }
      }
      assignments.set(node.id, { key, label, dependencyDepth, depthState });
    });
    return { assignments, topology };
  }

  function clusterCenters(keys, layout, lowInformation) {
    const centers = new Map();
    const ordered = [...keys].sort(compare);
    if (lowInformation) {
      if (ordered.length === 1) {
        centers.set(ordered[0], { x: 0, y: 0, z: 0 });
        return centers;
      }
      const radius = Math.min(170, 72 + ordered.length * 8);
      ordered.forEach((key, index) => {
        const angle = -Math.PI / 2 + index * (Math.PI * 2 / ordered.length);
        centers.set(key, {
          x: Math.cos(angle) * radius,
          y: Math.sin(angle) * radius * .58,
          z: 0,
        });
      });
      return centers;
    }
    if (layout === "critical") {
      const depthKeys = ordered.filter(key => key.startsWith("critical:depth:"));
      const maxDepth = Math.max(0, ...depthKeys.map(key => Number(key.split(":").at(-1))));
      ordered.forEach((key, index) => {
        if (key === "critical:owners") centers.set(key, { x: -560, y: -180, z: -40 });
        else if (key === "critical:evidence") centers.set(key, { x: 560, y: 190, z: 40 });
        else if (key === "critical:unresolved") centers.set(key, { x: 0, y: 330, z: -170 });
        else if (key === "critical:outside") centers.set(key, { x: 0, y: -320, z: -180 });
        else {
          const depth = Number(key.split(":").at(-1));
          const x = maxDepth ? -390 + (780 * depth / maxDepth) : 0;
          centers.set(key, { x, y: 0, z: (index % 3 - 1) * 44 });
        }
      });
      return centers;
    }

    ordered.forEach((key, index) => {
      if (key.includes("quarantine")) {
        centers.set(key, { x: 0, y: 340, z: -210 });
        return;
      }
      const count = Math.max(1, ordered.length);
      if (layout === "swarm") {
        const angle = -Math.PI / 2 + index * (Math.PI * 2 / count);
        centers.set(key, {
          x: Math.cos(angle) * 390,
          y: Math.sin(angle) * 235,
          z: Math.sin(angle * 2 + .35) * 145,
        });
        return;
      }
      const yUnit = 1 - (2 * (index + .5) / count);
      const radial = Math.sqrt(Math.max(0, 1 - yUnit * yUnit));
      const angle = index * GOLDEN_ANGLE;
      centers.set(key, {
        x: Math.cos(angle) * radial * 430,
        y: yUnit * 280,
        z: Math.sin(angle) * radial * 235,
      });
    });
    return centers;
  }

  function makeClusters(model, layout) {
    const { assignments, topology } = clusterAssignments(model, layout);
    const lowInformation = lowInformationState(model);
    const members = new Map();
    model.nodes.forEach(node => {
      const assignment = assignments.get(node.id);
      if (!members.has(assignment.key)) members.set(assignment.key, []);
      members.get(assignment.key).push(node);
    });
    members.forEach(nodes => nodes.sort((left, right) => compare(left.id, right.id)));
    const centers = clusterCenters(members.keys(), layout, lowInformation);
    const clusters = [...members].sort(([left], [right]) => compare(left, right)).map(([key, nodes], index) => {
      const assignment = assignments.get(nodes[0].id);
      return {
        id: key,
        label: assignment.label,
        index,
        memberCount: nodes.length,
        center: centers.get(key),
        radius: Math.min(190, 60 + Math.sqrt(nodes.length) * 19),
        nodeIds: nodes.map(node => node.id),
      };
    });
    return { assignments, clusters, topology, lowInformation };
  }

  function placeNodes(model, layout, clusterResult) {
    const clustersById = new Map(clusterResult.clusters.map(cluster => [cluster.id, cluster]));
    const indexByCluster = new Map(clusterResult.clusters.map(cluster => [cluster.id, 0]));
    const nodes = model.nodes.map(node => {
      const assignment = clusterResult.assignments.get(node.id);
      const cluster = clustersById.get(assignment.key);
      const index = indexByCluster.get(cluster.id);
      indexByCluster.set(cluster.id, index + 1);
      const angle = index * GOLDEN_ANGLE + unit(node.id, "angle") * .72;
      let radial = node.kind === "agent" ? 0 : 34 + Math.sqrt(index + 1) * 20;
      if (EVIDENCE_KINDS.has(node.kind)) radial += 24;
      const flatten = layout === "critical" ? .82 : .68;
      const world = {
        x: cluster.center.x + Math.cos(angle) * radial,
        y: cluster.center.y + Math.sin(angle) * radial * flatten,
        z: cluster.center.z + (unit(node.id, "depth") - .5) * Math.min(150, cluster.radius),
      };
      if (layout === "critical" && assignment.dependencyDepth !== null) {
        world.x = cluster.center.x + (unit(node.id, "x") - .5) * 36;
        world.y = cluster.center.y + (index - (cluster.memberCount - 1) / 2) * 34;
      }
      return {
        ...node,
        clusterId: cluster.id,
        clusterLabel: cluster.label,
        dependencyDepth: assignment.dependencyDepth,
        depthState: assignment.depthState,
        world,
        labelPriority: node.missing || ATTENTION.has(node.status) ? 1 : node.status === "running" ? 2 : 3,
      };
    });
    return nodes;
  }

  function placeEdges(model, nodesById) {
    const operations = record(record(model.documents).operations);
    const envelope = record(operations.graph_envelope);
    const envelopePresent = text(envelope.schema_version) === "GraphEnvelopeV1" ||
      Object.prototype.hasOwnProperty.call(envelope, "edges");
    const admittedEdgeIds = envelopePresent
      ? new Set(list(envelope.edges).map((value, index) => {
        const supplied = record(value);
        const source = text(supplied.source);
        const target = text(supplied.target);
        return text(supplied.id, `edge:${index}:${source}:${target}`);
      }))
      : null;
    return model.edges.map((edge, index) => {
      const source = nodesById.get(edge.source);
      const target = nodesById.get(edge.target);
      const bend = (unit(edge.id, "bend") - .5) * 80;
      return {
        ...edge,
        index,
        admitted: admittedEdgeIds ? admittedEdgeIds.has(edge.id) : true,
        sourceWorld: source ? source.world : { x: 0, y: 0, z: 0 },
        targetWorld: target ? target.world : { x: 0, y: 0, z: 0 },
        bend,
      };
    });
  }

  function eventBaseKey(event) {
    return JSON.stringify([text(event.at), text(event.id), text(event.kind), text(event.actor)]);
  }

  function eventKey(event, duplicateOrdinal) {
    return JSON.stringify([eventBaseKey(event), duplicateOrdinal]);
  }

  function placeOccurrences(model, edges, nodesById) {
    const ownershipByWork = new Map();
    edges.filter(edge => edge.kind === "owns").forEach(edge => ownershipByWork.set(edge.target, edge));
    const admittedByNode = new Map();
    edges.filter(edge => edge.admitted).forEach(edge => {
      [edge.source, edge.target].forEach(nodeId => {
        if (!admittedByNode.has(nodeId)) admittedByNode.set(nodeId, []);
        admittedByNode.get(nodeId).push(edge);
      });
    });
    const duplicateOrdinals = new Map();
    return model.activity.map(raw => {
      const event = record(raw);
      const baseKey = eventBaseKey(event);
      const duplicateOrdinal = duplicateOrdinals.get(baseKey) || 0;
      duplicateOrdinals.set(baseKey, duplicateOrdinal + 1);
      const candidates = [text(event.id), `work:${text(event.id)}`];
      const nodeId = candidates.find(candidate => nodesById.has(candidate)) || "";
      const ownership = ownershipByWork.get(nodeId);
      const admittedRoute = list(admittedByNode.get(nodeId))
        .sort((left, right) => Number(right.target === nodeId) - Number(left.target === nodeId) || compare(left.id, right.id))[0];
      let motionState = "unplaced";
      if (ownership) {
        const owner = nodesById.get(ownership.source);
        const actor = text(event.actor).toLowerCase();
        const matches = actor && [text(owner && owner.actor).toLowerCase(), lane(owner && owner.owner)].includes(actor);
        motionState = matches && admittedRoute ? "exact_hold" : matches ? "admitted_edge_absent" : "actor_mismatch";
      }
      return {
        key: eventKey(event, duplicateOrdinal),
        id: text(event.id),
        at: text(event.at),
        kind: text(event.kind, "event"),
        actor: text(event.actor, "unknown"),
        nodeId,
        edgeId: motionState === "exact_hold" ? admittedRoute.id : "",
        motionState,
      };
    }).sort((left, right) => compare(left.at, right.at) || compare(left.key, right.key));
  }

// Focus is a deterministic view over the already-admitted graph envelope.
// It never reaches around graph_envelope, invents relationships, or changes
// the source receipt: it ranks recorded frontier states and then admits their
// recorded one-hop neighbourhood until the visual budget is full.
function focusNodeIds(scene, suppliedLimit) {
  const limit = clamp(Math.floor(Number(suppliedLimit) || 60), 1, 60);
  const nodes = list(record(scene).nodes);
  const edges = list(record(scene).admittedEdges).length
    ? list(record(scene).admittedEdges)
    : list(record(scene).edges).filter(edge => edge.admitted !== false);
  const nodesById = new Map(nodes.map(node => [node.id, node]));
  const degree = new Map(nodes.map(node => [node.id, 0]));
  const adjacency = new Map(nodes.map(node => [node.id, new Set()]));
  edges.forEach(edge => {
    if (!nodesById.has(edge.source) || !nodesById.has(edge.target)) return;
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    adjacency.get(edge.source).add(edge.target);
    adjacency.get(edge.target).add(edge.source);
  });
  const terminal = new Set(["done", "complete", "completed", "closed", "archived"]);
  const running = new Set(["running", "active"]);
  const rank = node => {
    const status = text(node.status).toLowerCase();
    if (node.missing || ATTENTION.has(status)) return 0;
    if (running.has(status)) return 1;
    if (node.kind === "agent") return 2;
    if (!terminal.has(status) && ["work", "job", "missing_work"].includes(node.kind)) return 3;
    if (EVIDENCE_KINDS.has(node.kind)) return 4;
    return 5;
  };
  const ordered = [...nodes].sort((left, right) =>
    rank(left) - rank(right)
    || (degree.get(right.id) || 0) - (degree.get(left.id) || 0)
    || compare(left.id, right.id));
  const frontier = ordered.filter(node => rank(node) <= 3).slice(0, Math.min(24, limit));
  const selected = [];
  const selectedIds = new Set();
  const add = nodeId => {
    if (!nodeId || selectedIds.has(nodeId) || !nodesById.has(nodeId) || selected.length >= limit) return;
    selectedIds.add(nodeId);
    selected.push(nodeId);
  };
  frontier.forEach(node => add(node.id));
  frontier.forEach(node => {
    [...(adjacency.get(node.id) || [])]
      .sort((left, right) => {
        const leftNode = nodesById.get(left);
        const rightNode = nodesById.get(right);
        return rank(leftNode) - rank(rightNode)
          || (degree.get(right) || 0) - (degree.get(left) || 0)
          || compare(left, right);
      })
      .forEach(add);
  });
  ordered.forEach(node => add(node.id));
  return new Set(selected);
}

  function buildScene(model, suppliedOptions) {
    const options = { layout: "swarm", ...record(suppliedOptions) };
    const layout = ["swarm", "context", "critical"].includes(options.layout) ? options.layout : "swarm";
    const sceneModel = admittedSceneModel(model);
    const clusterResult = makeClusters(sceneModel, layout);
    const nodes = placeNodes(sceneModel, layout, clusterResult);
    const nodesById = new Map(nodes.map(node => [node.id, node]));
    const edges = placeEdges(sceneModel, nodesById);
    const occurrences = placeOccurrences(sceneModel, edges, nodesById);
    return {
      schemaVersion: "SpatialSceneV1",
      layoutRevision: "coord-spatial-deterministic-v1",
      layout,
      clusters: clusterResult.clusters,
      nodes,
      edges,
      admittedEdges: edges.filter(edge => edge.admitted),
      occurrences,
      nodesById,
      edgesById: new Map(edges.map(edge => [edge.id, edge])),
      topology: clusterResult.topology,
      topologyAvailability: topologyAvailability(sceneModel),
      lowInformation: clusterResult.lowInformation,
      sourceReceipt: sceneModel.receipt,
    };
  }

  function rotatePoint(point, camera) {
    const cy = Math.cos(camera.yaw);
    const sy = Math.sin(camera.yaw);
    const cp = Math.cos(camera.pitch);
    const sp = Math.sin(camera.pitch);
    const x1 = point.x * cy + point.z * sy;
    const z1 = -point.x * sy + point.z * cy;
    return {
      x: x1,
      y: point.y * cp - z1 * sp,
      z: point.y * sp + z1 * cp,
    };
  }

  function projectPoint(point, camera, viewport, suppliedProjection) {
    const projection = suppliedProjection === "flat" ? "flat" : "perspective";
    const rotated = projection === "flat"
      ? { x: point.x, y: point.y, z: point.z }
      : rotatePoint(point, camera);
    const base = Math.min(viewport.width / 1100, viewport.height / 560);
    const depthScale = projection === "perspective"
      ? clamp(900 / (900 - rotated.z), .64, 1.42)
      : 1;
    const scale = base * camera.zoom * depthScale;
    return {
      x: viewport.width / 2 + camera.panX + rotated.x * scale,
      y: viewport.height / 2 + camera.panY + rotated.y * scale,
      z: rotated.z,
      scale,
      depthScale,
      fog: projection === "perspective" ? clamp(.36 + depthScale * .48, .54, 1) : 1,
      projection,
    };
  }

  function projectedBounds(points, camera, viewport, projection) {
    const projected = list(points).map(point => projectPoint(point, camera, viewport, projection));
    if (!projected.length) {
      return {
        empty: true,
        minX: viewport.width / 2,
        maxX: viewport.width / 2,
        minY: viewport.height / 2,
        maxY: viewport.height / 2,
        width: 0,
        height: 0,
        centerX: viewport.width / 2,
        centerY: viewport.height / 2,
      };
    }
    const minX = Math.min(...projected.map(point => point.x));
    const maxX = Math.max(...projected.map(point => point.x));
    const minY = Math.min(...projected.map(point => point.y));
    const maxY = Math.max(...projected.map(point => point.y));
    return {
      empty: false,
      minX,
      maxX,
      minY,
      maxY,
      width: maxX - minX,
      height: maxY - minY,
      centerX: (minX + maxX) / 2,
      centerY: (minY + maxY) / 2,
    };
  }

  function fitCameraToPoints(points, camera, viewport, suppliedOptions) {
    const options = { padding: 72, minZoom: .25, maxZoom: 2.5, projection: "perspective", ...record(suppliedOptions) };
    const baseCamera = { ...camera, zoom: 1, panX: 0, panY: 0 };
    const bounds = projectedBounds(points, baseCamera, viewport, options.projection);
    if (bounds.empty) return { ...camera, zoom: 1, panX: 0, panY: 0 };
    const availableWidth = Math.max(1, viewport.width - options.padding * 2);
    const availableHeight = Math.max(1, viewport.height - options.padding * 2);
    const widthZoom = bounds.width > 0 ? availableWidth / bounds.width : options.maxZoom;
    const heightZoom = bounds.height > 0 ? availableHeight / bounds.height : options.maxZoom;
    const zoom = clamp(Math.min(widthZoom, heightZoom), options.minZoom, options.maxZoom);
    const offsetX = bounds.centerX - viewport.width / 2;
    const offsetY = bounds.centerY - viewport.height / 2;
    return {
      ...camera,
      zoom,
      panX: -offsetX * zoom,
      panY: -offsetY * zoom,
    };
  }

  root.CoordSwarmMeshModel = Object.freeze({
    buildScene,
    focusNodeIds,
    fitCameraToPoints,
    projectPoint,
    projectedBounds,
    rotatePoint,
    stableHash,
  });
}(window));
