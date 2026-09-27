# Origin main sync, 2026-09-28

Scope: confirm that the public repository contains the current provider usage work and publish the remaining local fix.

## Source state

- The checkout began clean at `6d9067fb274f20516a424e0f61ca56623f25c640`, one commit ahead of `origin/main` after `git fetch origin --prune`.
- The bounded Claude account profile implementation is commit `d410815b50dac9274ebdb828a876356d41a6b103`, already an ancestor of `origin/main` before this sync. Its proof is in `docs/receipts/claude-dual-account-usage.md`.
- Local cost accounting and provider-anchored estimates are in `582e996e3a762f4ec02113a1b571a6fb643f8f7c` and `cd47551a608d71f8973d4f253dadb6079cd8d6f6`, already ancestors of `origin/main` before this sync.
- The unpublished `6d9067f` change increases the bounded provider-account action timeouts in the native client and public board adapter.
- Other active worktrees were clean. A separate older native Stats appearance branch remains outside this usage sync.

## Verification

- `git diff --check origin/main..main`: PASS.
- `.venv/bin/python -m pytest -q tests/usage/test_account_actions.py tests/usage/test_dashboard_proxy.py tests/usage/test_local_foundation.py`: 76 passed.
- `xcodebuild -project CoordCockpit.xcodeproj -scheme CoordCockpitMac -configuration Debug -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO test -quiet` from `apps/`: PASS.
- Publication and extraction tests, documentation validation, and public generalization tests: 119 passed.
- `python tools/extract/repin.py --check`: PASS after declaring 18 previously unlisted tracked usage files and refreshing six stale source pins, plus this receipt.
- `python tools/privacy_hygiene.py` on the current tracked tree: PASS. `python tools/public_hygiene_sweep.py`: PASS for generic patterns; the optional private vocabulary was not configured.
- `python tools/privacy_hygiene.py --history`: FAIL on six historical findings. All six objects were already reachable from the pre-existing `origin/main` before this sync. This push adds no newly identified history finding; the repository's complete-history gate remains red until that separate history issue is resolved.

## Final sync

`git push origin main` published commits through `d804ac4f18008ef2da86c3b6b5cefdd71676c140`. A fresh `git fetch origin main`, `git rev-parse main`, `git rev-parse origin/main`, and `git ls-remote origin refs/heads/main` all returned that same commit. The worktree was clean at this checkpoint.
