// The shared motion layer. Every animated behaviour on the map goes through
// here, because motion is a claim like any other mark: a pulse says "this just
// arrived", a marching dash says "this direction is recorded", a breathing
// glow says "this is derived-running right now". Views opt in with classes and
// data attributes; they never define their own keyframes for these meanings,
// so one audit of this file audits every moving thing on the page.
//
// Honesty contract:
//   .m-new       — applied HERE, never by a view: only to elements whose
//                  data-key was absent from the previous paint. A re-render
//                  replays nothing; arrival pulses fire from real data diffs.
//   .m-flow      — views put this on SVG paths whose geometry runs FROM the
//                  provider TO the consumer of a recorded dependency. The
//                  march direction is the recorded direction, nothing else.
//   .m-run       — views put this on marks for rows whose derived status is
//                  running. The breath is steady-state and carries no rate.
//   .m-ticker    — a container whose children are newest-first; the layer
//                  keeps it pinned to the top unless the reader has scrolled
//                  or is hovering.
//   .m-ambient   — decorative drift. It carries NOTHING, and any view using
//                  it must say so in its own copy.
// All of it is disabled under prefers-reduced-motion by motion.css; nothing
// here encodes information that is not also present standing still.
(() => {
  "use strict";

  const seenByPanel = new Map(); // panel id -> Set of data-key strings

  function stampArrivals(root) {
    const panel = root.closest(".panel");
    const panelId = panel ? panel.id : "_";
    const keyed = root.querySelectorAll("[data-key]");
    const seen = seenByPanel.get(panelId);
    const next = new Set();
    keyed.forEach(el => {
      const key = el.getAttribute("data-key");
      next.add(key);
      // First paint of a panel stamps nothing: everything would be "new",
      // which is the false claim this layer exists to prevent.
      if (seen && !seen.has(key)) el.classList.add("m-new");
    });
    seenByPanel.set(panelId, next);
  }

  function keepTickersPinned(root) {
    root.querySelectorAll(".m-ticker").forEach(el => {
      if (el.matches(":hover")) return;
      if (el.dataset.mReaderScrolled === "1") return;
      el.scrollTop = 0;
      if (!el.dataset.mTickerWired) {
        el.dataset.mTickerWired = "1";
        el.addEventListener("scroll", () => {
          el.dataset.mReaderScrolled = el.scrollTop > 4 ? "1" : "";
        }, { passive: true });
      }
    });
  }

  // Marching dashes on recorded-direction paths. Dash phase advances via one
  // rAF loop writing a CSS custom property; CSS consumes it, so the CSP is
  // untouched and prefers-reduced-motion (which zeroes the animation in CSS)
  // wins without JS having to ask.
  let rafStarted = false;
  function startFlowClock() {
    if (rafStarted) return;
    rafStarted = true;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    let last = 0;
    function tick(ts) {
      if (!reduce.matches && ts - last > 33) { // ~30fps is plenty for a dash march
        last = ts;
        document.documentElement.style.setProperty("--m-phase", String(-(ts / 40) % 24));
      }
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  // Views call none of this directly: cockpit.js announces each paint.
  globalThis.coordMotion = {
    afterPaint(root) {
      if (!(root instanceof Element)) return;
      stampArrivals(root);
      keepTickersPinned(root);
      startFlowClock();
    },
  };
})();
