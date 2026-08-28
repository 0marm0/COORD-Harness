// Order — what the board has written down, and what anything still waits for.
//
// The map already answers who is working where (Fleet), what waits on what
// (Dependencies), how the estate is distributed (Shape), which verticals cross
// (Crossings) and what anyone said about their own row (Context). None of them
// reads the timeline document across rows -- timeline.js renders it one row at
// a time inside the drawer -- and none of them crosses a dependency edge with
// the state of the row at either end. This does both.
//
// It answers the time question at the only two resolutions this data supports:
//   1. the recorded moments, exhaustively, aggregated across rows;
//   2. precedence crossed with stage -- for every recorded `depends_on`, how
//      far the thing being waited on has got.
// The second is ordering without a clock. It says what must happen before what,
// and never how long anything took, because no field carries a duration.
//
// The endpoints publish occurrence, kind and actor lane and nothing else. So
// nothing in this view is drawn to scale against time: no bar, no span, no
// axis, no connector, no mark whose size varies with anything. Where a shape
// would have to imply a measurement the view prints a sentence instead.
//
// One global: view_chronicle(data). Everything else is closed over inside the
// IIFE, deliberately: this board loads its views as classic scripts, so a
// top-level `const TL_GLYPHS` here would collide with timeline.js's top-level
// binding of the same name and the whole file would fail to parse.
//
// The stylesheet is served (view-chronicle.css), never injected: the board
// sends `style-src 'self'` with no unsafe-inline, so an injected <style> gets a
// null sheet and every rule in it is dropped without an error.

