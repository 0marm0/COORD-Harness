# Historical cost tariff correction — 2026-10-22

## Correction

Historical API-equivalent pricing now selects a documented dated base tariff from each canonical request's UTC time before context/tier/region adjustments. The prior current-card base plus one adjustment equals the dated result, avoiding a duplicate premium. Provider-only UTC-day cost estimation consumes the same dated measured mix.

Supported frozen legacy rows preserve their recorded historical API estimates, including captured adjustments. Recalculation at today's base rates remains a separate comparison. Unknown and unsupported model tariffs remain unpriced; invalid/missing original amounts cannot silently become a current-price fallback under a historical label. Original token/cost records are immutable.

The blanket legacy warning claiming cached input was double-counted is removed: independent original-row and producer inspection did not support it. Component-specific historical authority and assumptions remain explicit. Exact execution models are not inferred from the current composer.

## Validation

455 Core usage/pricing tests and106 companion tests passed. Regression coverage includes UTC cutovers with a different local display day, provider-gap allocation, preserved legacy premiums, unsupported rates and invalid originals. Frozen identical-receipt comparison changes historical cost while preserving tokens, with exact closed-day parity between harnesses. Private source counts and dollar amounts remain outside this public receipt.

Runtime deployment preserves reversible module backups, hashes and wheel RECORD custody. Production native renderers are unchanged. Strict public-contract and accounting validation runs independently of provider freshness; a genuine provider timeout retains its original observation and reason.

## Limits

Dated sources establish the specified price changes. Some earlier cache/write rates, exact model/tier identity and provider-only components remain assumptions. These are API-equivalent estimates, not reconstructed invoices. Future price changes require dated authority and regression fixtures. No public commit or push is authorized by this receipt.

Final runtime: standalone local and mesh routes both returned two providers with the corrected estimate; the installed native decoders accepted both contracts. Cold request-driven warming and actual provider timeouts remain explicit, with their failed acceptance receipts preserved privately. Independent normal-default cache reproduction completed successfully without a suppressed exception.
