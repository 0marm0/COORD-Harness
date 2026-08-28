// The drawer timeline.
//
// The drawer answers "what is this row and where does it connect". It cannot
// answer "what has happened to it", because the snapshot carries state, not
// history: a row that was claimed, blocked, re-claimed and finished looks
// exactly like one that went straight through. This renders the sequence.
//
// What it deliberately does not render is elapsed time. The timeline endpoint
// publishes occurrence, kind and actor lane and nothing else -- no durations,
// no prose. So this draws markers at uniform spacing, never a bar or a span:
// the distance between two markers here is layout, not a measurement, and
// drawing it to scale would put a number on the page that no field carries.
//
// One global: coordTimelineHtml(workId, esc) -> Promise<string>. The document
// is fetched once per page lifetime and every later call filters the cache,
// because the drawer re-opens on every row and the board is read-only.
//
// The stylesheet is served (timeline.css), never injected: the board sends
// `style-src 'self'` with no unsafe-inline, so an injected <style> element
// gets a null sheet and every rule in it is dropped without an error.

const TL_URL = "/api/v1/timeline";
const TL_SCHEMA = "TimelineV1";

// Fallback escaper, used only if a caller omits the helper. Identical to the
// one the map passes in; interpolation is never left unescaped either way.
const tlEscape = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Markers are drawn, not typed: a glyph font is a dependency and an image is a
// request. Each shape is distinct in silhouette so the kinds stay apart at
// 14px without relying on colour -- colour here carries only three states and
// the label beside every marker carries the kind itself.
const TL_GLYPHS = {
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

// Own-property lookups only. A kind arriving as "constructor", "__proto__" or
// "toString" would otherwise resolve to something off Object.prototype instead
// of missing, and the renderer would read glyph/tone off it.
const tlOwn = (table, key) =>
  Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;

// Tone is deliberately thin. Green is the accent, so it may only mean "this
// finished"; amber and red are semantic and fixed, so they mean "waiting" and
// "failed" whatever accent is chosen. Every other kind is muted -- including
// verdict, because the endpoint redacts the verdict value, and colouring a
// review green or red would publish an outcome the payload does not carry. An
// unrecognised kind keeps its literal label and stays muted rather than being
// guessed into a colour, which is what every long snake_case control-plane kind
// (audit_request, session_closeout, handoff_superseded) will land on.
const TL_KINDS = {
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

// Resolved once, then reused: {ok:true, byId, generatedAt, items, events} or
// {ok:false, gap}. A failure is cached too -- a board that does not serve the
// endpoint will not start serving it because the drawer opened again.
let tlCache = null;
let tlInflight = null;

function tlMarker(glyph) {
  return `<svg class="tlmark" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true"
    focusable="false" fill="currentColor" stroke="currentColor" stroke-width="1.5"
    stroke-linecap="round" stroke-linejoin="round">${tlOwn(TL_GLYPHS, glyph) || TL_GLYPHS.unknown}</svg>`;
}

// Only `at`, `kind` and `actor` are ever read. Anything else the response
// happens to carry -- title, body, refs, payload, session, severity, verdict,
// trust -- is dropped here as well as at the boundary, so a server that leaks
// a field still cannot get it onto the page through this renderer.
function tlEvent(raw) {
  if (!raw || typeof raw !== "object") return null;
  return { at: raw.at == null ? "" : String(raw.at),
    kind: raw.kind == null ? "" : String(raw.kind),
    actor: raw.actor == null ? "" : String(raw.actor) };
}

// The contract says events arrive ascending, so this is a guard rather than a
// reordering: entries whose time does not parse hold their position (sort is
// stable) instead of being pushed to an end they were never recorded at.
function tlSort(events) {
  return events.slice().sort((a, b) => {
    const x = Date.parse(a.at);
    const y = Date.parse(b.at);
    if (Number.isNaN(x) || Number.isNaN(y) || x === y) return 0;
    return x - y;
  });
}

function tlIngest(doc) {
  if (!doc || typeof doc !== "object") {
    return { ok: false, gap: "the timeline endpoint answered with something this view cannot read" };
  }
  const schema = doc.schema_version == null ? "" : String(doc.schema_version);
  if (schema !== TL_SCHEMA) {
    return { ok: false, gap: schema
      ? `the timeline endpoint answers schema ${schema}; this view reads ${TL_SCHEMA}`
      : `the timeline endpoint answered without a schema version; this view reads ${TL_SCHEMA}` };
  }
  // A missing or non-array `items` is a schema violation, not an empty board.
  // Coercing it to [] would make the view say "0 rows the document carries",
  // which is a fact about the document that the document never stated.
  if (!Array.isArray(doc.items)) {
    return { ok: false, gap: "the timeline endpoint answered without a readable list of rows" };
  }
  const byId = new Map();
  let events = 0;
  let dropped = 0;
  doc.items.forEach(item => {
    if (!item || typeof item !== "object" || item.id == null) { dropped++; return; }
    const id = String(item.id);
    // Two entries under one id are concatenated rather than overwritten: the
    // contract does not promise ids are unique, and dropping the earlier one
    // would silently lose events.
    const entry = byId.get(id) || { events: [], bad: false };
    if (Array.isArray(item.events)) {
      const list = item.events.map(tlEvent).filter(Boolean);
      events += list.length;
      entry.events = entry.events.concat(list);
    } else {
      // Named but unreadable. Recorded as such so the row is never described
      // as having no events, which would assert something not in the payload.
      entry.bad = true;
    }
    byId.set(id, entry);
  });
  return { ok: true, byId, events, dropped, items: byId.size,
    generatedAt: doc.generated_at == null ? "" : String(doc.generated_at) };
}

// Entries the document carried but this view could not read are counted, never
// silently folded into the readable ones: "2 rows" would otherwise be stated
// over a document that actually listed more.
function tlDroppedNote(state) {
  if (!state.dropped) return "";
  return ` ${state.dropped} further entr${state.dropped === 1 ? "y" : "ies"} in the document `
    + `${state.dropped === 1 ? "was" : "were"} not in a shape this view can read and `
    + `${state.dropped === 1 ? "is" : "are"} not counted here.`;
}

async function tlFetch() {
  let response;
  try {
    response = await fetch(TL_URL, { headers: { Accept: "application/json" } });
  } catch {
    // Refused, blocked or offline: from the page's side the endpoint is absent.
    return { ok: false, gap: "the timeline endpoint is not served on this board" };
  }
  if (response.status === 404) {
    return { ok: false, gap: "the timeline endpoint is not served on this board" };
  }
  if (!response.ok) {
    // Something answered, so "not served" would be the wrong statement.
    return { ok: false, gap: `the timeline endpoint answered HTTP ${response.status} on this board` };
  }
  try {
    return tlIngest(await response.json());
  } catch {
    return { ok: false, gap: "the timeline endpoint answered with something this view cannot read as JSON" };
  }
}

function tlLoad() {
  if (tlCache) return Promise.resolve(tlCache);
  if (!tlInflight) {
    // tlFetch never rejects, so the cache always settles into a stated result.
    tlInflight = tlFetch().then(state => {
      tlCache = state;
      tlInflight = null;
      return state;
    });
  }
  return tlInflight;
}

function tlBlock(inner) {
  return `<div class="rel tlblock"><p class="meta">Timeline</p>${inner}</div>`;
}

function tlGap(text, esc) {
  return tlBlock(`<p class="tlgap">${esc(text)}</p>`);
}

// The time is printed as the endpoint recorded it. No relative phrasing, no
// reformatting into a locale: "3 hours ago" is a second fact derived from the
// reader's clock, and the one thing this view must not do is add facts.
function tlTime(at, esc) {
  if (!at) return `<span class="tltime meta">time not recorded</span>`;
  if (Number.isNaN(Date.parse(at))) return `<span class="tltime">${esc(at)}</span>`;
  return `<time class="tltime" datetime="${esc(at)}">${esc(at)}</time>`;
}

function tlRow(event, esc) {
  const kind = event.kind.trim().toLowerCase();
  const shape = tlOwn(TL_KINDS, kind) || { glyph: "unknown", tone: "" };
  const label = event.kind.trim() || "kind not recorded";
  const actor = event.actor.trim();
  return `<li class="tlrow${shape.tone ? ` t-${shape.tone}` : ""}">
    ${tlMarker(shape.glyph)}
    <span class="tlkind">${esc(label)}</span>
    ${tlTime(event.at, esc)}
    <span class="tlactor">${actor ? esc(actor) : "actor lane not recorded"}</span>
  </li>`;
}

async function coordTimelineHtml(workId, esc) {
  const e = typeof esc === "function" ? esc : tlEscape;
  const state = await tlLoad();
  if (!state.ok) return tlGap(state.gap, e);

  const id = workId == null ? "" : String(workId);
  const entry = state.byId.get(id);
  if (!entry) {
    // Absent from the document is not the same fact as present-and-empty, and
    // the difference is worth stating: one says the endpoint never mentioned
    // this row, the other says it mentioned it and recorded nothing on it.
    const held = `${state.items} row${state.items === 1 ? "" : "s"}`;
    return tlBlock(`<p class="tlgap">This row is not among the ${held} the timeline document carries, `
      + `so the board records no events for it here.${tlDroppedNote(state)}</p>`);
  }
  const events = tlSort(entry.events);
  if (!events.length) {
    // Named-but-unreadable and named-with-nothing-on-it are different facts.
    return tlBlock(`<p class="tlgap">${entry.bad
      ? "The timeline document names this row, but its list of events is not in a shape this view can "
        + "read, so nothing is drawn here. This is not a statement that the row has no events."
      : "The timeline carries this row with no events on it."}</p>`);
  }

  const kinds = new Set(events.map(ev => ev.kind.trim().toLowerCase()));
  const verdictNote = kinds.has("verdict")
    ? " A verdict event records that a review happened; this endpoint publishes the occurrence, not which way it went."
    : "";
  // Stated where the count is stated: if one entry under this id was unreadable
  // the count is a floor, and printing it bare would overstate what was read.
  const partialNote = entry.bad
    ? " The document carried a further entry for this row whose event list this view could not read,"
      + " so this is at least that many, not necessarily all of them."
    : "";
  const count = `${events.length} event${events.length === 1 ? "" : "s"}`;
  const footText = (state.generatedAt
    ? `Read from the timeline document generated ${tlTime(state.generatedAt, e)}.`
    : "") + tlDroppedNote(state);
  const foot = footText.trim() ? `<p class="tlfoot">${footText.trim()}</p>` : "";

  return tlBlock(`<ol class="tl" role="list">${events.map(ev => tlRow(ev, e)).join("")}</ol>
    <p class="tlnote">${count} recorded for this row, in the order the board holds them. The endpoint
    carries occurrence, kind and actor lane only: markers sit at even spacing, so the gap between two
    of them is layout and not a duration, and nothing here says how long any step took.${
      partialNote}${verdictNote}</p>
    ${foot}`);
}

// Declared at top level like the other view modules, which the board loads as
// plain scripts; the explicit assignment keeps the same call site working if it
// is ever loaded as a module instead.
globalThis.coordTimelineHtml = coordTimelineHtml;
