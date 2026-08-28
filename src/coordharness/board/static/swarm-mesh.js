(() => {
  "use strict";

  const byId = id => document.getElementById(id);
  const list = value => Array.isArray(value) ? value : [];
  const record = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const text = (value, fallback) => {
    const result = value === null || value === undefined ? "" : String(value).trim();
    return result || (fallback || "");
  };
  const compare = (left, right) => String(left).localeCompare(String(right));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const escapeHTML = value => String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
  const formatCount = value => Number(value || 0).toLocaleString();
  const shortTime = value => {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  const statusGroup = status => {
    const value = text(status).toLowerCase();
    if (["blocked", "attention", "failed", "stuck", "missing"].includes(value)) return "attention";
    if (["running", "active"].includes(value)) return "running";
    if (["done", "complete", "completed", "closed", "archived"].includes(value)) return "done";
    return "recorded";
  };

  const viewport = byId("mesh-viewport");
  const canvas = byId("mesh-canvas");
  const context = canvas.getContext("2d", { alpha: true });
  const flowCanvas = byId("flow-chart");
  const flowContext = flowCanvas.getContext("2d", { alpha: true });
  canvas.setAttribute("aria-hidden", "true");

  const BUNDLE_ENDPOINTS = Object.freeze([
    ["/api/v2/operations-bundle", "OpsAtlasBundleV2"],
    ["/api/v1/operations-bundle", "OpsAtlasBundleV1"],
  ]);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const keyboardNavigationHelp = "Arrow keys move node focus · Enter pins the focused node · Escape clears the pinned node";
  const meshAuthorityCore = "Projection only · layout-only; direction is not activity.";
  const state = {
    bundle: null,
    bundleSchema: "",
    pulse: null,
    pendingBundle: null,
    model: null,
    scene: null,
    layout: "context",
    projection: "perspective",
    motion: "direction",
    population: "focus",
    focusNodeIds: new Set(),
    visibleClusterLabelCount: 0,
    boardPulseReason: "",
    selectedId: "",
    focusedId: "",
    hoverId: "",
    clusterFilter: "",
    query: "",
    question: "",
    pathNodeIds: new Set(),
    pathEdgeIds: new Set(),
    knownOccurrenceKeys: null,
    activeTracks: [],
    replayIndex: 0,
    replayPulse: null,
    lastReplayAt: 0,
    frozen: false,
    camera: { yaw: -.2, pitch: .28, zoom: 1, panX: 0, panY: 0 },
    screenNodes: [],
    clusterLabelEpoch: 0,
    clusterLabelMetricKey: "",
    clusterLabelLayoutKey: "",
    clusterLabelLayout: null,
    clusterLabelOverlayBoxes: [],
    frame: { count: 0, sampledAt: performance.now(), fps: 0 },
    drag: null,
    pointer: { x: 0, y: 0 },
    drawTime: 0,
    dirty: false,
    rafId: 0,
    wasAnimating: false,
    accessibleSceneSignature: "",
  };

  function setMeshCaption(caveats) {
    const root = byId("mesh-caption");
    if (!root) return;
    const currentDisclosure = root.querySelector("details");
    const wasOpen = Boolean(currentDisclosure && currentDisclosure.open);
    const core = document.createElement("span");
    core.className = "mesh-caption-core";
    core.textContent = meshAuthorityCore;
    const disclosure = document.createElement("details");
    disclosure.className = "mesh-caption-disclosure";
    disclosure.open = wasOpen;
    const summary = document.createElement("summary");
    summary.textContent = "Controls & caveats";
    const detail = document.createElement("span");
    detail.className = "mesh-caption-detail";
    detail.textContent = caveats;
    disclosure.append(summary, detail);
    root.replaceChildren(core, disclosure);
  }

  function setMeshTruthCore() {
    const paragraph = document.querySelector(".mesh-truth p");
    if (!paragraph) return;
    const lead = document.createElement("b");
    lead.textContent = "Projection only.";
    paragraph.replaceChildren(
      lead,
      document.createTextNode(" Layout-only; direction is not activity. This surface performs no lifecycle writes."),
    );
  }

  function isLowInformation() {
    return Boolean(state.scene && state.scene.lowInformation);
  }

  function renderingPaused() {
    return state.frozen || document.hidden;
  }

  function animationActive(now) {
    if (!state.scene || renderingPaused() || reducedMotion.matches) return false;
    if (state.motion === "traffic") return communicationRoutes().length > 0;
    if (isLowInformation()) return false;
    if (state.motion === "direction") return state.scene.edges.some(edge => edge.admitted && edgeVisible(edge));
    if (state.motion === "replay") return state.scene.occurrences.length > 0;
    if (state.motion === "live") {
      return state.activeTracks.some(track => now - track.startedAt < track.duration);
    }
    return false;
  }

  function scheduleFrame() {
    if (state.rafId || renderingPaused()) return;
    state.rafId = requestAnimationFrame(drawFrame);
  }

  function markDirty() {
    state.dirty = true;
    scheduleFrame();
  }

  function announce(message) {
    const root = byId("mesh-status-announcer");
    if (!root) return;
    root.textContent = "";
    window.setTimeout(() => { root.textContent = message; }, 0);
  }

  function palette() {
    const style = getComputedStyle(document.documentElement);
    const token = name => style.getPropertyValue(name).trim();
    return {
      ink: token("--ink"),
      muted: token("--muted"),
      line: token("--line"),
      panel: token("--panel"),
      green: token("--green"),
      blue: token("--blue"),
      amber: token("--amber"),
      red: token("--red"),
      background: token("--c-bg"),
    };
  }

  function withAlpha(alpha, draw) {
    context.save();
    context.globalAlpha = alpha;
    draw();
    context.restore();
  }

  function canvasSize(target, ctx) {
    const rect = target.getBoundingClientRect();
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const width = Math.max(1, Math.floor(rect.width));
    const height = Math.max(1, Math.floor(rect.height));
    const pixelWidth = Math.floor(width * ratio);
    const pixelHeight = Math.floor(height * ratio);
    if (target.width !== pixelWidth || target.height !== pixelHeight) {
      target.width = pixelWidth;
      target.height = pixelHeight;
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width, height, ratio };
  }

  function setAlert(message, tone) {
    const alert = byId("mesh-alert");
    alert.hidden = !message;
    alert.textContent = message || "";
    alert.dataset.tone = tone || "";
    if (message) {
      // The bar above this is a different height standalone than embedded in a
      // native cockpit, so a fixed offset put the banner behind the chrome in
      // one of the two. CSSOM rather than a style attribute: the page runs
      // under a CSP with no unsafe-inline.
      const bar = document.querySelector(".shellbar");
      alert.style.top = `${bar ? Math.round(bar.getBoundingClientRect().bottom) : 0}px`;
    }
  }

  // A search that finds nothing has two very different meanings here: no such
  // node exists, or the node exists and this bounded view does not carry it.
  // The view holds a few hundred of many thousands, so silence was almost
  // always the second one -- and indistinguishable from the first.
  function reportSearchReach() {
    if (!state.scene) return;
    const query = state.query.trim();
    if (!query) {
      setAlert("");
      return;
    }
    if (state.scene.nodes.some(node => queryMatches(node))) {
      setAlert("");
      return;
    }
    const receipt = state.scene.sourceReceipt || {};
    const omitted = Number(receipt.omittedNodes) || 0;
    const emitted = Number(receipt.emittedNodes) || state.scene.nodes.length;
    setAlert(
      omitted
        ? `No match among the ${formatCount(emitted)} nodes this view carries. `
          + `${formatCount(omitted)} more were omitted by the envelope, so this is `
          + `not evidence that nothing matches.`
        : `No match. This view carries the whole population, so nothing matches.`,
      omitted ? "caution" : "",
    );
  }


  async function readBundle() {
    for (const [endpoint, schema] of BUNDLE_ENDPOINTS) {
      const response = await fetch(endpoint, { headers: { Accept: "application/json" }, cache: "no-store" });
      if ([404, 405].includes(response.status)) continue;
      if (!response.ok) throw new Error(`${endpoint} returned ${response.status}`);
      const bundle = await response.json();
      if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)) throw new Error("operations bundle returned non-document JSON");
      if (bundle.schema_version !== schema) throw new Error(`unexpected operations bundle version from ${endpoint}`);
      for (const name of ["snapshot", "graph", "context", "timeline", "operations", "read_status"]) {
        if (!bundle[name] || typeof bundle[name] !== "object" || Array.isArray(bundle[name])) {
          throw new Error(`operations bundle missing ${name}`);
        }
      }
      if (schema === "OpsAtlasBundleV2" &&
          (!bundle.pulse || bundle.pulse.schema_version !== "PulseV1")) {
        throw new Error("V2 operations bundle missing PulseV1");
      }
      return bundle;
    }
    throw new Error("operations bundle endpoints are unavailable");
  }


  function laneName(value) {
    return text(value).split(":", 1)[0].trim().toLowerCase();
  }

  function pulseTraffic() {
    const pulse = record(state.pulse);
    if (text(pulse.schema_version) !== "PulseV1") return [];
    return list(pulse.traffic)
      .map((value, index) => ({ ...record(value), index, count: Math.max(0, Number(record(value).count) || 0) }))
      .filter(route => text(route.from) && text(route.to) && route.count > 0);
  }

  function agentLane(node) {
    return laneName(text(record(node).actor, record(node).owner));
  }

  function communicationRoutes() {
    if (!state.scene) return [];
    const agents = state.scene.nodes
      .filter(node => node.kind === "agent" && nodeVisible(node))
      .sort((left, right) => Number(statusGroup(left.status) !== "running") - Number(statusGroup(right.status) !== "running") || compare(left.id, right.id));
    const byLane = new Map();
    agents.forEach(node => {
      const lane = agentLane(node);
      if (!lane || byLane.has(lane)) return;
      byLane.set(lane, node);
    });
    return pulseTraffic().map(route => {
      const source = byLane.get(laneName(route.from));
      const target = byLane.get(laneName(route.to));
      if (!source || !target) return null;
      return { ...route, id: `traffic:${route.index}:${route.kind}:${route.from}:${route.to}`, source, target };
    }).filter(Boolean);
  }

  function renderCommunicationSummary() {
    const root = byId("mesh-comms-summary");
    if (!root) return;
    const routes = communicationRoutes();
    const rawRoutes = pulseTraffic();
    const acts = rawRoutes.reduce((sum, route) => sum + route.count, 0);
    const represented = routes.reduce((sum, route) => sum + route.count, 0);
    const lanes = new Set(rawRoutes.flatMap(route => [laneName(route.from), laneName(route.to)])).size;
    root.dataset.state = state.pulse ? (routes.length ? "represented" : "unrepresented") : "unavailable";
    if (!state.pulse || text(state.pulse.schema_version) !== "PulseV1") {
      byId("mesh-comms-counts").textContent = "PulseV1 unavailable on this V1 compatibility bundle; traffic is withheld.";
      byId("mesh-comms-truth").textContent = "No communication route or motion is inferred from topology or timeline data.";
      return;
    }
    const generated = shortTime(state.pulse.generated_at);
    byId("mesh-comms-counts").textContent = `${formatCount(acts)} recorded acts · ${formatCount(rawRoutes.length)} typed routes · ${formatCount(lanes)} lanes · ${generated}`;
    byId("mesh-comms-truth").textContent = routes.length
      ? `Moving artifacts show recorded direction, not current activity. ${formatCount(represented)} acts map to visible agent nodes.`
      : "Recorded traffic is present, but its lane endpoints are absent from the visible agent nodes; no motion is drawn.";
  }

  async function refresh() {
    try {
      const bundle = await readBundle();
      if (state.frozen) state.pendingBundle = bundle;
      else applyBundle(bundle);
      // Clearing unconditionally wiped the search-reach disclosure on the next
      // poll, roughly a second after it appeared. Re-derive it instead: it is
      // about the standing query, not about this read.
      setAlert("");
      reportSearchReach();
    } catch (error) {
      const retained = Boolean(state.bundle && state.scene);
      document.body.classList.remove("mesh-loading");
      document.body.classList.toggle("mesh-no-generation", !retained);
      setAlert(
        retained
          ? `The board read failed. The last coherent generation remains visible. ${error.message}`
          : `The board read failed and no coherent generation has loaded yet. ${error.message}`,
        "failure",
      );
      setFreshness(
        "DEGRADED READ",
        retained ? "last coherent generation retained" : "no coherent generation loaded",
        "failure",
      );
    }
  }

  // A selection made on the Board arrives in the hash. Honour it once the
  // scene exists: the node is selected and focused exactly as a click would,
  // and if the envelope omitted it, the search-reach banner already knows how
  // to say so rather than staying silent.
  let carriedSelection=(()=>{
    const raw=new URLSearchParams((window.location.hash||"").replace(/^#/,""));
    return raw.get("sel")||"";
  })();
  function applyCarriedSelection(){
    if(!carriedSelection||!state.scene)return;
    const target=state.scene.nodesById.get(`work:${carriedSelection}`)
      ||state.scene.nodesById.get(carriedSelection);
    carriedSelection="";
    if(!target)return;
    selectNode(target.id,"carried from board");
    state.focusedId=target.id;
  }

  function buildScene() {
    if (!state.model) return;
    const selected = state.selectedId;
    state.scene = window.CoordSwarmMeshModel.buildScene(state.model, { layout: state.layout });
    state.focusNodeIds = window.CoordSwarmMeshModel.focusNodeIds(state.scene, 60);
    if (state.scene.lowInformation && state.layout === "critical") {
      state.layout = "context";
      state.scene = window.CoordSwarmMeshModel.buildScene(state.model, { layout: state.layout });
      document.querySelectorAll("[data-layout]").forEach(button => {
        const active = button.dataset.layout === state.layout;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
    }
    if (selected && !state.scene.nodesById.has(selected)) state.selectedId = "";
    if (!state.focusedId || !state.scene.nodesById.has(state.focusedId)) {
      state.focusedId = text([...state.focusNodeIds][0], text(state.scene.nodes[0] && state.scene.nodes[0].id));
    }
    state.pathNodeIds = new Set(state.selectedId ? [state.selectedId] : []);
    state.pathEdgeIds = new Set();
    if (!state.selectedId) seedBoardPulse();
    if (isLowInformation()) {
      state.activeTracks = [];
      state.replayPulse = null;
      state.question = "";
    }
    syncTopologyControls();
    buildClusterLabels();
    renderStaticPanels();
    renderCommunicationSummary();
    renderInspector();
    renderAccessibleNavigator();
    markDirty();
  }

  function applyBundle(bundle) {
    const documents = {
      snapshot: bundle.snapshot,
      graph: bundle.graph,
      context: bundle.context,
      timeline: bundle.timeline,
      operations: bundle.operations,
    };
    const nextModel = window.CoordOpsAtlasModel.build(documents, { status: "all" });
    const nextScene = window.CoordSwarmMeshModel.buildScene(nextModel, { layout: state.layout });
    const nextKeys = new Set(nextScene.occurrences.map(occurrence => occurrence.key));
    let arrivalCount = 0;
    if (state.knownOccurrenceKeys) {
      const arrivals = nextScene.occurrences
        .filter(occurrence => !state.knownOccurrenceKeys.has(occurrence.key));
      arrivalCount = arrivals.length;
      arrivals.filter(occurrence => !nextScene.lowInformation && occurrence.motionState === "exact_hold").slice(-12).forEach(occurrence => {
        state.activeTracks.push({ edgeId: occurrence.edgeId, startedAt: performance.now(), duration: 1350, key: occurrence.key });
      });
    }
    state.knownOccurrenceKeys = nextKeys;
    state.bundle = bundle;
    state.bundleSchema = text(bundle.schema_version);
    state.pulse = text(record(bundle.pulse).schema_version) === "PulseV1" ? bundle.pulse : null;
    state.model = nextModel;
    document.body.classList.remove("mesh-loading", "mesh-no-generation");
    buildScene();
    if (arrivalCount && state.motion === "live" && !isLowInformation()) {
      byId("motion-truth").textContent = `LIVE ARRIVALS · ${arrivalCount} NEW`;
    }
    renderBundleReceipts();
  }

  function setFreshness(label, detail, tone) {
    byId("freshness-state").textContent = label;
    byId("freshness-time").textContent = detail;
    byId("freshness-dot").className = `state-dot${tone ? ` ${tone}` : ""}`;
  }

  function renderBundleReceipts() {
    if (!state.bundle || !state.scene) return;
    const bundle = state.bundle;
    const operations = record(bundle.operations);
    const metrics = record(operations.metrics);
    const readStatus = record(bundle.read_status);
    const sessions = list(record(bundle.snapshot).sessions);
    byId("rail-generation").textContent = formatCount(bundle.cache_generation);
    byId("rail-nodes").textContent = formatCount(state.scene.sourceReceipt.emittedNodes);
    byId("rail-edges").textContent = formatCount(state.scene.sourceReceipt.emittedEdges);
    byId("rail-events").textContent = formatCount(metrics.recorded_events || state.scene.occurrences.length);
    byId("rail-sessions").textContent = `${sessions.filter(session => session.live === true).length}/${sessions.length}`;
    const generated = text(bundle.generated_at, text(operations.generated_at));
    const stale = Boolean(operations.stale || readStatus.degraded);
    setFreshness(
      stale ? "LAST GOOD GENERATION" : "COHERENT GENERATION",
      `${shortTime(generated)} · ${stale ? "degraded refresh" : "read-only projection"}`,
      stale ? "attention" : "",
    );

    document.querySelectorAll("#generation-stages li").forEach(stage => stage.classList.add("ready"));
    byId("generation-receipt").textContent = `generation ${bundle.cache_generation} · ${state.scene.sourceReceipt.complete ? "complete envelope" : "qualified envelope"} · ${shortTime(generated)}`;
  }

  function syncTopologyControls() {
    const lowInformation = isLowInformation();
    if (lowInformation && state.projection === "perspective") {
      state.projection = "flat";
      state.camera = { yaw: 0, pitch: 0, zoom: 1, panX: 0, panY: 0 };
    }
    document.body.classList.toggle("mesh-low-information", lowInformation);
    document.querySelectorAll("[data-projection]").forEach(button => {
      const perspectiveUnavailable = lowInformation && button.dataset.projection === "perspective";
      button.disabled = perspectiveUnavailable;
      const active = button.dataset.projection === state.projection;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      if (perspectiveUnavailable) button.setAttribute("aria-describedby", "mesh-caption");
      else button.removeAttribute("aria-describedby");
    });
    document.querySelectorAll("[data-motion]").forEach(button => {
      const unavailable = button.dataset.motion === "traffic"
        ? communicationRoutes().length === 0
        : lowInformation;
      button.disabled = unavailable;
      const active = button.dataset.motion === state.motion;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    document.querySelectorAll("[data-layout]").forEach(button => {
      const relationshipUnavailable = lowInformation && button.dataset.layout === "critical";
      button.disabled = relationshipUnavailable;
      if (relationshipUnavailable) button.setAttribute("aria-describedby", "mesh-caption");
      else button.removeAttribute("aria-describedby");
    });
    document.querySelectorAll("[data-question]").forEach(button => {
      button.disabled = lowInformation;
      if (lowInformation) button.classList.remove("active");
    });
    const badge = byId("motion-truth");
    const projectionBadge = byId("projection-truth");
    if (lowInformation) {
      projectionBadge.className = "truth-badge unavailable";
      projectionBadge.textContent = "PERSPECTIVE UNAVAILABLE · 0 ADMITTED EDGES";
      if (state.motion === "traffic" && communicationRoutes().length) {
        badge.className = "truth-badge traffic";
        badge.textContent = "RECORDED TRAFFIC · NOT CURRENT ACTIVITY";
        setMeshCaption(`Topology motion remains unavailable because no authoritative graph edges were admitted. `
          + `PulseV1 traffic draws only recorded lane direction between visible agent nodes; it is not current activity. `
          + `Drag to pan · Wheel or +/− to zoom · 0 resets the camera · ${keyboardNavigationHelp}.`);
        return;
      }
      badge.className = "truth-badge topology-unavailable";
      badge.textContent = "TOPOLOGY MOTION UNAVAILABLE";
      setMeshCaption(
        `Perspective and Critical Flow are unavailable because no authoritative edges were admitted. `
        + `Nodes remain visible in 2D, searchable, focusable, and inspectable; no links, particles, paths, or motion are synthesized. `
        + `Drag to pan · Wheel or +/− to zoom · 0 resets the camera · ${keyboardNavigationHelp}.`,
      );
      return;
    }
    projectionBadge.className = `truth-badge ${state.projection}`;
    projectionBadge.textContent = state.projection === "perspective"
      ? "PERSPECTIVE · LAYOUT-ONLY Z"
      : "2D · Z FLATTENED";
    badge.className = `truth-badge ${state.motion}`;
    if (state.motion === "live") badge.textContent = "LIVE ARRIVALS · FIRST READ SILENT";
    if (state.motion === "replay") badge.textContent = "RECORDED REPLAY · STATIONARY HISTORY";
    if (state.motion === "direction") badge.textContent = "SCHEMATIC DIRECTION · NOT ACTIVITY";
    if (state.motion === "traffic") badge.textContent = "RECORDED TRAFFIC · NOT CURRENT ACTIVITY";
    setMeshCaption(state.projection === "perspective"
      ? `Perspective projects deterministic layout-only z separation; z is not measured time, priority, certainty, or activity. `
        + `Particles travel only along admitted edges. Dashed label leaders are layout aids, not relationships. `
        + `Drag to orbit · Shift-drag to pan · Wheel or +/− to zoom · 0 resets the camera · Click a node to pin · ${keyboardNavigationHelp} · Shift+Arrow keys orbit.`
      : `2D flattens the deterministic layout-only z separation. Nodes and admitted edges retain the same identities. `
        + `Dashed label leaders are layout aids, not relationships. Drag to pan · Wheel or +/− to zoom · 0 resets the camera · `
        + `Click a node to pin · ${keyboardNavigationHelp}.`);
  }

  function renderStaticNodeRoster() {
    const root = byId("static-node-roster");
    const nodes = [...state.scene.nodes].sort((left, right) => compare(left.label, right.label) || compare(left.id, right.id));
    const matches = nodes.filter(queryMatches).length;
    root.innerHTML = nodes.map(node => {
      const match = queryMatches(node);
      const classes = [
        match && state.query ? "search-match" : "",
        !match && state.query ? "search-dimmed" : "",
        node.id === state.selectedId ? "selected" : "",
      ].filter(Boolean).join(" ");
      return `<li><button type="button" class="${classes}" data-static-node="${escapeHTML(node.id)}"><b>${escapeHTML(node.label)}</b><span>${escapeHTML(node.id)} · ${escapeHTML(node.status || "unknown")}</span></button></li>`;
    }).join("");
    byId("static-node-count").textContent = state.query
      ? `${formatCount(matches)} of ${formatCount(nodes.length)} match`
      : `${formatCount(nodes.length)} nodes`;
  }

  function renderTopologyAdmission() {
    const root = byId("mesh-topology-receipt");
    const active = isLowInformation();
    root.hidden = !active;
    if (!active) return false;

    const receipt = record(state.scene.topologyAvailability);
    const population = record(receipt.population);
    const admitted = record(receipt.admitted);
    const omitted = record(receipt.omitted);
    const source = record(receipt.source);
    const freshness = record(receipt.freshness);
    byId("mesh-topology-reason").textContent = text(
      receipt.reason,
      "No authoritative relationships were admitted; connectivity remains unknown.",
    );
    byId("mesh-topology-prerequisite").textContent =
      `Missing prerequisite: ${text(receipt.missing_prerequisite, "authoritative_graph_relationships")}. Perspective and Critical Flow are unavailable. Nodes remain inspectable in 2D; Mesh will not synthesize edges, paths, particles, or motion.`;
    const fallbackReceipt = state.scene.sourceReceipt || {};
    const populationNodes = Number.isFinite(population.nodes) ? population.nodes : state.scene.nodes.length;
    const populationEdges = Number.isFinite(population.edges) ? population.edges : fallbackReceipt.populationEdges || 0;
    const admittedNodes = Number.isFinite(admitted.nodes) ? admitted.nodes : state.scene.nodes.length;
    const admittedEdges = Number.isFinite(admitted.edges) ? admitted.edges : state.scene.edges.length;
    const facts = [
      ["Population", `${formatCount(population.work_items)} work · ${formatCount(populationNodes)} nodes · ${formatCount(populationEdges)} edges · ${formatCount(population.events)} events`],
      ["Admission", `${formatCount(admittedNodes)} nodes · ${formatCount(admittedEdges)} edges admitted; ${formatCount(omitted.nodes)} nodes · ${formatCount(omitted.edges)} edges omitted`],
      ["Source", `${text(source.graph_declared, "unknown")} · ${text(source.graph_schema_version, "unknown schema")} · ${text(source.graph_content_sha256, "no fingerprint").slice(0, 18)}`],
      ["Freshness", `${text(freshness.state, "unknown")} · ${text(freshness.generated_at) ? shortTime(freshness.generated_at) : "generation unavailable"} · ${formatCount(freshness.document_skew_seconds)}s skew`],
    ];
    byId("mesh-topology-facts").innerHTML = facts.map(([label, value]) =>
      `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`
    ).join("");
    renderStaticNodeRoster();
    return true;
  }

  function queryMatches(node) {
    const query = state.query.trim().toLowerCase();
    if (!query) return true;
    return [node.id, node.label, node.owner, node.module, node.currentStep, node.status, ...list(node.modules)]
      .join(" ").toLowerCase().includes(query);
  }

  function nodeVisible(node) {
    const inPopulation = state.population === "admitted"
      || state.focusNodeIds.has(node.id)
      || state.pathNodeIds.has(node.id)
      || [state.selectedId, state.focusedId, state.hoverId].includes(node.id)
      || (state.query && queryMatches(node));
    return Boolean(inPopulation && (!state.clusterFilter || node.clusterId === state.clusterFilter));
  }

  function visibleClusterRows() {
    if (!state.scene) return [];
    return state.scene.clusters.map(cluster => ({
      cluster,
      visibleCount: state.scene.nodes.filter(node => node.clusterId === cluster.id && nodeVisible(node)).length,
    })).filter(row => row.visibleCount > 0).sort((left, right) =>
      right.visibleCount - left.visibleCount
      || (right.cluster.memberCount || 0) - (left.cluster.memberCount || 0)
      || compare(left.cluster.id, right.cluster.id));
  }

  function edgeVisible(edge) {
    const source = state.scene && state.scene.nodesById.get(edge.source);
    const target = state.scene && state.scene.nodesById.get(edge.target);
    return Boolean(source && target && nodeVisible(source) && nodeVisible(target));
  }

  function buildClusterLabels() {
    const root = byId("cluster-labels");
    root.replaceChildren();
    if (!state.scene) return;
    const rows = visibleClusterRows();
    const labelRows = state.population === "focus" ? rows.slice(0, 8) : rows;
    labelRows.forEach(({ cluster, visibleCount }) => {
      const label = document.createElement("span");
      label.className = "cluster-label";
      label.dataset.clusterId = cluster.id;
      label.textContent = `${cluster.label} · ${visibleCount}`;
      label.title = `${formatCount(visibleCount)} shown · ${formatCount(cluster.memberCount)} admitted`;
      root.appendChild(label);
    });
    state.clusterLabelEpoch += 1;
    state.clusterLabelMetricKey = "";
    state.clusterLabelLayoutKey = "";
    state.clusterLabelLayout = null;
    // Fresh spans carry no position, and the draw loop is what positions them.
    // A hidden tab or a reduced-motion session may not run another frame for a
    // long time, so compute and apply one cached layout immediately.
    if (canvas) {
      const layout = getClusterLabelLayout(canvasSize(canvas, context));
      applyClusterLabelLayout(layout);
      // That first measurement runs on spans appended microseconds ago, so it can
      // read fallback-font metrics. The cache key is epoch plus canvas size and
      // carries no notion of font readiness, so those too-narrow widths would
      // otherwise stick -- and placement rejects a candidate only when the boxes it
      // was given overlap. Narrow boxes fit where the rendered text does not, which
      // is how two labels can be placed 5px apart and still collide on screen.
      // Re-measure once the fonts settle; if the widths were already right this
      // recomputes to the same layout and costs one frame.
      if (document.fonts && document.fonts.ready && typeof document.fonts.ready.then === "function") {
        document.fonts.ready.then(() => {
          if (!byId("cluster-labels")) return;
          state.clusterLabelMetricKey = "";
          state.clusterLabelLayoutKey = "";
          state.clusterLabelLayout = null;
          applyClusterLabelLayout(getClusterLabelLayout(canvasSize(canvas, context)));
        }).catch(() => {});
      }
    }
  }

  function renderStaticPanels() {
    if (!state.scene || !state.model) return;
    renderClusterRoster();
    renderScope();
    applyCarriedSelection();
    const lowInformation = renderTopologyAdmission();
    if (lowInformation) byId("mesh-evidence").open = true;
    document.querySelectorAll(".telemetry-panel").forEach(panel => { panel.hidden = lowInformation; });
    if (!lowInformation) {
      renderEvents();
      renderEdgeLedger();
      renderTraversalGrid();
      renderIntegrity();
      renderFlow();
    }
  }

  function renderClusterRoster() {
    const root = byId("cluster-roster");
    root.replaceChildren();
    const rows = visibleClusterRows();
    rows.forEach(({ cluster, visibleCount }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `cluster-row${state.clusterFilter === cluster.id ? " active" : ""}`;
      button.dataset.clusterId = cluster.id;
      const marker = document.createElement("i");
      marker.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.textContent = cluster.label;
      const count = document.createElement("b");
      count.textContent = formatCount(visibleCount);
      count.title = `${formatCount(visibleCount)} shown · ${formatCount(cluster.memberCount)} admitted`;
      button.append(marker, label, count);
      root.appendChild(button);
    });
    byId("cluster-count").textContent = `${rows.length} shown groups`;
  }

  function renderScope() {
    const receipt = state.scene.sourceReceipt;
    const visibleNodeIds = new Set(state.scene.nodes.filter(nodeVisible).map(node => node.id));
    const visibleNodes = visibleNodeIds.size;
    const hiddenNodes = state.scene.nodes.length - visibleNodes;
    const visibleEdges = state.scene.edges.filter(edge => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)).length;
    const hiddenEdges = state.scene.edges.length - visibleEdges;
    byId("scope-ledger").innerHTML = [
      ["Emitted nodes", receipt.emittedNodes],
      ["Visible nodes", visibleNodes],
      ["Emitted edges", receipt.emittedEdges],
      ["Visible edges", visibleEdges],
      ["Omitted nodes", receipt.omittedNodes],
      ["Omitted edges", receipt.omittedEdges],
      ["Unknown terms", receipt.unknownCount],
    ].map(([label, value]) => `<dt>${escapeHTML(label)}</dt><dd>${formatCount(value)}</dd>`).join("");
    byId("hidden-receipt").textContent = `${formatCount(hiddenNodes)} NODES · ${formatCount(hiddenEdges)} EDGES HIDDEN`;
    renderCoverage(visibleNodes, visibleEdges);
  }

  function renderCoverage(suppliedVisibleNodes, suppliedVisibleEdges) {
    if (!state.scene) return;
    const receipt = state.scene.sourceReceipt || {};
    const shownNodes = Number.isFinite(suppliedVisibleNodes)
      ? suppliedVisibleNodes
      : state.scene.nodes.filter(nodeVisible).length;
    const shownEdges = Number.isFinite(suppliedVisibleEdges)
      ? suppliedVisibleEdges
      : state.scene.edges.filter(edgeVisible).length;
    const emittedGraphNodes = Number(receipt.emittedNodes) || 0;
    const emittedGraphEdges = Number(receipt.emittedEdges) || 0;
    // The spatial scene also carries source-backed actor/ownership overlays
    // derived from the same coherent bundle. Count those in both admitted and
    // whole view coverage so the inequality remains exact without pretending
    // they were graph-envelope population rows.
    const admittedNodes = state.scene.nodes.length;
    const admittedEdges = state.scene.edges.length;
    const sourceOverlayNodes = Math.max(0, admittedNodes - emittedGraphNodes);
    const sourceOverlayEdges = Math.max(0, admittedEdges - emittedGraphEdges);
    const wholeNodes = (Number(receipt.populationNodes) || emittedGraphNodes) + sourceOverlayNodes;
    const wholeEdges = (Number(receipt.populationEdges) || emittedGraphEdges) + sourceOverlayEdges;
    const root = byId("coverage-summary");
    root.dataset.mode = state.population;
    root.dataset.shown = String(shownNodes);
    root.dataset.admitted = String(admittedNodes);
    root.dataset.whole = String(wholeNodes);
    root.dataset.shownEdges = String(shownEdges);
    root.dataset.admittedEdges = String(admittedEdges);
    root.dataset.wholeEdges = String(wholeEdges);
    root.textContent = `${state.population === "focus" ? "Focus" : "Expanded"} · ${formatCount(shownNodes)} shown of ${formatCount(admittedNodes)} admitted · ${formatCount(wholeNodes)} whole graph · ${formatCount(receipt.omittedNodes)} omitted by envelope`;
  }

  function renderEvents() {
    const occurrences = [...state.scene.occurrences].reverse();
    const root = byId("event-ledger");
    root.innerHTML = occurrences.slice(0, 7).map(occurrence => `
      <li class="${occurrence.motionState === "exact_hold" ? "" : "event-static"}">
        <time>${escapeHTML(shortTime(occurrence.at))}</time>
        <span>${escapeHTML(occurrence.actor)}</span>
        <b>${escapeHTML(occurrence.kind)} · ${escapeHTML(occurrence.id)}</b>
      </li>`).join("") || "<li><span>—</span><span>—</span><b>No recorded occurrences in this window</b></li>";
    if (occurrences.length) {
      const oldest = occurrences.at(-1).at;
      const newest = occurrences[0].at;
      byId("event-window").textContent = `${formatCount(occurrences.length)} · ${shortTime(oldest)}–${shortTime(newest)}`;
    } else byId("event-window").textContent = "0 recorded";
  }

  function segmentMarkup(count, maximum) {
    const on = maximum ? Math.max(count ? 1 : 0, Math.round(count / maximum * 16)) : 0;
    return `<span class="ledger-segments" aria-hidden="true">${Array.from({ length: 16 }, (_, index) => `<i class="${index < on ? "on" : ""}"></i>`).join("")}</span>`;
  }

  function renderEdgeLedger() {
    const counts = new Map();
    state.scene.edges.forEach(edge => counts.set(edge.kind, (counts.get(edge.kind) || 0) + 1));
    const rows = [...counts].sort((left, right) => right[1] - left[1] || compare(left[0], right[0]));
    const maximum = Math.max(0, ...rows.map(row => row[1]));
    byId("edge-ledger").innerHTML = rows.slice(0, 5).map(([kind, count]) => `
      <div class="ledger-row"><span>${escapeHTML(kind.replaceAll("_", " "))}</span><b>${formatCount(count)}</b>${segmentMarkup(count, maximum)}</div>`).join("");
    byId("edge-total").textContent = `${formatCount(state.scene.edges.length)} typed`;
  }

  function renderTraversalGrid() {
    const actors = [...new Set(state.scene.occurrences.map(occurrence => occurrence.actor))].sort(compare);
    const actorIndex = new Map(actors.map((actor, index) => [actor, index % 4]));
    const occurrences = state.scene.occurrences.slice(-100);
    byId("traversal-grid").innerHTML = occurrences.map(occurrence =>
      `<i class="actor-${actorIndex.get(occurrence.actor)}" title="${escapeHTML(`${occurrence.actor} · ${occurrence.kind} · ${occurrence.id}`)}"></i>`
    ).join("") || Array.from({ length: 20 }, () => "<i></i>").join("");
    byId("traversal-total").textContent = `${formatCount(occurrences.length)} cells`;
    byId("traversal-note").textContent = occurrences.length
      ? `Each cell is one recorded occurrence. ${actors.length} actor ${actors.length === 1 ? "identity" : "identities"}; position is order, not duration.`
      : "No recorded occurrences in this window; empty cells carry no activity claim.";
  }

  function renderIntegrity() {
    const health = record(record(state.bundle).operations).health || {};
    const receipt = state.scene.sourceReceipt;
    const rows = [
      ["Missing targets", list(health.missing_targets).length, "failure"],
      ["Cycles", list(health.cycles).length, "failure"],
      ["Lease risk", list(health.expired_claims).length + list(health.expiring_claims).length, "attention"],
      ["Incomplete proof", list(health.done_without_artifact).length, "attention"],
      ["Envelope omissions", receipt.omittedNodes + receipt.omittedEdges, "attention"],
      ["Identity collisions", record(receipt.collisions).identityCount || 0, "failure"],
    ];
    byId("integrity-ledger").innerHTML = rows.map(([label, value, tone]) =>
      `<div class="ledger-row ${Number(value) ? tone : ""}"><span>${escapeHTML(label)}</span><b>${formatCount(value)}</b></div>`
    ).join("");
    const issueCount = rows.reduce((sum, row) => sum + Number(row[1] || 0), 0);
    const summary = issueCount ? `${formatCount(issueCount)} bounded signals` : "No bounded signals";
    byId("integrity-state").textContent = issueCount ? `${formatCount(issueCount)} signals` : "clear";
    byId("evidence-summary").textContent = summary;
  }

  function renderFlow() {
    const operations = record(record(state.bundle).operations);
    const execution = record(operations.execution);
    const metrics = record(operations.metrics);
    const layers = list(execution.layers);
    byId("parallel-width").textContent = `${formatCount(metrics.max_parallel_width)} wide`;
    byId("flow-ledger").innerHTML = [
      ["Critical path", `${formatCount(metrics.critical_path_steps)} steps`],
      ["Measured layers", formatCount(layers.length)],
      ["Dependency-clear planned", formatCount(metrics.dependency_clear_planned)],
      ["Topology status", text(execution.topology_metrics_status, "unknown")],
    ].map(([label, value]) => `<dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd>`).join("");
    drawFlowChart();
  }

  function drawFlowChart() {
    if (!state.scene) return;
    const size = canvasSize(flowCanvas, flowContext);
    const colors = palette();
    flowContext.clearRect(0, 0, size.width, size.height);
    const buckets = Array.from({ length: 18 }, () => 0);
    const parsed = state.scene.occurrences.map(occurrence => new Date(occurrence.at).valueOf()).filter(Number.isFinite);
    if (!parsed.length) {
      flowContext.strokeStyle = colors.line;
      flowContext.beginPath();
      flowContext.moveTo(0, size.height * .72);
      flowContext.lineTo(size.width, size.height * .72);
      flowContext.stroke();
      return;
    }
    const low = Math.min(...parsed);
    const high = Math.max(...parsed);
    parsed.forEach(value => {
      const index = high === low ? buckets.length - 1 : Math.min(buckets.length - 1, Math.floor((value - low) / (high - low) * buckets.length));
      buckets[index] += 1;
    });
    const maximum = Math.max(...buckets, 1);
    const gradient = flowContext.createLinearGradient(0, 0, 0, size.height);
    gradient.addColorStop(0, colors.green);
    gradient.addColorStop(1, colors.blue);
    flowContext.strokeStyle = gradient;
    flowContext.lineWidth = 1.5;
    flowContext.beginPath();
    buckets.forEach((value, index) => {
      const x = index / (buckets.length - 1) * size.width;
      const y = size.height - 7 - value / maximum * (size.height - 15);
      if (!index) flowContext.moveTo(x, y); else flowContext.lineTo(x, y);
    });
    flowContext.stroke();
  }

  function accessibleNodeLabel(node, index, count) {
    return `Node ${index + 1} of ${count}: ${node.kind}, ${node.label}; status ${node.status || "unknown"}; cluster ${node.clusterLabel}; id ${node.id}.`;
  }

  function renderAccessibleNavigator(announceFocus) {
    const root = byId("mesh-a11y");
    const nodes = state.scene ? state.scene.nodes.filter(nodeVisible) : [];
    const node = nodes.find(candidate => candidate.id === state.focusedId) || nodes[0];
    if (!node) {
      root.textContent = "No graph nodes are available.";
      viewport.removeAttribute("aria-activedescendant");
      state.accessibleSceneSignature = "";
      return;
    }
    state.focusedId = node.id;
    const signature = JSON.stringify(nodes.map(candidate => [
      candidate.id,
      candidate.kind,
      candidate.label,
      candidate.status,
      candidate.clusterLabel,
    ]));
    if (signature !== state.accessibleSceneSignature || !byId("mesh-a11y-options")) {
      root.innerHTML = `
        <p id="mesh-a11y-summary">${formatCount(nodes.length)} nodes in ${formatCount(state.scene.clusters.length)} clusters. ${keyboardNavigationHelp}.</p>
        <div id="mesh-a11y-options" role="group" aria-labelledby="mesh-a11y-summary">
          ${nodes.map((candidate, index) => `<div id="mesh-node-option-${index}" role="button" tabindex="-1"${candidate.id === state.selectedId ? ' aria-current="true"' : ""} data-node-id="${escapeHTML(candidate.id)}">${escapeHTML(accessibleNodeLabel(candidate, index, nodes.length))}</div>`).join("")}
        </div>`;
      state.accessibleSceneSignature = signature;
    } else {
      root.querySelectorAll('[role="button"][aria-current="true"]').forEach(option => {
        option.removeAttribute("aria-current");
      });
      const selectedIndex = nodes.findIndex(candidate => candidate.id === state.selectedId);
      if (selectedIndex >= 0) byId(`mesh-node-option-${selectedIndex}`).setAttribute("aria-current", "true");
    }
    const activeIndex = nodes.findIndex(candidate => candidate.id === node.id);
    const activeId = `mesh-node-option-${activeIndex}`;
    viewport.setAttribute("aria-activedescendant", activeId);
    if (announceFocus) {
      announce(`Focused ${node.label}. ${node.kind}; status ${node.status || "unknown"}; cluster ${node.clusterLabel}; node ${activeIndex + 1} of ${nodes.length}. Press Enter to pin.`);
    }
  }

  function directTraversal(nodeId, question) {
    const nodeIds = new Set([nodeId]);
    const edgeIds = new Set();
    let frontier = [nodeId];
    const maximumDepth = question === "impact" ? 4 : 1;
    for (let depth = 0; depth < maximumDepth && frontier.length; depth += 1) {
      const next = [];
      state.scene.edges.forEach(edge => {
        let candidate = "";
        if (question === "impact") {
          if (edge.kind === "depends_on" && frontier.includes(edge.source)) candidate = edge.target;
        } else if (frontier.includes(edge.source)) candidate = edge.target;
        else if (frontier.includes(edge.target)) candidate = edge.source;
        if (!candidate || nodeIds.has(candidate)) return;
        nodeIds.add(candidate);
        edgeIds.add(edge.id);
        next.push(candidate);
      });
      frontier = next.sort(compare);
    }
    state.scene.edges.forEach(edge => {
      if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) edgeIds.add(edge.id);
    });
    return { nodeIds, edgeIds };
  }

  function seedBoardPulse() {
    if (!state.scene || !state.scene.nodes.length) return;
    const attentionTarget = window.CoordOpsAtlasModel.questionTarget(state.model, "attention");
    const target = attentionTarget && state.focusNodeIds.has(attentionTarget)
      ? attentionTarget
      : text([...state.focusNodeIds][0], state.scene.nodes[0].id);
    if (!target || !state.scene.nodesById.has(target)) return;
    state.selectedId = target;
    state.focusedId = target;
    state.question = "pulse";
    state.pathNodeIds = new Set([target]);
    state.pathEdgeIds = new Set();
    state.boardPulseReason = attentionTarget === target
      ? "bounded attention target from the coherent operations document"
      : "first source-recorded node in the bounded focus";
  }

  function selectNode(nodeId, reason) {
    if (!state.scene || !state.scene.nodesById.has(nodeId)) return;
    state.selectedId = nodeId;
    state.focusedId = nodeId;
    const traversal = directTraversal(nodeId, state.question);
    state.pathNodeIds = traversal.nodeIds;
    state.pathEdgeIds = traversal.edgeIds;
    renderInspector(reason);
    renderAccessibleNavigator();
    if (isLowInformation()) renderStaticNodeRoster();
    markDirty();
  }

  function clearSelection() {
    state.selectedId = "";
    state.question = "";
    state.pathNodeIds = new Set();
    state.pathEdgeIds = new Set();
    document.querySelectorAll("#question-buttons button").forEach(button => button.classList.remove("active"));
    seedBoardPulse();
    renderInspector();
    renderAccessibleNavigator();
    if (isLowInformation()) renderStaticNodeRoster();
    announce("Board pulse restored from the current coherent graph.");
    markDirty();
  }

  function renderInspector(reason) {
    const root = byId("selection-inspector");
    const node = state.scene && state.scene.nodesById.get(state.selectedId);
    byId("clear-selection").hidden = !node;
    if (!node) {
      root.innerHTML = "<p class=\"selection-empty\">No admitted node is available in the current coherent graph. No inferred Board pulse was substituted.</p>";
      byId("path-ledger").replaceChildren();
      byId("path-hops").textContent = "0 hops";
      return;
    }
    const receipt = state.scene.sourceReceipt;
    const relatedEdges = state.scene.edges.filter(edge => edge.source === node.id || edge.target === node.id);
    const sourceFields = [...new Set(relatedEdges.map(edge => edge.sourceField).filter(Boolean))].sort(compare);
    root.innerHTML = `
      <p class="selection-kicker">${escapeHTML(state.question === "pulse" ? "BOARD PULSE" : state.question ? `ANSWER · ${state.question}` : node.kind)}</p>
      <h3>${escapeHTML(node.label)}</h3>
      <p class="selection-id">${escapeHTML(node.id)}</p>
      <div class="selection-tags"><span>${escapeHTML(node.status || "unknown")}</span><span>${escapeHTML(node.clusterLabel)}</span><span>${escapeHTML(node.kind)}</span></div>
      ${node.currentStep ? `<p class="selection-step">${escapeHTML(node.currentStep)}</p>` : ""}
      <dl class="selection-receipt">
        <dt>Owner</dt><dd>${escapeHTML(node.owner || "unowned")}</dd>
        <dt>Module</dt><dd>${escapeHTML(node.module || "unassigned")}</dd>
        <dt>Direct relations</dt><dd>${formatCount(relatedEdges.length)}</dd>
        <dt>Source fields</dt><dd>${formatCount(sourceFields.length)}</dd>
        <dt>Envelope</dt><dd>${receipt.complete ? "complete" : "qualified"}</dd>
        <dt>Why selected</dt><dd>${escapeHTML(reason || (state.question === "pulse" ? state.boardPulseReason : "direct selection"))}</dd>
      </dl>`;
    const pathNodes = [...state.pathNodeIds].map(id => state.scene.nodesById.get(id)).filter(Boolean);
    byId("path-ledger").innerHTML = pathNodes.slice(0, 8).map((pathNode, index) => `<li><span>${String(index + 1).padStart(2, "0")}</span>${escapeHTML(pathNode.label)}</li>`).join("");
    byId("path-hops").textContent = `${formatCount(state.pathEdgeIds.size)} edges`;
    if (reason) {
      announce(`Selected ${node.label}. ${relatedEdges.length} direct relationships. Inspector updated from ${reason}.`);
    }
  }

  function answerQuestion(question) {
    if (!state.model || isLowInformation()) return;
    state.question = question;
    document.querySelectorAll("#question-buttons button").forEach(button => button.classList.toggle("active", button.dataset.question === question));
    const target = window.CoordOpsAtlasModel.questionTarget(state.model, question);
    if (target) selectNode(target, `bounded ${question} query`);
    else {
      clearSelection();
      state.question = question;
      byId("selection-inspector").innerHTML = `<p class="selection-empty">The current coherent operations document has no bounded target for “${escapeHTML(question)}.” No inferred answer was substituted.</p>`;
      announce(`No bounded target is available for ${question}. No inferred answer was substituted.`);
      markDirty();
    }
  }

  function edgeColor(edge, colors) {
    if (edge.missing) return colors.red;
    if (edge.kind === "owns") return colors.green;
    if (edge.kind === "depends_on") return colors.blue;
    if (["evidence", "runtime_evidence"].includes(edge.kind)) return colors.amber;
    return colors.muted;
  }

  function nodeColor(node, colors) {
    if (node.missing || statusGroup(node.status) === "attention") return colors.red;
    if (node.kind === "agent") return colors.green;
    if (["job", "artifact"].includes(node.kind)) return colors.amber;
    if (statusGroup(node.status) === "running") return colors.green;
    return colors.blue;
  }

  function quadraticPoint(source, controlPoint, target, progress) {
    const inverse = 1 - progress;
    const interpolate = (key, fallback) =>
      inverse * inverse * Number(source[key] ?? fallback) +
      2 * inverse * progress * Number(controlPoint[key] ?? fallback) +
      progress * progress * Number(target[key] ?? fallback);
    return {
      x: interpolate("x", 0),
      y: interpolate("y", 0),
      z: interpolate("z", 0),
      scale: interpolate("scale", 1),
      depthScale: interpolate("depthScale", 1),
      fog: interpolate("fog", 1),
    };
  }

  function projectedEdge(edge, viewportSize) {
    const source = window.CoordSwarmMeshModel.projectPoint(edge.sourceWorld, state.camera, viewportSize, state.projection);
    const target = window.CoordSwarmMeshModel.projectPoint(edge.targetWorld, state.camera, viewportSize, state.projection);
    const midpoint = {
      x: (edge.sourceWorld.x + edge.targetWorld.x) / 2,
      y: (edge.sourceWorld.y + edge.targetWorld.y) / 2 + edge.bend * .26,
      z: (edge.sourceWorld.z + edge.targetWorld.z) / 2 + edge.bend,
    };
    const controlPoint = window.CoordSwarmMeshModel.projectPoint(midpoint, state.camera, viewportSize, state.projection);
    return { source, target, control: controlPoint };
  }

  function drawClusterHulls(size, colors) {
    state.scene.clusters.forEach((cluster, index) => {
      if (state.clusterFilter && cluster.id !== state.clusterFilter) return;
      const projected = window.CoordSwarmMeshModel.projectPoint(cluster.center, state.camera, size, state.projection);
      const radius = cluster.radius * projected.scale;
      const color = [colors.green, colors.blue, colors.amber][index % 3];
      withAlpha(.1 * projected.fog, () => {
        const gradient = context.createRadialGradient(projected.x, projected.y, 0, projected.x, projected.y, radius);
        gradient.addColorStop(0, color);
        gradient.addColorStop(1, colors.background);
        context.fillStyle = gradient;
        context.beginPath();
        context.ellipse(projected.x, projected.y, radius, radius * .58, 0, 0, Math.PI * 2);
        context.fill();
      });
      withAlpha(.38 * projected.fog, () => {
        context.strokeStyle = color;
        context.lineWidth = 1.15;
        context.setLineDash([3, 7]);
        context.beginPath();
        context.ellipse(projected.x, projected.y, radius, radius * .58, 0, 0, Math.PI * 2);
        context.stroke();
        context.setLineDash([]);
      });
    });
  }

  function drawEdges(size, colors) {
    const visible = new Set(state.scene.nodes.filter(nodeVisible).map(node => node.id));
    const edges = state.scene.edges.filter(edge => visible.has(edge.source) && visible.has(edge.target));
    edges.sort((left, right) => ((left.sourceWorld.z + left.targetWorld.z) - (right.sourceWorld.z + right.targetWorld.z)) || compare(left.id, right.id));
    edges.forEach(edge => {
      const projected = projectedEdge(edge, size);
      const selected = state.pathEdgeIds.has(edge.id) || edge.source === state.selectedId || edge.target === state.selectedId;
      const queryDimmed = state.query && !queryMatches(state.scene.nodesById.get(edge.source)) && !queryMatches(state.scene.nodesById.get(edge.target));
      const fog = (projected.source.fog + projected.control.fog + projected.target.fog) / 3;
      context.save();
      context.globalAlpha = (selected ? .92 : queryDimmed ? .04 : edge.kind === "depends_on" ? .42 : .24) * fog;
      context.strokeStyle = edgeColor(edge, colors);
      context.lineWidth = selected ? 2 : edge.kind === "depends_on" ? 1.2 : .9;
      if (edge.kind === "depends_on") context.setLineDash([4, 4]);
      if (edge.missing) context.setLineDash([2, 6]);
      context.beginPath();
      context.moveTo(projected.source.x, projected.source.y);
      context.quadraticCurveTo(projected.control.x, projected.control.y, projected.target.x, projected.target.y);
      context.stroke();
      context.restore();
      if (selected) drawArrow(projected, edgeColor(edge, colors));
    });
    return edges;
  }

  function drawArrow(projected, color) {
    const near = quadraticPoint(projected.source, projected.control, projected.target, .94);
    const angle = Math.atan2(projected.target.y - near.y, projected.target.x - near.x);
    withAlpha(.88 * projected.target.fog, () => {
      context.fillStyle = color;
      context.beginPath();
      context.moveTo(projected.target.x, projected.target.y);
      context.lineTo(projected.target.x - Math.cos(angle - .55) * 7, projected.target.y - Math.sin(angle - .55) * 7);
      context.lineTo(projected.target.x - Math.cos(angle + .55) * 7, projected.target.y - Math.sin(angle + .55) * 7);
      context.closePath();
      context.fill();
    });
  }

  function drawNodeGlyph(node, projected, colors, selected, hovered, focused) {
    const color = nodeColor(node, colors);
    const base = node.kind === "agent" ? 11 : ["job", "artifact"].includes(node.kind) ? 8 : 6.5;
    const radius = clamp(base * Math.sqrt(projected.scale), 4.8, 17);
    const emphasized = selected || hovered || focused;
    if (emphasized || statusGroup(node.status) === "running") {
      withAlpha(emphasized ? .28 : .12, () => {
        context.fillStyle = color;
        context.beginPath();
        context.arc(projected.x, projected.y, radius * (emphasized ? 2.2 : 1.7), 0, Math.PI * 2);
        context.fill();
      });
    }
    context.save();
    context.strokeStyle = color;
    context.fillStyle = color;
    context.lineWidth = emphasized ? 2 : 1.1;
    if (node.missing) {
      context.beginPath();
      context.moveTo(projected.x - radius, projected.y - radius);
      context.lineTo(projected.x + radius, projected.y + radius);
      context.moveTo(projected.x + radius, projected.y - radius);
      context.lineTo(projected.x - radius, projected.y + radius);
      context.stroke();
    } else if (node.kind === "agent") {
      context.beginPath();
      for (let index = 0; index < 6; index += 1) {
        const angle = -Math.PI / 2 + index * Math.PI / 3;
        const x = projected.x + Math.cos(angle) * radius;
        const y = projected.y + Math.sin(angle) * radius;
        if (!index) context.moveTo(x, y); else context.lineTo(x, y);
      }
      context.closePath();
      context.fill();
      context.strokeStyle = colors.ink;
      context.beginPath();
      context.arc(projected.x, projected.y, Math.max(1.5, radius * .24), 0, Math.PI * 2);
      context.stroke();
    } else if (["job", "artifact"].includes(node.kind)) {
      context.beginPath();
      context.moveTo(projected.x, projected.y - radius);
      context.lineTo(projected.x + radius, projected.y);
      context.lineTo(projected.x, projected.y + radius);
      context.lineTo(projected.x - radius, projected.y);
      context.closePath();
      context.stroke();
      withAlpha(.25, () => context.fill());
    } else {
      context.beginPath();
      context.arc(projected.x, projected.y, radius, 0, Math.PI * 2);
      context.fill();
      if (node.status === "done") {
        context.strokeStyle = colors.background;
        context.beginPath();
        context.moveTo(projected.x - radius * .45, projected.y);
        context.lineTo(projected.x - radius * .08, projected.y + radius * .35);
        context.lineTo(projected.x + radius * .5, projected.y - radius * .42);
        context.stroke();
      }
    }
    context.restore();
    return radius;
  }

  function drawLabel(node, projected, colors, occupied, size) {
    const label = text(node.label, node.id);
    context.font = '650 13px -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif';
    const shown = label.length > 28 ? `${label.slice(0, 27)}…` : label;
    const width = Math.ceil(context.measureText(shown).width) + 12;
    // A label sits to the right of its node. Near the right edge that ran the
    // text off the canvas and clipped it mid-word, which reads as a shorter
    // name rather than as a name that did not fit. Flip to the left side when
    // there is room there, and draw nothing when there is room on neither --
    // an absent label is honest, a truncated one is not.
    let x = projected.x + 11;
    if (size && x + width > size.width - 4) {
      const flipped = projected.x - 11 - width;
      if (flipped < 4) return false;
      x = flipped;
    }
    const y = projected.y - 16;
    const box = { x, y: y - 11, width, height: 19 };
    if (occupied.some(other => box.x < other.x + other.width && box.x + box.width > other.x && box.y < other.y + other.height && box.y + box.height > other.y)) return false;
    occupied.push(box);
    withAlpha(.88, () => {
      context.fillStyle = colors.background;
      context.fillRect(box.x, box.y, box.width, box.height);
    });
    context.fillStyle = colors.ink;
    context.fillText(shown, x + 6, y + 3);
    return true;
  }

  function boxesOverlap(left, right, padding) {
    const gap = Number(padding) || 0;
    return left.x - gap < right.x + right.width
      && left.x + left.width + gap > right.x
      && left.y - gap < right.y + right.height
      && left.y + left.height + gap > right.y;
  }

  function measureClusterLabelMetrics(labels, size) {
    const metricKey = `${state.clusterLabelEpoch}:${size.width}x${size.height}`;
    if (state.clusterLabelMetricKey === metricKey) return;
    const hidden = labels.map(label => label.hidden);
    labels.forEach(label => { label.hidden = false; });
    labels.forEach(label => {
      const box = label.getBoundingClientRect();
      label.dataset.w = String(Math.ceil(box.width));
      label.dataset.h = String(Math.ceil(box.height));
    });
    labels.forEach((label, index) => { label.hidden = hidden[index]; });
    const viewportBox = viewport.getBoundingClientRect();
    state.clusterLabelOverlayBoxes = [document.querySelector(".canvas-tools"), document.querySelector(".axis-key")]
      .filter(Boolean)
      .map(element => element.getBoundingClientRect())
      .filter(box => box.width > 0 && box.height > 0)
      .map(box => ({
        x: box.left - viewportBox.left - 6,
        y: box.top - viewportBox.top - 6,
        width: box.width + 12,
        height: box.height + 12,
      }));
    state.clusterLabelMetricKey = metricKey;
    state.clusterLabelLayoutKey = "";
  }

  function clusterLabelCandidateCenters(anchor, width, height, size) {
    const margin = 6;
    const lowX = margin + width / 2;
    const highX = Math.max(lowX, size.width - margin - width / 2);
    const lowY = margin + height / 2;
    const highY = Math.max(lowY, size.height - margin - height / 2);
    const result = [];
    const seen = new Set();
    const add = (x, y, tier) => {
      const candidate = {
        x: clamp(x, lowX, highX),
        y: clamp(y, lowY, highY),
        tier,
      };
      const key = `${Math.round(candidate.x * 10)}:${Math.round(candidate.y * 10)}`;
      if (seen.has(key)) return;
      seen.add(key);
      result.push(candidate);
    };

    add(anchor.x, anchor.y, 0);
    const stepX = Math.max(28, Math.min(56, width * .42));
    const stepY = height + 8;
    for (let ring = 1; ring <= 4; ring += 1) {
      const offsets = [];
      for (let row = -ring; row <= ring; row += 1) {
        for (let column = -ring; column <= ring; column += 1) {
          if (Math.max(Math.abs(column), Math.abs(row)) !== ring) continue;
          offsets.push({
            x: column * stepX,
            y: row * stepY,
            distance: column * column + row * row,
          });
        }
      }
      offsets.sort((left, right) =>
        left.distance - right.distance
        || Math.abs(left.y) - Math.abs(right.y)
        || left.y - right.y
        || left.x - right.x);
      offsets.forEach(offset => add(anchor.x + offset.x, anchor.y + offset.y, 1));
    }

    // Local rings keep labels near their cluster. The fallback grid makes the
    // solution complete for ordinary board sizes instead of hiding a label
    // merely because its immediate neighborhood is dense.
    const fallback = [];
    for (let y = lowY; y <= highY + .5; y += height + 7) {
      for (let x = lowX; x <= highX + .5; x += 20) {
        fallback.push({
          x,
          y,
          distance: Math.hypot((x - anchor.x) / Math.max(width, 1), (y - anchor.y) / Math.max(height, 1)),
        });
      }
    }
    fallback.sort((left, right) =>
      left.distance - right.distance
      || left.y - right.y
      || left.x - right.x);
    fallback.forEach(candidate => add(candidate.x, candidate.y, 2));
    return result;
  }

  function computeClusterLabelLayout(size) {
    const labels = [...document.querySelectorAll(".cluster-label")];
    measureClusterLabelMetrics(labels, size);
    const cameraKey = [
      state.camera.yaw,
      state.camera.pitch,
      state.camera.zoom,
      state.camera.panX,
      state.camera.panY,
    ].map(value => Number(value).toFixed(4)).join(":");
    const layoutKey = [
      state.clusterLabelMetricKey,
      state.layout,
      state.projection,
      state.clusterFilter,
      cameraKey,
    ].join("|");
    if (state.clusterLabelLayoutKey === layoutKey && state.clusterLabelLayout) {
      return state.clusterLabelLayout;
    }

    const clustersById = new Map(state.scene.clusters.map(cluster => [cluster.id, cluster]));
    const inactive = [];
    const candidates = labels.map(label => ({
      label,
      cluster: clustersById.get(label.dataset.clusterId),
    })).filter(entry => {
      const active = entry.cluster && (!state.clusterFilter || entry.cluster.id === state.clusterFilter);
      if (!active) inactive.push(entry.label);
      return active;
    });
    candidates.sort((left, right) =>
      (right.cluster.memberCount || 0) - (left.cluster.memberCount || 0)
      || compare(left.cluster.id, right.cluster.id));

    const placedBoxes = [];
    const entries = [];
    candidates.forEach(({ label, cluster }) => {
      const anchor = window.CoordSwarmMeshModel.projectPoint({
        x: cluster.center.x,
        y: cluster.center.y - cluster.radius * .65,
        z: cluster.center.z,
      }, state.camera, size, state.projection);
      const width = Number(label.dataset.w) || 60;
      const height = Number(label.dataset.h) || 22;
      const center = clusterLabelCandidateCenters(anchor, width, height, size).find(candidate => {
        const box = {
          x: candidate.x - width / 2,
          y: candidate.y - height / 2,
          width,
          height,
        };
        return ![...state.clusterLabelOverlayBoxes, ...placedBoxes]
          .some(other => boxesOverlap(box, other, 5));
      });
      if (!center) {
        entries.push({ label, cluster, anchor, placement: null });
        return;
      }
      const box = {
        x: center.x - width / 2,
        y: center.y - height / 2,
        width,
        height,
      };
      placedBoxes.push(box);
      entries.push({
        label,
        cluster,
        anchor,
        placement: {
          x: center.x,
          y: center.y,
          box,
          leader: Math.hypot(center.x - anchor.x, center.y - anchor.y) > 8,
        },
      });
    });

    let overlapCount = 0;
    for (let left = 0; left < placedBoxes.length; left += 1) {
      for (let right = left + 1; right < placedBoxes.length; right += 1) {
        if (boxesOverlap(placedBoxes[left], placedBoxes[right], 0)) overlapCount += 1;
      }
    }
    const layout = {
      entries,
      inactive,
      boxes: placedBoxes,
      overlapCount,
      rosterCount: entries.filter(entry => !entry.placement).length,
      leaderCount: entries.filter(entry => entry.placement && entry.placement.leader).length,
    };
    state.clusterLabelLayoutKey = layoutKey;
    state.clusterLabelLayout = layout;
    return layout;
  }

  function getClusterLabelLayout(size) {
    return computeClusterLabelLayout(size);
  }

  function drawNodes(size, colors, clusterLayout) {
    const projected = state.scene.nodes.filter(nodeVisible).map(node => ({
      node,
      projected: window.CoordSwarmMeshModel.projectPoint(node.world, state.camera, size, state.projection),
    })).sort((left, right) => left.projected.z - right.projected.z || compare(left.node.id, right.node.id));
    const occupied = clusterLayout.boxes.map(box => ({ ...box }));
    let labels = 0;
    const searchMatches = projected.filter(entry => queryMatches(entry.node)).map(entry => entry.node.id);
    const searchSet = new Set(searchMatches);
    projected.forEach(entry => {
      const node = entry.node;
      const selected = node.id === state.selectedId;
      const hovered = node.id === state.hoverId;
      const focused = node.id === state.focusedId && viewport === document.activeElement;
      const dimmed = state.query && !searchSet.has(node.id);
      context.save();
      context.globalAlpha = (dimmed ? .12 : 1) * entry.projected.fog;
      const radius = drawNodeGlyph(node, entry.projected, colors, selected, hovered, focused);
      context.restore();
      entry.radius = radius;
    });
    const priorities = [...projected].sort((left, right) => {
      const leftForce = [state.selectedId, state.hoverId, state.focusedId].includes(left.node.id) ? 0 : left.node.labelPriority;
      const rightForce = [state.selectedId, state.hoverId, state.focusedId].includes(right.node.id) ? 0 : right.node.labelPriority;
      return leftForce - rightForce || compare(left.node.id, right.node.id);
    });
    const labelLimit = state.population === "focus"
      ? Math.max(0, 12 - state.visibleClusterLabelCount)
      : state.camera.zoom > 1.45 ? 90 : state.camera.zoom > .98 ? 45 : 20;
    priorities.some(entry => {
      if (labels >= labelLimit) return true;
      const forced = [state.selectedId, state.hoverId, state.focusedId].includes(entry.node.id) || entry.node.labelPriority <= 1 || (state.query && queryMatches(entry.node));
      if (!forced && state.camera.zoom < 1.12) return false;
      if (drawLabel(entry.node, entry.projected, colors, occupied, size)) labels += 1;
      return false;
    });
    state.screenNodes = projected;
    return labels;
  }

  function drawDirectionParticles(edges, size, colors, now) {
    const admitted = edges.filter(edge => edge.admitted).slice(0, 42);
    canvas.dataset.motionEdgeCount = String(admitted.length);
    canvas.dataset.motionSource = admitted.length ? "operations.graph_envelope.edges" : "none";
    if (state.motion !== "direction" || reducedMotion.matches || isLowInformation()) return;
    admitted.forEach((edge, index) => {
      const projected = projectedEdge(edge, size);
      const speed = edge.kind === "depends_on" ? 3600 : 5200;
      const progress = ((now / speed) + (window.CoordSwarmMeshModel.stableHash(edge.id) % 997) / 997 + index * .017) % 1;
      const color = edgeColor(edge, colors);
      for (let trail = 4; trail >= 0; trail -= 1) {
        const at = (progress - trail * .018 + 1) % 1;
        const point = quadraticPoint(projected.source, projected.control, projected.target, at);
        withAlpha((5 - trail) / 5 * .92 * point.fog, () => {
          context.fillStyle = color;
          context.beginPath();
          context.arc(point.x, point.y, (trail ? 1.8 : 3.2) * Math.sqrt(point.depthScale), 0, Math.PI * 2);
          context.fill();
        });
      }
    });
  }

  function drawCommunicationTraffic(size, colors, now) {
    const routes = communicationRoutes().slice(0, 24);
    canvas.dataset.commsRouteCount = String(routes.length);
    canvas.dataset.commsActCount = String(routes.reduce((sum, route) => sum + route.count, 0));
    canvas.dataset.commsSource = state.pulse ? "bundle.pulse.traffic" : "none";
    if (state.motion !== "traffic" || !routes.length) return;
    routes.forEach(route => {
      const hash = window.CoordSwarmMeshModel.stableHash(route.id);
      const edge = {
        sourceWorld: route.source.world,
        targetWorld: route.target.world,
        bend: (hash % 161) - 80,
      };
      const projected = projectedEdge(edge, size);
      const color = route.kind === "audit_request" ? colors.amber : route.kind === "audit_verdict" ? colors.green : colors.blue;
      context.save();
      context.globalAlpha = .64 * (projected.source.fog + projected.target.fog) / 2;
      context.strokeStyle = color;
      context.lineWidth = clamp(1 + Math.log2(route.count + 1) * .35, 1.2, 3.4);
      context.setLineDash([3, 5]);
      context.beginPath();
      context.moveTo(projected.source.x, projected.source.y);
      context.quadraticCurveTo(projected.control.x, projected.control.y, projected.target.x, projected.target.y);
      context.stroke();
      context.restore();
      drawArrow(projected, color);
      if (reducedMotion.matches) return;
      const progress = ((now / 3100) + (hash % 997) / 997) % 1;
      for (let trail = 3; trail >= 0; trail -= 1) {
        const at = (progress - trail * .025 + 1) % 1;
        const point = quadraticPoint(projected.source, projected.control, projected.target, at);
        withAlpha((4 - trail) / 4 * .95 * point.fog, () => {
          context.fillStyle = color;
          context.beginPath();
          context.arc(point.x, point.y, trail ? 2 : 3.8, 0, Math.PI * 2);
          context.fill();
        });
      }
    });
  }

  function drawLiveTracks(size, colors, now) {
    state.activeTracks = state.activeTracks.filter(track => now - track.startedAt < track.duration);
    if (state.motion !== "live" || reducedMotion.matches || isLowInformation()) return;
    state.activeTracks.slice(-12).forEach(track => {
      const edge = state.scene.edgesById.get(track.edgeId);
      if (!edge) return;
      const projected = projectedEdge(edge, size);
      const progress = clamp((now - track.startedAt) / track.duration, 0, 1);
      for (let trail = 5; trail >= 0; trail -= 1) {
        const at = clamp(progress - trail * .025, 0, 1);
        const point = quadraticPoint(projected.source, projected.control, projected.target, at);
        withAlpha((6 - trail) / 6 * point.fog, () => {
          context.fillStyle = colors.green;
          context.beginPath();
          context.arc(point.x, point.y, (trail ? 2 : 3.6) * Math.sqrt(point.depthScale), 0, Math.PI * 2);
          context.fill();
        });
      }
    });
  }

  function updateReplay(now) {
    if (state.motion !== "replay" || reducedMotion.matches || isLowInformation() || !state.scene.occurrences.length) return;
    if (now - state.lastReplayAt < 1050) return;
    const occurrence = state.scene.occurrences[state.replayIndex % state.scene.occurrences.length];
    state.replayIndex += 1;
    state.lastReplayAt = now;
    state.replayPulse = { nodeId: occurrence.nodeId, startedAt: now, occurrence };
    byId("motion-truth").textContent = "RECORDED REPLAY · STATIONARY HISTORY";
  }

  function drawReplayPulse(size, colors, now) {
    updateReplay(now);
    if (!state.replayPulse || !state.replayPulse.nodeId) return;
    const elapsed = now - state.replayPulse.startedAt;
    if (elapsed > 900) {
      state.replayPulse = null;
      return;
    }
    const node = state.scene.nodesById.get(state.replayPulse.nodeId);
    if (!node) return;
    const projected = window.CoordSwarmMeshModel.projectPoint(node.world, state.camera, size, state.projection);
    const progress = elapsed / 900;
    withAlpha((1 - progress) * projected.fog, () => {
      context.strokeStyle = colors.blue;
      context.lineWidth = 1.5;
      context.beginPath();
      context.arc(projected.x, projected.y, (8 + progress * 24) * Math.sqrt(projected.depthScale), 0, Math.PI * 2);
      context.stroke();
    });
  }

  function applyClusterLabelLayout(layout) {
    if (layout.applied) return;
    const rostered = [];
    layout.inactive.forEach(label => {
      label.hidden = true;
      label.dataset.placement = "filtered";
      label.dataset.leader = "false";
    });
    layout.entries.forEach(entry => {
      const { label, cluster, anchor, placement } = entry;
      label.dataset.anchorX = anchor.x.toFixed(2);
      label.dataset.anchorY = anchor.y.toFixed(2);
      if (!placement) {
        label.hidden = true;
        label.dataset.placement = "roster";
        label.dataset.leader = "false";
        rostered.push(cluster.label);
        return;
      }
      label.hidden = false;
      label.style.left = `${placement.x}px`;
      label.style.top = `${placement.y}px`;
      label.dataset.placement = placement.leader ? "leader" : "anchor";
      label.dataset.leader = String(placement.leader);
    });
    state.suppressedClusterLabels = layout.rosterCount;
    state.visibleClusterLabelCount = layout.entries.length - layout.rosterCount;
    const receipt = byId("label-receipt");
    if (receipt) {
      const visibleCount = layout.entries.length - layout.rosterCount;
      receipt.dataset.overlapCount = String(layout.overlapCount);
      receipt.dataset.visibleCount = String(visibleCount);
      receipt.dataset.rosterCount = String(layout.rosterCount);
      receipt.dataset.leaderCount = String(layout.leaderCount);
      receipt.textContent = layout.rosterCount
        ? `${formatCount(visibleCount)}/${formatCount(layout.entries.length)} LABELS SHOWN · ${formatCount(layout.rosterCount)} IN ROSTER`
        : `${formatCount(visibleCount)}/${formatCount(layout.entries.length)} LABELS SHOWN · 0 OVERLAPS`;
      receipt.title = rostered.length
        ? `Placed in cluster roster: ${rostered.join(", ")}`
        : "Every active cluster identity is visible on the canvas.";
      receipt.dataset.state = layout.rosterCount || layout.overlapCount ? "partial" : "complete";
    }
    layout.applied = true;
  }

  function drawClusterLabelLeaders(layout, colors) {
    const leaders = layout.entries.filter(entry =>
      entry.placement && entry.placement.leader
      && Number.isFinite(entry.anchor.x) && Number.isFinite(entry.anchor.y));
    if (!leaders.length) return;
    context.save();
    context.strokeStyle = colors.muted || colors.line;
    context.fillStyle = colors.muted || colors.line;
    context.globalAlpha = .55;
    context.lineWidth = 1;
    context.setLineDash([3, 4]);
    leaders.forEach(({ anchor, placement }) => {
      const dx = placement.x - anchor.x;
      const dy = placement.y - anchor.y;
      const xScale = Math.abs(dx) > .001 ? (placement.box.width / 2) / Math.abs(dx) : Infinity;
      const yScale = Math.abs(dy) > .001 ? (placement.box.height / 2) / Math.abs(dy) : Infinity;
      const scale = Math.min(xScale, yScale, 1);
      const endX = placement.x - dx * scale;
      const endY = placement.y - dy * scale;
      context.beginPath();
      context.moveTo(anchor.x, anchor.y);
      context.lineTo(endX, endY);
      context.stroke();
    });
    context.setLineDash([]);
    leaders.forEach(({ anchor }) => {
      context.beginPath();
      context.arc(anchor.x, anchor.y, 1.5, 0, Math.PI * 2);
      context.fill();
    });
    context.restore();
  }

  function drawFrame(now) {
    state.rafId = 0;
    if (renderingPaused()) {
      byId("rail-fps").textContent = "STILL";
      state.wasAnimating = false;
      return;
    }
    const animating = animationActive(now);
    if (!state.scene || (!state.dirty && !animating && !state.wasAnimating)) return;
    if (animating && !state.wasAnimating) {
      state.frame.count = 0;
      state.frame.sampledAt = now;
    }
    if (animating) {
      state.frame.count += 1;
      if (now - state.frame.sampledAt >= 1000) {
        state.frame.fps = Math.round(state.frame.count * 1000 / (now - state.frame.sampledAt));
        state.frame.count = 0;
        state.frame.sampledAt = now;
        byId("rail-fps").textContent = String(state.frame.fps);
      }
    }
    state.dirty = false;
    const started = performance.now();
    const size = canvasSize(canvas, context);
    const colors = palette();
    const clusterLayout = getClusterLabelLayout(size);
    context.clearRect(0, 0, size.width, size.height);
    drawClusterHulls(size, colors);
    const edges = drawEdges(size, colors);
    drawDirectionParticles(edges, size, colors, now);
    drawCommunicationTraffic(size, colors, now);
    drawLiveTracks(size, colors, now);
    drawClusterLabelLeaders(clusterLayout, colors);
    const nodeLabelCount = drawNodes(size, colors, clusterLayout);
    drawReplayPulse(size, colors, now);
    applyClusterLabelLayout(clusterLayout);
    canvas.dataset.visibleNodeCount = String(state.screenNodes.length);
    canvas.dataset.nodeLabelCount = String(nodeLabelCount);
    canvas.dataset.clusterLabelCount = String(state.visibleClusterLabelCount);
    canvas.dataset.totalGraphLabelCount = String(nodeLabelCount + state.visibleClusterLabelCount);
    state.drawTime = performance.now() - started;
    state.wasAnimating = animating;
    if (animating) scheduleFrame();
    else byId("rail-fps").textContent = "STILL";
  }

  function pickNode(x, y) {
    let best = null;
    let bestDistance = Infinity;
    state.screenNodes.forEach(entry => {
      const distance = Math.hypot(entry.projected.x - x, entry.projected.y - y);
      const threshold = Math.max(10, entry.radius + 6);
      if (distance <= threshold && distance < bestDistance) {
        best = entry.node;
        bestDistance = distance;
      }
    });
    return best;
  }

  function localPointer(event) {
    const rect = viewport.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function showTooltip(node, point) {
    const tooltip = byId("mesh-tooltip");
    if (!node) {
      tooltip.hidden = true;
      return;
    }
    tooltip.innerHTML = `<b>${escapeHTML(node.label)}</b><span>${escapeHTML(node.kind)} · ${escapeHTML(node.status || "unknown")} · ${escapeHTML(node.clusterLabel)}</span>`;
    tooltip.hidden = false;
    const rect = viewport.getBoundingClientRect();
    tooltip.style.left = `${clamp(point.x + 14, 8, rect.width - 280)}px`;
    tooltip.style.top = `${clamp(point.y + 14, 8, rect.height - 70)}px`;
  }

  function defaultCamera() {
    return state.projection === "perspective"
      ? { yaw: -.2, pitch: .28, zoom: 1, panX: 0, panY: 0 }
      : { yaw: 0, pitch: 0, zoom: 1, panX: 0, panY: 0 };
  }

  function setLayout(layout) {
    if (!["swarm", "context", "critical"].includes(layout)) return;
    if (layout === "critical" && isLowInformation()) {
      announce("Critical Flow is unavailable because the authoritative topology admitted zero edges.");
      return;
    }
    state.layout = layout;
    state.clusterFilter = "";
    state.camera = defaultCamera();
    document.querySelectorAll("[data-layout]").forEach(button => {
      const active = button.dataset.layout === layout;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    buildScene();
  }

  function setPopulation(population) {
    if (!["focus", "admitted"].includes(population) || state.population === population) return;
    state.population = population;
    state.clusterFilter = "";
    document.querySelectorAll("[data-population]").forEach(button => {
      const active = button.dataset.population === population;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    buildClusterLabels();
    renderStaticPanels();
    renderCommunicationSummary();
    syncTopologyControls();
    renderInspector();
    renderAccessibleNavigator();
    fitCamera();
    announce(population === "focus"
      ? "Focus restored to at most 60 source-recorded nodes."
      : "Expanded to every node admitted by the current graph envelope.");
  }

  function setProjection(projection) {
    if (!["flat", "perspective"].includes(projection)) return;
    if (projection === "perspective" && isLowInformation()) {
      announce("Perspective is unavailable because the authoritative topology admitted zero edges.");
      return;
    }
    if (state.projection === projection) return;
    state.projection = projection;
    state.camera = defaultCamera();
    syncTopologyControls();
    fitCamera();
    announce(projection === "perspective"
      ? "Perspective enabled. Z is deterministic layout-only separation, not measured data."
      : "2D enabled. Layout-only z separation is flattened.");
  }

  function setMotion(mode) {
    if (!["live", "replay", "direction", "traffic"].includes(mode)) return;
    if (mode === "traffic" && !communicationRoutes().length) return;
    if (mode !== "traffic" && isLowInformation()) return;
    state.motion = mode;
    state.replayPulse = null;
    state.lastReplayAt = 0;
    document.querySelectorAll("[data-motion]").forEach(button => {
      const active = button.dataset.motion === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const badge = byId("motion-truth");
    badge.className = `truth-badge ${mode}`;
    if (mode === "live") badge.textContent = "LIVE ARRIVALS · FIRST READ SILENT";
    if (mode === "replay") badge.textContent = "RECORDED REPLAY · STATIONARY HISTORY";
    if (mode === "direction") badge.textContent = "SCHEMATIC DIRECTION · NOT ACTIVITY";
    if (mode === "traffic") badge.textContent = "RECORDED TRAFFIC · NOT CURRENT ACTIVITY";
    markDirty();
  }

  function fitCamera() {
    if (!state.scene) return;
    const size = canvasSize(canvas, context);
    const points = state.scene.nodes.filter(nodeVisible).map(node => node.world);
    state.camera = window.CoordSwarmMeshModel.fitCameraToPoints(
      points,
      state.camera,
      size,
      { padding: size.width < 520 ? 58 : 78, minZoom: .25, maxZoom: 2.5, projection: state.projection },
    );
    byId("camera-zoom").textContent = `${Math.round(state.camera.zoom * 100)}%`;
    markDirty();
  }

  function moveSpatialFocus(direction) {
    if (!state.screenNodes.length) return;
    const current = state.screenNodes.find(entry => entry.node.id === state.focusedId) || state.screenNodes[0];
    let best = null;
    let score = Infinity;
    state.screenNodes.forEach(candidate => {
      if (candidate === current) return;
      const dx = candidate.projected.x - current.projected.x;
      const dy = candidate.projected.y - current.projected.y;
      const directional = direction === "left" ? dx < -2 : direction === "right" ? dx > 2 : direction === "up" ? dy < -2 : dy > 2;
      if (!directional) return;
      const primary = ["left", "right"].includes(direction) ? Math.abs(dx) : Math.abs(dy);
      const secondary = ["left", "right"].includes(direction) ? Math.abs(dy) : Math.abs(dx);
      const candidateScore = primary + secondary * 2.2;
      if (candidateScore < score) {
        score = candidateScore;
        best = candidate;
      }
    });
    if (best) {
      state.focusedId = best.node.id;
      renderAccessibleNavigator(true);
      markDirty();
    } else {
      announce(`No graph node is ${direction} of ${current.node.label}. Focus remains on ${current.node.label}.`);
    }
  }

  viewport.addEventListener("pointerdown", event => {
    if (event.target.closest("button, input, select, a")) return;
    const point = localPointer(event);
    state.drag = { ...point, startX: point.x, startY: point.y, yaw: state.camera.yaw, pitch: state.camera.pitch, panX: state.camera.panX, panY: state.camera.panY, pan: state.projection === "flat" || event.shiftKey || event.button === 1 };
    viewport.classList.add("dragging");
    try {
      viewport.setPointerCapture(event.pointerId);
    } catch (_error) {
      // Synthetic and accessibility-driven drags may not own a native pointer.
    }
  });

  viewport.addEventListener("pointermove", event => {
    const point = localPointer(event);
    state.pointer = point;
    if (state.drag) {
      const dx = point.x - state.drag.x;
      const dy = point.y - state.drag.y;
      if (state.drag.pan) {
        state.camera.panX = state.drag.panX + dx;
        state.camera.panY = state.drag.panY + dy;
      } else {
        state.camera.yaw = clamp(state.drag.yaw + dx * .0045, -.85, .85);
        state.camera.pitch = clamp(state.drag.pitch + dy * .0035, .08, .65);
      }
      markDirty();
      return;
    }
    const node = pickNode(point.x, point.y);
    const nextHover = text(node && node.id);
    if (nextHover !== state.hoverId) {
      state.hoverId = nextHover;
      markDirty();
    }
    showTooltip(node, point);
  });

  viewport.addEventListener("pointerup", event => {
    if (!state.drag) return;
    const point = localPointer(event);
    const moved = Math.hypot(point.x - state.drag.startX, point.y - state.drag.startY);
    state.drag = null;
    viewport.classList.remove("dragging");
    if (moved < 5) {
      const node = pickNode(point.x, point.y);
      if (node) selectNode(node.id, "canvas selection");
    }
  });
  viewport.addEventListener("pointercancel", () => { state.drag = null; viewport.classList.remove("dragging"); markDirty(); });
  viewport.addEventListener("pointerleave", () => { if (!state.drag) { state.hoverId = ""; showTooltip(null); markDirty(); } });
  viewport.addEventListener("wheel", event => {
    event.preventDefault();
    state.camera.zoom = clamp(state.camera.zoom * Math.exp(-event.deltaY * .0012), .55, 2.5);
    byId("camera-zoom").textContent = `${Math.round(state.camera.zoom * 100)}%`;
    markDirty();
  }, { passive: false });

  viewport.addEventListener("keydown", event => {
    const direction = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" }[event.key];
    if (direction && event.shiftKey && state.projection === "perspective") {
      event.preventDefault();
      if (direction === "left") state.camera.yaw = clamp(state.camera.yaw - .08, -.85, .85);
      if (direction === "right") state.camera.yaw = clamp(state.camera.yaw + .08, -.85, .85);
      if (direction === "up") state.camera.pitch = clamp(state.camera.pitch - .06, .08, .65);
      if (direction === "down") state.camera.pitch = clamp(state.camera.pitch + .06, .08, .65);
      markDirty();
      return;
    }
    if (direction) {
      event.preventDefault();
      moveSpatialFocus(direction);
      return;
    }
    if (event.key === "Enter" && state.focusedId) selectNode(state.focusedId, "keyboard selection");
    if (event.key === "Escape") clearSelection();
    if (["+", "="].includes(event.key)) state.camera.zoom = clamp(state.camera.zoom * 1.12, .55, 2.5);
    if (event.key === "-") state.camera.zoom = clamp(state.camera.zoom / 1.12, .55, 2.5);
    if (event.key === "0") state.camera = defaultCamera();
    if (["+", "=", "-", "0"].includes(event.key)) {
      byId("camera-zoom").textContent = `${Math.round(state.camera.zoom * 100)}%`;
      markDirty();
    }
  });

  byId("layout-controls").addEventListener("click", event => {
    const button = event.target.closest("[data-layout]");
    if (button) setLayout(button.dataset.layout);
  });
  byId("population-controls").addEventListener("click", event => {
    const button = event.target.closest("[data-population]");
    if (button) setPopulation(button.dataset.population);
  });
  document.querySelector(".canvas-tools").addEventListener("click", event => {
    const button = event.target.closest("[data-projection]");
    if (button) setProjection(button.dataset.projection);
  });
  byId("motion-controls").addEventListener("click", event => {
    const button = event.target.closest("[data-motion]");
    if (button) setMotion(button.dataset.motion);
  });
  byId("cluster-roster").addEventListener("click", event => {
    const button = event.target.closest("[data-cluster-id]");
    if (!button) return;
    state.clusterFilter = state.clusterFilter === button.dataset.clusterId ? "" : button.dataset.clusterId;
    renderClusterRoster();
    renderScope();
    renderAccessibleNavigator();
    markDirty();
  });
  byId("question-buttons").addEventListener("click", event => {
    const button = event.target.closest("[data-question]");
    if (button) answerQuestion(button.dataset.question);
  });
  byId("clear-selection").addEventListener("click", clearSelection);
  byId("static-node-roster").addEventListener("click", event => {
    const button = event.target.closest("[data-static-node]");
    if (button) selectNode(button.dataset.staticNode, "static node roster");
  });
  byId("mesh-search").addEventListener("input", event => {
    state.query = event.target.value;
    const first = state.scene && state.scene.nodes.find(node => queryMatches(node));
    if (first) state.focusedId = first.id;
    reportSearchReach();
    renderAccessibleNavigator();
    if (isLowInformation()) renderStaticNodeRoster();
    markDirty();
  });
  byId("mesh-search").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      const first = state.scene && state.scene.nodes.find(node => queryMatches(node));
      if (first) {
        selectNode(first.id, "search result");
        fitCamera();
      }
    }
    if (event.key === "Escape") {
      event.target.value = "";
      state.query = "";
      setAlert("");
      if (isLowInformation()) renderStaticNodeRoster();
      event.target.blur();
      markDirty();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      byId("mesh-search").focus();
    }
  });
  byId("camera-minus").addEventListener("click", () => { state.camera.zoom = clamp(state.camera.zoom / 1.16, .55, 2.5); byId("camera-zoom").textContent = `${Math.round(state.camera.zoom * 100)}%`; markDirty(); });
  byId("camera-plus").addEventListener("click", () => { state.camera.zoom = clamp(state.camera.zoom * 1.16, .55, 2.5); byId("camera-zoom").textContent = `${Math.round(state.camera.zoom * 100)}%`; markDirty(); });
  byId("camera-fit").addEventListener("click", fitCamera);
  byId("camera-reset").addEventListener("click", () => { state.camera = defaultCamera(); byId("camera-zoom").textContent = "100%"; markDirty(); });
  byId("mesh-refresh").addEventListener("click", refresh);
  byId("mesh-freeze").addEventListener("click", () => {
    state.frozen = !state.frozen;
    byId("mesh-freeze").setAttribute("aria-pressed", String(state.frozen));
    byId("mesh-freeze").textContent = state.frozen ? "Resume view" : "Freeze";
    if (state.frozen) {
      state.wasAnimating = false;
      byId("rail-fps").textContent = "STILL";
    }
    if (!state.frozen && state.pendingBundle) {
      const pending = state.pendingBundle;
      state.pendingBundle = null;
      applyBundle(pending);
      setAlert("The view resumed and applied the newest coherent generation observed while frozen.", "");
      setTimeout(() => setAlert(""), 3200);
    } else if (!state.frozen) markDirty();
  });

  reducedMotion.addEventListener("change", () => {
    state.replayPulse = null;
    byId("rail-fps").textContent = reducedMotion.matches ? "STILL" : "—";
    markDirty();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      state.wasAnimating = false;
      byId("rail-fps").textContent = "STILL";
    } else markDirty();
  });
  const resizeObserver = new ResizeObserver(entries => {
    if (entries.some(entry => entry.target === flowCanvas)) drawFlowChart();
    markDirty();
  });
  resizeObserver.observe(flowCanvas);
  resizeObserver.observe(viewport);

  setMeshTruthCore();
  setMotion("direction");
  refresh();
  window.setInterval(refresh, 5000);
})();
