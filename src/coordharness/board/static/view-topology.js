// Topology — the fleet as an org in motion.
//
// The question no other tab answers: which lane is holding what right now, and
// what recorded coordination acts have crossed between lanes. Fleet counts
// holdings as an owner x vertical matrix and lists steps only for running
// rows; Dependencies and Crossings are row-to-row structure with no actor in
// them; Order is instants in sequence, not exchanges between lanes. This view
// lists every held row under its actor lane with its recorded step whatever
// its state, puts the session roster beside the holdings, and draws the
// recorded handoff / audit_request / audit_verdict acts that crossed between
// lanes — directed counts that appear nowhere else on the map.
//
// Direction ships on the pulse document (PulseV1 traffic, lane pairs only —
// the raw to_selector never reaches the wire). This view consumes it and
// feature-detects: without a pulse document it degrades to counts by
// originating lane, computed from the timeline, and says so in a sentence.
//
// Honesty spine: every count here is a count of written records on rows this
// board carries — never effort, never duration, never workload. Nothing is
// drawn to scale against elapsed time; no mark's size varies with anything.
// Timestamps group by parsed instant, never by string. Whatever this view
// cannot read is counted and surfaced as a floor note, never silently dropped.
//
// One global: view_topology(data). Classic script, IIFE — every other binding
// is closed over so nothing leaks into the page's shared top-level scope. The
// stylesheet is served (view-topology.css), never injected: this board sends
// style-src 'self' with no unsafe-inline, so an injected <style> is dropped.

