# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
for human-readable release notes. The package remains a private preview; no
public release or compatibility promise is implied by the entries below.

## Unreleased

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

- Generic fresh-project MCP orientation remains fail-closed pending an accepted
  generic-versus-strict deployment-profile contract.
- Local model support is Preview because real inference is not part of the
  release receipt.
- Distributed coordination, hosted service, authentication, tenant isolation,
  app-store distribution, and a freeform authoritative whiteboard are not
  shipped.
- Public visibility remains gated on publication rights, provenance review,
  clean history, and explicit maintainer approval.
