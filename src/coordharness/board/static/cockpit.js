// The coordination map.
//
// The board answers "what is the state of every row". This answers the question
// you actually ask when a fleet is running: who is working where, what is
// waiting on what, and what did anyone say about it. Three views over the same
// read-only snapshot, no writes and no second source of truth.
//
// Embedded in the native cockpit through /cockpit?native_map=1, which hides the
// masthead so the page sits inside the window's own chrome.

const esc = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const LANES = {
  claude: { label: "Chat agents", mark: "/static/mark-claude.png" },
  codex: { label: "Code agents", mark: "/static/mark-codex.png" },
  local: { label: "Local runners", mark: "/static/mark-compute.png" },
  service: { label: "Scheduled", mark: null },
};
const lane = owner => String(owner || "").split(":")[0].trim().toLowerCase();

const STATE_ORDER = ["running", "blocked", "attention", "next", "planned", "done", "unknown"];
const STATE_ALIASES = {
  queued: "next",
  failed: "attention",
  complete: "done",
  completed: "done",
  closed: "done",
};
const stateOf = row => {
  const status = String(row.status || "").toLowerCase();
  if (STATE_ORDER.includes(status)) return status;
  return STATE_ALIASES[status] || "unknown";
};

let snapshot = null;
let graph = null;
let context = null;
let timeline = null;
let operations = null;
let bundleProjection = null;
let readStatus = null;
let pulse = null;
let selected = null;
let continuousCommsMode = false;
let continuousCommsSection = "";
let continuousHeightFrame = 0;
let continuousPublishedHeight = 0;
let depsHops = 1;
let searchController = null;
let bundleFailure = "";
let pulseFailure = "";
let pulseRetained = false;
let pulseCoherent = false;
let lastDrawerTrigger = null;
let coreRequestTicket = 0;
let coreController = null;
let orbitResizeFrame = 0;
const RELATIONSHIP_LENSES = new Set(["ceiling", "deps", "crossings"]);
const MORE_LENSES = new Set([
  "flowpath", "ceiling", "topology", "shape", "crossings",
  "chronicle", "subjects", "orbit", "context",
]);

// ---------------------------------------------------------------- fleet view

// Agents down the side, verticals across the top. A cell is a count, weighted so
// recorded running work is what your eye lands on: an idle module
// should recede, not compete with one where four agents are mid-flight.
function renderFleet() {
  const rows = snapshot.rows.filter(r => r.bucket !== "epic");
  const modules = [...new Set(rows.map(r => r.module).filter(Boolean))].sort();
  const showTotals = modules.length > 1;
  // Unowned rows are real work and were being dropped from every cell while the
  // footer total still counted them, so the columns did not add up to their own
  // sum. They get a row of their own rather than being silently excluded.
  const UNOWNED = "\u2014 unowned";
  const ownerOf = row => row.owner || UNOWNED;
  const owners = [...new Set(rows.map(ownerOf))]
    .sort((a, b) => (a === UNOWNED) - (b === UNOWNED) || a.localeCompare(b));

  if (!owners.length) {
    return `<div class="card"><h3>No work</h3><p class="meta">This bundle publishes no rows to map.</p></div>`;
  }

  const cell = (owner, module) => {
    const here = rows.filter(r => ownerOf(r) === owner && r.module === module);
    if (!here.length) return `<td class="c0"></td>`;
    const running = here.filter(r => stateOf(r) === "running").length;
    const stuck = here.filter(r => ["blocked", "attention"].includes(stateOf(r))).length;
    const weight = running ? "hot" : stuck ? "warn" : "cool";
    const titles = here.map(r => `${r.id} ${r.title}`).join("\n");
    // A cell is a way in, not a readout: clicking it opens the rows it counts.
    return `<td class="c ${weight}" title="${esc(titles)}" tabindex="0" role="button"
      data-cell="${esc(owner)}|${esc(module)}"><b>${here.length}</b>${
      running ? `<i class="dot run"></i>` : ""}${stuck ? `<i class="dot stuck"></i>` : ""}</td>`;
  };

  const head = `<tr><th class="corner">agent \\ vertical</th>${
    modules.map(m => `<th><span>${esc(m)}</span></th>`).join("")}${showTotals ? '<th class="tot">all</th>' : ""}</tr>`;
  const body = owners.map(owner => {
    const meta = LANES[lane(owner)] || { label: lane(owner), mark: null };
    const mine = rows.filter(r => ownerOf(r) === owner);
    const live = snapshot.sessions.some(s => s.id === owner && s.live);
    return `<tr><th class="who">${
      meta.mark ? `<img class="mark" src="${meta.mark}" alt="">` : `<i class="dot ${live ? "run" : ""}"></i>`
    }<span>${esc(owner)}</span></th>${
      modules.map(m => cell(owner, m)).join("")
    }${showTotals ? `<td class="tot"><b>${mine.length}</b></td>` : ""}</tr>`;
  }).join("");
  const foot = `<tr><th class="who">all</th>${
    modules.map(m => `<td class="tot"><b>${rows.filter(r => r.module === m).length}</b></td>`).join("")
  }${showTotals ? `<td class="tot"><b>${rows.length}</b></td>` : ""}</tr>`;

  const running = rows.filter(r => stateOf(r) === "running");
  const strip = Object.entries(LANES).map(([key, meta]) => {
    const mine = running.filter(r => lane(r.owner) === key);
    if (!mine.length) return "";
    return `<div class="lane"><p class="meta">${
      meta.mark ? `<img class="mark" src="${meta.mark}" alt="">` : ""}${esc(meta.label)} - ${mine.length} recorded running</p>${
      mine.map(r => `<article class="wisp" tabindex="0" data-row="${esc(r.id)}"><b>${esc(r.title)}</b><span class="meta">${esc(r.owner)} - ${esc(r.module || "unassigned")}</span>${
        r.current_step ? `<span class="step">${esc(r.current_step)}</span>` : ""}</article>`).join("")}</div>`;
  }).join("");

  const matrixHeading = continuousCommsMode
    ? `<h3 class="continuous-fleet-label" id="fleet-matrix-label">Fleet matrix <span>${owners.length} agents · ${modules.length} verticals</span></h3>`
    : `<h3 id="fleet-matrix-label">Who is working where</h3>`;
  const matrixDescription = `<p class="${continuousCommsMode ? "visually-hidden" : "meta"}" id="fleet-matrix-description">${
    owners.length} agents across ${modules.length} verticals. A cell counts the rows that agent holds in that vertical; the marker says whether any row is recorded running.</p>`;
  const continuousReceipts = continuousCommsMode
    ? projectionReceiptHtml() + topologyExplanationHtml() : "";
  return `<div class="card">${matrixHeading}${matrixDescription}
    <div class="tablewrap"><table class="matrix" aria-labelledby="fleet-matrix-label" aria-describedby="fleet-matrix-description">${head}${body}${foot}</table></div>
    ${continuousReceipts}
    <p class="legend"><i class="dot run"></i>running <i class="dot stuck"></i>blocked or needing attention</p></div>
    <div class="lanes">${strip || `<div class="card"><p class="meta">No row is recorded running in this bundle.</p></div>`}</div>`;
}

// ----------------------------------------------------------- dependency view

