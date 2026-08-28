// One product, one navigation. Static pages carry a useful fallback shell;
// this module normalizes every route (including the Work root) to the same
// three product areas and one compact route-local switcher.
(() => {
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
    if (view === "work") return "board";
    return ["overview", "attention", "jobs", "graph", "comms"].includes(view) ? view : "overview";
  };

  const globalDestinations = [
    { label: "Work", href: "/#v=overview", key: "work" },
    { label: "Intelligence", href: "/map", key: "intelligence" },
    { label: "Usage", href: "/#v=usage", key: "usage" },
  ];
  const localDestinations = {
    work: [
      { label: "Overview", href: "/#v=overview", key: "overview" },
      { label: "Board", href: "/#v=work&layout=list", key: "board" },
      { label: "Attention", href: "/#v=attention", key: "attention" },
      { label: "Jobs", href: "/#v=jobs", key: "jobs" },
      { label: "Graph", href: "/#v=graph", key: "graph" },
      { label: "Comms", href: "/#v=comms", key: "comms" },
    ],
    intelligence: [
      { label: "Map", href: "/map", key: "map" },
      { label: "Mesh", href: "/mesh", key: "mesh" },
      { label: "Operations Atlas", href: "/ops", key: "atlas" },
    ],
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
