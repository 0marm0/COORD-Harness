# two-agents-one-file

**What it proves:** two independent agent sessions can each hold their own valid
claim on their own row, and still be about to step on the same code — COORD
surfaces that collision *by name* (which two rows, which two claims, which two
sessions, which two scopes) at the moment the second claim is taken, not after
a merge conflict or an overwritten file discovers it for you. The check is a
report, not a lock: neither claim is blocked, because plenty of overlapping
declarations are harmless (reading the same file, or two agents converging on
purpose) — `coord conflicts` is what an orchestrator checks before assigning
the second job.

Code path: `declare_write_set` / `write_set_overlaps` in
[`src/coordharness/coord/work_contracts.py`](../../src/coordharness/coord/work_contracts.py),
reached through `coord claim --write-scope` and `coord conflicts` in
[`src/coordharness/coord/cli.py`](../../src/coordharness/coord/cli.py).

## Run it

```bash
/path/to/.venv/bin/python examples/two-agents-one-file/run.py
```

The script creates its own `tempfile.TemporaryDirectory()` — a fresh git repo
and a fresh `coord.db` inside it — and deletes both on exit. It never touches
any database that existed before it ran.

## What happens

1. **Claude** creates and claims `DEMO-CLA-BILLING-RATES`, declaring it will
   write the whole `src/billing/` directory.
2. **Codex** creates and claims a *different* row, `DEMO-CDX-BILLING-RETRY`,
   declaring the narrower `src/billing/retries.py` — which sits inside the
   directory Claude just claimed.
3. Codex's own `claim` response already reports the collision — the second
   claimant is told at the only moment the warning is still cheap to act on.
4. `coord conflicts` (a read-only, non-mutating query) reports the same
   finding independently, naming both work ids, both claim ids, both
   sessions, and both scopes.

## Expected output (captured 2026-08-31, real run against this worktree)

```
[2/4] Codex creates and claims a DIFFERENT row, declaring a file INSIDE that directory
    $ coord claim DEMO-CDX-BILLING-RETRY --write-scope path=src/billing/retries.py   [session=codex:backend]
    -> claim clm-ebd2e6b3993f, write_set=[{'kind': 'path', 'value': 'src/billing/retries.py'}]

[3/4] The SECOND claim's own response names the collision immediately:
    write_set_conflicts = {
  "count": 1,
  "findings": [
    "path scope collision: DEMO-CLA-BILLING-RATES (clm-a00aabffec7d) declares 'src/billing' while DEMO-CDX-BILLING-RETRY (clm-ebd2e6b3993f) declares 'src/billing/retries.py'"
  ]
}

[4/4] `coord conflicts` names both sides of the collision by row, claim, session, and scope:
    $ coord conflicts   [session=claude:frontend]
    -> path scope collision: DEMO-CLA-BILLING-RATES (clm-a00aabffec7d) declares 'src/billing' while DEMO-CDX-BILLING-RETRY (clm-ebd2e6b3993f) declares 'src/billing/retries.py'
```

Claim ids are `clm-<random hex>` and differ on every run; the collision text
around them does not. The full transcript this excerpt was cut from is
committed alongside this README as [`captured_output.txt`](captured_output.txt).

## What this does *not* prove

`conflicts` never blocks a write — it is advisory. Two agents can still edit
the same file after this report fires; the guard this recipe demonstrates is
*visibility*, not enforcement. See `docs/comparison.md` for where COORD does
enforce something (proof-gated completion), and where it deliberately does not.
