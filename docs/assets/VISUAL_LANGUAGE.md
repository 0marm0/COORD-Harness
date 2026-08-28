# Visual language

This is the drawing contract for every diagram in this repository. It is prescriptive, not
suggestive. If a rule below and your taste disagree, the rule wins — the point is that six
different authors on six different days produce one set, not six sketches.

Scope: every `.svg` under `docs/assets/`. Screen captures under `docs/assets/screens/` are
governed by `provenance.json` and the publication gate instead, not by this document.

---

## 1. What is wrong with the current set

Read before you copy anything from the existing files. These are the specific defects this
contract exists to end.

| Defect | Where it shows | Rule that fixes it |
|---|---|---|
| Two incompatible systems in one directory | `architecture.svg` / `context-tiers.svg` use a token set (`--panel`, `--accent`, `--good/--warn/--bad`); `lifecycle-proof.svg` hardcodes `#f8fafc`, `#0f172a`, `#94a3b8` with no tokens at all | §2 — one token block, pasted verbatim |
| Alert vocabulary standing in for domain vocabulary | `--good` / `--warn` / `--bad` say "healthy / caution / error", which is not what any of these diagrams are about | §3 — five roles, named after what the system actually has |
| Near-black and near-white type | `lifecycle-proof.svg`: `.label{fill:#0f172a}`, dark override `#f8fafc` | §2 — `--ink` never reaches either end |
| Partial dark overrides | `lifecycle-proof.svg` dark block redefines `.done{fill}` but not its stroke; `.guard` gets a dark fill and keeps its light stroke | §2 — dark redefines *only* properties, never rules |
| Full-bleed painted backgrounds | `<rect class="bg" width="1200" height="650" rx="24"/>`, and `fill="var(--panel)"` plates in the others | §2 — background stays transparent |
| No shared type scale | 11 / 13 / 15 px in one file, 14 / 18 px bold in another | §6 — four sizes, 12–14 px, nothing bold except the title |
| Rounded-everything | `rx="45"`, `rx="60"`, `rx="24"` on things that are not states | §4 — corner radius carries meaning |
| Unqualified element ids | `lifecycle-proof.svg` declares `id="title"`, `id="desc"`, `id="a"`, `id="green"` | §9 — every id carries a file slug |
| Diagrams that restate their heading | boxes labelled with the words already in the `<title>` | §8 — every diagram carries numbered notices |

One thing the current set gets right and must keep: `viewBox` only, no `width`/`height`
attributes, so GitHub scales the drawing to the column.

---

## 2. The token block

Paste this into every SVG, unchanged, as the first child after `<defs>`. Do not add tokens.
Do not rename tokens. If a diagram needs a colour this block does not have, the diagram is
wrong, not the block.

```xml
<style>
  :root{
    --ink:#22292f; --muted:#5b666d; --line:#7d8a91; --rule:#d7dee1;
    --surface:#f2f5f6; --bg:transparent;
    --agent:#2C5F8D;     --agent-fill:#e4edf5;
    --authority:#1F6F63; --authority-fill:#dcece8;
    --derived:#6A5A9E;   --derived-fill:#ebe7f4;
    --refused:#A6392B;   --refused-fill:#f6e5e2;
    --proof:#3E7A2E;     --proof-fill:#e4f0df;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --ink:#dfe6ec; --muted:#9aa4ad; --line:#8b959c; --rule:#2b3238;
      --surface:#161c22; --bg:transparent;
      --agent:#79AEE6;     --agent-fill:#152a3c;
      --authority:#4FC3AC; --authority-fill:#0f2c27;
      --derived:#B0A2E8;   --derived-fill:#221f38;
      --refused:#F0887A;   --refused-fill:#33191a;
      --proof:#74BF62;     --proof-fill:#182b14;
    }
  }
  text{ font-family:ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif; }
  .t-title{ font-size:14px; font-weight:600; fill:var(--ink); }
  .t-group{ font-size:12px; font-weight:600; fill:var(--muted); letter-spacing:.06em; }
  .t-node { font-size:13px; font-weight:500; fill:var(--ink); }
  .t-sub  { font-size:12px; fill:var(--muted); }
  .t-edge { font-size:12px; fill:var(--muted); }
  .t-note { font-size:12px; fill:var(--ink); }
  .t-cap  { font-size:12px; fill:var(--muted); }
  .n         { stroke-width:1.5; }
  .n-plain   { fill:var(--surface);        stroke:var(--line); }
  .n-agent   { fill:var(--agent-fill);     stroke:var(--agent); }
  .n-auth    { fill:var(--authority-fill); stroke:var(--authority); }
  .n-derived { fill:var(--derived-fill);   stroke:var(--derived); }
  .n-refused { fill:var(--refused-fill);   stroke:var(--refused); }
  .n-proof   { fill:var(--proof-fill);     stroke:var(--proof); }
  .rim       { fill:none; }  /* cylinder rim: declared after .n-* so it wins on fill */
  .grp       { fill:none; stroke:var(--rule); stroke-width:1; stroke-dasharray:4 4; }
  .e         { fill:none; stroke:var(--line); stroke-width:1.5; }
  .e-derived { fill:none; stroke:var(--line); stroke-width:1.25; stroke-dasharray:5 3; }
  .e-refused { fill:none; stroke:var(--refused); stroke-width:1.5; }
  .bound     { fill:none; stroke:var(--line); stroke-width:1.25; }
  .chip      { fill:var(--surface); stroke:var(--line); stroke-width:1.25; }
</style>
```

