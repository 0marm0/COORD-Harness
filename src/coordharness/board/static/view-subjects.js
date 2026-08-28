// Territories -- a directed subject-area x subject-area adjacency matrix.
//
// Why this is not a restatement of an existing view. Crossings (view-flow.js)
// filters `kind === "depends_on"` and then drops any edge that touches an epic,
// which discards most of the edges this board records. Every `parent` edge is
// aggregated nowhere else. Fleet is owner x module, Shape is module x status,
// Dependencies is row-level rather than aggregated, Context is free text.
// module x module is unoccupied, and it is the only place three facts show up:
// a subject area that only ever receives, epics that do not partition the estate
// by subject, and large areas that are wholly self-contained.
//
// One global: view_subjects(data). Everything else is closed over. The helpers
// (esc, rowById, ...) arrive on `data` and are never redefined here. Styles ship
// as the served view-subjects.css -- the board sends `style-src 'self'` with no
// unsafe-inline, so an injected <style> gets a null sheet and a style= attribute
// is dropped. Nothing here writes to the CSSOM either: every dimension is an SVG
// attribute, so the view has no paint-time dependency at all.
//
// The two relations are NEVER summed. A `parent` edge is membership -- which
// epic a row was filed under. A `depends_on` edge is a wait. One ratio over both
// has no referent, so containment is reported as two figures with two
// denominators, and every label states the kinds separately.

