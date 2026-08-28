# Lifecycle contract

## One normal session

1. Set a stable actor/session identity for the current process.
2. Orient with the bounded preflight and exact work context.
3. Claim one assignable work item with a concrete current step.
4. Renew only when a long turn approaches lease expiry or reaches a material milestone.
5. Record decision-bearing context as an event or artifact reference, not heartbeat prose.
6. Complete only after the declared done signal validates.
7. Otherwise park, block, or release with a specific next condition.

Use `coord --help` for the installed CLI shape and MCP tool discovery for the
configured server. The two surfaces call the same core; neither should invent a
parallel state file.

## Handoff

A handoff changes responsibility and therefore requires more than a note. Bind
it to the work item version, current assignee, and event head when supported.
Include acceptance, constraints, evidence pointers, and the receiving actor.
If the row drifted, reread and decide again instead of forcing the write.

## Proof and review

A terminal artifact is evidence, not automatic authorization for deployment,
publication, or another lifecycle. Keep author completion, independent review,
and higher-level program state separate. Never mark work done merely because a
file exists at an undeclared path.
