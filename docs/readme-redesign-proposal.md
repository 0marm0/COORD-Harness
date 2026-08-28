# README front-door redesign proposal

This is the review copy for reorganizing the public repository landing page. It
does not treat the README as a catalogue of every screen. The front door should
answer five questions, in this order:

1. What is this system?
2. What can I see and do with it?
3. What is authoritative, and what is only a projection or recall aid?
4. Can I run it in five minutes?
5. Where do I go for the detailed protocol, application, or memory docs?

## Baseline assessment of the previous page

The current README contains strong material, but the sequence makes the reader
assemble the product themselves.

- The first real product screenshot arrives after the architecture hero, long
  introduction, capability table, refusal list, and native-app section. A new
  reader sees contracts before seeing the product.
- The Apps section precedes the system surfaces, so macOS, menu bar, iOS, web
  board, map, and Atlas read like separate products rather than projections of
  one authority.
- Board, Operations Atlas, coordination map, map lenses, and live-layer lenses
  are explained serially. Their distinct questions are valid, but the repeated
  screenshots make the README feel like a gallery instead of an argument.
- Job execution, lifecycle coordination, graph context, retrieval, and accepted
  memory are all present, but the reader is not first given the boundary that
  separates them.
- Three context diagrams appear together. They are accurate, but only the
  authority/retrieval plane diagram is needed in the landing page; the transcript
  problem and tier-expansion diagrams belong in the memory guide.
- The page explains dependency structure in several places. The strongest
  dependency-depth visual should remain; weaker duplicate dependency images
  should move to detailed docs.
- Native-client screenshots are useful proof that the contract travels, but
  they should follow the core system and quick start, not interrupt the first
  explanation of it.

## Recommended information architecture

### 1. Hero: show the operator cockpit, then the fleet

Use the full-width macOS Cockpit capture immediately below the badges so the first
product image is the bounded work surface an operator scans. Place the Swarm Mesh
capture near the top, immediately after the opening boundary. Mesh should show the
clustered topology, coherent-read rail, and typed relationship receipts, and should
link to `docs/swarm-mesh.md`.

### 2. One system, three responsibilities

Use one compact table to establish the boundaries that the rest of the page
depends on:

| Responsibility | Authority | What it carries |
|---|---|---|
| Coordinate work | `coord.db` through typed lifecycle operations | work, claims, leases, handoffs, reviews, events, artifacts, proof-gated completion |
| Execute long jobs | tracked processes and compact job telemetry | pid evidence, progress, ETA, resource locks, terminal receipts |
| Carry context forward | bounded context, source-bound retrieval, and accepted memory | small boot capsules, cited facts, proposals, immutable accepted generations |

The paragraph under this table should say plainly: coordination can change
lifecycle state; jobs report execution evidence; context and memory help a new
agent recall but never acquire writer authority.

### 3. The proof gate

Move `birdseye.svg` here. It is the best second visual because the reader has
already seen the product and now needs to understand why it is trustworthy.
Keep its existing claim → heartbeat → proof-gated done caption.

### 4. Five-minute start

