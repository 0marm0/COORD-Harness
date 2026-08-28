(function (root) {
  "use strict";

  const DONE = new Set(["done", "complete", "completed", "closed", "archived"]);
  const ATTENTION = new Set(["blocked", "attention", "failed", "stuck"]);
  const RUNNING = new Set(["running", "active"]);
  const EVIDENCE_KINDS = new Set(["job", "artifact"]);
  const OPERATING_FOCUS_CAP = 60;

  const text = (value, fallback) => {
    const rendered = value === null || value === undefined ? "" : String(value).trim();
    return rendered || (fallback || "");
  };
  const list = value => Array.isArray(value) ? value : [];
  const record = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const compare = (left, right) => String(left).localeCompare(String(right));
  const unique = values => [...new Set(values.filter(Boolean))].sort(compare);
  const workNodeId = id => {
    const value = text(id);
    return value.startsWith("work:") || value.startsWith("job:") ? value : `work:${value}`;
  };
  const rowIdForNode = nodeId => text(nodeId).startsWith("work:") ? text(nodeId).slice(5) : text(nodeId);
  const lane = owner => text(owner, "unowned").split(":", 1)[0].toLowerCase();
  const statusClass = status => {
    const value = text(status, "unknown").toLowerCase();
    if (RUNNING.has(value)) return "running";
    if (ATTENTION.has(value)) return "attention";
    if (DONE.has(value)) return "done";
    return value.replace(/[^a-z0-9_-]/g, "-") || "unknown";
  };

  function graphDocument(documents) {
    const docs = record(documents);
    const graph = record(docs.graph);
    const operations = record(docs.operations);
    const envelope = record(operations.graph_envelope);
    const envelopePresent = text(envelope.schema_version) === "GraphEnvelopeV1" ||
      Object.prototype.hasOwnProperty.call(envelope, "nodes") ||
      Object.prototype.hasOwnProperty.call(envelope, "emitted");
    const graphInstant = text(graph.generated_at);
    const envelopeInstant = text(envelope.generated_at);
    const sourceMismatch = Boolean(envelopePresent && graphInstant && envelopeInstant && graphInstant !== envelopeInstant);

    // An explicitly present envelope is authoritative even when it emits zero
    // nodes. Empty can be the truthful bounded result; falling back to a raw
    // graph here would silently undo caps, quarantine, and omission accounting.
    if (envelopePresent) {
      return { graph: envelope, envelope, source: "operations.graph_envelope", sourceMismatch, envelopePresent: true };
    }
    return { graph, envelope, source: "legacy graph (envelope absent)", sourceMismatch: false, envelopePresent: false };
  }

  function envelopeReceipt(source) {
    const envelope = record(source.envelope);
    const graph = record(source.graph);
    const population = record(envelope.population);
    const eligible = record(envelope.eligible);
    const emitted = record(envelope.emitted);
    const omitted = record(envelope.omitted);
    const collisions = record(envelope.collisions);
    const nodeCollisions = record(collisions.nodes);
    const edgeCollisions = record(collisions.edges);
    const unknowns = record(envelope.unknowns);
    const envelopeSource = record(envelope.source);
    const nodes = list(graph.nodes).length;
    const edges = list(graph.edges).length;
    const nodeReasons = list(omitted.node_reasons).map(value => ({
      reason: text(record(value).reason, "unspecified"),
      count: Number.isFinite(record(value).count) ? record(value).count : 0,
    }));
    const edgeReasons = list(omitted.edge_reasons).map(value => ({
      reason: text(record(value).reason, "unspecified"),
      count: Number.isFinite(record(value).count) ? record(value).count : 0,
    }));
    const unknownRows = [
      ...list(unknowns.node_kinds).map(value => ({ domain: "node kind", ...record(value) })),
      ...list(unknowns.edge_kinds).map(value => ({ domain: "edge kind", ...record(value) })),
      ...list(unknowns.relationship_states).map(value => ({ domain: "relationship state", ...record(value) })),
    ].map(value => ({
      domain: text(value.domain),
      reason: text(value.reason, "unspecified"),
      count: Number.isFinite(value.count) ? value.count : 0,
    }));
    const collisionIdentities = (Number(nodeCollisions.identity_count) || 0) +
      (Number(edgeCollisions.identity_count) || 0);
    const unknownCount = unknownRows.reduce((sum, value) => sum + value.count, 0);
    return {
      populationNodes: Number.isFinite(population.nodes) ? population.nodes : nodes,
      populationEdges: Number.isFinite(population.edges) ? population.edges : edges,
      eligibleNodes: Number.isFinite(eligible.nodes) ? eligible.nodes : nodes,
      eligibleEdges: Number.isFinite(eligible.edges) ? eligible.edges : edges,
      emittedNodes: Number.isFinite(emitted.nodes) ? emitted.nodes : nodes,
      emittedEdges: Number.isFinite(emitted.edges) ? emitted.edges : edges,
      omittedNodes: Number.isFinite(omitted.nodes) ? omitted.nodes : 0,
      omittedEdges: Number.isFinite(omitted.edges) ? omitted.edges : 0,
      nodeReasons,
      edgeReasons,
      collisions: {
        nodes: nodeCollisions,
        edges: edgeCollisions,
        identityCount: collisionIdentities,
      },
      unknowns: unknownRows,
      unknownCount,
      envelopePresent: source.envelopePresent,
      complete: source.envelopePresent && envelope.complete === true &&
        !(omitted.nodes || omitted.edges),
      source: source.source,
      sourceMismatch: source.sourceMismatch,
      sourceStale: text(envelopeSource.freshness_state) === "stale",
      sourceFingerprint: text(envelopeSource.content_sha256),
    };
  }

  function normalize(documents) {
    const docs = record(documents);
    const snapshot = record(docs.snapshot);
    const context = record(docs.context);
    const timeline = record(docs.timeline);
    const source = graphDocument(docs);
    const suppliedNodes = list(source.graph.nodes);
    const suppliedEdges = list(source.graph.edges);
    const rows = list(snapshot.rows).map(row => ({ ...record(row) }));
    const sessions = list(snapshot.sessions).map(session => ({ ...record(session) }));
    const rowsById = new Map(rows.filter(row => text(row.id)).map(row => [text(row.id), row]));
    const contextById = new Map(list(context.items).filter(item => text(item && item.id)).map(item => [text(item.id), record(item)]));
    const timelineById = new Map(list(timeline.items).filter(item => text(item && item.id)).map(item => [text(item.id), list(item.events)]));
    const sessionsById = new Map(sessions.filter(session => text(session.id)).map(session => [text(session.id), session]));
    const nodesById = new Map();

    suppliedNodes.forEach(value => {
      const supplied = record(value);
      const id = text(supplied.id);
      if (!id || nodesById.has(id)) return;
      const rowId = rowIdForNode(id);
      const row = rowsById.get(rowId) || {};
      const kind = text(supplied.kind, "unknown");
      const column = EVIDENCE_KINDS.has(kind) ? "evidence" : "work";
      nodesById.set(id, {
        id,
        kind,
        column,
        label: text(row.title, text(supplied.label, id)),
        status: text(row.status, text(supplied.status, supplied.missing ? "missing" : "unknown")).toLowerCase(),
        owner: text(row.owner),
        module: text(row.module, "unassigned"),
        modules: [],
        currentStep: text(row.current_step),
        missing: Boolean(supplied.missing),
        rowId,
        row,
        graphNode: supplied,
      });
    });

    suppliedEdges.forEach(value => {
      const edge = record(value);
      [text(edge.source), text(edge.target)].forEach(id => {
        if (!id || nodesById.has(id)) return;
        nodesById.set(id, {
          id,
          kind: "missing_node",
          column: "work",
          label: `Missing endpoint: ${id}`,
          status: "missing",
          owner: "",
          module: "unassigned",
          modules: [],
          currentStep: "",
          missing: true,
          rowId: rowIdForNode(id),
          row: {},
          graphNode: {},
        });
      });
    });

    const ownerNames = new Set(sessions.map(session => text(session.id)).filter(Boolean));
    rows.forEach(row => { if (text(row.owner) && !text(row.id).startsWith("job:")) ownerNames.add(text(row.owner)); });
    [...ownerNames].sort(compare).forEach(owner => {
      const session = sessionsById.get(owner) || {};
      nodesById.set(`agent:${owner}`, {
        id: `agent:${owner}`,
        kind: "agent",
        column: "agent",
        label: text(session.label, owner),
        status: session.live === true ? "running" : "recorded",
        owner,
        actor: text(session.actor, lane(owner)),
        module: "",
        modules: [],
        currentStep: "",
        missing: false,
        rowId: "",
        row: {},
        session,
      });
    });

    const edges = [];
    const edgeIds = new Set();
    const addEdge = edge => {
      if (!edge.source || !edge.target || edgeIds.has(edge.id)) return;
      edgeIds.add(edge.id);
      edges.push(edge);
    };

    suppliedEdges.forEach((value, index) => {
      const supplied = record(value);
      const storedSource = text(supplied.source);
      const storedTarget = text(supplied.target);
      if (!storedSource || !storedTarget) return;
      const kind = text(supplied.kind, "unknown");
      const dependency = kind === "depends_on";
      addEdge({
        id: text(supplied.id, `edge:${index}:${storedSource}:${storedTarget}`),
        source: dependency ? storedTarget : storedSource,
        target: dependency ? storedSource : storedTarget,
        storedSource,
        storedTarget,
        kind,
        sourceField: text(supplied.source_field, "unspecified source"),
        relationshipState: text(supplied.relationship_state, "unknown"),
        missing: text(supplied.relationship_state) !== "source_bound" ||
          Boolean(nodesById.get(storedSource) && nodesById.get(storedSource).missing) ||
          Boolean(nodesById.get(storedTarget) && nodesById.get(storedTarget).missing),
        semantic: dependency ? "prerequisite provides to consumer" :
          (kind === "evidence" || kind === "runtime_evidence" ? "work records evidence" : kind),
        geometryReversed: dependency,
        supplied,
      });
    });

    rows.forEach(row => {
      if (text(row.id).startsWith("job:") || !text(row.owner)) return;
      const candidateIds = [workNodeId(row.id), text(row.id)];
      const nodeId = candidateIds.find(id => nodesById.has(id));
      const agentId = `agent:${text(row.owner)}`;
      if (!nodeId || !nodesById.has(agentId)) return;
      addEdge({
        id: `owns:${agentId}:${nodeId}`,
        source: agentId,
        target: nodeId,
        storedSource: agentId,
        storedTarget: nodeId,
        kind: "owns",
        sourceField: "snapshot.rows.owner",
        relationshipState: "source_bound",
        missing: false,
        semantic: "owner holds work",
        geometryReversed: false,
        supplied: {},
      });
    });

    const modules = new Map([...nodesById].map(([id, node]) => [id, new Set(node.module ? [node.module] : [])]));
    edges.forEach(edge => {
      const sourceNode = nodesById.get(edge.source);
      const targetNode = nodesById.get(edge.target);
      if (edge.kind === "owns" && targetNode) modules.get(edge.target).forEach(value => modules.get(edge.source).add(value));
      if ((edge.kind === "evidence" || edge.kind === "runtime_evidence") && sourceNode) {
        modules.get(edge.source).forEach(value => modules.get(edge.target).add(value));
      }
    });
    nodesById.forEach((node, id) => {
      node.modules = unique([...modules.get(id)]);
      if (!node.module && node.modules.length === 1) node.module = node.modules[0];
    });

    const activity = list(record(docs.operations).activity).length
      ? list(record(docs.operations).activity)
      : [...timelineById].flatMap(([id, events]) => events.map(event => ({ id, ...record(event) })));
    activity.sort((left, right) => compare(text(left.at), text(right.at)) || compare(text(left.id), text(right.id)));

    return {
      documents: docs,
      nodes: [...nodesById.values()].sort((a, b) => compare(a.id, b.id)),
      edges: edges.sort((a, b) => compare(a.id, b.id)),
      nodesById,
      rowsById,
      contextById,
      timelineById,
      activity,
      receipt: envelopeReceipt(source),
    };
  }

  function neighbourhood(nodes, edges, rootId, hops) {
    const nodeIds = new Set(nodes.map(node => node.id));
    if (!rootId || !nodeIds.has(rootId) || hops === "all") {
      return { nodes, edges, rootFound: Boolean(rootId && nodeIds.has(rootId)), hops: null, hiddenNodes: 0, hiddenEdges: 0 };
    }
    const depth = Number(hops);
    if (!Number.isInteger(depth) || depth < 0 || depth > 2) {
      return { nodes, edges, rootFound: true, hops: null, hiddenNodes: 0, hiddenEdges: 0 };
    }
    const adjacent = new Map(nodes.map(node => [node.id, new Set()]));
    edges.forEach(edge => {
      if (!adjacent.has(edge.source) || !adjacent.has(edge.target)) return;
      adjacent.get(edge.source).add(edge.target);
      adjacent.get(edge.target).add(edge.source);
    });
    const visible = new Set([rootId]);
    let frontier = [rootId];
    for (let current = 0; current < depth; current += 1) {
      const next = [];
      frontier.forEach(id => [...adjacent.get(id)].sort(compare).forEach(neighbour => {
        if (visible.has(neighbour)) return;
        visible.add(neighbour);
        next.push(neighbour);
      }));
      frontier = next;
    }
    const keptNodes = nodes.filter(node => visible.has(node.id));
    const keptEdges = edges.filter(edge => visible.has(edge.source) && visible.has(edge.target));
    return {
      nodes: keptNodes,
      edges: keptEdges,
      rootFound: true,
      hops: depth,
      hiddenNodes: nodes.length - keptNodes.length,
      hiddenEdges: edges.length - keptEdges.length,
    };
  }

  function operatingFocus(normalized, options) {
    if (options.expandOperating === true) {
      return {
        nodes: normalized.nodes,
        edges: normalized.edges,
        hiddenNodes: 0,
        hiddenEdges: 0,
        operatingFocusCap: OPERATING_FOCUS_CAP,
        operatingExpanded: true,
        note: "Expanded Operating view admits every client topology node before search, status, and vertical filters.",
      };
    }

    const recent = new Set();
    normalized.activity.slice(-24).forEach(value => {
      const rowId = text(record(value).id);
      if (!rowId) return;
      recent.add(rowId);
      recent.add(workNodeId(rowId));
    });
    const rank = node => {
      if (node.id === options.selectedId) return 0;
      if (node.missing || ATTENTION.has(node.status)) return 1;
      if (RUNNING.has(node.status)) return 2;
      if (recent.has(node.id) || recent.has(node.rowId)) return 3;
      if (!DONE.has(node.status)) return 4;
      if (EVIDENCE_KINDS.has(node.kind)) return 5;
      return 6;
    };
    const ordered = [...normalized.nodes].sort((left, right) =>
      rank(left) - rank(right) ||
      compare(left.module, right.module) ||
      compare(left.owner, right.owner) ||
      compare(left.id, right.id));
    const orderIndex = new Map(ordered.map((node, index) => [node.id, index]));
    const adjacent = new Map(normalized.nodes.map(node => [node.id, new Set()]));
    normalized.edges.forEach(edge => {
      if (adjacent.has(edge.source) && adjacent.has(edge.target)) {
        adjacent.get(edge.source).add(edge.target);
        adjacent.get(edge.target).add(edge.source);
      }
    });
    const kept = new Set();
    const admit = id => {
      if (kept.size < OPERATING_FOCUS_CAP && adjacent.has(id)) kept.add(id);
    };

    // Reserve part of the budget for directly recorded owners, prerequisites,
    // and evidence around the strongest signals instead of publishing a list of
    // disconnected high-ranked rows.
    ordered.filter(node => rank(node) <= 3).slice(0, 40).forEach(node => admit(node.id));
    [...kept].forEach(id => [...adjacent.get(id)]
      .sort((left, right) => orderIndex.get(left) - orderIndex.get(right) || compare(left, right))
      .forEach(admit));
    ordered.forEach(node => admit(node.id));

    const nodes = normalized.nodes.filter(node => kept.has(node.id));
    const edges = normalized.edges.filter(edge => kept.has(edge.source) && kept.has(edge.target));
    return {
      nodes,
      edges,
      hiddenNodes: normalized.nodes.length - nodes.length,
      hiddenEdges: normalized.edges.length - edges.length,
      operatingFocusCap: OPERATING_FOCUS_CAP,
      operatingExpanded: false,
      note: "Operating focus deterministically admits at most " + OPERATING_FOCUS_CAP + " high-signal client nodes, then directly recorded neighbours, before search, status, and vertical filters.",
    };
  }

  function modeCut(normalized, options) {
    const mode = text(options.mode, "operating");
    if (mode === "critical") {
      const critical = new Set(list(record(record(normalized.documents.operations).execution).critical_path).map(workNodeId));
      const keep = new Set(critical);
      normalized.edges.forEach(edge => {
        if (critical.has(edge.source) && (edge.kind === "owns" || edge.kind === "evidence" || edge.kind === "runtime_evidence")) keep.add(edge.target);
        if (critical.has(edge.target) && edge.kind === "owns") keep.add(edge.source);
      });
      const nodes = normalized.nodes.filter(node => keep.has(node.id));
      const edges = normalized.edges.filter(edge => keep.has(edge.source) && keep.has(edge.target));
      return {
        nodes,
        edges,
        hiddenNodes: normalized.nodes.length - nodes.length,
        hiddenEdges: normalized.edges.length - edges.length,
        note: critical.size ? "Critical-path work is shown in prerequisite-first execution order, with directly recorded owners and evidence." : "No critical path is present in the operations document.",
      };
    }
    if (mode === "neighbourhood") {
      if (!options.selectedId || !normalized.nodesById.has(options.selectedId)) {
        const focus = operatingFocus(normalized, { ...options, expandOperating: false });
        return {
          ...focus,
          note: "Select an admitted node to define a neighbourhood; the deterministic Operating focus remains visible until then.",
        };
      }
      const cut = neighbourhood(normalized.nodes, normalized.edges, options.selectedId, options.hops);
      return {
        ...cut,
        note: cut.rootFound
          ? (cut.hops === null ? "No neighbourhood cut is in force." : `${cut.hops}-hop undirected neighbourhood around ${options.selectedId}. Edge arrows retain recorded semantics.`)
          : "Select a node to define a neighbourhood; the complete emitted graph remains visible.",
      };
    }
    return operatingFocus(normalized, options);
  }

  function filterCut(modeResult, options) {
    const query = text(options.search).toLowerCase();
    const status = text(options.status, "operational");
    const moduleName = text(options.module, "all");
    const matches = node => {
      const searchable = [node.id, node.label, node.status, node.owner, node.module, node.currentStep, ...node.modules].join(" ").toLowerCase();
      if (query && !searchable.includes(query)) return false;
      if (moduleName !== "all" && !node.modules.includes(moduleName) && node.module !== moduleName) return false;
      if (status === "operational" && DONE.has(node.status)) return false;
      if (status !== "all" && status !== "operational" && node.status !== status) return false;
      return true;
    };
    const nodes = modeResult.nodes.filter(matches);
    const ids = new Set(nodes.map(node => node.id));
    const edges = modeResult.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target));
    return {
      nodes,
      edges,
      hiddenNodes: modeResult.nodes.length - nodes.length,
      hiddenEdges: modeResult.edges.length - edges.length,
    };
  }

  function dependencyOrder(workNodes, edges) {
    const ids = new Set(workNodes.map(node => node.id));
    const indegree = new Map(workNodes.map(node => [node.id, 0]));
    const outgoing = new Map(workNodes.map(node => [node.id, []]));
    edges.filter(edge => edge.kind === "depends_on" && ids.has(edge.source) && ids.has(edge.target)).forEach(edge => {
      outgoing.get(edge.source).push(edge.target);
      indegree.set(edge.target, indegree.get(edge.target) + 1);
    });
    outgoing.forEach(values => values.sort(compare));
    const rank = new Map(workNodes.map(node => [node.id, 0]));
    const queue = [...ids].filter(id => indegree.get(id) === 0).sort(compare);
    while (queue.length) {
      const source = queue.shift();
      outgoing.get(source).forEach(target => {
        rank.set(target, Math.max(rank.get(target), rank.get(source) + 1));
        indegree.set(target, indegree.get(target) - 1);
        if (indegree.get(target) === 0) {
          queue.push(target);
          queue.sort(compare);
        }
      });
    }
    return [...workNodes].sort((left, right) =>
      rank.get(left.id) - rank.get(right.id) ||
      compare(left.module, right.module) || compare(left.owner, right.owner) || compare(left.id, right.id));
  }

  function layout(nodes, edges) {
    const columns = {
      agent: nodes.filter(node => node.column === "agent").sort((a, b) =>
        Number(b.status === "running") - Number(a.status === "running") || compare(a.id, b.id)),
      work: dependencyOrder(nodes.filter(node => node.column === "work"), edges),
      evidence: nodes.filter(node => node.column === "evidence").sort((a, b) => compare(a.kind, b.kind) || compare(a.id, b.id)),
    };
    const x = { agent: 170, work: 570, evidence: 970 };
    const width = 1140;
    const rowGap = 76;
    const top = 66;
    const positions = new Map();
    Object.keys(columns).forEach(column => columns[column].forEach((node, index) => {
      positions.set(node.id, { x: x[column], y: top + index * rowGap, column });
    }));
    const height = Math.max(620, top * 2 + (Math.max(1, ...Object.values(columns).map(values => values.length)) - 1) * rowGap + 54);
    return { columns, positions, width, height, nodeWidth: 264, nodeHeight: 52 };
  }

  function build(documents, suppliedOptions) {
    const options = {
      mode: "operating",
      selectedId: null,
      hops: 1,
      search: "",
      status: "operational",
      module: "all",
      expandOperating: false,
      ...record(suppliedOptions),
    };
    const normalized = normalize(documents);
    const modeResult = modeCut(normalized, options);
    const filtered = filterCut(modeResult, options);
    const graphLayout = layout(filtered.nodes, filtered.edges);
    const selected = options.selectedId && normalized.nodesById.has(options.selectedId)
      ? normalized.nodesById.get(options.selectedId) : null;
    return {
      ...normalized,
      visibleNodes: filtered.nodes,
      visibleEdges: filtered.edges,
      layout: graphLayout,
      selected,
      options,
      scope: {
        emittedNodes: normalized.nodes.length,
        emittedEdges: normalized.edges.length,
        modeNodes: modeResult.nodes.length,
        modeEdges: modeResult.edges.length,
        visibleNodes: filtered.nodes.length,
        visibleEdges: filtered.edges.length,
        hiddenByModeNodes: modeResult.hiddenNodes,
        hiddenByModeEdges: modeResult.hiddenEdges,
        hiddenByFiltersNodes: filtered.hiddenNodes,
        hiddenByFiltersEdges: filtered.hiddenEdges,
        operatingFocusCap: modeResult.operatingFocusCap || null,
        operatingExpanded: modeResult.operatingExpanded === true,
        note: modeResult.note,
      },
      modules: unique(normalized.nodes.flatMap(node => node.modules)),
      statuses: unique(normalized.nodes.map(node => node.status)),
    };
  }

  function questionTarget(model, question) {
    const operations = record(model.documents.operations);
    const execution = record(operations.execution);
    const health = record(operations.health);
    let rowId = "";
    if (question === "impact") rowId = text(record(list(execution.impact)[0]).id);
    if (question === "attention") {
      rowId = text(list(health.missing_targets)[0]);
      if (!rowId) rowId = text(list(health.blocked_without_resume)[0]);
      if (!rowId) rowId = text(list(health.done_without_artifact)[0]);
      if (!rowId) {
        const row = [...model.rowsById.values()].find(value => ATTENTION.has(text(value.status).toLowerCase()));
        rowId = text(row && row.id);
      }
    }
    if (question === "leases") rowId = text(list(health.expired_claims)[0], text(list(health.expiring_claims)[0]));
    if (question === "recent") rowId = text(record(model.activity[model.activity.length - 1]).id);
    if (!rowId) return null;
    const candidates = [rowId, workNodeId(rowId)];
    return candidates.find(id => model.nodesById.has(id)) || null;
  }

  root.CoordOpsAtlasModel = Object.freeze({
    build,
    neighbourhood,
    OPERATING_FOCUS_CAP,
    questionTarget,
    rowIdForNode,
    statusClass,
  });
}(window));
