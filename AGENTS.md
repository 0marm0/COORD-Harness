# CoordHarness agent entrypoint

`COORDHARNESS_AGENT_INSTRUCTION_SENTINEL=v1`

CoordHarness coordinates multi-agent work through one local SQLite authority. `coord.db`
owns work, claims, leases, handoffs, and lifecycle events. Markdown provides stable
orientation only; dashboards, job sidecars, `knowledge.db`, facts, KFTS, and accepted
memory are evidence or recall, never lifecycle state.

## Start and finish

1. Use `$operating-coordharness` (repo skill: `.agents/skills/operating-coordharness/`).
2. Run `.venv/bin/coord onboard --register-clients` once, then MCP `preflight`.
   If the board is empty, use `.venv/bin/coord create` with a concrete
   repository-relative `done_signal`.
3. Expand with bounded `board`, `next_work`, and `work_context` reads. Use the context
   lenses only when the active row needs broader history or source evidence.
4. Claim before substantive work. Renew only near lease expiry or a material milestone.
5. Complete only after the declared proof passes. Otherwise park, block, release, or use
   the typed handoff command with exact row fences.

Do not edit SQLite or lifecycle projections by hand. Preserve unrelated dirty work and
never weaken identity, proof, review, or authority checks to make a transition pass.

## Context hierarchy

- Clean-room installation and verification: `docs/agent-onboarding.md`
- Claims, reviews, events, and handoffs: `docs/agent-protocol.md`
- Bounded board and federated retrieval: `docs/context-architecture.md`
- Facts, knowledge, and memory boundaries: `docs/context-and-memory.md`
- Durable CPU/GPU processes: `docs/jobs-and-runs.md`
- Detailed skill references: `.agents/skills/operating-coordharness/references/`

Read the focused document that answers the current gap; do not load the documentation
set or a full board dump by default. Run focused tests for changed surfaces and report
exact files, commands, uncertainty, and remaining risk. Do not commit or push unless the
user explicitly asks.
