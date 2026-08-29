"""One idempotent database bootstrap used by every public runtime surface."""

from __future__ import annotations

import hashlib
import sqlite3
from importlib.resources import files
from pathlib import Path

from . import config
from .coord import create_schema


def _migration_resources():
    root = files("coordharness.coord").joinpath("migrations")
    return sorted(
        (entry for entry in root.iterdir() if entry.name.endswith(".sql")),
        key=lambda entry: entry.name,
    )


def bootstrap_database(db_path: str | Path | None = None) -> dict:
    """Apply the base schema and each numbered migration exactly once."""
    path = Path(db_path) if db_path is not None else config.coord_db_path()
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    base = create_schema.apply_schema(path)
    applied: list[str] = []
    conn = sqlite3.connect(path)
    try:
        known = {
            str(row[0])
            for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
        for resource in _migration_resources():
            if resource.name in known:
                continue
            sql = resource.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            conn.executescript(sql)
            version = int(resource.name.split("_", 1)[0])
            conn.execute(
                "INSERT INTO schema_migrations(version,name,applied_at,checksum) "
                "VALUES (?,?,strftime('%s','now'),?)",
                (version, resource.name, checksum),
            )
            conn.commit()
            applied.append(resource.name)
    finally:
        conn.close()
    return {
        "db": str(path.resolve()),
        "base": base,
        "migrations_applied": applied,
        "profile": config.deployment_profile(),
    }


def database_current(db_path: str | Path | None = None) -> bool:
    """Check installed schema/migrations through a state-safe private copy."""
    path = Path(db_path) if db_path is not None else config.coord_db_path()
    if not path.is_file():
        return False
    from coordharness.board.snapshot import _materialized_connection

    try:
        with _materialized_connection(path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            views = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='view'"
                ).fetchall()
            }
            names = {
                str(row[0])
                for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
            }
    except Exception:
        return False
    expected_migrations = {"coord_v1_initial"} | {
        resource.name for resource in _migration_resources()
    }
    return (
        # work_contract_write_sets is listed because a database created before
        # it moved into the base schema is NOT current: the conflict query is a
        # read, a read-only connection cannot create the table it is missing,
        # and reporting such a database as current would skip the one bootstrap
        # that repairs it.
        {
            "schema_migrations", "agent_sessions", "work_items", "claims",
            "runs", "work_contract_write_sets",
        } <= tables
        and {"v_work_owner", "v_session_rollup"} <= views
        and expected_migrations <= names
    )


__all__ = ["bootstrap_database", "database_current"]
