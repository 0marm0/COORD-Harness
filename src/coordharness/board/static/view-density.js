// Shape — the fourth view over the same read-only snapshot.
//
// Fleet answers "who holds what". Dependencies answers "what waits on what".
// Context answers "what did anyone say". None of them shows the distribution of
// the estate: how much work each vertical carries, and what state that work is
// in. Today the only way to see that a vertical is entirely un-started is to
// count rows by eye in the board table.
//
// A unit mosaic, not an area interpolation: one tile is exactly one row, so the
// thing you look at and the thing the drawer opens are the same object.
//
// Nothing here is animated except a hover cue, which is disabled under
// prefers-reduced-motion. No listeners are bound: the view re-renders whole on
// every poll, so interaction comes entirely from data-row + tabindex, which the
// page's delegated click and keydown handlers already serve.

function view_density(data) {
  const { snapshot, esc, stateOf } = data;

  // ---------------------------------------------------------------- styles
  // Injected once, tokens only, never a colour literal. Semantic colour
  // (amber = waiting, red = failed) is fixed and does not follow the accent.
  // Styles ship as a served stylesheet, not an injected <style>: the board
  // sends `style-src 'self'` with no unsafe-inline, so an injected element
  // is silently dropped -- the sheet is never even created. Weakening the
  // policy to make a view render would be the wrong trade.


  // ------------------------------------------------------------------ data
  // Initiatives are containers for the rows below them; counting both would
  // count the same work twice. Same exclusion the Fleet matrix makes.
  const rows = (snapshot.rows || []).filter(r => r.bucket !== "epic");

  if (!rows.length) {
    return `<div class="card"><h3>The shape of the estate</h3>
      <p class="meta">This board holds no rows outside its initiatives, so there is no distribution to draw.</p></div>`;
  }

  // stateOf is the map's shared reading of a status, and every other view uses
  // it — but it returns "planned" for ANY status it does not model, so a row
  // that is actively held, or one that failed, paints as "not started" under a
  // caption whose whole thesis is work nobody has started. So: keep stateOf
  // wherever it genuinely models the status, and open up its catch-all into a
  // red "failed" band and a labelled "other" band that names what it holds.
  const rawStatus = row => String(row.status == null ? "" : row.status).trim().toLowerCase();
  const bandOf = row => {
    const raw = rawStatus(row);
    const mapped = stateOf(row);
    if (mapped !== "planned" || raw === "planned") return mapped;
    return raw === "failed" ? "failed" : "other";
  };

  const BANDS = [
    ["planned", "not started"],
    ["next", "queued"],
    ["running", "running"],
    ["blocked", "blocked"],
    ["attention", "attention"],
    ["failed", "failed"],
    ["done", "done"],
    ["other", "other status"],
  ];

  // A vertical literally named "unassigned" would collide with the label for
  // rows that name no vertical, so the absent case gets a key no value can take.
  const NONE = "\u0000none";
  const modKey = row => {
    const value = row.module == null ? "" : String(row.module).trim();
    return value === "" ? NONE : value;
  };
  const modLabel = key => (key === NONE ? "unassigned" : key);

  const counts = new Map();
  rows.forEach(r => counts.set(modKey(r), (counts.get(modKey(r)) || 0) + 1));
  // Column order is meaningful — size rank — with an alphabetical tie-break so
  // the picture does not reshuffle between polls on equal counts.
  const modules = [...counts.keys()].sort((a, b) =>
    (counts.get(b) - counts.get(a)) || modLabel(a).localeCompare(modLabel(b)));

  const bandRows = new Map(BANDS.map(([key]) => [key, []]));
  rows.forEach(r => bandRows.get(bandOf(r)).push(r));

  // Rows the map has no reading for, named rather than silently absorbed.
  const otherStatuses = [...new Set(
    bandRows.get("other").map(r => rawStatus(r) || "(no status recorded)"))].sort();

  const total = rows.length;
  const pct = n => (total ? (n / total) * 100 : 0).toFixed(2);
  // One track, one denominator: both margins are a share of the same total, so
  // a footer bar and a right-hand bar of equal length mean equal shares. The
  // bar sits inside a fixed-width track rather than taking a percentage of its
  // cell, because cell widths are content-driven and would give two equal
  // shares two different lengths.
  const margin = n =>
    `<i class="dvz-track"><i class="dvz-bar" data-w="${pct(n)}"></i></i><b class="dvz-n">${n}</b>`;

  // ---------------------------------------------------------------- markup
  // One rule for both the order and the label, matching the board's own sorter
  // (coord_db.priority_sort_value): a rank at or below zero, or one that is not
  // a number, is not a rank — and rows on this board do carry priority 0.
  const rank = row => {
    const value = Number(row.priority);
    return Number.isFinite(value) && value > 0 ? value : Infinity;
  };

  const tile = row => {
    const band = bandOf(row);
    const owner = row.owner ? String(row.owner) : "";
    const priority = rank(row) === Infinity ? "no priority" : `P${rank(row)}`;
    const label = `${row.id} ${row.title || ""} — ${rawStatus(row) || "no status"}, ${
      modLabel(modKey(row))}, ${priority}, ${owner || "unassigned"}`;
    // role=button, and the state is inside the label, so colour never carries a
    // fact on its own. data-row + tabindex are all the page's delegated click
    // and Enter/Space handlers need to open the drawer on this exact row.
    return `<span class="dvz-tile ${band}${owner ? "" : " free"}" role="button" tabindex="0"
      data-row="${esc(row.id)}" title="${esc(label)}" aria-label="${esc(label)}"></span>`;
  };

  const cell = (module, band) => {
    const here = bandRows.get(band).filter(r => modKey(r) === module);
    // Priority first; a row that declares none has no rank to place it by, so it
    // sorts last, with id as the tie-break so the order is stable across polls.
    here.sort((a, b) => (rank(a) - rank(b)) || String(a.id).localeCompare(String(b.id)));
    return `<td class="dvz-slot">${here.map(tile).join("")}</td>`;
  };

  const head = `<tr><th class="dvz-corner" scope="col">state \\ vertical</th>${
    modules.map(m => `<th class="dvz-col" scope="col"><b>${esc(modLabel(m))}</b></th>`).join("")
  }<th class="dvz-margin" scope="col">share of all rows</th></tr>`;

  const body = BANDS.map(([band, label]) => {
    const here = bandRows.get(band);
    return `<tr class="dvz-band${here.length ? "" : " dvz-void"}">
      <th class="dvz-state" scope="row">${esc(label)}</th>${
      modules.map(m => cell(m, band)).join("")
    }<td class="dvz-margin">${margin(here.length)}</td></tr>`;
  }).join("");

  const foot = `<tr class="dvz-foot"><th class="dvz-state" scope="row">all</th>${
    modules.map(m => `<td class="dvz-margin">${margin(counts.get(m))}</td>`).join("")
  }<td class="dvz-margin"><b class="dvz-n">${total} rows</b></td></tr>`;

  const legend = `<p class="meta dvz-legend"><i class="dvz-tile running"></i>running
    <i class="dvz-tile blocked"></i>stalled <i class="dvz-tile failed"></i>failed
    <i class="dvz-tile planned"></i>not started
    <i class="dvz-tile other"></i>a status this map does not model
    <i class="dvz-tile done"></i>done <i class="dvz-tile planned free"></i>nobody owns it</p>`;

  const unowned = rows.filter(r => !r.owner).length;
  // The unassigned column is not a vertical, so it is not counted as one.
  const named = modules.filter(m => m !== NONE).length;
  const spread = `${total} row${total === 1 ? "" : "s"} across ${named} vertical${
    named === 1 ? "" : "s"}${modules.includes(NONE) ? ", plus a column for rows that name none" : ""}`;

  // The caption states, in plain sentences, every place this drawing could be
  // read as carrying a fact it does not carry.
  const caption = `
    <p class="meta">One tile is one row, and every tile is the same size. Size says nothing about effort,
    scope, or how far along a row is — some rows do report progress, and this view ignores it.
    ${spread}; initiatives are left out so their children are not counted twice.</p>
    <details class="readnote"><summary>How to read this, and what it does not say</summary><div class="body"><p>Columns are ordered by how many rows the vertical holds, ties alphabetically — that order
    is the only thing column position means. Every column is the same width, so width means nothing.
    ${modules.includes(NONE)
      ? "The column labelled <em>unassigned</em> is rows that name no vertical, never a guess at one."
      : ""}
    Bands run top to bottom in a fixed reading order. Blocked, attention and failed are stalls, not a stage
    that comes after running: this snapshot carries no history, so nothing here says whether such a row ever
    ran. The order is a reading convention, not a claim about sequence.</p><p>Inside a cell, tiles run most urgent first by priority, with rows that declare no priority
    last; nothing else about a tile's position means anything. A hollow tile is a row nobody owns
    (${unowned} here). A band with no rows anywhere is drawn as an empty strip rather than removed —
    an empty state is a fact about the board.</p><p>The rest of the map reads any status it does not model as "planned". This view does not:
    failed rows get their own band, and ${otherStatuses.length
      ? `the <em>other status</em> band holds ${esc(otherStatuses.join(", "))} — statuses this map has no
         reading for, which is why they are grey rather than coloured. Those same rows appear as "not
         started" in Fleet and in the drawer's badge.`
      : `no row here carries a status outside that set, so the <em>other status</em> band is empty.`}</p><p>The bars in the margins share one track and one denominator: each is that column's or that
    band's share of all ${total} rows, so a footer bar and a right-hand bar of equal length mean equal shares.
    Nothing in this view encodes dependency — for what waits on what, use the Dependencies view.</p></div></details>`;

  return `<div class="card">
    <h3>The shape of the estate</h3>
    ${caption}
    <div class="dvz-scroll">
      <table class="dvz" aria-label="Rows by vertical and state, one tile per row">
        ${head}${body}${foot}
      </table>
    </div>
    ${legend}
    <p class="dvz-note">Every tile opens the row it stands for. Above a few hundred rows the tab-stop count
    stops being reasonable, at which point a cell should degrade to one proportional block plus its count —
    and the first sentence of this caption has to change with it.</p>
  </div>`;
}
