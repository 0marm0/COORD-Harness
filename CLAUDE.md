# CoordHarness Claude entrypoint

`COORDHARNESS_AGENT_INSTRUCTION_SENTINEL=v1`

CoordHarness coordinates multi-agent work through one local SQLite authority. `coord.db`
owns work, claims, leases, handoffs, and lifecycle events. Markdown provides stable
orientation only; dashboards, job sidecars, `knowledge.db`, facts, KFTS, and accepted
memory are evidence or recall, never lifecycle state.

## Start and finish

0. If `.venv/bin/coord` is missing, run `./scripts/setup.sh`, then restart this
   session — the checked-in MCP launcher (`scripts/coord-mcp-launch.sh`) fails
   closed with that same instruction instead of a raw ENOENT. Setup is done when
   `.venv/bin/coord doctor` exits `0`, `.venv/bin/coord onboard` reports `PASS`,
   and MCP `preflight` returns `query_core.mode="generic_coord_db"`
   (`docs/agent-onboarding.md` §5, "Use the bounded MCP reads").
1. Use the `operating-coordharness` repo skill at
   `.claude/skills/operating-coordharness/`, and the packaged commands in
   `.claude/commands/`, invoked as `/coord-start`.
2. Run `coord-start`: it fixes one stable session identity, runs
   `.venv/bin/coord onboard` once, then MCP `preflight`.
   If the board is empty, use `.venv/bin/coord create` with a concrete
   repository-relative `done_signal`.
3. Expand with bounded `board`, `next_work`, and `work_context` reads. Use the context
   lenses only when the active row needs broader history or source evidence.
4. Claim before substantive work (`coord-claim`): check the row's `done_signal` and
   acceptance first, then renew only near lease expiry or a material milestone.
5. Close with `coord-close`: complete only after the declared proof passes, otherwise
   park, block, or release with a resume contract. `coord-handoff` moves responsibility
   with exact row fences; `coord-recover` reconciles a row the board is wrong about.

Do not edit SQLite or lifecycle projections by hand. Preserve unrelated dirty work and
never weaken identity, proof, review, or authority checks to make a transition pass.

## Context hierarchy

- Clean-room installation and verification: `docs/agent-onboarding.md`
- Claims, reviews, events, and handoffs: `docs/agent-protocol.md`
- Bounded board and federated retrieval: `docs/context-architecture.md`
- Facts, knowledge, and memory boundaries: `docs/context-and-memory.md`
- Durable CPU/GPU processes: `docs/jobs-and-runs.md`
- Detailed skill references: `.claude/skills/operating-coordharness/references/`
- Session, claim, close, handoff, and recovery commands: `.claude/commands/`

Read the focused document that answers the current gap; do not load the documentation
set or a full board dump by default. Run focused tests for changed surfaces and report
exact files, commands, uncertainty, and remaining risk. Do not commit or push unless the
user explicitly asks.
