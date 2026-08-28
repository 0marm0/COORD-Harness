// Accent switch.
//
// The palette is one hue used for "this is live" plus the ambient glow behind
// the masthead. Which hue that is, is taste, and taste differs -- so it is a
// stored preference rather than a decision baked into the stylesheet. Every
// colour that carries meaning (amber for waiting, red for failed) stays put:
// swapping those would change what the page says, not how it looks.

// Two complete palettes, not one hue swapped in. The neutrals a dark UI sits on
// are never truly neutral: a grey with a green bias reads green whatever the
// accent is doing, which is exactly what made "switch to blue" look like it had
// barely done anything. Each palette carries its own ground, panel, hairline and
// muted text, all biased toward its own hue.
//
// Semantic colour -- amber for waiting, red for failed -- is deliberately absent
// here. Those say what a row *is*; changing them with the accent would change
// what the page means, not how it looks.
const ACCENTS = {
  green: {
    label: "Green",
    hue: "#8fd7aa",
    glow: "#0e2a1e88",
    tokens: {
      "--c-bg": "#050706",
      "--c-panel": "#0f1312",
      "--c-line": "#1d2422",
      "--c-hairline": "#33403b",
      "--c-hover": "#161d1a",
      "--c-card-a": "#111614",
      "--c-card-b": "#0a0e0d",
      "--c-muted": "#8a938e",
      "--c-ink": "#eef1ee",
    },
  },
  blue: {
    label: "Blue",
    hue: "#7db4e8",
    glow: "#0b2540aa",
    tokens: {
      "--c-bg": "#04060a",
      "--c-panel": "#0d1218",
      "--c-line": "#1b232c",
      "--c-hairline": "#2f3d4b",
      "--c-hover": "#141c25",
      "--c-card-a": "#101720",
      "--c-card-b": "#080c11",
      "--c-muted": "#88919c",
      "--c-ink": "#eef1f5",
    },
  },
};

const STORE = "coordharness.accent";

function applyAccent(name) {
  const accent = ACCENTS[name] || ACCENTS.blue;
  const root = document.documentElement;
  root.style.setProperty("--green", accent.hue);
  root.style.setProperty("--accent-glow", accent.glow);
  Object.entries(accent.tokens).forEach(([token, value]) =>
    root.style.setProperty(token, value));
  root.dataset.accent = name;
  document.querySelectorAll("[data-accent-option]").forEach(button => {
    const active = button.dataset.accentOption === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function mountAccentSwitch() {
  const nav = document.querySelector("nav");
  if (!nav || nav.querySelector(".accent")) return;
  const wrap = document.createElement("div");
  wrap.className = "accent";
  wrap.innerHTML = `<span class="meta">Accent</span>${
    Object.entries(ACCENTS).map(([key, a]) =>
      `<button data-accent-option="${key}" title="${a.label} accent"><i></i>${a.label}</button>`
    ).join("")}`;
  // Both dots resolve through the active token. Rendering an inactive hue here
  // would make the computed-style invariant false before page content began.
  wrap.addEventListener("click", event => {
    const choice = event.target.closest("[data-accent-option]");
    if (!choice) return;
    const name = choice.dataset.accentOption;
    try { localStorage.setItem(STORE, name); } catch { /* private mode: session only */ }
    applyAccent(name);
  });
  nav.appendChild(wrap);
}

// Applied before first paint where possible, so the page does not flash the
// stylesheet default and then correct itself.
let stored = null;
try { stored = localStorage.getItem(STORE); } catch { /* ignore */ }
applyAccent(stored && ACCENTS[stored] ? stored : "blue");
document.addEventListener("DOMContentLoaded", () => {
  mountAccentSwitch();
  applyAccent(document.documentElement.dataset.accent);
});
