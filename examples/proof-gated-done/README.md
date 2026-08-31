# proof-gated-done

**What it proves:** `coord done` is refused, verbatim and by name, for every
state short of "the declared artifact is staged in git's index" — a file that
merely exists on disk is not proof, only a file `git diff` can see is. The
same claim is then completed by doing nothing more than `git add`ing the file
(no commit required) and re-running the identical `done` command.

Code path: the completion gate lives in `complete_claim` /
`done_signal_satisfied` in
[`src/coordharness/coord/coord_db.py`](../../src/coordharness/coord/coord_db.py),
which calls `done_signal_custodied` / `completion_proof_is_tracked` in
[`src/coordharness/jobs/status.py`](../../src/coordharness/jobs/status.py) —
the function that actually runs `git ls-files` against the repo the artifact
claims to live in. Reached through `coord done --artifact` in
[`src/coordharness/coord/cli.py`](../../src/coordharness/coord/cli.py).
(No line numbers quoted: they go stale. `grep -n def complete_claim
src/coordharness/coord/coord_db.py` finds the current one.)

## Run it

```bash
/path/to/.venv/bin/python examples/proof-gated-done/run.py
```

The script creates its own `tempfile.TemporaryDirectory()` — a fresh git repo
and a fresh `coord.db` inside it — and deletes both on exit. It never touches
any database that existed before it ran.

## What happens

1. Create a row declaring `done_signal=docs/reports/invoice-export.md`.
2. Claim it.
3. Attempt `coord done` before the file exists — refused: **does not exist**.
4. Write the file to disk, but do **not** `git add` it. Attempt `coord done`
   again — refused: **not carried by git's index**, with the exact `git add`
   command to run next.
5. `git add` the file (staged only — not committed) and retry the identical
   `coord done` command — succeeds.

## Expected output (captured 2026-08-31, real run against this repository at commit `2477d887`)

```
[3/5] Attempt `coord done` before the artifact file exists at all
    $ coord done DEMO-CLA-INVOICE-EXPORT --artifact docs/reports/invoice-export.md
    -> refused: coord: complete_claim artifact proof does not exist for claim 'clm-203e2d339d67': docs/reports/invoice-export.md

[4/5] Write the artifact to disk, but do NOT `git add` it, then attempt `coord done` again
    $ coord done DEMO-CLA-INVOICE-EXPORT --artifact docs/reports/invoice-export.md
    -> refused: coord: complete_claim artifact proof exists but is not carried by git's index for claim 'clm-203e2d339d67': docs/reports/invoice-export.md. The custody gate requires the proof to be staged -- run `git add docs/reports/invoice-export.md` and retry. Staging is enough; it does not need to be committed.

[5/5] `git add` the artifact (staged, not committed) and retry `coord done`
    $ coord done DEMO-CLA-INVOICE-EXPORT --artifact docs/reports/invoice-export.md
    -> {"ok": true, "work_id": "DEMO-CLA-INVOICE-EXPORT", "artifact_path": "docs/reports/invoice-export.md", "canonical_event_id": 1, ...}
```

The claim id (`clm-…`) is random per run; the refusal wording is not. The full
transcript this excerpt was cut from is committed alongside this README as
[`captured_output.txt`](captured_output.txt).

## What this does *not* prove

Staging is enough — the artifact does not need to be **committed**, only
`git add`ed. That is a deliberate choice (an agent's working tree is dirty
mid-task far more often than it is clean), not an oversight; do not read this
recipe as "COORD verifies a commit exists." It verifies the proof is in the
index, which is the boundary a reviewer can actually `git diff` against.
