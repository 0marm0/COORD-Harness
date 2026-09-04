# Authored COORD group labels and shared-worktree containment

**Work:** `N0902-CDX-COORD-GROUP-LABEL-IDENTITY-R1` (T1)
**Repository:** `COORD-Harness`
**Starting commit:** `6d0a14ae1ae72f99fc662527778eef4af4997e00`

## Outcome

PASS for the source tree. Session grouping now prefers a registered root
session with a descriptive authored label over a raw UUID alias, so a correct
group no longer captions itself with the lexicographically smaller opaque id.
A linked worktree is now a weak, uniqueness-checked display bridge: two root
chats sharing it stay separate. Lifecycle claim-family resolution is stricter
and never treats a worktree-only match as ownership authority.

## Semantics changed

- Strong grouping identities remain session id and external thread id.
- Parent-session edges still roll subagents into the root orchestrator.
- A worktree may attach an otherwise unregistered display alias only when all
  registered sessions carrying that worktree already resolve to one strong
  family. An ambiguous worktree is ignored for grouping.
- Canonical root selection ranks descriptive labels first, non-opaque ids
  second, and lexical identity only as the deterministic final tiebreak.
- Claim renewal and mutation families use session id/suffix or exact external
  thread id; `worktree_id` is excluded.

## Regression evidence

Pre-change baseline: `29 passed` in
`tests/test_native_cockpit_session_grouping.py`.

Red tests before the fix:

1. Authored `Coord session contracts` lost to raw UUID
   `8960a254-6cd7-442d-91ad-0cb2050f0f3c`.
2. `codex:one` and `codex:two` sharing `wt-7` collapsed to one group.
3. `claude:two` sharing a worktree with `claude:one` entered the first chat's
   claim family and could resolve its held claim.

Post-fix gates:

- `python -m pytest tests/test_native_cockpit_session_grouping.py -q` ->
  **32 passed**.
- Focused plus adjacent board/authority suites -> **103 passed**.
- Ruff over all changed Python/test files -> **PASS**.
- `git diff --check` -> **PASS**.
- Bounded all-suite shards -> **1,942 passed, 27 skipped**. Two PTY-device
  tests were deliberately deselected after each independently terminated this
  non-interactive command transport with signal exit 129; their remaining
  files passed **16/16** and **29/29** respectively. The monolithic run had
  already reached 66% with no failures before the same transport boundary.
- Safety doctor path-line regression -> **7 passed**; `coord doctor` ->
  **PASS**, including `doctor.public_paths`.

## Installed application state

The installed COORD application was **not rebuilt, restarted, deployed, or
otherwise mutated** in this work. It therefore does not yet carry these source
changes. Adoption requires the private-integration build tree to absorb this
patch and an separately authorized rebuild/restart. No claim is made about the
currently installed binary.

## Files

- `src/coordharness/coord/native_cockpit.py`
- `src/coordharness/coord/coord_db.py`
- `src/coordharness/coord/ingest.py`
- `src/coordharness/safety/doctor.py`
- `tests/test_native_cockpit_session_grouping.py`
- `tests/safety/test_safety_doctor.py`
- `reports/N0902-CDX-COORD-GROUP-LABEL-IDENTITY-R1.md`

No network publication occurred.