(function () {
  "use strict";

  const TP_TL_SCHEMA = "TimelineV1";
  const TP_PULSE_SCHEMA = "PulseV1";
  const TP_TRAFFIC = ["handoff", "audit_request", "audit_verdict"];
  const TP_STATE_ORDER = ["running", "blocked", "attention", "next", "planned", "done"];
  const TP_LANE_ORDER = ["claude", "codex", "local", "service"];
  // Past this many chips a column stops being readable; the tail collapses
  // into a native <details> whose summary carries the exact count.
  const TP_MAX_CHIPS = 14;
  // Arc band geometry. All of it is layout: anchor spacing, arc height and
  // curvature carry nothing, and the copy says so. Height is chosen ONLY to
  // keep strokes and printed numerals apart — the numeral is the sole
  // quantity, so it must never sit on top of another numeral. TP_ARC_LIFT is
  // the minimum height difference two arcs sharing a label column must have;
  // a label sits at base - h/2, so a lift of 26 buys 13px of clear space
  // under a 10.5px numeral.
  const TP_ARC_SLOT = 100;
  const TP_ARC_MIN_H = 24;
  const TP_ARC_SPREAD = 12;
  const TP_ARC_LIFT = 26;

  // Own-property lookups only: a lane or kind arriving as "constructor" or
  // "__proto__" must miss, not resolve off Object.prototype.
  const tpOwn = (table, key) =>
    Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;

  const tpFallbackEsc = value => String(value == null ? "" : value)
    .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const tpPlural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  // ------------------------------------------------------------- ingest

  // Only at / kind / actor are ever read off an event; anything else the
  // response happens to carry is dropped here as well as at the server.
  // An event missing kind or actor is COUNTED as dropped, never skipped
  // quietly; an event whose timestamp does not parse keeps its kind count
  // but is excluded from any instant ordering, and both floors are printed.
  function tpIngestTimeline(doc, laneFn) {
    // A gap is never allowed to render as a measured zero: when the timeline
    // cannot be read, the per-lane strips say so and the event count is
    // withheld, rather than every lane printing "no recorded events".
    const NO_RECORD = " No per-lane event record could be read, so the lane"
      + " strips and the event count are withheld this poll rather than shown"
      + " as zero.";
    if (doc == null) {
      return { ok: false, gap: "The timeline endpoint was not available this poll." + NO_RECORD };
    }
    if (typeof doc !== "object" || !Array.isArray(doc.items)
      || String(doc.schema_version == null ? "" : doc.schema_version) !== TP_TL_SCHEMA) {
      return { ok: false, gap: `The timeline document did not arrive in the ${TP_TL_SCHEMA} shape this view reads.` + NO_RECORD };
    }
    const events = [];
    const rowsWith = new Set();
    let dropped = 0;
    let unparseable = 0;
    doc.items.forEach(item => {
      if (!item || typeof item !== "object" || item.id == null || !Array.isArray(item.events)) {
        dropped += 1;
        return;
      }
      const id = String(item.id);
      item.events.forEach(raw => {
        if (!raw || typeof raw !== "object") { dropped += 1; return; }
        const kind = String(raw.kind == null ? "" : raw.kind).trim();
        const actor = String(raw.actor == null ? "" : raw.actor).trim();
        if (!kind || !actor) { dropped += 1; return; }
        const at = String(raw.at == null ? "" : raw.at);
        const ms = Date.parse(at);
        const parsed = !Number.isNaN(ms);
        if (!parsed) unparseable += 1;
        events.push({ row: id, at, ms: parsed ? ms : null, kind, actorLane: laneFn(actor) || actor });
        rowsWith.add(id);
      });
    });
    return { ok: true, events, rowsWith, dropped, unparseable };
  }

  // The pulse document owns direction: traffic is served as lane-pair counts,
  // and a selector the server could not read as a lane arrives as undirected.
  // "We do not know where this went" is kept apart from "this went nowhere".
  function tpIngestPulse(doc) {
    if (doc == null) return { ok: false, gap: "" };
    if (typeof doc !== "object" || !Array.isArray(doc.traffic)
      || String(doc.schema_version == null ? "" : doc.schema_version) !== TP_PULSE_SCHEMA) {
      // States only what failed. What is shown INSTEAD is stated by whichever
      // branch actually renders, so this sentence can never promise a
      // fallback that the poll did not produce.
      return { ok: false, gap: `A pulse document arrived but not in the ${TP_PULSE_SCHEMA} shape this view reads, so no recorded direction could be taken from it.` };
    }
    const directed = [];
    const selfPairs = [];
    let undirected = 0;
    doc.traffic.forEach(t => {
      if (!t || typeof t !== "object") return;
      const kind = String(t.kind == null ? "" : t.kind).trim();
      if (!TP_TRAFFIC.includes(kind)) return;
      const from = String(t.from == null ? "" : t.from).trim();
      const count = Math.floor(Number(t.count));
      if (!from || !Number.isFinite(count) || count <= 0) return;
      const to = t.to == null ? "" : String(t.to).trim();
      if (!to) undirected += count;
      else if (to === from) selfPairs.push({ kind, lane: from, count });
      else directed.push({ kind, from, to, count });
    });
    (Array.isArray(doc.traffic_undirected) ? doc.traffic_undirected : []).forEach(t => {
      if (!t || typeof t !== "object") return;
      if (!TP_TRAFFIC.includes(String(t.kind == null ? "" : t.kind).trim())) return;
      const count = Math.floor(Number(t.count));
      if (Number.isFinite(count) && count > 0) undirected += count;
    });
    return { ok: true, directed, selfPairs, undirected, gap: "" };
  }

  // ----------------------------------------------------------- pure layout

  // Column order is fixed reading order and carries nothing: the map's own
  // lanes first, then anything else the data names, alphabetically. A lane
  // outside LANES (the fixture's `operator`) gets a real column with its raw
  // name as the label, exactly as the Fleet matrix labels it — never a
  // footnote, never an "other" bucket.
  function tpLaneKeys(laneNames, dataLanes) {
    const known = Array.isArray(laneNames) ? laneNames.map(String) : [];
    const ordered = TP_LANE_ORDER.filter(k => known.includes(k))
      .concat(known.filter(k => !TP_LANE_ORDER.includes(k)).sort((a, b) => a.localeCompare(b)));
    const extras = [...new Set((dataLanes || []).map(k => String(k == null ? "" : k)))]
      .filter(k => k && !ordered.includes(k))
      .sort((a, b) => a.localeCompare(b));
    return ordered.concat(extras);
  }

  const tpStateIndex = state => {
    const i = TP_STATE_ORDER.indexOf(state);
    return i === -1 ? TP_STATE_ORDER.length : i;
  };

  // Anchor x for lane column i in the arc band. Pure layout.
  const tpAnchorX = i => i * TP_ARC_SLOT + TP_ARC_SLOT / 2;

  // Quadratic arc from one anchor to another at a given height, over a given
  // baseline. Height is layout only — the copy states that curvature and
  // height carry nothing. The path is drawn FROM the recorded source TO the
  // recorded target, so a marching dash on it marches in the recorded
  // direction and no other.
  function tpArcGeometry(i1, i2, h, base) {
    const x1 = tpAnchorX(i1);
    const x2 = tpAnchorX(i2);
    const mx = (x1 + x2) / 2;
    return {
      d: `M${x1.toFixed(1)} ${base} Q${mx.toFixed(1)} ${(base - h).toFixed(1)} ${x2.toFixed(1)} ${base}`,
      labelX: mx,
      labelY: base - h / 2 - 5,
    };
  }

  // Height assignment. Every arc starts at a height set by how far apart its
  // two anchors are, then lifts until neither its stroke nor its printed
  // numeral shares space with one already placed in the same label column.
  // Deterministic: the input order is already sorted by anchor index and kind.
  function tpArcHeights(pairs) {
    const placed = [];
    return pairs.map(p => {
      const x = (tpAnchorX(p.i1) + tpAnchorX(p.i2)) / 2;
      let h = TP_ARC_MIN_H + Math.abs(p.i2 - p.i1) * TP_ARC_SPREAD;
      while (placed.some(q => Math.abs(q.x - x) < 14 && Math.abs(q.h - h) < TP_ARC_LIFT)) {
        h += TP_ARC_LIFT;
      }
      placed.push({ x, h });
      return h;
    });
  }

  // ------------------------------------------------------------------ view

  function view_topology(data) {
    const d = data || {};
    const esc = typeof d.esc === "function" ? d.esc : tpFallbackEsc;
    const laneFn = typeof d.lane === "function"
      ? d.lane
      : owner => String(owner || "").split(":")[0].trim().toLowerCase();
    const stateOf = typeof d.stateOf === "function"
      ? d.stateOf
      : row => {
        const status = String((row && row.status) || "").toLowerCase();
        if (TP_STATE_ORDER.includes(status)) return status;
        return status === "queued" ? "next" : "planned";
      };
    const LANES = d.LANES && typeof d.LANES === "object" ? d.LANES : {};
    const snapshot = d.snapshot && typeof d.snapshot === "object" ? d.snapshot : {};
    const allRows = Array.isArray(snapshot.rows) ? snapshot.rows : [];
    const allSessions = Array.isArray(snapshot.sessions) ? snapshot.sessions : [];
    // Unreadable session entries are counted and surfaced as a floor, never
    // silently folded into the live/total ratio.
    const sessions = allSessions.filter(s => s && typeof s === "object");
    const skippedSessions = allSessions.length - sessions.length;

    // ---------------------------------------------------------- the estate
    if (!allRows.length) {
      return `<div class="card tpcard"><h3>Empty board</h3><p class="meta">No rows this poll; there is no fleet to map.</p></div>`;
    }
    let skippedNoId = 0;
    const epics = allRows.filter(r => r && r.bucket === "epic").length;
    const rows = allRows.filter(r => {
      if (!r || typeof r !== "object") { skippedNoId += 1; return false; }
      if (r.bucket === "epic") return false;      // containers, not work — same rule as Fleet
      if (r.id == null) { skippedNoId += 1; return false; }
      return true;
    });
    if (!rows.length) {
      return `<div class="card tpcard"><h3>Empty board</h3><p class="meta">${
        epics ? esc(`Every row on this board is an epic container; no work rows to place in lanes.`)
          : esc(`No readable work rows this poll; there is no fleet to map.`)}</p></div>`;
    }

    const tl = tpIngestTimeline(d.timeline, laneFn);
    const pulse = tpIngestPulse(d.pulse);
    const events = tl.ok ? tl.events : [];

    // Lane universe: declared lanes, then any lane named by an owner, a
    // session actor, or an event actor. Absence renders as "0 rows", because
    // a declared lane holding nothing is information.
    const ownerLanes = rows.filter(r => r.owner).map(r => laneFn(r.owner)).filter(Boolean);
    const sessionLanes = sessions.map(s => laneFn(s && s.actor)).filter(Boolean);
    const eventLanes = events.map(ev => ev.actorLane).filter(Boolean);
    const laneKeys = tpLaneKeys(Object.keys(LANES), ownerLanes.concat(sessionLanes, eventLanes));
    const laneLabel = key => {
      const meta = tpOwn(LANES, key);
      return (meta && meta.label) || key;
    };

    const unowned = rows.filter(r => !r.owner);
    const rowsOf = key => rows.filter(r => r.owner && laneFn(r.owner) === key);
    const sessionsOf = key => sessions.filter(s => laneFn(s.actor) === key);
    const eventsOf = key => events.filter(ev => ev.actorLane === key);
    const liveCount = list => list.filter(s => s && s.live === true).length;

    // ---------------------------------------------------------------- chips
    const chip = row => {
      const id = String(row.id);
      const state = String(stateOf(row));
      const step = String(row.current_step == null ? "" : row.current_step).trim();
      const stale = row.stale === true;
      return `<button type="button" class="chip ${esc(state)}${state === "running" ? " m-run" : ""}"`
        + ` data-row="${esc(id)}" data-key="row:${esc(id)}" title="${esc(row.title == null ? id : row.title)}">`
        + `${esc(id)}<span>${esc(row.title == null ? "" : row.title)}${stale ? ` <em class="tp-stale">stale</em>` : ""}`
        + `${step ? `<span class="tp-step">${esc(step)}</span>` : ""}</span></button>`;
    };

    const chipColumn = held => {
      const sorted = held.slice().sort((a, b) =>
        tpStateIndex(String(stateOf(a))) - tpStateIndex(String(stateOf(b)))
        || String(a.id).localeCompare(String(b.id)));
      if (sorted.length <= TP_MAX_CHIPS) {
        return sorted.map(chip).join("");
      }
      const head = sorted.slice(0, TP_MAX_CHIPS).map(chip).join("");
      const tail = sorted.slice(TP_MAX_CHIPS);
      return `${head}<details class="tp-more"><summary>+${tail.length} more</summary>${tail.map(chip).join("")}</details>`;
    };

    const roster = (key, mine) => {
      if (!mine.length) return `<p class="tp-roster tp-nosess">no sessions recorded</p>`;
      const live = liveCount(mine);
      // Roster order is the session's own id, not the order the response
      // happened to list them in: an arbitrary server order would reshuffle
      // the dots between polls and read as movement that nothing recorded.
      const dots = mine.slice().sort((a, b) =>
        String(a.id == null ? a.label : a.id).localeCompare(String(b.id == null ? b.label : b.id))
      ).map(s => {
        const sid = String(s.id == null ? "" : s.id);
        const label = String(s.label == null ? sid : s.label);
        return `<span class="tp-sess" data-key="sess:${esc(sid || label)}" title="${esc(label)}">`
          + `<i class="tp-dot${s.live === true ? " live" : ""}"></i>${esc(label)}</span>`;
      }).join("");
      return `<div class="tp-roster">${dots}<span class="tp-livecount">${live} of ${mine.length} sessions live</span></div>`;
    };

    const stripOf = key => {
      // Absence of a readable timeline is NOT a measured zero. When the
      // document did not arrive, every strip says the record is missing.
      if (!tl.ok) return `<p class="tp-strip tp-nostrip">no event record this poll</p>`;
      const mine = eventsOf(key);
      if (!mine.length) return `<p class="tp-strip tp-nostrip">no recorded events</p>`;
      const counts = new Map();
      mine.forEach(ev => counts.set(ev.kind, (counts.get(ev.kind) || 0) + 1));
      const lines = [...counts.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([kind, n]) =>
          `<span class="tp-kindline" data-key="strip:${esc(key)}:${esc(kind)}">${esc(kind)} <b>&times; ${n}</b></span>`);
      return `<div class="tp-strip">${lines.join("")}</div>`;
    };

    const laneColumn = key => {
      const held = rowsOf(key);
      const mine = sessionsOf(key);
      return `<section class="tp-lane" data-key="lane:${esc(key)}">
        <header class="tp-lanehead">
          <span class="tp-lanename">${esc(laneLabel(key))}</span>
          <span class="tp-count">${tpPlural(held.length, "row", "rows")}</span>
          ${roster(key, mine)}
        </header>
        <div class="tp-chips">${chipColumn(held) || `<p class="tp-nostrip">no held rows</p>`}</div>
        ${stripOf(key)}
      </section>`;
    };

    const unownedRail = !unowned.length ? "" : `<section class="tp-lane tp-unowned" data-key="lane:(unowned)">
        <header class="tp-lanehead">
          <span class="tp-lanename">unowned</span>
          <span class="tp-count">${tpPlural(unowned.length, "row", "rows")}</span>
          <p class="tp-roster tp-nosess">no owner, so no lane and no roster</p>
        </header>
        <div class="tp-chips">${chipColumn(unowned)}</div>
        <p class="tp-strip tp-nostrip">events attribute to actor lanes, never to unowned rows</p>
      </section>`;

    // ----------------------------------------------------------- stat band
    const totalSessions = sessions.length;
    const totalLive = liveCount(sessions);
    const trafficEvents = events.filter(ev => TP_TRAFFIC.includes(ev.kind)).length;
    const stats = [
      ["work rows", String(rows.length)],
      // Lanes, not columns: the unowned rail is a column but its own header
      // says it is not a lane, and counting it here would contradict that.
      ["lanes", String(laneKeys.length)],
      ["sessions live", totalSessions ? `${totalLive}<span>/${totalSessions}</span>` : "&mdash;"],
      ["recorded events", tl.ok ? String(events.length) : "&mdash;"],
      ["traffic records", tl.ok ? String(trafficEvents) : (pulse.ok ? String(
        pulse.directed.reduce((n, t) => n + t.count, 0)
        + pulse.selfPairs.reduce((n, t) => n + t.count, 0) + pulse.undirected) : "&mdash;")],
    ].map(([label, num]) =>
      `<div class="tp-stat" data-key="stat:${esc(label)}"><span class="tp-statlab">${esc(label)}</span><span class="tp-statnum">${num}</span></div>`).join("");

    // -------------------------------------------------------- traffic band
    let traffic = "";
    const trafficKindTotals = () => {
      const totals = new Map(TP_TRAFFIC.map(k => [k, 0]));
      events.forEach(ev => {
        if (totals.has(ev.kind)) totals.set(ev.kind, totals.get(ev.kind) + 1);
      });
      return totals;
    };
    // Only ever called where tl.ok holds: these are timeline-derived counts,
    // and a timeline that did not arrive can produce no zero to print.
    const otherKindsSentence = () => {
      const other = events.length - trafficEvents;
      if (other <= 0) return "";
      const kinds = new Set(events.filter(ev => !TP_TRAFFIC.includes(ev.kind)).map(ev => ev.kind));
      return `<p class="tp-honest">${esc(`${tpPlural(other, "other event", "other events")} (${tpPlural(kinds.size, "kind", "kinds")}) are recorded on this board's rows and appear in the lane strips.`)}</p>`;
    };
    const zeroSentence = () => {
      const totals = trafficKindTotals();
      return `<p class="tp-honest">${esc(`${totals.get("handoff")} handoff, ${totals.get("audit_request")} audit_request and ${totals.get("audit_verdict")} audit_verdict events are recorded on this board's rows — no coordination traffic to draw.`)}</p>`
        + otherKindsSentence();
    };

    // Standing cross-check, available to every pulse branch: the pulse
    // aggregates and the timeline record the same acts, so when the two
    // documents disagree the view says so rather than picking a winner.
    const pulseTotal = !pulse.ok ? 0
      : pulse.directed.reduce((n, t) => n + t.count, 0)
        + pulse.selfPairs.reduce((n, t) => n + t.count, 0) + pulse.undirected;
    const crossCheck = () => (tl.ok && pulse.ok && pulseTotal !== trafficEvents
      ? `<p class="tp-honest">${esc(`The pulse document counts ${pulseTotal} coordination ${pulseTotal === 1 ? "record" : "records"}; the timeline this poll carries ${trafficEvents} ${trafficEvents === 1 ? "event" : "events"} of these kinds. The two documents disagree, so read this band as the pulse's claim, not the timeline's.`)}</p>`
      : "");
    const gapLines = [tl.ok ? "" : tl.gap, pulse.ok ? "" : pulse.gap]
      .filter(Boolean).map(g => `<p class="tp-honest">${esc(g)}</p>`).join("");

    if (!tl.ok && !pulse.ok) {
      traffic = `${gapLines}<p class="tp-honest">${esc("Neither document this poll carries coordination traffic, so nothing is drawn and no count is claimed.")}</p>`;
    } else if (pulse.ok && (pulse.directed.length || pulse.selfPairs.length || pulse.undirected)) {
      // Arcs mode: the pulse document serves direction as lane-pair counts.
      const anchors = tpLaneKeys(laneKeys,
        pulse.directed.flatMap(t => [t.from, t.to]).concat(pulse.selfPairs.map(t => t.lane)));
      const anchorIndex = new Map(anchors.map((k, i) => [k, i]));
      const width = anchors.length * TP_ARC_SLOT;
      const sortedArcs = pulse.directed.slice().sort((a, b) =>
        (anchorIndex.get(a.from) - anchorIndex.get(b.from))
        || (anchorIndex.get(a.to) - anchorIndex.get(b.to))
        || a.kind.localeCompare(b.kind));
      // Heights first, then the band sizes itself to hold them: an arc that
      // does not fit is never squashed onto another, because two strokes at
      // one height would print two numerals in one place and the numeral is
      // the only quantity this band carries.
      const pairs = sortedArcs.map(t => ({ i1: anchorIndex.get(t.from), i2: anchorIndex.get(t.to) }));
      const heights = tpArcHeights(pairs);
      const arcBase = heights.reduce((m, h) => Math.max(m, h), TP_ARC_MIN_H) + 14;
      const bandH = arcBase + 34;
      const arcMarks = sortedArcs.map((t, n) => {
        const g = tpArcGeometry(pairs[n].i1, pairs[n].i2, heights[n], arcBase);
        const kindClass = TP_TRAFFIC.includes(t.kind) ? ` tp-k-${t.kind}` : "";
        const label = `${tpPlural(t.count, `${t.kind} record`, `${t.kind} records`)} from ${t.from} to ${t.to}`;
        return `<path class="tp-arcline${kindClass}${t.kind === "handoff" ? " m-flow" : ""}"`
          + ` data-key="arc:${esc(t.kind)}:${esc(t.from)}:${esc(t.to)}"`
          + ` d="${g.d}" marker-end="url(#tp-tip)"><title>${esc(label)}</title></path>`
          + `<text class="tp-arcnum" x="${g.labelX.toFixed(1)}" y="${g.labelY.toFixed(1)}" text-anchor="middle">${t.count}</text>`;
      }).join("");
      const lattice = anchors.map((k, i) =>
        `<line class="tp-lat" x1="${tpAnchorX(i)}" y1="8" x2="${tpAnchorX(i)}" y2="${arcBase}"></line>`
        + `<circle class="tp-anchorglow" cx="${tpAnchorX(i)}" cy="${arcBase}" r="7" filter="url(#tp-blur)"></circle>`
        + `<circle class="tp-anchor" cx="${tpAnchorX(i)}" cy="${arcBase}" r="2.5"></circle>`
        + `<text class="tp-lanetag" x="${tpAnchorX(i)}" y="${arcBase + 20}" text-anchor="middle">${esc(k)}</text>`).join("");
      const aria = sortedArcs.length
        ? `Recorded coordination between lanes: ${sortedArcs.map(t => `${t.from} to ${t.to}, ${t.count} ${t.kind}`).join("; ")}.`
        : "No directed coordination records between distinct lanes.";
      const svg = sortedArcs.length
        ? `<div class="tp-arcwrap"><svg class="tp-arcs" viewBox="0 0 ${width} ${bandH}" role="img" aria-label="${esc(aria)}">
            <defs>
              <filter id="tp-blur" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="3"></feGaussianBlur></filter>
              <marker id="tp-tip" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z"></path></marker>
            </defs>
            <line class="tp-base" x1="0" y1="${arcBase}" x2="${width}" y2="${arcBase}"></line>
            ${lattice}${arcMarks}</svg></div>`
        : "";
      const selfLines = pulse.selfPairs.map(t =>
        `<p class="tp-honest" data-key="self:${esc(t.kind)}:${esc(t.lane)}">${esc(`${tpPlural(t.count, `${t.kind} record`, `${t.kind} records`)} within ${t.lane} — same lane on both ends, so no arc is drawn.`)}</p>`).join("");
      const residue = pulse.undirected
        ? `<p class="tp-honest">${esc(`${tpPlural(pulse.undirected, "event of these kinds carries", "events of these kinds carry")} no recorded direction.`)}</p>`
        : "";
      const legend = `<div class="tp-legend">
        <span class="key"><svg width="26" height="8" aria-hidden="true"><path class="tp-arcline tp-k-handoff m-flow" d="M1 4 H25"></path></svg>handoff</span>
        <span class="key"><svg width="26" height="8" aria-hidden="true"><path class="tp-arcline tp-k-audit_request" d="M1 4 H25"></path></svg>audit_request</span>
        <span class="key"><svg width="26" height="8" aria-hidden="true"><path class="tp-arcline tp-k-audit_verdict" d="M1 4 H25"></path></svg>audit_verdict</span>
        <span class="key">arrowhead = recorded target; the printed number is the only quantity</span>
      </div>`;
      traffic = `${svg}${selfLines}${residue}${legend}${gapLines}${crossCheck()}`;
    } else if (pulse.ok) {
      // The pulse serves zero traffic. Its zero is the claim on screen; the
      // timeline's own tally is only quotable when the timeline was read, and
      // if the two disagree the disagreement is the finding, not either number.
      traffic = `<p class="tp-honest">${esc(`The pulse document records no handoff, audit_request or audit_verdict passing between lanes this poll, so there is no coordination traffic to draw.`
        + (tl.ok && !trafficEvents ? ` The timeline agrees: it carries no events of these kinds either.` : ""))}</p>`
        + (tl.ok && !trafficEvents ? otherKindsSentence() : "") + gapLines + crossCheck();
    } else if (!tl.ok) {
      traffic = gapLines;
    } else if (!trafficEvents) {
      traffic = `${gapLines}${zeroSentence()}`;
    } else {
      // Counts mode: no pulse document, so no direction — counts by
      // originating lane, computed from the timeline, with the gap stated.
      const byLane = new Map();
      events.forEach(ev => {
        if (!TP_TRAFFIC.includes(ev.kind)) return;
        if (!byLane.has(ev.actorLane)) byLane.set(ev.actorLane, new Map(TP_TRAFFIC.map(k => [k, 0])));
        const m = byLane.get(ev.actorLane);
        m.set(ev.kind, m.get(ev.kind) + 1);
      });
      const laneRows = tpLaneKeys(laneKeys, [...byLane.keys()])
        .filter(k => byLane.has(k))
        .map(k => `<tr data-key="tc:${esc(k)}"><th>${esc(laneLabel(k))}</th>${
          TP_TRAFFIC.map(kind => `<td>${byLane.get(k).get(kind)}</td>`).join("")}</tr>`).join("");
      traffic = `<div class="tablewrap"><table class="tp-trafficcounts">
          <thead><tr><th>originating lane</th>${TP_TRAFFIC.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead>
          <tbody>${laneRows}</tbody></table></div>
        ${gapLines}
        <p class="tp-honest">Direction is recorded in coord.db (<code>to_selector</code>) and served by the pulse document, which this view could not read this poll; these are counts by originating lane only, and no arrow is drawn.</p>`;
    }

    // --------------------------------------------------------------- floors
    const floors = [];
    if (skippedNoId) floors.push(`${tpPlural(skippedNoId, "row was", "rows were")} skipped for carrying no id`);
    if (skippedSessions) floors.push(`${tpPlural(skippedSessions, "session entry was", "session entries were")} unreadable and are outside the live-session ratio`);
    if (tl.ok && tl.dropped) floors.push(`${tpPlural(tl.dropped, "event was", "events were")} dropped (missing kind or actor)`);
    if (tl.ok && tl.unparseable) floors.push(`${tpPlural(tl.unparseable, "timestamp is", "timestamps are")} unparseable — counted by kind, excluded from any instant grouping`);
    const floorLine = floors.length
      ? `<p class="tp-honest">${esc(floors.join("; ") + ".")}</p>` : "";

    // --------------------------------------------------------------- footer
    let foot = "";
    if (tl.ok && events.length) {
      // Newest by PARSED instant, never by string. Two events can carry the
      // same instant in two spellings, and several can share one instant
      // outright: naming one of them "the newest" would claim an order the
      // records do not carry, so a tie is stated as a tie.
      let newestMs = null;
      events.forEach(ev => {
        if (ev.ms == null) return;
        if (newestMs == null || ev.ms > newestMs) newestMs = ev.ms;
      });
      const atNewest = newestMs == null ? [] : events.filter(ev => ev.ms === newestMs);
      const allDone = rows.every(r => String(stateOf(r)) === "done");
      const doneLine = allDone
        ? ` All ${rows.length} rows are done; strips still count records, not activity.` : "";
      const lead = atNewest.length === 1
        ? `Newest recorded event: ${atNewest[0].kind} by ${atNewest[0].actorLane} at ${atNewest[0].at}. `
        : atNewest.length > 1
          ? `Newest recorded instant: ${atNewest[0].at} — ${atNewest.length} events share it, so none of them is the last. `
          : `No event this poll carries a parseable timestamp, so no newest instant can be named. `;
      foot = `<p class="tp-foot">${esc(lead)}${
        esc(`${tpPlural(events.length, "event", "events")} on ${tl.rowsWith.size} of ${allRows.length} rows this poll.`)}${esc(doneLine)}</p>`;
    }

    // ------------------------------------------------------------- readnote
    const directionSentence = (pulse.ok)
      ? ""
      : ` Until the board serves handoff direction, traffic is counted by originating lane only, and no arrow is drawn.`;
    const readnote = `<details class="readnote"><summary>How to read this, and what it does not say</summary>
      <div class="body">
      <p>${esc("Columns are actor lanes. A chip is a row whose owner prefix is that lane, coloured by its derived state; the line under a chip is its recorded current step, printed only when one exists. Dots are sessions: filled means the session lease was live at this poll, hollow means it was not. The strip under each lane counts recorded events by kind, and the traffic band counts handoff, audit_request and audit_verdict records between lanes.")}</p>
      <p>${esc("What the geometry does not say: lane order, lane spacing and chip order encode nothing — not rank, not chronology, not affinity. Column height is not workload. Arc height and curvature are layout; every arc is drawn at the same width, and the number printed on it is the only quantity." + directionSentence + " Nothing here is drawn to scale against elapsed time: an event count is how many records exist, not how long anything took, how busy a lane was, or how much was accomplished. Counts cover only events attached to rows this board carries — work done off-board, or events recorded without a readable kind and actor, is absent, and a floor note says how many were dropped. The board records no delegation between actors: these lanes are peers on a shared board, and no apex or hierarchy exists in the data for this view to draw.")}</p>
      <p>${esc("The marching dash on a handoff arc runs along the recorded direction and nothing else. The hairline lattice behind the arcs, the glow under a lane anchor, and the brief settle when this panel redraws are decoration and carry nothing.")}</p>
      </div></details>`;

    // ------------------------------------------------------------- assembly
    return `<div class="card tpcard"><h3>Who holds what, lane by lane</h3>
      <p class="meta">Fleet counts holdings as an owner &times; vertical matrix and lists a step only for running rows. This lists every held row under its actor lane with its recorded step whatever its state, beside the lane's session roster — and below, the recorded acts that crossed between lanes, which no other tab draws.</p>
      <div class="tp">
        <div class="tp-stats">${stats}</div>
        <div class="tp-lanes">${laneKeys.map(laneColumn).join("")}${unownedRail}</div>
        <p class="tp-caption">${esc(`Counts of recorded events on this board's rows — not effort, not duration, not workload. Counts cover only events attached to rows this board carries; work done off-board is absent.${epics ? ` ${tpPlural(epics, "epic container is", "epic containers are")} excluded — containers, not work.` : ""}`)}</p>
        ${readnote}
      </div></div>
      <div class="card tpcard"><h3>Coordination traffic</h3>
      <p class="meta">Recorded handoff, audit_request and audit_verdict acts between lanes. Records, not statuses: every stroke is ink, and a count of one is drawable.</p>
      ${traffic}${floorLine}${foot}</div>`;
  }

  globalThis.view_topology = view_topology;

  // Exposed on the one global so the pure ingest and layout maths can be
  // exercised outside a browser without leaking a second top-level name.
  view_topology.__internals = {
    tpIngestTimeline, tpIngestPulse, tpLaneKeys, tpArcGeometry, tpArcHeights, tpStateIndex,
  };
})();
