# fleet-fan-out

**What it proves:** a dozen subagents fanned out under one orchestrator still
show up as ONE board row, because their attempts are recorded against the
parent's held claim — and recording against a claim nobody holds is refused
with `UnclaimedFleetError`, not written as an orphaned row nobody can find.
This exercises `record_child_attempt`, a real shipped guard in
`work_contracts.py` with no CLI verb or MCP tool yet — reached only by
importing the module directly, which is exactly what this recipe does.

Code path:
[`src/coordharness/coord/work_contracts.py`](../../src/coordharness/coord/work_contracts.py) —
`record_child_attempt` (raises `UnclaimedFleetError` via `_held_claim_row`
when the `claim_id` is unknown or not in `('running','paused','blocked')`),
`record_child_outcome`, and `child_attempts`. As of this writing, `grep -rn
record_child_attempt src/` turns up only its definition in `work_contracts.py`
and its schema registration in `create_schema.py` — no CLI subcommand, no
`mcp_coord_server.py` tool wraps it. This recipe is the first runnable proof
this surface behaves as documented.

## Run it

```bash
/path/to/.venv/bin/python examples/fleet-fan-out/run.py
```

The script creates its own `tempfile.TemporaryDirectory()` — a fresh git repo
and a fresh `coord.db` inside it — and deletes both on exit. It never touches
any database that existed before it ran.

## What happens

1. Create and claim **one** row, `DEMO-CLA-MARKET-SCAN`, as the orchestrator.
2. **GUARD:** call `record_child_attempt` against a claim id that was never
   claimed — `UnclaimedFleetError` is raised, not swallowed.
3. Record three subagent attempts against the claim actually held: two
   explorers and a synthesizer, each with its own model.
4. Record each attempt's outcome — including one `failed` — via
   `record_child_outcome`.
5. Read `child_attempts(work_id=...)` back: three rows, one parent claim.
   Write a summary artifact that cites all three, stage it, and `coord done`
   the one row. `coord board` afterward still shows exactly one row.

## Expected output (captured 2026-08-31, real run against this repository at commit `2477d887`)

```
[2/5] GUARD: recording a child attempt against a claim nobody holds fails
    record_child_attempt(claim_id='clm-never-claimed-0000', ...)
    -> UnclaimedFleetError: unknown claim_id 'clm-never-claimed-0000': a fleet must be recorded under an already-claimed job row — a fleet spawned under no live claim leaves no trace of the work it did, so claim the row before spawning

[3/5] Record three subagent attempts under the HELD claim clm-c0d26aa655fb
    recorded cat_1052fe60bccd4637  label='explorer-pricing'  model='sonnet-4.5'
    recorded cat_c797228cc4b14c99  label='explorer-competitors'  model='sonnet-4.5'
    recorded cat_5554419d62434012  label='synthesizer'  model='haiku-4.5'

    child_attempts(work_id='DEMO-CLA-MARKET-SCAN') -> 3 rows, ONE parent claim:
      explorer-pricing       model=sonnet-4.5 outcome=completed  ref=docs/reports/pricing-notes.md
      explorer-competitors   model=sonnet-4.5 outcome=failed     ref=timed out after 40 turns, no citations found
      synthesizer            model=haiku-4.5  outcome=completed  ref=docs/reports/market-scan-summary.md

    board row count for DEMO-CLA-MARKET-SCAN: 1 (status=done)
```

`clm-…`/`cat_…` ids are random per run; the guard message and the roll-up
shape are not. The full transcript this excerpt was cut from is committed
alongside this README as [`captured_output.txt`](captured_output.txt).

## What this does *not* prove

This is not the "swarm shows as one row" feature end-to-end — that also
covers how the **board and its projections render** rolled-up subagent
activity next to a claim, which is a separate, larger surface. This recipe
proves the narrower, load-bearing fact underneath it: the write path that
would let a fleet's history exist at all refuses to write it anywhere but
under a claim that is actually held.