**Define the tokens in `<style>`, never in a `style=` attribute on `<svg>`.** An inline style
attribute on the root element outranks any `:root` rule in a stylesheet, so a dark-mode block
written as `@media (prefers-color-scheme: dark){ :root{ … } }` would silently never apply.
Diagrams authored with the tokens in an attribute look correct in light mode and are broken in
dark mode with no visible symptom at author time. This has to be stated because the shorthand
`style="--ink:…"` reads plausible and is not.

Three consequences of these values, all deliberate:

- **`--ink` is `#22292f` / `#dfe6ec`, not black or white.** Pure ends look like a screenshot of
  a different application and vibrate against GitHub's greys.
- **`--bg` is transparent and nothing is ever painted full-bleed.** A painted plate makes the
  drawing a card sitting on the page rather than part of it, and it fights the reader's theme.
- **Known limitation, stated once:** an SVG loaded through `<img>` evaluates
  `prefers-color-scheme` against the browser or OS setting, not against the GitHub theme
  toggle. A reader who runs a light OS and a dark GitHub gets the light token set on a dark
  page. That is why `--line` and `--muted` are mid-tones rather than being pushed to the
  extremes, and why §3 forbids colour-only meaning: in the mismatch case the drawing is
  lower-contrast but still fully readable, because shape and label carry everything.

---

## 3. Semantic palette

Five roles. Colour repeats meaning that shape and label already carry — it is never decoration,
and it is never the only carrier of a distinction.

| Token | Role | Use for | Never use for |
|---|---|---|---|
| `--agent` | **agent / actor** | anything that acts: an agent session of any `runner_type` (`claude_chat`, `codex`, `local_gpu`, `background`, `subagent`, `workflow`), a CLI invocation, an MCP client | anything stored |
| `--authority` | **authority / store of record** | the one SQLite board and only it — the row whose value settles a dispute | any cache, mirror, or copy |
| `--derived` | **derived / read-only** | snapshots, views, projections, board renders, native cockpit reads — anything computed from the authority that cannot write back | anything a write path passes through |
| `--refused` | **refused / blocked** | a policy refusal, a guard that fires, a blocked write, an unmet precondition | a warning, a "to do", an unfinished feature |
| `--proof` | **proof / verified** | a git-committed artifact, a satisfied `done_signal`, a passing test that pins behaviour | an aspiration, a plan, an expected result |

Rules that make this hold:

1. **Neutral by default.** A node with no role is `.n-plain`. Most nodes in most diagrams are
   plain. If more than half the nodes are coloured, the palette has stopped meaning anything.
2. **Exactly one `--authority` element per diagram, at most.** There is one board. Two teal
   cylinders in one drawing is a factual error, not a style error.
3. **Edges are neutral.** Every edge is `--line` except a refused edge, which is `--refused`.
   No other role recolours a line. This keeps marker definitions to three per file.
