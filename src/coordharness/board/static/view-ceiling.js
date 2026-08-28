// Ceiling — how parallel this board can actually go, and at what N adding
// another agent stops helping.
//
// Every shipped tab answers a local question. None of them reads the recorded
// `depends_on` edges as a global schedule constraint. This one does, and only
// that: work cannot finish in fewer sequential rows than the longest recorded
// prerequisite chain (L), and cannot finish in fewer than ceil(T / N) rows
// with N agents on T open rows. max(L, ceil(T / N)) is a lower bound on ORDER
// — never a schedule, never a time — and N* = ceil(T / L) is where the
// recorded structure, rather than headcount, becomes the binding constraint.
//
// Reads exactly two documents: snapshot rows and graph depends_on edges.
// timeline, context and pulse are never opened — none carries a sequencing
// fact, and opening them is how a duration would leak in.
//
// One global: view_ceiling(data). Styles ship as the served view-ceiling.css:
// the board sends `style-src 'self'` with no unsafe-inline, so an injected
// <style> gets a null sheet and a style= attribute is dropped. Every computed
// position is an SVG attribute; the one percent width rides the existing
// [data-w] pass in cockpit.js.
//
// Motion: arrival pulses come from the shared layer diffing data-key sets —
// every key here is a stable identity (row id, agent count + its bound, depth
// level + its count), never an index. The chain separators are SVG paths drawn
// FROM the prerequisite TO the dependent, so .m-flow marches in the recorded
// direction; .m-run breathes only on chips whose row derives to running. The
// entrance fade lives on the persistent #ceiling section (one play per tab
// activation, not per 5s repaint) and dies under prefers-reduced-motion.
(() => {
  "use strict";

  function view_ceiling(data) {
    const esc = data.esc || (v => String(v == null ? "" : v).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
    const stateOf = typeof data.stateOf === "function" ? data.stateOf : (() => "");
    const snapshot = data.snapshot;
    const graph = data.graph;

    const card = inner => `<div class="card clcard">
      <h3>How parallel this board can go</h3>${inner}</div>`;

    // ------------------------------------------------------------- readnote
    // Properties of the computation only. No sentence in here asserts a
    // property of the current data; everything data-dependent is computed and
    // rendered in the regions, never hardcoded in this copy.
    const readnote = `<details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body">
      <p><b>Everything here is counted in rows.</b> This view never reads <code>progress_fraction</code> or <code>eta_seconds</code>. Some rows may carry them; neither is an estimate of remaining work, and folding either in would turn a count of rows into a duration the board cannot substantiate. So a &ldquo;row&rdquo; here is one unit of work of entirely unknown size, and the chain length, the bound and the knee are all counts of rows, not amounts of time. Two rows on the same chain may differ by an order of magnitude in real work and this view cannot tell.</p>
      <p><b>The bound is a floor on order, not a schedule.</b> max(L, ceil(T / N)) is the smallest number of sequential rows any assignment could achieve if every row took the same effort, every agent were interchangeable, and nothing blocked, failed or was reopened. None of those hold. Nothing here predicts when the board finishes, in what order rows will actually be claimed, or who should take what. It states only that no plan can do better than this on the structure recorded.</p>
      <p><b>The knee is where the recorded structure stops binding — not where more agents stop helping.</b> Past N*, the dependency chain is the constraint rather than headcount, so an extra agent buys nothing <i>against this bound</i>. Real work has constraints this view cannot see: unrecorded prerequisites, shared files, one GPU, review capacity, and the coordination cost of the agents themselves. The true point of diminishing returns is at or below the knee, never above it.</p>
      <p><b>Only <code>depends_on</code> edges are used.</b> <code>parent</code> edges are containment — an epic holding its children — and a child does not wait on its epic. Folding them in would multiply the chain length by how the epics happen to be drawn. Their count is printed in the ledger so their exclusion is visible rather than silent.</p>
      <p><b>Depth is the longest recorded prerequisite path to a row, not a stage.</b> Rows sharing a depth have no recorded prerequisite among themselves — if a recorded path joined two rows at one depth, the later one would sit strictly deeper, so equal depth rules out any recorded path between them. That is why a depth level can be counted as simultaneously claimable; it is a fact about the record, not a claim that the rows are independent in the work. Rows at depth 0 are only rows with nothing recorded ahead of them.</p>
      <p><b>Done rows and epics are excluded, and dropped edges are counted.</b> A finished row constrains nothing further; an epic is a container, not an assignable item. Any edge dropped for a done endpoint, a target this board does not carry, or a cycle is listed in the ledger with its count. Every drop makes L and the width profile floors under floors, never overestimates.</p>
      <p><b>No mark's size varies with anything but a count.</b> Bar length counts rows at a depth. Every dot is the same size, every bar the same height. The slope of the falling line is an artefact of the plot's proportions, the area under it means nothing, and the line is drawn in steps because there is no such thing as three and a half agents. The curve itself adds no information beyond the two figures printed above it — every point on it is max(L, ceil(T / N)) — and is drawn only as an aid to reading where those two numbers change places.</p>
      <p><b>Status colours the chain chips and nothing else.</b> No count here depends on any status except <code>done</code>, so the view renders identically however the open rows are distributed across running, blocked or planned. Its stability under a changing board is by construction, not a bug.</p>
      <p><b>What moves, and what the movement claims.</b> A brief pulse marks a figure or chip absent from the previous paint — a real change in the data, never a re-render. The dash on the chain separators marches from prerequisite to dependent because each path is drawn in that recorded direction; its speed carries nothing. A breathing chip is a row whose derived status is running; the breath is steady and carries no rate.</p>
      <p><b>This view reads two documents and ignores the rest.</b> It uses recorded rows and recorded dependency edges. It never opens the timeline, the context document or the pulse, so it carries no instant, no actor, no note and no prose, and it renders identically whether or not those endpoints answer.</p>
    </div></details>`;

    // ------------------------------------------------------------ row intake
    const allRows = (snapshot && Array.isArray(snapshot.rows)) ? snapshot.rows : null;
    if (!allRows || !allRows.length) {
      return card(`${readnote}<p class="cl-empty">This board has no rows. There is no ceiling to compute.</p>`);
    }

    let unreadableRows = 0;
    const rows = [];
    allRows.forEach(r => { if (r && r.id != null && String(r.id) !== "") rows.push(r); else unreadableRows += 1; });

    const byId = new Map(rows.map(r => [String(r.id), r]));
    const strip = id => String(id == null ? "" : id).replace(/^(work|job):/, "");
    // nodeRowId semantics, raw id first, stripped second — stripping first is
    // the trap that once put every job in "unassigned" on the Crossings card.
    const resolve = id => {
      const raw = String(id == null ? "" : id);
      if (byId.has(raw)) return byId.get(raw);
      return byId.get(strip(raw)) || null;
    };

    const isDone = r => String(r.status == null ? "" : r.status).trim().toLowerCase() === "done";
    const isEpic = r => r.bucket === "epic";
    const open = rows.filter(r => !isDone(r) && !isEpic(r));
    const epicCount = rows.filter(isEpic).length;
    const statusMissing = rows.filter(r => !isEpic(r) && String(r.status == null ? "" : r.status).trim() === "").length;
    const T = open.length;
    const openIds = new Set(open.map(r => String(r.id)));

    if (T === 0) {
      return card(`${readnote}<p class="cl-empty">Every row on this board is done or is an epic container. Nothing is open to schedule.</p>`);
    }

    // ------------------------------------------------------------ edge intake
    const graphAbsent = !graph || !Array.isArray(graph.edges);
    const edges = graphAbsent ? [] : graph.edges;

    let depsTotal = 0, retained = 0;
    let dDone = 0, dOff = 0, dEpic = 0, dUnreadable = 0, dDangling = 0;
    let parentCount = 0, otherCount = 0;
    const prereq = new Map(); // open id -> [open prerequisite ids]

    edges.forEach(e => {
      if (!e || e.kind !== "depends_on") {
        if (e && e.kind === "parent") parentCount += 1; else otherCount += 1;
        return;
      }
      depsTotal += 1;
      if (e.source == null || e.target == null) { dUnreadable += 1; return; }
      if (e.relationship_state !== "source_bound") { dDangling += 1; return; }
      const src = resolve(e.source), tgt = resolve(e.target);
      if (!src || !tgt) { dOff += 1; return; }
      if (isEpic(src) || isEpic(tgt)) { dEpic += 1; return; }
      if (!openIds.has(String(src.id)) || !openIds.has(String(tgt.id))) { dDone += 1; return; }
      retained += 1;
      const s = String(src.id);
      if (!prereq.has(s)) prereq.set(s, []);
      prereq.get(s).push(String(tgt.id)); // source waits on target
    });
    prereq.forEach(list => list.sort());

    // ---------------------------------------------------------------- depth
    // Memoised DFS with an explicit ancestor set. An edge closing back onto
    // the stack is ignored, counted, and disclosed: a cyclic record has no
    // depth, so on a cyclic board L is a lower bound on a lower bound.
    let cyclesBroken = 0;
    const depthOf = new Map();
    const onStack = new Set();
    const depth = x => {
      if (depthOf.has(x)) return depthOf.get(x);
      onStack.add(x);
      let best = 0;
      (prereq.get(x) || []).forEach(p => {
        if (onStack.has(p)) { cyclesBroken += 1; return; }
        best = Math.max(best, depth(p) + 1);
      });
      onStack.delete(x);
      depthOf.set(x, best);
      return best;
    };
    // Seeded in sorted id order, NOT snapshot order. On an acyclic record the
    // memoised longest path is order-independent, but a cycle makes the result
    // depend on which node the DFS enters first: seeding A,B,C on a 2-cycle
    // gave L = 3 and seeding B,A,C gave L = 2 on the same data. A served number
    // that moves when the server happens to reorder its rows is not a fact
    // about the board, and the motion layer would read the changed data-key as
    // an arrival. Sorting makes every figure here a function of the row and
    // edge SETS alone.
    open.map(r => String(r.id)).sort().forEach(id => depth(id));

    const L = Math.max(...open.map(r => depthOf.get(String(r.id)))) + 1;
    const width = [];
    for (let d = 0; d < L; d += 1) width.push(0);
    open.forEach(r => { width[depthOf.get(String(r.id))] += 1; });
    // Edges that survived the endpoint filters but were then ignored to break a
    // cycle are NOT in the DAG: counting them as retained would let the card
    // read "18 of 18 retained" beside "2 ignored to break a cycle", which
    // double-counts the same two edges and overstates what L was computed on.
    const inDag = retained - cyclesBroken;
    const Nstar = Math.ceil(T / L);
    const boundAt = n => Math.max(L, Math.ceil(T / n));
    const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

    // ---------------------------------------------------- longest chain(s)
    // Tie count by path DP; one chain rendered deterministically (lexicographic
    // at every choice). The count is stated unconditionally so silence is
    // never ambiguous between "one chain" and "an unrun check".
    const chainCnt = new Map();
    const countChains = x => {
      if (chainCnt.has(x)) return chainCnt.get(x);
      const d = depthOf.get(x);
      let total = 0;
      (prereq.get(x) || []).forEach(p => { if (depthOf.get(p) === d - 1) total += countChains(p); });
      if (total === 0) total = 1;
      chainCnt.set(x, total);
      return total;
    };
    const tops = open.map(r => String(r.id)).filter(id => depthOf.get(id) === L - 1).sort();
    const ties = tops.reduce((sum, id) => sum + countChains(id), 0);
    const chain = [];
    if (tops.length) {
      let at = tops[0];
      chain.push(at);
      for (let d = L - 1; d > 0; d -= 1) {
        const next = (prereq.get(at) || []).filter(p => depthOf.get(p) === d - 1).sort()[0];
        if (next === undefined) break;
        chain.push(next);
        at = next;
      }
    }

    // ---------------------------------------------------------------- regions
    const singleChain = L >= T && inDag > 0;
    const noDeps = !graphAbsent && inDag === 0;

    const verdict = `<div class="cl-verdicts" data-key="cl:v:${T}:${L}:${Nstar}">
      <p class="cl-verdict"><b>${T}</b> open row${T === 1 ? "" : "s"}. Longest chain of recorded prerequisites: <b>${L}</b> row${L === 1 ? "" : "s"}.</p>
      ${(singleChain || noDeps || graphAbsent || T === 1) ? "" :
        `<p class="cl-verdict">Past <b>${Nstar}</b> agents, the recorded structure stops being the constraint.</p>`}
      ${graphAbsent ? "" : `<p class="cl-sub" data-key="cl:sub:${inDag}:${depsTotal}:${parentCount}">${inDag} of ${depsTotal} recorded depends_on edge${depsTotal === 1 ? "" : "s"} retained &middot; ${parentCount} parent edge${parentCount === 1 ? "" : "s"} present, not used</p>`}
      <p class="cl-sub">Assumes every row is one unit of work. The board records no size for any row.</p>
    </div>`;

    // Region B — the bound curve. The knee is suppressed when no dependency
    // survives: there L = 1, so the "asymptote" is the floor of one row and a
    // marked knee would dress the absence of a record as a finding.
    const plot = () => {
      // The domain MUST contain N*. A fixed cap of 24 put the knee at x = 866
      // in a 720-wide viewBox on a wide, shallow board (T = 60, L = 2, N* = 30):
      // the knee line, its "N* = 30" label and its tick were all emitted and all
      // clipped away, so the card printed "Past 30 agents..." above a curve that
      // was still falling at the right edge with nothing to say it had been cut
      // off. Nmax is now driven by N* itself, and stays within N* + 12 of it so
      // the flat region is visible without drawing hundreds of dots.
      // Exception: with no surviving dependency the knee is suppressed and
      // N* = T, so reaching it would pack T dots into 636px as a solid band and
      // destroy the discreteness the step shape exists to show. No knee is
      // claimed there, so nothing off the right edge is being withheld.
      const Nmax = noDeps
        ? Math.max(8, Math.min(24, T))
        : Math.max(8, Math.min(2 * Nstar, Nstar + 12));
      const X0 = 64, X1 = 700, Y0 = 24, Y1 = 256;
      const px = n => X0 + (n - 1) * (X1 - X0) / (Nmax - 1);
      const py = v => Y1 - (v / T) * (Y1 - Y0);
      const half = (X1 - X0) / (Nmax - 1) / 2;
      const r2 = v => Math.round(v * 100) / 100;

      // ~12 labels whatever the domain, and N* always among them. The step is
      // derived from Nmax so a wide domain thins the labels instead of pushing
      // a tick past the right edge, where it would be drawn and then clipped.
      const tickStep = Math.max(1, Math.ceil(Nmax / 12));
      const ticks = [];
      for (let n = 1; n <= Nmax; n += tickStep) ticks.push(n);
      if (!ticks.includes(Nstar) && Nstar <= Nmax) ticks.push(Nstar);
      ticks.sort((a, b) => a - b);

      let d = "";
      for (let n = 1; n <= Nmax; n += 1) {
        const x0 = r2(Math.max(X0, px(n) - half)), x1 = r2(Math.min(X1, px(n) + half));
        const y = r2(py(boundAt(n)));
        d += (n === 1 ? `M${x0} ${y}` : `V${y}`) + ` H${x1}`;
      }

      const lattice = ticks.map(n => `<line class="cl-lattice" x1="${r2(px(n))}" y1="${Y0}" x2="${r2(px(n))}" y2="${Y1}"></line>`).join("")
        + [0, L, T].map(v => `<line class="cl-lattice" x1="${X0}" y1="${r2(py(v))}" x2="${X1}" y2="${r2(py(v))}"></line>`).join("");

      const dots = [];
      for (let n = 1; n <= Nmax; n += 1) {
        const f = boundAt(n);
        dots.push(`<circle class="cl-pt" data-key="cl:pt:${n}:${f}" cx="${r2(px(n))}" cy="${r2(py(f))}" r="2.5"><title>${n} agent${n === 1 ? "" : "s"}: no fewer than ${plural(f, "row")} of sequential order</title></circle>`);
      }

      const knee = (noDeps) ? "" :
        `<line class="cl-knee" x1="${r2(px(Nstar))}" y1="${r2(py(boundAt(Nstar)))}" x2="${r2(px(Nstar))}" y2="${Y1}"></line>
         <text class="cl-kneelabel" x="${r2(px(Nstar))}" y="288" text-anchor="middle">N* = ${Nstar}</text>`;

      const xlabels = ticks.map(n => `<text class="cl-tick" x="${r2(px(n))}" y="270" text-anchor="middle">${n}</text>`).join("");
      const ylabels = `<text class="cl-tick" x="58" y="${Y1 + 4}" text-anchor="end">0</text>
        <text class="cl-tick" x="58" y="${r2(py(L)) + 4}" text-anchor="end">${L}</text>
        <text class="cl-tick" x="58" y="${Y0 + 4}" text-anchor="end">T = ${T} open rows</text>`;

      // The spoken label must describe the plotted domain. In the no-deps case
      // the minimum sits at N* = T, outside it, so naming it here would read as
      // a point on a chart that does not contain it.
      const aria = noDeps
        ? `Scheduling bound against agent count: ${plural(T, "open row")}, no recorded prerequisite surviving between two open rows; the bound is ceil(${T} / N) rows of order, plotted for 1 to ${Nmax} agent${Nmax === 1 ? "" : "s"}.`
        : `Scheduling bound against agent count: ${plural(T, "open row")}, longest prerequisite chain ${plural(L, "row")}; the bound reaches its minimum, ${plural(L, "row")} of order, at ${Nstar} agent${Nstar === 1 ? "" : "s"}.`;

      return `<h4 class="cl-h">Scheduling bound</h4>
      <div class="cl-plotwrap"><svg class="cl-plot" viewBox="0 0 720 300" role="img" aria-label="${esc(aria)}">
        <defs><filter id="clglow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="3"></feGaussianBlur></filter></defs>
        ${lattice}
        <line class="cl-axis" x1="${X0}" y1="${Y0}" x2="${X0}" y2="${Y1}"></line>
        <line class="cl-axis" x1="${X0}" y1="${Y1}" x2="${X1}" y2="${Y1}"></line>
        <line class="cl-asymptote" x1="${X0}" y1="${r2(py(L))}" x2="${X1}" y2="${r2(py(L))}"></line>
        <text class="cl-asymlabel" x="700" y="${r2(py(L)) - 6}" text-anchor="end">L = ${plural(L, "row")}</text>
        <path class="cl-glowline" d="${d}" filter="url(#clglow)"></path>
        <path class="cl-step" d="${d}"></path>
        ${dots.join("")}${knee}${xlabels}${ylabels}
        <text class="cl-axname" transform="rotate(-90 16 140)" x="16" y="140" text-anchor="middle">rows of sequential order</text>
        <text class="cl-axname" x="382" y="298" text-anchor="middle">agents (N)</text>
      </svg></div>
      <p class="cl-note">The step height is a count of rows, never a time. Hover a dot for the bound at that agent count; the vertical position is the only thing the dot encodes.</p>`;
    };

    const widths = () => {
      const maxW = Math.max(...width, 1);
      const rowsHtml = width.map((n, d) => `<div class="cl-wrow" data-key="cl:w:${d}:${n}">
        <span class="cl-wlabel">depth ${d}</span>
        <div class="cl-bar"><i data-w="${Math.round(n / maxW * 100)}"></i></div>
        <span class="cl-wcount">${plural(n, "row")}</span></div>`).join("");
      // The independence sentence is a reading of a record that was READ. When
      // the graph document did not answer there is no record to read, and the
      // single depth-0 bar is the shape of that silence — saying those rows
      // "have no recorded prerequisite among each other" there would dress a
      // failed fetch as a measured finding.
      const note = graphAbsent
        ? `How many rows sit at each prerequisite depth. Every row sits at depth 0 because no dependency document could be read, not because any row was found to be free of prerequisites. Depth is a position in the recorded order, not a stage anything moves through.`
        : `How many rows sit at each prerequisite depth. Rows at one depth have no recorded prerequisite among each other, so they could in principle all be claimed at once. Depth is a position in the recorded order, not a stage anything moves through.`;
      return `<h4 class="cl-h">Prerequisite depth</h4><div class="cl-widths">${rowsHtml}</div>
      <p class="cl-note">${esc(note)}</p>`;
    };

    // Chain separators are SVG paths drawn from the prerequisite (right) to the
    // dependent (left): .m-flow's dash marches in that recorded direction.
    const sep = `<svg class="cl-sep" viewBox="0 0 26 12" aria-hidden="true">
      <path class="cl-flowpath m-flow" d="M24 6 L5 6"></path>
      <path class="cl-flowhead" d="M9 2.5 L2 6 L9 9.5 Z"></path></svg>`;

    const chainRegion = ids => {
      const chips = ids.map(id => {
        const row = byId.get(id);
        const state = row ? stateOf(row) : "";
        const mod = row && row.module ? `<span class="cl-mod">${esc(String(row.module))}</span>` : "";
        const title = row && row.title ? ` title="${esc(String(row.title))}"` : "";
        return `<button class="chip cl-link ${esc(state)}${state === "running" ? " m-run" : ""}" data-row="${esc(id)}" data-key="cl:c:${esc(id)}"${title}>${esc(id)} ${mod}</button>`;
      });
      const aria = `Longest recorded chain, dependent first: ${ids.join(", each waiting on ")}.`;
      const tieNote = ties === 1
        ? `One chain has this length; it is shown in full.`
        : `${ties} chains tie at this length; one is shown. The bound is the length, not this particular chain.`;
      return `<h4 class="cl-h">Longest recorded chain</h4>
      <div class="cl-chain" role="group" aria-label="${esc(aria)}">${chips.join(sep)}</div>
      <p class="cl-note">${esc(tieNote)}</p>`;
    };

    const ledger = () => {
      const dropSentence = `L and the width profile are computed on the retained edges only; both are lower bounds on a board with dropped structure.`;
      const line = (slug, label, value, isDrop) => `<div class="cl-lline${isDrop && value > 0 ? " drop" : ""}" data-key="cl:lg:${slug}:${value}"><dt>${esc(label)}</dt><dd><b>${value}</b></dd></div>`;
      const edgeLines = graphAbsent
        ? `<div class="cl-lline drop" data-key="cl:lg:absent"><dt>graph document</dt><dd><b>did not answer</b></dd></div>`
        : [
            line("rec", "depends_on edges recorded", depsTotal, false),
            line("ret", "retained in this DAG", inDag, false),
            line("done", "dropped, endpoint already done", dDone, true),
            line("off", "dropped, target not on this board", dOff, true),
            line("epic", "dropped, endpoint is an epic", dEpic, true),
            line("unread", "dropped, endpoint unreadable", dUnreadable, true),
            line("dangle", "dropped, not source_bound", dDangling, true),
            line("cycle", "ignored to break a recorded cycle", cyclesBroken, true),
          ].join("");
      const rowLines = [
        line("epics", "rows excluded as epics", epicCount, false),
        line("nostatus", "rows with no recorded status", statusMissing, false),
        line("norow", "rows unreadable, no id", unreadableRows, true),
      ].join("");
      const parentLines = graphAbsent ? "" : [
        line("parent", "parent edges present, not used as sequencing", parentCount, false),
        line("other", "other edges present, not sequencing", otherCount, false),
      ].join("");
      const anyDrop = dDone + dOff + dEpic + dUnreadable + dDangling + cyclesBroken + unreadableRows > 0;
      const cycleNote = cyclesBroken > 0
        ? `<p class="cl-note">${cyclesBroken} edge${cyclesBroken === 1 ? " was" : "s were"} ignored to break a recorded cycle. A cyclic dependency record has no depth; the L reported above is what remains after those edges were dropped, and is therefore a floor under a floor.</p>`
        : "";
      return `<h4 class="cl-h">Edge ledger</h4>
      <dl class="cl-ledger">${edgeLines}${rowLines}${parentLines}</dl>
      ${anyDrop && !graphAbsent ? `<p class="cl-note drop">${esc(dropSentence)}</p>` : ""}${cycleNote}`;
    };

    // ------------------------------------------------------ degenerate states
    if (graphAbsent) {
      return card(`${readnote}${verdict}
        <p class="cl-empty">The graph document did not answer, so no dependency edge could be read. With no recorded prerequisites the bound is ceil(${T} / N) rows of order at N agents and falls without limit &mdash; which is a statement about the absence of a record, not about the work.</p>
        ${widths()}${ledger()}`);
    }
    if (T === 1) {
      return card(`${readnote}${verdict}
        <p class="cl-empty">One row is open. It takes one row's worth of order to finish, at any number of agents. A second agent has nothing to take.</p>
        ${chainRegion(chain.length ? chain : [String(open[0].id)])}${ledger()}`);
    }
    if (noDeps) {
      return card(`${readnote}${verdict}
        <p class="cl-empty">No depends_on edge survives between two open rows, so L = 1 and the bound is ceil(${T} / N) rows of order with no asymptote above 1. The board records no order here; it does not follow that none exists.</p>
        ${plot()}${widths()}${ledger()}`);
    }
    const singleChainNote = singleChain
      ? `<p class="cl-empty">Every open row lies on one chain. No number of agents finishes this in fewer than ${plural(T, "row")} of order; parallelism has nothing to work with.</p>`
      : "";

    return card(`${readnote}${verdict}${singleChainNote}${plot()}${widths()}${chainRegion(chain)}${ledger()}`);
  }

  globalThis.view_ceiling = view_ceiling;
})();
