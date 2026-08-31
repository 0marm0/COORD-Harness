from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from coordharness import config as _harness_config

_REPO_ROOT = _harness_config.project_root()
DEFAULT_DB_PATH = _harness_config.coord_db_path()

_WAREHOUSE_MARKERS = tuple(
    part
    for part in os.environ.get("COORD_EXCLUDED_PATH_MARKERS", "").split(",")
    if part.strip()
)

DEFAULT_LANES: tuple[str, ...] = ("claude", "codex")
_LANES_ENV_VAR = "COORD_LANES"

_JOURNAL_SIZE_LIMIT = 33_554_432
WAL_AUTOCHECKPOINT_PAGES = int(os.environ.get("COORD_COORD_WAL_AUTOCHECKPOINT_PAGES", "1000"))
SQLITE_HEADER = b"SQLite format 3\x00"
COORD_DB_FILE_MODE = 0o600
# SQLite's own files, not ours. `-wal` and `-shm` hold committed and in-flight
# page images, so they disclose exactly what the database discloses; `-journal`
# is the rollback-mode equivalent and is listed because a database opened before
# the WAL pragma runs, or one whose journal mode was changed, still produces it.
DB_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
_COORD_SENTINEL_TABLES = {"agent_sessions", "work_items", "claims", "runs"}
_AUTONOMY_TIER_KEYS = (
    "supervisor_readonly_digest",
    "modeld_advisory",
    "bounded_writers",
)


def _canonical_json_sha256(raw: str) -> str:
    value = json.loads(str(raw))
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _declaration_core_sha256(raw: str) -> str:
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise ValueError("authority declaration must be an object")
    value.pop("authority_source_sha256", None)
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _register_coord_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "coord_canonical_json_sha256", 1, _canonical_json_sha256, deterministic=True
    )
    conn.create_function(
        "coord_declaration_core_sha256", 1, _declaration_core_sha256, deterministic=True
    )


def configured_lanes() -> tuple[str, ...]:
    """The coordination lanes this deployment recognises, in declared order.

    Read from ``COORD_LANES`` (comma-separated) on every call so a test or a
    supervisor can rebind the set without reimporting the control plane. Tokens
    are trimmed and lowercased, duplicates collapse to their first appearance,
    and each token must satisfy the same public-identifier grammar as
    ``COORD_ACTOR``. An empty or whitespace-only value is a configuration error
    rather than a silent fallback: a deployment that sets the variable meant to
    say something, and an empty lane set would refuse every actor.
    """
    raw = os.environ.get(_LANES_ENV_VAR)
    if raw is None:
        return DEFAULT_LANES
    lanes: list[str] = []
    for token in str(raw).split(","):
        clean = token.strip().lower()
        if not clean:
            continue
        _harness_config.actor_name(clean)
        if clean not in lanes:
            lanes.append(clean)
    if not lanes:
        raise ValueError(
            f"{_LANES_ENV_VAR} must name at least one lane "
            f"(comma-separated, e.g. {','.join(DEFAULT_LANES)})"
        )
    return tuple(lanes)


def lane_set() -> frozenset[str]:
    """``configured_lanes`` as a set, for membership tests."""
    return frozenset(configured_lanes())


def lanes_display() -> str:
    """The configured lanes rendered for an error message: ``claude|codex``."""
    return "|".join(configured_lanes())


def counterpart_lane(actor: str | None) -> str | None:
    """The lane a cross-lane message from ``actor`` defaults to.

    With the default two lanes this is exactly the other one. With more than
    two it is the first configured lane that is not ``actor`` -- deterministic,
    and still never the actor's own lane, which is the invariant that matters
    (a lane may not review or hand off to itself). Returns ``None`` when no
    other lane is configured.
    """
    clean = str(actor or "").strip().lower()
    for lane in configured_lanes():
        if lane != clean:
            return lane
    return None


def assert_outside_warehouse(path: Path) -> None:
    rp = str(Path(path).resolve())
    for marker in _WAREHOUSE_MARKERS:
        if f"/{marker}/" in rp or rp.endswith(f"/{marker}"):
            raise RuntimeError(
                f"coord.db path {rp!r} resolves under warehouse root {marker!r}; "
                "the control plane must stay outside the .nosync data warehouse."
            )


def _validate_existing_db_file(path: Path) -> None:
    if not path.exists():
        return
    size = path.stat().st_size
    if size == 0:
        raise RuntimeError(f"coord.db at {path} is empty/zero-byte; refusing to create over it")
    with path.open("rb") as f:
        header = f.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise RuntimeError(f"coord.db at {path} has a foreign/non-SQLite header; refusing to open")
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            r[0]
            for r in ro.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not str(r[0]).startswith("sqlite_")
        }
    finally:
        ro.close()
    if not tables:
        # A valid, openable SQLite file with zero user tables previously fell
        # through here: `tables and not (tables & _COORD_SENTINEL_TABLES)` is
        # False when `tables` is empty, so this looked exactly like a legal
        # not-yet-bootstrapped coord.db instead of what it actually is -- a
        # database nothing has ever run `bootstrap_database()` against, or an
        # unrelated empty file. `connect()`/`connect_ro()` are read/write
        # accessors, not the bootstrapper, so they must refuse it the same way
        # they refuse a foreign schema below rather than silently opening it.
        raise RuntimeError(
            f"coord.db at {path} is a valid SQLite file with no tables at all "
            "(not even the coordination schema); refusing to open. Fix: run "
            "bootstrap_database() against this path to initialize it, or point "
            "COORD_DB / --db at the correct existing coord.db."
        )
    if not (tables & _COORD_SENTINEL_TABLES):
        raise RuntimeError(
            f"coord.db at {path} appears to be a foreign SQLite database "
            f"(tables={sorted(tables)[:5]}); refusing to open"
        )


