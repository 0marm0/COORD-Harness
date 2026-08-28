# Bounded implementation briefs

These briefs are optional contributor starting points. They describe only the
standalone COORD contracts in this repository. Confirm current behavior with the
tests and documentation before editing; no brief overrides lifecycle safety,
privacy gates, or review requirements.

## Lifecycle reliability

**Objective:** strengthen one lifecycle transition without adding a second
authority.

**Required checks:**

- use the temporary SQLite fixtures;
- preserve claim fencing and declared completion proof;
- demonstrate one accepted path and one fail-closed path;
- run the focused lifecycle tests and the clean-install packaging test.

## Context retrieval

**Objective:** improve bounded context traversal without treating recall as
lifecycle truth.

**Required checks:**

- every response identifies its source store and freshness;
- absence stays distinct from an empty result;
- limits and truncation are explicit;
- read-only tests fingerprint the store before and after every query.

## Tracked local work

**Objective:** improve job telemetry or resource controls while preserving
process and attempt identity.

**Required checks:**

- launch and terminal writes remain atomic;
- a stale writer cannot resurrect a terminal run;
- resource limits cover the child process tree;
- diagnostics never synthesize authoritative work.

## Read-only clients

**Objective:** improve the web, macOS, or iOS viewer without adding a write path.

**Required checks:**

- render the same versioned snapshot contract;
- keep cached and live data visibly distinct;
- preserve usable compact layouts at narrow widths;
- run browser checks and the applicable native build tests.

## Publication safety

**Objective:** strengthen privacy or release verification.

**Required checks:**

- scan tracked candidate bytes and reachable Git objects;
- never print raw private vocabulary;
- use an external vocabulary file for organization-specific terms;
- prove failures with synthetic fixtures;
- keep credentials, local databases, receipts, and host paths outside Git.

For contribution mechanics, see [Contributing](../.github/CONTRIBUTING.md).