// Ranked left to right by depth from a root, which is what removes most edge
// crossings; the board's smaller graph uses the same ordering.
function layout(nodes, edges) {
  const byId = new Map(nodes.map(n => [n.id, n]));
  const incoming = new Map(nodes.map(n => [n.id, 0]));
  edges.forEach(e => { if (byId.has(e.target)) incoming.set(e.target, incoming.get(e.target) + 1); });

  const depth = new Map();
  const roots = nodes.filter(n => !incoming.get(n.id));
  const queue = roots.map(n => [n.id, 0]);
  while (queue.length) {
    const [id, d] = queue.shift();
    if (depth.has(id) && depth.get(id) >= d) continue;
    depth.set(id, d);
    edges.filter(e => e.source === id).forEach(e => { if (byId.has(e.target)) queue.push([e.target, d + 1]); });
  }
  nodes.forEach(n => { if (!depth.has(n.id)) depth.set(n.id, 0); });

  // Depth alone gives a first column holding every root, which for a board of
  // any size is one very tall column stretched to the page width -- and a
  // stretched SVG scales its labels with it. Tall columns wrap into sub-columns
  // so the drawing keeps a readable aspect and the type stays the size it was
  // set at.
  const PER_COLUMN = 14;
  const columns = new Map();
  nodes.forEach(n => {
    const d = depth.get(n.id);
    if (!columns.has(d)) columns.set(d, []);
    columns.get(d).push(n);
  });

  const lanes = [];
  [...columns.keys()].sort((a, b) => a - b).forEach(d => {
    const group = columns.get(d);
    for (let i = 0; i < group.length; i += PER_COLUMN) {
      lanes.push({ depth: d, nodes: group.slice(i, i + PER_COLUMN) });
    }
  });

  const tallest = Math.max(...lanes.map(l => l.nodes.length), 1);
  const gap = 30;
  const colGap = 250;
  const placed = new Map();
  lanes.forEach((column, index) => {
    column.nodes.forEach((n, i) => {
      placed.set(n.id, { x: 24 + index * colGap, y: 30 + i * gap, node: n, depth: column.depth });
    });
  });
  return {
    placed,
    width: 24 + (lanes.length - 1) * colGap + 210,
    height: 60 + (tallest - 1) * gap,
  };
}

// Relationships differ, so they should not draw identically. Containment runs
// one way and is structural; a dependency is a constraint on ordering and is
// what you scan for when something is stuck.
const EDGE_KINDS = {
  parent: { label: "belongs to", cls: "kin", note: "structural containment" },
  depends_on: { label: "waits on", cls: "dep", note: "ordering constraint" },
};

let depsLayout = "ranked";

// Ranked answers "what comes before what". Clustered answers "where does the
// work sit" -- the same edges, grouped by vertical, which makes a dependency
// that crosses verticals visible as a line leaving its band.
function clusterLayout(nodes) {
  const rows = rowById();
  // Snapshot ids and graph node ids agree for jobs (`job:x`) and differ for work
  // (`work:UI-101` against `UI-101`), so try the raw id before the stripped one.
  // Stripping first put every job in "unassigned".
  const groupOf = node => {
    const row = rows.get(node.id) || rows.get(node.id.replace(/^(work|job):/, ""));
    return (row && row.module) || "unassigned";
  };
  const groups = new Map();
  nodes.forEach(n => {
    const key = groupOf(n);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  });
  const ordered = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  const placed = new Map();
  const bands = [];
  const perRow = 4;
  const cellW = 176, cellH = 30, bandPad = 34;
  let y = 40;
  ordered.forEach(([name, members]) => {
    const height = Math.ceil(members.length / perRow) * cellH;
    bands.push({ name, y: y - 20, height: height + 12, count: members.length });
    members.forEach((n, i) => {
      placed.set(n.id, {
        x: 30 + (i % perRow) * cellW,
        y: y + Math.floor(i / perRow) * cellH,
        node: n,
      });
    });
    y += height + bandPad;
  });
  return { placed, bands, width: 30 + perRow * cellW + 40, height: y };
}


const DEPS_STRUCTURAL_LIMIT = 60;
let depsStructuralMode = "focus";

const depsStableCompare = (left, right) => {
  const a = String(left ?? "");
  const b = String(right ?? "");
  return a < b ? -1 : a > b ? 1 : 0;
};

function depsStructuralFocus(nodes, edges, limit = DEPS_STRUCTURAL_LIMIT) {
  const admittedNodes = Array.isArray(nodes) ? nodes.slice() : [];
  const admittedEdges = Array.isArray(edges) ? edges.slice() : [];
  const orderedNodes = admittedNodes
    .filter(node => node && node.id != null)
    .sort((a, b) => depsStableCompare(a.id, b.id));
  const byId = new Map(orderedNodes.map(node => [String(node.id), node]));
  const adjacency = new Map([...byId.keys()].map(id => [id, new Set()]));
  const degree = new Map([...byId.keys()].map(id => [id, 0]));

  admittedEdges.forEach(edge => {
    if (!edge) return;
    const source = String(edge.source);
    const target = String(edge.target);
    if (!byId.has(source) || !byId.has(target)) return;
    adjacency.get(source).add(target);
    adjacency.get(target).add(source);
    degree.set(source, degree.get(source) + 1);
    degree.set(target, degree.get(target) + 1);
  });

  const ranked = [...byId.keys()].sort((a, b) =>
    (degree.get(b) - degree.get(a)) || depsStableCompare(a, b));
  const seed = ranked[0] || null;
  const visible = new Set();
  const queue = seed ? [seed] : [];
  const cappedLimit = Math.max(0, Math.min(DEPS_STRUCTURAL_LIMIT, Number(limit) || 0));

  while (queue.length && visible.size < cappedLimit) {
    const id = queue.shift();
    if (visible.has(id)) continue;
    visible.add(id);
    const neighbours = [...(adjacency.get(id) || [])]
      .filter(neighbour => !visible.has(neighbour))
      .sort((a, b) => (degree.get(b) - degree.get(a)) || depsStableCompare(a, b));
    neighbours.forEach(neighbour => {
      if (!visible.has(neighbour) && !queue.includes(neighbour)) queue.push(neighbour);
    });
  }

  const keptNodes = orderedNodes.filter(node => visible.has(String(node.id)));
  const keptEdges = admittedEdges
    .filter(edge => edge && visible.has(String(edge.source)) && visible.has(String(edge.target)))
    .sort((a, b) => depsStableCompare(
      [a.source, a.target, a.kind, a.id].join("\u0000"),
      [b.source, b.target, b.kind, b.id].join("\u0000"),
    ));

  return {
    nodes: keptNodes,
    edges: keptEdges,
    hiddenNodes: admittedNodes.length - keptNodes.length,
    hiddenEdges: admittedEdges.length - keptEdges.length,
    totalNodes: admittedNodes.length,
    totalEdges: admittedEdges.length,
    seed,
    seedDegree: seed ? degree.get(seed) : 0,
  };
}


const DEPS_VISIBLE_LABEL_LIMIT = 28;