function view_subjects(data) {
  const { snapshot, graph, esc } = data;
  const rowById = data.rowById;

  const PITCH = 10;      // mark pitch, matched to the viewBox width below
  const MARK = 7;        // mark side
  const PER_LINE = 5;    // marks per line -> viewBox width 4*10 + 7 = 47
  const GRID_W = (PER_LINE - 1) * PITCH + MARK;
  const CAP = 24;        // above this a cell shows counts, never truncated marks
  const BAR_W = 60;
  const BAR_H = 5;

  // The sentinel carries a control character so that no real module value can
  // mint it, because every module value has its control characters replaced.
  const UNASSIGNED = "\u0000unassigned";
  const nameOf = key => key === UNASSIGNED ? "unassigned" : key;
  // esc() covers the five markup-significant characters and nothing else, so a
  // C0/C1 byte in a module value would otherwise reach the DOM invisibly.
  // U+FFFD is the standard glyph for a character that cannot be represented:
  // it asserts nothing about the work, and it keeps the sentinel unforgeable.
  const CONTROL = /[\u0000-\u001f\u007f-\u009f]/g;

  // ---------------------------------------------------------------- gather
  const allRows = (snapshot && snapshot.rows) || [];
  const rows = typeof rowById === "function"
    ? rowById()
    : new Map(allRows.map(r => [r.id, r]));
  // Snapshot ids and graph node ids agree for jobs and differ for work
  // (`work:UI-101` against `UI-101`), so the raw id is tried before the
  // stripped one -- stripping first is what once put every job in "unassigned".
  const strip = id => String(id || "").replace(/^(work|job):/, "");
  const rowFor = id => rows.get(id) || rows.get(strip(id));
  const modKeyOf = row =>
    (row && row.module) ? String(row.module).replace(CONTROL, "\ufffd") : UNASSIGNED;

  const rowCount = new Map();
  allRows.forEach(r => {
    const k = modKeyOf(r);
    rowCount.set(k, (rowCount.get(k) || 0) + 1);
  });

  // Nested, not "src<SEP>tgt": with any single separator two different pairs
  // can spell the same key ("a" -> SEP+"b" and "a"+SEP -> "b"), which merged
  // two cells and drew every one of their edges twice. A nested map has no
  // spelling to collide.
  const cells = new Map();       // src -> Map(tgt -> { parent, dep })
  const touching = new Map();    // module -> edges with either end in it
  const filed = new Map();       // module -> { inside, total } over parent edges
  const waits = new Map();       // module -> { inside, total } over depends_on
  const crossings = [];          // every edge that leaves its subject area
  let unplaced = 0, otherKind = 0, placed = 0;
  // Counted per kind, never pooled: see the crossing sentence below.
  const byKind = { parent: { placed: 0, cross: 0 }, dep: { placed: 0, cross: 0 } };
  const EMPTY = new Map();

  const bump = (map, key) => map.set(key, (map.get(key) || 0) + 1);
  const side = (map, key, isInside) => {
    const cur = map.get(key) || { inside: 0, total: 0 };
    cur.total += 1;
    if (isInside) cur.inside += 1;
    map.set(key, cur);
  };

  ((graph && graph.edges) || []).forEach(edge => {
    const kind = edge && edge.kind;
    if (kind !== "parent" && kind !== "depends_on") { otherKind += 1; return; }
    const srcRow = rowFor(edge.source);
    const tgtRow = rowFor(edge.target);
    // Guessing a subject area from an id prefix is exactly the inference this
    // view refuses: several id families disagree with `module`.
    if (!srcRow || !tgtRow) { unplaced += 1; return; }
    placed += 1;
    const src = modKeyOf(srcRow);
    const tgt = modKeyOf(tgtRow);
    const inner = cells.get(src) || new Map();
    const cell = inner.get(tgt) || { parent: 0, dep: 0 };
    if (kind === "parent") cell.parent += 1; else cell.dep += 1;
    inner.set(tgt, cell);
    cells.set(src, inner);
    bump(touching, src);
    if (tgt !== src) bump(touching, tgt);
    side(kind === "parent" ? filed : waits, src, tgt === src);
    const tally = byKind[kind === "parent" ? "parent" : "dep"];
    tally.placed += 1;
    if (tgt !== src) { tally.cross += 1; crossings.push({ kind, src, tgt, srcRow, tgtRow }); }
  });

  const cellAt = (src, tgt) =>
    (cells.get(src) || EMPTY).get(tgt) || { parent: 0, dep: 0 };

  // ------------------------------------------------------------- ordering
  // Deterministic and stated in the caption: it carries GROUPING, never rank.
  // Connected components of the undirected module graph come first (largest
  // first), then edges touching, then rows, then name. Every subject area with
  // no recorded edge is a component of one, so they are held back as a single
  // trailing block rather than as one rule per area across the whole table.
  const linked = [...rowCount.keys()].filter(m => (touching.get(m) || 0) > 0);
  const parent = new Map(linked.map(m => [m, m]));
  const find = m => {
    while (parent.get(m) !== m) { parent.set(m, parent.get(parent.get(m))); m = parent.get(m); }
    return m;
  };
  const union = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent.set(ra, rb); };
  crossings.forEach(c => { if (parent.has(c.src) && parent.has(c.tgt)) union(c.src, c.tgt); });

  const byName = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  const cmpRank = (a, b) =>
    (touching.get(b) || 0) - (touching.get(a) || 0) ||
    (rowCount.get(b) || 0) - (rowCount.get(a) || 0) ||
    byName(a, b);

  const comps = new Map();
  linked.forEach(m => {
    const r = find(m);
    if (!comps.has(r)) comps.set(r, []);
    comps.get(r).push(m);
  });
  const compList = [...comps.values()].map(members => {
    members.sort(cmpRank);
    return {
      members,
      size: members.length,
      edges: members.reduce((n, m) => n + (touching.get(m) || 0), 0),
      first: members.slice().sort(byName)[0]
    };
  }).sort((a, b) => b.size - a.size || b.edges - a.edges || byName(a.first, b.first));

  const isolated = [...rowCount.keys()]
    .filter(m => !(touching.get(m) > 0))
    .sort((a, b) => (rowCount.get(b) || 0) - (rowCount.get(a) || 0) || byName(a, b));

  const order = [];
  const blockStart = new Set();   // first member of every block after the first
  compList.forEach((c, i) => {
    if (i > 0) blockStart.add(c.members[0]);
    c.members.forEach(m => order.push(m));
  });
  if (isolated.length) {
    if (order.length) blockStart.add(isolated[0]);
    isolated.forEach(m => order.push(m));
  }

  // ---------------------------------------------------------- degenerate
  const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
  const footNote = () => {
    const bits = [];
    if (unplaced) bits.push(`${plural(unplaced, "edge", "edges")} could not be placed, because one end names no row on this board`);
    if (otherKind) bits.push(`${plural(otherKind, "edge", "edges")} carried a relation this view does not draw`);
    return bits.length ? `<p class="meta tz-foot">${esc(bits.join(". "))}.</p>` : "";
  };
  const shell = inner => `<div class="card tzcard">
    <h3>Which subject areas share structure, and which stand alone</h3>${inner}</div>`;

  if (!allRows.length || !order.length) {
    return shell(`<p class="meta">This board carries no rows, so there are no subject areas to compare.</p>`);
  }
  if (!placed) {
    return shell(`<p class="meta">${esc(plural(allRows.length, "row", "rows"))} across ${esc(plural(order.length, "subject area", "subject areas"))}, and not one recorded edge between them. There is no structure to draw yet, and an empty grid would only restate that.</p>${footNote()}`);
  }
  if (order.length === 1) {
    const only = order[0];
    const self = cellAt(only, only);
    return shell(`<p class="meta">Every row on this board -- ${esc(plural(allRows.length, "row", "rows"))} -- sits in one subject area, ${esc(nameOf(only))}, holding ${esc(plural(self.parent, "parent edge", "parent edges"))} and ${esc(plural(self.dep, "dependency", "dependencies"))}. A one-by-one matrix is a degenerate shape, so this is the sentence instead.</p>${footNote()}`);
  }

  // ------------------------------------------------------------- drawing
  const marksSvg = (cell) => {
    const total = cell.parent + cell.dep;
    if (!total) return "";
    if (total > CAP) {
      // A mark always means exactly one edge. Rather than truncate marks into a
      // sample that no longer carries that meaning, the cell switches to counts.
      return `<span class="tz-over"><b>${esc(String(cell.parent))}</b> filed<br><b>${esc(String(cell.dep))}</b> waits</span>`;
    }
    const lines = Math.ceil(total / PER_LINE);
    const h = (lines - 1) * PITCH + MARK;
    const marks = [];
    for (let i = 0; i < total; i += 1) {
      const cls = i < cell.parent ? "tz-par" : "tz-dep";
      const x = (i % PER_LINE) * PITCH;
      const y = Math.floor(i / PER_LINE) * PITCH;
      marks.push(`<rect class="tz-m ${cls}" x="${x}" y="${y}" width="${MARK}" height="${MARK}" rx="1.5"/>`);
    }
    return `<svg class="tz-marks" width="${GRID_W}" height="${h}" viewBox="0 0 ${GRID_W} ${h}" aria-hidden="true">${marks.join("")}</svg>`;
  };

  // Two margins, two denominators. Membership and a wait are never blended.
  const marginBar = (stat, insideWord, nothingWord) => {
    if (!stat || !stat.total) {
      return `<span class="tz-mrow"><span class="tz-none">&mdash;</span><span class="tz-mlab">no ${esc(nothingWord)}</span></span>`;
    }
    const w = Math.round((BAR_W * stat.inside / stat.total) * 100) / 100;
    const pct = Math.round(100 * stat.inside / stat.total);
    return `<span class="tz-mrow">` +
      `<svg class="tz-bar" width="${BAR_W}" height="${BAR_H}" viewBox="0 0 ${BAR_W} ${BAR_H}" aria-hidden="true">` +
      `<rect class="tz-in" x="0" y="0" width="${w}" height="${BAR_H}" rx="2"/>` +
      `<rect class="tz-out" x="${w}" y="0" width="${Math.round((BAR_W - w) * 100) / 100}" height="${BAR_H}" rx="2"/>` +
      `</svg><span class="tz-mlab">${pct}% ${esc(insideWord)} <i>(${esc(String(stat.inside))} of ${esc(String(stat.total))})</i></span></span>`;
  };

  const cellLabel = (src, tgt, cell) => {
    const where = `${nameOf(src)} to ${nameOf(tgt)}`;
    if (!cell.parent && !cell.dep) return `${where}: no recorded edges`;
    const bits = [];
    if (cell.parent) bits.push(plural(cell.parent, "row filed here", "rows filed here"));
    if (cell.dep) bits.push(plural(cell.dep, "dependency", "dependencies"));
    return `${where}: ${bits.join(", and separately ")}`;
  };
  const headLabel = m => nameOf(m) + ": " + plural(rowCount.get(m) || 0, "row", "rows") +
    ", " + plural(touching.get(m) || 0, "edge", "edges") + " touching it";

  const head = `<tr><th class="tz-corner" scope="col"><b>from &darr; / to &rarr;</b></th>` +
    order.map(m => `<th class="tz-col${blockStart.has(m) ? " tz-cb" : ""}" scope="col" tabindex="0" title="${esc(nameOf(m))}" aria-label="${esc(headLabel(m))}"><span class="tz-vert">${esc(nameOf(m))}</span></th>`).join("") +
    `<th class="tz-marginh" scope="col">kept inside</th></tr>`;

  const body = order.map(src => {
    const cls = blockStart.has(src) ? " tz-cb" : "";
    const rowH = `<th class="tz-rowh${cls}" scope="row" tabindex="0" aria-label="${esc(headLabel(src))}"><span class="tz-name">${esc(nameOf(src))}</span><span class="tz-n">${esc(plural(rowCount.get(src) || 0, "row", "rows"))}</span></th>`;
    const tds = order.map(tgt => {
      const cell = cellAt(src, tgt);
      const total = cell.parent + cell.dep;
      const kind = (src === tgt ? " tz-diag" : "") + (blockStart.has(tgt) ? " tz-cb" : "");
      if (!total) return `<td class="tz-cell tz-zero${kind}"></td>`;
      return `<td class="tz-cell${kind}" tabindex="0" aria-label="${esc(cellLabel(src, tgt, cell))}">${marksSvg(cell)}</td>`;
    }).join("");
    const margin = `<td class="tz-margin">` +
      marginBar(filed.get(src), "filed inside", "rows filed from here") +
      marginBar(waits.get(src), "waits inside", "dependencies from here") +
      `</td>`;
    return `<tr>${rowH}${tds}${margin}</tr>`;
  }).join("");

  const swatch = cls => `<svg class="tz-key" width="9" height="9" viewBox="0 0 9 9" aria-hidden="true"><rect class="tz-m ${cls}" x="1" y="1" width="${MARK}" height="${MARK}" rx="1.5"/></svg>`;
  const legend = `<p class="meta tz-keys">${swatch("tz-par")} one row filed under an epic` +
    `${swatch("tz-dep")} one row waiting on another` +
    `<i class="tz-key tz-diagkey" aria-hidden="true"></i> shaded diagonal: structure that stays inside one area</p>`;

  // One pooled crossing rate would be exactly the blend the read-note forbids,
  // and it hides the finding: on this board filing crosses a boundary far less
  // often than waiting does, and a single figure sits between the two.
  const crossPhrase = (tally, one, many) => tally.placed
    ? `${plural(tally.placed, one, many)} placed, ${tally.cross} of them ` +
      `(${Math.round(100 * tally.cross / tally.placed)}%) crossing into another area`
    : `no ${many} recorded`;
  const meta = `<p class="meta">Rows are the subject area an edge starts in, columns the area it points to. One mark is one recorded edge: hollow means a row filed under an epic, solid means a row waiting on another. The two are counted apart everywhere, including here and in the margin: ${esc(crossPhrase(byKind.parent, "parent edge", "parent edges"))}, and separately ${esc(crossPhrase(byKind.dep, "dependency", "dependencies"))}. The two are never added into one rate, because a filing and a wait are different claims.</p>`;

  const note = `<details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body">
    <p>Two different relations share one grid. For a hollow mark the row is the child and the column is the epic it was filed under; for a solid mark the row is the one that waits and the column is what it waits on. Membership and a wait are not the same claim, and a parent edge cannot be added to a dependency. That is why the margin carries two figures with two denominators -- how much of an area's filing stays inside it, and how much of its waiting stays inside it -- and never one blended rate, which would measure how the epics were drawn as much as anything about the work.</p>
    <p>One mark is one recorded edge. Marks are grouped by kind, hollow first; beyond that where a mark sits inside its cell is arbitrary, and only how many there are and how they are filled carries meaning. A cell holding more than ${esc(String(CAP))} edges shows counts instead of marks, because a truncated mark would stop meaning one edge.</p>
    <p>An empty cell means no edge of either kind was recorded between those two areas. It is not a claim that none should exist, nor that the two areas are unrelated in the work itself.</p>
    <p>Row and column order carries grouping, not rank. Areas that reach each other are placed together, largest group first, then by edges touching, then by rows, then by name; areas with no recorded edge at all are held back as one block at the end. Nothing here is ordered by importance, priority or time.</p>
    <p>The epics are included, unlike Fleet and Crossings which drop them, because the parent edges <em>are</em> the epic-to-child relation, and dropping epics would erase most of the recorded structure. One consequence is visible here: an epic does not necessarily hold rows from a single subject area.</p>
    <p>Containment is a ratio of recorded edges, not of work. An area with no edges of a kind is not isolated work, it is work with no recorded structure of that kind, and it gets an em dash rather than a zero. An edge count is also not a strength or a density: two areas can show the same internal count over different row counts, and the matrix does not licence comparing them. A large diagonal is often just an area whose rows all hang off one epic.</p>
    <p>There is no time in this view, and none is available to it. The board's only time data is a handful of single notes sharing one instant, so nothing drawn here is a history, a recency, a duration or a span -- and the view reads exactly the same whether or not the timeline endpoint answers at all.</p>
    <p>Subject area is the row's <em>module</em> field. The <em>group</em> field is a less complete copy of it and is not used, and the id prefix is not used because it disagrees with the module for several id families.</p>
  </div></details>`;

  const matrixCard = `<div class="card tzcard">
    <h3>Which subject areas share structure, and which stand alone</h3>
    ${meta}
    ${note}
    <div class="tz-wrap"><table class="tz"><thead>${head}</thead><tbody>${body}</tbody></table></div>
    ${legend}
    ${footNote()}
  </div>`;

  // -------------------------------------------------------------- ledger
  const relWord = c => c.kind === "parent" ? "is filed under" : "waits on";
  const ledger = crossings.length
    ? `<ul class="tz-ledger">${crossings.map(c => `<li class="tz-cross">` +
        `<button class="chip" type="button" data-row="${esc(c.srcRow.id)}" tabindex="0" title="${esc(c.srcRow.title || c.srcRow.id)}">${esc(c.srcRow.id)}<span>${esc(nameOf(c.src))}</span></button>` +
        `<span class="tz-rel">${esc(relWord(c))}</span>` +
        `<button class="chip" type="button" data-row="${esc(c.tgtRow.id)}" tabindex="0" title="${esc(c.tgtRow.title || c.tgtRow.id)}">${esc(c.tgtRow.id)}<span>${esc(nameOf(c.tgt))}</span></button>` +
      `</li>`).join("")}</ul>`
    : `<p class="meta">Not one recorded edge leaves the subject area it was raised in. Every mark in the matrix above sits on the diagonal.</p>`;

  const ledgerCard = `<div class="card tzcard">
    <h3>Every edge that leaves the subject area it was raised in</h3>
    <p class="meta">The off-diagonal marks, one by one, with nothing aggregated away. Filed-under and waits-on are listed together because they cross the same boundary, but they stay different claims and the wording says which is which. Open either end to read the row.</p>
    ${ledger}
  </div>`;

  return matrixCard + ledgerCard;
}
