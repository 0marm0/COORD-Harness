# Operations graph architecture

COORD exposes coordination topology through a bounded, read-only graph
contract. The graph is a projection of recorded work, dependencies, evidence,
and runtime observations. It is not a second lifecycle authority.

## Contract

Every graph response must carry:

- source identity and generation;
- full-population, eligible, emitted, and omitted counts;
- typed nodes and typed edges;
- the source field supporting each relationship;
- unknown and missing-target receipts;
- deterministic caps and truncation reasons.

Duplicate identities are quarantined rather than merged silently. Missing
endpoints remain visible as missing; the renderer must not invent them.

## Surfaces

The browser board, Operations Atlas, Swarm Mesh, and Coordination Map consume
the same admitted graph envelope. A surface may choose a layout, focus, or
saved local view, but it may not change the underlying counts or relationship
meaning.

- **Operations Atlas** combines topology, context, timeline, and health.
- **Swarm Mesh** emphasizes spatial exploration with deterministic cameras.
- **Coordination Map** keeps analytical questions as explicit lenses.
- **Fleet** summarizes recorded agent placement and work ownership.
- **Pulse** summarizes recent recorded events without claiming push delivery.

## Safety boundaries

- clients are read-only;
- geometry carries no meaning unless the caption states it;
- line thickness and animation never substitute for recorded counts;
- local saved views are preferences, not canonical state;
- one failed lens becomes unavailable without taking down other surfaces;
- stale cached data is labelled and never presented as live.

## Verification

Graph changes require fixtures for duplicate IDs, missing targets, stale
inputs, cap exhaustion, zero eligible nodes, and partial sources. Browser tests
must exercise narrow and wide layouts. Publication checks must scan all new
code, fixtures, generated assets, and reachable history.

See [Operations Atlas](operations-atlas.md), [Visual atlas](visual-atlas.md),
and [Security and privacy](security-and-privacy.md).
