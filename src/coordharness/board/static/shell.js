// One product, one navigation. Static pages carry a useful fallback shell;
// this module normalizes every route (including the Work root) to the same
// three product areas and one compact route-local switcher.
(() => {
  // ---- the one destination model ----------------------------------------
  // There used to be two. This file listed six Work destinations flat, and
  // app.js listed seven of its own grouped into Work/More for a rail that
  // ships hidden. Two lists that must agree and are edited separately do not
  // stay in agreement, so the list lives here -- this module loads on every
  // page, app.js loads on one -- and app.js reads it rather than restating it.
  //
  // `key` names the destination in this navigation; `panel` names the DOM
  // panel app.js activates for it. They differ in exactly one place ("Board"
  // shows `#work`) and writing that down once removes the special case that
  // used to be spelled out in three functions.
  const globalDestinations = [
    { label: "Work", href: "/#v=overview", key: "work" },
    { label: "Intelligence", href: "/map", key: "intelligence" },
    { label: "Usage", href: "/#v=usage", key: "usage" },
  ];
  const localDestinations = {
    work: [
      { label: "Overview", href: "/#v=overview", key: "overview", panel: "overview" },
      { label: "Board", href: "/#v=work&layout=list", key: "board", panel: "work" },
      { label: "Attention", href: "/#v=attention", key: "attention", panel: "attention" },
      { label: "Jobs", href: "/#v=jobs", key: "jobs", panel: "jobs" },
      { label: "Graph", href: "/#v=graph", key: "graph", panel: "graph" },
      { label: "Comms", href: "/#v=comms", key: "comms", panel: "comms" },
    ],
    intelligence: [
      { label: "Map", href: "/map", key: "map" },
      { label: "Mesh", href: "/mesh", key: "mesh" },
      { label: "Operations Atlas", href: "/ops", key: "atlas" },
    ],
  };
  // Every board panel this shell can route to, in navigation order. Usage is
  // reached from the product-area row rather than the Work switcher, so it is
  // appended here: a destination that exists is named exactly once, and a
  // panel absent from this list is a panel nothing can navigate to.
  const boardPanels = [
    ...localDestinations.work.map(item => ({ id: item.panel, label: item.label })),
    ...globalDestinations
      .filter(item => item.key === "usage")
      .map(item => ({ id: item.key, label: item.label })),
  ];
  // Published before the early return below: a page without a shellbar still
  // has one destination model, and app.js must never fall back to a second.
  window.CoordNav = { globalDestinations, localDestinations, boardPanels };

  const search = new URLSearchParams(window.location.search);
  const embedded = search.get("embedded") === "1";
  if (embedded) document.documentElement.setAttribute("data-embedded", "1");

  const shell = document.querySelector(".shellbar");
  const primary = shell?.querySelector(".shell-nav");
  if (!shell || !primary) return;

  const makeLink = ({ label, href, key }, current) => {
    const link = document.createElement("a");
    link.textContent = label;
    link.href = href;
    link.dataset.shellDestination = key;
    if (key === current) link.setAttribute("aria-current", "page");
    return link;
  };

  const hashCapsule = () => new URLSearchParams(
    (window.location.hash || "").replace(/^#/, ""),
  );

  const currentArea = () => {
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    if (["/map", "/mesh", "/ops"].includes(path)) return "intelligence";
    if (path === "/" && (document.body.dataset.view || hashCapsule().get("v")) === "usage") {
      return "usage";
    }
    return "work";
  };

  const currentLocal = area => {
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    if (area === "intelligence") {
      if (path === "/mesh") return "mesh";
      if (path === "/ops") return "atlas";
      return "map";
    }
    const view = document.body.dataset.view || hashCapsule().get("v") || "overview";
    const match = localDestinations.work.find(item => item.panel === view);
    return match ? match.key : "overview";
  };

  let secondary = document.querySelector(".shell-subnav");

  const sync = () => {
    const area = currentArea();
    primary.replaceChildren(...globalDestinations.map(item => makeLink(item, area)));

    const destinations = localDestinations[area];
    if (embedded || !destinations) {
      secondary?.remove();
      secondary = null;
      document.body.classList.remove("has-shell-subnav");
      return;
    }

    if (!secondary) {
      secondary = document.createElement("nav");
      secondary.className = "shell-subnav";
      shell.insertAdjacentElement("afterend", secondary);
    }
    secondary.setAttribute(
      "aria-label",
      area === "intelligence" ? "Intelligence views" : "Work views",
    );
    const local = currentLocal(area);
    secondary.replaceChildren(...destinations.map(item => makeLink(item, local)));
    document.body.classList.add("has-shell-subnav");
  };

  sync();
  new MutationObserver(sync).observe(document.body, {
    attributes: true,
    attributeFilter: ["data-view"],
  });
  window.addEventListener("hashchange", sync);
  window.addEventListener("popstate", sync);
})();
