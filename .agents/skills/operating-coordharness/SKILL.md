---
name: operating-coordharness
description: Operates a local CoordHarness board for multi-agent work, bounded context retrieval, typed handoffs, and tracked CPU or GPU jobs. Use when an agent needs to orient, claim, renew, complete, pause, hand off, retrieve context, or attach a long-running process without creating a second source of truth.
---

# Operating CoordHarness

Use one `coord.db` as lifecycle authority. Treat dashboards, native clients,
sidecars, documentation, and memory as projections or evidence; none may
silently change lifecycle state.

## Start from the smallest truthful context

1. Resolve the coordinated project root and database explicitly. Do not infer a
   database from an unrelated checkout.
2. Run `coord preflight` when available; otherwise inspect `coord board` and the
   exact work item before loading broad project context.
3. Claim an existing assignable item before substantive work. If new work is
   genuinely required, create it with an acceptance condition and concrete
   `done_signal` before claiming it.

Read [references/lifecycle.md](references/lifecycle.md) for the lifecycle,
handoff, review, and completion-proof contract.

## Keep parallel work bounded

The orchestrator owns the durable claim. Give each subagent a non-overlapping
objective and file scope; record its bounded result under the parent instead of
minting misleading peer ownership. Use typed handoff when responsibility moves
between actors, and optimistic concurrency when the row may have changed.

## Separate agent turns from durable processes

Use tracked jobs for long CPU, GPU, build, indexing, or local-model work. A job
publishes compact liveness and progress; its command body, prompts, stdout,
credentials, and source text do not belong in the sidecar or board.

Read [references/jobs.md](references/jobs.md) before launching or terminating a
process, especially when a GPU lock or process-group cleanup is involved.

## Expand context intentionally

Exact board state and source artifacts outrank capsules, search indexes, facts,
and accepted memory. Begin with the bounded capsule, follow exact pointers for
the active item, and expand only the plane that answers the current question.

Read [references/context.md](references/context.md) when using context federation,
facts, graph traversal, memory proposals, or fresh-session continuation.

## Stop honestly

- Complete only after the declared proof validates.
- Park when the next action is known and resumable.
- Block when progress requires authority, credentials, or external state the
  current actor does not control.
- Release or close out every held claim before the session ends.
- Do not retry a fail-closed transition by weakening identity, proof, review, or
  authority checks.

The packaged MCP server is a transport for these same operations, not a second
database. The generic profile accepts a fresh local `coord.db` and labels its read
receipt accordingly. The strict profile retains deployment-specific custody gates;
do not select generic to bypass a strict refusal on deployment-owned state.
