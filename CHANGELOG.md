# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
for human-readable release notes and [Semantic Versioning](https://semver.org/).
The source repository is public. Publication is not distribution: a tagged
version, a package-index upload, a signed binary, or a store submission is a
separate deliberate event, and none of them is claimed here. Entries below
describe what the public source contains, not a hosted or packaged product.

## Unreleased

## 0.1.0 - 2026-08-28

First public source release, matching `version = "0.1.0"` in `pyproject.toml`.
Interfaces marked Preview in [`docs/feature-status.json`](docs/feature-status.json)
may still change; the shipped core surfaces follow the promises in
[`docs/compatibility.md`](docs/compatibility.md).

### Fixed

- Pinned the reviewed Ruff release and aligned local and hosted lint scope so
  dependency drift cannot create a CI-only rule-set change.
- Upgraded immutable GitHub Actions pins to the stable Node 24 releases,
  removing the hosted Node 20 deprecation warning.

### Added

- One SQLite-WAL coordination authority with claims, leases, typed lifecycle
  events, review tiers, and proof-gated completion.
- CLI, Python, and preview MCP surfaces for Claude Code, Codex, generic MCP
  clients, and local automation.
- Bounded context, full-text knowledge, accepted-memory, source-bound graph,
  local usage-ledger, and tracked-job primitives.
- A read-only loopback web board plus clean-room macOS menu-bar/desktop and iOS
  snapshot clients.
- A deterministic fictional board, eleven authored architecture diagrams, five
  browser captures, and two native captures with machine-readable provenance.
- Byte-identical project skills for Codex and Claude, a read-only safety doctor,
  and explicit local MLX model preflight and resource locking.

### Security

- Publication inventory, privacy-pattern, strict PNG, fidelity, and reachable
  history checks.
- Exact work/session/claim/fence validation held transactionally through local
  process creation, preventing concurrent claim revocation from racing launch.
- Loopback-by-default projection, explicit non-loopback opt-in, host/origin
  checks, query-only SQLite reads, and no lifecycle mutation endpoint.

### Boundaries

- Generic fresh-project MCP orientation works: 34 tools are declared, 33 register
  by default, and preflight, board reads, and the lifecycle writers answer against
  a fresh `coord.db`. Two default tools still fail closed there — `facts_lookup`
  until a knowledge store exists, and `orient` until an exact-authority policy is
  enforced — and the strict deployment profile's board-backed reads fail closed by
  design.
- Local model support is Preview because real inference is not part of the
  release receipt.
- Distributed coordination, hosted service, authentication, tenant isolation,
  app-store distribution, and a freeform authoritative whiteboard are not
  shipped.
- Distribution beyond this public source — tagged releases, package-index
  uploads, signed binaries, and store submissions — remains gated on publication
  rights, provenance review, clean history, and explicit maintainer approval.
