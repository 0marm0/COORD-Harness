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
- Native clients consume a snapshot and do not open `coord.db`. This keeps SQLite schema migration independent of app releases.

## Portability limits

Process liveness uses operating-system identity and is strongest on POSIX systems. Windows support is planned work, not a current claim. Network filesystems and multiple hosts are excluded because SQLite file locking is not a distributed coordination protocol.

The current feature truth is [`feature-status.json`](feature-status.json).
