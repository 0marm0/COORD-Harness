# Usage refresh durability — 2026-10-22

## Repair

Source invalidations carry generations. A provider or scan callback racing a payload build cannot be erased by the obsolete build. One coalesced follow-up consumes the new source without waiting for another user request; continuously changing sources remain explicitly invalid after the bounded follow-up.

Normal cache refresh starts halfway through its TTL. A healthy snapshot has a bounded valid age (default 30-second TTL plus 30-second grace), with original observation time and an accurate expiry. Source invalidation, explicit force, and actual failure bypass grace. Long builds do not reset observation age on completion. Failures retain prior valid values, report failure, and use bounded retry backoff.

Every self-computed snapshot now checks unique daily dates, daily-to-lifetime token conservation, daily-to-headline cost conservation and noncharged pending attribution before publication. An inconsistent replacement cannot overwrite the last valid snapshot. Provider-specific delayed-receipt warnings retain their actual observation time and reason.

## Validation

428 usage tests passed, including race, refresh timing, failure, accounting guard and previous pricing/retention regressions. Installed runtime modules were deployed with before/after hashes, reversible backups and updated wheel RECORD entries. Repeated live checks ended with all three companion/local routes fresh, Codex provider observations fresh, accounting/strict public contracts valid and both providers decoded by production Swift models.

Existing installed companion cost strips display the retained totals. Native source/binaries were unchanged because this repair changes cache publication and validation only. No geometry or chart renderer changes are claimed.

## Limits

Cold services may report warming while the first build runs. Genuine provider outages, missing attribution or unsupported rates remain explicit. Costs remain API-equivalent estimates and are not raised without receipt or rate evidence. Retained normalized counts and account-bound receipts survive restart; this change adds no credential or account snapshot cache. No commit or public push was authorized.