function depsVisibleLabels(nodes) {
  const entries = (Array.isArray(nodes) ? nodes : [])
    .filter(node => node && node.id != null)
    .map(node => ({
      id: String(node.id),
      label: String(node.label || node.id),
    }))
    .sort((a, b) => depsStableCompare(a.id, b.id));
  const groups = new Map();
  entries.forEach(entry => {
    if (!groups.has(entry.label)) groups.set(entry.label, []);
    groups.get(entry.label).push(entry.id);
  });
  const labels = new Map();

  entries.forEach(entry => {
    const duplicates = groups.get(entry.label) || [];
    if (duplicates.length === 1) {
      const display = entry.label.length <= DEPS_VISIBLE_LABEL_LIMIT
        ? entry.label
        : entry.label.slice(0, DEPS_VISIBLE_LABEL_LIMIT - 1) + "…";
      labels.set(entry.id, display);
      return;
    }

    const compactIds = duplicates.map(id => id.replace(/^(work|job):/, ""));
    const compact = entry.id.replace(/^(work|job):/, "");
    let suffixLength = Math.min(4, compact.length);
    while (suffixLength < Math.min(12, compact.length)
        && compactIds.filter(id => id.slice(-suffixLength) === compact.slice(-suffixLength)).length > 1) {
      suffixLength += 1;
    }
    let suffix = compact.slice(-suffixLength);
    if (compactIds.filter(id => id.slice(-suffixLength) === suffix).length > 1) {
      const stableIndex = duplicates.indexOf(entry.id) + 1;
      suffix = suffix + "#" + stableIndex;
    }
    const separator = " · ";
    const labelBudget = Math.max(1, DEPS_VISIBLE_LABEL_LIMIT - separator.length - suffix.length);
    const base = entry.label.length <= labelBudget
      ? entry.label
      : entry.label.slice(0, Math.max(1, labelBudget - 1)) + "…";
    labels.set(entry.id, base + separator + suffix);
  });
  return labels;
}

function depsStructuralControls(cut, focused) {
  const mode = depsStructuralMode === "admitted" ? "admitted" : "focus";
  const shownNodes = cut.nodes.length;
  const shownEdges = cut.edges.length;
  const hiddenNodes = cut.totalNodes - shownNodes;
  const hiddenEdges = cut.totalEdges - shownEdges;
  const focusActive = mode === "focus";
  const reason = focused.seed
    ? `Focus chose ${focused.seed} because its ${focused.seedDegree} admitted incident relationships are highest; stable ID breaks ties. Connected breadth-first expansion then admits at most ${DEPS_STRUCTURAL_LIMIT} nodes, ordered by degree and stable ID. No row selection was created.`
    : "Focus found no admitted node to seed. No row selection was created.";

  return `<div class="hop-control structural-focus-control" role="group" aria-label="Structural graph scope">
      <span class="meta">Structural scope</span>
      <button type="button" class="${focusActive ? "active" : ""}" data-deps-scope="focus" aria-pressed="${focusActive}">${esc(`Focus ≤${DEPS_STRUCTURAL_LIMIT}`)}</button>
      <button type="button" class="${!focusActive ? "active" : ""}" data-deps-scope="admitted" aria-pressed="${!focusActive}">Expand admitted</button>
    </div>
    <div class="projection-receipt structural-focus-receipt" data-deps-mode="${mode}"
      data-shown-nodes="${shownNodes}" data-admitted-nodes="${cut.totalNodes}" data-hidden-nodes="${hiddenNodes}"
      data-shown-edges="${shownEdges}" data-admitted-edges="${cut.totalEdges}" data-hidden-edges="${hiddenEdges}">
      <p class="meta nb-count">Nodes: ${shownNodes} shown of ${cut.totalNodes} admitted (${hiddenNodes} hidden).</p>
      <p class="meta nb-count-edges">Edges: ${shownEdges} shown of ${cut.totalEdges} admitted (${hiddenEdges} hidden).</p>
      <p class="meta nb-note">${esc(reason)}</p>
    </div>`;
}

function renderDeps() {
  const allNodes = graph.nodes || [];
  const allEdges = graph.edges || [];
  if (!allNodes.length) {
    return `<div class="card"><h3>No recorded relationships</h3><p class="meta">Nothing on this board declares a parent or a dependency.</p></div>`;
  }
  const focused = selected ? null : depsStructuralFocus(allNodes, allEdges);
  const cut = selected
    ? coordNeighbourhood(allNodes, allEdges, selected, depsHops)
    : depsStructuralMode === "admitted"
      ? {
          nodes: allNodes.slice(), edges: allEdges.slice(),
          hiddenNodes: 0, hiddenEdges: 0,
          totalNodes: allNodes.length, totalEdges: allEdges.length,
          seed: focused.seed, seedDegree: focused.seedDegree,
        }
      : focused;
  const nodes = cut.nodes;
  const edges = cut.edges;
  const visibleLabels = depsVisibleLabels(nodes);
  const laid = depsLayout === "clustered" ? clusterLayout(nodes) : layout(nodes, edges);
  const { placed, width, height } = laid;

  // Degree, so a node everything hangs off reads as one. Counting both
  // directions: an epic with many children and a row many things wait on are
  // both hubs, for different reasons.
  const degree = new Map(nodes.map(n => [n.id, 0]));
  edges.forEach(e => {
    [e.source, e.target].forEach(id => degree.has(id) && degree.set(id, degree.get(id) + 1));
  });
  const busiest = Math.max(...degree.values(), 1);

  const paths = edges.map(e => {
    const a = placed.get(e.source), b = placed.get(e.target);
    if (!a || !b) return "";
    const mid = (a.x + b.x) / 2;
    const kind = EDGE_KINDS[e.kind] || { cls: "kin" };
    const missing = e.relationship_state !== "source_bound";
    return `<path d="M${a.x + 8} ${a.y} C${mid} ${a.y}, ${mid} ${b.y}, ${b.x - 12} ${b.y}" class="edge ${kind.cls}${
      missing ? " dashed" : ""}" marker-end="url(#tip-${kind.cls})"><title>${esc(e.source)} ${
      esc((EDGE_KINDS[e.kind] || {}).label || e.kind)} ${esc(e.target)}</title></path>`;
  }).join("");

  const marks = [...placed.values()].map(({ x, y, node }) => {
    const state = String(node.status || "").toLowerCase();
    const cls = state === "running" ? "run" : ["blocked", "attention", "failed"].includes(state) ? "stuck"
      : state === "done" ? "done" : "";
    const weight = degree.get(node.id) || 0;
    const radius = 4 + Math.round((weight / busiest) * 5);
    // A job runs on hardware and a work item is a plan; a square and a circle
    // say that without a legend entry per status.
    const shape = node.kind === "job"
      ? `<rect x="${-radius}" y="${-radius}" width="${radius * 2}" height="${radius * 2}" rx="1.5"></rect>`
      : `<circle r="${radius}"></circle>`;
    return `<g transform="translate(${x},${y})" class="gnode ${cls}${node.missing ? " ghost" : ""} k-${esc(node.kind)}" tabindex="0" data-node="${esc(node.id)}">
      ${shape}<text x="${radius + 7}" y="4" paint-order="stroke fill" stroke="var(--map-surface-1)" stroke-width="3" stroke-linejoin="round">${esc(visibleLabels.get(String(node.id)) || node.id)}</text>
      <title>${esc(node.label || node.id)} (${esc(node.id)}) - ${esc(node.kind)}${node.status ? " - " + esc(node.status) : ""} - ${weight} relationship${weight === 1 ? "" : "s"}</title></g>`;
  }).join("");

  const counts = edges.reduce((acc, e) => ({ ...acc, [e.kind]: (acc[e.kind] || 0) + 1 }), {});
  const legend = Object.entries(EDGE_KINDS).filter(([kind]) => counts[kind]).map(([kind, meta]) =>
    `<span class="key"><svg width="26" height="8" aria-hidden="true"><path d="M1 4 H25" class="edge ${meta.cls}"></path></svg>${
      esc(meta.label)} <i>${counts[kind]}</i> <em>${esc(meta.note)}</em></span>`).join("");

  const bound = edges.filter(e => e.relationship_state === "source_bound").length;
  const markers = Object.values(EDGE_KINDS).map(m =>
    `<marker id="tip-${m.cls}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse" class="${m.cls}"><path d="M0 0 L10 5 L0 10 z"></path></marker>`).join("");

  const bandArt = (laid.bands || []).map(b =>
    `<g class="band"><rect x="8" y="${b.y}" width="${width - 24}" height="${b.height}" rx="8"></rect>` +
    `<text x="18" y="${b.y + 13}">${esc(b.name)} <tspan>${b.count}</tspan></text></g>`).join("");

  const layoutPicker = `<div class="layoutpick">${
    [["ranked", "Ranked", "ordered by depth"], ["clustered", "Clustered", "grouped by vertical"]]
      .map(([key, label, note]) => `<button data-layout="${key}"${
        depsLayout === key ? ' class="active"' : ""} title="${esc(note)}">${esc(label)}</button>`).join("")}</div>`;
  const scopeControls = selected
    ? coordNeighbourhoodControls({
        nodes: allNodes, edges: allEdges, rootId: selected, hops: depsHops, result: cut,
      }, esc)
    : depsStructuralControls(cut, focused);

  return `<div class="card"><h3>What waits on what</h3>${layoutPicker}${scopeControls}
    <p class="meta">${edges.length} visible edges, ${bound} bound to a recorded source field. Node size is how many visible relationships touch it; squares are jobs, circles are work. ${
      depsLayout === "clustered"
        ? "Bands are verticals, so an edge leaving a band is a dependency that crosses one; position within a band means nothing."
        : "Columns are depth from a root; position within a column means nothing."}</p>
    <p class="keys">${legend}<span class="key"><i class="swatch run"></i>running</span><span class="key"><i class="swatch stuck"></i>blocked</span><span class="key"><i class="swatch done"></i>done</span></p>
    <div class="gwrap"><svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Dependency graph">
      <defs>${markers}</defs>${bandArt}${paths}${marks}</svg></div></div>
    ${renderAnalysis()}`;
}


