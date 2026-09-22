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
- Public fixtures are synthetic or privacy-redacted. No sibling-product names,
  paths, board rows, prompts, or private data may enter COORD.
- A Claude provider may expose additive bounded `account_profiles`. When present, every profile remains visible with its own session, weekly, and named quota bars; `active` means the selected sign-in target, not the only measured account.
- Account profiles never own or duplicate provider history, cost, breakdowns, or active-session telemetry. Those provider-level facts render once. An unauthenticated profile shows `Sign-in needed` and never inherits another profile’s quota.
- Compact native and browser menu surfaces show distinct profile labels and bars while retaining Codex. Payloads without `account_profiles` preserve the legacy one-Claude-row presentation.
- An installed board configured with an upstream profile feed persists its loopback URL and expected schema in the LaunchAgent so profile rows survive relaunches and reboots.
- COORD accepts profile fields only through its strict public allowlist. Emails, organization/account identifiers, identity hashes, configuration paths, nested history, nested costs, and unknown fields do not cross the proxy.
- Every "today" figure (cost, tokens, quota reconciliation) is for the payload's `calendar.local_date` exactly. A day with no row is never filled from the newest priced day.
- The payload carries a top-level `history_store` block (`state` of `fresh`, `stale`, `building`, or `unavailable`; `last_scan_at`; `age_seconds`; `stale_after_seconds`; `refreshing`; `serving`). A missing today row reads as `$0.00` only when `state` is `fresh`; otherwise the surface says the history is stale or still building instead of printing a zero or another day's figure. The standalone service keeps its own store current with one background incremental scan when the last completed scan is older than 120 seconds, and `history_store` crosses the proxy through the strict allowlist.
- Day basis: the scan store keeps usage in 15-minute UTC slots (schema 5, table `file_slot`) and derives calendar days at serve time -- local days in the payload's `calendar.time_zone` for display and "today", UTC days for provider anchoring. A scan under any timezone writes the identical store and a timezone change needs no rescan. The slot width is exact for every zone in the current tz database (all offsets and DST transitions are quarter-hour multiples). A schema-4 store discards its day-bucketed rows on open (frozen legacy rows are kept) and the next scan re-parses every transcript. Legacy (imported) days are superseded by measured usage on the same UTC day.
- Codex cost is provider-anchored. `costs.api_rate_estimate.amount_nanos` is the headline and equals `measured_amount_nanos + estimated_amount_nanos + legacy_amount_nanos` (every dollar the daily chart draws); `amount_semantics` is `measured_plus_estimated_plus_legacy`. `estimated_amount_nanos` is `null` whenever no estimate was made, never `0` standing in for unknown. `estimate_state` is `applied`, `unavailable` (with a machine-readable `estimate_reason`, e.g. `provider_series_not_yet_fetched`, `codex_app_server_timeout`, `provider_series_expired`, `provider_series_disabled`), or `not_applicable` (Claude: `provider_reports_no_daily_tokens`). `estimate_basis` and `provider_series_observed_at` state what the estimate rests on. Each `history.daily` row carries `measured_api_rate_estimate_nanos`, `estimated_api_rate_estimate_nanos` and `estimated_tokens`; `api_rate_estimate_nanos` is their sum. A local day the provider saw usage on and this machine did not has `provenance: "provider_anchor_estimate"` and `total_tokens: 0`. `history.provider_anchor` carries the reconciliation totals. A surface never shows the estimate as measured: the chart shades it and the hover splits it. Every one of these fields crosses the proxy allowlist and a test asserts it.
- The rate card in force is the vendored card with the user-local override `~/.coordharness/rate_card.json` layered over it (override entries win). The override is fetched from `https://models.dev/api.json` when a model is unpriced for want of a rate, otherwise at most once per 24h, in the background with a bounded timeout; a malformed or suspicious fetch is refused and the last good card stays. `COORD_USAGE_RATE_CARD_REFRESH=0` disables the fetch; `coord usage-rate-card-refresh [--dry-run]` runs it on demand. `pricing_key` / `rate_card_digest` describe the effective card: identical to the vendored card when the override changes nothing, otherwise `<vendored key>.ovr-<12 hex>` over only the rates that differ, with no timestamps hashed. `rate_card_override_models` counts the overriding models. `COORD_USAGE_CODEX_PROVIDER_SERIES=0` disables the Codex provider series fetch (cost then stays measured-only with reason `provider_series_disabled`).
- Both native applications expose a persistent operator setting for the independent battery menu-bar item. Turning it off removes only that separate status item; turning it on recreates it without changing the primary app item or silently resetting after relaunch.

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
6. Round-trip the battery-item preference through the persisted config and verify
   idempotent status-item creation/removal in both native implementations.

If either repository, installed app, or live endpoint cannot be exercised, park or
block the parity row as partial. Do not close it on one repository's unit tests.
