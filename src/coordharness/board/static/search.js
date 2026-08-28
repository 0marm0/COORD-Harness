// Search as navigation.
//
// The map answers "what is the state of every row" five different ways, and
// every one of them is a browse: you scan a matrix, a graph, a list. The thing
// you actually do when a fleet is running is arrive already knowing the row --
// somebody said N0812 in a message, or you remember half a title -- and the
// only way to reach it was to find it inside a view first. This is the other
// door: type, and the row itself is the destination.
//
// One global: coordSearchMount(container, api). Everything else is closed over
// inside the module function, because these assets load as classic scripts on
// one page -- a top-level `const esc` here would collide with the identically
// named binding in cockpit.js and abort the whole script with a redeclaration
// error. Nothing here is redefined on the page's behalf and nothing is read
// from it: the rows arrive through `api`.
//
// api = { rows: () => Array<row>, select: (id) => void }
// row = { id, title, status, owner, module, current_step, bucket }
//
// Styles ship as /static/search.css. The board sends `default-src 'self'` with
// no unsafe-inline, so an injected <style> element gets a null sheet and every
// rule in it is dropped silently; a style= attribute is dropped the same way.
// Nothing here writes either.

(function (root) {
  "use strict";

  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Twelve is a screenful. The count that matters is announced either way, so a
  // query matching forty rows says so rather than pretending it matched twelve.
  const MAX_RESULTS = 12;

  // Ranking policy. The order is the order the fields are trusted in: an id is
  // the thing you were told, a title is the thing you remember, a step and an
  // owner are what you'd reach for when you remember neither.
  //
  // The third tier is the one addition to the stated order: an id matched in the
  // middle rather than at the front, which otherwise finds nothing at all. It
  // sits with the other two id tiers rather than below the rest, because an id
  // fragment is still the strongest thing you can be holding -- `J-77-N0812`
  // must outrank a row whose *title* merely mentions N0812. That is also where
  // the ranker this replaces put it, so the swap does not silently reorder
  // anybody's results.
  const TIERS = [
    { field: "exact id", test: (f, q) => f.id === q },
    { field: "id", test: (f, q) => f.id.startsWith(q) },
    { field: "id", test: (f, q) => f.id.includes(q) },
    { field: "title", test: (f, q) => f.title.includes(q) },
    { field: "step", test: (f, q) => f.step.includes(q) },
    { field: "owner", test: (f, q) => f.owner.includes(q) },
  ];

  const lower = value => String(value == null ? "" : value).toLowerCase();

  // Pure, DOM-free and exported, so the ranking can be pinned by a
  // dependency-free Node test rather than only through a rendered page.
  function rankRows(rows, query) {
    const needle = String(query == null ? "" : query).trim().toLowerCase();
    if (!needle) return [];
    const matches = [];
    (Array.isArray(rows) ? rows : []).forEach(row => {
      if (!row || row.id == null || row.id === "") return;
      const fields = {
        id: lower(row.id),
        title: lower(row.title),
        step: lower(row.current_step),
        owner: lower(row.owner),
      };
      for (let tier = 0; tier < TIERS.length; tier += 1) {
        if (!TIERS[tier].test(fields, needle)) continue;
        matches.push({ row: row, field: TIERS[tier].field, rank: tier });
        return;
      }
    });
    // Ties are broken by title then id so the list is stable across the five
    // second repaint: a result that moves under the cursor is a misclick.
    matches.sort((a, b) => a.rank - b.rank
      || String(a.row.title == null ? "" : a.row.title)
        .localeCompare(String(b.row.title == null ? "" : b.row.title))
      || String(a.row.id).localeCompare(String(b.row.id)));
    return matches;
  }

  // Mounted controllers, keyed by container. A second mount returns the first
  // one instead of building a second input over the same rows.
  const mounts = new WeakMap();
  let sequence = 0;

  function hitMarkup(match, index, active, base) {
    const row = match.row;
    // Only fields the row actually carries are drawn. An em dash standing in
    // for an absent owner would read as a fact about the row.
    const meta = [row.owner, row.module].filter(v => v != null && v !== "")
      .map(v => `<span>${esc(v)}</span>`).join("");
    const status = row.status == null || row.status === "" ? "" :
      `<span class="coordsearch-state s-${esc(lower(row.status).replace(/[^a-z0-9]+/g, "-"))}">${
        esc(row.status)}</span>`;
    return `<button type="button" class="coordsearch-hit" id="${base}-opt-${index}"
      role="option" aria-selected="${active ? "true" : "false"}"
      data-row="${esc(row.id)}" data-index="${index}" tabindex="0">
      <b class="coordsearch-hit-id">${esc(row.id)}</b>
      <span class="coordsearch-hit-title">${esc(row.title == null ? "" : row.title)}</span>
      <span class="coordsearch-hit-meta">${status}${meta}</span>
      <small class="coordsearch-hit-field">${esc(match.field)}</small>
    </button>`;
  }

  function announce(asked, shown, total) {
    if (!asked) return "";
    if (!total) return "No matching row.";
    if (total > shown) return `Showing ${shown} of ${total} matching rows.`;
    return `${total} matching row${total === 1 ? "" : "s"}.`;
  }

  function coordSearchMount(container, api) {
    if (!container || typeof container.querySelector !== "function") return null;

    const existing = mounts.get(container);
    if (existing) {
      // Not a double mount: the same input, pointed at whatever rows the caller
      // is holding now.
      existing.setApi(api);
      return existing;
    }
    // A root left behind by a controller this page no longer holds -- a script
    // reload, say. Dropping it keeps the invariant that one container carries
    // exactly one search.
    const orphan = container.querySelector(".coordsearch");
    if (orphan && orphan.parentNode) orphan.parentNode.removeChild(orphan);

    sequence += 1;
    const base = `coordsearch-${sequence}`;
    let source = api;
    let index = -1;
    let shown = [];

    const root = container.ownerDocument.createElement("div");
    root.className = "coordsearch";
    root.innerHTML = `<label class="coordsearch-label" for="${base}-input">Find a row</label>
      <input id="${base}-input" class="coordsearch-input" type="search" role="combobox"
        autocomplete="off" spellcheck="false" aria-autocomplete="list"
        aria-controls="${base}-results" aria-expanded="false"
        placeholder="ID, title, owner, or current step">
      <p id="${base}-live" class="coordsearch-live" role="status" aria-live="polite"></p>
      <div id="${base}-results" class="coordsearch-results" role="listbox"
        aria-label="Matching rows" hidden></div>`;
    container.appendChild(root);

    const input = root.querySelector(`#${base}-input`);
    const results = root.querySelector(`#${base}-results`);
    const live = root.querySelector(`#${base}-live`);

    const rows = () => {
      if (!source || typeof source.rows !== "function") return [];
      try {
        const value = source.rows();
        return Array.isArray(value) ? value : [];
      } catch (error) {
        return [];
      }
    };

    // Rows are read here, on the keystroke, and never cached: the page repaints
    // every five seconds and a list built against the previous snapshot would
    // navigate to a row that has since moved.
    function render() {
      const asked = Boolean(input.value.trim());
      const all = rankRows(rows(), input.value);
      shown = all.slice(0, MAX_RESULTS);
      index = shown.length ? Math.max(0, Math.min(index, shown.length - 1)) : -1;
      results.hidden = !asked;
      input.setAttribute("aria-expanded", asked && shown.length ? "true" : "false");
      results.innerHTML = !asked ? ""
        : shown.length
          ? shown.map((match, i) => hitMarkup(match, i, i === index, base)).join("")
          : `<p class="coordsearch-empty">No matching row.</p>`;
      live.textContent = announce(asked, shown.length, all.length);
      if (asked && index >= 0) input.setAttribute("aria-activedescendant", `${base}-opt-${index}`);
      else input.removeAttribute("aria-activedescendant");
      return shown;
    }

    function collapse(options) {
      const clear = Boolean(options && options.clear);
      if (clear) input.value = "";
      index = -1;
      shown = [];
      results.hidden = true;
      results.innerHTML = "";
      live.textContent = "";
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function open(id) {
      if (id == null || id === "") return;
      collapse({ clear: true });
      if (source && typeof source.select === "function") source.select(String(id));
    }

    function move(delta) {
      const list = render();
      if (!list.length) return;
      index = (Math.max(index, 0) + delta + list.length) % list.length;
      render();
      const active = results.querySelector('[aria-selected="true"]');
      if (active && typeof active.scrollIntoView === "function") {
        active.scrollIntoView({ block: "nearest" });
      }
    }

    input.addEventListener("input", () => {
      // The top result is highlighted as you type, so Enter is never a guess
      // about which row it will open.
      index = 0;
      render();
    });

    input.addEventListener("keydown", event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        move(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Enter") {
        const list = render();
        if (!list.length) return;
        event.preventDefault();
        open(list[Math.max(index, 0)].row.id);
        return;
      }
      if (event.key === "Escape") {
        // Only swallowed when there was something to clear. With an empty box
        // the key belongs to whatever else on the page listens for it.
        const held = Boolean(input.value) || !results.hidden;
        if (held) {
          event.preventDefault();
          event.stopPropagation();
        }
        collapse({ clear: true });
        input.blur();
      }
    });

    // Both handlers stop propagation: the page also delegates [data-row] on the
    // document, and letting the event through would select the row twice and
    // push two history entries for one click.
    results.addEventListener("click", event => {
      const hit = event.target.closest("[data-row]");
      if (!hit) return;
      event.stopPropagation();
      open(hit.dataset.row);
    });

    results.addEventListener("keydown", event => {
      const hit = event.target.closest("[data-row]");
      if (!hit) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        open(hit.dataset.row);
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        index = Number(hit.dataset.index);
        move(event.key === "ArrowDown" ? 1 : -1);
        const active = results.querySelector('[aria-selected="true"]');
        if (active) active.focus();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        collapse({ clear: true });
        input.blur();
      }
    });

    // The list starts collapsed as a state, not merely as a `hidden` attribute
    // in the markup: everything that opens it goes through render(), so the
    // closed state is set by the same code path that reopens it.
    collapse();

    const controller = {
      root: root,
      focus: () => input.focus(),
      clear: () => collapse({ clear: true }),
      refresh: () => { if (input.value.trim()) render(); },
      setApi: next => { if (next && typeof next.rows === "function") source = next; },
      destroy: () => {
        collapse({ clear: true });
        if (root.parentNode) root.parentNode.removeChild(root);
        mounts.delete(container);
      },
    };
    mounts.set(container, controller);
    return controller;
  }

  coordSearchMount.rank = rankRows;
  root.coordSearchMount = coordSearchMount;
  root.coordSearchRank = rankRows;
})(typeof globalThis === "object" ? globalThis : this);
