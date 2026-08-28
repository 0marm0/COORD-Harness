// Pulse — what has actually been written to this board's record, by which lane,
// in which order, and where it was routed.
//
// Distinctness against the shipped views, stated because the review demanded it:
// Order (view-chronicle.js) reads the timeline document exhaustively and crosses
// precedence with stage; Fleet counts rows per owner. Neither can name the one
// fact Pulse serves: RECORDED DIRECTION — who handed what to whom. The timeline
// event tuple is sealed at {at, kind, actor} and carries no destination; the
// PulseV1 document aggregates `to_selector` into lane-pair counts (`traffic`),
// which no other endpoint publishes. Pulse also serves the per-lane kind mix and
// the per-day ledger, which are aggregations of the record, not of ownership.
// Per the same review, the event wall was CUT (a grid of one-kind cells is a
// count rendered as ink) and the roster's held/running/attn digits were CUT
// (a re-derivation of Fleet's owner-by-status matrix in a third place). What
// remains is the recency bus, the routing exchange, the lane roster of records,
// the kind spectrum and the day ledger — each a direct read of the document.
//
// Honesty: every mark counts a record. No rate is computed ("events per minute"
// over sparse irregular instants fabricates a continuous process the record
// does not contain), no duration is drawn (no served field carries one), and no
// mark's size varies with anything except where a width literally counts
// records (the kind spectrum's segments, applied through the board's [data-w]
// mechanism, never an inline style). Timestamps are grouped by PARSED instant,
// never by string. Anything unreadable is counted aloud, never dropped quietly.
//
// One global: view_pulse(data). Everything else is closed over in the IIFE —
// this board loads views as classic scripts, so a top-level const here would
// collide with other views' top-level bindings.
//
// The stylesheet is served (view-pulse.css), never injected: the board sends
// `style-src 'self'` with no unsafe-inline.