def enforce_db_file_modes(path: Path | str | None = None) -> dict[str, str]:
    """Hold the database and its SQLite sidecars at ``0600``, best effort.

    The trust boundary for this control plane is the filesystem, so the mode on
    these files is the boundary. Nothing set it before: a database created under
    the common ``022`` umask lands at ``0644``, and so do ``-wal`` and ``-shm``,
    which carry the same content.

    Two measured facts shape the design, and both are why a single chmod at
    creation would not have been enough:

    * SQLite deletes ``-wal``/``-shm`` on the last clean close and recreates them
      on the next write. A chmod applied to the sidecars alone is undone by the
      next open/close cycle.
    * SQLite derives the creation mode of a sidecar from the *main database
      file's* mode. Tightening the database before the connection is what makes
      the recreated sidecars ``0600`` without any further action.

    So the database mode is enforced before the connection opens (governing what
    SQLite creates) and again after, which catches a database this call itself
    created. Returns a per-file report rather than raising: a database owned by
    another account cannot be chmod'ed by this process, and refusing to open it
    would be a worse outcome than opening it and being able to say so. Callers
    that need the guarantee must read the report.
    """
    base = Path(path) if path is not None else DEFAULT_DB_PATH
    report: dict[str, str] = {}
    for suffix in ("", *DB_SIDECAR_SUFFIXES):
        target = Path(str(base) + suffix)
        name = suffix or "db"
        try:
            info = target.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            # chmod follows symlinks; a link standing where the database should
            # be is a different problem and this is not the function that owns
            # it. Report rather than widen the mode of whatever it points at.
            report[name] = "skipped:symlink"
            continue
        if not stat.S_ISREG(info.st_mode):
            report[name] = "skipped:not-regular"
            continue
        if stat.S_IMODE(info.st_mode) == COORD_DB_FILE_MODE:
            report[name] = "ok"
            continue
        try:
            os.chmod(target, COORD_DB_FILE_MODE)
        except OSError as exc:
            report[name] = f"refused:{exc.errno}"
        else:
            report[name] = "tightened"
    return report


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    assert_outside_warehouse(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_existing_db_file(db_path)
    # Before the connection, so SQLite inherits the tight mode onto any sidecar
    # it is about to create; see enforce_db_file_modes for what was measured.
    enforce_db_file_modes(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        _register_coord_functions(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA journal_size_limit = {_JOURNAL_SIZE_LIMIT}")
        conn.execute(f"PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT_PAGES}")
    except Exception:
        conn.close()
        raise
    # Again, because the pass above cannot tighten a database that did not exist
    # yet: this connection created it, and the WAL pragma created its sidecars,
    # both at the umask default.
    enforce_db_file_modes(db_path)
    return conn


def check_integrity(path: Path | str | None = None) -> str:
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    assert_outside_warehouse(db_path)
    _validate_existing_db_file(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    try:
        result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"coord.db integrity_check failed: {result}")
    return result


def connect_ro(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    assert_outside_warehouse(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"coord.db does not exist: {db_path}")
    _validate_existing_db_file(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        _register_coord_functions(conn)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        conn.close()
        raise
    return conn


def resource_modes_path(path: Path | str | None = None) -> Path:
    return Path(path) if path is not None else _harness_config.resource_modes_path()


def harness_autonomy_config(
    path: Path | str | None = None,
    *,
    default_enabled: bool = False,
) -> dict[str, Any]:
    modes_path = resource_modes_path(path)
    tiers = {key: bool(default_enabled and key != "bounded_writers") for key in _AUTONOMY_TIER_KEYS}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source": str(modes_path),
        "enabled": bool(default_enabled),
        "mode": "report",
        "tiers": tiers,
        "tier2_requires": ["R6.1", "structured_status:enforce"],
        "reason": "default",
    }
    try:
        parsed = json.loads(modes_path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["reason"] = f"resource_modes_unreadable:{exc}"
        return payload
    if not isinstance(parsed, dict):
        payload["enabled"] = False
        payload["tiers"] = {key: False for key in _AUTONOMY_TIER_KEYS}
        payload["reason"] = "resource_modes_not_object"
        return payload
    raw = parsed.get("harness_autonomy")
    if not isinstance(raw, dict):
        payload["reason"] = "harness_autonomy_missing"
        return payload
    enabled = bool(raw.get("enabled"))
    raw_tiers = raw.get("tiers") if isinstance(raw.get("tiers"), dict) else {}
    payload.update(
        {
            "enabled": enabled,
            "mode": str(raw.get("mode") or "report"),
            "reason": str(raw.get("reason") or ("enabled" if enabled else "master_disabled")),
            "kill_switch": "resource_modes.json:harness_autonomy.enabled",
        }
    )
    payload["tiers"] = {
        "supervisor_readonly_digest": bool(enabled and raw_tiers.get("supervisor_readonly_digest")),
        "modeld_advisory": bool(enabled and raw_tiers.get("modeld_advisory")),
        "bounded_writers": bool(
            enabled
            and raw_tiers.get("bounded_writers")
            and raw.get("r6_1_complete") is True
            and raw.get("structured_status_mode") == "enforce"
        ),
    }
    if isinstance(raw.get("tier2_requires"), list):
        payload["tier2_requires"] = [str(item) for item in raw["tier2_requires"]]
    return payload
