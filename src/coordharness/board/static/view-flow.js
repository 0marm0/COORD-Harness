// Crossings — where a dependency leaves the vertical it was raised in.
//
// The map already answers three questions. Fleet: who is working where.
// Dependencies: what waits on what, row by row. Context: what anyone said about
// their own work. None of them aggregates the recorded `depends_on` edges up to
// the vertical, so nobody can see which verticals are upstream of which, which
// are pure providers, which are pure consumers, and which never touch another
// vertical at all. That is what this card is for.
//
// It is meant to be appended to the #deps panel, above the analysis cards: the
// Dependencies view's Clustered layout draws exactly these crossings row by row,
// and this is the same fact aggregated, so the two belong on one screen rather
// than in two tabs.
//
// One global: view_flow(data). Everything else is closed over. The helpers
// (esc, stateOf, rowById, contextById, ...) come in on `data` and are never
// redefined here. The stylesheet is injected once, under an id guard, because
// the panel re-renders every five seconds.

function view_flow(data) {
  const { snapshot, graph, esc, stateOf, rowById, contextById } = data;

  // ------------------------------------------------------------------ style
  // Tokens only. Semantic colour (amber = waiting, dashed amber = not on the
  // board) is never expressed as a literal and never follows the accent.
  // Styles ship as a served stylesheet, not an injected <style>: the board
  // sends `style-src 'self'` with no unsafe-inline, so an injected element is
  // silently dropped -- the sheet object is never even created.


  // ------------------------------------------------------------- aggregation
  const OFF = "\u0000offboard";        // target exists as a reference, not as a row
  const UNASSIGNED = "\u0000unassigned"; // row carries no module
  const nameOf = key => key === OFF ? "not on this board"
    : key === UNASSIGNED ? "unassigned vertical" : key;

  const rows = rowById();
  const ctx = contextById();
  // Snapshot ids and graph node ids agree for jobs and differ for work
  // (`work:UI-101` against `UI-101`), so the raw id is tried before the
  // stripped one — stripping first is what once put every job in "unassigned".
  const strip = id => String(id || "").replace(/^(work|job):/, "");
  const rowFor = id => rows.get(id) || rows.get(strip(id));
  const modKeyOf = row => (row && row.module) ? String(row.module) : UNASSIGNED;

  const deps = ((graph && graph.edges) || []).filter(e => e.kind === "depends_on");

  const pairs = new Map();      // pair key "provider\u0001consumer" -> { provider, consumer, off, records[] }
  const insideBy = new Map();   // module -> dependencies that never leave it
  const waitOnThis = new Map(); // module -> crossing records where it is the provider
  const waitsElse = new Map();  // module -> crossing records where it is the consumer
  const offBy = new Map();      // module -> of those, how many point off the board
  const bump = (map, key, by) => map.set(key, (map.get(key) || 0) + (by === undefined ? 1 : by));

  let crossing = 0, inside = 0, offboard = 0, skippedEpic = 0, skippedUnplaceable = 0;

  deps.forEach(edge => {
    const consumerRow = rowFor(edge.source);
    const providerRow = rowFor(edge.target);
    // Epics are containers, not work; the Fleet matrix drops them and so does this.
    if ((consumerRow && consumerRow.bucket === "epic") || (providerRow && providerRow.bucket === "epic")) {
      skippedEpic += 1;
      return;
    }
    // Without a consumer row there is no vertical to draw the arc from, and
    // guessing one from the id prefix is exactly the inference this view refuses.
    if (!consumerRow) { skippedUnplaceable += 1; return; }

    const consumer = modKeyOf(consumerRow);
    const unresolved = edge.relationship_state !== "source_bound" || !providerRow;
    if (!unresolved && modKeyOf(providerRow) === consumer) {
      inside += 1;
      bump(insideBy, consumer);
      return;
    }
    const provider = unresolved ? OFF : modKeyOf(providerRow);
    if (unresolved) { offboard += 1; bump(offBy, consumer); } else { crossing += 1; }
    bump(waitOnThis, provider);
    bump(waitsElse, consumer);

    const key = provider + "\u0001" + consumer;
    if (!pairs.has(key)) pairs.set(key, { provider, consumer, off: unresolved, records: [] });
    pairs.get(key).records.push({
      providerId: providerRow ? providerRow.id : strip(edge.target),
      providerRow,
      consumerId: consumerRow.id,
      consumerRow,
    });
  });

  const total = deps.length;
  const drawn = [...pairs.values()];

  // A pair is amber when a consumer on it records blocked_reason_class =
  // upstream_dependency. The board records THAT a row is held up by something
  // upstream, never WHICH dependency that names, so this marks a waiting
  // consumer on the pair — it does not assert that this arc is the cause.
  const isWaiting = pair => pair.records.some(r => {
    const item = ctx.get(r.consumerId);
    return !!item && item.blocked_reason_class === "upstream_dependency";
  });

  // ------------------------------------------------------- degenerate states
  const allModules = [...new Set(((snapshot && snapshot.rows) || [])
    .filter(r => r.bucket !== "epic").map(modKeyOf))];
  const trayLine = keys => keys.length
    ? `<p class="meta xtray">No cross-vertical dependency recorded: ${
        keys.sort((a, b) => nameOf(a).localeCompare(nameOf(b))).map(k => {
          const n = insideBy.get(k) || 0;
          return `<b>${esc(nameOf(k))}</b> (${n ? `${n} inside` : "no dependency recorded"})`;
        }).join(" &middot; ")}. These have no position on the axis.</p>`
    : "";

  const excluded = (skippedEpic || skippedUnplaceable)
    ? `<p class="meta">${skippedEpic ? `${skippedEpic} dependenc${skippedEpic === 1 ? "y" : "ies"} touching an epic row ${skippedEpic === 1 ? "is" : "are"} left out, as on the Fleet matrix. ` : ""}${
        skippedUnplaceable ? `${skippedUnplaceable} could not be placed because the waiting row is not on this board; nothing was inferred from its id.` : ""}</p>`
    : "";

  // The counts have to close. `inside` alone does not account for a dependency
  // dropped as an epic or as unplaceable, and a sentence that reads like a
  // partition but silently loses rows is the defect this whole card is against.
  const unaccounted = total - inside;
  const countSentence = total === 0
    ? "No dependency is recorded on this board at all."
    : total === 1
      ? (inside === 1
          ? "The one dependency recorded stays inside a single vertical."
          : "One dependency is recorded, and it is accounted for below.")
      : `${total} dependencies are recorded; ${inside} of them stay inside a single vertical${
          unaccounted ? `, and the other ${unaccounted} ${
            unaccounted === 1 ? "is" : "are"} accounted for below` : ""}.`;

  if (!drawn.length) {
    return `<div class="card flowcard"><h3>Where work crosses a vertical</h3>
      <p class="meta">No recorded dependency crosses a vertical boundary on this board. ${
        countSentence} Only a dependency written down as data can be counted here: one stated in a note and nowhere else is invisible to this card, so an absence is a missing record, not a missing dependency.</p>
      ${trayLine(allModules.filter(k => (waitOnThis.get(k) || 0) + (waitsElse.get(k) || 0) === 0))}
      ${excluded}</div>`;
  }

  // ------------------------------------------------------------------ layout
  // Rank is the longest path through the aggregated graph, after collapsing any
  // cycle: two verticals that wait on each other have no order between them, and
  // the axis must not invent one.
  const modules = [...new Set(drawn.flatMap(p => [p.provider, p.consumer]))];
  const links = drawn.map(p => [p.provider, p.consumer]);
  // The off-board terminus occupies a node but is not a vertical, so it must not
  // be counted as one anywhere a count is stated.
  const verticalCount = modules.filter(m => m !== OFF).length;

  const scc = (nodes, edges) => {
    const adj = new Map(nodes.map(n => [n, []]));
    const rev = new Map(nodes.map(n => [n, []]));
    edges.forEach(([a, b]) => { if (a !== b) { adj.get(a).push(b); rev.get(b).push(a); } });
    const order = [], seen = new Set();
    nodes.forEach(start => {
      if (seen.has(start)) return;
      seen.add(start);
      const stack = [[start, 0]];
      while (stack.length) {
        const frame = stack[stack.length - 1];
        const kids = adj.get(frame[0]);
        if (frame[1] < kids.length) {
          const next = kids[frame[1]++];
          if (!seen.has(next)) { seen.add(next); stack.push([next, 0]); }
        } else { order.push(frame[0]); stack.pop(); }
      }
    });
    const comp = new Map();
    let count = 0;
    for (let i = order.length - 1; i >= 0; i -= 1) {
      const start = order[i];
      if (comp.has(start)) continue;
      const stack = [start];
      comp.set(start, count);
      while (stack.length) {
        const node = stack.pop();
        rev.get(node).forEach(prev => {
          if (!comp.has(prev)) { comp.set(prev, count); stack.push(prev); }
        });
      }
      count += 1;
    }
    return { comp, count };
  };

  const { comp, count } = scc(modules, links);
  const members = new Map();
  modules.forEach(m => bump(members, comp.get(m)));

  const cAdj = new Map(), indeg = new Map();
  for (let c = 0; c < count; c += 1) { cAdj.set(c, new Set()); indeg.set(c, 0); }
  links.forEach(([a, b]) => {
    const ca = comp.get(a), cb = comp.get(b);
    if (ca !== cb && !cAdj.get(ca).has(cb)) { cAdj.get(ca).add(cb); indeg.set(cb, indeg.get(cb) + 1); }
  });
  const rankOf = new Map();
  const pending = new Map(indeg);
  const queue = [...pending.keys()].filter(c => pending.get(c) === 0);
  queue.forEach(c => rankOf.set(c, 0));
  while (queue.length) {
    const c = queue.shift();
    cAdj.get(c).forEach(next => {
      rankOf.set(next, Math.max(rankOf.get(next) || 0, (rankOf.get(c) || 0) + 1));
      pending.set(next, pending.get(next) - 1);
      if (pending.get(next) === 0) queue.push(next);
    });
  }
  for (let c = 0; c < count; c += 1) if (!rankOf.has(c)) rankOf.set(c, 0);

  const NODE_W = 216, NODE_H = 58, COL_GAP = 346, ROW_GAP = 78, PAD_X = 26, TOP = 44;
  const columns = new Map();
  modules.forEach(m => {
    const r = rankOf.get(comp.get(m));
    if (!columns.has(r)) columns.set(r, []);
    columns.get(r).push(m);
  });
  const ranks = [...columns.keys()].sort((a, b) => a - b);
  // Alphabetical inside a column, and alphabetical is all it is.
  ranks.forEach(r => columns.get(r).sort((a, b) => nameOf(a).localeCompare(nameOf(b))));

  const placed = new Map();
  ranks.forEach((r, col) => columns.get(r).forEach((m, i) => {
    placed.set(m, { x: PAD_X + col * COL_GAP, y: TOP + i * ROW_GAP, col, rank: r });
  }));

  const mutual = new Set(modules.filter(m => (members.get(comp.get(m)) || 1) > 1));
  const tallest = Math.max(...ranks.map(r => columns.get(r).length), 1);
  const hasMutual = drawn.some(p => placed.get(p.provider).col === placed.get(p.consumer).col);

  // An arc that skips a column would otherwise run straight through whatever
  // sits in between, and two arcs leaving the same node at the same height would
  // lie exactly on top of each other over the shorter one's whole length — three
  // dependencies would read as two. Skipping arcs are bowed clear. The bow is
  // separation and nothing else; it is not a quantity.
  const BOW = 54;
  const bowOf = pair =>
    Math.max(placed.get(pair.consumer).col - placed.get(pair.provider).col - 1, 0) * BOW;
  const skips = drawn.some(p => bowOf(p) > 0);
  // A cubic with both control points pushed down by `bow` dips 0.75*bow at its
  // midpoint; the node it leaves already owns NODE_H/2 of that.
  const drop = Math.max(0, ...drawn.map(p => bowOf(p) * 0.75 - NODE_H / 2));

  const width = PAD_X * 2 + (ranks.length - 1) * COL_GAP + NODE_W + (hasMutual ? 100 : 0);
  const height = TOP + (tallest - 1) * ROW_GAP + NODE_H + 26 + Math.round(drop);

  // ------------------------------------------------------------------ drawing
  const guides = ranks.map((r, col) => {
    const x = PAD_X + col * COL_GAP;
    const caption = ranks.length === 1 ? "one column — mutual, no order"
      : col === 0 ? "upstream" : col === ranks.length - 1 ? "downstream" : "";
    return `<line class="xguide" x1="${x - 13}" y1="${TOP - 12}" x2="${x - 13}" y2="${height - 14}"></line>${
      caption ? `<text class="xrank" x="${x}" y="${TOP - 18}">${esc(caption)}</text>` : ""}`;
  }).join("");

  const arcs = drawn.map(pair => {
    const a = placed.get(pair.provider), b = placed.get(pair.consumer);
    const n = pair.records.length;
    // Thickness counts records. Nothing else in the arc encodes a quantity.
    const w = (1.2 + 0.9 * Math.sqrt(Math.max(n - 1, 0))).toFixed(2);
    const cls = pair.off ? "miss" : isWaiting(pair) ? "wait" : "";
    const y1 = a.y + NODE_H / 2, y2 = b.y + NODE_H / 2;
    let d, head;
    if (a.col === b.col) {
      // No order between them: leave and re-enter on the same side, so the arc
      // never reads as a left-to-right step it is not.
      const x1 = a.x + NODE_W, x2 = b.x + NODE_W + 11;
      d = `M${x1} ${y1} C${x1 + 74} ${y1}, ${x2 + 74} ${y2}, ${x2} ${y2}`;
      head = `M${b.x + NODE_W + 1} ${y2} l8 -4.5 l0 9 z`;
    } else {
      const x1 = a.x + NODE_W, x2 = b.x - 11;
      const span = Math.max(x2 - x1, 1);
      const bow = bowOf(pair);
      d = `M${x1} ${y1} C${x1 + span * 0.42} ${y1 + bow}, ${x2 - span * 0.42} ${y2 + bow}, ${x2} ${y2}`;
      head = `M${b.x - 1} ${y2} l-8 -4.5 l0 9 z`;
    }
    const label = `${nameOf(pair.provider)} is upstream of ${nameOf(pair.consumer)} — ${n} dependenc${
      n === 1 ? "y" : "ies"}${pair.off ? ", target not on this board" : ""}${
      cls === "wait" ? "; a waiting row here records an upstream dependency, though not which one" : ""}`;
    return `<path class="xarc ${cls}" d="${d}" stroke-width="${w}"><title>${esc(label)}</title></path>
      <path class="xhead ${cls}" d="${head}"></path>`;
  }).join("");

  const sentence = key => {
    const on = waitOnThis.get(key) || 0, els = waitsElse.get(key) || 0;
    const ins = insideBy.get(key) || 0, offn = offBy.get(key) || 0;
    const name = nameOf(key);
    // The terminus is not a vertical: it is every dependency whose target the
    // board names but does not hold. Describing it with the vertical sentence
    // would assert it has work inside it, which is exactly what it does not have.
    if (key === OFF) {
      return `${on} dependenc${on === 1 ? "y on this board names" : "ies on this board name"} a row the board does not hold. Its vertical is unknown, and the board records only the id, so none is shown.`;
    }
    const first = on
      ? `${on} dependenc${on === 1 ? "y" : "ies"} in other verticals wait${on === 1 ? "s" : ""} on ${name}`
      : `nothing outside ${name} waits on it`;
    const second = els
      ? `${name} waits on ${els} thing${els === 1 ? "" : "s"} outside itself${
          offn ? ` (${offn} of them not on this board)` : ""}`
      : `${name} waits on nothing outside itself`;
    const third = `${ins} dependenc${ins === 1 ? "y stays" : "ies stay"} inside ${name}`;
    return `${first}; ${second}; ${third}.`;
  };

  const nodes = modules.map(m => {
    const p = placed.get(m);
    const on = waitOnThis.get(m) || 0, els = waitsElse.get(m) || 0;
    const cls = [m === OFF ? "offboard" : "", mutual.has(m) ? "mutual" : ""].filter(Boolean).join(" ");
    const name = nameOf(m);
    const shown = name.length > 26 ? name.slice(0, 25) + "…" : name;
    const lines = m === OFF
      ? [`${on} dependenc${on === 1 ? "y points" : "ies point"} off the board`, "no vertical is recorded for it"]
      : [on ? `${on} wait${on === 1 ? "s" : ""} on this vertical` : "nothing waits on this vertical",
         els ? `this vertical waits on ${els}` : "waits on nothing outside"];
    return `<g class="xnode ${cls}" transform="translate(${p.x},${p.y})">
      <rect rx="5" width="${NODE_W}" height="${NODE_H}"></rect>
      <text class="xname" x="12" y="21">${esc(shown)}</text>
      <text class="xcount" x="12" y="37">${esc(lines[0])}</text>
      <text class="xcount" x="12" y="50">${esc(lines[1])}</text>
      <title>${esc(sentence(m))}</title></g>`;
  }).join("");

  // The picture is a picture. Everything it shows is also written out below in
  // text, so nothing here is reachable only by hovering an SVG shape.
  const roster = ranks.map((r, col) => columns.get(r).map(m => {
    const place = m === OFF ? "off the axis"
      : ranks.length === 1 ? "no order between these"
      : col === 0 ? "upstream" : col === ranks.length - 1 ? "downstream" : `column ${col + 1} of ${ranks.length}`;
    return `<li><b>${esc(nameOf(m))}</b> — ${esc(place)}${
      mutual.has(m) ? ", mutual" : ""} — ${esc(sentence(m))}</li>`;
  }).join("")).join("");

  const chip = (id, row) => `<button class="chip ${row ? esc(stateOf(row)) : ""}" data-row="${
    esc(id)}" tabindex="0" title="${esc(row ? row.title : id)}">${esc(id)}<span>${
    esc(row ? row.title : "not on this board")}</span></button>`;

  const ledger = drawn.slice().sort((a, b) => {
    const ra = placed.get(a.provider).col - placed.get(b.provider).col;
    if (ra) return ra;
    return nameOf(a.provider).localeCompare(nameOf(b.provider))
      || nameOf(a.consumer).localeCompare(nameOf(b.consumer));
  }).map(pair => `<li><h4>${pair.off
    ? `<span class="xv">${esc(nameOf(pair.consumer))}</span> waits on a row this board does not hold`
    : `<span class="xv">${esc(nameOf(pair.provider))}</span> is upstream of <span class="xv">${esc(nameOf(pair.consumer))}</span>`}</h4>${
    pair.records.map(rec => `<p class="xpair">${chip(rec.providerId, rec.providerRow)}<span class="xrel">is waited on by</span>${
      chip(rec.consumerId, rec.consumerRow)}</p>`).join("")}</li>`).join("");

  // A live example of the grouping being the module field rather than the id,
  // named only when the board actually contains one. An id prefix that is merely
  // an abbreviation of its own module (PLT in platform, SRCH in search) is not a
  // surprise and would make the sentence untrue, so a prefix whose letters appear
  // in order inside the module name is not offered as an example.
  const abbreviates = (prefix, module) => {
    let at = 0;
    for (const ch of prefix) {
      at = module.indexOf(ch, at) + 1;
      if (!at) return false;
    }
    return true;
  };
  const mismatch = drawn.flatMap(p => p.records).find(rec => {
    if (!rec.providerRow || !rec.providerRow.module) return false;
    const prefix = String(rec.providerId).split("-")[0].toLowerCase();
    const module = String(rec.providerRow.module).toLowerCase();
    return prefix !== module && !abbreviates(prefix, module);
  });
  const idNote = mismatch
    ? ` On this board ${esc(mismatch.providerId)} sits in <b>${esc(mismatch.providerRow.module)}</b>, which its id does not say.`
    : "";

  // Every dependency the tally names must be one of the three it then counts, or
  // the sentence reads as a partition while quietly losing the epic and
  // unplaceable ones. When any were dropped, the counted denominator is stated.
  const counted = crossing + inside + offboard;
  const tally = `<p class="xtally">${total} recorded dependenc${total === 1 ? "y" : "ies"}${
    counted === total ? "" : `, ${counted} of them counted here`}. <b>${
    crossing}</b> cross${crossing === 1 ? "es" : ""} a vertical; ${inside} stay${
    inside === 1 ? "s" : ""} inside one${
    offboard ? `; ${offboard} point${offboard === 1 ? "s" : ""} at a row that is not on this board` : ""}.</p>`;

  return `<div class="card flowcard">
    <h3>Where work crosses a vertical</h3>
    <p class="meta">Left to right is dependency direction: a vertical sits left of every vertical that waits on it. Arc thickness counts dependency records, never delay.</p>
    <details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body"><p>Left to right is dependency direction: a vertical sits left of every vertical that waits on it, so the leftmost column is the upstream provider and the rightmost is the downstream consumer. A column is the longest path through the aggregated graph. Stacking inside a column, and the order of ties, is alphabetical and carries nothing. An arc's curve, and how far it bows, are geometry: an arc that skips a column is bowed clear of the nodes and arcs it would otherwise be drawn over, and that bow means nothing beyond keeping the lines apart. Only an arc's thickness counts anything, and what it counts is dependency records, not delay: a thicker arc is more records, never a longer wait. A vertical with no crossing has no place on this axis and is listed underneath instead; that is not &ldquo;least upstream&rdquo;. Two verticals that wait on each other share a column and are drawn with a dashed outline as mutual, because between them the axis is not an order.</p><p class="xnote">Only a dependency recorded as data is drawn: one stated in a note and nowhere else is invisible here, so a missing arc is a missing record, not a missing dependency. Epic rows are left out, as on the Fleet matrix, and a dependency whose target is not a row on this board is drawn to an explicit terminus rather than having a vertical guessed from its id.${idNote} Amber marks a pair where a waiting row records <code>blocked_reason_class = upstream_dependency</code>; the board records that such a row is held up by something upstream, never which dependency that is, so amber is a waiting consumer on the pair, not a claim that this arc is the cause. Dashed amber is a target that is not on this board. The Dependencies view's Clustered layout shows these same crossings row by row; this card is the same fact aggregated to the vertical.</p></div></details>
    ${tally}
    <div class="gwrap"><svg class="flowmap" width="${width}" height="${height}" role="img"
      aria-label="Cross-vertical dependency flow: ${crossing + offboard} crossing dependenc${
        crossing + offboard === 1 ? "y" : "ies"} among ${verticalCount} vertical${
        verticalCount === 1 ? "" : "s"}${modules.includes(OFF)
          ? ", plus one terminus for targets this board does not hold — that terminus is not a vertical"
          : ""}, upstream on the left. The same facts are listed in text below.">
      ${guides}${arcs}${nodes}</svg></div>
    <ul class="xroster">${roster}</ul>
    ${trayLine(allModules.filter(k => !placed.has(k)))}
    ${excluded}
    <ol class="xledger">${ledger}</ol>
  </div>`;
}