// ------------------------------------------------------------ graph analysis

function renderAnalysis() {
  const execution = operations?.execution;
  if (!execution) {
    return `<div class="card"><h3>Execution analysis unavailable</h3>
      <p class="meta">This bundle does not publish an operations.execution receipt.</p></div>`;
  }
  const rows = rowById();
  const line = (id, trailing) => {
    const row = rows.get(id);
    return `<button class="chip ${row ? esc(stateOf(row)) : ""}" data-row="${esc(id)}">${esc(id)}<span>${
      esc(row ? row.title : "not on this board")}${trailing ? ` — ${esc(trailing)}` : ""}</span></button>`;
  };
  const impactRows = Array.isArray(execution.impact) ? execution.impact : [];
  const order = Array.isArray(execution.critical_path) ? execution.critical_path : [];
  const status = String(execution.topology_metrics_status || "not recorded");
  const population = Number(execution.analysis_population_emitted || 0);
  const total = Number(execution.analysis_population_total || population);
  const scope = `operations.execution status ${status}; analyzed ${population} of ${total} work rows`;
  return `<div class="card"><h3>Recorded downstream reach</h3>
      <p class="meta">Server-derived rows reachable through recorded dependency order. This is structural reach, not a promise that finishing one row releases another. ${esc(scope)}.</p>
      <div class="chips">${impactRows.length
        ? impactRows.map(item => line(item.id, `${Number(item.downstream || 0)} downstream row${Number(item.downstream || 0) === 1 ? "" : "s"} of recorded order`)).join("")
        : `<p class="meta">No downstream reach rows are published for this execution scope.</p>`}</div></div>
    <div class="card"><h3>Recorded prerequisite order</h3>
      <p class="meta">${order.length} ordered step${order.length === 1 ? "" : "s"} published by operations.execution. Position reports prerequisite order; it does not measure elapsed time. ${esc(scope)}.</p>
      <div class="chips">${order.length
        ? order.map((id, index) => line(id, `recorded order ${index + 1}`)).join("")
        : `<p class="meta">No critical order is published for this execution scope.</p>`}</div></div>`;
}

// -------------------------------------------------------------- context view

// What the fleet has said about its own work. The knowledge store is a separate
// surface with no read endpoint on this server yet, so this shows the context
// the board itself carries and says plainly where the rest lives -- an empty
// panel implying there is nothing to read would be the wrong answer.
function renderContext() {
  const rows = snapshot.rows.filter(r => r.current_step);
  const byModule = new Map();
  rows.forEach(r => {
    const key = r.module || "unassigned";
    if (!byModule.has(key)) byModule.set(key, []);
    byModule.get(key).push(r);
  });

  const groups = [...byModule.entries()].sort((a, b) => b[1].length - a[1].length).map(([module, items]) =>
    `<div class="card ctx"><h3>${esc(module)}</h3><p class="meta">${items.length} rows carrying context</p>${
      items.map(r => `<article class="note" tabindex="0" data-row="${esc(r.id)}"><b>${esc(r.title)}</b><span class="meta">${esc(r.id)} - ${esc(r.owner || "unassigned")} - ${esc(r.status)}</span><p>${esc(r.current_step)}</p></article>`).join("")
    }</div>`).join("");

  return `<div class="card"><h3>Context on the board</h3>
    <p class="meta">Every row that records what its holder is doing, grouped by vertical. Use the global row search above to navigate by id, title, owner, or step.</p></div>
    <div id="ctxgroups" class="ctxgroups">${groups || `<div class="card"><p class="meta">No row currently records a step.</p></div>`}</div>
    <div class="card"><h3>Where the rest lives</h3><p class="meta">Durable notes, decisions and the full-text knowledge store are held outside this snapshot. They are deliberately not exposed here: this server is read-only and unauthenticated, and that material is not. Read it through the lifecycle tooling instead.</p></div>`;
}


// --------------------------------------------------------------- the drawer

// One place that answers "what is this row, and where do I go from here".
//
// Everything in it is a link. That is the whole point: the old context view
// listed rows and left you to scroll for whatever they mentioned, so following
// a dependency meant reading an id and searching for it by eye. A relationship
// you cannot click is a relationship you have to hold in your head.

const rowById = () => new Map((snapshot?.rows || []).map(r => [r.id, r]));
const contextById = () => new Map((context?.items || []).map(c => [c.id, c]));

function chipList(ids, label) {
  if (!ids || !ids.length) return "";
  const rows = rowById();
  return `<div class="rel"><p class="meta">${esc(label)}</p><div class="chips">${
    ids.map(id => {
      const row = rows.get(id);
      const state = row ? stateOf(row) : "";
      return `<button class="chip ${esc(state)}" data-row="${esc(id)}" title="${esc(row ? row.title : id)}">${
        esc(id)}${row ? `<span>${esc(row.title)}</span>` : `<span class="meta">not on this board</span>`}</button>`;
    }).join("")}</div></div>`;
}