Put the smallest runnable path before the feature catalogue:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[mcp]'
python -m coordharness.demo
python -m coordharness board --db .coordharness/demo/coord.db
```

Then name the destinations in one line: `/mesh` for the spatial control room,
`/` for the compact board, `/map` for analytical lenses, and `/ops` for the
joined operations document.

### 5. Choose the surface by question

Replace the repeated “Board / Atlas / Map” introductions with one selection
table:

| Question | Surface | Why |
|---|---|---|
| What is running or needs attention? | Board | compact row state and job telemetry |
| How is the fleet connected right now? | Swarm Mesh | clustered spatial graph, typed motion, traversal, and telemetry |
| What does the estate look like through a specific analytical lens? | Coordination Map | dependencies, shape, subjects, order, crossings, context, and scheduling ceiling |
| What is structurally actionable in this coherent generation? | Operations Atlas | joined graph envelope, operational metrics, health, and bounded questions |
| What can I glance at away from the browser? | macOS, menu bar, iOS | read-only native projections of the same contract |

Show no more than three visuals in this section: the Swarm Mesh opening image already
seen, one dependency-depth or selected-traversal capture, and one compact native
client pair.

### 6. Protocol and capability sections

Retain the existing “What it refuses to do” section, but place it after the
quick start and surface selection. It works better as the safety contract once
the reader knows what the product does.

Then organize detailed capabilities under three headings that mirror the
three-responsibility table:

- Coordinate: claim, renew, hand off, review, complete with proof.
- Execute: launch, supervise, account for resources, and close tracked jobs.
- Remember: boot from bounded context, retrieve with receipts, propose memory,
  and accept immutable generations.

### 7. Clients, MCP, and extension points

Move the macOS cockpit, menu bar, and iOS screenshots here. Keep one desktop
capture and the existing two-column menu-bar/iOS pair. Explain once that all are
read-only projections; do not repeat that boundary under each image.

MCP belongs beside CLI and Python as another client of the same lifecycle
authority—not as a separate product. Use the existing single-authority diagram
in the detailed MCP guide and link to it from a short README paragraph.

### 8. Memory and graph engineering

Keep `context-retrieval.svg` in the README because it states the authority
boundary. Move `context.svg` and `context-tiers.svg` to the memory guide and
link to that guide from the three-responsibility section.

The Swarm Mesh is structural context over the published coordination graph. It
must not be described as the fact ledger, vector memory, or accepted-memory
store. The graph can help navigate evidence and prerequisites; it cannot turn a
proximity or cluster into a remembered fact.

## Exact proposed opening copy

The first screenful should read approximately as follows:

> **COORD-Harness**
>
> A local-first control plane for fleets of Claude, Codex, MCP clients, shell
> agents, and local model jobs. They claim work without colliding, hand off
> bounded context, supervise long processes, and finish only when the declared
> proof exists.

After the badges and Cockpit hero, with Swarm Mesh immediately following the opening boundary:

> **One authority. Many workers. Inspectable proof.**
>
> `coord.db` owns lifecycle state. Agents write through the same fenced
> operations; long jobs publish process evidence; web and native clients render
> read-only projections; retrieval and accepted memory help the next session
> recall without gaining authority to rewrite the board.

Swarm Mesh caption:

> **Swarm Mesh** is the graph-first control room. The same bounded topology can
> be grouped by recorded actor lane, module, or measured prerequisite depth. Bright
> particles explain typed edge direction; historical replay stays at the
> recorded node; only a newly observed actor-matched arrival may traverse a
> current hold. Motion never means thought, progress, or productivity.

## Visual keep, move, and remove decision

| Asset or group | Decision | Destination |
|---|---|---|
| `macos-cockpit.png` | Keep as the primary hero | README opening |
| `swarm-mesh-context.png` | Keep near the top | README opening |
| `birdseye.svg` | Keep, move below the three-responsibility table | README proof gate |
| `projection-topology.svg` | Move out of the front door; it repeats the authority/projection boundary | architecture or clients guide |
| Board overview | Keep one smaller image | surface selection or board guide |
| Operations Atlas overview and topology | Keep topology only in README; both remain in Atlas docs | detailed docs |
| Map dependency images | Keep the measured depth/ceiling visual; remove the weaker duplicate from README | map guide for the rest |
| Map lens gallery | Move the 2×2 and live-layer galleries out of README | coordination-map guide |
| menu-bar + iOS pair | Keep lower on page | clients section |
| `context-retrieval.svg` | Keep | memory boundary |
| `context.svg`, `context-tiers.svg` | Move | memory guide |

## Product UI convergence recommendations

The web application should converge around one persistent shell rather than
asking the operator to remember which standalone page owns which insight.

1. Make Swarm Mesh the graph-first workspace inside the existing navigation,
   while preserving the compact Board as the default triage view.
2. Treat Board, Mesh, Map, and Atlas as lenses over one coherent read contract.
   A selected row, search query, time/freeze state, and palette should survive
   lens changes where the source document supports them.
3. Put job telemetry and lifecycle state in one Operations rail, but keep
   context/retrieval/memory under a separate Knowledge rail. Combining their
   navigation is useful; combining their authority language is not.
4. Add a global command/search field that ranks exact id, title, current step,
   owner, module, artifact, and accepted-memory citation, while naming which
   provider produced each result.
5. Let every graph selection open the same semantic inspector: state, owner,
   current step, direct typed relations, source fields, done signal, artifacts,
   bounded event occurrences, and omission receipts.
6. Keep saved views explicit: Context, Swarm, Critical Flow, Neighbourhood, and
   Intervention. A saved view stores display controls only; it cannot persist a
   lifecycle mutation.
7. Add density-aware label levels and a minimap only after the 400-node cap is
   measured in real browsers. A minimap that silently drops quarantined or
   omitted nodes would be worse than no minimap.
8. Add an optional presentation mode that enlarges graph labels and collapses
   secondary ledgers for demos; it must display the same source receipts and
   motion-truth badge as the operator mode.

## New visual backlog after this revision

Only add a visual if it answers a question not already answered by the retained
set.

- A source-generated lifecycle diagram that fails CI when the state machine
  changes without regeneration.
- A handoff sequence that includes the real refused-claim error.
- A neighbourhood animation that states “showing N of M; K hidden” before and
  after hop expansion.
- A context-to-memory promotion lineage diagram: source occurrence → proposal →
  review → accepted content hash → compiled boot generation.
- A local-job custody diagram: request → tracked launch → pid/resource evidence
  → compact telemetry → terminal artifact.
- A native-client topology that shows which app reads HTTP, which reads SQLite,
  and where typed control requests stop because the public repository ships no
  mutating control endpoint.

Each new diagram must name whether geometry, order, size, color, and motion are
measured, categorical, or schematic. Each capture must come from the synthetic
demo board and have an exact provenance entry.

## Review checkpoint

The implementation branch now provides the material this redesign needs:

- a live `/mesh` route rather than a static mockup;
- Context, Swarm, and Critical Flow layouts over one bounded source graph;
- orbit, pan, zoom, search, node selection, cluster filtering, keyboard
  navigation, and bounded graph questions;
- a coherent read rail and surrounding event, edge, traversal, health, and
  dependency telemetry;
- strictly separated live-arrival, stationary-replay, and schematic-direction
  motion semantics;
- full-width, traversal, actor-lane, critical-flow, and mobile captures generated
  from the fictional demo board.

The isolated review branch now applies the opening hierarchy and move/remove
matrix as a concrete candidate: quick start and surface selection are high;
repeated map galleries and duplicate projection/context diagrams are out; native
screens are lower; the detailed captures remain in `docs/swarm-mesh.md`. Review
the candidate before merging it into the public repository's main branch.
