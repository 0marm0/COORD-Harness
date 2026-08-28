from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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

_JOURNAL_SIZE_LIMIT = 33_554_432
WAL_AUTOCHECKPOINT_PAGES = int(os.environ.get("COORD_COORD_WAL_AUTOCHECKPOINT_PAGES", "1000"))
SQLITE_HEADER = b"SQLite format 3\x00"
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
    if tables and not (tables & _COORD_SENTINEL_TABLES):
        raise RuntimeError(
            f"coord.db at {path} appears to be a foreign SQLite database "
            f"(tables={sorted(tables)[:5]}); refusing to open"
        )


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    assert_outside_warehouse(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_existing_db_file(db_path)
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
