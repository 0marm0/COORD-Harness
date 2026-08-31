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

No CI job runs any part of this package on Windows. Two of the original import-time blockers are
now resolved (below); these are the concrete blockers that remain, not an exhaustive list:

- **Unconditional POSIX-only imports -- RESOLVED.** All six modules that use `fcntl` for advisory
  file locking (`src/coordharness/coord/native_cockpit.py`,
  `src/coordharness/runtime/console_release_retention.py`,
  `src/coordharness/knowledge/accepted_memory_r4.py`, `src/coordharness/jobs/pglaunch.py`,
  `src/coordharness/jobs/diagnostic_marker.py`, and `src/coordharness/jobs/resource_lock.py`, which
  established the pattern) now import `fcntl` inside a `try/except ImportError` and set it to
  `None` on failure, so every one of them imports cleanly on a platform without `fcntl`. What
  happens when a caller then actually needs the lock differs by module, because "no lock" means
  different things to different call sites:
  - `console_release_retention.apply_retention_plan()` and `accepted_memory_r4.publish_current()`
    delete release directories / perform a compare-and-swap pointer publish respectively --
    running either unlocked is a correctness trap, not a convenience gap, so both raise a
    descriptive error (`RetentionError` / `AcceptedMemoryError`) immediately and mutate nothing.
  - `diagnostic_marker.wrapper_control_lock()` guards concurrent job-control writes the same way;
    entering it raises `WrapperControlLockError` before touching any file.
  - `native_cockpit.flush_requested_refresh()` only uses its lock to avoid redundant concurrent
    refreshes (not corruption), so it degrades to running unlocked, exposes that fact via the
    module-level `FCNTL_AVAILABLE` flag, and logs the degradation once per process.
  - `pglaunch._terminalize_wrapper_loss()` is a best-effort recovery path; without the lock it
    reports "did not terminalize" (a return value its other failure branches already produce)
    rather than mutating the shared progress sidecar unprotected, and also logs once.
  - Test coverage: `tests/test_portability_guards.py` simulates fcntl's absence via an import hook
    for all six modules and asserts each import succeeds and degrades as described above.
- **`ps` as an external dependency -- RESOLVED (explicit, not simulated).**
  `src/coordharness/coord/process_liveness.py` shells out to the POSIX `ps` binary (via
  `subprocess`) to read a process's start time (`ps -o lstart=`); there is no Windows equivalent to
  invoke, and none is faked. The module now probes for the `ps` binary once at import time
  (`PS_LSTART_AVAILABLE`, via `shutil.which`) instead of discovering its absence on first use, and
  `pid_start_time()` reports unavailable (`None`) immediately -- logging that once per process
  rather than on every call -- when it is absent. `pid_matches()` already treated an unresolvable
  start time as "cannot verify" (conservative `False` when a caller supplied one to check against);
  that behavior is unchanged, so PID-reuse protection degrades visibly to a bare `pid_exists()`
  liveness check rather than silently passing. `tests/test_portability_guards.py` covers both the
  absence path and the pre-existing "cannot verify" semantics.
- **Process-group and signal semantics -- still open.** `os.fork` / `os.setsid` / `os.killpg` in
  `src/coordharness/jobs/pglaunch.py` (`os.fork()` at line 219, `os.killpg` at lines 159, 194, 204)
  and `src/coordharness/jobs/launch.py` (`_terminate_process_group`, `os.killpg` at lines 261, 269,
  506) assume a POSIX process-group model with no Windows equivalent in this code path -- unlike
  the `fcntl` imports, these are used unconditionally rather than behind a capability guard, and
  fixing them is a separate, larger change than the import-time fix above.
  `os.kill(pid, 0)` as a liveness probe (`src/coordharness/coord/process_liveness.py:17`,
  `src/coordharness/coord/reaper.py:25`, `src/coordharness/jobs/sidecar_writer.py:215,301`) runs
  on Windows but with different signal-0 semantics, and the broad `except OSError` around it would
  silently misreport a live process as dead rather than fail loudly.
- **Permission bits that mean something different on Windows.** `os.chmod`/`os.open` mode bits
  such as `0o600` and `0o700` (`src/coordharness/coord/coord_db.py:150,158`,
  `src/coordharness/usage/replica.py` and `src/coordharness/usage/local_profiles.py`,
  `src/coordharness/jobs/diagnostic_marker.py:233,313,337`) express POSIX owner-only permissions;
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
failure, which is now also the pattern every `fcntl` import in the package follows (above), and
the pattern `process_liveness.PS_LSTART_AVAILABLE` follows for the `ps` dependency.

Network filesystems and multiple hosts are excluded on every platform because SQLite file
locking is not a distributed coordination protocol.

The current feature truth is [`feature-status.json`](feature-status.json).
