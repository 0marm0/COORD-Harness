// Graph neighbourhood — the "just show me around this row" filter.
//
// The dependency graph is readable at twenty nodes and unreadable at two
// hundred. Cutting it to the rows within N relationships of the one you
// selected is the fix, and hiding is the danger: a filtered graph looks exactly
// like a small graph, so a cut that does not say what it removed turns a
// display problem into a false fact. Every function here therefore reports what
// it hid, and the controls carry a plain-text count line that is not optional.
//
// Two rules follow from that and are load-bearing:
//
//   1. Traversal is UNDIRECTED. A row you wait on and a row that waits on you
//      are both in your neighbourhood — direction is a property of the edge,
//      not of relevance — and containment (`parent`) and ordering
//      (`depends_on`) are walked alike. Every edge kind is walked; this module
//      does not know which kinds exist and does not need to.
//
//   2. Failure is OPEN. A root that is not on the graph, or a hop count outside
//      the contract, returns the WHOLE graph and says so. An empty graph reads
//      as "this board has no relationships"; the truth in that case is "the
//      root was bad", and those two must never look the same.
//
// DOM-free and listener-free by construction: pure functions in, a string out.
// cockpit.js owns the element, the event wiring and the state. Nothing here
// writes a style attribute or a colour — there is no styling in this file at
// all, only class names for the served stylesheet to reach, which is what the
// board's `default-src 'self'` CSP requires (an injected sheet is dropped
// silently, so it is never attempted). No animation is introduced, so there is
// nothing for a reduced-motion preference to switch off.

// ---------------------------------------------------------------- escaping

