// Flowpath — the custody pipeline, drawn as STATIONS.
//
// A station is a place a row is standing. The board records where each row IS;
// it has never once recorded a row MOVING. So there is no connecting rail, no
// arrowhead, no funnel and no percentage anywhere in this view: five same-size
// boxes hold the five statuses a row can stand in, a proof gate is drawn as a
// line (not a place), leases are bucketed ordinally (never a countdown bar),
// and the two ways a claim ends are printed as numerals, never as two bars.
//
// Consumes `snapshot` and `context` ONLY. No graph edge, no timeline event —
// so nothing here implies order of occurrence or a relationship between rows.
// Never reads progress_fraction, eta_seconds, priority or current_step.
//
// Every figure on the screen is computed at render time from the live
// documents; nothing is a literal carried over from any spec or fixture.
//
// One global: view_flowpath(data). Styles are served from view-flowpath.css —
// the board's CSP is style-src 'self' with no unsafe-inline, so no style=
// attribute and no <style> element is ever emitted here.
(() => {
  "use strict";

  globalThis.view_flowpath = function view_flowpath(data) {
    const d = data || {};
    const esc = typeof d.esc === "function" ? d.esc
      : s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const stateOf = typeof d.stateOf === "function" ? d.stateOf
      : row => String((row && row.status) || "").toLowerCase();
    const snapshot = d.snapshot;
    const context = d.context;

    // ------------------------------------------------------------ no board
    if (!snapshot || !Array.isArray(snapshot.rows)) {
      return `<div class="card stcard"><h3>No board</h3>
        <p class="meta">The map was handed no snapshot, so there is nothing to place at a station.</p></div>`;
    }

    // ------------------------------------------------------------ intake
    let unreadable = 0;
    const rows = [];
    snapshot.rows.forEach(r => {
      if (!r || r.id == null) { unreadable += 1; return; }
      rows.push(r);
    });
    const epics = rows.filter(r => r.bucket === "epic");
    const work = rows.filter(r => r.bucket !== "epic");
    const statusOf = r => String(r.status == null ? "" : r.status).trim().toLowerCase();

    const at = key => work.filter(r => statusOf(r) === key);
    const planned = at("planned");
    const queued = at("queued");
    const running = at("running");
    const doneRows = at("done");
    const STOP = ["attention", "blocked", "failed"];
    const stopped = work.filter(r => STOP.includes(statusOf(r)));
    const KNOWN = new Set(["planned", "queued", "running", "done", ...STOP]);
    const unstationed = work.filter(r => statusOf(r) === "");
    const foreign = work.filter(r => statusOf(r) !== "" && !KNOWN.has(statusOf(r)));

    const stale = snapshot.stale ? "STALE · " : "";
    const genOk = Number.isFinite(Date.parse(String(snapshot.generated_at || "")));

    // ------------------------------------------------------------ context
    const ctxOk = !!(context && Array.isArray(context.items));
    const items = [];
    // A malformed FIELD is not an unreadable ENTRY: the entry is still counted
    // everywhere else, so it must never be folded into the "counted nowhere"
    // tally — only the field it fumbled goes uncounted.
    let malformed = 0;
    if (ctxOk) context.items.forEach(i => {
      if (!i || i.id == null) { unreadable += 1; return; }
      if (("artifact_recorded" in i && typeof i.artifact_recorded !== "boolean")
        || ("claim_present" in i && typeof i.claim_present !== "boolean")) malformed += 1;
      items.push(i);
    });
    const ctxMap = new Map(items.map(i => [String(i.id), i]));
    const rowMap = new Map(rows.map(r => [String(r.id), r]));

    const hasClaimField = items.some(i => "claim_present" in i);
    const hasLeaseField = items.some(i => "lease_remaining_s" in i);
    // A claim is only drawn at the Claimed station if it lands on a work row.
    // A claim on an initiative, or on an id this snapshot does not hold, would
    // otherwise inflate a station beside a card that says initiatives stand at
    // no station — so those are split out and printed at the foot instead.
    const claimAll = hasClaimField ? items.filter(i => i.claim_present === true) : [];
    const claimEpic = claimAll.filter(i => {
      const r = rowMap.get(String(i.id));
      return !!r && r.bucket === "epic";
    });
    const claimOffBoard = claimAll.filter(i => !rowMap.has(String(i.id)));
    const claimed = claimAll.filter(i => {
      const r = rowMap.get(String(i.id));
      return !!r && r.bucket !== "epic";
    });

    const noCtx = work.filter(r => !ctxMap.has(String(r.id)));
    const noCtxJobs = noCtx.filter(r => String(r.id).startsWith("job:"));
    const ctxWork = work.length - noCtx.length;

    const declaredAll = items.filter(i => typeof i.done_signal === "string" && i.done_signal.trim() !== "");
    const recordedAll = items.filter(i => i.artifact_recorded === true);
    const declaredDone = declaredAll.filter(i => {
      const r = rowMap.get(String(i.id));
      return !!r && statusOf(r) === "done";
    });

    // ------------------------------------------------------------ no work
    if (work.length === 0) {
      return `<div class="card stcard"><h3>Where the board holds custody</h3>
        <p class="meta">${stale ? esc(stale) : ""}This board holds ${epics.length} initiative${epics.length === 1 ? "" : "s"} and no work beneath them, so no row stands at any station.${
          unreadable ? ` ${unreadable} entr${unreadable === 1 ? "y" : "ies"} could not be read and ${unreadable === 1 ? "is" : "are"} counted nowhere.` : ""}</p></div>`;
    }

    // ------------------------------------------------------------ helpers
    const n = (count, one, many) => `${count} ${count === 1 ? one : (many || one + "s")}`;
    const unitClass = r => {
      const s = statusOf(r);
      if (s === "running") return "running m-run";
      if (s === "failed") return "failed";
      if (s === "attention" || s === "blocked") return "stopped";
      if (s === "done") return "done";
      return (r.owner == null || String(r.owner).trim() === "") ? "unowned" : "";
    };
    const byId = (a, b) => String(a.id).localeCompare(String(b.id));

    const TILE = 6, GAP = 3, PER = 13, LINES = 3, CAP = PER * LINES;
    const tiles = (list, x0, y0, ns, maxLines) => {
      const cap = PER * (maxLines || LINES);
      const shown = list.slice().sort(byId).slice(0, cap);
      const marks = shown.map((r, i) => {
        const x = x0 + (i % PER) * (TILE + GAP);
        const y = y0 + Math.floor(i / PER) * (TILE + GAP);
        const cls = unitClass(r);
        const glow = cls.includes("running") ? ` filter="url(#stfp-glow)"` : "";
        return `<rect class="st-unit ${cls}" x="${x}" y="${y}" width="${TILE}" height="${TILE}"${glow}
          data-row="${esc(String(r.id))}" data-key="${esc(ns + ":" + String(r.id))}" tabindex="0"
          ><title>${esc(`${r.id} · ${r.title == null ? "" : r.title} · ${statusOf(r) || "no status recorded"}`)}</title></rect>`;
      }).join("");
      const over = list.length > cap
        ? `<text class="st-over" x="${x0}" y="${y0 + (maxLines || LINES) * (TILE + GAP) + 8}">+${list.length - cap}</text>`
        : "";
      return marks + over;
    };

    // ------------------------------------------------------------ stations
    // Claimed reads the context document's claim_present, not a status.
    const claimedRows = claimed.map(i => rowMap.get(String(i.id)));
    const stations = [
      { key: "planned", name: "PLANNED", list: planned },
      { key: "queued", name: "QUEUED", list: queued },
      { key: "claimed", name: "CLAIMED", list: claimedRows, gap: !(ctxOk && hasClaimField) },
      { key: "running", name: "RUNNING", list: running },
      { key: "done", name: "DONE", list: doneRows },
    ];
    const X = [60, 240, 420, 600, 780], W = 132, BY = 64, BH = 104;

    const lattice = X.map(x => [x - 14, x + W + 14]).flat()
      .map(x => `<line class="st-lat" x1="${x}" y1="56" x2="${x}" y2="180"></line>`).join("");

    const boxes = stations.map((s, i) => {
      const x = X[i];
      if (s.gap) {
        return `<g><rect class="st-box gap" x="${x}" y="${BY}" width="${W}" height="${BH}" rx="3"
          ><title>Claimed: this board does not publish claim_present, so this station cannot be counted.</title></rect>
          <text class="st-name" x="${x + 12}" y="${BY + 20}">${esc(s.name)}</text>
          <text class="st-num" x="${x + 12}" y="${BY + 54}">&#8212;</text>
          <text class="st-gapnote" x="${x + 12}" y="${BY + 72}">not published</text></g>`;
      }
      return `<g><rect class="st-box" x="${x}" y="${BY}" width="${W}" height="${BH}" rx="3"></rect>
        <text class="st-name" x="${x + 12}" y="${BY + 20}">${esc(s.name)}</text>
        <text class="st-num" x="${x + 12}" y="${BY + 54}">${s.list.length}</text>
        ${tiles(s.list, x + 9, BY + 62, "st-" + s.key)}</g>`;
    }).join("");

    // Stopped tray sits ABOVE the stations, off any axis: a stopped row is not
    // further along than a running one, so it must not sit past Done.
    const tray = `<g><rect class="st-tray" x="60" y="6" width="460" height="44" rx="3"></rect>
      <text class="st-name" x="72" y="22">STOPPED &#183; NOT A STATION</text>
      <text class="st-traynum" x="72" y="42">${stopped.length}</text>
      ${stopped.length ? tiles(stopped, 280, 14, "st-stopped", 2)
        : `<text class="st-gapnote" x="280" y="32">no row on this board is stopped</text>`}</g>`;

    // The proof gate is a line, not a station: rows do not stand in it.
    let gateMain, gateSub;
    if (!ctxOk) {
      gateMain = "PROOF · not published";
      gateSub = "no context document was handed to the map";
    } else if (doneRows.length === 0) {
      gateMain = "PROOF · no row claims to be finished";
      gateSub = `context holds ${ctxWork} of ${work.length} work rows`;
    } else {
      const assessable = doneRows.filter(r => ctxMap.has(String(r.id)));
      if (assessable.length === 0) {
        gateMain = `PROOF · 0 of ${doneRows.length} assessable`;
        gateSub = `${n(doneRows.length, "done row")} ${doneRows.length === 1 ? "has" : "have"} no context entry · context holds ${ctxWork} of ${work.length} work rows`;
      } else {
        const decl = assessable.filter(r => {
          const i = ctxMap.get(String(r.id));
          return typeof i.done_signal === "string" && i.done_signal.trim() !== "";
        }).length;
        const rec = assessable.filter(r => ctxMap.get(String(r.id)).artifact_recorded === true).length;
        const un = doneRows.length - assessable.length;
        gateMain = `PROOF · ${decl} of ${assessable.length} declared · ${rec} recorded`;
        gateSub = `${un ? `${n(un, "done row")} unassessable (no context entry) · ` : ""}context holds ${ctxWork} of ${work.length} work rows`;
      }
    }

    // Only the short headline stays inside the viewBox. The sub-line is a
    // sentence whose length grows with the board's own numbers; inside a fixed
    // viewBox a long one is silently CLIPPED at x=960, so it is printed as HTML
    // beneath the rail, where it wraps instead of losing its tail.
    const gate = `<g><line class="st-gate" x1="756" y1="56" x2="756" y2="176"></line>
      <text class="st-gatelab" x="756" y="196" text-anchor="middle">${esc(gateMain)}</text></g>`;

    const rail = `<div class="gwrap st-enter st-d1"><svg class="strail" viewBox="0 0 960 216" preserveAspectRatio="xMinYMin meet" role="img"
      aria-label="Five custody stations: ${planned.length} planned, ${queued.length} queued, ${
        ctxOk && hasClaimField ? `${claimedRows.length} claimed` : "claimed not published"}, ${running.length} running, ${
        doneRows.length} done, with ${stopped.length} stopped rows in a tray above and a proof gate drawn as a line between running and done. No mark connects the stations; the same facts are listed in text below.">
      <defs><filter id="stfp-glow" x="-80%" y="-80%" width="260%" height="260%">
        <feGaussianBlur in="SourceGraphic" stdDeviation="1.4" result="b"></feGaussianBlur>
        <feMerge><feMergeNode in="b"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge>
      </filter></defs>
      ${lattice}${tray}${boxes}${gate}</svg></div>
      <p class="meta st-gatesub st-enter st-d1">${esc(gateSub)}</p>`;

    // ------------------------------------------------------------ lease strip
    let lease;
    if (!ctxOk || !hasClaimField) {
      lease = `<p class="meta st-gapline st-enter st-d2">This board does not publish claims to this view, so no lease horizon can be drawn.</p>`;
    } else if (!hasLeaseField) {
      lease = `<p class="meta st-gapline st-enter st-d2">This board does not publish lease horizons. A claim is either recorded or not; how long it has left is not on the wire, so nothing here can say which running rows are about to lose theirs.</p>`;
    } else if (!genOk) {
      lease = `<p class="meta st-gapline st-enter st-d2">The board's own timestamp could not be read, and the lease horizon can only be measured against it &mdash; never against this browser's clock.</p>`;
    } else {
      const buckets = [
        { key: "expired", label: "expired (≤0)", rows: [] },
        { key: "imminent", label: "under 5 min", rows: [] },
        { key: "b3", label: "5–30 min", rows: [] },
        { key: "b4", label: "30 min – 2 h", rows: [] },
        { key: "b5", label: "over 2 h", rows: [] },
        { key: "none", label: "no lease published", rows: [] },
      ];
      claimed.forEach(i => {
        const s = i.lease_remaining_s;
        if (typeof s !== "number" || !Number.isFinite(s)) { buckets[5].rows.push(i); return; }
        if (s <= 0) buckets[0].rows.push(i);
        else if (s < 300) buckets[1].rows.push(i);
        else if (s < 1800) buckets[2].rows.push(i);
        else if (s < 7200) buckets[3].rows.push(i);
        else buckets[4].rows.push(i);
      });
      const cells = buckets.map(b => `<div class="st-cell ${b.key}">
        <div class="st-ticks">${b.rows.slice().sort(byId).map(i => {
          const s = i.lease_remaining_s;
          const secs = (typeof s === "number" && Number.isFinite(s)) ? `${s} s remaining against the board's generated-at` : "no lease published";
          return `<button class="st-tick" data-row="${esc(String(i.id))}" data-key="${esc("st-lease:" + String(i.id))}"
            title="${esc(`${i.id} · ${secs}`)}"></button>`;
        }).join("")}</div>
        <span class="st-blab">${esc(b.label)}</span><span class="st-bcount">${b.rows.length}</span>
      </div>`).join("");
      lease = `<div class="st-enter st-d2"><h4 class="st-h">LEASE HORIZON &mdash; ${n(claimed.length, "claim")}, bucketed, never a countdown</h4>
        <div class="stlease">${cells}</div></div>`;
    }

    // ------------------------------------------------------------ drop ledger
    const chip = r => {
      const row = rowMap.get(String(r.id)) || null;
      return `<button class="chip ${row ? esc(String(stateOf(row))) : ""}" data-row="${esc(String(r.id))}"
        data-key="${esc("st-led:" + String(r.id))}" title="${esc(row && row.title != null ? String(row.title) : String(r.id))}">${esc(String(r.id))}</button>`;
    };

    let ledgerLeft;
    if (!ctxOk) {
      ledgerLeft = `<div><h4 class="st-h">STOPPED, AND WHAT IT LEFT BEHIND</h4>
        <p class="meta">This board did not hand the map a context document, so nothing here can say why anything stopped.</p></div>`;
    } else if (stopped.length === 0) {
      ledgerLeft = `<div><h4 class="st-h">STOPPED, AND WHAT IT LEFT BEHIND</h4>
        <p class="meta">No row on this board is stopped.</p></div>`;
    } else {
      const bins = new Map(); // label -> {rows, none, noctx}
      stopped.forEach(r => {
        const i = ctxMap.get(String(r.id));
        let label, cls;
        if (!i) { label = "no context entry"; cls = "noctx"; }
        else {
          const c = typeof i.blocked_reason_class === "string" ? i.blocked_reason_class.trim() : "";
          label = c || "no class recorded";
          cls = c ? "" : "none";
        }
        if (!bins.has(label)) bins.set(label, { cls, rows: [] });
        bins.get(label).rows.push(r);
      });
      const order = [...bins.keys()].sort((a, b) => {
        const rank = k => k === "no class recorded" ? 1 : k === "no context entry" ? 2 : 0;
        return rank(a) - rank(b) || a.localeCompare(b);
      });
      const allUnclassified = bins.size === 1 && bins.has("no class recorded");
      ledgerLeft = `<div><h4 class="st-h">STOPPED, AND WHAT IT LEFT BEHIND</h4>
        ${order.map(label => {
          const bin = bins.get(label);
          const note = label === "no context entry"
            ? " &mdash; absent from the context document, so no reason class can be read either way"
            : label === "no class recorded"
              ? " &mdash; a context entry exists and its reason class is empty"
              : "";
          return `<p class="st-class ${bin.cls}"><span class="st-clab">${esc(label)}</span>
            <b class="st-n">${bin.rows.length}</b>${note}<span class="chips">${
            bin.rows.slice().sort(byId).map(chip).join("")}</span></p>`;
        }).join("")}
        ${allUnclassified ? `<p class="meta">All ${stopped.length} stopped rows carry no reason class. The board records that they stopped, not why.</p>` : ""}</div>`;
    }

    const deltaLanded = ctxOk && hasClaimField && hasLeaseField;
    const expiredCount = claimed.filter(i => typeof i.lease_remaining_s === "number"
      && Number.isFinite(i.lease_remaining_s) && i.lease_remaining_s <= 0).length;
    const reasonedCount = stopped.filter(r => {
      const i = ctxMap.get(String(r.id));
      return !!i && typeof i.blocked_reason_class === "string" && i.blocked_reason_class.trim() !== "";
    }).length;
    const asymNum = v => deltaLanded ? String(v) : "&#8212;";
    const asymNote = deltaLanded ? ""
      : `<p class="meta">This board does not publish claims and lease horizons to this view, so neither count can be given &mdash; and the reason-class count alone would silently make the released side look like the whole population.</p>`;

    const ledgerRight = `<div><h4 class="st-h">TWO WAYS A CLAIM ENDS</h4>
      <div class="st-asym"><b>The lease ran out.</b> The claim is gone and the row is claimable again.
        Nothing was written down about why it stopped, because nobody stopped it &mdash; a clock did.
        <span class="st-n">${asymNum(expiredCount)}</span> ${deltaLanded ? `claim${expiredCount === 1 ? " carries" : "s carry"} an expired lease right now.` : ""}</div>
      <div class="st-asym"><b>Someone let go of it.</b> The claim was released with a disposition, so the
        row still carries its reason class and, where one was given, the condition for resuming.
        <span class="st-n">${asymNum(deltaLanded ? reasonedCount : 0)}</span> ${deltaLanded ? `stopped row${reasonedCount === 1 ? " carries" : "s carry"} a recorded reason class.` : ""}</div>
      ${asymNote}
      ${ctxOk ? `<div class="st-asym decl"><b>Declared, never recorded.</b>
        <span class="st-n">${declaredAll.length}</span> context row${declaredAll.length === 1 ? "" : "s"} name a completion
        artifact; the board has recorded <span class="st-n">${recordedAll.length}</span> of them, and
        <span class="st-n">${declaredDone.length}</span> of the declaring rows ${declaredDone.length === 1 ? "is" : "are"} done.
        A declaration is a promise the board has not yet seen kept.</div>` : ""}</div>`;

    const ledger = `<div class="stledger st-enter st-d3">${ledgerLeft}${ledgerRight}</div>`;

    // ------------------------------------------------------------ floor notes
    const floor = [];
    if (epics.length) floor.push(`<b>${epics.length}</b> initiative${epics.length === 1 ? " is a container" : "s are containers"}, not work; ${epics.length === 1 ? "it is" : "they are"} not counted at any station.`);
    if (ctxOk && noCtx.length) {
      const structural = noCtxJobs.length === noCtx.length
        ? ` All ${noCtx.length} are job: rows, which this board's context document omits by construction &mdash; a structural absence, not a data defect.`
        : "";
      floor.push(`<b>${noCtx.length}</b> row${noCtx.length === 1 ? " has" : "s have"} no context entry; this view cannot say either way about ${noCtx.length === 1 ? "its" : "their"} proof or ${noCtx.length === 1 ? "its" : "their"} claim.${structural}`);
    }
    if (unstationed.length) floor.push(`<b>${unstationed.length}</b> row${unstationed.length === 1 ? "" : "s"} carr${unstationed.length === 1 ? "ies" : "y"} no status at all and stand${unstationed.length === 1 ? "s" : ""} at no station &mdash; never folded into Planned.`);
    if (foreign.length) floor.push(`<b>${foreign.length}</b> row${foreign.length === 1 ? "" : "s"} carr${foreign.length === 1 ? "ies" : "y"} a status this view has no station for (${
      [...new Set(foreign.map(statusOf))].sort().map(esc).join(", ")}); ${foreign.length === 1 ? "it is" : "they are"} listed here, never quietly counted as planned.`);
    if (ctxOk && hasClaimField && claimEpic.length) floor.push(`<b>${claimEpic.length}</b> claim${claimEpic.length === 1 ? " lands" : "s land"} on an initiative rather than on work; ${claimEpic.length === 1 ? "it is" : "they are"} held out of the Claimed station, because an initiative stands at no station.`);
    if (ctxOk && hasClaimField && claimOffBoard.length) floor.push(`<b>${claimOffBoard.length}</b> claim${claimOffBoard.length === 1 ? "" : "s"} name${claimOffBoard.length === 1 ? "s" : ""} an id this snapshot does not hold, so ${claimOffBoard.length === 1 ? "it stands" : "they stand"} at no station and ${claimOffBoard.length === 1 ? "is" : "are"} not counted in the claims figure.`);
    if (malformed) floor.push(`<b>${malformed}</b> context entr${malformed === 1 ? "y publishes" : "ies publish"} a proof-or-claim field that is not a true-or-false value. ${malformed === 1 ? "That entry is" : "Those entries are"} still counted everywhere else on this screen, but never as a recorded artifact and never as a claim.`);
    if (unreadable) floor.push(`<b>${unreadable}</b> entr${unreadable === 1 ? "y" : "ies"} in these documents carr${unreadable === 1 ? "ies" : "y"} no id at all, so ${unreadable === 1 ? "it" : "they"} could not be read and ${unreadable === 1 ? "is" : "are"} counted nowhere above. Every count on this screen is a floor.`);
    const empty = stations.filter(s => !s.gap && s.list.length === 0).map(s => s.name.toLowerCase());
    if (empty.length) floor.push(`${empty.map(esc).join(", ")} stand${empty.length === 1 ? "s" : ""} empty. An empty station is drawn, not removed: an empty station is a fact about the board.`);
    if (doneRows.length === work.length) floor.push(`Every row here is done. This snapshot carries no history, so nothing here says any of them ever ran.`);

    // Reconciliation against the board's own summary, computed live.
    const sm = snapshot.summary;
    if (!sm || typeof sm !== "object") {
      floor.push("This board published no summary to reconcile against.");
    } else {
      // The summary counts EVERY row the board holds, initiatives included, so
      // the reconciliation has to be done over every row too. That means a
      // figure here can be larger than the station drawn above it — so every
      // figure names its population, and the initiative share is spelled out
      // rather than left to collide with the station's numeral.
      const allBy = k => rows.filter(r => statusOf(r) === k).length;
      const workBy = k => work.filter(r => statusOf(r) === k).length;
      const num = v => (typeof v === "number" && Number.isFinite(v)) ? v : null;
      const split = k => {
        const a2 = allBy(k), w2 = workBy(k), e2 = a2 - w2;
        return e2 ? ` (${w2} of them work rows drawn at ${k}, plus ${e2} initiative${e2 === 1 ? "" : "s"} that stand at no station)` : "";
      };
      const parts = [];
      const q = allBy("queued"), p = allBy("planned");
      const smNext = num(sm.next);
      if (smNext != null && smNext !== q) {
        parts.push(`counts ${smNext} in its next bucket while ${q} row${q === 1 ? " carries" : "s carry"} the status queued${
          smNext === q + p ? ` &mdash; the arithmetic closes only when the ${p} rows carrying planned are folded in${split("planned")}` : ""}`);
      }
      const a = allBy("attention"), b = allBy("blocked"), f = allBy("failed");
      const smAtt = num(sm.attention);
      if (smAtt != null && smAtt !== a) {
        parts.push(`counts ${smAtt} in its attention bucket while ${a} row${a === 1 ? " carries" : "s carry"} the status attention${
          smAtt === a + b + f ? ` &mdash; it closes only when the ${b} carrying blocked${split("blocked")} and ${f} carrying failed${split("failed")} are folded in` : ""}`);
      }
      [["running", "running"], ["done", "done"]].forEach(([bk, st]) => {
        const v = num(sm[bk]);
        if (v != null && v !== allBy(st)) parts.push(`counts ${v} ${bk} while ${allBy(st)} row${allBy(st) === 1 ? " carries" : "s carry"} that status${split(st)}`);
      });
      floor.push(parts.length
        ? `The board's own summary ${parts.join("; it ")}. This view counts statuses, not buckets.`
        : "The board's summary and its row statuses agree on every count this view draws. This view counts statuses, not buckets.");
    }

    const floorHtml = floor.length
      ? `<ul class="st-floor st-enter st-d4">${floor.map(t => `<li>${t}</li>`).join("")}</ul>` : "";

    // ------------------------------------------------------------ stat strip
    const dash = "&#8212;";
    const stat = (label, value) => `<div class="st-stat"><b>${value}</b><span>${esc(label)}</span></div>`;
    const stats = `<div class="st-stats st-enter st-d1">${[
      stat("work rows", String(work.length)),
      stat("epics excluded", String(epics.length)),
      stat("claims", ctxOk && hasClaimField ? String(claimed.length) : dash),
      stat("declared", ctxOk ? String(declaredAll.length) : dash),
      stat("recorded", ctxOk ? String(recordedAll.length) : dash),
      stat("stopped", String(stopped.length)),
      stat("no context", ctxOk ? String(noCtx.length) : dash),
    ].join("")}</div>`;

    // ------------------------------------------------------------ copy
    const caption = `${esc(stale)}${work.length} row${work.length === 1 ? "" : "s"} across five stations, ${
      epics.length} initiative${epics.length === 1 ? "" : "s"} excluded${
      ctxOk ? `, ${noCtx.length} with no context entry` : ", no context document"}.`;

    const readnote = `<details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body">
      <p>Five stations, left to right. That order is a reading convention and nothing more. This board
      publishes where each row stands right now; it has never recorded a row moving from one station to
      another. So there is no funnel here, no drop-off between stages, no connecting line between the
      boxes, and no percentage anywhere in this view. If you want to know whether the rows at Done ever
      passed through Running, this screen cannot tell you, and neither can any other.</p>
      <p>Every station box is the same size and the same distance from its neighbours. Width, height and
      the gaps between stations mean nothing. One small tile is one row, and every tile is the same size
      &mdash; the only quantity any mark encodes is a count of records. The tiles wrap at a fixed line
      width, so the number of lines a station fills is a wrapping artefact: only the numeral and the
      count of tiles are claims. Tiles sit in alphabetical order of row id, which is a sorting
      convention and nothing more: a tile's position inside its station is not an arrival order, a
      queue position, or a rank. Every tile is drawn at the same weight, so a station holding more
      records looks like it holds more records. Where this view gives a status a colour, a tile carries
      the row's own status colour and nothing else; otherwise it is neutral, and a hollow dashed tile
      means only that the row records no owner. Above the cap a station prints the
      remainder as a number rather than shrinking anything. The stopped tray sits above the stations
      because a stopped row is not further along than a running one; its position carries nothing.</p>
      <p>Stations read the row's own <em>status</em>, not the board's summary buckets. Where the two
      disagree, the difference is printed at the foot rather than reconciled away. A status this view has
      no station for is listed at the foot; it is never quietly counted as planned. The Claimed station
      reads the context document's claim record, not a status.</p>
      <p>Proof is drawn as a line between Running and Done, not as a station, because it is not a place a
      row stands &mdash; it is a question asked of a row that claims to be finished. The Claimed station
      counts claims that land on work rows; a claim on an initiative, or on an id this snapshot does not
      hold, is printed at the foot rather than folded into the station. Declared means the
      row names a completion artifact. Recorded means the board has seen that artifact. A row can be done
      with neither &mdash; and a done row the context document does not cover cannot be asked the question
      at all, which is printed as unassessable, never as a zero.</p>
      <p>The lease horizon is grouped into ordered buckets, never a countdown bar. Remaining lease time is
      not elapsed time and not a count of anything, so nothing here is drawn to its length; only the
      ordering survives. Every tick is the same size. The arithmetic is the lease deadline minus the
      board's own generated-at timestamp, never this browser's clock &mdash; and it is not an estimate of
      remaining work, so a long horizon does not mean a long job. Claims with no lease on the wire sit in
      their own bucket rather than being read as unclaimed.</p>
      <p>The two ways a claim ends are not symmetric. An expired lease drops the claim and leaves nothing
      behind: the row becomes claimable again, and the board holds no record of why it stopped, because
      nobody stopped it. A deliberate release keeps the disposition &mdash; a reason class, and sometimes
      a resume condition. Those two counts are printed as numbers, side by side, and never as two bars:
      they are not two halves of one population.</p>
      <p>This view reads only the snapshot and the context document. It uses no dependency edge and no
      recorded event, so nothing here implies an order of occurrence or a relationship between rows. It
      never reads progress, ETA, priority or the current step: the first two are durations this board
      cannot substantiate, and the last two say nothing about custody. Counts of blocked reasons and
      resume conditions are counts only &mdash; the text of either is never shown here.</p>
      <p>Motion: a tile for a derived-running row breathes at a fixed period that carries no rate; a brief
      pulse marks an element whose identity was absent from the previous paint, and nothing else. The faint
      vertical lattice behind the stations is decoration and carries nothing.</p>
      <p>Every count on this screen is a floor. Rows the board holds no context entry for cannot be
      assessed for proof or for a claim, and are listed at the foot rather than assumed either way.</p>
    </div></details>`;

    // ------------------------------------------------------------ assemble
    return `<div class="card stcard">
      <h3>Where the board holds custody</h3>
      <p class="meta stcap">${caption}</p>
      ${readnote}
      ${stats}
      ${rail}
      ${lease}
      ${ledger}
      ${floorHtml}
    </div>`;
  };
})();
