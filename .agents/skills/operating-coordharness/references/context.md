# Context and memory planes

Use the narrowest plane that can answer the question:

1. Exact plane: current work, claims, events, runs, and source artifacts.
2. Bounded context: boot capsule, focused work lens, direct dependencies, and handoff pointers.
3. Retrieval: full-text facts, indexed documents, and source-bound graph traversal.
4. Recall: accepted memory and supersession history.

Only the exact plane is lifecycle authority. Retrieval and recall must preserve
source pointers, generation, and uncertainty; a useful match is not permission
to write state.

For a fresh session, carry the active work ID, acceptance condition, current
claim disposition, recent decisions, exact evidence pointers, and known
dead-ends. Do not replay full boards, raw logs, or an entire prior conversation
when those bounded fields are sufficient.