(function () {
  "use strict";

  const PL_SCHEMA = "PulseV1";
  const PL_BUS_MAX = 12;
  const PL_MIX_MAX = 4;      // kind lines per roster card before "+n more kinds"
  const PL_TRAFFIC_MAX = 24; // exchange rows drawn before the remainder is counted

  // The family map is THIS VIEW'S OWN GROUPING of a free-text column, and the
  // readnote says so. `events.kind` is unconstrained TEXT with no registry, so
  // no grouping of it is a property of the record: a kind this table has never
  // heard of renders in the `other` family and is never silently dropped, and
  // fill-versus-hollow distinguishes families from each other and claims
  // nothing else — no "grade", no rank, no importance.
  const PL_FAMILY = {
    decision: "decision", adjudication: "decision", authority_adjudication: "decision",
    compensating_adjudication: "decision", audit_verdict: "decision",
    handoff: "routing", handoff_superseded: "routing", continuation_ready: "routing",
    audit_request: "routing", audit_request_retired: "routing", session_closeout: "routing",
    blocked_reason_classified: "block", claim_conflict: "block",
    classified_block_released: "release", orphaned_block_recovered: "release",
    reopen: "release", work_resumed: "release",
    tier_corrected: "repair", acceptance_contract_repaired: "repair",
    invalid_projection_reconciled: "repair", work_context_backfilled: "repair",
    work_context_pointer_corrected: "repair", resume_predicate_migrated: "repair",
    board_hygiene_policy_moot_closed: "repair", controller_source_grant: "repair",
    controller_source_consumption: "repair",
    note: "note", heartbeat: "note",
  };
  const PL_FAMILY_ORDER = ["decision", "routing", "block", "release", "repair", "note", "other"];

  // Own-property lookups only: a kind arriving as "constructor" or "__proto__"
  // must miss, not resolve off Object.prototype.
  const plOwn = (table, key) =>
    Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;
  const plFamilyOf = kind =>
    plOwn(PL_FAMILY, String(kind == null ? "" : kind).trim().toLowerCase()) || "other";

  const plFallbackEsc = value => String(value == null ? "" : value)
    .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const plPlural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  // A count field is printed only if it is a real finite number; anything else
  // prints as an em dash rather than NaN, because NaN on a board is a claim.
  const plInt = value => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.floor(n) : null;
  };
  const plDigits = value => {
    const n = plInt(value);
    return n == null ? "—" : String(n);
  };

  // Times are normalised through the parsed instant, so `...Z` and `...+00:00`
  // print identically; an unparseable stamp yields null and the caller counts it.
  //
  // Date.parse is NOT usable here. It is a lenient fallback parser: V8 turns
  // "garbage-1" into 2001-01-01 and "Dec 5" into 2001-12-05, so an unreadable
  // stamp came back as a plausible instant, was printed as a real clock time
  // and joined a real date group instead of being counted as unreadable. Worse,
  // it resolves those non-ISO strings in the HOST timezone — the same document
  // rendered 2001-01-01T00:00Z on a UTC machine and 2000-12-31T18:30Z on an IST
  // one, so the page was not even deterministic across readers. Only an
  // explicit ISO-8601 instant carrying Z or a numeric offset is an instant;
  // everything else is null and the caller says so out loud.
  const PL_ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|z|[+-]\d{2}:?\d{2})$/;
  const plMs = at => {
    const m = PL_ISO.exec(String(at == null ? "" : at).trim());
    if (!m) return null;
    const [, Y, Mo, D, H, Mi, S, F, Z] = m;
    const frac = F ? Number(String(F).slice(0, 3).padEnd(3, "0")) : 0;
    const ms = Date.UTC(+Y, +Mo - 1, +D, +H, +Mi, +(S || 0), frac);
    if (!Number.isFinite(ms)) return null;
    // Date.UTC silently rolls 2026-02-31 over into March; a stamp that does not
    // survive the round trip never named the day it claimed to name.
    const dt = new Date(ms);
    if (dt.getUTCFullYear() !== +Y || dt.getUTCMonth() !== +Mo - 1 || dt.getUTCDate() !== +D
      || dt.getUTCHours() !== +H || dt.getUTCMinutes() !== +Mi || dt.getUTCSeconds() !== +(S || 0)) return null;
    if (Z === "Z" || Z === "z") return ms;
    const digits = Z.slice(1).replace(":", "");
    const offset = (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4))) * 60000;
    return Z[0] === "-" ? ms + offset : ms - offset;
  };
  const plClock = ms => `${new Date(ms).toISOString().slice(11, 19)}Z`;
  const plDate = ms => new Date(ms).toISOString().slice(0, 10);

  // ------------------------------------------------------------- ingest

  // A missing document, a wrong schema and an unreadable list are three
  // different facts; none is coerced into "the board recorded nothing".
  function plIngestPulse(doc) {
    if (doc == null) {
      return { ok: false, gap: "The pulse document was not served this poll. Nothing is drawn in its place." };
    }
    if (typeof doc !== "object") {
      return { ok: false, gap: "The pulse document arrived in a shape this view cannot read." };
    }
    const schema = doc.schema_version == null ? "" : String(doc.schema_version);
    if (schema !== PL_SCHEMA) {
      return { ok: false, gap: schema
        ? `The pulse document answers schema ${schema}; this view reads ${PL_SCHEMA}.`
        : `The pulse document arrived without a schema version; this view reads ${PL_SCHEMA}.` };
    }
    const counts = doc.counts && typeof doc.counts === "object" ? doc.counts : {};
    const list = key => (Array.isArray(doc[key]) ? doc[key].filter(x => x && typeof x === "object") : []);
    return {
      ok: true,
      counts,
      kinds: list("kinds"),
      lanes: list("lanes"),
      days: list("days"),
      traffic: list("traffic"),
      undirected: list("traffic_undirected"),
      recent: list("recent"),
    };
  }

  // Timeline fallback for the bus when the pulse document is absent: the same
  // occurrence tuple, with no destination, because the timeline does not carry
  // one. Only {at, kind, actor} are ever read off an event.
  function plTimelineEvents(timeline) {
    if (!timeline || typeof timeline !== "object" || !Array.isArray(timeline.items)) return null;
    const events = [];
    let dropped = 0;
    timeline.items.forEach(item => {
      if (!item || typeof item !== "object" || item.id == null) { dropped += 1; return; }
      const id = String(item.id);
      if (!Array.isArray(item.events)) { dropped += 1; return; }
      item.events.forEach(raw => {
        if (!raw || typeof raw !== "object") { dropped += 1; return; }
        events.push({
          at: raw.at == null ? "" : String(raw.at),
          kind: raw.kind == null ? "" : String(raw.kind),
          actor: raw.actor == null ? "" : String(raw.actor),
          to: null,
          row: id,
        });
      });
    });
    return { events, dropped };
  }

  // Newest first by parsed instant; ties keep the input order, which for the
  // pulse document is the SQL (ts, event_id) order and is stated in the copy.
  function plNewestFirst(events) {
    return events
      .map((ev, i) => ({ ev, i, ms: plMs(ev.at) }))
      .filter(x => x.ms != null)
      .sort((a, b) => (b.ms - a.ms) || (a.i - b.i))
      .map(x => x.ev);
  }

  function plLaneOrder(keys, laneKeys) {
    const known = Array.isArray(laneKeys) ? laneKeys : [];
    const present = [...new Set((keys || []).map(k => String(k == null ? "" : k)).filter(Boolean))];
    const first = known.filter(k => present.includes(k));
    const rest = present.filter(k => !first.includes(k)).sort((a, b) => a.localeCompare(b));
    return first.concat(rest);
  }

  // ------------------------------------------------------------- view

  function view_pulse(data) {
    const d = data || {};
    const esc = typeof d.esc === "function" ? d.esc : plFallbackEsc;
    const laneOf = typeof d.lane === "function"
      ? d.lane
      : owner => String(owner || "").split(":")[0].trim().toLowerCase();
    const LANES = d.LANES && typeof d.LANES === "object" ? d.LANES : {};
    const rows = typeof d.rowById === "function" ? d.rowById() : new Map();
    const snapshotRows = (d.snapshot && Array.isArray(d.snapshot.rows)) ? d.snapshot.rows : null;
    const sessions = (d.snapshot && Array.isArray(d.snapshot.sessions)) ? d.snapshot.sessions : null;
    const stateOf = typeof d.stateOf === "function"
      ? d.stateOf
      : row => String((row && row.status) || "").toLowerCase();

    // Any lane gets a printable label: the map's own label where it has one,
    // the raw key otherwise — the way Fleet handles an owner outside LANES.
    // Nothing is demoted to an "other" card.
    const laneLabel = key => {
      const meta = plOwn(LANES, key);
      return (meta && meta.label) || key;
    };
    const laneBadge = key => String(key || "?").slice(0, 2);

    const chip = id => {
      if (id == null || String(id) === "") {
        return `<span class="pl-more">(no row)</span>`;
      }
      const row = rows.get(String(id));
      const running = row && stateOf(row) === "running";
      return `<button type="button" class="chip pl-chip${running ? " m-run" : ""}" `
        + `data-row="${esc(id)}" tabindex="0" title="${esc(row ? row.title : `${id} — not on this board`)}">`
        + `${esc(id)}</button>`;
    };

    const swatch = family =>
      `<i class="pl-sw pl-f-${esc(family)}" aria-hidden="true"></i>`;

    const hd = text => `<p class="pl-hd">${esc(text)}</p>`;
    const floor = text => `<p class="pl-floor">${esc(text)}</p>`;
    const gapCard = text => `<p class="pl-empty">${esc(text)}</p>`;

    const pulse = plIngestPulse(d.pulse);
    const tl = plTimelineEvents(d.timeline);

    // -------------------------------------------------------- counters
    // Dense instrument tiles. Every digit is a count the document publishes;
    // a tile with an unreadable value prints an em dash, never NaN.
    function counters(counts) {
      const rowsN = plInt(counts.rows);
      const withEv = plInt(counts.rows_with_events);
      const sess = plInt(counts.sessions);
      const live = plInt(counts.sessions_live);
      const tiles = [
        ["events", plDigits(counts.events), "recorded events"],
        ["instants", plDigits(counts.distinct_instants), "distinct instants"],
        ["rows", withEv == null || rowsN == null ? "—" : `${withEv}/${rowsN}`, "rows with events / rows"],
        ["days", plDigits(counts.days), "UTC days touched"],
        ["lanes", plDigits(counts.lanes), "lanes that wrote"],
        ["live", live == null || sess == null ? "—" : `${live}/${sess}`, "sessions live / sessions"],
      ];
      return `<div class="pl-stats">${tiles.map(([key, value, label]) =>
        `<div class="pl-stat" data-key="stat:${esc(key)}" title="${esc(label)}">`
        + `<span class="pl-stat-n">${esc(value)}</span><span class="pl-stat-l">${esc(label)}</span></div>`).join("")}</div>`;
    }

    // ------------------------------------------------------------- bus
    function bus(entries, total, sourceNote) {
      if (!entries.length) return "";
      let lastDate = null;
      const items = entries.map(ev => {
        const ms = plMs(ev.at);
        const date = plDate(ms);
        const stamp = date !== lastDate
          ? `<span class="pl-bus-date">${esc(date)}</span>` : "";
        lastDate = date;
        const key = `bus:${ev.at}|${ev.kind}|${ev.actor}|${ev.row}`;
        const family = plFamilyOf(ev.kind);
        const dest = ev.to ? `<span class="pl-bus-to">→ ${esc(ev.to)}</span>` : "";
        return `<span class="pl-bus-ev" data-key="${esc(key)}">${stamp}${swatch(family)}`
          + `<span class="pl-lanebadge" title="${esc(laneLabel(ev.actor) || "actor not recorded")}">${esc(laneBadge(ev.actor))}</span>`
          + `<span class="pl-bus-kind">${esc(ev.kind || "(unlabeled)")}</span>${dest}`
          + `${chip(ev.row)}<span class="pl-bus-at">${esc(plClock(ms))}</span></span>`;
      }).join("");
      return hd("LAST WRITTEN — newest first · a poll of the record, not a push feed")
        + `<div class="pl-bus">${items}</div>`
        + `<p class="pl-busfoot">${esc(`The ${plPlural(entries.length, "newest entry", "newest entries")} of ${plPlural(total, "recorded event", "recorded events")}${sourceNote}. `
          + `Order is recency by the full parsed instant, down to the millisecond; the stamp is printed only to the second, `
          + `so two neighbours can print the same second and still be ordered by a finer instant this stamp does not show. `
          + `Only entries whose parsed instants are identical keep the record's own order. Spacing is layout. `
          + `Refreshed by the poll, not pushed.`)}</p>`;
    }

    // --------------------------------------------------------- traffic
    // The exchange: directed lane-pair counts, the fact only this document
    // serves. Geometry: every path is drawn FROM the sender TO the receiver,
    // which is what licenses the marching dash (.m-flow). Stroke width is
    // constant; the count is a printed digit, never a thickness.
    function traffic(records, undirected) {
      const valid = [];
      let skipped = 0;
      records.forEach(r => {
        const count = plInt(r.count);
        if (typeof r.kind === "string" && typeof r.from === "string" && typeof r.to === "string"
          && r.kind && r.from && r.to && count != null && count > 0) {
          valid.push({ kind: r.kind, from: r.from, to: r.to, count });
        } else skipped += 1;
      });

      let undirSkipped = 0;
      const undirLines = undirected.map(u => {
        const count = plInt(u.count);
        if (count == null || !u.kind || !u.from) { undirSkipped += 1; return ""; }
        return `<li>${swatch(plFamilyOf(u.kind))}${esc(`${u.kind} · ${u.from} → destination not recorded · ${count}`)}</li>`;
      }).filter(Boolean).join("");
      const undirSkipNote = undirSkipped
        ? floor(`${plPlural(undirSkipped, "undirected record", "undirected records")} arrived in a shape this view could not read and ${undirSkipped === 1 ? "is" : "are"} not listed.`)
        : "";
      const undirBlock = (undirLines
        ? `<p class="pl-floor">Routed acts whose selector names no lane — "we do not know where this went" is not "this went nowhere":</p><ul class="pl-undir">${undirLines}</ul>`
        : "") + undirSkipNote;

      if (!valid.length) {
        return hd("THE EXCHANGE — recorded routing between lanes")
          + gapCard("No directed exchange is recorded this poll: no handoff, audit request or verdict named a destination lane.")
          + undirBlock
          + (skipped ? floor(`${plPlural(skipped, "traffic record", "traffic records")} arrived in a shape this view could not read and ${skipped === 1 ? "is" : "are"} not drawn.`) : "");
      }

      const drawn = valid.slice(0, PL_TRAFFIC_MAX);
      const elided = valid.length - drawn.length;
      const laneKeys = plLaneOrder(
        drawn.map(r => r.from).concat(drawn.map(r => r.to)), Object.keys(LANES));
      const liveByLane = new Map();
      (sessions || []).forEach(s => {
        if (s && s.live) {
          const key = String(s.actor || laneOf(s.id));
          liveByLane.set(key, (liveByLane.get(key) || 0) + 1);
        }
      });

      const W = 560, LX = 132, RX = 428, TOP = 30, ROW = 34;
      const yOf = i => TOP + i * ROW;
      const H = TOP + laneKeys.length * ROW + 8;
      const idx = new Map(laneKeys.map((k, i) => [k, i]));

      // Records sharing a lane pair bend apart so their labels stay legible;
      // the bend is layout and carries nothing.
      const byPair = new Map();
      drawn.forEach(r => {
        const key = `${r.from}|${r.to}`;
        if (!byPair.has(key)) byPair.set(key, []);
        byPair.get(key).push(r);
      });

      const paths = [];
      const labels = [];
      byPair.forEach(group => {
        group.forEach((r, k) => {
          const y1 = yOf(idx.get(r.from));
          const y2 = yOf(idx.get(r.to));
          const off = (k - (group.length - 1) / 2) * 24;
          const mx = (LX + RX) / 2;
          const dPath = `M${LX + 10} ${y1} C${mx} ${y1 + off}, ${mx} ${y2 + off}, ${RX - 12} ${y2}`;
          const title = `${r.from} → ${r.to} · ${r.kind} · ${plPlural(r.count, "recorded act", "recorded acts")}`;
          paths.push(`<path class="pl-tr m-flow" data-key="tr:${esc(`${r.kind}|${r.from}|${r.to}`)}" `
            + `d="${dPath}" marker-end="url(#pl-tip)"><title>${esc(title)}</title></path>`);
          labels.push(`<text class="pl-tlab" x="${mx}" y="${((y1 + y2) / 2 + off * 0.75 - 5).toFixed(1)}" `
            + `text-anchor="middle">${esc(`${r.kind} · ${r.count}`)}</text>`);
        });
      });

      const nodes = laneKeys.map((key, i) => {
        const y = yOf(i);
        const live = liveByLane.get(key) || 0;
        const glow = live ? `<circle class="pl-node-glow" cx="0" cy="0" r="7" filter="url(#pl-glow)"></circle>` : "";
        const dot = side => `<g transform="translate(${side === "l" ? LX : RX},${y})">${glow}`
          + `<circle class="pl-node${live ? " live" : ""}" cx="0" cy="0" r="4.5">`
          + `<title>${esc(`${laneLabel(key)} — ${plPlural(live, "live session", "live sessions")} this poll`)}</title></circle></g>`;
        return `<text class="pl-tname" x="${LX - 14}" y="${y + 4}" text-anchor="end">${esc(laneLabel(key))}</text>`
          + dot("l") + dot("r")
          + `<text class="pl-tname" x="${RX + 14}" y="${y + 4}">${esc(laneLabel(key))}</text>`;
      }).join("");

      const totalActs = valid.reduce((n, r) => n + r.count, 0);
      const summary = `${plPlural(totalActs, "routed act", "routed acts")} across ${plPlural(valid.length, "lane pair", "lane pairs")}; every arrow runs from sender to receiver.`;

      return hd("THE EXCHANGE — recorded routing between lanes")
        + `<div class="pl-tscroll"><svg class="pl-tmap" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(summary)}">`
        + `<defs>`
        + `<pattern id="pl-grid" width="24" height="24" patternUnits="userSpaceOnUse">`
        + `<path class="pl-lattice" d="M24 0H0V24" fill="none"></path></pattern>`
        + `<filter id="pl-glow" x="-80%" y="-80%" width="260%" height="260%">`
        + `<feGaussianBlur stdDeviation="2.6"></feGaussianBlur></filter>`
        + `<marker id="pl-tip" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5.5" markerHeight="5.5" orient="auto-start-reverse">`
        + `<path class="pl-tiphead" d="M0 0 L10 5 L0 10 z"></path></marker>`
        + `</defs>`
        + `<rect class="pl-latticebg" x="${LX + 6}" y="6" width="${RX - LX - 12}" height="${H - 12}" fill="url(#pl-grid)" rx="8"></rect>`
        + `${paths.join("")}${labels.join("")}${nodes}</svg></div>`
        + `<p class="pl-busfoot">${esc(`${summary} The dash marches along the recorded direction and nothing else; `
          + `arrow length, curvature and the lattice behind them are layout and carry nothing. A glowing terminal `
          + `has at least one live session this poll. Counts are the printed digits: no line's thickness counts `
          + `anything, and the only time a line's thickness moves at all is the shared arrival pulse a lane pair `
          + `gets once, on the poll it first appears.`)}</p>`
        + (elided ? floor(`${plPlural(elided, "further lane pair is", "further lane pairs are")} counted in the totals above but not drawn.`) : "")
        + (skipped ? floor(`${plPlural(skipped, "traffic record", "traffic records")} arrived in a shape this view could not read and ${skipped === 1 ? "is" : "are"} not drawn.`) : "")
        + undirBlock;
    }

    // ---------------------------------------------------------- roster
    function roster(pulseLanes) {
      const cards = pulseLanes.map(rec => {
        const key = String(rec.lane == null ? "" : rec.lane);
        const events = plInt(rec.events);
        const sessN = plInt(rec.sessions);
        const liveN = plInt(rec.sessions_live);
        const mySessions = sessions
          ? sessions.filter(s => s && String(s.actor || laneOf(s.id)) === key)
          : null;
        const dots = mySessions
          ? mySessions.map(s =>
            `<i class="pl-live${s.live ? "" : " off"}" title="${esc(`${s.label || s.id}${s.live ? " — live" : " — not live"}`)}"></i>`).join("")
          : "";
        const sessLine = sessN == null ? ""
          : `<div class="pl-lane-sess">${dots}<span>${esc(`${plPlural(sessN, "session", "sessions")}, ${liveN == null ? "?" : liveN} live`)}</span></div>`;
        // An absent mix and an unreadable mix are different facts: saying
        // "wrote nothing" for a lane whose kind list arrived in a shape this
        // view cannot read would put a claim where there is only a gap.
        const mixReadable = Array.isArray(rec.kinds);
        const kinds = mixReadable ? rec.kinds.filter(k => k && typeof k === "object") : [];
        const mixDropped = mixReadable ? rec.kinds.length - kinds.length : 0;
        const shown = kinds.slice(0, PL_MIX_MAX);
        const moreK = kinds.length - shown.length;
        const mix = shown.length
          ? `<ul class="pl-kmix">${shown.map(k =>
            `<li>${swatch(plFamilyOf(k.kind))}<span>${esc(String(k.kind || "(unlabeled)"))}</span><b>${esc(plDigits(k.count))}</b></li>`).join("")}`
            + `${moreK > 0 ? `<li class="pl-more">${esc(`+ ${plPlural(moreK, "more kind", "more kinds")}`)}</li>` : ""}`
            + `${mixDropped > 0 ? `<li class="pl-more">${esc(`+ ${plPlural(mixDropped, "entry this view could not read", "entries this view could not read")}`)}</li>` : ""}</ul>`
          : !mixReadable
            ? `<p class="pl-more">kind mix arrived in a shape this view cannot read</p>`
            : mixDropped
              ? `<p class="pl-more">${esc(`${plPlural(mixDropped, "kind entry", "kind entries")} arrived in a shape this view cannot read; no mix is drawn`)}</p>`
              : `<p class="pl-more">wrote nothing this poll</p>`;
        return `<div class="pl-lane" data-key="lane:${esc(key)}">`
          + `<div class="pl-lane-hd"><span class="pl-lanebadge">${esc(laneBadge(key))}</span>`
          + `<b>${esc(laneLabel(key))}</b>`
          + `<span class="pl-lane-ev">${esc(`${plDigits(events)} ${events === 1 ? "event" : "events"}`)}</span></div>`
          + sessLine + mix + `</div>`;
      }).join("");

      // Lanes that own rows but wrote nothing — stated as their own line, not
      // demoted to an overflow card. On the demo board this names `operator`.
      let silent = "";
      if (snapshotRows) {
        const wrote = new Set(pulseLanes.map(r => String(r.lane == null ? "" : r.lane)));
        const owned = new Map();
        snapshotRows.forEach(r => {
          const key = laneOf(r.owner);
          if (key) owned.set(key, (owned.get(key) || 0) + 1);
        });
        const quiet = [...owned.entries()].filter(([key]) => !wrote.has(key))
          .sort((a, b) => a[0].localeCompare(b[0]));
        if (quiet.length) {
          silent = `<p class="pl-floor">${esc(quiet.map(([key, n]) =>
            `${laneLabel(key)} owns ${plPlural(n, "row", "rows")} on this board and wrote nothing to the record this poll`)
            .join("; ") + ". Silence means nothing was written, not that nothing happened.")}</p>`;
        }
      } else {
        silent = `<p class="pl-floor">Snapshot unavailable — session dots and owner attribution withheld.</p>`;
      }
      return hd("LANE ROSTER — who is writing the record")
        + `<div class="pl-lanes">${cards || ""}</div>${silent}`;
    }

    // ---------------------------------------------------- kind spectrum
    function spectrum(kinds) {
      const valid = kinds.filter(k => typeof k.kind === "string" && plInt(k.count) != null && plInt(k.count) >= 0)
        .map(k => ({ kind: k.kind, count: plInt(k.count), family: plFamilyOf(k.kind) }));
      const unreadable = kinds.length - valid.length;
      if (!valid.length) {
        return unreadable
          ? hd("KIND SPECTRUM — what the record is made of")
            + gapCard(`${plPlural(unreadable, "kind record", "kind records")} arrived in a shape this view could not read; nothing is drawn in their place.`)
          : "";
      }
      const total = valid.reduce((n, k) => n + k.count, 0);
      if (!total) return "";
      // A zero-count kind is not part of what the record is made of, and a
      // segment for it would be pure ink standing for no record; it stays in
      // the vocabulary list below, where the count is printed as zero.
      const drawnSegs = valid.filter(k => k.count > 0);
      const segs = drawnSegs.map(k =>
        `<span class="pl-seg pl-f-${esc(k.family)}" data-key="seg:${esc(k.kind)}" data-w="${((k.count / total) * 100).toFixed(3)}" `
        + `title="${esc(`${k.kind} · ${plPlural(k.count, "event", "events")} · family ${k.family}`)}"></span>`).join("");
      const listed = valid.map(k =>
        `<li data-key="kind:${esc(k.kind)}">${swatch(k.family)}<span>${esc(k.kind)}</span><b>${k.count}</b></li>`).join("");
      const famCounts = new Map();
      valid.forEach(k => famCounts.set(k.family, (famCounts.get(k.family) || 0) + k.count));
      const famLine = PL_FAMILY_ORDER.filter(f => famCounts.has(f))
        .map(f => `${swatch(f)}${esc(`${f} · ${famCounts.get(f)}`)}`)
        .join("<span class=\"pl-dotgap\"></span>");
      // "The whole record is one kind" is a universal claim, so it is only made
      // when nothing was left out of the reckoning: one unreadable kind record
      // is enough to make it false.
      const single = valid.length === 1 && !unreadable
        ? floor(`The whole record is one kind: all ${plPlural(total, "event is", "events are")} '${valid[0].kind}'. There is no mix to draw, only this count.`)
        : valid.length === 1
          ? floor(`Every kind this view could read is '${valid[0].kind}', covering ${plPlural(total, "event", "events")}; ${plPlural(unreadable, "further kind record", "further kind records")} could not be read, so this is not the whole record.`)
          : "";
      const zeroed = valid.length - drawnSegs.length;
      return hd("KIND SPECTRUM — what the record is made of")
        + single
        + `<div class="pl-spectrum" role="img" aria-label="${esc(`${plPlural(total, "event", "events")} across ${plPlural(drawnSegs.length, "kind", "kinds")}; segment width counts records`)}">${segs}</div>`
        + `<p class="pl-busfoot">${esc("Segment width counts records and nothing else — no minimum width props a rare kind up, so a kind too few to fill a pixel here draws nothing and is read from the vocabulary below instead. Colour is the kind's family — a grouping this view imposes on a free-text column, not a property of the record; fill-versus-hollow only tells families apart.")}</p>`
        + `<p class="pl-legendline">${famLine}</p>`
        + (zeroed ? floor(`${plPlural(zeroed, "kind is", "kinds are")} listed with a count of zero and ${zeroed === 1 ? "draws" : "draw"} no segment.`) : "")
        + (unreadable ? floor(`${plPlural(unreadable, "further kind record", "further kind records")} arrived in a shape this view could not read and ${unreadable === 1 ? "is" : "are"} neither drawn nor listed.`) : "")
        + `<details class="pl-vocab"><summary>${esc(`The raw vocabulary — ${plPlural(valid.length, "kind string", "kind strings")} observed`)}</summary>`
        + `<ul class="pl-kinds">${listed}</ul></details>`;
    }

    // ------------------------------------------------------ day ledger
    function days(list) {
      const valid = list.filter(day => typeof day.date === "string" && plInt(day.events) != null);
      const unreadable = list.length - valid.length;
      if (!valid.length) {
        return unreadable
          ? hd("DAY LEDGER — the record by UTC date")
            + gapCard(`${plPlural(unreadable, "day record", "day records")} arrived in a shape this view could not read; nothing is drawn in their place.`)
          : "";
      }
      const lines = valid.map(day => {
        const first = plMs(day.first_at);
        const last = plMs(day.last_at);
        const span = first != null && last != null
          ? `${plClock(first)} → ${plClock(last)}` : "instants not readable";
        return `<li data-key="day:${esc(day.date)}"><b>${esc(day.date)}</b>`
          + `<span class="pl-day-n">${esc(plPlural(plInt(day.events), "event", "events"))}</span>`
          + `<span class="pl-day-span">${esc(span)}</span></li>`;
      }).join("");
      return hd("DAY LEDGER — the record by UTC date")
        + `<ul class="pl-days">${lines}</ul>`
        + (unreadable ? floor(`${plPlural(unreadable, "further day record", "further day records")} arrived in a shape this view could not read and ${unreadable === 1 ? "is" : "are"} not drawn.`) : "")
        + `<p class="pl-busfoot">${esc("First and last are the day's recorded instants. The stretch between them is not drawn as a bar because a count is not a duration, and events inside a day are sparse instants, not a continuous stream.")}</p>`;
    }

    // -------------------------------------------------------- readnote
    const readnote = `<details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body">`
      + `<p>${esc("Pulse is the board's written record and its routing. Every mark counts something recorded: one bus entry per event, one arrow per lane pair, one digit per count the pulse document publishes. Event counts per lane count records written, not effort, workload or busyness — a lane that writes little may be doing much, and silence means nothing was written, not that nothing happened.")}</p>`
      + `<p>${esc("The bus is not a live push feed: it is the newest entries of a document polled every few seconds. Its order is strict recency by the full parsed instant. The stamp beside each entry is truncated to the second, so two neighbours may print the same second while the order between them comes from a finer instant the stamp does not show; only entries whose parsed instants are identical fall back to the record's own order. Spacing between entries is layout, not time.")}</p>`
      + `<p>${esc("The exchange draws only what the record routes: a handoff or audit whose selector names a lane. Arrows run from sender to receiver — the marching dash repeats that recorded direction and adds nothing. Length, curvature, bend and the lattice behind them carry no meaning; the count is always the printed digit, and no line's thickness counts anything — the one time a line's thickness moves is the shared arrival pulse a lane pair gets once, on the poll it first appears. An act whose selector names no lane is listed apart, because not knowing where it went is different from it going nowhere.")}</p>`
      + `<p>${esc("Colour groups kinds into families — a grouping this view imposes on a free-text column with no registry, not a property of the record. Fill-versus-hollow only tells families apart; it grades nothing. Lanes are listed in the map's fixed order and that order is not a ranking. This view computes no rate: events per minute over sparse, irregular instants would invent a continuous process the record does not contain. No mark is drawn to scale against elapsed time, because no served field carries a duration.")}</p>`
      + `<p>${esc("Counts the document could not attribute — events without a row, timestamps it could not represent — are printed in the notes and never silently dropped. Where this view's own arithmetic over the document disagrees with the document's headline counts, it says so rather than choosing a side.")}</p>`
      + `</div></details>`;

    // ------------------------------------------------------- assembly
    const out = [];

    if (!pulse.ok) {
      out.push(`<div class="pl-region">${gapCard(pulse.gap)}</div>`);
      // The bus and a minimal roster can still be read off the timeline; that
      // fallback is stated, not silent.
      if (tl && tl.events.length) {
        const ordered = plNewestFirst(tl.events);
        const unparseable = tl.events.length - ordered.length;
        out.push(`<div class="pl-region">${bus(ordered.slice(0, PL_BUS_MAX), tl.events.length,
          " (read from the timeline document, which carries no destination field)")}` +
          (unparseable ? floor(`${plPlural(unparseable, "event had a timestamp", "events had timestamps")} this view could not parse; counted here and drawn nowhere.`) : "") +
          (tl.dropped ? floor(`${plPlural(tl.dropped, "further entry", "further entries")} could not be read; these figures are a floor.`) : "") +
          `</div>`);
        out.push(`<div class="pl-region">${gapCard("The exchange, the kind spectrum, the day ledger and the lane roster are served only by the pulse document and are withheld with it. Nothing is drawn in their place.")}</div>`);
      } else if (tl && !tl.events.length) {
        out.push(`<div class="pl-region">${gapCard(`The board has recorded no events this poll.${snapshotRows ? ` ${plPlural(snapshotRows.length, "row exists", "rows exist")}; none has written to the record.` : ""}`)}</div>`);
      } else {
        out.push(`<div class="pl-region">${gapCard("The timeline document was not served this poll either. Nothing is drawn in its place.")}</div>`);
      }
      out.push(`<div class="pl-region">${readnote}</div>`);
      return `<div class="plroot">${out.join("")}</div>`;
    }

    const counts = pulse.counts;
    const eventsN = plInt(counts.events);

    if (eventsN === 0) {
      const rowsN = plInt(counts.rows);
      out.push(`<div class="pl-region">${gapCard(`The board has recorded no events this poll.${rowsN != null ? ` ${plPlural(rowsN, "row exists", "rows exist")}; none has written to the record.` : ""}`)}</div>`);
      out.push(`<div class="pl-region">${roster(pulse.lanes)}</div>`);
      out.push(`<div class="pl-region">${readnote}</div>`);
      return `<div class="plroot">${out.join("")}</div>`;
    }

    // Internal arithmetic over the document, so a section that stops re-summing
    // to the headline is called out instead of averaged away.
    const sums = [
      ["kinds", pulse.kinds.reduce((n, k) => n + (plInt(k.count) || 0), 0)],
      ["lanes", pulse.lanes.reduce((n, l) => n + (plInt(l.events) || 0), 0)],
      ["days", pulse.days.reduce((n, day) => n + (plInt(day.events) || 0), 0)],
    ];
    const disagree = eventsN == null ? [] : sums.filter(([, n]) => n !== eventsN);
    const arithmeticNote = disagree.length
      ? floor(`The document's own sections disagree with its headline of ${eventsN} events: `
        + disagree.map(([name, n]) => `${name} re-sum to ${n}`).join(", ")
        + ". Both figures are shown; this view does not choose between them.")
      : "";

    const floorNotes = [];
    const orphan = plInt(counts.events_without_row);
    if (orphan) floorNotes.push(floor(`${plPlural(orphan, "event references", "events reference")} no row this board carries; counted in the totals, drawn nowhere.`));
    const unrep = plInt(counts.events_unrepresentable_time);
    if (unrep) floorNotes.push(floor(`${plPlural(unrep, "event carries", "events carry")} a timestamp the document could not represent; counted in the totals, drawn nowhere.`));
    const rowsN = plInt(counts.rows);
    const withEv = plInt(counts.rows_with_events);
    if (rowsN != null && withEv != null && rowsN > 0 && withEv / rowsN < 0.25) {
      floorNotes.push(floor(`${withEv} of ${rowsN} rows carry recorded events this poll; the other ${rowsN - withEv} are silent, not idle — silence here means nothing was written, not that nothing happened.`));
    }

    // The unparseable count is taken BEFORE the slice. Deriving it afterwards
    // (ordered-vs-listed) is only correct while the slice is not full: with 20
    // readable and 3 unreadable entries the slice fills at 12 and the shortfall
    // is entirely the slice, so the three unreadable ones went unmentioned.
    const recentAll = plNewestFirst(pulse.recent.map(r => ({
      at: r.at == null ? "" : String(r.at),
      kind: r.kind == null ? "" : String(r.kind),
      actor: r.actor == null ? "" : String(r.actor),
      to: r.to == null ? null : String(r.to),
      row: r.row == null ? "" : String(r.row),
    })));
    const busUnparseable = pulse.recent.length - recentAll.length;
    const recent = recentAll.slice(0, PL_BUS_MAX);

    out.push(`<div class="pl-region">${counters(counts)}${arithmeticNote}${floorNotes.join("")}</div>`);
    out.push(`<div class="pl-region">${recent.length
      ? bus(recent, eventsN == null ? recent.length : eventsN, "")
      : gapCard("The pulse document lists no recent entries this poll.")}${busUnparseable > 0
      ? floor(`${plPlural(busUnparseable, "recent entry had a timestamp", "recent entries had timestamps")} this view could not parse; counted in the totals, drawn nowhere.`)
      : ""}</div>`);
    out.push(`<div class="pl-cols">`
      + `<div class="pl-region">${roster(pulse.lanes)}</div>`
      + `<div class="pl-region">${traffic(pulse.traffic, pulse.undirected)}${spectrum(pulse.kinds)}${days(pulse.days)}</div>`
      + `</div>`);
    out.push(`<div class="pl-region">${readnote}</div>`);
    return `<div class="plroot">${out.join("")}</div>`;
  }

  globalThis.view_pulse = view_pulse;

  // Exposed on the one global so the pure pieces can be exercised outside a
  // browser without leaking a second top-level name.
  view_pulse.__internals = { plIngestPulse, plTimelineEvents, plNewestFirst, plLaneOrder, plFamilyOf };
})();