function renderDetail(id) {
  const row = rowById().get(id);
  const ctx = contextById().get(id) || {};
  if (!row) {
    return `<header><p class="eyebrow">${esc(id)}</p><h2>Not on this board</h2></header>
      <p class="meta">This id is referenced by something here but is not itself a row the board holds.</p>`;
  }
  const laneMeta = LANES[lane(row.owner)] || {};
  const state = stateOf(row);
  const facts = [
    ["State", `<span class="badge ${esc(state)}">${esc(row.status)}</span>`],
    ["Owner", row.owner
      ? `${laneMeta.mark ? `<img class="mark" src="${laneMeta.mark}" alt="">` : ""}${esc(row.owner)}`
      : `<span class="meta">unassigned</span>`],
    ["Vertical", esc(row.module || "—")],
    ["Priority", row.priority ? `P${esc(row.priority)}` : "—"],
    ["Progress", row.progress_fraction != null ? `${Math.round(row.progress_fraction * 100)}%` : "—"],
    ["Proof", ctx.done_signal
      ? `<code>${esc(ctx.done_signal)}</code> ${ctx.artifact_recorded
          ? `<span class="ok">recorded</span>` : `<span class="meta">not yet recorded</span>`}`
      : `<span class="meta">no completion artifact declared</span>`],
  ];
  const why = ctx.blocked_reason_class
    ? `<div class="why"><p class="meta">Blocked — ${esc(ctx.blocked_reason_class)}</p>${
        ctx.resume_when ? `<p>${esc(ctx.resume_when)}</p>` : ""}</div>` : "";

  return `<header>
      <p class="eyebrow">${esc(row.id)}</p>
      <h2>${esc(row.title)}</h2>
      ${row.current_step ? `<p class="step">${esc(row.current_step)}</p>` : ""}
    </header>
    ${why}
    <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
    ${chipList(ctx.parent ? [ctx.parent] : [], "Belongs to")}
    ${chipList(ctx.children, "Contains")}
    ${chipList(ctx.depends_on, "Waits on")}
    ${chipList(ctx.dependents, "Waited on by")}
    ${chipList(ctx.siblings, "Alongside")}`;
}

function nodeRowId(nodeId) {
  const rows = rowById();
  if (rows.has(nodeId)) return nodeId;
  if (String(nodeId).startsWith("work:") && rows.has(String(nodeId).slice(5))) {
    return String(nodeId).slice(5);
  }
  if (String(nodeId).startsWith("job:") && rows.has(String(nodeId).slice(4))) {
    return String(nodeId).slice(4);
  }
  return String(nodeId).replace(/^work:/, "");
}

function bundledTimelineHtml(workId) {
  if (typeof tlIngest !== "function" || typeof tlBlock !== "function") {
    return `<div class="rel tlblock"><p class="meta">Timeline</p>
      <p class="tlgap">The bundled timeline renderer did not load.</p></div>`;
  }
  const state = tlIngest(timeline);
  if (!state.ok) return tlGap(state.gap, esc);
  const id = String(workId ?? "");
  const entry = state.byId.get(id);
  if (!entry) {
    return tlBlock(`<p class="tlgap">This row is not among the ${state.items} row${
      state.items === 1 ? "" : "s"} the bundled timeline carries.</p>`);
  }
  const events = tlSort(entry.events);
  if (!events.length) {
    return tlBlock(`<p class="tlgap">${entry.bad
      ? "The bundled timeline names this row, but its event list is unreadable."
      : "The bundled timeline carries this row with no events on it."}</p>`);
  }
  const count = `${events.length} event${events.length === 1 ? "" : "s"}`;
  const generated = state.generatedAt
    ? `<p class="tlfoot">Read from the same-bundle timeline generated ${tlTime(state.generatedAt, esc)}.</p>`
    : "";
  return tlBlock(`<ol class="tl" role="list">${events.map(event => tlRow(event, esc)).join("")}</ol>
    <p class="tlnote">${count} recorded for this row, in the order the board holds them.
    Markers are evenly spaced layout, not elapsed time.</p>${generated}`);
}

function openDrawer(drawer, trigger = document.activeElement) {
  const opening = !drawer.classList.contains("open");
  if (opening) {
    const element = trigger instanceof Element && trigger !== document.body ? trigger : null;
    let selector = "";
    if (element?.dataset.row) {
      selector = `[data-row="${CSS.escape(element.dataset.row)}"]`;
    } else if (element?.dataset.node) {
      selector = `[data-node="${CSS.escape(element.dataset.node)}"]`;
    } else if (element?.dataset.cell) {
      selector = `[data-cell="${CSS.escape(element.dataset.cell)}"]`;
    } else if (element?.id) {
      selector = `#${CSS.escape(element.id)}`;
    }
    lastDrawerTrigger = element ? { element, selector } : null;
  }
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  if (opening) drawer.querySelector(".close").focus();
}