4. **Text is `--ink` or `--muted`, always.** Never colour a text node with a role colour; role
   colour lives in strokes and fills. Coloured type at 12 px is the first thing to fail on a
   mismatched theme.
5. **Never `--good` / `--warn` / `--bad`.** That vocabulary describes health. These diagrams
   describe structure. A refusal is not an error; it is the system working.

---

## 4. Shape grammar

Shape is the primary carrier. A reader who cannot see colour at all must still get the diagram.

| Shape | Geometry | Means |
|---|---|---|
| **Rounded rectangle** | `rx="6"` | **a process** — something that runs and returns: an agent session, a command, a policy check, a job |
| **Sharp rectangle** | `rx="0"` | **a record** — something stored and re-readable: a table, a row, a file, an artifact, a snapshot |
| **Pill** | `rx = height / 2` | **a lifecycle state** — `queued`, `running`, `done`, `blocked`. Reserved for state machines; never use a pill as a pretty box |
| **Cylinder** | ellipse `ry:12` cap, straight body, rim arc redrawn with `class="n n-auth rim"` | **the authority** — the one durable store. At most one per diagram |
| **Diamond** | 4-point polygon, half-width ≈ 54, half-height ≈ 42 | **a gate** — a point where an edge can branch, and where one branch is a refusal. If nothing can be refused, it is not a diamond |
| **Dashed container** | `rx="8"`, class `.grp`, stroke `--rule` | **a boundary or grouping** — a package, a trust boundary, a band of related nodes. Labelled with `.t-group` at its top-left, inset 16 |

Do not invent hexagons, cloud shapes, cut corners, or icons. Six shapes is the whole vocabulary.
No drop shadows, no gradients, no filters.

---

## 5. Line grammar

| Line | Style | Means |
|---|---|---|
| **Solid, filled arrowhead** | `.e`, `marker-end` filled triangle | a **synchronous call or write** — the caller waits, and the target changes |
| **Dashed, open chevron** | `.e-derived`, `5 3` dash, hollow chevron | a **derived read** — the target is computed from the source and writes nothing back |
| **Solid `--refused`, bar terminator** | `.e-refused`, marker is a perpendicular bar, **not** an arrowhead | a **refused write** — the bar is the point: the edge stops, and the refusal is recorded |
| **Doubled line, no arrowhead** | two `.bound` strokes 4 units apart | an **enforced boundary** — nothing crosses it without passing the checks that sit on it. Draw it as a rule across the whole band it separates, and label it |

Arrowheads: `markerWidth="8" markerHeight="8"`, `refX="9" refY="5"`,
`orient="auto-start-reverse"`. One arrowhead per edge; a double-headed arrow means you have not
decided which way the causality runs.

Edge labels are `.t-edge`, placed 10 units clear of the line, at the midpoint, `text-anchor` set
so the label never crosses another edge. **Label the edge with the verb, not the noun**:
`claim`, `pass`, `complete`, `emit` — not `data` or `flow`. An unlabelled edge is only
acceptable when it is one of a set that shares a single labelled exemplar.

---

## 6. Type scale

Four sizes, all between 12 and 14 px. Nothing else.

| Class | Size / weight | Colour | Used for |
|---|---|---|---|
| `.t-title` | 14 px / 600 | `--ink` | the one diagram title, at `x=32 y=38`. One per file |
| `.t-node` | 13 px / 500 | `--ink` | node labels |
| `.t-group` | 12 px / 600, `letter-spacing:.06em`, upper case | `--muted` | band and container labels |
| `.t-sub` `.t-edge` `.t-note` `.t-cap` | 12 px / 400 | `--muted` (`.t-note` is `--ink`) | sublabels, edge labels, notices, caption |

No other weight than 400, 500, 600. No italics. No letter-spacing except on `.t-group`. Sentence
case everywhere except `.t-group`, which is upper case. Identifiers taken from the source keep
their exact casing and underscores in a sublabel: `runner_type: codex`, `done_signal`,
`creation_lint`.

**Caption convention.** Every diagram ends with one `.t-cap` line at the bottom-left, one
sentence, ending in a period. It states the claim the drawing supports — never a restatement of
the title, never "diagram of X". It is the sentence a reader would quote back.

---

## 7. Canvas, grid, and spacing

Two canvas widths. Pick one; do not invent a third.