(function () {
  "use strict";

  const ORD_SCHEMA = "TimelineV1";

  // Above this many named rows a chip list stops being a list you can read and
  // starts being a wall; the per-lane table still carries the counts, and the
  // swap is stated in the caption rather than done quietly.
  const ORD_MAX_CHIPS = 24;
  // Without a ceiling the strip's width is m * 158px, so a board with real
  // history would render a six-figure-pixel SVG. Columns beyond this are
  // elided, and the elision is stated.
  const ORD_MAX_COLS = 40;

  const ORD_GUTTER = 96;
  const ORD_CELL_W = 140;
  const ORD_CELL_GAP = 18;
  const ORD_LANE_H = 34;
  const ORD_PAD = 12;
  const ORD_STAMP_H = 34;
  const ORD_PITCH = 22;
  // Marks drawn per lane per cell before the remainder is counted instead.
  const ORD_MAX_MARKS = 8;

  // Marks are drawn, not typed: a glyph font is a dependency and an image is a
  // request. Each shape is distinct in silhouette so the kinds stay apart at
  // 14px without relying on colour -- the kind is also in the title and the
  // aria-label, so colour is never the only carrier.
  const ORD_GLYPHS = {
    claim: '<path d="M7 2.4 11.6 7 7 11.6 2.4 7Z"/>',
    heartbeat: '<path d="M1.5 7h2.4l1.3-3.3L8 10.5l1.4-3.5h3.1" fill="none"/>',
    note: '<path d="M2.7 4.9h8.6M2.7 7h8.6M2.7 9.1h5.2" fill="none"/>',
    release: '<circle cx="7" cy="7" r="4.4" fill="none"/>',
    complete: '<path d="M2.5 7.2 5.8 10.5 11.5 3.7" fill="none"/>',
    verdict: '<path d="M7 2.2 12.2 11.1H1.8Z" fill="none"/>',
    block: '<circle cx="7" cy="7" r="4.4" fill="none"/><path d="M3.9 3.9 10.1 10.1" fill="none"/>',
    park: '<path d="M5.2 3.3v7.4M8.8 3.3v7.4" fill="none"/>',
    fail: '<path d="M3.9 3.9 10.1 10.1M10.1 3.9 3.9 10.1" fill="none"/>',
    unknown: '<circle cx="7" cy="7" r="2.8"/>',
  };

  // Tone is deliberately thin. Green is the accent, so it may only mean "this
  // finished"; amber and red are semantic and fixed, so they mean "waiting" and
  // "failed" whatever accent is chosen. Everything else is muted -- including
  // verdict, because the endpoint publishes that a review happened and not
  // which way it went, and colouring it would put an outcome on the page that
  // the payload does not carry.
  const ORD_KINDS = {
    claim: { glyph: "claim", tone: "" },
    heartbeat: { glyph: "heartbeat", tone: "" },
    note: { glyph: "note", tone: "" },
    release: { glyph: "release", tone: "" },
    complete: { glyph: "complete", tone: "done" },
    verdict: { glyph: "verdict", tone: "" },
    block: { glyph: "block", tone: "wait" },
    blocked: { glyph: "block", tone: "wait" },
    park: { glyph: "park", tone: "wait" },
    parked: { glyph: "park", tone: "wait" },
    attention: { glyph: "block", tone: "wait" },
    fail: { glyph: "fail", tone: "bad" },
    failed: { glyph: "fail", tone: "bad" },
    error: { glyph: "fail", tone: "bad" },
  };

  // Where a prerequisite has got to. Read off the raw status, NOT through
  // stateOf: stateOf folds any status outside its own list into "planned", so a
  // failed provider would be reported as one nobody has started yet -- which is
  // the opposite of true and exactly the kind of claim this view must not make.
  const ORD_STAGE = {
    done: "finished", complete: "finished", completed: "finished",
    running: "underway",
    planned: "notstarted", queued: "notstarted", next: "notstarted",
    blocked: "stopped", attention: "stopped", parked: "stopped", park: "stopped",
    failed: "stopped", fail: "stopped", error: "stopped",
  };

  const ORD_BANDS = [
    { key: "finished", head: "the prerequisite has finished", always: true,
      empty: "No dependency on this board waits on something that has finished." },
    { key: "underway", head: "the prerequisite is underway", always: true,
      empty: "No dependency on this board waits on something that is underway." },
    { key: "notstarted", head: "the prerequisite has not been started", always: true,
      empty: "No dependency on this board waits on something nobody has started." },
    { key: "stopped", head: "the prerequisite is stopped", always: true,
      empty: "No dependency on this board waits on something that is stopped." },
    { key: "unrecognised", head: "the prerequisite carries a stage this view does not recognise", always: false, empty: "" },
    { key: "offboard", head: "the prerequisite is not a row this board holds", always: false, empty: "" },
  ];

  // Own-property lookups only. A kind or status arriving as "constructor",
  // "__proto__" or "toString" would otherwise resolve off Object.prototype
  // instead of missing, and the renderer would read a glyph or a band off it.
  const ordOwn = (table, key) =>
    Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;

  const ordFallbackEsc = value => String(value == null ? "" : value)
    .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const ordPlural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  // ------------------------------------------------------------- the document

  // Only `at`, `kind` and `actor` are ever read. Anything else the response
  // happens to carry is dropped here as well as at the boundary, so a server
  // that leaks a field still cannot get it onto the page through this view.
  function ordEvent(raw) {
    if (!raw || typeof raw !== "object") return null;
    return {
      at: raw.at == null ? "" : String(raw.at),
      kind: raw.kind == null ? "" : String(raw.kind),
      actor: raw.actor == null ? "" : String(raw.actor),
    };
  }

  // A missing document, a wrong schema and an unreadable list are three
  // different facts and each gets its own sentence. None of them is coerced
  // into "the board has recorded nothing", which is a statement about the
  // board that the document never made.
  function ordIngest(doc) {
    if (doc == null) {
      return { ok: false, gap: "This board did not hand the map a timeline document, so nothing here can say what it has written down." };
    }
    if (typeof doc !== "object") {
      return { ok: false, gap: "The timeline document arrived in a shape this view cannot read." };
    }
    const schema = doc.schema_version == null ? "" : String(doc.schema_version);
    if (schema !== ORD_SCHEMA) {
      return { ok: false, gap: schema
        ? `The timeline document answers schema ${schema}; this view reads ${ORD_SCHEMA}.`
        : `The timeline document arrived without a schema version; this view reads ${ORD_SCHEMA}.` };
    }
    if (!Array.isArray(doc.items)) {
      return { ok: false, gap: "The timeline document arrived without a readable list of rows." };
    }
    const named = [];        // row ids the document names, in document order
    const seen = new Set();
    const events = [];       // { row, at, kind, actor }
    let dropped = 0;         // entries the document carried that this view could not read
    let unreadable = 0;      // rows named with an event list this view could not read
    doc.items.forEach(item => {
      if (!item || typeof item !== "object" || item.id == null) { dropped += 1; return; }
      const id = String(item.id);
      if (!seen.has(id)) { seen.add(id); named.push(id); }
      if (!Array.isArray(item.events)) { unreadable += 1; return; }
      item.events.forEach(raw => {
        const ev = ordEvent(raw);
        // An entry this view cannot read is COUNTED, never quietly skipped. A
        // silent drop makes the headline understate the document without
        // saying so, and "carries 1 event" over a list of four is a claim
        // about the record that the record does not support.
        if (ev) events.push({ row: id, at: ev.at, kind: ev.kind, actor: ev.actor });
        else dropped += 1;
      });
    });
    return { ok: true, named, events, dropped, unreadable };
  }

  // ------------------------------------------------------------ pure layout

  // Distinct parseable INSTANTS, ascending, capped. Grouping is by the parsed
  // moment and never by the literal string: `...296Z` and `...296+00:00` are
  // one instant written two ways, and a column each would draw a sequence out
  // of simultaneity, then count it as two in the headline and caption it as
  // "the distinct instants this board recorded". An event whose timestamp does
  // not parse gets NO column: putting it in one would assert a position in an
  // order it is not recorded in. Those are listed as text instead.
  function ordColumns(events, cap) {
    const limit = Number.isFinite(cap) && cap > 0 ? Math.floor(cap) : ORD_MAX_COLS;
    const stamps = new Map();   // parsed ms -> { ms, at, spellings }
    let unplaced = 0;
    (events || []).forEach(ev => {
      const at = String(ev && ev.at != null ? ev.at : "");
      const ms = Date.parse(at);
      if (Number.isNaN(ms)) { unplaced += 1; return; }
      const slot = stamps.get(ms);
      if (!slot) { stamps.set(ms, { ms, at, spellings: new Set([at]) }); return; }
      slot.spellings.add(at);
      // The stamp printed under a merged column is the smallest spelling, so
      // the same document always draws the same label whatever order the
      // events happen to arrive in.
      if (at < slot.at) slot.at = at;
    });
    const all = [...stamps.values()].sort((a, b) => a.ms - b.ms);
    const elided = Math.max(0, all.length - limit);
    const merged = all.filter(slot => slot.spellings.size > 1).length;
    return {
      columns: all.slice(elided).map(slot => ({ at: slot.at, ms: slot.ms, spellings: slot.spellings.size })),
      distinct: all.length, elided, unplaced, merged,
    };
  }

  // Lane order is the map's own lane order first, then anything else the data
  // carries, alphabetically. It is fixed reading order and carries nothing.
  function ordLaneOrder(keys, laneKeys) {
    const known = Array.isArray(laneKeys) ? laneKeys : [];
    const present = [...new Set((keys || []).map(k => String(k == null ? "" : k)))];
    const first = known.filter(k => present.includes(k));
    const rest = present.filter(k => !first.includes(k)).sort((a, b) => a.localeCompare(b));
    return first.concat(rest);
  }

  // Marks sharing a cell sit side by side at a fixed pitch, squeezed only if
  // they would otherwise leave the cell. Both the count of one and the count of
  // zero are guarded: no division by (k - 1) ever runs at k = 1.
  //
  // The squeeze has no lower bound on purpose. A minimum pitch would let a
  // crowded cell push its marks past the cell edge, and a mark drawn under the
  // next instant's box is a mark filed at a moment it was not recorded at.
  // Containment wins; legibility is protected by capping how many are drawn.
  function ordSpread(count, cellWidth, pitch) {
    const k = Math.max(0, Math.floor(count || 0));
    if (k === 0) return [];
    const centre = cellWidth / 2;
    if (k === 1) return [centre];
    const want = pitch > 0 ? pitch : ORD_PITCH;
    const room = Math.max(0, cellWidth - 16);
    const step = Math.min(want, room / (k - 1));
    const start = centre - (step * (k - 1)) / 2;
    const out = [];
    for (let i = 0; i < k; i += 1) out.push(start + step * i);
    return out;
  }

  function ordStripSize(columnCount, laneCount) {
    const m = Math.max(0, Math.floor(columnCount || 0));
    const n = Math.max(0, Math.floor(laneCount || 0));
    const cellsH = n * ORD_LANE_H + ORD_PAD * 2;
    const width = m > 0 ? ORD_GUTTER + m * (ORD_CELL_W + ORD_CELL_GAP) - ORD_CELL_GAP : ORD_GUTTER;
    return { width, cellsH, height: cellsH + ORD_STAMP_H };
  }

  const ordLaneY = index => ORD_PAD + index * ORD_LANE_H + ORD_LANE_H / 2;

  // ------------------------------------------------------------------ view

  function view_chronicle(data) {
    const d = data || {};
    const esc = typeof d.esc === "function" ? d.esc : ordFallbackEsc;
    const lane = typeof d.lane === "function"
      ? d.lane
      : owner => String(owner || "").split(":")[0].trim().toLowerCase();
    const LANES = d.LANES && typeof d.LANES === "object" ? d.LANES : {};
    const rows = typeof d.rowById === "function" ? d.rowById() : new Map();
    const snapshotRows = (d.snapshot && Array.isArray(d.snapshot.rows)) ? d.snapshot.rows : [];
    const edges = (d.graph && Array.isArray(d.graph.edges)) ? d.graph.edges : [];
    const sessions = (d.snapshot && Array.isArray(d.snapshot.sessions)) ? d.snapshot.sessions : [];

    // Snapshot ids and graph node ids agree for jobs and differ for work
    // (`work:UI-101` against `UI-101`), so the raw id is tried before the
    // stripped one. Chips carry the id the drawer can open, never the node id:
    // a chip carrying `work:ML-202` opens nothing at all.
    const strip = id => String(id == null ? "" : id).replace(/^(work|job):/, "");
    const rowFor = id => rows.get(id) || rows.get(strip(id)) || null;

    const doc = ordIngest(d.timeline);

    // ------------------------------------------------------------- chips
    const chip = (id, extra) => {
      const row = rowFor(id);
      const rowId = row ? row.id : strip(id);
      const status = row ? String(row.status || "").toLowerCase() : "";
      return `<button type="button" class="chip${status ? ` ${esc(status)}` : ""}" `
        + `data-row="${esc(rowId)}" tabindex="0" title="${esc(row ? row.title : rowId)}">`
        + `${esc(rowId)}${row
          ? `<span>${esc(row.title)}${extra ? ` ${esc(extra)}` : ""}</span>`
          : `<span class="meta">not on this board</span>`}</button>`;
    };

    // --------------------------------------------------------- the record
    const cards = [];
    const readnote = body =>
      `<details class="readnote"><summary>How to read this, and what it does not say</summary>`
      + `<div class="body">${body}</div></details>`;

    if (!doc.ok) {
      cards.push(`<div class="card ordcard"><h3>What this board has written down</h3>
        <p class="meta">${esc(doc.gap)}</p>
        <p class="ord-gap">Absence of a document is not a statement that nothing has happened; it is a
        statement that this view was given no record to read.</p></div>`);
    } else {
      const evs = doc.events;
      const grid = ordColumns(evs, ORD_MAX_COLS);
      const namedSet = new Set(doc.named);
      const laneOf = ev => lane(ev.actor) || "\u0000unattributed";
      const laneLabel = key => key === "\u0000unattributed"
        ? "actor not recorded"
        : ((ordOwn(LANES, key) && LANES[key].label) || key);

      const lanes = ordLaneOrder(evs.map(laneOf), Object.keys(LANES));

      // Headline. This is the finding at n = 5, and it is a sentence rather
      // than a picture because a picture of it would be four dots in one box.
      const boardTotal = snapshotRows.length;
      const headline = `The board holds ${ordPlural(boardTotal, "row", "rows")}. `
        + `The timeline document names ${doc.named.length} of them and carries `
        + `${ordPlural(evs.length, "event", "events")}`
        + (grid.distinct === 1 && evs.length > 1
          ? `, all of them stamped at one instant`
          : grid.distinct > 1 ? `, at ${ordPlural(grid.distinct, "distinct instant", "distinct instants")}` : "")
        + `. This counts written records, not work done.`;

      let strip_svg = "";
      let stripNote = "";
      if (grid.distinct > 1 && lanes.length) {
        // One column per instant. A bordered, self-contained cell per moment
        // reads as a discrete occurrence; a shared horizontal rail would read
        // as an axis, and an axis is the one lie this data would tell.
        const size = ordStripSize(grid.columns.length, lanes.length);
        const laneIndex = new Map(lanes.map((k, i) => [k, i]));
        // Bucketed once by parsed instant, so an event lands in the column for
        // the moment it carries rather than for the way that moment is spelled.
        const byMs = new Map();
        evs.forEach(ev => {
          const ms = Date.parse(ev.at);
          if (Number.isNaN(ms)) return;
          const slot = byMs.get(ms);
          if (slot) slot.push(ev); else byMs.set(ms, [ev]);
        });
        const cells = grid.columns.map((col, j) => {
          const at = col.at;
          const here = byMs.get(col.ms) || [];
          const groups = lanes.map((key, i) => {
            const all = here.filter(ev => laneOf(ev) === key);
            // Past this many in one cell the glyphs overlap into a smear that
            // reads as one bigger mark, and size means nothing here. The rest
            // are counted in place rather than dropped or drawn on top of
            // each other, and the count is reachable by keyboard like a mark.
            const mine = all.slice(0, ORD_MAX_MARKS);
            const extra = all.length - mine.length;
            const xs = ordSpread(mine.length + (extra ? 1 : 0), ORD_CELL_W, ORD_PITCH);
            const tail = extra
              ? `<text class="ord-more" x="${xs[xs.length - 1].toFixed(1)}" y="${ordLaneY(i)}"
                  text-anchor="middle" dominant-baseline="middle"><title>${esc(
                    `${ordPlural(extra, "further event", "further events")} recorded in this lane at this `
                    + `instant, not drawn individually`)}</title>+${extra}</text>`
              : "";
            return mine.map((ev, k) => {
              const kind = ev.kind.trim().toLowerCase();
              const shape = ordOwn(ORD_KINDS, kind) || { glyph: "unknown", tone: "" };
              const label = ev.kind.trim() || "kind not recorded";
              const actor = ev.actor.trim() || "actor not recorded";
              const row = rowFor(ev.row);
              const rowId = row ? row.id : strip(ev.row);
              return `<g class="ord-mark${shape.tone ? ` t-${shape.tone}` : ""}" data-row="${esc(rowId)}"
                tabindex="0" role="button"
                aria-label="${esc(`${rowId}: ${label} recorded by ${actor} at ${ev.at}`)}"
                transform="translate(${xs[k].toFixed(1)},${ordLaneY(i)})">
                <title>${esc(`${rowId} — ${label} — ${actor}`)}</title>
                <circle class="ord-hit" cx="0" cy="0" r="12"/>
                <g class="ord-glyph" transform="translate(-7,-7)" fill="currentColor" stroke="currentColor"
                  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
                  >${ordOwn(ORD_GLYPHS, shape.glyph) || ORD_GLYPHS.unknown}</g>
              </g>`;
            }).join("") + tail;
          }).join("");
          const mid = ORD_CELL_W / 2;
          const parts = String(at).split("T");
          const head = parts[0] || String(at);
          const tail = parts.length > 1 ? parts.slice(1).join("T") : "";
          const x = ORD_GUTTER + j * (ORD_CELL_W + ORD_CELL_GAP);
          return `<g class="ord-cell" transform="translate(${x},0)">
            <rect class="ord-cellbox" x="0" y="0" width="${ORD_CELL_W}" height="${size.cellsH}" rx="6"/>
            ${groups}
            <text class="ord-stamp" y="${size.cellsH + 13}" text-anchor="middle"><tspan x="${mid}"
              >${esc(head)}</tspan>${tail ? `<tspan x="${mid}" dy="11">${esc(tail)}</tspan>` : ""}</text>
          </g>`;
        }).join("");
        const labels = lanes.map((key, i) =>
          `<text class="ord-lane" x="0" y="${ordLaneY(i)}" dominant-baseline="middle">${esc(laneLabel(key))}</text>`
        ).join("");
        const summary = `${ordPlural(evs.length, "recorded event", "recorded events")} at `
          + `${ordPlural(grid.columns.length, "instant", "instants")} across `
          + `${ordPlural(lanes.length, "actor lane", "actor lanes")}; the same facts are listed in the table below.`;
        strip_svg = `<div class="ord-scroll"><svg class="ord-strip" width="${size.width}"
          height="${size.height}" viewBox="0 0 ${size.width} ${size.height}" role="img"
          aria-label="${esc(summary)}"><g class="ord-lanelabels">${labels}</g><g class="ord-cells">${cells}</g></svg></div>`;
        if (grid.elided) {
          stripNote = `<p class="ord-note">${esc(`Only the ${grid.columns.length} latest instants are drawn; `
            + `${ordPlural(grid.elided, "earlier instant is", "earlier instants are")} not shown. `
            + `The counts in the table below are over the whole document.`)}</p>`;
        }
      }

      const mergedNote = grid.merged
        ? `<p class="ord-note">${esc(`${ordPlural(grid.merged, "instant is", "instants are")} recorded under more `
          + `than one spelling of the same timestamp. Each is one column, because it is one moment, not `
          + `${grid.merged === 1 ? "two" : "several"}.`)}</p>`
        : "";
      const unplacedNote = grid.unplaced
        ? `<p class="ord-note">${esc(`${ordPlural(grid.unplaced, "event carries", "events carry")} a timestamp `
          + `this view cannot read. ${grid.unplaced === 1 ? "It is" : "They are"} counted above and given no `
          + `position, because ${grid.unplaced === 1 ? "it has" : "they have"} none.`)}</p>`
        : "";
      const droppedNote = (doc.dropped || doc.unreadable)
        ? `<p class="ord-note">${esc(`${ordPlural(doc.dropped + doc.unreadable, "further entry", "further entries")} `
          + `in the document could not be read by this view and ${doc.dropped + doc.unreadable === 1 ? "is" : "are"} `
          + `not counted here, so these figures are a floor.`)}</p>`
        : "";

      // ------------------------------------------------------ the ledger
      const boardLanes = snapshotRows.map(r => (r.owner ? lane(r.owner) : "\u0000unowned"));
      const ledgerKeys = ordLaneOrder(
        boardLanes.filter(k => k !== "\u0000unowned").concat(evs.map(laneOf)),
        Object.keys(LANES));
      const ledger = ledgerKeys.map(key => {
        const held = snapshotRows.filter(r => r.owner && lane(r.owner) === key).length;
        const mine = evs.filter(ev => laneOf(ev) === key);
        return { label: laneLabel(key), held, names: new Set(mine.map(ev => ev.row)).size, events: mine.length };
      });
      const unowned = snapshotRows.filter(r => !r.owner).length;
      if (unowned) ledger.push({ label: "not attributed", held: unowned, names: null, events: null });

      const ledgerRows = ledger.map(r => `<tr><td>${esc(r.label)}</td><td>${r.held}</td>`
        + `<td>${r.names == null ? "—" : r.names}</td>`
        + `<td>${r.events == null ? "—" : r.events}</td></tr>`).join("");
      // A header row over an empty body is a shape, not a statement: it draws a
      // table where there is nothing to count. Say so instead.
      const table = !ledger.length
        ? `<p class="ord-note">${esc("No row and no event here carries an actor lane, so there is no per-lane count to give.")}</p>`
        : `<div class="tablewrap"><table class="ord-reach">
        <thead><tr><th>Actor lane</th><th>Rows the board holds</th><th>Rows this document names</th>
        <th>Events recorded</th></tr></thead><tbody>${ledgerRows}</tbody></table></div>`;

      const namedIds = doc.named;
      const chips = namedIds.length && namedIds.length <= ORD_MAX_CHIPS
        ? `<p class="ord-chipnote">${esc(`The ${ordPlural(namedIds.length, "row", "rows")} the document names:`)}</p>`
          + `<div class="chips">${namedIds.map(id => chip(id)).join("")}</div>`
        : namedIds.length
          ? `<p class="ord-chipnote">${esc(`The document names ${ordPlural(namedIds.length, "row", "rows")}, `
            + `more than the ${ORD_MAX_CHIPS} this card lists individually, so only the per-lane counts are shown.`)}</p>`
          : `<p class="ord-chipnote">${esc("The document names no rows at all.")}</p>`;

      const sessionNote = sessions.length
        ? ` The board publishes ${ordPlural(sessions.length, "session", "sessions")}, but no row and no event `
          + `references a session id — only a coarse actor string — so nothing here can be attributed to a `
          + `particular session.`
        : "";

      cards.push(`<div class="card ordcard"><h3>What this board has written down</h3>
        <p class="meta">${esc(headline)}</p>
        ${strip_svg
          ? `<p class="meta">Columns are the distinct instants this board recorded, left to right in the order
             they occurred. Column spacing is fixed layout: the document carries occurrence, kind and actor
             lane and nothing else, so nothing here says how long anything took, and no mark's size means
             anything.</p>`
          : `<p class="meta">${esc(grid.distinct === 1
              ? "Every event this document carries shares one instant, so there is no order to draw — this is one recorded moment, not a sequence."
              : "The document records no placeable event, so there is nothing here to draw.")}</p>`}
        ${readnote(`<p>${esc("A row absent from this document is a row the board wrote nothing about, not a row where nothing happened.")}</p>
          <p>${esc("Lane order is fixed and carries nothing. Where two events share an instant and a lane they are placed side by side in document order, which is arbitrary: equal timestamps carry no order between them.")}</p>
          <p>${esc("A glyph is a label for a kind, never a measure. A verdict mark records that a review happened and never which way it went, which is why it stays muted.")}</p>
          <p>${esc("The document names a row by id; owner is a separate field on the row, so the actor on an event and the owner of the row need not be the same person. Fleet counts rows per agent; this counts records per actor. They are different fields.")}${esc(sessionNote)}</p>
          <p>${esc("No context item on this board records an artifact against its declared done-signal, so a declared done-signal is a stated path and never evidence of completion.")}</p>`)}
        ${strip_svg}${stripNote}${mergedNote}${unplacedNote}${droppedNote}
        ${table}${chips}</div>`);
    }

    // ------------------------------------------------- precedence x stage
    const deps = edges.filter(e => e && e.kind === "depends_on");
    if (!deps.length) {
      cards.push(`<div class="card ordcard"><h3>What each waiting row is waiting for</h3>
        <p class="meta">The board records no dependencies, so it carries no ordering between rows at all.</p></div>`);
    } else {
      const bucket = new Map(ORD_BANDS.map(b => [b.key, []]));
      let skippedEpic = 0;
      const strikes = [];

      deps.forEach(edge => {
        const consumer = rowFor(edge.source);
        const provider = rowFor(edge.target);
        // Epics are containers, not work; the Fleet matrix drops them, Crossings
        // drops them, and so does this.
        if ((consumer && consumer.bucket === "epic") || (provider && provider.bucket === "epic")) {
          skippedEpic += 1;
          return;
        }
        const providerStatus = provider ? String(provider.status || "").trim().toLowerCase() : "";
        const band = !provider ? "offboard" : (ordOwn(ORD_STAGE, providerStatus) || "unrecognised");
        const consumerId = consumer ? consumer.id : strip(edge.source);
        const providerId = provider ? provider.id : strip(edge.target);
        bucket.get(band).push({ consumerId, providerId, providerStatus, consumer, provider });
        if (consumer && String(consumer.status || "").toLowerCase() === "running" && band === "stopped") {
          strikes.push({ consumerId, providerId, providerStatus });
        }
      });

      const drawn = ORD_BANDS.reduce((n, b) => n + bucket.get(b.key).length, 0);
      const finished = bucket.get("finished").length;

      const bandsHtml = ORD_BANDS.map(spec => {
        const list = bucket.get(spec.key);
        if (!list.length && !spec.always) return "";
        list.sort((a, b) => a.consumerId.localeCompare(b.consumerId));
        const body = list.length
          ? list.map(p => `<p class="ord-pair">${chip(p.consumerId)}`
              + `<span class="ord-rel">waits on</span>`
              + `${chip(p.providerId, p.providerStatus ? `— ${p.providerStatus}` : "")}</p>`).join("")
          : `<p class="ord-empty">${esc(spec.empty)}</p>`;
        return `<li class="ord-band"><h4>${esc(spec.head)}<span class="ord-n">${list.length}</span></h4>${body}</li>`;
      }).join("");

      const strikeLine = !strikes.length ? ""
        : strikes.length <= 3
          ? `<p class="ord-strike">${strikes.map(s => esc(`${s.consumerId} is underway while ${s.providerId}, `
            + `which it is recorded as depending on, is ${s.providerStatus || "stopped"}.`)).join(" ")}</p>`
          : `<p class="ord-strike">${esc(`${strikes.length} rows are underway while something they are recorded as `
            + `depending on is stopped; they are listed in the stopped band below.`)}</p>`;

      const epicLine = skippedEpic
        ? ` ${ordPlural(skippedEpic, "dependency touches", "dependencies touch")} an epic and `
          + `${skippedEpic === 1 ? "is" : "are"} not drawn.`
        : "";

      const tally = `The board records ${ordPlural(deps.length, "dependency", "dependencies")} between rows. `
        + `Of the ${drawn} drawn here, ${finished === 0 ? "none" : finished} `
        + `${finished === 1 ? "has" : "have"} a prerequisite that has finished.${epicLine}`;

      cards.push(`<div class="card ordcard"><h3>What each waiting row is waiting for</h3>
        <p class="ord-tally">${esc(tally)}</p>
        ${strikeLine}
        <p class="meta">Band order is fixed for reading. It is not a timeline, not a schedule and not a ranking
        of urgency. Stage is where a prerequisite has got to, never when it got there.</p>
        ${readnote(`<p>${esc("Within a band the order is the consumer's id, alphabetically, and carries nothing.")}</p>
          <p>${esc("Only a dependency recorded as data is drawn, so a missing pair is a missing record, not a missing dependency.")}</p>
          <p>${esc("A row that is underway while its prerequisite is stopped is stated as the fact it is, not called out of order: the board carries no order it could be out of.")}</p>
          <p>${esc("Stage is read from the row's own status. A status this view has no band for keeps its literal value and is shown apart rather than folded into one of the four.")}</p>`)}
        <ol class="ord-bands">${bandsHtml}</ol>
        ${ordFreeSet(snapshotRows, deps, strip, chip, esc)}</div>`);
    }

    return cards.join("");
  }

  // Rows nobody has started that the board records no prerequisite for. This is
  // deliberately not a fifth band: it is a different kind of statement -- an
  // absence of records, not a stage -- and "no recorded prerequisite" is not
  // "ready", it means the board was never told of one.
  function ordFreeSet(snapshotRows, deps, strip, chip, esc) {
    const consumers = new Set(deps.map(e => strip(e.source)));
    const planned = snapshotRows.filter(r =>
      r.bucket !== "epic" && String(r.status || "").toLowerCase() === "planned");
    if (!planned.length) return "";
    const free = planned.filter(r => !consumers.has(String(r.id)));
    if (!free.length) {
      return `<p class="ord-free">${esc(`Every one of the ${ordPlural(planned.length, "row", "rows")} nobody `
        + `has started has at least one recorded prerequisite.`)}</p>`;
    }
    const head = `<p class="ord-free">${esc(`${free.length} of the `
      + `${ordPlural(planned.length, "row", "rows")} nobody has started ${free.length === 1 ? "has" : "have"} `
      + `no recorded prerequisite. That is an absence of records, not a statement that they are ready to run.`)}</p>`;
    return free.length <= 24
      ? `${head}<div class="chips">${free.map(r => chip(r.id)).join("")}</div>`
      : head;
  }

  globalThis.view_chronicle = view_chronicle;

  // Exposed on the one global rather than as separate bindings, so the pure
  // layout and ingest maths can be exercised outside a browser without this
  // file leaking a second name into the page's shared top-level scope.
  view_chronicle.__internals = { ordColumns, ordLaneOrder, ordSpread, ordStripSize, ordIngest, ordLaneY };
})();
