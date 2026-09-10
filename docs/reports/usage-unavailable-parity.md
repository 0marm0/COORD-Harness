# Usage unavailable-state parity

Work item: COORD-CDX-USAGE-UNAVAILABLE-PARITY

## Result

PASS. The COORD usage proxy now accepts the upstream LITAN unavailable live-observation state instead of rejecting the entire provider map.

## Root cause

LITAN legitimately emits live_observation_state unavailable when a provider observation cannot be established. COORD's strict state allowlist omitted that value, so validation failed closed and returned an empty provider map. That made both Claude and Codex usage disappear even when the rest of the upstream document was valid.

## Change

- Added unavailable to the bounded observation-state vocabulary.
- Added a regression test while retaining the unknown-state fail-closed test.
- Upgraded only the installed COORD Python package and restarted the board service. The full installer was not used because it would regenerate the LaunchAgent environment and discard the configured LITAN upstream URL.

## Verification

- Focused proxy tests: 39 passed.
- Before the fix, the COORD endpoint returned coord_proxy_invalid_contract with an empty provider map.
- After deployment, the COORD endpoint returned both claude and codex, with two quota groups each, from snapshot 2026-09-09T01:04:41.554153Z.
