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

This package is written mostly against `pathlib`, which is itself cross-platform, but its
runtime -- process supervision, file locking, permission bits -- is POSIX-shaped. The
three tiers below are stated as what was actually checked, not as a plan.

### macOS -- developer-verified, not CI-verified

The primary development and manual-test environment. The Python test suite is run here
routinely, but no GitHub Actions workflow runs it on macOS: `native.yml` builds and tests
only the Swift/Xcode app on `macos-15`. A macOS-only regression in the Python package can
therefore land without CI catching it.

### Linux -- CI-verified for the Python package

`python.yml` runs the full non-slow suite, the complete browser suite, and a clean
wheel/sdist install, all on `runs-on: ubuntu-latest`, across Python 3.11-3.13. This is the
strongest portability claim in the matrix above and the one CI actually enforces.

### Windows -- not supported; specific blockers, not a timeline

No CI job runs any part of this package on Windows, and several modules would fail before
first use. These are the concrete blockers, not an exhaustive list:

- **Unconditional POSIX-only imports.** `import fcntl` at module load time in
  `src/coordharness/coord/native_cockpit.py`, `src/coordharness/runtime/console_release_retention.py`,
  `src/coordharness/knowledge/accepted_memory_r4.py`, `src/coordharness/jobs/pglaunch.py`, and
  `src/coordharness/jobs/diagnostic_marker.py` raise `ModuleNotFoundError` at import time on
  Windows, before any platform check runs. `src/coordharness/jobs/resource_lock.py` shows the
  pattern these five would need: it imports `fcntl` inside a `try/except ImportError`, sets it to
  `None` on failure, and raises a descriptive `ResourceLockError` only when a lock is actually
  requested -- the module still imports cleanly.
- **Process-group and signal semantics.** `os.fork` / `os.setsid` / `os.killpg` in
  `src/coordharness/jobs/pglaunch.py` (`os.fork()` at line 219, `os.killpg` at lines 159, 194, 204)
  and `src/coordharness/jobs/launch.py` (`_terminate_process_group`, `os.killpg` at lines 261, 269,
  506) assume a POSIX process-group model with no Windows equivalent in this code path.
  `os.kill(pid, 0)` as a liveness probe (`src/coordharness/coord/process_liveness.py:17`,
  `src/coordharness/coord/reaper.py:25`, `src/coordharness/jobs/sidecar_writer.py:215,301`) runs
  on Windows but with different signal-0 semantics, and the broad `except OSError` around it would
  silently misreport a live process as dead rather than fail loudly.
- **`ps` as an external dependency.** `src/coordharness/coord/process_liveness.py` shells out to
  the POSIX `ps` binary (via `subprocess`) to read process start time; there is no Windows
  equivalent invoked.
- **Permission bits that mean something different on Windows.** `os.chmod`/`os.open` mode bits
  such as `0o600` and `0o700` (`src/coordharness/coord/coord_db.py:150,158`,
  `src/coordharness/usage/replica.py` and `src/coordharness/usage/local_profiles.py`,
  `src/coordharness/jobs/diagnostic_marker.py:225,292,316`) express POSIX owner-only permissions;
  NTFS ACLs do not map onto them, so these calls would not raise but also would not deliver the
  access control the code assumes.
- **Bash-only tooling.** `scripts/setup.sh` (this branch's clone-setup entry point) is a
  `#!/usr/bin/env bash` script gated with `set -euo pipefail` and array/`${BASH_SOURCE[0]}`
  syntax; it runs unmodified on Linux and macOS but needs WSL or Git Bash on Windows, since `cmd.exe`
  and PowerShell cannot execute it directly. The venv/db/config lane it drives has no macOS-specific
  dependency left in it -- only the native macOS/iOS app lane (Xcode, `apps/install.sh`) is
  Darwin-gated -- but the script itself is still a POSIX shell script, which is an unrelated
  portability question from what it installs.

What already generalizes: path construction is `pathlib`-based throughout the package (67 of 117
modules under `src/coordharness` import `pathlib.Path`; only one uses `os.path.join`), and the
two modules that do hardware/platform branching --
`src/coordharness/coord/modeld_lite.py` and `src/coordharness/coord/runners/mlx_runner.py` -- do
it with an explicit `sys.platform` / `platform.machine()` check rather than an import-time
failure, which is the pattern the five `fcntl` imports above do not yet follow.

Network filesystems and multiple hosts are excluded on every platform because SQLite file
locking is not a distributed coordination protocol.

The current feature truth is [`feature-status.json`](feature-status.json).
