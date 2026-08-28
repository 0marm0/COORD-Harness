# Standalone module coverage

This ledger answers a harder question than “how many files were copied?” It
compares reusable orchestration behavior in the source product with the
standalone repository, records deliberate exclusions, and prevents a polished
README from being mistaken for parity.

The audit uses a pinned private-source snapshot and a pinned standalone
pre-integration checkpoint. Their exact identities stay in the non-published
working manifest so public documentation does not disclose private repository
metadata.

## Classification

| Class | Meaning |
|---|---|
| Present | Reusable invariant and usable contract exist here |
| Partial | Useful implementation exists, but an authority, safety, or UX boundary remains |
| Reimplement | The source idea is reusable but its implementation is product-coupled |
| Excluded | Legacy compatibility or private product behavior should not be published |
| Planned | Accepted standalone direction without a complete implementation |

## Coverage matrix

| Family | Standalone state | Evidence and boundary |
|---|---|---|
| SQLite lifecycle, schema, migrations, claims, leases, proof, events | **Present** | `src/coordharness/coord/coord_db.py`, schema, migrations, policy, and tests |
| CLI, Python, and MCP convergence | **Partial** | One core is present; MCP declares 34 tools and exposes 33 in the default profile, while the public CLI intentionally exposes a smaller operator set |
| Bounded board context, facts, full-text retrieval, accepted memory | **Present, derived-read contract** | Fresh generic capsule/lens reads are accepted; strict reads retain exact-authority gates. Facts, KFTS, and accepted memory remain derived recall, never lifecycle authority |
| Source-bound dependency and evidence graph | **Present, Preview contract** | Web graph names the source field for every edge and exposes missing targets |
| Causal provenance activation and rollback runtime | **Reimplement** | Tables exist, but import, CAS activation, rollback, and coverage receipts do not yet ship |
| Local run records, process identity, sidecars, status | **Present** | Atomic writes, attempt fencing, process custody, and terminal reconciliation |
| Tracked CPU job launcher binding | **Present, Preview contract** | Exact work/session/claim/fence validation, one reserved run row, and a transaction-held final guard prevent stale or concurrently revoked launches; telemetry never synthesizes work |
| External job/event collector | **Planned** | Current launcher accounts for its own jobs; a generic reconciler for external background sources is absent |
| Local MLX model orchestration | **Present, Preview contract** | `coord-models`, hardware/dependency preflight, process-held resource lock, neutral catalog, and failure cleanup |
| Global resource governor and watchdog | **Partial** | Process-group controls and model lock exist; resident mode enforcement, emergency latch, and global GPU-lane recovery are not parity-complete |
| Unified safety and verifier surface | **Present** | `coord doctor` is read-only and covers generic path, pointer, Git, MCP configuration, lifecycle, and job checks with stable PASS/BLOCKED output |
| Publication extraction and history gate | **Present** | Index-only inventory, explicit source manifest, PNG parsing, privacy patterns, fidelity, and reachable-history scan |
| Web projection | **Present, Preview contract** | Loopback, read-only/query-only SQLite, GET/HEAD reads plus OPTIONS metadata, versioned snapshot |
| macOS desktop and menu-bar viewer | **Present, Preview contract** | Clean-room SwiftUI snapshot client with cache, health, work, jobs, graph summary, and menu bar |
| iOS viewer | **Present, Preview contract** | Clean-room Home, Jobs, and Settings client over the same snapshot |
| Rich product operations console | **Excluded** | Product modules, startup atlas, action broker, branded surfaces, and hosted operations stay in the private product |
| Freeform shared whiteboard | **Planned** | The source contains a product annotation overlay, not a reusable authoritative coordination whiteboard |
| Legacy JSON roadmap bridges | **Excluded** | The standalone contract uses `coord.db`; retired sidecar lifecycle compatibility would create a second authority |
| Product data, reports, tasks, models, branding, and domain UI | **Excluded** | Never part of the standalone extraction scope |

## Remaining priority waves

1. Implement causal provenance runtime before any readiness claim consumes it.
2. Add generic external event ingestion with diagnostic-versus-authoritative
   fencing.
3. Finish resident resource enforcement and ownership-aware lock recovery.
4. Expand native diagnostics and saved read-only views without enabling
   lifecycle actions.
5. Implement a standalone advisory whiteboard only if its versioning,
   redaction, and non-authority contract are explicit.

“Excluded” is a decision, not a missing-file excuse. “Preview” is source that
works locally but may still change. No status in this document authorizes public
visibility, hosted service, app distribution, or publication of the source
product.
