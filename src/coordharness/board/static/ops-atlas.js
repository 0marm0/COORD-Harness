(function (root, document) {
  "use strict";

  const AtlasModel = root.CoordOpsAtlasModel;
  if (!AtlasModel) throw new Error("COORD Operations Atlas model did not load");

  const BUNDLE_ENDPOINTS = Object.freeze([
    ["/api/v2/operations-bundle", "OpsAtlasBundleV2"],
    ["/api/v1/operations-bundle", "OpsAtlasBundleV1"],
  ]);
  const LEGACY_ENDPOINTS = Object.freeze({
    snapshot: "/api/v1/snapshot",
    graph: "/api/v1/graph",
    context: "/api/v1/context",
    timeline: "/api/v1/timeline",
    operations: "/api/v1/operations",
    readStatus: "/api/v1/read-status",
  });
  const DOCUMENT_NAMES = Object.freeze(Object.keys(LEGACY_ENDPOINTS));
  const SVG_NS = "http://www.w3.org/2000/svg";
  const AUTO_REFRESH_MS = 15000;
  const ZOOMS = [0.75, 1, 1.25, 1.5];
  const state = {
    documents: {},
    failures: new Map(),
    retained: new Set(),
    loading: false,
    frozen: false,
    selectedId: null,
    mode: "operating",
    expandOperating: false,
    hops: 1,
    search: "",
    status: "operational",
    module: "all",
    zoom: 1,
    fit: false,
    activeQuestion: null,
    model: null,
    timer: null,
    initialized: false,
    pendingReceipts: [],
    changedNodes: new Map(),
    changeDescription: "",
    walkRequest: null,
    walkDescription: "",
    motionDescription: "",
    transport: "pending",
    bundleSchema: "",
    transportWarning: "",
    bundleReceiptMismatch: false,
    refreshGeneration: 0,
    walkTimer: null,
    pendingWalkEdges: [],
    pendingHashSelection: undefined,
    focusSelectionAfterRender: false,
    selectionUnavailable: "",
    preludeResizeTimer: null,
    preludeCompact: null,
  };

  const byId = id => document.getElementById(id);
  const elements = {
    alert: byId("atlas-alert"),
    clock: byId("atlas-clock"),
    freeze: byId("atlas-freeze"),
    refresh: byId("atlas-refresh"),
    expand: byId("atlas-expand"),
    prelude: byId("atlas-prelude"),
    preludeAction: document.querySelector(".prelude-summary-action"),
    questions: byId("atlas-questions"),
    questionDock: byId("atlas-question-dock"),
    questionHome: document.querySelector(".intro-copy"),
    documentStages: byId("document-stages"),
    metrics: byId("atlas-metrics"),
    scope: byId("topology-scope"),
    receipts: byId("topology-receipts"),
    caption: byId("topology-caption"),
    search: byId("atlas-search"),
    status: byId("atlas-status"),
    module: byId("atlas-module"),
    hops: byId("atlas-hops"),
    hopsHelp: byId("atlas-hops-help"),
    zoomOut: byId("atlas-zoom-out"),
    zoomIn: byId("atlas-zoom-in"),
    zoomFit: byId("atlas-zoom-fit"),
    zoomValue: byId("atlas-zoom-value"),
    viewport: byId("topology-viewport"),
    svg: byId("topology-svg"),
    empty: byId("topology-empty"),
    lowInformation: byId("topology-low-information"),
    lowInformationReason: byId("topology-low-information-reason"),
    lowInformationFacts: byId("topology-low-information-facts"),
    technicalReceipt: byId("topology-technical-receipt"),
    technicalFacts: byId("topology-technical-facts"),
    nodeRoster: byId("topology-node-roster"),
    nodeRosterCount: byId("topology-node-roster-count"),
    nodeRosterItems: byId("topology-node-roster-items"),
    topologyActions: document.querySelector(".topology-actions"),
    inspector: byId("atlas-inspector"),
    activityCount: byId("activity-count"),
    activityChart: byId("activity-chart"),
    activityLedger: byId("activity-ledger"),
    healthState: byId("health-state"),
    healthLedger: byId("health-ledger"),
    impactTitle: byId("impact-title"),
    pathLength: byId("path-length"),
    criticalStrip: byId("critical-strip"),
    impactLedger: byId("impact-ledger"),
    fleetCount: byId("fleet-count"),
    fleetLedger: byId("fleet-ledger"),
    trafficPanel: byId("atlas-traffic"),
    trafficFreshness: byId("traffic-freshness"),
    trafficFacts: byId("traffic-facts"),
    trafficRoutes: byId("traffic-routes"),
    trafficTruth: byId("traffic-truth"),
  };

  const escapeHtml = value => String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[character]);
  const text = (value, fallback) => {
    const rendered = value === null || value === undefined ? "" : String(value).trim();
    return rendered || (fallback || "");
  };
  const list = value => Array.isArray(value) ? value : [];
  const record = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const number = value => Number.isFinite(value) ? value : null;
  const plural = (count, singular, other) => `${count} ${count === 1 ? singular : (other || `${singular}s`)}`;
  const short = (value, limit) => {
    const rendered = text(value);
    return rendered.length <= limit ? rendered : `${rendered.slice(0, Math.max(1, limit - 1))}…`;
  };
  const formatCount = value => number(value) === null ? "—" : Number(value).toLocaleString();
  const formatTime = value => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? text(value, "unknown time") : parsed.toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  };
  const rowNodeId = id => {
    const value = text(id);
    if (!state.model) return value;
    return [value, value.startsWith("work:") ? value : `work:${value}`].find(candidate => state.model.nodesById.has(candidate)) || value;
  };
  const reducedMotion = () => root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function selectionFromHash() {
    const parameters = new URLSearchParams(root.location.hash.replace(/^#/, ""));
    return parameters.has("sel") ? text(parameters.get("sel")) || null : null;
  }

  function writeSelectionHash(nodeId) {
    const parameters = new URLSearchParams(root.location.hash.replace(/^#/, ""));
    const selected = text(nodeId);
    if (selected) parameters.set("sel", selected);
    else parameters.delete("sel");
    const fragment = parameters.toString();
    const next = `${root.location.pathname}${root.location.search}${fragment ? `#${fragment}` : ""}`;
    root.history.replaceState(root.history.state, "", next);
  }

  function occurrenceList(documents) {
    const timeline = record(documents.timeline);
    const timelineEvents = list(timeline.items).flatMap(item =>
      list(record(item).events).map(event => ({ id: text(record(item).id), ...record(event) })));
    return timelineEvents.length ? timelineEvents : list(record(documents.operations).activity).map(event => ({ ...record(event) }));
  }

  const occurrenceKey = event => [event.id, event.at, event.kind, event.actor].map(value => text(value)).join("\u0000");
  const SNAPSHOT_FIELDS = Object.freeze(["status", "owner", "current_step", "progress_fraction", "stale"]);
  const snapshotState = snapshot => new Map(list(record(snapshot).rows).filter(row => text(record(row).id)).map(row => {
    const value = record(row);
    return [text(value.id), Object.fromEntries(SNAPSHOT_FIELDS.map(field => [field, String(value[field])]))];
  }));

  function changedFields(before, after) {
    return SNAPSHOT_FIELDS.filter(field => before[field] !== after[field]);
  }

  function documentLabel(name) {
    return text(name).replace(/([A-Z])/g, " $1").toLowerCase();
  }

  function optionValues(select, values, preserve) {
    const wanted = new Set(values);
    [...select.options].forEach(option => {
      if (option.dataset.dynamic === "true" && !wanted.has(option.value)) option.remove();
    });
    values.forEach(value => {
      if ([...select.options].some(option => option.value === value)) return;
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.dataset.dynamic = "true";
      select.append(option);
    });
    select.value = [...select.options].some(option => option.value === preserve) ? preserve : select.options[0].value;
  }

  function buildModel() {
    state.model = AtlasModel.build(state.documents, {
      mode: state.mode,
      selectedId: state.selectedId,
      hops: state.hops,
      search: state.search,
      status: state.status,
      module: state.module,
      expandOperating: state.expandOperating,
    });
    optionValues(elements.module, state.model.modules, state.module);
    optionValues(elements.status, state.model.statuses, state.status);
    state.module = elements.module.value;
    state.status = elements.status.value;
    prepareWalk();
  }

  function criticalEdgeIds(model) {
    const path = list(record(record(model.documents.operations).execution).critical_path).map(rowNodeId);
    const ids = [];
    for (let index = 0; index < path.length - 1; index += 1) {
      const edge = model.edges.find(value => value.kind === "depends_on" && value.source === path[index] && value.target === path[index + 1]);
      if (edge) ids.push(edge.id);
    }
    return ids.slice(0, 12);
  }

  function prepareWalk() {
    if (!state.walkRequest || !state.model) return;
    let edgeIds = [];
    if (state.walkRequest === "critical") {
      edgeIds = criticalEdgeIds(state.model);
      state.walkDescription = edgeIds.length
        ? `A bounded ${edgeIds.length}-relationship critical-path walk is highlighted prerequisite-first.`
        : "No emitted critical-path relationships are available to walk.";
    } else {
      const target = state.selectedId;
      const adjacent = state.model.visibleEdges.filter(edge => edge.source === target || edge.target === target);
      edgeIds = adjacent.slice(0, 8).map(edge => edge.id);
      state.walkDescription = edgeIds.length
        ? `A bounded ${edgeIds.length}-relationship walk is highlighted around the structured-question answer.`
        : "The structured-question answer has no currently visible relationships to walk.";
    }
    state.walkRequest = null;
    state.pendingWalkEdges = edgeIds;
  }

  function projectionIssues() {
    if (!state.model) return [];
    const receipt = state.model.receipt;
    const issues = [];
    if (!receipt.envelopePresent) issues.push("graph envelope absent; completeness is unknown");
    if (receipt.omittedNodes || receipt.omittedEdges || !receipt.complete) {
      issues.push(`envelope omitted ${receipt.omittedNodes} nodes and ${receipt.omittedEdges} relationships`);
    }
    if (receipt.collisions.identityCount) {
      issues.push(`${receipt.collisions.identityCount} quarantined identity collisions`);
    }
    if (receipt.unknownCount) issues.push(`${receipt.unknownCount} unknown graph terms`);
    if (receipt.sourceMismatch) issues.push("graph source generation does not match its envelope");
    if (receipt.sourceStale) issues.push("graph source declares itself stale");
    if (state.transport === "legacy") issues.push("documents arrived through the unbundled compatibility path");
    if (state.bundleReceiptMismatch) issues.push("bundle and read-status cache generations disagree");
    return issues;
  }

  function topologyAvailability() {
    const receipt = record(record(state.documents.operations).topology_availability);
    return text(receipt.schema_version) === "TopologyAvailabilityV1" ? receipt : {};
  }

  function lowInformationReceipt() {
    const receipt = topologyAvailability();
    return Object.keys(receipt).length && text(receipt.state) !== "available" ? receipt : null;
  }

  function formatReasonRows(rows) {
    return list(rows).slice(0, 8).map(value =>
      `${text(record(value).reason, "unspecified")} ×${formatCount(record(value).count)}`).join(", ");
  }

  function collisionReceipt(label, value) {
    const receipt = record(value);
    const count = Number(receipt.identity_count) || 0;
    if (!count) return "";
    const ids = list(receipt.ids).slice(0, 4);
    const named = ids.length ? `; ids ${ids.join(", ")}${receipt.truncated ? ", …" : ""}` : "";
    return `${label} ${formatCount(count)} identities / ${formatCount(receipt.entry_count)} entries${named}`;
  }

  function renderAlert() {
    const readStatus = record(state.documents.readStatus);
    const health = record(record(state.documents.operations).health);
    const futureSignal = list(health.signals).find(signal => text(signal.key) === "future_events");
    const futureCount = number(record(futureSignal).count) || 0;
    const serverDegraded = readStatus.degraded === true;
    if (!state.failures.size && !serverDegraded && !state.transportWarning && !state.bundleReceiptMismatch && !futureCount) {
      elements.alert.hidden = true;
      elements.alert.textContent = "";
      return;
    }
    const details = [...state.failures].map(([name, message]) =>
      state.retained.has(name) ? `${name} failed; the previous ${name} document is retained` : `${name} unavailable (${message})`);
    if (state.transportWarning) details.push(state.transportWarning);
    if (state.bundleReceiptMismatch) details.push("bundle cache_generation does not match read_status.cache_generation");
    if (serverDegraded) {
      details.push(`server cache refresh is degraded after ${formatCount(readStatus.consecutive_refresh_failures)} consecutive failures; class ${text(readStatus.last_failure_class, "not reported")}`);
    }
    if (futureCount) details.push(`${formatCount(futureCount)} recorded events occur after the document read clock`);
    elements.alert.textContent = `DEGRADED READ: ${details.join(". ")}. Retained documents stay visible and are labeled; this surface does not claim a complete live state.`;
    elements.alert.hidden = false;
  }

  function renderClock() {
    const readStatus = record(state.documents.readStatus);
    const health = record(record(state.documents.operations).health);
    const futureSignal = list(health.signals).find(signal => text(signal.key) === "future_events");
    const futureCount = number(record(futureSignal).count) || 0;
    const generated = text(readStatus.source_generated_at, text(record(state.documents.snapshot).generated_at, text(record(state.documents.operations).generated_at)));
    const degraded = state.failures.size || readStatus.degraded === true || projectionIssues().length || futureCount;
    const mode = state.frozen ? "FROZEN" : degraded ? "DEGRADED" : "LIVE";
    const generation = number(readStatus.cache_generation) === null ? "" : ` · cache generation ${readStatus.cache_generation}`;
    const failures = readStatus.degraded === true ? ` · ${formatCount(readStatus.consecutive_refresh_failures)} server refresh failures` : "";
    elements.clock.textContent = generated ? `${mode} · frozen copy ${formatTime(generated)}${generation}${failures}` : `${mode} · waiting for readable documents`;
  }

  function renderDocuments() {
    elements.documentStages.innerHTML = DOCUMENT_NAMES.map(name => {
      const doc = record(state.documents[name]);
      const present = Boolean(Object.keys(doc).length);
      const retained = state.retained.has(name);
      const stateLabel = retained ? "retained after failed refresh" :
        present ? (state.transport === "bundle" ? "same bundle generation" : "unbundled response") : "unavailable";
      return `<article class="document-stage${present ? "" : " missing"}"><i aria-hidden="true"></i>` +
        `<b>${escapeHtml(documentLabel(name))}</b><span>${escapeHtml(text(doc.schema_version, "no schema"))}</span>` +
        `<span>${escapeHtml(stateLabel)}${text(doc.generated_at) ? ` · ${escapeHtml(formatTime(doc.generated_at))}` : ""}</span></article>`;
    }).join("");
  }

  function renderMetrics() {
    const metrics = record(record(state.documents.operations).metrics);
    const health = record(record(state.documents.operations).health);
    const execution = record(record(state.documents.operations).execution);
    const topologyStatus = text(execution.topology_metrics_status, "available");
    const cycleLimited = topologyStatus.includes("cycle");
    const populationLimited = execution.analysis_population_truncated === true ||
      topologyStatus.includes("population");
    const populationTotal = number(execution.analysis_population_total) || 0;
    const populationEmitted = number(execution.analysis_population_emitted) || 0;
    const boundaryEdges = number(execution.analysis_boundary_dependencies_total) || 0;
    const missingEdges = number(execution.missing_dependencies_total) || 0;
    const unresolvedRows = number(execution.unresolved_tainted_total) || 0;
    const unresolvedLimited = topologyStatus.includes("unresolved") || boundaryEdges > 0 || missingEdges > 0;
    const topologyNotes = [];
    if (cycleLimited) topologyNotes.push("cycle-tainted rows withheld");
    if (populationLimited) {
      topologyNotes.push(`${formatCount(populationEmitted)} of ${formatCount(populationTotal)} rows analyzed`);
    }
    if (unresolvedLimited) {
      topologyNotes.push(`${formatCount(boundaryEdges)} boundary + ${formatCount(missingEdges)} missing edges; ${formatCount(unresolvedRows)} unique rows withheld`);
    }
    const topologyNote = topologyNotes.join("; ");
    const topologyPrefix = cycleLimited ? "Acyclic" : populationLimited ? "Bounded" : unresolvedLimited ? "Dependency-safe" : "";
    const issues = projectionIssues();
    const futureSignal = list(health.signals).find(signal => text(signal.key) === "future_events");
    const futureCount = number(record(futureSignal).count) || 0;
    const integrity = !Object.keys(health).length ? "—" : !health.ok ? "CHECK" : issues.length ? "PARTIAL" : "CLEAR";
    const integrityNote = !Object.keys(health).length ? "health document unavailable" :
      futureCount ? `${formatCount(futureCount)} events after read clock` :
      !health.ok ? "structural errors recorded" : issues.length ? `${issues.length} projection caveats` : "health and envelope clear";
    const definitions = [
      ["Work items", metrics.work_items, "snapshot work rows", ""],
      ["Live sessions", metrics.live_sessions, `${formatCount(metrics.sessions)} recorded`, "good"],
      ["Job projections", metrics.job_projections, "local telemetry rows", ""],
      ["Events / 24h", metrics.events_24h, `${formatCount(metrics.recorded_events)} bounded occurrences`, ""],
      ["Dependency-clear", metrics.dependency_clear_planned, topologyNote || "planned rows only", "good"],
      [topologyPrefix ? `${topologyPrefix} steps` : "Critical steps", metrics.critical_path_steps, topologyNote || "prerequisite-first", ""],
      [topologyPrefix ? `${topologyPrefix} width` : "Parallel width", metrics.max_parallel_width, topologyNote || "derived dependency layer", ""],
      ["Integrity", integrity, integrityNote, integrity === "CLEAR" ? "good" : integrity === "—" ? "" : "partial"],
    ];
    elements.metrics.innerHTML = definitions.map(([label, value, note, tone]) =>
      `<article class="atlas-metric ${escapeHtml(tone)}"><span>${escapeHtml(label)}</span>` +
      `<b>${escapeHtml(typeof value === "number" ? formatCount(value) : value)}</b><small>${escapeHtml(note)}</small></article>`).join("");
  }

  function renderScope() {
    const scope = state.model.scope;
    const receipt = state.model.receipt;
    const availability = lowInformationReceipt();
    if (availability) {
      elements.scope.textContent = "";
      elements.receipts.replaceChildren();
      elements.caption.textContent = "";
      return;
    }
    const focusLabel = scope.operatingFocusCap
      ? (scope.operatingExpanded
        ? "Expanded Operating view admits every client topology node before filters."
        : "Operating focus admits at most " + scope.operatingFocusCap + " deterministic high-signal client nodes before filters; Expand admitted reveals the full admitted client graph.")
      : scope.note;
    elements.scope.textContent = "Showing " + scope.visibleNodes + " nodes and " + scope.visibleEdges +
      " relationships after active filters. This view admits " + scope.modeNodes + " of " + scope.emittedNodes +
      " client topology nodes and " + scope.modeEdges + " of " + scope.emittedEdges +
      " client relationships; view focus withholds " + scope.hiddenByModeNodes + " nodes and " +
      scope.hiddenByModeEdges + " relationships, while filters hide " + scope.hiddenByFiltersNodes +
      " nodes and " + scope.hiddenByFiltersEdges + " relationships. Server envelope emitted " +
      receipt.emittedNodes + " of " + receipt.populationNodes + " whole-source nodes and " +
      receipt.emittedEdges + " of " + receipt.populationEdges + " source relationships. " + focusLabel;

    const rows = [];
    if (!receipt.envelopePresent) {
      rows.push(["concern", "Envelope absent — legacy graph completeness and omissions are unknown."]);
    } else if (receipt.complete && !receipt.unknownCount && !receipt.collisions.identityCount && !receipt.sourceMismatch && !receipt.sourceStale) {
      rows.push(["ok", `Envelope accounted for all ${receipt.populationNodes} nodes and ${receipt.populationEdges} relationships.`]);
    } else if (!receipt.omittedNodes && !receipt.omittedEdges) {
      rows.push(["neutral", "Envelope omitted no identities, but the caveats below prevent a clear/complete claim."]);
    }
    if (receipt.omittedNodes || receipt.omittedEdges) {
      rows.push(["concern", `Omitted ${receipt.omittedNodes} nodes and ${receipt.omittedEdges} relationships.`]);
    }
    if (receipt.nodeReasons.length) rows.push(["concern", `Node omission reasons: ${formatReasonRows(receipt.nodeReasons)}.`]);
    if (receipt.edgeReasons.length) rows.push(["concern", `Relationship omission reasons: ${formatReasonRows(receipt.edgeReasons)}.`]);
    const nodeCollision = collisionReceipt("Node collisions:", receipt.collisions.nodes);
    const edgeCollision = collisionReceipt("Relationship collisions:", receipt.collisions.edges);
    if (nodeCollision) rows.push(["concern", `${nodeCollision}.`]);
    if (edgeCollision) rows.push(["concern", `${edgeCollision}.`]);
    if (receipt.unknowns.length) {
      rows.push(["concern", `Unknown terms retained: ${receipt.unknowns.slice(0, 8).map(value => `${value.domain} ${value.reason} ×${formatCount(value.count)}`).join(", ")}.`]);
    }
    if (receipt.sourceMismatch) rows.push(["concern", "Source mismatch: raw graph and envelope generated_at values differ; the envelope remains the rendered authority."]);
    if (receipt.sourceStale) rows.push(["concern", "Source freshness receipt declares stale."]);
    if (state.transport === "legacy") rows.push(["concern", "Compatibility transport: documents were fetched separately because the bundle endpoint was absent."]);
    if (state.bundleReceiptMismatch) rows.push(["concern", "Bundle receipt mismatch: top-level and read-status cache generations differ."]);
    elements.receipts.innerHTML = rows.map(([tone, message]) =>
      `<span class="truth-receipt ${tone}">${escapeHtml(message)}</span>`).join("");

    elements.caption.textContent = `Columns encode node kind. Vertical order is deterministic grouping, not time, priority, or duration. Dependency arrows run prerequisite/provider → consumer for execution readability; the inspector preserves the stored consumer → prerequisite endpoints. ${state.walkDescription} ${state.changeDescription}`;
  }

  function sourceNodeRoster() {
    const envelope = record(record(state.documents.operations).graph_envelope);
    const query = state.search.toLowerCase();
    return list(envelope.nodes).map(value => record(value)).filter(node => {
      if (!query) return true;
      return [node.id, node.label, node.kind, node.status, node.owner, node.module]
        .map(value => text(value).toLowerCase()).some(value => value.includes(query));
    });
  }

  function renderLowInformation() {
    const receipt = lowInformationReceipt();
    const active = Boolean(receipt);
    document.body.classList.toggle("atlas-low-information", active);
    elements.lowInformation.hidden = !active;
    if (!active) return false;

    const population = record(receipt.population);
    const admitted = record(receipt.admitted);
    const omitted = record(receipt.omitted);
    const source = record(receipt.source);
    const freshness = record(receipt.freshness);
    elements.lowInformationReason.textContent = text(receipt.reason, "No authoritative topology was admitted.");
    const facts = [
      ["Admission", text(receipt.state, "low_information").replaceAll("_", " ")],
      ["Source", `${text(source.graph_declared, "unknown")} · ${text(source.graph_schema_version, "unknown schema")}`],
      ["Freshness", `${text(freshness.state, "unknown")} · ${text(freshness.generated_at) ? formatTime(freshness.generated_at) : "generation unavailable"} · ${formatCount(freshness.document_skew_seconds)}s skew`],
    ];
    elements.lowInformationFacts.innerHTML = facts.map(([label, value]) =>
      `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
    const technicalFacts = [
      ["Reason code", text(receipt.reason_code, "unavailable")],
      ["Population", `${formatCount(population.work_items)} work · ${formatCount(population.nodes)} nodes · ${formatCount(population.edges)} edges · ${formatCount(population.events)} events`],
      ["Admitted", `${formatCount(admitted.work_items)} work · ${formatCount(admitted.nodes)} nodes · ${formatCount(admitted.edges)} edges · ${formatCount(admitted.events)} events`],
      ["Omitted", `${formatCount(omitted.work_items)} work · ${formatCount(omitted.nodes)} nodes · ${formatCount(omitted.edges)} edges · ${formatCount(omitted.events)} events`],
      ["Missing prerequisite", text(receipt.missing_prerequisite, "authoritative_graph_relationships")],
      ["Source fingerprint", text(source.graph_content_sha256, "not reported")],
    ];
    elements.technicalFacts.innerHTML = technicalFacts.map(([label, value]) =>
      `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

    const roster = sourceNodeRoster();
    const total = list(record(record(state.documents.operations).graph_envelope).nodes).length;
    elements.nodeRoster.hidden = false;
    elements.nodeRosterCount.textContent = `${formatCount(roster.length)} of ${formatCount(total)}`;
    elements.nodeRosterItems.innerHTML = roster.length ? roster.slice(0, 40).map(node =>
      `<li><b>${escapeHtml(text(node.label, node.id))}</b><span>${escapeHtml(text(node.kind, "node"))} · ${escapeHtml(text(node.status, "unreported"))} · ${escapeHtml(text(node.id))}</span></li>`).join("") :
      "<li><b>No admitted nodes match this search.</b><span>Clear the node search to restore the source roster.</span></li>";
    state.pendingReceipts = [];
    state.pendingWalkEdges = [];
    state.changedNodes.clear();
    state.motionDescription = "";
    return true;
  }

  function svgElement(name, attributes) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes || {}).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  }

  function edgePath(edge, index) {
    const layout = state.model.layout;
    const source = layout.positions.get(edge.source);
    const target = layout.positions.get(edge.target);
    if (!source || !target) return "";
    const half = layout.nodeWidth / 2;
    if (source.column === target.column) {
      const side = source.column === "evidence" ? -1 : 1;
      const x = source.x + side * half;
      const bend = x + side * (54 + (index % 5) * 12);
      const endX = target.x + side * half;
      return `M ${x} ${source.y} C ${bend} ${source.y}, ${bend} ${target.y}, ${endX} ${target.y}`;
    }
    const forward = source.x < target.x;
    const startX = source.x + (forward ? half : -half);
    const endX = target.x + (forward ? -half : half);
    const middle = (startX + endX) / 2;
    return `M ${startX} ${source.y} C ${middle} ${source.y}, ${middle} ${target.y}, ${endX} ${target.y}`;
  }

  function markerKind(edge) {
    if (edge.missing) return "missing";
    if (edge.kind === "owns") return "owns";
    if (edge.kind === "evidence" || edge.kind === "runtime_evidence") return "evidence";
    return "depends";
  }

  function clearMotion() {
    if (state.walkTimer) root.clearTimeout(state.walkTimer);
    state.walkTimer = null;
    elements.svg.querySelectorAll(".atlas-occurrence-receipt").forEach(token => token.remove());
    elements.svg.querySelectorAll(".atlas-edge.walking").forEach(edge => edge.classList.remove("walking"));
    state.pendingWalkEdges = [];
    state.pendingReceipts = [];
  }

  function startEdgeWalk() {
    const edgeIds = state.pendingWalkEdges.splice(0, 12);
    if (!edgeIds.length || state.frozen || reducedMotion()) return;
    if (state.walkTimer) root.clearTimeout(state.walkTimer);
    let index = 0;
    const step = () => {
      elements.svg.querySelectorAll(".atlas-edge.walking").forEach(edge => edge.classList.remove("walking"));
      if (index >= edgeIds.length || state.frozen) return;
      const edge = [...elements.svg.querySelectorAll("[data-edge-id]")].find(value => value.dataset.edgeId === edgeIds[index]);
      index += 1;
      if (!edge) {
        step();
        return;
      }
      edge.classList.add("walking");
      state.walkTimer = root.setTimeout(step, 620);
    };
    step();
  }

  function announceMotionDescription() {
    elements.caption.textContent += ` ${state.motionDescription}`;
    const description = byId("atlas-svg-description");
    if (description) description.textContent += ` ${state.motionDescription}`;
  }

  function renderMotionReceipts() {
    const receipts = state.pendingReceipts.splice(0, 12);
    if (!receipts.length || state.frozen) return;
    if (reducedMotion()) {
      state.motionDescription = `This refresh recorded ${receipts.length} new occurrence ${receipts.length === 1 ? "receipt" : "receipts"}; motion is suppressed by the reduced-motion preference, and no progress is implied.`;
      announceMotionDescription();
      return;
    }
    let moved = 0;
    let stationaryMismatch = 0;
    let unavailable = 0;
    receipts.forEach((event, receiptIndex) => {
      const target = rowNodeId(event.id);
      const holds = state.model.visibleEdges.filter(edge => edge.kind === "owns" && edge.target === target);
      if (!holds.length) {
        unavailable += 1;
        return;
      }
      const actor = text(event.actor).toLowerCase();
      const edge = holds.find(value => {
        const source = state.model.nodesById.get(value.source) || {};
        return actor && text(source.actor).toLowerCase() === actor;
      });
      const kind = text(event.kind, "event").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
      if (!edge) {
        const position = state.model.layout.positions.get(target);
        if (!position) {
          unavailable += 1;
          return;
        }
        const recordedActors = [...new Set(holds.map(value => {
          const source = state.model.nodesById.get(value.source) || {};
          return text(source.actor, text(source.owner, "unknown"));
        }))].sort();
        const expected = recordedActors.join(", ");
        const token = svgElement("circle", {
          class: `atlas-occurrence-receipt atlas-stationary-token mismatch ${kind}`,
          cx: position.x - 142, cy: position.y, r: "5", role: "img",
          "aria-label": `New recorded ${text(event.kind, "event")} occurrence by ${text(event.actor, "unknown")} for ${event.id}; stationary because the actor identity does not match recorded owner ${expected}; no owner attribution was inferred.`,
        });
        const title = svgElement("title");
        title.textContent = `Actor mismatch receipt: ${text(event.actor, "unknown")} does not match recorded owner identity ${expected}; stationary, with no inferred attribution.`;
        token.append(title);
        elements.svg.append(token);
        stationaryMismatch += 1;
        return;
      }
      const edgeIndex = state.model.visibleEdges.indexOf(edge);
      const path = edgePath(edge, Math.max(0, edgeIndex));
      if (!path) {
        unavailable += 1;
        return;
      }
      const token = svgElement("circle", {
        class: `atlas-occurrence-receipt atlas-motion-token ${kind}`, r: "4", role: "img",
        "aria-label": `New recorded ${text(event.kind, "event")} occurrence by ${text(event.actor, "unknown")} for ${event.id}; token follows the matching recorded owner-holds-work relationship. Motion is a receipt, not progress.`,
      });
      const title = svgElement("title");
      title.textContent = `Actor-matched occurrence receipt: ${text(event.kind, "event")} by ${text(event.actor, "unknown")} at ${text(event.at, "unknown time")}; motion is not progress.`;
      const motion = svgElement("animateMotion", {
        dur: "1.1s", begin: `${Math.min(receiptIndex, 8) * 0.14}s`, path, fill: "freeze",
      });
      token.append(title, motion);
      elements.svg.append(token);
      moved += 1;
    });
    const parts = [];
    if (moved) parts.push(`animated ${moved} actor-matched occurrence ${moved === 1 ? "receipt" : "receipts"} along recorded owner-holds-work edges`);
    if (stationaryMismatch) parts.push(`kept ${stationaryMismatch} actor-mismatched ${stationaryMismatch === 1 ? "receipt" : "receipts"} stationary and neutral, with no owner attribution inferred`);
    if (unavailable) parts.push(`could not place ${unavailable} ${unavailable === 1 ? "receipt" : "receipts"} because no matching visible recorded hold exists`);
    state.motionDescription = `This refresh ${parts.join("; ")}. These marks are occurrence evidence, not work progress.`;
    announceMotionDescription();
    root.setTimeout(() => elements.svg.querySelectorAll(".atlas-occurrence-receipt").forEach(token => token.remove()), 2400);
  }

  function renderGraph() {
    const model = state.model;
    const svg = elements.svg;
    svg.replaceChildren();
    if (lowInformationReceipt()) {
      elements.empty.hidden = true;
      elements.viewport.hidden = true;
      elements.zoomValue.textContent = state.fit ? "FIT" : `${Math.round(state.zoom * 100)}%`;
      state.pendingReceipts = [];
      state.pendingWalkEdges = [];
      state.changedNodes.clear();
      return;
    }
    elements.empty.hidden = model.visibleNodes.length > 0;
    elements.viewport.hidden = model.visibleNodes.length === 0;
    if (!model.visibleNodes.length) {
      state.pendingReceipts = [];
      state.pendingWalkEdges = [];
      state.changedNodes.clear();
      return;
    }

    const zoom = state.fit ? 1 : state.zoom;
    svg.setAttribute("viewBox", `0 0 ${model.layout.width} ${model.layout.height}`);
    svg.setAttribute("width", state.fit ? "100%" : String(Math.round(model.layout.width * zoom)));
    svg.setAttribute("height", String(Math.round(model.layout.height * (state.fit ? 1 : zoom))));
    svg.setAttribute("data-zoom", state.fit ? "fit" : String(zoom));
    svg.setAttribute("class", state.fit ? "zoom-fit" : `zoom-${Math.round(zoom * 100)}`);
    elements.zoomValue.textContent = state.fit ? "FIT" : `${Math.round(zoom * 100)}%`;

    const title = svgElement("title", { id: "atlas-svg-title" });
    title.textContent = "Coordination topology: agents and owners to work to recorded evidence";
    const description = svgElement("desc", { id: "atlas-svg-description" });
    description.textContent = `${model.visibleNodes.length} shown nodes and ${model.visibleEdges.length} shown relationships. Dependency arrows point from prerequisite provider to consumer, reversing only the geometry of stored consumer-to-prerequisite depends_on fields. ${state.walkDescription}`;
    svg.append(title, description);

    const defs = svgElement("defs");
    ["depends", "owns", "evidence", "missing"].forEach(kind => {
      const marker = svgElement("marker", {
        id: `atlas-arrow-${kind}`, class: `atlas-arrow ${kind}`, viewBox: "0 0 10 10",
        refX: "9", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse",
      });
      marker.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z" }));
      defs.append(marker);
    });
    svg.append(defs);

    [["AGENTS / OWNERS", 38], ["WORK / DEPENDENCY", 438], ["RECORDED EVIDENCE", 838]].forEach(([label, x]) => {
      const heading = svgElement("text", { x, y: 25, class: "atlas-column-label" });
      heading.textContent = label;
      const rule = svgElement("line", { x1: Number(x) + 132, y1: 36, x2: Number(x) + 132, y2: model.layout.height - 24, class: "atlas-column-rule" });
      svg.append(heading, rule);
    });

    model.visibleEdges.forEach((edge, index) => {
      const kind = markerKind(edge);
      const path = svgElement("path", {
        d: edgePath(edge, index),
        class: `atlas-edge ${edge.kind}${edge.missing ? " missing" : ""}`,
        "marker-end": `url(#atlas-arrow-${kind})`,
        "data-edge-id": edge.id,
      });
      const stored = edge.geometryReversed
        ? `Displayed ${edge.source} prerequisite/provider to ${edge.target} consumer. Stored ${edge.storedSource} consumer depends_on ${edge.storedTarget} prerequisite.`
        : `${edge.source} to ${edge.target}.`;
      const edgeTitle = svgElement("title");
      edgeTitle.textContent = `${edge.semantic}. ${stored} Source: ${edge.sourceField}.`;
      path.append(edgeTitle);
      svg.append(path);
    });

    model.visibleNodes.forEach(node => {
      const position = model.layout.positions.get(node.id);
      if (!position) return;
      const classes = ["atlas-node", node.kind, AtlasModel.statusClass(node.status)];
      if (node.column === "evidence") classes.push("evidence");
      if (node.missing) classes.push("missing");
      if (node.id === state.selectedId) classes.push("selected");
      const changed = state.changedNodes.get(node.id) || [];
      if (changed.length) classes.push("recent");
      const changeReceipt = changed.length ?
        ` Changed fields on this refresh: ${changed.join(", ")}. Pulse is a change receipt, not progress.` : "";
      const group = svgElement("g", {
        class: classes.join(" "), transform: `translate(${position.x} ${position.y})`,
        tabindex: "0", role: "button", "data-node-id": node.id,
        "aria-label": `${node.kind} ${node.label}; ${node.status}; id ${node.id}.${changeReceipt}`,
      });
      const nodeTitle = svgElement("title");
      nodeTitle.textContent = `${node.label}; id ${node.id}; kind ${node.kind}; status ${node.status}.${changeReceipt}`;
      group.append(nodeTitle);
      if (changed.length) {
        const changeLabel = svgElement("text", { class: "node-change-label", x: -128, y: -33 });
        changeLabel.textContent = short(`changed: ${changed.join(", ")} · not progress`, 48);
        group.append(changeLabel);
      }
      group.append(svgElement("rect", { class: "node-shell", x: -132, y: -26, width: 264, height: 52, rx: 10 }));
      group.append(svgElement("rect", { class: "node-accent", x: -132, y: -26, width: 5, height: 52, rx: 2 }));
      group.append(svgElement("circle", { class: "node-mark", cx: -112, cy: 0, r: 10 }));
      const label = svgElement("text", { class: "node-title", x: -94, y: -4 });
      label.textContent = short(node.label, 36);
      const meta = svgElement("text", { class: "node-meta", x: -94, y: 13 });
      meta.textContent = short(`${node.status} · ${node.id}`, 46);
      group.append(label, meta);
      svg.append(group);
    });
    renderMotionReceipts();
    startEdgeWalk();
    state.changedNodes.clear();
  }

  function badges(node) {
    const values = [node.kind, node.status, ...node.modules];
    return `<div class="inspector-badges">${values.filter(Boolean).map(value =>
      `<span class="badge ${escapeHtml(AtlasModel.statusClass(value))}">${escapeHtml(value)}</span>`).join("")}</div>`;
  }

  function relationLabel(edge, nodeId) {
    if (edge.kind === "depends_on") {
      return nodeId === edge.source
        ? `provides to consumer ${edge.target}`
        : `consumes prerequisite ${edge.source}`;
    }
    if (edge.kind === "owns") return nodeId === edge.source ? `holds ${edge.target}` : `held by ${edge.source}`;
    return nodeId === edge.source ? `${edge.kind} → ${edge.target}` : `${edge.kind} ← ${edge.source}`;
  }

  function renderInspector() {
    const node = state.model.selected;
    const inspectorOpen = Boolean(node || state.selectedId);
    document.body.classList.toggle("atlas-has-selection", Boolean(node));
    document.body.classList.toggle("atlas-inspector-open", inspectorOpen);
    elements.inspector.hidden = !inspectorOpen;
    if (!node) {
      const missing = state.selectedId && state.selectionUnavailable === "not_admitted"
        ? `<p>The requested selection <code>${escapeHtml(state.selectedId)}</code> is not admitted in this projection's authoritative <code>operations.graph_envelope</code>. This does not establish that it is absent from <code>coord.db</code>.</p>`
        : state.selectedId ? `<p>The prior selection <code>${escapeHtml(state.selectedId)}</code> is not present in the currently retained documents.</p>` :
        "<p>Select an agent, work row, job, or artifact to inspect its recorded relationships and occurrence-only timeline.</p>";
      elements.inspector.innerHTML = `<div class="inspector-empty"><p class="eyebrow">SELECTED NODE</p>` +
        `<h2 id="inspector-title">${state.selectedId ? "Selection unavailable." : "Walk the graph."}</h2>${missing}</div>`;
      return;
    }
    const context = record(state.model.contextById.get(node.rowId));
    const relations = state.model.edges.filter(edge => edge.source === node.id || edge.target === node.id);
    const events = list(state.model.timelineById.get(node.rowId));
    const relationHtml = relations.length ? relations.map(edge => {
      const related = edge.source === node.id ? edge.target : edge.source;
      return `<button type="button" data-select-node="${escapeHtml(related)}"><span>↗</span>` +
        `<span><b>${escapeHtml(relationLabel(edge, node.id))}</b><small>${escapeHtml(edge.sourceField)}</small></span></button>`;
    }).join("") : "<p class=\"meta\">No emitted relationship names this node.</p>";
    const eventHtml = events.length ? events.slice(-12).reverse().map(event =>
      `<div class="mini-event"><time>${escapeHtml(formatTime(event.at))}</time><i aria-hidden="true"></i>` +
      `<span>${escapeHtml(text(event.kind, "event"))}<small>${escapeHtml(text(event.actor, "unknown"))}</small></span></div>`).join("") :
      "<p class=\"meta\">No occurrence records are present for this node.</p>";
    const storedDependency = relations.some(edge => edge.geometryReversed)
      ? "Dependency geometry is provider → consumer; stored endpoints remain consumer → prerequisite."
      : "No dependency geometry is reversed for this node.";
    elements.inspector.innerHTML = `<div class="inspector-content"><header class="inspector-head">` +
      `<p class="eyebrow">${escapeHtml(node.column)} / ${escapeHtml(node.kind)}</p><h2 id="inspector-title">${escapeHtml(node.label)}</h2>` +
      `<code>${escapeHtml(node.id)}</code>${node.currentStep ? `<p class="step">${escapeHtml(node.currentStep)}</p>` : ""}${badges(node)}</header>` +
      `<dl class="inspector-facts"><dt>Status</dt><dd>${escapeHtml(node.status)}</dd><dt>Owner</dt><dd>${escapeHtml(node.owner || "not recorded")}</dd>` +
      `<dt>Vertical</dt><dd>${escapeHtml(node.module || "not recorded")}</dd><dt>Source semantics</dt><dd>${escapeHtml(storedDependency)}</dd>` +
      `<dt>Done signal</dt><dd>${escapeHtml(text(context.done_signal, "not present in bounded context"))}</dd>` +
      `<dt>Artifact</dt><dd>${Object.keys(context).length ? (context.artifact_recorded ? "recorded" : "not recorded") : "not available"}</dd></dl>` +
      `<section class="inspector-section"><h3>Recorded relationships</h3><div class="inspector-links">${relationHtml}</div></section>` +
      `<section class="inspector-section"><h3>Occurrence-only timeline</h3><div class="mini-timeline">${eventHtml}</div></section></div>`;
  }

  function renderActivity() {
    const activity = state.model.activity;
    elements.activityCount.textContent = plural(activity.length, "occurrence");
    const buckets = new Map();
    activity.forEach(event => {
      const parsed = new Date(event.at);
      const key = Number.isNaN(parsed.getTime()) ? text(event.at) : parsed.toISOString().slice(0, 13);
      buckets.set(key, (buckets.get(key) || 0) + 1);
    });
    const values = [...buckets].slice(-12);
    if (!values.length) {
      elements.activityChart.innerHTML = "<p class=\"meta\">No occurrence buckets are present.</p>";
    } else {
      const maximum = Math.max(1, ...values.map(value => value[1]));
      const chartWidth = 600;
      const startX = 28;
      const endX = chartWidth - 28;
      const points = values.map((value, index) => {
        const x = values.length === 1 ? chartWidth / 2 : startX + index * ((endX - startX) / (values.length - 1));
        const y = 68 - (value[1] / maximum) * 54;
        return { value, x, y };
      });
      const pointList = points.map(point => point.x + "," + point.y).join(" ");
      const pointReceipts = points.map(point => {
        const label = point.value[0] + ": " + point.value[1] + " recorded occurrences";
        return "<circle class=\"activity-point\" cx=\"" + point.x + "\" cy=\"" + point.y +
          "\" r=\"3\" tabindex=\"0\" role=\"img\" aria-label=\"" + escapeHtml(label) +
          "\"><title>" + escapeHtml(label) + "</title></circle>";
      }).join(" ");
      elements.activityChart.innerHTML = "<svg viewBox=\"0 0 600 96\" preserveAspectRatio=\"none\" role=\"img\" aria-label=\"Recorded event occurrence volume across " +
        values.length + " timestamp buckets; focus a point for its exact count\">" +
        "<line class=\"activity-grid\" x1=\"0\" y1=\"68\" x2=\"600\" y2=\"68\"></line>" +
        "<polygon class=\"activity-area\" points=\"" + startX + ",68 " + pointList + " " + endX + ",68\"></polygon>" +
        "<polyline class=\"activity-line\" points=\"" + pointList + "\"></polyline>" + pointReceipts +
        "<text class=\"activity-axis-label\" x=\"" + startX + "\" y=\"89\">" + escapeHtml(values[0][0]) + "</text>" +
        "<text class=\"activity-axis-label end\" x=\"" + endX + "\" y=\"89\">" + escapeHtml(values[values.length - 1][0]) + "</text></svg>";
    }
    elements.activityLedger.innerHTML = activity.length ? [...activity].reverse().slice(0, 40).map(event =>
      `<li><time>${escapeHtml(formatTime(event.at))}</time><span class="event-kind">${escapeHtml(text(event.kind, "event"))}</span>` +
      `<button type="button" data-select-row="${escapeHtml(event.id)}">${escapeHtml(event.id)}</button><small>${escapeHtml(text(event.actor, "unknown"))}</small></li>`).join("") :
      "<li><span class=\"meta\">No recorded event occurrences in this bounded document.</span></li>";
  }

  function signalTarget(key) {
    const health = record(record(state.documents.operations).health);
    const values = list(health[key]);
    if (key === "cycles") return text(list(values[0])[0]);
    return text(values[0]);
  }

  function renderHealth() {
    const health = record(record(state.documents.operations).health);
    const signals = list(health.signals);
    const issues = projectionIssues();
    elements.healthState.textContent = !Object.keys(health).length ? "UNAVAILABLE" :
      !health.ok ? "INTERVENTION" : issues.length ? "PARTIAL" : "CLEAR";
    elements.healthLedger.innerHTML = signals.length ? signals.map(signal => {
      const count = number(signal.count) || 0;
      const target = signalTarget(signal.key);
      const tone = count ? text(signal.severity, "warning") : "clean";
      return `<div class="health-item ${escapeHtml(tone)}"><span class="health-count">${escapeHtml(count)}</span>` +
        `<span><b>${escapeHtml(text(signal.label, signal.key))}</b><small>${escapeHtml(signal.key)}</small></span>` +
        `${target ? `<button type="button" data-select-row="${escapeHtml(target)}">Inspect</button>` : ""}</div>`;
    }).join("") : "<p class=\"meta\">No health signal document is available.</p>";
  }

  function renderImpact() {
    const execution = record(record(state.documents.operations).execution);
    const critical = list(execution.critical_path);
    const impact = list(execution.impact);
    const cycleImpact = list(execution.cycle_impact);
    const cycleTainted = list(execution.cycle_tainted);
    const topologyStatus = text(execution.topology_metrics_status, "available");
    const cycleLimited = topologyStatus.includes("cycle");
    const populationLimited = execution.analysis_population_truncated === true ||
      topologyStatus.includes("population");
    const counted = (value, fallback) => number(value) === null ? fallback : number(value);
    const populationTotal = counted(execution.analysis_population_total, 0);
    const populationEmitted = counted(execution.analysis_population_emitted, populationTotal);
    const populationOmitted = counted(execution.analysis_population_omitted, Math.max(0, populationTotal - populationEmitted));
    const boundaryDependenciesTotal = counted(execution.analysis_boundary_dependencies_total, 0);
    const boundaryDependenciesEmitted = counted(execution.analysis_boundary_dependencies_emitted, boundaryDependenciesTotal);
    const boundaryTaintedTotal = counted(execution.analysis_boundary_tainted_total, 0);
    const missingDependenciesTotal = counted(execution.missing_dependencies_total, 0);
    const missingDependenciesEmitted = counted(execution.missing_dependencies_emitted, missingDependenciesTotal);
    const missingTaintedTotal = counted(execution.missing_dependency_tainted_total, 0);
    const unresolvedTaintedTotal = counted(execution.unresolved_tainted_total, 0);
    const unresolvedLimited = topologyStatus.includes("unresolved") ||
      boundaryDependenciesTotal > 0 || missingDependenciesTotal > 0;
    const cycleTaintedTotal = counted(execution.cycle_tainted_total, cycleTainted.length);
    const cycleTaintedEmitted = counted(execution.cycle_tainted_emitted, cycleTainted.length);
    const cycleComponentsTotal = counted(execution.cycle_components_total, cycleImpact.length);
    const cycleComponentsEmitted = counted(execution.cycle_components_emitted, cycleImpact.length);
    const cycleMemberIdsTotal = counted(
      execution.cycle_member_ids_total,
      cycleImpact.reduce((sum, item) => sum + list(record(item).members).length, 0));
    const cycleMemberIdsEmitted = counted(
      execution.cycle_member_ids_emitted,
      cycleImpact.reduce((sum, item) => sum + list(record(item).members).length, 0));
    elements.impactTitle.textContent = cycleLimited && populationLimited
      ? "Bounded acyclic downstream reach"
      : cycleLimited
        ? "Acyclic downstream reach"
        : populationLimited
          ? "Bounded downstream reach"
          : unresolvedLimited
            ? "Dependency-safe downstream reach"
          : "Downstream reach";
    const pathReceipts = [plural(critical.length, cycleLimited || populationLimited ? "safe step" : "step")];
    if (cycleLimited) pathReceipts.push(plural(cycleTaintedTotal, "cycle-withheld row"));
    if (populationLimited) pathReceipts.push(plural(populationOmitted, "outside row"));
    if (unresolvedTaintedTotal) pathReceipts.push(plural(unresolvedTaintedTotal, "unresolved-withheld row"));
    elements.pathLength.textContent = pathReceipts.join(" · ");
    const criticalRows = critical.length ? critical.map((id, index) =>
      `${index ? "<i aria-hidden=\"true\">→</i>" : ""}<button type="button" data-select-row="${escapeHtml(id)}">${escapeHtml(id)}</button>`).join("") :
      "<span class=\"meta\">No critical path is present.</span>";
    const topologyNotes = [];
    if (cycleLimited) {
      const identityReceipt = execution.cycle_tainted_truncated === true
        ? ` Server emitted ${formatCount(cycleTaintedEmitted)} of ${formatCount(cycleTaintedTotal)} withheld row identities.`
        : "";
      topologyNotes.push(`Cycle-tainted rows are withheld from critical-path, layer, and individual-reach metrics until every recorded cycle component resolves.${identityReceipt}`);
    }
    if (populationLimited) {
      topologyNotes.push(`Dependency analysis emitted ${formatCount(populationEmitted)} of ${formatCount(populationTotal)} work rows; ${formatCount(populationOmitted)} rows are outside this deterministic bounded population.`);
    }
    if (boundaryDependenciesTotal) {
      const boundaryReceipt = execution.analysis_boundary_dependencies_truncated === true
        ? ` The server emitted ${formatCount(boundaryDependenciesEmitted)} of ${formatCount(boundaryDependenciesTotal)} boundary edge identities.`
        : "";
      topologyNotes.push(`${formatCount(boundaryTaintedTotal)} included row memberships are boundary-tainted because they or their downstream rows depend on work outside the analysis boundary.${boundaryReceipt}`);
    }
    if (missingDependenciesTotal) {
      const missingReceipt = execution.missing_dependencies_truncated === true
        ? ` The server emitted ${formatCount(missingDependenciesEmitted)} of ${formatCount(missingDependenciesTotal)} missing prerequisite edge identities.`
        : "";
      topologyNotes.push(`${formatCount(missingTaintedTotal)} row memberships are missing-prerequisite-tainted because they or their downstream rows depend on prerequisites absent from the board.${missingReceipt}`);
    }
    if (unresolvedTaintedTotal) {
      const unresolvedReceipt = execution.unresolved_tainted_truncated === true
        ? ` The server emitted ${formatCount(counted(execution.unresolved_tainted_emitted, 0))} of ${formatCount(unresolvedTaintedTotal)} unique withheld row identities.`
        : "";
      topologyNotes.push(`${formatCount(unresolvedTaintedTotal)} unique rows are withheld across unresolved prerequisite reasons; reason memberships above may overlap.${unresolvedReceipt}`);
    }
    elements.criticalStrip.innerHTML = `${criticalRows}${topologyNotes.map(note =>
      `<span class="meta">${escapeHtml(note)}</span>`).join("")}`;
    const maximum = Math.max(1, ...impact.map(item => number(item.downstream) || 0));
    const impactRows = impact.map(item =>
      `<div class="impact-row"><button type="button" data-select-row="${escapeHtml(item.id)}">${escapeHtml(item.id)}</button>` +
      `<meter min="0" max="${maximum}" value="${number(item.downstream) || 0}"></meter><b>${escapeHtml(number(item.downstream) || 0)}</b></div>`);
    const cycleRows = cycleImpact.map(item => {
      const members = list(item.members).map(value => text(value)).filter(Boolean);
      const membersTotal = counted(item.members_total, members.length);
      const omittedMembers = Math.max(0, membersTotal - members.length);
      const downstream = number(item.downstream_after_component) || 0;
      return `<div class="cycle-impact-receipt"><b>Cycle component · ${escapeHtml(members.join(" + "))}${omittedMembers ? " + …" : ""}</b>` +
        `<span>${escapeHtml(plural(downstream, "downstream row"))} in recorded component reach. No member is ranked individually, and resolving the component does not prove an immediate unlock.${omittedMembers ? ` Showing ${members.length} of ${membersTotal} member identities.` : ""}</span></div>`;
    });
    const cycleBounds = execution.cycle_impact_truncated === true || execution.cycle_member_ids_truncated === true
      ? `<div class="cycle-impact-receipt"><b>Bounded cycle receipt</b><span>Showing ${formatCount(cycleComponentsEmitted)} of ${formatCount(cycleComponentsTotal)} cycle components and ${formatCount(cycleMemberIdsEmitted)} of ${formatCount(cycleMemberIdsTotal)} member identities. Omitted components and identities are not implied absent.</span></div>`
      : "";
    elements.impactLedger.innerHTML = [...impactRows, ...cycleRows, cycleBounds].join("") ||
      "<p class=\"meta\">No dependency impact rows are present.</p>";
  }

  function renderFleet() {
    const lanes = list(record(record(state.documents.operations).distribution).lanes);
    const total = lanes.reduce((sum, item) => sum + (number(item.count) || 0), 0);
    const maximum = Math.max(1, ...lanes.map(item => number(item.count) || 0));
    elements.fleetCount.textContent = plural(total, "row");
    elements.fleetLedger.innerHTML = lanes.length ? lanes.map(item =>
      `<div class="fleet-row"><span>${escapeHtml(text(item.key, "unspecified"))}</span>` +
      `<meter min="0" max="${maximum}" value="${number(item.count) || 0}"></meter><b>${escapeHtml(number(item.count) || 0)}</b></div>`).join("") :
      "<p class=\"meta\">No lane distribution is available.</p>";
  }

  function renderTraffic() {
    const pulse = record(state.documents.pulse);
    const valid = text(pulse.schema_version) === "PulseV1";
    elements.trafficPanel.dataset.state = valid ? "available" : "unavailable";
    if (!valid) {
      elements.trafficFreshness.textContent = "UNAVAILABLE";
      elements.trafficFacts.innerHTML = "<p class=\"meta\">PulseV1 is absent from this V1 compatibility read; no traffic is inferred.</p>";
      elements.trafficRoutes.replaceChildren();
      elements.trafficTruth.textContent = "Atlas withholds coordination traffic when it is not part of the coherent bundle.";
      return;
    }
    const counts = record(pulse.counts);
    const traffic = list(pulse.traffic).filter(route => text(record(route).from) && text(record(route).to) && (number(record(route).count) || 0) > 0);
    const undirected = list(pulse.traffic_undirected);
    const directedActs = traffic.reduce((sum, route) => sum + (number(record(route).count) || 0), 0);
    const undirectedActs = undirected.reduce((sum, route) => sum + (number(record(route).count) || 0), 0);
    const readStatus = record(state.documents.readStatus);
    const stale = readStatus.degraded === true;
    const generated = formatTime(pulse.generated_at);
    elements.trafficFreshness.textContent = `${stale ? "LAST GOOD" : "COHERENT"} · ${generated}`;
    elements.trafficFacts.innerHTML = [
      ["Recorded events", number(counts.events) || 0],
      ["Directed acts", directedActs],
      ["Attributed lanes", number(counts.lanes) || 0],
    ].map(([label, value]) =>
      `<div class="traffic-fact"><span>${escapeHtml(label)}</span><b>${escapeHtml(formatCount(value))}</b></div>`
    ).join("");
    const ranked = [...traffic]
      .sort((left, right) => (number(record(right).count) || 0) - (number(record(left).count) || 0) || text(record(left).kind).localeCompare(text(record(right).kind)))
      .slice(0, 4);
    elements.trafficRoutes.innerHTML = ranked.length ? ranked.map(route =>
      `<div class="traffic-route"><b>${escapeHtml(text(route.from))} → ${escapeHtml(text(route.to))} · ${escapeHtml(text(route.kind, "coordination act"))}</b>` +
      `<span>${escapeHtml(formatCount(number(route.count) || 0))}</span></div>`).join("") :
      "<p class=\"meta\">No directed lane traffic was recorded in this PulseV1 document.</p>";
    elements.trafficTruth.textContent = `Compact receipt only · ${formatCount(traffic.length)} typed routes. `
      + `${formatCount(undirectedActs)} coordination acts name no target lane and are not rehomed. Open Map for Pulse or lane topology.`;
  }

  function renderControls() {
    const lowInformation = Boolean(lowInformationReceipt());
    const neighbourhoodReady = state.mode === "neighbourhood" &&
      Boolean(state.selectedId && state.model.nodesById.has(state.selectedId));
    elements.search.value = state.search;
    elements.status.value = state.status;
    elements.module.value = state.module;
    elements.hops.value = String(state.hops);
    elements.hops.disabled = lowInformation || !neighbourhoodReady;
    elements.hopsHelp.textContent = state.mode !== "neighbourhood"
      ? "Available only in Neighbourhood."
      : neighbourhoodReady ? "Limits the selected node's recorded undirected neighbourhood." : "Select an admitted node to activate Hops.";
    elements.freeze.setAttribute("aria-pressed", String(state.frozen));
    document.body.classList.toggle("atlas-frozen", state.frozen);
    elements.freeze.textContent = state.frozen ? "Resume" : "Freeze";
    elements.refresh.disabled = state.loading || state.frozen;
    elements.refresh.setAttribute("aria-disabled", String(state.frozen));
    elements.refresh.title = state.frozen ? "Resume the live view before fetching a new snapshot." : "Fetch one new bundled snapshot now.";
    elements.questions.hidden = lowInformation;
    elements.topologyActions.hidden = lowInformation;
    elements.expand.hidden = lowInformation || state.mode !== "operating";
    elements.expand.disabled = lowInformation || state.mode !== "operating";
    elements.expand.setAttribute("aria-pressed", String(state.expandOperating));
    elements.expand.textContent = state.expandOperating ? "Focus ≤60" : "Expand admitted";
    elements.expand.title = state.expandOperating
      ? "Return to the deterministic high-signal Operating focus."
      : "Admit the complete client topology before active filters; whole-source omissions remain disclosed.";
    [elements.status, elements.module, elements.zoomOut, elements.zoomIn, elements.zoomFit]
      .forEach(control => { control.disabled = lowInformation; });
    document.querySelectorAll("[data-mode]").forEach(button => {
      const active = button.dataset.mode === state.mode;
      button.disabled = lowInformation;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    let activeQuestionButton = null;
    elements.questions.querySelectorAll("button").forEach(button => {
      const active = button.dataset.question === state.activeQuestion;
      button.disabled = lowInformation;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      if (active) activeQuestionButton = button;
    });
    keepQuestionVisible(activeQuestionButton);
    if (!lowInformation) elements.nodeRoster.hidden = true;
  }

  function keepQuestionVisible(button) {
    if (!button || elements.questions.hidden) return;
    const row = elements.questions;
    const style = getComputedStyle(row);
    const startInset = Number.parseFloat(style.scrollPaddingInlineStart) || 0;
    const endInset = Number.parseFloat(style.scrollPaddingInlineEnd) || 0;
    const rowBounds = row.getBoundingClientRect();
    const buttonBounds = button.getBoundingClientRect();
    if (buttonBounds.left < rowBounds.left + startInset) {
      row.scrollLeft += buttonBounds.left - rowBounds.left - startInset;
    } else if (buttonBounds.right > rowBounds.right - endInset) {
      row.scrollLeft += buttonBounds.right - rowBounds.right + endInset;
    }
  }

  function render() {
    buildModel();
    if (state.initialized && state.pendingHashSelection !== undefined) {
      const requested = state.pendingHashSelection;
      state.pendingHashSelection = undefined;
      prepareSelection(requested);
    } else if (state.selectedId) {
      state.selectionUnavailable = state.model.nodesById.has(state.selectedId) ? "" : "not_admitted";
    }
    renderAlert();
    renderClock();
    renderDocuments();
    renderMetrics();
    renderLowInformation();
    renderScope();
    renderGraph();
    renderInspector();
    renderActivity();
    renderHealth();
    renderImpact();
    renderFleet();
    renderTraffic();
    renderControls();
    focusSelectedNode();
  }

  function prepareSelection(nodeId) {
    const requested = text(nodeId);
    if (!requested) {
      state.selectedId = null;
      state.selectionUnavailable = "";
      state.focusSelectionAfterRender = false;
      state.activeQuestion = null;
      buildModel();
      return;
    }
    const resolved = rowNodeId(requested);
    const admitted = state.model && state.model.nodesById.has(resolved);
    state.selectedId = admitted ? resolved : requested;
    state.selectionUnavailable = admitted ? "" : "not_admitted";
    state.focusSelectionAfterRender = Boolean(admitted && !lowInformationReceipt());
    if (admitted && !state.model.visibleNodes.some(node => node.id === resolved) && !lowInformationReceipt()) {
      state.mode = "neighbourhood";
      state.hops = 1;
      state.search = "";
      state.status = "all";
      state.module = "all";
      state.activeQuestion = null;
      elements.search.value = "";
    }
    buildModel();
  }

  function focusSelectedNode() {
    if (!state.focusSelectionAfterRender || !state.selectedId) return;
    state.focusSelectionAfterRender = false;
    const selected = [...elements.svg.querySelectorAll("[data-node-id]")]
      .find(node => node.dataset.nodeId === state.selectedId);
    if (!selected) return;
    selected.focus({ preventScroll: true });
    selected.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  async function fetchDocument(name, url) {
    const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const value = await response.json();
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("non-document JSON");
    return { name, value };
  }

  async function fetchBundle() {
    for (const [endpoint, schema] of BUNDLE_ENDPOINTS) {
      const response = await fetch(endpoint, { headers: { Accept: "application/json" }, cache: "no-store" });
      if ([404, 405].includes(response.status)) continue;
      if (!response.ok) throw new Error(`${endpoint} returned HTTP ${response.status}`);
      const value = await response.json();
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("non-document JSON");
      if (value.schema_version !== schema) throw new Error(`unexpected bundle schema from ${endpoint}`);
      for (const name of ["snapshot", "graph", "context", "timeline", "operations", "read_status"]) {
        if (!value[name] || typeof value[name] !== "object" || Array.isArray(value[name])) {
          throw new Error(`bundle missing ${name}`);
        }
      }
      if (schema === "OpsAtlasBundleV2" &&
          (!value.pulse || value.pulse.schema_version !== "PulseV1")) {
        throw new Error("V2 operations bundle missing PulseV1");
      }
      return value;
    }
    return null;
  }

  function applyBundle(bundle) {
    state.documents.snapshot = bundle.snapshot;
    state.documents.graph = bundle.graph;
    state.documents.context = bundle.context;
    state.documents.timeline = bundle.timeline;
    state.documents.operations = bundle.operations;
    state.documents.readStatus = bundle.read_status;
    state.documents.pulse = text(record(bundle.pulse).schema_version) === "PulseV1" ? bundle.pulse : null;
    state.transport = "bundle";
    state.transportWarning = "";
    state.bundleSchema = text(bundle.schema_version);
    const bundleGeneration = number(bundle.cache_generation);
    const readGeneration = number(record(bundle.read_status).cache_generation);
    state.bundleReceiptMismatch = bundleGeneration === null || readGeneration === null ||
      bundleGeneration !== readGeneration;
  }

  async function fetchLegacyDocuments() {
    const entries = Object.entries(LEGACY_ENDPOINTS);
    const settled = await Promise.allSettled(entries.map(([name, url]) => fetchDocument(name, url)));
    return { entries, settled };
  }

  function applyLegacyDocuments(result) {
    state.transport = "legacy";
    state.transportWarning = "operations bundle endpoint is absent; six documents were fetched independently, so cross-document coherence is not proven";
    state.bundleSchema = "";
    state.documents.pulse = null;
    state.bundleReceiptMismatch = false;
    result.settled.forEach((documentResult, index) => {
      const name = result.entries[index][0];
      if (documentResult.status === "fulfilled") {
        state.documents[name] = documentResult.value.value;
      } else {
        state.failures.set(name, text(documentResult.reason && documentResult.reason.message, "request failed"));
        if (state.documents[name]) state.retained.add(name);
      }
    });
  }

  async function refresh() {
    if (state.loading || state.frozen) return;
    const refreshGeneration = ++state.refreshGeneration;
    const beforeOccurrences = new Set(occurrenceList(state.documents).map(occurrenceKey));
    const beforeRows = snapshotState(state.documents.snapshot);
    state.loading = true;
    if (!state.initialized) document.body.classList.add("atlas-loading");
    elements.refresh.disabled = true;
    elements.refresh.setAttribute("aria-busy", "true");
    state.failures.clear();
    state.retained.clear();
    try {
      const bundle = await fetchBundle();
      if (state.frozen || refreshGeneration !== state.refreshGeneration) return;
      if (bundle) {
        applyBundle(bundle);
      } else {
        const legacyDocuments = await fetchLegacyDocuments();
        if (state.frozen || refreshGeneration !== state.refreshGeneration) return;
        applyLegacyDocuments(legacyDocuments);
      }
    } catch (error) {
      if (state.frozen || refreshGeneration !== state.refreshGeneration) return;
      state.failures.set("operations bundle", text(error && error.message, "request failed"));
      const hasPriorDocuments = DOCUMENT_NAMES.some(name => Boolean(state.documents[name]));
      state.transportWarning = hasPriorDocuments
        ? "bundle refresh failed; the previous coherent document set is retained"
        : "bundle refresh failed; no coherent document set has loaded yet";
      DOCUMENT_NAMES.forEach(name => {
        if (state.documents[name]) state.retained.add(name);
      });
    }

    if (state.frozen || refreshGeneration !== state.refreshGeneration) return;
    if (state.initialized) {
      state.pendingReceipts = occurrenceList(state.documents)
        .filter(event => !beforeOccurrences.has(occurrenceKey(event))).slice(-12);
      const currentRows = snapshotState(state.documents.snapshot);
      const changes = [];
      currentRows.forEach((value, id) => {
        if (!beforeRows.has(id)) return;
        const fields = changedFields(beforeRows.get(id), value);
        if (!fields.length) return;
        changes.push([text(id).startsWith("job:") ? id : `work:${id}`, fields]);
      });
      changes.sort((left, right) => left[0].localeCompare(right[0]));
      state.changedNodes = new Map(changes.slice(0, 12));
      state.changeDescription = changes.length ?
        `Changed-field receipts: ${changes.slice(0, 6).map(([id, fields]) => `${id} [${fields.join(", ")}]`).join("; ")}${changes.length > 6 ? `; plus ${changes.length - 6} more` : ""}. Pulses identify changed fields and are not progress.` : "";
    } else {
      state.pendingReceipts = [];
      state.changedNodes.clear();
      state.changeDescription = "";
    }
    state.initialized = true;
    state.loading = false;
    document.body.classList.remove("atlas-loading");
    elements.refresh.removeAttribute("aria-busy");
    render();
  }

  function selectNode(nodeId) {
    prepareSelection(nodeId);
    writeSelectionHash(state.selectedId);
    render();
  }

  function handleQuestion(question) {
    const target = AtlasModel.questionTarget(state.model, question);
    state.activeQuestion = question;
    state.walkRequest = question;
    if (target) {
      state.selectedId = target;
      state.selectionUnavailable = "";
      state.focusSelectionAfterRender = true;
      state.mode = "neighbourhood";
      state.hops = 1;
      state.status = "all";
      writeSelectionHash(target);
    }
    render();
  }

  function changeZoom(direction) {
    state.fit = false;
    const current = Math.max(0, ZOOMS.indexOf(state.zoom));
    state.zoom = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, current + direction))];
    renderGraph();
  }

  function updatePreludeAction() {
    elements.preludeAction.textContent = elements.prelude.open ? "Hide context" : "Show context";
  }

  function syncResponsivePrelude() {
    const compact = document.documentElement.dataset.embedded === "1" ||
      root.matchMedia("(max-width: 720px), (max-height: 800px) and (min-width: 721px)").matches;
    if (compact !== state.preludeCompact) {
      state.preludeCompact = compact;
      elements.prelude.open = !compact;
    }
    if (compact && elements.questions.parentElement !== elements.questionDock) {
      elements.questionDock.appendChild(elements.questions);
    } else if (!compact && elements.questions.parentElement !== elements.questionHome) {
      elements.questionHome.appendChild(elements.questions);
    }
    elements.questionDock.hidden = !compact;
    updatePreludeAction();
  }

  function bindEvents() {
    const zero = document.createElement("option");
    zero.value = "0";
    zero.textContent = "0 hops";
    elements.hops.insertBefore(zero, elements.hops.firstChild);
    elements.prelude.addEventListener("toggle", updatePreludeAction);
    root.addEventListener("resize", () => {
      root.clearTimeout(state.preludeResizeTimer);
      state.preludeResizeTimer = root.setTimeout(syncResponsivePrelude, 80);
    });

    elements.freeze.addEventListener("click", () => {
      state.frozen = !state.frozen;
      if (state.frozen) {
        state.refreshGeneration += 1;
        state.loading = false;
        elements.refresh.removeAttribute("aria-busy");
        clearMotion();
      }
      renderClock();
      renderControls();
      if (!state.frozen) refresh();
    });
    elements.refresh.addEventListener("click", refresh);
    elements.expand.addEventListener("click", () => {
      if (state.mode !== "operating") return;
      state.expandOperating = !state.expandOperating;
      render();
    });
    elements.search.addEventListener("input", event => { state.search = event.target.value; render(); });
    elements.status.addEventListener("change", event => { state.status = event.target.value; render(); });
    elements.module.addEventListener("change", event => { state.module = event.target.value; render(); });
    elements.hops.addEventListener("change", event => {
      state.hops = event.target.value === "all" ? "all" : Number(event.target.value);
      render();
    });
    document.querySelector(".topology-actions").addEventListener("click", event => {
      const button = event.target.closest("[data-mode]");
      if (!button || button.disabled) return;
      state.mode = button.dataset.mode;
      state.walkRequest = state.mode === "critical" ? "critical" : null;
      if (state.mode === "critical") state.status = "all";
      state.activeQuestion = null;
      render();
    });
    elements.questions.addEventListener("click", event => {
      const button = event.target.closest("[data-question]");
      if (button && !button.disabled) handleQuestion(button.dataset.question);
    });
    elements.zoomOut.addEventListener("click", () => changeZoom(-1));
    elements.zoomIn.addEventListener("click", () => changeZoom(1));
    elements.zoomFit.addEventListener("click", () => { state.fit = true; renderGraph(); });

    document.addEventListener("click", event => {
      const node = event.target.closest("[data-node-id]");
      const related = event.target.closest("[data-select-node]");
      const row = event.target.closest("[data-select-row]");
      if (node) selectNode(node.dataset.nodeId);
      else if (related) selectNode(related.dataset.selectNode);
      else if (row) selectNode(row.dataset.selectRow);
    });
    elements.svg.addEventListener("keydown", event => {
      const node = event.target.closest("[data-node-id]");
      if (!node) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectNode(node.dataset.nodeId);
        return;
      }
      if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(event.key)) return;
      const nodes = [...elements.svg.querySelectorAll("[data-node-id]")];
      const index = nodes.indexOf(node);
      const direction = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1;
      const next = nodes[(index + direction + nodes.length) % nodes.length];
      if (next) {
        event.preventDefault();
        next.focus();
      }
    });
    document.addEventListener("keydown", event => {
      const targetName = text(event.target && event.target.tagName).toLowerCase();
      const editable = targetName === "input" || targetName === "select" || targetName === "textarea" || event.target.isContentEditable;
      if (event.key === "/" && !editable && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        elements.search.focus();
      }
      if (event.key === "Escape") {
        if (state.search) {
          state.search = "";
          elements.search.value = "";
        } else {
          state.selectedId = null;
          state.selectionUnavailable = "";
          state.focusSelectionAfterRender = false;
          state.activeQuestion = null;
          writeSelectionHash(null);
        }
        render();
      }
    });
    root.addEventListener("hashchange", () => {
      state.pendingHashSelection = selectionFromHash();
      if (state.initialized) render();
    });
  }

  state.pendingHashSelection = selectionFromHash();
  bindEvents();
  syncResponsivePrelude();
  render();
  refresh();
  state.timer = root.setInterval(() => { if (!state.frozen) refresh(); }, AUTO_REFRESH_MS);
}(window, document));
