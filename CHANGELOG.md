# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
for human-readable release notes and [Semantic Versioning](https://semver.org/).
The source repository is public. Publication is not distribution: a tagged
version, a package-index upload, a signed binary, or a store submission is a
separate deliberate event, and none of them is claimed here. Entries below
describe what the public source contains, not a hosted or packaged product.

## Unreleased

### Changed

- **Behavior change — completion custody now covers every artifact type.**
  `done_signal_custodied()` previously required a declared proof to be in
  git's index only when its suffix was `.md`; every other suffix completed on
  existence alone, so the headline promise held for one file extension. It now
  holds for all of them. A `.json`, `.txt`, `.html`, `.csv` or extensionless
  proof that exists but was never `git add`ed is **refused** where it
  previously succeeded, and the refusal names the artifact, the `git add`
  command, and the way out. Exempt are the kinds that structurally cannot live
  in a git index — `.parquet`, `.duckdb`, `.db`, `.joblib`, `.bz2`, `.backup`
  — which must still exist; the exemption is about custody, not about proof. A
  directory proof is custodied by the tracked files inside it. Set
  `COORD_COMPLETION_CUSTODY_EXEMPT` to a comma-separated suffix list to rebind
  the exemption for an unusual artifact kind, or to `*` to turn the custody
  requirement off entirely. `path.duckdb::table` proofs are unaffected: they
  name rows, not a file to stage, and are still answered by reading the table.

## 0.1.0 - 2026-08-31

First public source release, matching `version = "0.1.0"` in `pyproject.toml`.
Interfaces marked Preview in [`docs/feature-status.json`](docs/feature-status.json)
may still change; the shipped core surfaces follow the promises in
[`docs/compatibility.md`](docs/compatibility.md).

### Fixed

- Pinned the reviewed Ruff release and aligned local and hosted lint scope so
  dependency drift cannot create a CI-only rule-set change.
- Upgraded immutable GitHub Actions pins to the stable Node 24 releases,
  removing the hosted Node 20 deprecation warning.
- `release_claim` now validates claim status/expiry the same way `heartbeat`
  and `complete` do, and its paused/blocked branch refuses to revert a
  terminal work state.
- MCP client configs exec a committed launch shim instead of the gitignored
  venv directly: a present venv launches normally, an absent one prints one
  actionable line instead of a silent `ENOENT`. `doctor` checks the shim's
  exec bit, not just its existence.
- `ps lstart` parses under `LC_ALL=C` and logs on failure instead of silently
  disabling PID-reuse protection; the cockpit sidecar index now cross-checks
  `pid_started_at` like every other liveness path; a caller-supplied
  `proc_pattern` gets a length cap and a catastrophic-backtracking-shape
  rejection; `uninstall.sh` removes the optional reaper LaunchAgent it can
  install.

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
- OS-agnostic `setup.sh`: the CLI/MCP lane runs on every OS; `--native`
  (LaunchAgent, `~/Applications`) and `--register-clients` (global MCP
  config) are opt-in, so a stranger's first command has no side effects
  beyond the clone. `--dry-run`/`--check` preview and verify, and every run
  ends with a receipt block; `setup-macos.sh` is now a forwarding shim.
- `host_id` (migration 004) stamps runs and sessions, so a PID probe against
  a foreign-host row answers "unknown" rather than "dead", on both
  `board_rows` and the single-row `work_context` path.
- The lane vocabulary (`claude`, `codex`) is now configuration via
  `COORD_LANES` instead of ~55 literal membership sites reading one default.
- Friendly `EADDRINUSE` and zero-table-database errors, exiting with a named
  remedy instead of a raw traceback, and a read-only, loopback-guarded
  `/metrics` endpoint exposing the quantities `doctor` already computes as
  Prometheus text.
- A record-only lint that detects an agent ending its turn immediately after
  spawning untracked child work (classification only, no auto-nudge).
- Three worked example recipes, each showing a refusal working rather than a
  happy path (write-set collisions surfaced by name, `done` refused until
  the declared artifact is in the index, a child attempt refused without a
  held claim), plus `comparison.md` stating what the guard set is not and
  when to reach for something else.

### Changed

- Claim readiness now lives in one core helper: MCP refuses an incomplete
  row with the missing-field list, the CLI warns, and `COORD_CLAIM_STRICT`
  (`0`/`1`) flips either surface's strictness.
- `board_rows` walks bounded keyset chunks under a status filter instead of
  the prior per-row 1+3N lookups (5.6x faster filtered on a 3,000-row board,
  identical results at every chunk seam).
- README now leads with Install, the agent entry point, troubleshooting, and
  platform support. `roadmap.md` is the one roadmap; `next-steps.md` is
  retitled as the release checklist. The doc validator now resolves prose
  section citations, which is how five dead ones were found.

### Security

- Publication inventory, privacy-pattern, strict PNG, fidelity, and reachable
  history checks.
- Exact work/session/claim/fence validation held transactionally through local
  process creation, preventing concurrent claim revocation from racing launch.
- Loopback-by-default projection, explicit non-loopback opt-in, host/origin
  checks, query-only SQLite reads, and no lifecycle mutation endpoint.

### Boundaries

- Generic fresh-project MCP orientation works: 37 tools are declared, 36 register
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
