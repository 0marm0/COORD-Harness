# COORD provider-usage companion parity

`USAGE_INTERFACE_PARITY_CONTRACT=v1`

The provider-usage experience in the private companion app and the standalone public COORD harness is
one compatibility surface implemented in two repositories. A change is incomplete
until both implementations satisfy the same observable contract, or the receipt
records a concrete not-applicable reason.

## Required observable contract

- Each Claude and Codex card shows current-day running cost, retained cost, token
  total, quota windows, and daily cost history without relabeling tokens as cost.
- The chart shows the peak amount once. Hovering any cost-bearing day reads model
  detail owned by that plotted point; it must not rejoin through a second snapshot
  or date-only lookup. Missing attribution is labeled `Unknown model`, not dropped.
- The full Codex card, chart, and date axis are visible initially on a display that
  has room. Smaller displays keep a working scroll fallback.
- Attached and detached usage windows use the same content geometry. Resizing only
  a glass/background view while leaving the host window fixed is a failure.
- Public fixtures are synthetic or privacy-redacted. No litigation-product names,
  paths, board rows, prompts, or private data may enter COORD.

## Acceptance gate

1. Exercise the same cost-bearing fixture in both repositories. It must include
   Claude and Codex, today's row, at least two models on one day, an unattributed
   model row, and more history than the visible chart width.
2. Decode the fixture through the production payload boundary and assert plotted
   points retain their model breakdown and today's cost.
3. Verify attached and detached geometry at the real 460-point content width and
   at a screen-capped height. Source-token tests alone are insufficient.
4. Build, install, and relaunch both native apps. On the installed binaries, open
   Provider usage, confirm the complete Codex card/date axis is reachable, and
   hover a known multi-model day.
5. Run the focused tests in both repositories and COORD's privacy/publication
   checks. Record commands, binary timestamps, payload evidence, and screenshots
   in the same coord-native work receipt.

If either repository, installed app, or live endpoint cannot be exercised, park or
block the parity row as partial. Do not close it on one repository's unit tests.
