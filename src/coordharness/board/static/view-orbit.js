// Orbit — one row, and everything the board records around it.
//
// The map already answers four questions at estate scale. Fleet: who is working
// where. Shape: how the estate is distributed. Crossings: which verticals wait on
// which, aggregated. Dependencies: the whole graph, row by row. Context: what
// anyone wrote down. None of them centres a SINGLE row and draws its surroundings
// as a picture; the drawer is the only per-row surface and it answers this as five
// flat chip lists with no relation between them. Reading "waits on PLT-302" and
// "alongside PLT-301, PLT-303" as one fact — that PLT-302 is family, or is not —
// costs a click into another row and a look at its parent. This card draws it.
//
// The payload it needs from cockpit.js:
//   { snapshot, graph, context, timeline, esc, stateOf, lane, LANES, rowById,
//     contextById, orbitRoot?, selected? }
// `timeline` may be absent on an older board; that is handled as a stated gap.
// `orbitRoot` is the recentre target — a module-level let in cockpit.js written by
// the [data-orbit] branch of wireNavigation(). If cockpit.js does not pass it, the
// view falls back to reading it off the panel element's dataset, and failing that
// to `selected`, and failing that to the row with the most recorded relationships.
// Whichever of the three was used is NAMED in the caption every render, so the
// reader is never looking at a default they mistook for a choice.
//
// One global: view_orbit(data). Everything else is closed over. The helpers come
// in on `data` and are never redefined here. Styles ship as view-orbit.css, linked
// from cockpit.html: the board sends style-src 'self' with no unsafe-inline, so an
// injected <style> gets a null sheet and every rule is dropped in silence, and a
// style= attribute never lands. SVG geometry (cx, r, d, x1) is an attribute, not a
// style, so nothing here needs the data-w channel and no colour appears in either
// file — tokens only, and semantic amber/red never follow the accent.