// The caller passes the board's own esc(). This local copy is a backstop, not
// an alternative: if a caller forgets the argument the output must still be
// escaped rather than becoming an injection point. It is byte-identical to the
// helper in cockpit.js.
const NEIGHBOURHOOD_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const neighbourhoodEscape = value =>
  String(value ?? "").replace(/[&<>"']/g, c => NEIGHBOURHOOD_ESCAPES[c]);

// ------------------------------------------------------------- root lookup

// A snapshot row is `UI-101` while its graph node is `work:UI-101`, and a job
// row is `job:x` in both. Resolving the namespaced forms is what makes a
// selection from the board line up with a node in the graph; without it every
// selection would resolve to "not found" and quietly show the full graph, which
// is the failure this module is otherwise built to make visible.
const NEIGHBOURHOOD_ID_PREFIXES = ["work:", "job:"];

function coordNeighbourhoodResolveRoot(ids, rootId) {
  const raw = typeof rootId === "string" ? rootId.trim() : "";
  if (!raw) return null;
  if (ids.has(raw)) return raw;
  for (const prefix of NEIGHBOURHOOD_ID_PREFIXES) {
    if (ids.has(prefix + raw)) return prefix + raw;
  }
  return null;
}

// ------------------------------------------------------------- the cut

// coordNeighbourhood(nodes, edges, rootId, hops)
//   -> { nodes, edges, hiddenNodes, hiddenEdges, root, rootFound, hops,
//        totalNodes, totalEdges }
//
// The first four keys are the contract; the rest are the evidence the controls
// need in order to describe the cut without recomputing it. Input arrays are
// never mutated and node/edge order is preserved, so the layout a caller draws
// from a cut is stable against the layout it draws from the full graph.
//
// hops is a non-negative integer. Zero is the selected row alone. Anything
// else — a negative, a fraction, NaN, "all", undefined — means "no cut": the whole graph comes back with
// hiddenNodes = 0. That is deliberate. Showing too much is a readability cost;
// showing too little is a wrong answer.
function coordNeighbourhood(nodes, edges, rootId, hops) {
  const allNodes = Array.isArray(nodes) ? nodes.slice() : [];
  const allEdges = Array.isArray(edges) ? edges.slice() : [];

  const uncut = (root, rootFound) => ({
    nodes: allNodes,
    edges: allEdges,
    hiddenNodes: 0,
    hiddenEdges: 0,
    root,
    rootFound,
    hops: null,
    totalNodes: allNodes.length,
    totalEdges: allEdges.length,
  });

  const ids = new Set(allNodes.map(node => String(node && node.id)));
  const root = coordNeighbourhoodResolveRoot(ids, rootId);
  // No usable root: the full graph, flagged so the caller can say why.
  if (root === null) return uncut(null, false);

  const limit = Number(hops);
  if (!Number.isInteger(limit) || limit < 0) return uncut(root, true);

  // Adjacency is built only between ids that exist as nodes, so traversal can
  // never step onto a phantom id an edge happens to name. Sets absorb duplicate
  // edges; a self-edge lands the node in its own adjacency and is then dropped
  // by the visited check, so neither can loop.
  // `node && node.id` mirrors the id set above rather than reaching into
  // `node.id` directly: a null member in the array must not throw. This runs on
  // a render path, and a throw there blanks the panel — strictly worse than the
  // fail-open the rest of this module is built around.
  const adjacency = new Map(allNodes.map(node => [String(node && node.id), new Set()]));
  allEdges.forEach(edge => {
    if (!edge) return;
    const source = String(edge.source);
    const target = String(edge.target);
    if (!adjacency.has(source) || !adjacency.has(target)) return;
    adjacency.get(source).add(target);
    adjacency.get(target).add(source);
  });

  // Breadth-first to a fixed depth. The visited set both dedupes and terminates
  // the walk: a cycle re-reaches a node that is already visible and stops there,
  // so a cyclic board finishes in one pass over each edge rather than spinning.
  const visible = new Set([root]);
  let frontier = [root];
  for (let depth = 0; depth < limit && frontier.length; depth += 1) {
    const next = [];
    frontier.forEach(id => {
      const neighbours = adjacency.get(id);
      if (!neighbours) return;
      neighbours.forEach(neighbour => {
        if (visible.has(neighbour)) return;
        visible.add(neighbour);
        next.push(neighbour);
      });
    });
    frontier = next;
  }

  // An edge is kept only when both ends are drawn. A half-drawn edge would
  // point at nothing and read as a relationship to somewhere off-screen, which
  // is a different claim from the one the data makes.
  const keptNodes = allNodes.filter(node => visible.has(String(node && node.id)));
  const keptEdges = allEdges.filter(edge =>
    edge && visible.has(String(edge.source)) && visible.has(String(edge.target)));

  return {
    nodes: keptNodes,
    edges: keptEdges,
    hiddenNodes: allNodes.length - keptNodes.length,
    hiddenEdges: allEdges.length - keptEdges.length,
    root,
    rootFound: true,
    hops: limit,
    totalNodes: allNodes.length,
    totalEdges: allEdges.length,
  };
}

// ------------------------------------------------------------- the controls

const NEIGHBOURHOOD_HOPS = [0, 1, 2, 3];

// coordNeighbourhoodControls(state, esc) -> HTML string
//
// state = {
//   nodes, edges   full graph, used to compute the cut when `result` is absent
//   rootId         the row or node id in focus, or null for no selection
//   hops           1 | 2 | 3 | "all" | null   — the CURRENT setting
//   result         optional, a coordNeighbourhood(...) return to reuse
//   rootLabel      optional human label for the root chip
// }
//
// The count line is required output, not a decoration. A neighbourhood view
// without it is a graph that has silently dropped rows, and a reader has no way
// to tell that from a board that never had them.
function coordNeighbourhoodControls(state, esc) {
  const safe = typeof esc === "function" ? esc : neighbourhoodEscape;
  const input = (state && typeof state === "object") ? state : {};

  const cut = (input.result && typeof input.result === "object")
    ? input.result
    : coordNeighbourhood(input.nodes, input.edges, input.rootId, input.hops);

  // Derived from the cut itself rather than from the caller's counts, so the
  // sentence can never disagree with the graph beside it.
  const shown = Array.isArray(cut.nodes) ? cut.nodes.length : 0;
  const hidden = Number(cut.hiddenNodes) || 0;
  const total = shown + hidden;
  const shownEdges = Array.isArray(cut.edges) ? cut.edges.length : 0;
  const hiddenEdges = Number(cut.hiddenEdges) || 0;
  const totalEdges = shownEdges + hiddenEdges;

  const rootFound = cut.rootFound === true;
  const asked = typeof input.rootId === "string" ? input.rootId.trim() : "";

  // "all" is the selected setting whenever no cut is in force, so the control
  // group always shows exactly one current state.
  const current = Number.isInteger(cut.hops) && cut.hops >= 0 ? cut.hops : "all";

  const hopButton = value => {
    const active = current === value;
    const label = value === "all" ? "All" : String(value);
    const note = value === "all"
      ? "show every row on the graph"
      : `rows within ${value} relationship${value === 1 ? "" : "s"} of the selected row`;
    return `<button type="button" class="nb-hop${active ? " active" : ""}" data-hops="${
      safe(value)}" aria-pressed="${active ? "true" : "false"}"${
      rootFound ? "" : " disabled aria-disabled=\"true\""} title="${safe(note)}">${safe(label)}</button>`;
  };

  // A cut in force at a hop count the standard group cannot express — restored
  // state, a deep link, a caller with its own picker — would otherwise leave
  // every button unpressed: the graph is filtered, no control says which setting
  // filtered it, and no click returns to it once you leave. The in-force value
  // therefore joins the group, which is what makes "exactly one current state"
  // true rather than merely intended.
  const offered = (current === "all" || NEIGHBOURHOOD_HOPS.includes(current))
    ? NEIGHBOURHOOD_HOPS
    : [...NEIGHBOURHOOD_HOPS, current].sort((a, b) => a - b);
  const buttons = [...offered.map(hopButton), hopButton("all")].join("");

  // The root names a row, so it carries data-row and an explicit tabindex and
  // opens the same drawer every other row-naming element opens.
  const rootId = rootFound ? String(cut.root) : asked;
  const rootChip = rootId
    ? `<button type="button" class="chip nb-root" data-row="${safe(rootId)}" tabindex="0" title="${
      safe(`Open ${rootId}`)}">${safe(input.rootLabel || rootId)}</button>`
    : "";

  // Required, plain text, exact shape. N of M and what that leaves out.
  const count = `<p class="meta nb-count">showing ${safe(shown)} of ${safe(total)} nodes (${
    safe(hidden)} hidden)</p>`;
  const edgeCount = `<p class="meta nb-count-edges">showing ${safe(shownEdges)} of ${
    safe(totalEdges)} relationships (${safe(hiddenEdges)} hidden)</p>`;

  // Why nothing is hidden, when nothing is. Both cases are states a reader can
  // otherwise mistake for "this board is small".
  const note = rootFound
    ? (current === "all"
      ? `<p class="meta nb-note">No cut in force — every row on the graph is drawn.</p>`
      : "")
    : (asked
      ? `<p class="meta nb-note">${safe(asked)} is not a node on this graph, so nothing is filtered and the whole graph is drawn.</p>`
      : `<p class="meta nb-note">No row selected, so the whole graph is drawn. Select a row to cut the graph to its neighbourhood.</p>`);

  return `<div class="hop-control nb-control" role="group" aria-label="Graph neighbourhood filter">` +
    `<span class="meta">Neighbourhood</span>${buttons}${rootChip}</div>` +
    `${count}${edgeCount}${note}`;
}

// ------------------------------------------------------------- self-test
//
// Fixture-driven and dependency-free: `node -e` loads this file and calls
// coordNeighbourhoodSelfTest(). It runs nowhere else — the browser has no
// `module`, so the export guard below is inert there and this function is never
// reached. Kept in this file because the module is one file by contract.
function coordNeighbourhoodSelfTest() {
  const failures = [];
  const check = (name, condition, detail) => {
    if (!condition) failures.push(detail ? `${name}: ${detail}` : name);
  };

  // A ring A -> B -> C -> A (a cycle), a spur D off B, a second spur E off D,
  // an isolated island F-G, a self-edge on A, and a duplicate of A -> B.
  const graph = {
    nodes: ["A", "B", "C", "D", "E", "F", "G"].map(id => ({ id: `work:${id}`, kind: "work", label: id })),
    edges: [
      { id: "e1", source: "work:A", target: "work:B", kind: "depends_on" },
      { id: "e2", source: "work:B", target: "work:C", kind: "depends_on" },
      { id: "e3", source: "work:C", target: "work:A", kind: "parent" },
      { id: "e4", source: "work:B", target: "work:D", kind: "parent" },
      { id: "e5", source: "work:D", target: "work:E", kind: "depends_on" },
      { id: "e6", source: "work:F", target: "work:G", kind: "depends_on" },
      { id: "e7", source: "work:A", target: "work:A", kind: "depends_on" },
      { id: "e8", source: "work:A", target: "work:B", kind: "parent" },
      { id: "e9", source: "work:A", target: "work:GHOST", kind: "depends_on" },
    ],
  };
  const ids = cut => cut.nodes.map(n => n.id).sort().join(",");
  const frozen = JSON.stringify(graph);

  // --- undirected traversal across both edge kinds -------------------------
  // C -> A is an incoming `parent` edge and A -> B an outgoing `depends_on`;
  // one hop from A must hold both, or "neighbourhood" means "downstream".
  const one = coordNeighbourhood(graph.nodes, graph.edges, "work:A", 1);
  check("hops=1 is undirected across kinds", ids(one) === "work:A,work:B,work:C", ids(one));
  check("hops=1 hidden count", one.hiddenNodes === 4, String(one.hiddenNodes));
  check("hops=1 keeps only edges with both ends drawn",
    one.edges.every(e => e.target !== "work:D" && e.target !== "work:GHOST"),
    JSON.stringify(one.edges.map(e => e.id)));
  check("hops=1 keeps the self-edge", one.edges.some(e => e.id === "e7"));
  check("hops=1 keeps duplicate edges as distinct records",
    one.edges.filter(e => e.source === "work:A" && e.target === "work:B").length === 2);
  check("hops=1 edge set is exactly the ring, the self-edge and the duplicate",
    one.edges.map(e => e.id).join(",") === "e1,e2,e3,e7,e8", one.edges.map(e => e.id).join(","));
  check("hops=1 edge hidden count", one.hiddenEdges === graph.edges.length - one.edges.length);
  check("hops=1 drops the edge whose target is not a node", !one.edges.some(e => e.id === "e9"));

  // --- monotonicity: more hops never means fewer nodes ---------------------
  ["work:A", "work:B", "work:E", "work:F"].forEach(root => {
    let previous = null;
    [1, 2, 3, 4, 25].forEach(hops => {
      const cut = coordNeighbourhood(graph.nodes, graph.edges, root, hops);
      if (previous) {
        check(`monotonic nodes ${root} at ${hops}`, cut.nodes.length >= previous.nodes.length,
          `${previous.nodes.length} -> ${cut.nodes.length}`);
        check(`monotonic edges ${root} at ${hops}`, cut.edges.length >= previous.edges.length);
        const before = new Set(previous.nodes.map(n => n.id));
        check(`superset ${root} at ${hops}`, [...before].every(id => cut.nodes.some(n => n.id === id)));
      }
      previous = cut;
    });
  });
  const two = coordNeighbourhood(graph.nodes, graph.edges, "work:A", 2);
  check("hops=2 reaches D", ids(two) === "work:A,work:B,work:C,work:D", ids(two));
  check("hops 1 < 2 strictly here", two.nodes.length > one.nodes.length);

  // --- cycles terminate, and never drag in a disconnected island ----------
  const deep = coordNeighbourhood(graph.nodes, graph.edges, "work:A", 9999);
  check("cycle terminates at the component boundary",
    ids(deep) === "work:A,work:B,work:C,work:D,work:E", ids(deep));
  check("island stays hidden", deep.hiddenNodes === 2, String(deep.hiddenNodes));
  const island = coordNeighbourhood(graph.nodes, graph.edges, "work:F", 50);
  check("island resolves to itself only", ids(island) === "work:F,work:G", ids(island));

  // --- a bad root shows everything and says so, never an empty graph ------
  const missing = coordNeighbourhood(graph.nodes, graph.edges, "work:NOPE", 1);
  check("missing root keeps every node", missing.nodes.length === graph.nodes.length);
  check("missing root keeps every edge", missing.edges.length === graph.edges.length);
  check("missing root hides nothing", missing.hiddenNodes === 0 && missing.hiddenEdges === 0);
  check("missing root is flagged", missing.rootFound === false && missing.root === null);
  [null, undefined, "", "   "].forEach(rootId => {
    const cut = coordNeighbourhood(graph.nodes, graph.edges, rootId, 1);
    check(`absent root ${JSON.stringify(rootId)} shows the full graph`,
      cut.nodes.length === graph.nodes.length && cut.hiddenNodes === 0);
  });

  // --- id namespacing: a board row id resolves to its graph node ----------
  const bare = coordNeighbourhood(graph.nodes, graph.edges, "A", 1);
  check("bare row id resolves through the work: prefix",
    bare.rootFound === true && bare.root === "work:A" && ids(bare) === ids(one));

  // --- zero hops is the selected row alone ---------------------------------
  const zero = coordNeighbourhood(graph.nodes, graph.edges, "work:A", 0);
  check("hops 0 keeps only the selected row",
    ids(zero) === "work:A" && zero.edges.length === 1 && zero.hops === 0, ids(zero));

  // --- hops outside the contract fails open --------------------------------
  [-3, 1.5, NaN, "all", undefined, null, {}].forEach(hops => {
    const cut = coordNeighbourhood(graph.nodes, graph.edges, "work:A", hops);
    check(`hops ${JSON.stringify(hops)} fails open`,
      cut.nodes.length === graph.nodes.length && cut.hiddenNodes === 0 && cut.hops === null,
      `${cut.nodes.length} nodes`);
    check(`hops ${JSON.stringify(hops)} still reports the root`, cut.rootFound === true);
  });
  const stringHops = coordNeighbourhood(graph.nodes, graph.edges, "work:A", "2");
  check("numeric string hops is honoured", ids(stringHops) === ids(two));

  // --- malformed members do not throw --------------------------------------
  // A render path may not throw: a blanked panel is a worse failure than any
  // wrong count, and it is the one failure the reader cannot diagnose. The id
  // set, the adjacency map and the kept-node filter must all guard alike.
  [
    ["a null node member", [{ id: "work:A" }, null, { id: "work:B" }], [{ id: "e", source: "work:A", target: "work:B" }]],
    ["an undefined node member", [{ id: "work:A" }, undefined], []],
    ["a null edge member", [{ id: "work:A" }, { id: "work:B" }], [null, { id: "e", source: "work:A", target: "work:B" }]],
    ["an edge with no endpoints", [{ id: "work:A" }], [{ id: "e" }]],
  ].forEach(([name, nodes, edges]) => {
    try {
      const cut = coordNeighbourhood(nodes, edges, "work:A", 1);
      check(`${name} keeps the arithmetic`,
        cut.nodes.length + cut.hiddenNodes === cut.totalNodes
        && cut.edges.length + cut.hiddenEdges === cut.totalEdges);
    } catch (err) {
      check(`${name} does not throw`, false, err && err.message);
    }
  });

  // --- degenerate inputs ---------------------------------------------------
  const empty = coordNeighbourhood([], [], "work:A", 1);
  check("empty graph stays empty and hides nothing",
    empty.nodes.length === 0 && empty.hiddenNodes === 0 && empty.rootFound === false);
  const nothing = coordNeighbourhood(null, null, null, 1);
  check("null inputs do not throw", nothing.nodes.length === 0 && nothing.edges.length === 0);

  // --- purity --------------------------------------------------------------
  check("inputs are not mutated", JSON.stringify(graph) === frozen);

  // --- controls ------------------------------------------------------------
  const html = coordNeighbourhoodControls(
    { nodes: graph.nodes, edges: graph.edges, rootId: "work:A", hops: 1 }, neighbourhoodEscape);
  [1, 2, 3].forEach(hops =>
    check(`controls carry data-hops="${hops}"`, html.includes(`data-hops="${hops}"`)));
  check('controls carry the clear control', html.includes('data-hops="all"'));
  check("controls state the count", html.includes("showing 3 of 7 nodes (4 hidden)"), html);
  check("count line is plain text", /showing \d+ of \d+ nodes \(\d+ hidden\)</.test(html));
  // Five of nine: the ring (A-B, B-C, C-A), the self-edge and the duplicate.
  // The two spur edges and the edge to a target that is not a node are hidden.
  check("controls state the edge count", html.includes("showing 5 of 9 relationships (4 hidden)"), html);
  check("root chip names the row", html.includes('data-row="work:A"') && html.includes('tabindex="0"'));
  check("current hop is marked", html.includes('data-hops="1" aria-pressed="true"'));
  check("other hops are not marked", html.includes('data-hops="2" aria-pressed="false"'));
  check("no inline style attribute", !/style\s*=/.test(html));
  check("no literal colour", !/#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(/.test(html));

  // Exactly one current state, at every hop value a caller can hold — including
  // ones the standard group does not list. Zero pressed buttons over a filtered
  // graph is the failure this pins: a cut with no control admitting to it.
  [1, 2, 3, 5, 40, "all", 0, -2, null, undefined, NaN, "2"].forEach(hops => {
    const markup = coordNeighbourhoodControls(
      { nodes: graph.nodes, edges: graph.edges, rootId: "work:A", hops }, neighbourhoodEscape);
    check(`exactly one hop is current at hops=${JSON.stringify(hops)}`,
      (markup.match(/aria-pressed="true"/g) || []).length === 1,
      String((markup.match(/aria-pressed="true"/g) || []).length));
  });
  const offbook = coordNeighbourhoodControls(
    { nodes: graph.nodes, edges: graph.edges, rootId: "work:A", hops: 5 }, neighbourhoodEscape);
  check("an unlisted in-force hop joins the group so it can be returned to",
    offbook.includes('data-hops="5" aria-pressed="true"'), offbook);
  check("the unlisted hop keeps the group in order",
    offbook.indexOf('data-hops="3"') < offbook.indexOf('data-hops="5"')
    && offbook.indexOf('data-hops="5"') < offbook.indexOf('data-hops="all"'));

  const cleared = coordNeighbourhoodControls(
    { nodes: graph.nodes, edges: graph.edges, rootId: "work:A", hops: "all" }, neighbourhoodEscape);
  check("cleared control shows everything", cleared.includes("showing 7 of 7 nodes (0 hidden)"), cleared);
  check("cleared control marks All", cleared.includes('data-hops="all" aria-pressed="true"'));
  check("cleared control says no cut is in force", cleared.includes("No cut in force"));

  const badRoot = coordNeighbourhoodControls(
    { nodes: graph.nodes, edges: graph.edges, rootId: "work:NOPE", hops: 2 }, neighbourhoodEscape);
  check("bad root shows the full graph", badRoot.includes("showing 7 of 7 nodes (0 hidden)"));
  check("bad root says the root is not on the graph", badRoot.includes("is not a node on this graph"));
  check("bad root disables the hop buttons", badRoot.includes("disabled"));

  const noRoot = coordNeighbourhoodControls({ nodes: graph.nodes, edges: graph.edges }, neighbourhoodEscape);
  check("no selection shows the full graph", noRoot.includes("showing 7 of 7 nodes (0 hidden)"));
  check("no selection says why", noRoot.includes("No row selected"));
  check("no selection renders no root chip", !noRoot.includes("data-row="));

  const nasty = '<img src=x onerror="alert(1)">';
  const escaped = coordNeighbourhoodControls({ nodes: graph.nodes, edges: graph.edges, rootId: nasty, hops: 1 },
    neighbourhoodEscape);
  check("interpolated ids are escaped", !escaped.includes("<img") && escaped.includes("&lt;img"), escaped);
  const unescaped = coordNeighbourhoodControls({ nodes: graph.nodes, edges: graph.edges, rootId: nasty, hops: 1 });
  check("a caller that forgets esc still gets escaped output", !unescaped.includes("<img"));

  const reused = coordNeighbourhoodControls({ result: two, hops: 2, rootId: "work:A" }, neighbourhoodEscape);
  check("a precomputed cut is described without recomputation",
    reused.includes("showing 4 of 7 nodes (3 hidden)"), reused);

  return failures;
}

// CommonJS only, and only when a CommonJS loader is present. In the browser
// this is a classic script: the declarations above are already globals and this
// block is skipped, so nothing here depends on a module system.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { coordNeighbourhood, coordNeighbourhoodControls, coordNeighbourhoodSelfTest };
}
