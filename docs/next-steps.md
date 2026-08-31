# Release readiness checklist

This is the maintainer-facing checklist for cutting a release, plus the small,
already-scoped gaps discovered while operating and documenting this
repository. Direction — what the project is moving toward and why — lives in
[`roadmap.md`](roadmap.md); read that first if you're looking for what's
next rather than what's blocking. This file tracks readiness, not intent.

## 1. Release readiness gates

A release candidate is ready only when:

1. the full test and lint suites pass in a clean environment;
2. packaging installs and runs outside the source checkout;
3. browser and applicable native tests pass;
4. documentation, assets, privacy, secret, and reachable-history gates pass;
5. the exact provider commit and advertised refs match the reviewed candidate.

## 2. Known gaps

Small, already-scoped follow-ups surfaced while writing
[`operators-handbook.md`](operators-handbook.md). None of these blocks a
release on its own; each is cited from the handbook by its number below, and
each stays listed until the fix it names actually ships.

### 2a. Two capability-probe endpoints stay unserved by design

`/api/state/compact` and `/api/capability_inventory` 404 on purpose — see
[operators handbook, "what the native clients read"](operators-handbook.md#24-what-the-native-clients-read-and-its-one-stale-edge).
Removing the two dead client probes, once every packaged client has dropped
them, closes this out.

### 2b. Sharing a URL does not carry the accent

The row id travels in the URL fragment (`#<id>`); the accent choice
(Green/Blue) does not. See
[operators handbook, "the accent switch"](operators-handbook.md#4-the-accent-switch).

### 2c. Two accent-literal regressions have no test coverage

An inline color literal and a `var()` fallback whose variable was never
defined both once escaped the accent switch. Both were found by sweeping
computed styles, not by reading the stylesheet, and neither has a regression
test yet. See
[operators handbook, "the accent switch"](operators-handbook.md#4-the-accent-switch).

### 2d. The fresh-board deployment-profile boundary is still open

`coord claim`, `heartbeat-claim`, `done`, `release`, `inbox`, `doctor`, and
the whole web board all depend on the same open deployment-profile boundary.
See
[operators handbook, "recipes: MCP first, CLI as the twin"](operators-handbook.md#5-recipes-mcp-first-cli-as-the-twin).

### 2e. The hardcoded-endpoint guard is built and enforced

`tests/test_no_hardcoded_endpoints.py` is written and green, and
`HarnessEndpoint` is the single resolution point for `COORD_BOARD_URL`. This
item is already closed; it stays listed so the guard doesn't get quietly
dropped later. See
[operators handbook, "environment"](operators-handbook.md#6-environment).

## 3. Where the public direction moved

The public-facing themes that used to live in this file — installation and
first-run confidence, provider usage and routing, system telemetry, context
and memory, coordination surfaces, and reliability and security — now live in
[`roadmap.md`](roadmap.md) under "Continuing themes". That move changes
where the direction is written down, not what's committed to.