function view_orbit(data) {
  const { snapshot, context, esc, stateOf, lane, LANES, rowById, contextById } = data;
  const timeline = data.timeline;

  const rows = rowById();
  const ctx = contextById();
  const list = value => Array.isArray(value) ? value.filter(Boolean).map(String) : [];
  const byId = (a, b) => String(a).localeCompare(String(b));

  // ------------------------------------------------------------- empty board
  if (!rows.size) {
    return `<div class="card orbitcard"><h3>A row in its world</h3>
      <p class="meta">This board holds no rows, so there is nothing to centre. That is an
      empty board, not an empty orbit.</p></div>`;
  }

  // ------------------------------------------------ relationships, per row
  // Ring one is only what the row itself names: parent, children, depends_on,
  // dependents. Pure siblings are NOT on the ring — see the note below.
  const relationsOf = item => {
    if (!item) return { parent: [], children: [], waitsOn: [], waitedOnBy: [] };
    return {
      parent: item.parent ? [String(item.parent)] : [],
      children: list(item.children).slice().sort(byId),
      waitsOn: list(item.depends_on).slice().sort(byId),
      waitedOnBy: list(item.dependents).slice().sort(byId),
    };
  };
  const degreeOf = item => {
    const r = relationsOf(item);
    return r.parent.length + r.children.length + r.waitsOn.length + r.waitedOnBy.length;
  };

  // ------------------------------------------------------------ root choice
  const known = id => !!id && (rows.has(id) || ctx.has(id));
  const fromPanel = () => {
    if (typeof document === "undefined") return null;
    const panel = document.querySelector("#orbit");
    return (panel && panel.dataset && panel.dataset.orbitRoot) || null;
  };
  const requested = data.orbitRoot || fromPanel();
  const bestDefault = [...ctx.values()]
    .sort((a, b) => (degreeOf(b) - degreeOf(a)) || byId(a.id, b.id))[0];
  let rootId = null;
  let rootSource = "";
  if (known(requested)) {
    rootId = String(requested);
    rootSource = "you clicked its recentre control";
  } else if (known(data.selected)) {
    rootId = String(data.selected);
    rootSource = "it is the row open in the drawer";
  } else if (bestDefault) {
    rootId = bestDefault.id;
    rootSource = `nothing was chosen, so this is the row naming the most relationships (${
      degreeOf(bestDefault)})`;
  } else {
    rootId = [...rows.keys()].sort(byId)[0];
    rootSource = "nothing was chosen and no row records a relationship, so this is the first row by id";
  }

  const rootRow = rows.get(rootId) || null;
  const rootCtx = ctx.get(rootId) || null;
  const titleOf = id => {
    const row = rows.get(id);
    return row && row.title ? String(row.title) : "";
  };

  // --------------------------------------------------------- the root picker
  // Usable with nothing selected: the five containers, plus whatever is centred.
  const pickerIds = [...rows.values()]
    .filter(row => row.bucket === "epic").map(row => row.id).sort(byId);
  if (!pickerIds.includes(rootId)) pickerIds.push(rootId);
  const pickerHtml = `<div class="ob-picker">${pickerIds.map(id => {
    const row = rows.get(id);
    return `<button class="chip ob-pick ${row ? esc(stateOf(row)) : ""}${
      id === rootId ? " current" : ""}" data-orbit="${esc(id)}" tabindex="0"
      title="Centre the orbit on ${esc(id)}">${esc(id)}<span>${
      esc(titleOf(id) || "not on this board")}</span></button>`;
  }).join("")}</div>`;

  // ----------------------------------------------------- board-wide kin tally
  // Three-valued, not two. A dependency is inside a family, across a family
  // boundary, or unclassifiable because a row on one end records no parent at
  // all. Collapsing the third into the second is what would let TASK-3, which
  // simply has no parent, be counted as a boundary crossing.
  let famInside = 0, famAcross = 0, famUnknown = 0;
  ctx.forEach(item => list(item.depends_on).forEach(target => {
    const other = ctx.get(target);
    if (!item.parent || !other || !other.parent) { famUnknown += 1; return; }
    if (other.parent === item.parent) famInside += 1; else famAcross += 1;
  }));
  const famTotal = famInside + famAcross + famUnknown;
  const famSentence = famTotal === 0
    ? "This board records no dependency at all, so there is no family question to ask of one."
    : `Across this board ${famTotal} dependenc${famTotal === 1 ? "y is" : "ies are"} recorded: ${
        famInside} stay${famInside === 1 ? "s" : ""} inside one initiative, ${famAcross} cross${
        famAcross === 1 ? "es" : ""} a boundary, and ${famUnknown} cannot be classified either way because a row on one end records no parent.`;

  // ------------------------------------------- rows this endpoint cannot see
  const missingFromContext = [...rows.keys()].filter(id => !ctx.has(id)).length;

  // ------------------------------------------------------------- time, or not
  // The only time data that exists. It is never drawn. It is reported as text so
  // that its absence is a stated fact rather than a silence.
  const timeLine = (() => {
    if (!timeline || !Array.isArray(timeline.items)) {
      return "This board served no timeline feed, so there is no time data here either way. Nothing on this picture is time.";
    }
    // The feed keys on ids, and nothing guarantees every one of them is a row this
    // snapshot holds. Counting all of them against the row count is how you get
    // "3 events across 3 of its 2 rows" — a fraction that cannot be true, from a
    // numerator and a denominator drawn from different sets. So the fraction is
    // computed over rows this board actually holds, and events belonging to
    // anything else are named separately instead of being folded in.
    const items = timeline.items.filter(Boolean);
    const onBoard = items.filter(item => rows.has(item.id));
    const stray = items.reduce((n, item) => n + (rows.has(item.id) ? 0 : list(item.events).length), 0);
    const events = onBoard.reduce((n, item) => n + list(item.events).length, 0);
    const withAny = onBoard.filter(item => list(item.events).length).length;
    const stamps = new Set();
    onBoard.forEach(item => (Array.isArray(item.events) ? item.events : [])
      .forEach(event => stamps.add(String(event && event.at))));
    const strayLine = stray
      ? ` A further ${stray} event${stray === 1 ? "" : "s"} in that feed belong${stray === 1 ? "s" : ""} to ids this board does not hold as rows.`
      : "";
    const mine = onBoard.find(item => item.id === rootId);
    const mineCount = mine ? (Array.isArray(mine.events) ? mine.events.length : 0) : 0;
    return `Nothing on this picture is time. The board's timeline holds ${events} event${
      events === 1 ? "" : "s"} across ${withAny} of its ${rows.size} rows${
      stamps.size === 1 && events > 1 ? ", all at a single instant" : ""}, and ${
      mineCount ? `${mineCount} of them belong${mineCount === 1 ? "s" : ""} to this row` : "none of them belongs to this row"
      }.${strayLine} There is no duration and no ordering to draw.`;
  })();

  // ---------------------------------------------- rows outside /api/v1/context
  if (!rootCtx) {
    const facts = rootRow ? [
      ["status", rootRow.status], ["vertical", rootRow.module],
      ["owner", rootRow.owner || "no owner recorded"],
      ["step", rootRow.current_step || "no step recorded"],
    ] : [];
    return `<div class="card orbitcard">
      <h3>A row in its world</h3>
      <p class="meta">Centred on <b>${esc(rootId)}</b>${
        rootRow && rootRow.title ? ` &mdash; ${esc(rootRow.title)}` : ""}, because ${esc(rootSource)}.</p>
      <p class="ob-empty">${rootRow
        ? `This row is not described by the board's context feed, which holds ${ctx.size} of the board's ${rows.size} rows and no job row among them. There is nothing to orbit &mdash; not because the row stands alone, but because this endpoint does not describe it.`
        : "No row on this board carries that id."}</p>
      ${facts.length ? `<ul class="ob-facts">${facts.map(([k, v]) =>
        `<li><span>${esc(k)}</span>${esc(String(v ?? ""))}</li>`).join("")}</ul>` : ""}
      ${pickerHtml}
      <p class="legend">${esc(timeLine)}</p>
    </div>`;
  }

  // ------------------------------------------------------------- the rings
  const rel = relationsOf(rootCtx);

  // One row can be named by two kinds at once — a child that also waits on its
  // parent is the ordinary case, not a malformed feed — and the raw lists would
  // then put ONE row at two angles on the same ring. That draws two rows where
  // the board records one, inflates every wedge's share (the wedge would be
  // counting relationship records while the caption says it counts rows), and
  // hands the reader two "centre" controls for the same id. So a row is drawn
  // once, in the first wedge that names it in the fixed clockwise order, and the
  // other kinds it carries are written beside it in the list rather than drawn.
  // A row that names ITSELF is dropped from the ring for the same reason: it is
  // already the centre, and nothing is one relationship away from itself.
  // Neither is silently absorbed — both are stated in the caption when they occur.
  const KINDS = [["parent", "belongs to"], ["waitedOnBy", "is waited on by"],
                 ["children", "contains"], ["waitsOn", "waits on"]];
  const alsoIn = new Map();   // id -> the later kinds that also name it
  const selfNamed = [];
  const drawn = { parent: [], waitedOnBy: [], children: [], waitsOn: [] };
  const claimed = new Set();
  KINDS.forEach(([key, verb]) => rel[key].forEach(id => {
    if (id === rootId) {
      if (!selfNamed.includes(verb)) selfNamed.push(verb);
      return;
    }
    if (claimed.has(id)) {
      if (!alsoIn.has(id)) alsoIn.set(id, []);
      if (!alsoIn.get(id).includes(verb)) alsoIn.get(id).push(verb);
      return;
    }
    claimed.add(id);
    drawn[key].push(id);
  }));

  const ring1Ids = [...drawn.parent, ...drawn.waitedOnBy, ...drawn.children, ...drawn.waitsOn];
  const onRing1 = new Set(ring1Ids);
  const pureSiblings = list(rootCtx.siblings)
    .filter(id => !onRing1.has(id) && id !== rootId).sort(byId);

  const rootParent = rootCtx.parent ? String(rootCtx.parent) : "";
  // Three-valued kin state for a dependency neighbour, as above.
  const kinOf = id => {
    if (!rootParent) return "suppressed";
    const other = ctx.get(id);
    if (!other || !other.parent) return "unrecorded";
    return other.parent === rootParent ? "kin" : "outside";
  };
  const isDep = id => rel.waitsOn.includes(id) || rel.waitedOnBy.includes(id);

  // Ring two: exactly two derived sets, both named. Nothing else is promoted.
  const ring2 = new Map();  // id -> { anchor, why }
  const addR2 = (id, anchor, why) => {
    if (!id || id === rootId || onRing1.has(id) || ring2.has(id)) return;
    ring2.set(id, { anchor, why });
  };
  ring1Ids.forEach(id => {
    if (!isDep(id) || kinOf(id) !== "outside") return;
    const other = ctx.get(id);
    addR2(String(other.parent), id, `it contains ${id}, which is a dependency outside this row's own initiative`);
  });
  rel.waitsOn.forEach(id => {
    const other = ctx.get(id);
    if (!other) return;
    list(other.depends_on).sort(byId).forEach(next =>
      addR2(next, id, `${id} is itself waiting on it`));
  });

  // ------------------------------------------------------------------ layout
  const SECTORS = [
    { key: "parent", label: "belongs to", verb: "belongs to", ids: drawn.parent },
    { key: "up", label: "waited on by", verb: "is waited on by", ids: drawn.waitedOnBy },
    { key: "children", label: "contains", verb: "contains", ids: drawn.children },
    { key: "down", label: "waits on", verb: "waits on", ids: drawn.waitsOn },
  ].filter(sector => sector.ids.length);

  // Draw into the estate the active panel actually owns. The old natural-size
  // SVG left most desktop cards blank and became a second scrolling viewport on
  // mobile. Recomputing the viewBox keeps SVG text at its declared size.
  const n1 = ring1Ids.length;
  const hasR2 = ring2.size > 0;
  const panel = typeof document === "undefined" ? null : document.querySelector("#orbit");
  const panelWidth = Number(panel?.getBoundingClientRect().width || 0);
  const viewportWidth = typeof window === "undefined" ? 0 : Number(window.innerWidth || 0);
  const rawWidth = panelWidth || viewportWidth || 640;
  const compact = rawWidth <= 720;
  const width = Math.max(288, Math.floor(rawWidth - (compact ? 24 : 28)));
  const height = compact
    ? Math.max(320, Math.min(390, Math.round(width * .98)))
    : Math.max(480, Math.min(720, Math.round(width * .54)));
  const ringGap = compact ? 44 : 72;
  const labelRoomX = compact ? 48 : 96;
  const labelRoomY = compact ? 42 : 54;
  const ringLimit = Math.max(76, Math.min(width / 2 - labelRoomX, height / 2 - labelRoomY));
  const desiredR1 = compact
    ? Math.min(92, Math.max(66, 11 * n1 + 44))
    : Math.min(260, Math.max(156, width * .19, 18 * n1 + 90));
  const R1 = Math.max(54, Math.min(desiredR1, ringLimit - (hasR2 ? ringGap : 0)));
  const R2 = R1 + ringGap;
  const cx = width / 2, cy = height / 2;

  // 0 degrees is twelve o'clock and degrees run clockwise.
  const at = (r, deg) => {
    const rad = (deg - 90) * Math.PI / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  };
  const n = value => Number(value).toFixed(1);

  // A sector's arc is its share of the ring, so every ring-one node ends up the
  // same angle from its neighbour. Order inside a sector is id ascending and is
  // stated as such: the panel is rebuilt every five seconds, and a shape that
  // reshuffles on each repaint reads as movement, which this data cannot support.
  // With more than one wedge a row sits in the middle of its own wedge, so it is
  // never drawn on a divider. With only one wedge there is no divider to avoid, so
  // the first row sits at the top instead of half a step past it — otherwise a row
  // with a single relationship lands at the bottom for no reason a reader can see.
  const lone = SECTORS.length === 1;
  let cursor = 0;
  const placedSectors = SECTORS.map(sector => {
    const span = n1 ? (sector.ids.length / n1) * 360 : 0;
    const start = cursor;
    cursor += span;
    const step = span / sector.ids.length;  // sector.ids.length >= 1 by construction
    return Object.assign({}, sector, {
      start, span, mid: start + span / 2,
      placed: sector.ids.map((id, i) => ({ id, deg: start + (i + (lone ? 0 : 0.5)) * step })),
    });
  });

  const angleOf = new Map();
  placedSectors.forEach(sector => sector.placed.forEach(p => angleOf.set(p.id, p.deg)));

  // A ring-two row sits outward of the ring-one row it came from. When several
  // share one anchor they are fanned apart; the fan is separation, not a quantity.
  const anchorGroups = new Map();
  [...ring2.keys()].sort(byId).forEach(id => {
    const anchor = ring2.get(id).anchor;
    if (!anchorGroups.has(anchor)) anchorGroups.set(anchor, []);
    anchorGroups.get(anchor).push(id);
  });
  const ring2Placed = [];
  anchorGroups.forEach((ids, anchor) => {
    const base = angleOf.has(anchor) ? angleOf.get(anchor) : 0;
    ids.forEach((id, i) => ring2Placed.push({
      id, anchor, deg: base + (i - (ids.length - 1) / 2) * 13,
    }));
  });

  // Past roughly fourteen neighbours the outward labels collide, so they are
  // dropped to hover instead of being drawn on top of one another, and the card
  // says that it did that.
  const LABEL_CAP = 14;
  const labelled = n1 <= LABEL_CAP;

  // ------------------------------------------------------------------ drawing
  const ringsSvg = n1 ? `<g class="ob-rings">
    <circle class="ob-ring" cx="${n(cx)}" cy="${n(cy)}" r="${n(R1)}"></circle>${
    hasR2 ? `<circle class="ob-ring" cx="${n(cx)}" cy="${n(cy)}" r="${n(R2)}"></circle>` : ""}
  </g>` : "";

  const sectorSvg = placedSectors.length > 1 ? `<g class="ob-sectors">${
    placedSectors.map(sector => {
      const a = at(46, sector.start), b = at(R1 + 8, sector.start);
      return `<line class="ob-div" x1="${n(a.x)}" y1="${n(a.y)}" x2="${n(b.x)}" y2="${n(b.y)}"></line>`;
    }).join("")}</g>` : "";

  const wedgeKey = placedSectors.length
    ? `<p class="ob-key">Wedges, clockwise from the top: ${placedSectors.map(sector =>
        `<b>${esc(sector.label)}</b> (${sector.ids.length})`).join(" &middot; ")}. A kind with nothing in it has no wedge.</p>`
    : "";

  // Kin arcs run from a dependency neighbour back to the parent node. They are an
  // actual recorded relationship — a shared parent — not decoration.
  // The arc runs to the parent node, so the parent itself can never be one end of
  // it: a row that is both this row's parent and its dependency would otherwise
  // get a zero-length path drawn at its own position, which is a mark with no
  // meaning rather than a relationship.
  const parentId = drawn.parent[0] || null;
  const kinSvg = (parentId && rootParent) ? `<g class="ob-kins">${
    ring1Ids.filter(id => id !== parentId && isDep(id) && kinOf(id) === "kin").map(id => {
      const a = at(R1, angleOf.get(id)), b = at(R1, angleOf.get(parentId));
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const ctrl = { x: cx + (mid.x - cx) * 0.55, y: cy + (mid.y - cy) * 0.55 };
      return `<path class="ob-kin" d="M${n(a.x)} ${n(a.y)} Q${n(ctrl.x)} ${n(ctrl.y)} ${
        n(b.x)} ${n(b.y)}"><title>${esc(`${id} and ${rootId} share the parent ${parentId}`)}</title></path>`;
    }).join("")}</g>` : "";

  // Containment has no arrowhead: it is not an ordering and must not be drawn as
  // one. Only the two dependency sectors carry a direction.
  const spokeSvg = `<g class="ob-spokes">${placedSectors.map(sector =>
    sector.placed.map(p => {
      // The inner end clears the root's own label; the outer end stops short of the
      // node. "waits on" points inward at the root, "waited on by" points outward,
      // and the head sits at the end of its line rather than part-way along it.
      const INNER = 54, OUTER = R1 - 15, HEAD = 9;
      const cls = sector.key === "down" ? "down" : sector.key === "up" ? "up" : "kinline";
      const directed = sector.key === "down" || sector.key === "up";
      const tipR = sector.key === "down" ? INNER : OUTER;
      const baseR = sector.key === "down" ? INNER + HEAD : OUTER - HEAD;
      const a = at(directed ? (sector.key === "down" ? OUTER : INNER) : INNER, p.deg);
      const b = at(directed ? baseR : OUTER, p.deg);
      let head = "";
      if (directed) {
        const tip = at(tipR, p.deg), base = at(baseR, p.deg);
        const dx = tip.x - base.x, dy = tip.y - base.y;
        const len = Math.hypot(dx, dy) || 1;
        const ux = dx / len, uy = dy / len;
        head = `<path class="ob-head ${cls}" d="M${n(tip.x)} ${n(tip.y)} L${
          n(base.x - uy * 4.5)} ${n(base.y + ux * 4.5)} L${
          n(base.x + uy * 4.5)} ${n(base.y - ux * 4.5)} Z"></path>`;
      }
      return `<line class="ob-spoke ${cls}" x1="${n(a.x)}" y1="${n(a.y)}" x2="${
        n(b.x)}" y2="${n(b.y)}"></line>${head}`;
    }).join("")).join("")}</g>`;

  const nodeSvg = (id, deg, radius, kind, sector) => {
    const row = rows.get(id);
    const p = at(radius, deg);
    // stateOf is the house bucketing and is kept, so this pip agrees with the chip
    // for the same row everywhere else on the page. It has no bucket for "failed"
    // and folds it into planned, which would draw a failed row as an ordinary one,
    // so the raw status is carried alongside it rather than instead of it.
    const state = row ? stateOf(row) : "";
    const raw = row ? String(row.status || "").toLowerCase() : "";
    const pipCls = `${state}${raw === "failed" ? " failed" : ""}`;
    const right = Math.sin(deg * Math.PI / 180) >= 0;   // 0-180 is the clockwise right half
    const label = at(radius + 22, deg);
    const markKey = row ? lane(row.owner) : "";
    const mark = (LANES && LANES[markKey] && LANES[markKey].mark) ? LANES[markKey].mark : null;
    const kin = kind === "ring1" && isDep(id) ? kinOf(id) : "";
    // A node sits in one wedge, so the wedge's verb alone would understate a row
    // the board names twice; the second kind rides along in the label and the
    // spoken name, and is written out in the list as well.
    const second = alsoIn.get(id);
    const relPhrase = (sector ? sector.verb : (ring2.has(id) ? ring2.get(id).why : ""))
      + (sector && second ? ` and also this row ${second.join(" and ")} it` : "");
    const kinPhrase = kin === "kin" ? ", inside this row's own initiative"
      : kin === "outside" ? ", outside this row's own initiative"
      : kin === "unrecorded" ? ", family not recorded" : "";
    const aria = `${id}${row && row.title ? `, ${row.title}` : ", not on this board"}${
      relPhrase ? `, ${relPhrase}` : ""}${kinPhrase}${row && raw ? `, ${raw}` : ""}${
      row && row.owner ? `, ${row.owner}` : ""}`;
    const full = `${id}${row && row.title ? ` — ${row.title}` : " — not a row on this board"}${
      relPhrase ? `\n${relPhrase}` : ""}${kinPhrase}`;
    const r = kind === "ring1" ? 12 : 9;
    return `<g class="ob-node ${kind}${row ? "" : " offboard"}${kin ? ` ${kin}` : ""}"
      data-row="${esc(id)}" tabindex="0" role="button" aria-label="${esc(aria)}"
      transform="translate(${n(p.x)},${n(p.y)})">
      <circle class="ob-disc" r="${r}"></circle>${
      row ? `<circle class="ob-pip ${esc(pipCls)}" cx="${r - 3}" cy="${-(r - 3)}" r="3"></circle>` : ""}${
      kin === "unrecorded" ? `<circle class="ob-tick" cx="${-(r - 3)}" cy="${-(r - 3)}" r="3.5"></circle>` : ""}${
      mark ? `<image class="ob-mark" href="${esc(mark)}" x="${-(r + 10)}" y="${r - 6}" width="12" height="12"></image>` : ""}
      <title>${esc(full)}</title></g>${
      labelled ? `<text class="ob-id ${right ? "r" : "l"}" x="${n(label.x)}" y="${
        n(label.y)}" aria-hidden="true">${esc(id)}</text>` : ""}`;
  };

  const ring1Svg = `<g class="ob-nodes">${placedSectors.map(sector =>
    sector.placed.map(p => nodeSvg(p.id, p.deg, R1, "ring1", sector)).join("")).join("")}${
    ring2Placed.map(p => nodeSvg(p.id, p.deg, R2, "ring2", null)).join("")}</g>`;

  const rootState = rootRow ? stateOf(rootRow) : "";
  const rootRaw = rootRow ? String(rootRow.status || "").toLowerCase() : "";
  const rootPip = `${rootState}${rootRaw === "failed" ? " failed" : ""}`;
  const centreSvg = `<g class="ob-centre">
    <g class="ob-node root ${esc(rootState)}" data-row="${esc(rootId)}" tabindex="0" role="button"
      aria-label="${esc(`${rootId}${rootRow && rootRow.title ? `, ${rootRow.title}` : ""}, the centre of this orbit`)}"
      transform="translate(${n(cx)},${n(cy)})">
      <circle class="ob-disc" r="20"></circle>${
      rootRow ? `<circle class="ob-pip ${esc(rootPip)}" cx="15" cy="-15" r="3.5"></circle>` : ""}
      <title>${esc(`${rootId}${rootRow && rootRow.title ? ` — ${rootRow.title}` : ""}`)}</title></g>
    <text class="ob-rootid" x="${n(cx)}" y="${n(cy + 38)}" aria-hidden="true">${esc(rootId)}</text>
  </g>`;

  const ariaMap = `Orbit of ${rootId}: ${n1} row${n1 === 1 ? "" : "s"} at one recorded relationship${
    hasR2 ? `, ${ring2.size} at two` : " and none at two"}. Every row shown is listed in text below this picture.`;

  const svg = n1 ? `<div class="gwrap"><svg class="ob-map" width="${width}" height="${height}"
    viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(ariaMap)}"
    data-layout-mode="${compact ? "compact" : "wide"}" data-layout-width="${width}"
    data-ring-one-radius="${n(R1)}" data-ring-two-radius="${hasR2 ? n(R2) : "0"}">
    ${ringsSvg}${sectorSvg}${kinSvg}${spokeSvg}${ring1Svg}${centreSvg}</svg>${wedgeKey}</div>`
    : `<p class="ob-empty">This row records no relationship to any other row, so there is no orbit to draw. No ring is shown, because nothing was there.</p>`;

  // ----------------------------------------------------------------- the text
  // Everything the picture shows is written out, so no fact here is reachable
  // only by hovering a shape.
  // Clicking a node opens the drawer, as every other view does. Recentring is a
  // second control so the two never compete, and it lives in the roster rather than
  // as a glyph welded to a twelve-pixel node.
  const recentre = id => `<button class="ob-re" data-orbit="${esc(id)}" tabindex="0"
    title="${esc(`Centre the orbit on ${id}`)}"
    aria-label="${esc(`Centre the orbit on ${id}`)}">centre</button>`;

  const kinWord = { kin: "shares this row's parent", outside: "belongs to a different initiative", unrecorded: "records no parent, so it is neither" };
  const roster = placedSectors.map(sector => `<li><h4>${esc(sector.label)}</h4><ul>${
    sector.placed.map(p => {
      const row = rows.get(p.id);
      const kin = isDep(p.id) ? kinOf(p.id) : "";
      // One span, not two: the roster row is a three-column grid, and a fourth
      // child would drop onto a second line under the id. The second kind is the
      // fact the picture cannot carry, so it is written here rather than drawn.
      const also = alsoIn.get(p.id);
      const notes = [
        kin && kin !== "suppressed" ? kinWord[kin] : "",
        also ? `this row also ${also.join(" and ")} it` : "",
      ].filter(Boolean).join(" · ");
      return `<li><button class="chip ${row ? esc(stateOf(row)) : ""}" data-row="${esc(p.id)}"
        tabindex="0" title="${esc(titleOf(p.id) || p.id)}">${esc(p.id)}<span>${
        esc(titleOf(p.id) || "not a row on this board")}</span></button>${
        notes ? `<span class="ob-kinnote ${kin && kin !== "suppressed" ? kin : ""}">${esc(notes)}</span>` : ""}${
        recentre(p.id)}</li>`;
    }).join("")}</ul></li>`).join("");

  const ring2Roster = ring2Placed.length ? `<li><h4>two relationships out</h4><ul>${
    ring2Placed.slice().sort((a, b) => byId(a.id, b.id)).map(p => {
      const row = rows.get(p.id);
      return `<li><button class="chip ${row ? esc(stateOf(row)) : ""}" data-row="${esc(p.id)}"
        tabindex="0" title="${esc(titleOf(p.id) || p.id)}">${esc(p.id)}<span>${
        esc(titleOf(p.id) || "not a row on this board")}</span></button>
        <span class="ob-why">${esc(ring2.get(p.id).why)}</span>${recentre(p.id)}</li>`;
    }).join("")}</ul></li>` : "";

  const siblingLine = pureSiblings.length
    ? `${pureSiblings.length} further row${pureSiblings.length === 1 ? "" : "s"} share${
        pureSiblings.length === 1 ? "s" : ""} this row's parent and are not drawn, because this row names no relationship to ${
        pureSiblings.length === 1 ? "it" : "them"} beyond the shared parent: ${pureSiblings.join(", ")}.`
    : rootParent
      ? "No other row shares this row's parent without also being drawn on the ring."
      : "This row records no parent, so it has no siblings to leave out.";

  const kinDisclosure = !rootParent
    ? "This row records no parent, so no dependency here can be inside or outside a family, and no arc is drawn at all."
    : `A dashed arc means the two rows share this row's parent. A dependency with no arc and no hollow tick belongs to a different initiative. A hollow tick means that row records no parent of its own, which is a third thing entirely and is not a boundary crossing.`;

  // Both of these are conditions the picture cannot show, so they are said in
  // words on the renders where they are true, and are absent otherwise rather
  // than standing as a permanent caveat nobody can check.
  const dupLine = alsoIn.size
    ? `${alsoIn.size} row${alsoIn.size === 1 ? " here is" : "s here are"} named by more than one kind at once. Each is drawn once, in the first wedge that names it, and its other kind is written beside it in the list instead of being drawn as a second node — so a wedge counts rows drawn, and no row appears twice on the ring.`
    : "";
  const selfLine = selfNamed.length
    ? `This row also names itself in its own relationship lists (${selfNamed.join(", ")}); that is not drawn, because nothing is one relationship away from itself.`
    : "";

  const r2Line = hasR2
    ? `${ring2.size} row${ring2.size === 1 ? "" : "s"} sit${ring2.size === 1 ? "s" : ""} on the outer ring.`
    : "Nothing at two relationships from here, so no outer ring is drawn. Nothing is hidden: nothing was there.";

  const declared = rootCtx.done_signal
    ? `This row declares <code>${esc(rootCtx.done_signal)}</code> as its done signal &mdash; declared, not verified. The board records no artifact against it.`
    : "This row declares no done signal.";

  const epicNote = rootRow && rootRow.bucket === "epic"
    ? " This row is a container: what it holds is drawn, and any dependency on the outer ring belongs to one of its children, not to it."
    : "";

  return `<div class="card orbitcard">
    <h3>A row in its world</h3>
    <p class="meta">Centred on <b>${esc(rootId)}</b>${
      rootRow && rootRow.title ? ` &mdash; ${esc(rootRow.title)}` : ""}, because ${esc(rootSource)}.
      Distance from the centre is the number of recorded relationships, never time; the direction a row
      sits in says which kind of relationship it is.${esc(epicNote)}</p>
    <details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body">
      <p>Radius is the number of recorded relationships between this row and that one, and nothing else. It is not time, not order, not priority, not progress, and not distance in any other sense. There are only ever two radii on this picture, never a continuum, so that no length here can be read as a quantity.</p>
      <p>Angle carries the kind of relationship: four wedges, in a fixed clockwise order from the top &mdash; belongs to, waited on by, contains, waits on. A wedge's width is its share of the ring, so it counts rows and nothing more; a wide wedge is a common relationship, not an important one. Order inside a wedge is id ascending, chosen so the shape does not reshuffle when the panel repaints every five seconds. A wedge with nothing in it is not drawn, and when only one kind is present its first row sits at the top. Which wedge is which is named in the line under the picture, in the order they run.</p>
      <p>Rows that only share this row's parent are not on the ring at all. Every row you see is one this row names directly, so the ring is exactly what it is tied to and the arcs mean one thing. ${esc(siblingLine)}${dupLine ? ` ${esc(dupLine)}` : ""}${selfLine ? ` ${esc(selfLine)}` : ""}</p>
      <p>${esc(kinDisclosure)} ${esc(famSentence)}</p>
      <p>An arrow points along a dependency; a plain line means containment, which has no arrowhead because it is not an ordering. ${esc(r2Line)} The outer ring holds two derived sets and only those two &mdash; the initiative containing a dependency that sits outside this row's own, and whatever this row's blockers are themselves waiting on. A row missing from the outer ring is not a claim that no further relationship exists; it is a claim that it is not in those two sets. An outer-ring row is drawn just beyond the inner-ring row it came from, and that is the whole of what its angle says: where several came from the same row they are fanned a fixed step apart in id order, and that step is spacing so they can be told apart, not a quantity and not an ordering. Discs are drawn smaller on the outer ring only so the two rings read apart at a glance; no disc size on this picture is a measurement.</p>
      <p>${esc(timeLine)}</p>
      <p>The mark beside a node is the row's coarse owner string. This board's live sessions are not referenced by any row, so nothing here names a session or claims who is at the keyboard, and a row with no owner recorded simply carries no mark rather than a placeholder. ${declared}</p>
      <p>Only what is written down as data is drawn. ${missingFromContext ? `${missingFromContext} of this board's ${rows.size} rows are absent from the context feed entirely and can never appear here; that is a gap in the endpoint, not evidence that they stand alone. ` : ""}Clicking a node opens it in the drawer, as everywhere else on this page; the <i>centre</i> control beside its entry in the list below moves the orbit onto it. A dashed outline is an id this row names that the board does not hold as a row.${
      labelled ? "" : ` There are more than ${LABEL_CAP} rows on the inner ring, so their labels were dropped to hover rather than drawn over one another; every one of them is listed in text below.`}</p>
    </div></details>
    ${pickerHtml}
    ${svg}
    <ol class="ob-roster">${roster}${ring2Roster}</ol>
    <p class="legend">${esc(siblingLine)} ${esc(r2Line)}</p>
  </div>`;
}