| Width | Grid | For |
|---|---|---|
| **900** | 3 columns × 256, gutter 34, margins 32 | a single mechanism: one lifecycle, one guard, one flow |
| **1200** | 4 columns × 260, gutter 32, margins 32 | the bird's-eye view and anything with more than three bands |

Height is free, but a diagram intended for the top of a README stays at or under
0.62 × width. Every coordinate is a multiple of 2; every node origin is a multiple of 4.

Standard node sizes — snap to these:

| Size | Dimensions | For |
|---|---|---|
| S | 150 × 44 | pills, legend swatch boxes |
| M | 200 × 68 | the default node, label plus one sublabel |
| L | 260 × 88 | a node carrying a label and two sublabels |
| Wide | 2 columns (546 at 900, 552 at 1200) | a band that spans, e.g. a policy pipeline |

Minimum gaps, all hard floors:

- 32 horizontal and 28 vertical between nodes;
- 16 between a node and its container's border;
- 10 between an edge label and the edge;
- 56 reserved at the top of the canvas for the title;
- 48 reserved at the bottom for the notice rail and caption.

Bands run left to right in causal order and top to bottom in time order. Never both at once in
one band.

---

## 8. Annotation convention

A diagram that only restates its heading is noise. Every diagram carries **one to three numbered
notices** that say what a reader should notice — a consequence, a constraint, or a thing that is
deliberately absent.

Mechanics:

- A **chip**: `<circle r="10" class="chip"/>` with a `.t-note` numeral centred in it, placed
  touching the element it refers to, never overlapping a label.
- A **notice rail**: `.t-note` lines stacked at the bottom-left, 18 units apart. Set the numeral
  and the sentence as two separate `<text>` elements, at the left margin and margin + 16 — XML
  collapses runs of whitespace, so `"1  Every…"` inside one element loses its gutter.
- Maximum three. If you need four, the diagram is doing two jobs — split it.

Write a notice about the mechanism, not the picture:

- Good: `1 · Every lifecycle write crosses one boundary; there is no path around the checks.`
- Good: `2 · A refusal is a stored row with a reason, not a dropped call.`
- Bad: `1 · Agents write to the database.` — that is the arrow, already drawn.

Counts and identifiers are information density, and they belong in sublabels rather than
notices: `11 tables · 4 views at v1`, `7 checks, in order`, `362 tests`, `57 Swift files`.
Only put a number in a diagram if you can point at the file that establishes it. Never invent a
benchmark, a latency, or an adoption figure.

---

## 9. File contract

Every `.svg` in `docs/assets/`:

1. Root element carries `viewBox`, `xmlns`, `role="img"`, and
   `aria-labelledby="<slug>-title <slug>-desc"`. **No `width` or `height` attributes.**
2. First two children are `<title id="<slug>-title">` and `<desc id="<slug>-desc">`. The title
   is a noun phrase. The desc is two to four sentences that let someone who cannot see the image
   reconstruct the argument, not just the layout.
3. **Every id is prefixed with the file slug** — `lifecycle-arrow`, not `arrow`; `arch-title`,
   not `title`. Ids collide the moment two of these are inlined into one docs page.
4. Exactly three markers, named `<slug>-arrow`, `<slug>-open`, `<slug>-stop`.
5. Hand-authored. No generator output, no embedded raster, no `<image>`, no external
   references of any kind, no web font, no `<script>`.
6. **No vertical-specific domain vocabulary**, in any case, anywhere — labels, ids, `<title>`,
   `<desc>`, comments. This tool coordinates agents doing software work; it is not scoped to a
   legal, medical, financial, or scientific domain, and a diagram that borrows one industry's
   nouns silently narrows what readers believe the tool is for. Every worked example is generic
   software work: a payments service, an API migration, a test suite, a schema change.
7. No emoji, anywhere.
8. Register the file in `provenance.json` and run
   `python .github/scripts/validate_docs.py` from the repository root before committing.

---

## 10. Reference sheet

The file below demonstrates every token, shape, line, type size, and the annotation convention.
Copy it as the starting point for a new diagram, then delete the legend bands.