function hashCapsule() {
  return new URLSearchParams((location.hash || "").replace(/^#/, ""));
}

function selectedFromHash() {
  return hashCapsule().get("sel") || "";
}

function urlWithSelection(id) {
  const url = new URL(location.href);
  const capsule = hashCapsule();
  if (id) capsule.set("sel", id); else capsule.delete("sel");
  const encoded = capsule.toString();
  url.hash = encoded ? `#${encoded}` : "";
  return `${url.pathname}${url.search}${url.hash}`;
}

function select(id, { push = true, redraw = true } = {}) {
  const trigger = document.activeElement;
  selected = id;
  if (redraw && graph && activeLensName() === "deps") renderLens("deps");
  const drawer = document.querySelector("#drawer");
  drawer.innerHTML = `<button class="close" aria-label="Close">&times;</button>${renderDetail(id)}
    ${bundledTimelineHtml(id)}`;
  openDrawer(drawer, trigger);
  // Addressable, so a row can be linked to and the back button works.
  const nextUrl = urlWithSelection(id);
  const currentUrl = `${location.pathname}${location.search}${location.hash}`;
  if (push && currentUrl !== nextUrl) history.pushState({ id }, "", nextUrl);
  document.querySelectorAll("[data-row],[data-node]").forEach(el => {
    const key = el.dataset.row || nodeRowId(el.dataset.node || "");
    el.classList.toggle("current", key === id);
  });
}

function closeDrawer({ push = true, redraw = true } = {}) {
  selected = null;
  if (redraw && graph && activeLensName() === "deps") renderLens("deps");
  const drawer = document.querySelector("#drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  drawer.innerHTML = "";
  document.querySelectorAll(".current").forEach(el => el.classList.remove("current"));
  if (push && selectedFromHash()) history.pushState({}, "", urlWithSelection(""));
  const restore = lastDrawerTrigger;
  lastDrawerTrigger = null;
  const target = restore?.element?.isConnected
    ? restore.element : restore?.selector ? document.querySelector(restore.selector) : null;
  if (target) {
    requestAnimationFrame(() => target.focus());
  }
}

// A cell counts rows; opening it should show those rows, not an arbitrary one.
function selectCell(owner, module) {
  const rows = (snapshot.rows || [])
    .filter(r => (r.owner || "\u2014 unowned") === owner && r.module === module);
  if (rows.length === 1) return select(rows[0].id);
  const drawer = document.querySelector("#drawer");
  drawer.innerHTML = `<button class="close" aria-label="Close">&times;</button>
    <header><p class="eyebrow">${esc(owner)} in ${esc(module)}</p><h2>${rows.length} rows</h2></header>
    ${chipList(rows.map(r => r.id), "Rows here")}`;
  openDrawer(drawer);
}

// ------------------------------------------------------------------- wiring

// Views built as separate modules register by exporting one global. Called
// through a guard so a module that failed to load leaves a stated gap rather
// than an empty panel that reads as "nothing to show".
function renderModule(name, fn, overrides = {}) {
  if (typeof fn !== "function") {
    return `<div class="card"><h3>View unavailable</h3><p class="meta">The ${esc(name)} module did not load.</p></div>`;
  }
  try {
    return fn({ snapshot, graph, context, timeline, operations, readStatus, pulse, esc, stateOf, lane, LANES, rowById, contextById, ...overrides });
  } catch (error) {
    return `<div class="card"><h3>View failed</h3><p class="meta">${esc(String(error))}</p></div>`;
  }
}

function projectionCount(name) {
  const item = bundleProjection?.[name];
  const published = Number(item?.published);
  const omitted = Number(item?.omitted);
  if (!Number.isFinite(published) || !Number.isFinite(omitted) || published < 0 || omitted < 0) return null;
  return { published, omitted, total: published + omitted };
}

function projectionReceiptHtml() {
  const compact = continuousCommsMode;
  const receiptClass = `projection-receipt${compact ? " projection-receipt-compact" : ""}`;
  if (!bundleProjection) {
    return `<details class="${receiptClass}" data-projection-state="unavailable"${compact ? "" : " open"}>
      <summary><b>${compact ? "Coverage unavailable" : "Projection receipt unavailable"}</b></summary>
      <div class="projection-detail">Population admission and omission are not stated.</div></details>`;
  }
  if (bundleProjection.applied !== true) {
    return `<details class="${receiptClass}" data-projection-state="unapplied"
      data-projection-capped="false"${compact ? "" : " open"}>
      <summary><b>${compact ? "Coverage not applied" : "Projection not applied"}</b></summary>
      <div class="projection-detail">${
        esc(bundleProjection.reason || "The bundle does not state why it is unprojected.")}</div></details>`;
  }
  const labels = [
    ["snapshot_rows", "snapshot rows"],
    ["graph_nodes", "graph nodes"],
    ["graph_edges", "graph edges"],
    ["context", "context rows"],
    ["timeline", "timeline rows"],
  ];
  const counts = labels.map(([key, label]) => [label, projectionCount(key)]).filter(([, value]) => value);
  const capped = counts.some(([, value]) => value.omitted > 0);
  const degraded = Boolean(bundleFailure || readStatus?.degraded === true);
  const rows = projectionCount("snapshot_rows");
  const edges = projectionCount("graph_edges");
  const detail = counts.map(([label, value]) => `${label}: ${value.published} published of ${value.total}`).join("; ");
  const summary = compact
    ? `Coverage ${capped ? "capped" : degraded ? "degraded" : "complete"}${
      rows ? ` · ${rows.published}/${rows.total} rows` : ""}${edges ? ` · ${edges.published}/${edges.total} edges` : ""}`
    : `Projection ${capped ? "capped" : degraded ? "degraded" : "complete"}${
      rows ? ` · ${rows.published} rows` : ""}${edges ? ` · ${edges.published} edges` : ""}`;
  return `<details class="${receiptClass}" data-projection-state="${
    capped ? "capped" : degraded ? "degraded" : "complete"}" data-projection-capped="${capped}"${
    !compact && (capped || degraded) ? " open" : ""}>
    <summary><b>${esc(summary)}</b><span class="projection-disclosure">${compact ? "Exact counts" : "Exact populations"}</span></summary>
    <div class="projection-detail">${esc(detail || "No population counts were published.")}. These counts scope the visible Map; larger snapshot summary totals do not label the projected matrix.</div></details>`;
}

function topologyUnavailable() {
  const receipt = operations?.topology_availability;
  if (!receipt || typeof receipt !== "object") return false;
  return receipt.state === "low_information" || Number(receipt.admitted?.edges) === 0;
}

function topologyExplanationHtml() {
  if (!topologyUnavailable()) return "";
  const receipt = operations.topology_availability;
  const source = receipt.source?.graph_declared || "the operations topology availability receipt";
  const reason = receipt.reason_code || "authoritative_relationships_absent";
  return `<p class="relationship-receipt"><b>Relationship lenses unavailable.</b> The authoritative ${
    esc(source)} topology admits 0 recorded edges (${esc(reason)}). No relationships are inferred from an empty authoritative relation set.</p>`;
}

function syncRelationshipTabs() {
  const unavailable = topologyUnavailable();
  document.querySelectorAll("#maptabs [data-tab]").forEach(control => {
    if (!RELATIONSHIP_LENSES.has(control.dataset.tab)) return;
    control.disabled = unavailable;
    control.setAttribute("aria-disabled", String(unavailable));
    control.title = unavailable ? "Unavailable: the authoritative topology admits no recorded edges" : "";
  });
  if (unavailable && RELATIONSHIP_LENSES.has(activeLensName())) {
    activateLens("fleet", { persist: true });
  }
}

function pulseReceiptHtml() {
  const generated = pulse?.generated_at ? new Date(pulse.generated_at).toLocaleTimeString() : "";
  const state = pulseFailure
    ? (pulseRetained ? `failed; retaining the last-good pulse (${pulseFailure})` : `unavailable (${pulseFailure})`)
    : pulse ? `last read ${generated || "with no recorded generation time"}` : "waiting for its first read";
  const authority = pulseCoherent
    ? "Coherent Pulse receipt. Pulse and topology share one cache generation"
    : "Compatibility Pulse receipt. Pulse refreshed independently from topology";
  return `<p class="pulse-refresh-receipt"><b>${authority}.</b> ${esc(state)}.</p>`;
}

function pulseDocumentForView() {
  const events = Number(pulse?.counts?.events);
  const pulseRows = Number(pulse?.counts?.rows);
  if (!pulse || events !== 0 || pulseRows !== 0 || !(snapshot?.rows || []).length) return pulse;
  const counts = { ...pulse.counts };
  delete counts.rows;
  return { ...pulse, counts };
}

function pulsePopulationReceiptHtml() {
  const events = Number(pulse?.counts?.events);
  if (events !== 0 || !(snapshot?.rows || []).length) return "";
  const projected = projectionCount("snapshot_rows");
  const rows = projected?.published ?? snapshot.rows.length;
  const total = projected?.total ?? rows;
  return `<p class="pulse-zero-receipt"><b>Zero pulse occurrences, not zero rows.</b> The coherent snapshot bundle publishes ${
    rows} of ${total} rows; the independently refreshed pulse records zero coordination occurrences for its poll.</p>`;
}

function paintPulse() {
  if (!snapshot || activeLensName() !== "pulse") return;
  renderLens("pulse");
}

function paintReadState() {
  const alert = document.querySelector("#mapreadalert");
  const serverDegraded = readStatus?.degraded === true;
  const details = [];
  if (bundleFailure) {
    details.push(snapshot
      ? `operations bundle refresh failed; last-good rows retained (${bundleFailure})`
      : `operations bundle unavailable (${bundleFailure})`);
  }
  if (serverDegraded) {
    details.push(`server cache degraded after ${Number(readStatus.consecutive_refresh_failures || 0)} refresh failure${
      Number(readStatus.consecutive_refresh_failures || 0) === 1 ? "" : "s"}; class ${
      readStatus.last_failure_class || "not recorded"}`);
  }
  alert.hidden = !details.length;
  alert.textContent = details.length
    ? `DEGRADED READ: ${details.join(". ")}. This surface does not claim complete live state.`
    : "";
  if (!snapshot) {
    document.querySelector("#mapmeta").textContent = bundleFailure
      ? `Cannot read the operations bundle: ${bundleFailure}`
      : "Reading the operations bundle…";
    return;
  }
  const mode = bundleFailure || serverDegraded ? "DEGRADED" : "LIVE";
  const generation = readStatus?.cache_generation == null ? "" : ` - cache generation ${readStatus.cache_generation}`;
  const projected = projectionCount("snapshot_rows");
  const population = projected
    ? `${projected.published} of ${projected.total} rows published`
    : `${snapshot.rows.length} published rows`;
  const running = snapshot.rows.filter(row => stateOf(row) === "running").length;
  document.querySelector("#mapmeta").textContent =
    `${mode} - ${running} recorded running in the published rows - ${population}${generation} - generated ${
      new Date(snapshot.generated_at).toLocaleTimeString()}${snapshot.stale ? " - stale" : ""}`;
}

function lensControls() {
  return [...document.querySelectorAll("#maptabs [data-tab]")];
}

function activeLensName() {
  return lensControls().find(control => control.classList.contains("active"))?.dataset.tab || "fleet";
}

function closeMoreMenu({ focus = false } = {}) {
  const trigger = document.querySelector("#map-more-trigger");
  const menu = document.querySelector("#map-more-menu");
  if (!trigger || !menu) return;
  menu.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  menu.querySelectorAll('[role="menuitemradio"]').forEach(item => { item.tabIndex = -1; });
  if (focus) trigger.focus();
}

function openMoreMenu() {
  const trigger = document.querySelector("#map-more-trigger");
  const menu = document.querySelector("#map-more-menu");
  if (!trigger || !menu) return;
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  const items = [...menu.querySelectorAll('[role="menuitemradio"]')].filter(item => !item.disabled);
  const target = items.find(item => item.getAttribute("aria-checked") === "true") || items[0];
  if (target) {
    target.tabIndex = 0;
    target.focus();
  }
}

function lensFromLocation(params = new URLSearchParams(location.search)) {
  const capsule = hashCapsule();
  const rawHash = (location.hash || "").replace(/^#/, "");
  const bareHashLens = rawHash && !rawHash.includes("=") ? rawHash : "";
  return params.get("lens") || capsule.get("lens") || bareHashLens || "fleet";
}

function activateLens(name, { focus = false, persist = true } = {}) {
  const controls = lensControls();
  const requested = controls.find(candidate => candidate.dataset.tab === name);
  const control = requested && !requested.disabled
    ? requested
    : controls.find(candidate => !candidate.disabled);
  if (!control) return;

  const selectedName = control.dataset.tab;
  const overflowActive = MORE_LENSES.has(selectedName);
  controls.forEach(candidate => {
    const active = candidate === control;
    candidate.classList.toggle("active", active);
    if (candidate.getAttribute("role") === "tab") {
      candidate.setAttribute("aria-selected", String(active));
      candidate.tabIndex = active ? 0 : -1;
    } else {
      candidate.setAttribute("aria-checked", String(active));
      candidate.tabIndex = -1;
    }
    const panel = document.getElementById(candidate.getAttribute("aria-controls"));
    if (panel) {
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    }
  });

  const more = document.querySelector("#map-more-trigger");
  const moreLabel = more?.querySelector(".map-more-label");
  if (more) {
    const label = overflowActive ? control.textContent.trim() : "";
    more.classList.toggle("active", overflowActive);
    more.setAttribute("aria-selected", String(overflowActive));
    more.tabIndex = overflowActive ? 0 : -1;
    more.dataset.currentLens = overflowActive ? selectedName : "";
    more.setAttribute(
      "aria-label",
      overflowActive ? `More Map views, current: ${label}` : "More Map views",
    );
    if (moreLabel) moreLabel.textContent = overflowActive ? `More · ${label}` : "More";
  }
  closeMoreMenu();

  if (persist) {
    const url = new URL(location.href);
    url.searchParams.set("lens", selectedName);
    history.replaceState(history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }
  if (focus) (overflowActive ? more : control)?.focus();
  if (snapshot) renderLens(selectedName);
}

function wireTabs() {
  const tabs = document.querySelector("#maptabs");
  const more = document.querySelector("#map-more-trigger");
  const menu = document.querySelector("#map-more-menu");

  tabs.addEventListener("click", event => {
    const item = event.target.closest("[data-tab]");
    if (item && !item.disabled) {
      activateLens(item.dataset.tab, { focus: item.getAttribute("role") === "menuitemradio" });
      return;
    }
    if (event.target.closest("#map-more-trigger")) {
      if (menu.hidden) openMoreMenu(); else closeMoreMenu({ focus: true });
    }
  });

  tabs.addEventListener("keydown", event => {
    const item = event.target.closest('[role="menuitemradio"]');
    if (item) {
      const enabled = [...menu.querySelectorAll('[role="menuitemradio"]')]
        .filter(candidate => !candidate.disabled);
      const index = enabled.indexOf(item);
      let next = null;
      if (event.key === "ArrowDown" || event.key === "ArrowRight") next = enabled[(index + 1) % enabled.length];
      if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = enabled[(index - 1 + enabled.length) % enabled.length];
      if (event.key === "Home") next = enabled[0];
      if (event.key === "End") next = enabled[enabled.length - 1];
      if (event.key === "Escape") {
        event.preventDefault();
        closeMoreMenu({ focus: true });
        return;
      }
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activateLens(item.dataset.tab, { focus: true });
        return;
      }
      if (next) {
        event.preventDefault();
        item.tabIndex = -1;
        next.tabIndex = 0;
        next.focus();
      }
      return;
    }

    const current = event.target.closest('[role="tab"]');
    if (!current) return;
    if (current === more && ["Enter", " ", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      openMoreMenu();
      return;
    }
    const primary = [...tabs.querySelectorAll('[role="tab"]')].filter(tab => !tab.disabled);
    const index = primary.indexOf(current);
    let next = null;
    if (event.key === "ArrowRight") next = primary[(index + 1) % primary.length];
    if (event.key === "ArrowLeft") next = primary[(index - 1 + primary.length) % primary.length];
    if (event.key === "Home") next = primary[0];
    if (event.key === "End") next = primary[primary.length - 1];
    if (!next) return;
    event.preventDefault();
    if (next.dataset.tab) activateLens(next.dataset.tab, { focus: true });
    else next.focus();
  });

  document.addEventListener("click", event => {
    if (!event.target.closest(".map-more")) closeMoreMenu();
  });
}

function renderLens(name) {
  if (!snapshot) return;
  const panel = document.getElementById(name);
  if (!panel) return;
  let body = "";
  if (RELATIONSHIP_LENSES.has(name) && topologyUnavailable()) {
    body = "";
  } else {
    const modules = {
      flowpath: ["Stations", typeof view_flowpath === "function" ? view_flowpath : null],
      ceiling: ["Ceiling", typeof view_ceiling === "function" ? view_ceiling : null],
      topology: ["Lanes", typeof view_topology === "function" ? view_topology : null],
      shape: ["Shape", typeof view_density === "function" ? view_density : null],
      chronicle: ["Order", typeof view_chronicle === "function" ? view_chronicle : null],
      subjects: ["Subjects", typeof view_subjects === "function" ? view_subjects : null],
      orbit: ["Orbit", typeof view_orbit === "function" ? view_orbit : null],
      crossings: ["Crossings", typeof view_flow === "function" ? view_flow : null],
    };
    if (name === "fleet") body = renderFleet();
    else if (name === "pulse") body = pulseReceiptHtml() + pulsePopulationReceiptHtml() +
      renderModule("Pulse", typeof view_pulse === "function" ? view_pulse : null, { pulse: pulseDocumentForView() });
    else if (name === "deps") body = renderDeps();
    else if (name === "context") body = renderContext();
    else if (modules[name]) body = renderModule(...modules[name]);
  }
  const leadingReceipts = continuousCommsMode && name === "fleet"
    ? "" : projectionReceiptHtml() + topologyExplanationHtml();
  panel.innerHTML = leadingReceipts + body;

  panel.querySelectorAll("[data-w]").forEach(el => {
    const width = Number(el.dataset.w);
    if (Number.isFinite(width)) el.style.width = `${Math.max(0, Math.min(100, width))}%`;
  });
  if (globalThis.coordMotion) coordMotion.afterPaint(panel);
}

function publishContinuousCommsHeight() {
  if (!continuousCommsMode || parent === window) return;
  cancelAnimationFrame(continuousHeightFrame);
  continuousHeightFrame = requestAnimationFrame(() => {
    const height = Math.ceil(document.documentElement.scrollHeight);
    if (!height || Math.abs(height - continuousPublishedHeight) < 2) return;
    continuousPublishedHeight = height;
    parent.postMessage({
      type: "coord.continuous-comms.height",
      height,
    }, location.origin);
  });
}

function paint() {
  syncRelationshipTabs();
  if (continuousCommsMode) {
    const continuousPanels = continuousCommsSection ? [continuousCommsSection] : ["fleet", "pulse"];
    for (const name of continuousPanels) {
      const panel = document.getElementById(name);
      if (!panel) continue;
      panel.hidden = false;
      panel.classList.add("active");
      renderLens(name);
    }
  } else {
    renderLens(activeLensName());
  }
  if (searchController) searchController.refresh();
  if (selected) select(selected, { push: false, redraw: false });

  paintReadState();
  publishContinuousCommsHeight();
}

async function fetchDocument(url, options = {}) {
  const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", ...options });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const value = await response.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("non-document JSON");
  }
  return value;
}

async function refreshCore() {
  const ticket = ++coreRequestTicket;
  if (coreController) coreController.abort();
  const controller = new AbortController();
  coreController = controller;
  try {
    let bundle;
    try {
      bundle = await fetchDocument("/api/v2/operations-bundle", { signal: controller.signal });
    } catch (error) {
      if (!String(error?.message || "").includes("HTTP 404")) throw error;
      bundle = await fetchDocument("/api/v1/operations-bundle", { signal: controller.signal });
    }
    if (!["OpsAtlasBundleV1", "OpsAtlasBundleV2"].includes(bundle.schema_version)) throw new Error("unexpected bundle schema");
    ["snapshot", "graph", "context", "timeline", "operations", "read_status", "bundle_projection"].forEach(name => {
      if (!bundle[name] || typeof bundle[name] !== "object" || Array.isArray(bundle[name])) {
        throw new Error(`bundle missing ${name}`);
      }
    });
    if (Number(bundle.cache_generation) !== Number(bundle.read_status.cache_generation)) {
      throw new Error("bundle/read-status generation mismatch");
    }
    if (ticket !== coreRequestTicket) return;
    snapshot = bundle.snapshot;
    graph = bundle.graph;
    context = bundle.context;
    timeline = bundle.timeline;
    operations = bundle.operations;
    bundleProjection = bundle.bundle_projection;
    pulseCoherent = bundle.schema_version === "OpsAtlasBundleV2";
    if (pulseCoherent) {
      if (!bundle.pulse || typeof bundle.pulse !== "object" || Array.isArray(bundle.pulse)) throw new Error("V2 bundle missing pulse");
      pulse = bundle.pulse;
      pulseFailure = "";
      pulseRetained = false;
    }
    readStatus = bundle.read_status;
    bundleFailure = "";
    paint();
  } catch (error) {
    if (ticket !== coreRequestTicket || error?.name === "AbortError") return;
    bundleFailure = String(error?.message || error || "request failed");
    if (snapshot) renderLens(activeLensName());
    paintReadState();
  } finally {
    if (ticket === coreRequestTicket) coreController = null;
  }
}

async function refreshPulse() {
  try {
    const nextPulse = await fetchDocument("/api/v1/pulse");
    pulse = nextPulse;
    pulseFailure = "";
    pulseRetained = false;
  } catch (error) {
    pulseFailure = String(error?.message || error || "request failed");
    pulseRetained = Boolean(pulse);
  }
  paintPulse();
}

async function refresh() {
  await refreshCore();
  if (pulseCoherent) paintPulse();
  else await refreshPulse();
}

function wireNavigation() {
  // Delegated, because every view re-renders on refresh: handlers bound to
  // elements would be lost every five seconds.
  document.addEventListener("click", event => {
    if (event.target.closest("#drawer .close")) return closeDrawer();
    const chip = event.target.closest("#drawer [data-row]");
    if (chip) return select(chip.dataset.row);
    const node = event.target.closest("[data-node]");
    if (node) return select(nodeRowId(node.dataset.node));
    const rowEl = event.target.closest("[data-row]");
    if (rowEl) return select(rowEl.dataset.row);
    const pick = event.target.closest("[data-layout]");
    if (pick) {
      depsLayout = pick.dataset.layout;
      renderLens("deps");
      return;
    }
    const structuralScope = event.target.closest("[data-deps-scope]");
    if (structuralScope) {
      depsStructuralMode = structuralScope.dataset.depsScope === "admitted" ? "admitted" : "focus";
      renderLens("deps");
      return;
    }
    const hop = event.target.closest("[data-hops]");
    if (hop) {
      depsHops = hop.dataset.hops === "all" ? "all" : Number(hop.dataset.hops);
      renderLens("deps");
      return;
    }
    const cell = event.target.closest("[data-cell]");
    if (cell) {
      const [owner, module] = cell.dataset.cell.split("|");
      return selectCell(owner, module);
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && document.querySelector("#drawer").classList.contains("open")) {
      return closeDrawer();
    }
    if (event.key === "Enter" || event.key === " ") {
      const target = event.target.closest("[data-row],[data-node],[data-cell]");
      if (target) { event.preventDefault(); target.click(); }
    }
    const editing = event.target.matches("input,textarea,select,[contenteditable='true']");
    if (event.key === "/" && !editing && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      if (searchController) searchController.focus();
    }
  });

  addEventListener("popstate", () => {
    const lens = lensFromLocation();
    if (lens !== activeLensName()) activateLens(lens, { persist: false });
    const id = selectedFromHash();
    if (id) select(id, { push: false }); else closeDrawer({ push: false });
  });
}

function start() {
  // The native cockpit supplies its own window chrome, so the page drops its
  // masthead when embedded rather than stacking two headers.
  const params = new URLSearchParams(location.search);
  continuousCommsMode = params.get("continuous") === "1";
  continuousCommsSection = continuousCommsMode && ["fleet", "pulse"].includes(params.get("section"))
    ? params.get("section") : "";
  if (params.get("native_map") === "1" || params.get("embedded") === "1" || continuousCommsMode) {
    document.documentElement.setAttribute("data-embedded", "1");
  }
  if (continuousCommsMode) {
    document.body.classList.add("continuous-comms");
    document.querySelector("#maptabs")?.setAttribute("hidden", "");
    if (continuousCommsSection) {
      document.body.classList.add(`continuous-section-${continuousCommsSection}`);
    }
    for (const panel of document.querySelectorAll("main > .panel")) {
      const visible = continuousCommsSection ? panel.id === continuousCommsSection
        : panel.id === "fleet" || panel.id === "pulse";
      panel.hidden = !visible;
      panel.classList.toggle("active", visible);
    }
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(publishContinuousCommsHeight).observe(document.body);
    }
    addEventListener("load", publishContinuousCommsHeight, { once: true });
  } else {
    wireTabs();
    activateLens(lensFromLocation(params), { persist: false });
  }
  if (typeof coordSearchMount === "function") {
    searchController = coordSearchMount(document.querySelector("#global-search"), {
      rows: () => snapshot?.rows || [], select,
    });
  } else {
    document.querySelector("#global-search").innerHTML =
      '<p class="meta">Global row search did not load.</p>';
  }
  wireNavigation();
  addEventListener("resize", () => {
    cancelAnimationFrame(orbitResizeFrame);
    orbitResizeFrame = requestAnimationFrame(() => {
      if (snapshot && activeLensName() === "orbit") renderLens("orbit");
    });
  });
  refresh().then(() => {
    // A shared link opens on the row it names.
    const id = selectedFromHash();
    if (id && snapshot) select(id, { push: false });
  });
  setInterval(refresh, 5000);
}

if (typeof document !== "undefined" && typeof fetch === "function") start();
