# Compatibility

This page defines supported interfaces, not every environment in which the code might happen to run.

## Runtime matrix

| Surface | Compatibility |
|---|---|
| Python package and CLI | Python 3.11 or newer; POSIX process semantics are the primary tested path |
| Database | SQLite with WAL, foreign keys, and read-only URI support; local filesystem only |
| MCP | stdio transport through the optional `mcp>=1.20,<2.0` dependency |
| Web board | Loopback HTTP; current browser versions; no JavaScript framework dependency |
| macOS client | Preview; exact deployment target follows the source project and CI workflow |
| iOS client | Preview; exact deployment target follows the source project and CI workflow |

## Compatibility promises

- The SQLite schema changes through numbered, forward migrations. Downgrade compatibility is not promised.
- `coord` command names and required positional arguments are treated as public once shipped.
- MCP tool names are public; preview tools and complex parameter contracts may evolve before a stable release.
- The snapshot API is versioned under `/api/v1/`. Additive fields are expected; consumers must ignore unknown fields.
- Native clients differ, and the difference is deliberate. The macOS menu-bar panel (`CoordMenuBar`) and the Cockpit window (`CoordCockpitWindow`) read `COORD_DB` directly over a `SQLITE_OPEN_READONLY` connection with `PRAGMA query_only=ON`, falling back to the HTTP snapshot when the file is absent or unreadable; they are therefore coupled to the SQLite schema and must be rebuilt across a migration. The iOS client (`CoordCockpitIOS`) and the snapshot-only macOS app (`CoordCockpitMac`) consume `/api/v1/snapshot` and `/healthz` over `URLSession` and never open the database, which keeps them independent of schema migration. No client writes.

## Portability limits

Process liveness uses operating-system identity and is strongest on POSIX systems. Windows support is planned work, not a current claim. Network filesystems and multiple hosts are excluded because SQLite file locking is not a distributed coordination protocol.

The current feature truth is [`feature-status.json`](feature-status.json).