```svg
<svg viewBox="0 0 900 700" xmlns="http://www.w3.org/2000/svg" role="img"
     aria-labelledby="vl-title vl-desc">
  <title id="vl-title">Visual language reference sheet</title>
  <desc id="vl-desc">A reference drawing for the shared diagram grammar. The upper band shows an
  agent session sending a lifecycle write across an enforced policy boundary into a gate; the
  gate either refuses the write, recording a row with a reason, or passes it to the single
  authority database, from which a read-only board snapshot is derived. The lower band shows the
  state pills queued, running and done, with a git-committed artifact required before done, next
  to legends for the five colour roles, the six shapes, and the four line styles.</desc>

  <defs>
    <marker id="vl-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8"
            orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="var(--line)"/>
    </marker>
    <marker id="vl-open" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8"
            orient="auto-start-reverse">
      <path d="M1,1 L9,5 L1,9" fill="none" stroke="var(--line)" stroke-width="1.6"/>
    </marker>
    <marker id="vl-stop" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="8" markerHeight="8"
            orient="auto">
      <path d="M5,1 V9" stroke="var(--refused)" stroke-width="2"/>
    </marker>
  </defs>

  <style>
    :root{
      --ink:#22292f; --muted:#5b666d; --line:#7d8a91; --rule:#d7dee1;
      --surface:#f2f5f6; --bg:transparent;
      --agent:#2C5F8D;     --agent-fill:#e4edf5;
      --authority:#1F6F63; --authority-fill:#dcece8;
      --derived:#6A5A9E;   --derived-fill:#ebe7f4;
      --refused:#A6392B;   --refused-fill:#f6e5e2;
      --proof:#3E7A2E;     --proof-fill:#e4f0df;
    }
    @media (prefers-color-scheme: dark){
      :root{
        --ink:#dfe6ec; --muted:#9aa4ad; --line:#8b959c; --rule:#2b3238;
        --surface:#161c22; --bg:transparent;
        --agent:#79AEE6;     --agent-fill:#152a3c;
        --authority:#4FC3AC; --authority-fill:#0f2c27;
        --derived:#B0A2E8;   --derived-fill:#221f38;
        --refused:#F0887A;   --refused-fill:#33191a;
        --proof:#74BF62;     --proof-fill:#182b14;
      }
    }
    text{ font-family:ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif; }
    .t-title{ font-size:14px; font-weight:600; fill:var(--ink); }
    .t-group{ font-size:12px; font-weight:600; fill:var(--muted); letter-spacing:.06em; }
    .t-node { font-size:13px; font-weight:500; fill:var(--ink); }
    .t-sub  { font-size:12px; fill:var(--muted); }
    .t-edge { font-size:12px; fill:var(--muted); }
    .t-note { font-size:12px; fill:var(--ink); }
    .t-cap  { font-size:12px; fill:var(--muted); }
    .n         { stroke-width:1.5; }
    .n-plain   { fill:var(--surface);        stroke:var(--line); }
    .n-agent   { fill:var(--agent-fill);     stroke:var(--agent); }
    .n-auth    { fill:var(--authority-fill); stroke:var(--authority); }
    .n-derived { fill:var(--derived-fill);   stroke:var(--derived); }
    .n-refused { fill:var(--refused-fill);   stroke:var(--refused); }
    .n-proof   { fill:var(--proof-fill);     stroke:var(--proof); }
    .rim       { fill:none; }  /* cylinder rim: declared after .n-* so it wins on fill */
    .grp       { fill:none; stroke:var(--rule); stroke-width:1; stroke-dasharray:4 4; }
    .e         { fill:none; stroke:var(--line); stroke-width:1.5; marker-end:url(#vl-arrow); }
    .e-derived { fill:none; stroke:var(--line); stroke-width:1.25; stroke-dasharray:5 3;
                 marker-end:url(#vl-open); }
    .e-refused { fill:none; stroke:var(--refused); stroke-width:1.5; marker-end:url(#vl-stop); }
    .bound     { fill:none; stroke:var(--line); stroke-width:1.25; }
    .chip      { fill:var(--surface); stroke:var(--line); stroke-width:1.25; }
  </style>

  <text class="t-title" x="32" y="38">Visual language — reference sheet</text>

  <!-- BAND A: grammar in use -->
  <rect class="grp" x="20" y="58" width="860" height="224" rx="8"/>
  <text class="t-group" x="36" y="76">GRAMMAR IN USE</text>

  <rect class="n n-agent" x="32" y="92" width="200" height="68" rx="6"/>
  <text class="t-node" x="132" y="120" text-anchor="middle">Agent session</text>
  <text class="t-sub"  x="132" y="140" text-anchor="middle">runner_type: codex</text>

  <path class="bound" d="M270,84 V280"/>
  <path class="bound" d="M274,84 V280"/>
  <text class="t-edge" x="258" y="228" text-anchor="middle"
        transform="rotate(-90 258 228)">policy boundary</text>
  <circle class="chip" cx="272" cy="70" r="10"/>
  <text class="t-note" x="272" y="74" text-anchor="middle">1</text>

  <path class="e" d="M232,126 H300"/>

  <polygon class="n n-plain" points="302,126 356,84 410,126 356,168"/>
  <text class="t-node" x="356" y="122" text-anchor="middle">policy</text>
  <text class="t-sub"  x="356" y="140" text-anchor="middle">7 checks</text>

  <path class="e" d="M410,126 H490"/>
  <text class="t-edge" x="451" y="116" text-anchor="middle">pass</text>

  <path class="n n-auth" d="M492,104 A117,12 0 0 1 726,104 V164 A117,12 0 0 1 492,164 Z"/>
  <path class="n n-auth rim" d="M492,104 A117,12 0 0 0 726,104"/>
  <text class="t-node" x="609" y="132" text-anchor="middle">coord.db</text>
  <text class="t-sub"  x="609" y="152" text-anchor="middle">11 tables · 4 views at v1</text>

  <path class="e-refused" d="M356,168 V210"/>
  <rect class="n n-refused" x="296" y="212" width="164" height="52"/>
  <text class="t-node" x="378" y="236" text-anchor="middle">refused</text>
  <text class="t-sub"  x="378" y="254" text-anchor="middle">recorded as a row</text>
  <circle class="chip" cx="288" cy="206" r="10"/>
  <text class="t-note" x="288" y="210" text-anchor="middle">2</text>

  <path class="e-derived" d="M609,176 V210"/>
  <rect class="n n-derived" x="492" y="212" width="234" height="52"/>
  <text class="t-node" x="609" y="236" text-anchor="middle">board snapshot</text>
  <text class="t-sub"  x="609" y="254" text-anchor="middle">read-only projection</text>

  <!-- BAND B: states, proof, roles -->
  <text class="t-group" x="32" y="310">STATES AND PROOF</text>
  <text class="t-group" x="660" y="310">COLOUR ROLES</text>

  <rect class="n n-plain" x="32" y="330" width="150" height="44" rx="22"/>
  <text class="t-node" x="107" y="357" text-anchor="middle">queued</text>
  <path class="e" d="M182,352 H250"/>
  <text class="t-edge" x="216" y="343" text-anchor="middle">claim</text>

  <rect class="n n-agent" x="254" y="330" width="150" height="44" rx="22"/>
  <text class="t-node" x="329" y="357" text-anchor="middle">running</text>
  <path class="e" d="M404,352 H472"/>
  <text class="t-edge" x="438" y="343" text-anchor="middle">complete</text>

  <rect class="n n-proof" x="476" y="330" width="150" height="44" rx="22"/>
  <text class="t-node" x="551" y="357" text-anchor="middle">done</text>

  <rect class="n n-proof" x="352" y="406" width="224" height="52"/>
  <text class="t-node" x="464" y="430" text-anchor="middle">git-committed artifact</text>
  <text class="t-sub"  x="464" y="448" text-anchor="middle">checked before done</text>
  <path class="e" d="M438,406 V358"/>
  <circle class="chip" cx="342" cy="400" r="10"/>
  <text class="t-note" x="342" y="404" text-anchor="middle">3</text>

  <rect class="n n-agent"   x="660" y="322" width="14" height="14"/>
  <text class="t-sub" x="684" y="333">agent · actor</text>
  <rect class="n n-auth"    x="660" y="346" width="14" height="14"/>
  <text class="t-sub" x="684" y="357">authority · store of record</text>
  <rect class="n n-derived" x="660" y="370" width="14" height="14"/>
  <text class="t-sub" x="684" y="381">derived · read-only</text>
  <rect class="n n-refused" x="660" y="394" width="14" height="14"/>
  <text class="t-sub" x="684" y="405">refused · blocked</text>
  <rect class="n n-proof"   x="660" y="418" width="14" height="14"/>
  <text class="t-sub" x="684" y="429">proof · verified</text>

  <!-- BAND C: shape and line legends -->
  <text class="t-group" x="32" y="496">SHAPES</text>
  <text class="t-group" x="612" y="496">LINES</text>

  <rect class="n n-plain" x="32" y="512" width="92" height="36" rx="6"/>
  <text class="t-node" x="78" y="535" text-anchor="middle">runs</text>
  <text class="t-sub"  x="78" y="564" text-anchor="middle">process</text>

  <rect class="n n-plain" x="136" y="512" width="92" height="36"/>
  <text class="t-node" x="182" y="535" text-anchor="middle">stores</text>
  <text class="t-sub"  x="182" y="564" text-anchor="middle">record</text>

  <rect class="n n-plain" x="240" y="512" width="92" height="36" rx="18"/>
  <text class="t-node" x="286" y="535" text-anchor="middle">state</text>
  <text class="t-sub"  x="286" y="564" text-anchor="middle">lifecycle</text>

  <path class="n n-plain" d="M344,518 A46,6 0 0 1 436,518 V542 A46,6 0 0 1 344,542 Z"/>
  <path class="n n-plain rim" d="M344,518 A46,6 0 0 0 436,518"/>
  <text class="t-node" x="390" y="536" text-anchor="middle">store</text>
  <text class="t-sub"  x="390" y="564" text-anchor="middle">one only</text>

  <polygon class="n n-plain" points="448,530 494,512 540,530 494,548"/>
  <text class="t-node" x="494" y="534" text-anchor="middle">gate</text>
  <text class="t-sub"  x="494" y="564" text-anchor="middle">branches</text>

  <path class="e"         d="M612,516 H698"/>
  <text class="t-sub" x="708" y="520">call, synchronous</text>
  <path class="e-derived" d="M612,538 H698"/>
  <text class="t-sub" x="708" y="542">derived, read-only</text>
  <path class="e-refused" d="M612,560 H698"/>
  <text class="t-sub" x="708" y="564">refused</text>
  <path class="bound" d="M612,580 H698"/>
  <path class="bound" d="M612,584 H698"/>
  <text class="t-sub" x="708" y="586">enforced boundary</text>

  <!-- notice rail and caption -->
  <text class="t-note" x="32" y="620">1</text>
  <text class="t-note" x="48" y="620">Every lifecycle write crosses one boundary; there is no path around the checks.</text>
  <text class="t-note" x="32" y="638">2</text>
  <text class="t-note" x="48" y="638">A refusal is a stored row with a reason, not a dropped call.</text>
  <text class="t-note" x="32" y="656">3</text>
  <text class="t-note" x="48" y="656">Completion needs both a live claim and a committed artifact; each is pinned by a test.</text>
  <text class="t-cap"  x="32" y="684">Colour only repeats a role that shape and label already carry, so no diagram depends on colour alone.</text>
</svg>
```

---

## 11. Checklist before committing a diagram

- [ ] `viewBox` present; no `width` or `height` attribute.
- [ ] Token block pasted verbatim, inside `<style>`, not in a `style=` attribute.
- [ ] Nothing painted full-bleed; background left transparent.
- [ ] `<title>` and `<desc>` present, non-empty, and referenced by `aria-labelledby`.
- [ ] Every id prefixed with the file slug.
- [ ] Type sizes are only 12, 13, 14; weights only 400, 500, 600.
- [ ] At most one `--authority` element.
- [ ] Every coloured node's role matches §3; nothing is coloured for looks.
- [ ] Every edge labelled with a verb, or covered by a labelled exemplar.
- [ ] One to three numbered notices, each saying something the drawing does not already say.
- [ ] One caption sentence at bottom-left, not a restatement of the title.
- [ ] No vertical-specific domain vocabulary (§9.6), no emoji, no invented numbers.
- [ ] Renders correctly in both light and dark; checked by flipping the OS appearance, not by
      reading the CSS.
- [ ] `python .github/scripts/validate_docs.py` passes and `provenance.json` is updated.
