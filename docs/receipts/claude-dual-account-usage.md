# Claude dual-account usage UI R1

**Date:** 2026-09-18
**Scope:** bounded dual-account provider-usage presentation
**Verdict:** **PASS** for the standalone COORD implementation using bounded,
synthetic fixtures and a generic loopback upstream contract.

## Delivered

- The strict usage proxy accepts a bounded, redacted Claude `account_profiles`
  projection while continuing to accept legacy one-account payloads.
- Native full-card and compact menu-bar surfaces render one labeled row/card per
  Claude profile with real session, weekly, and named-quota bars. Codex remains
  visible alongside those rows.
- The browser full card and compact menu strip implement the same profile
  behavior. A profile with no quota observation is shown as `Sign-in needed` and
  never inherits another profile's bars.
- Shared cost, token history, retained cost, breakdowns, and active-session data
  remain provider-level and render once; they are not falsely attributed to or
  duplicated across accounts.
- Public sanitization strips unknown nested fields and does not admit emails,
  credentials, cookies, raw provider identifiers, configuration paths, nested
  history, or nested cost data.
- `apps/install.sh` now accepts a generic loopback usage URL and expected schema,
  validates both, and persists them in the board LaunchAgent. This keeps the
  multi-account feed connected across relaunches and reinstalls without adding a
  private-product dependency to public COORD.
- The existing Battery settings control remains a persistent opt-in for the
  independent battery status item; disabling it removes only that item, while
  enabling it recreates the same item idempotently.

## Runtime-contract proof

The installer persists an explicitly configured loopback usage URL and expected
upstream schema across board relaunches. The public board proxy then emits the
standalone `coordharness.usage-intelligence.v1` schema. Synthetic fixtures cover
two independent Claude profiles:

| profile | authentication | quota result |
| --- | --- | --- |
| Account A | authenticated | independent session, weekly, and named-quota windows |
| Account B | sign-in required | visible unavailable row; zero borrowed windows/groups |

A cold read may be empty during bounded upstream warm-up; a later read preserves
both profiles. COORD retains only safe labels and quota windows admitted by its
public allowlist.

## Verification

- `bash -n apps/install.sh`: **PASS**.
- Browser/proxy/installer affected suite: **54 passed**.
- Native macOS Xcode suite: **158 passed**.
- Battery preference round-trip and status-item lifecycle tests: **PASS**.
- COORD `git diff --check`: **PASS**.
- Proxy fixtures: **PASS** for two rows, independent quota state, and no
  provider-level cost/history duplication.
- The absent-snapshot regression ensures an unauthenticated second profile
  remains visible rather than being discarded or borrowing another profile's
  quota.
