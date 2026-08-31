"""The sentinel-table check `coord/config.py` applies before opening a coord.db.

Reviewer-disclosed gap: `_validate_existing_db_file` rejected a file whose
tables exist and share none with `_COORD_SENTINEL_TABLES`, but the guard was
written as `if tables and not (tables & _COORD_SENTINEL_TABLES): raise ...` --
which is False whenever `tables` is itself empty. A valid, openable SQLite
file with zero tables at all (an untouched `sqlite3.connect()` target, a copy
interrupted before `bootstrap_database()` ran, an unrelated empty `.db`) then
passed straight through `connect()`/`connect_ro()` as though it were a legal,
merely-not-yet-bootstrapped coord.db.

These pin the three file shapes the connection layer must tell apart --
zero-table (now refused), sentinel-bearing (still opens), genuinely-foreign
with unrelated tables (still refused, unchanged) -- plus the two boundary
cases the fix must not disturb: no file at all, and a corrupt/non-SQLite
header.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coordharness.coord import create_schema
from coordharness.coord.config import connect, connect_ro


def _zero_table_db(path: Path) -> None:
    """A real, openable SQLite file that never had a single CREATE TABLE run
    against it -- `PRAGMA user_version` forces SQLite to write page 1 (and
    therefore the header) without creating any user table.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def _foreign_db(path: Path) -> None:
    """A real SQLite file belonging to some other application entirely."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE unrelated_app_table (id INTEGER PRIMARY KEY, note TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def _sentinel_db(path: Path) -> None:
    """A genuine coord.db shape, via the same schema applier production uses."""
    create_schema.apply_schema(path)


# --------------------------------------------------------------------------
# The fix: zero tables is refused, not silently accepted


def test_connect_refuses_a_zero_table_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _zero_table_db(db)
    with pytest.raises(RuntimeError, match="no tables at all"):
        connect(db)


def test_connect_ro_refuses_a_zero_table_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _zero_table_db(db)
    with pytest.raises(RuntimeError, match="no tables at all"):
        connect_ro(db)


def test_zero_table_error_names_the_path_and_the_fix(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _zero_table_db(db)
    with pytest.raises(RuntimeError) as excinfo:
        connect(db)
    message = str(excinfo.value)
    assert str(db) in message
    assert "bootstrap_database()" in message
    assert "COORD_DB" in message and "--db" in message


# --------------------------------------------------------------------------
# A genuine coord.db still opens normally through both accessors


def test_connect_opens_a_genuine_sentinel_bearing_db(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _sentinel_db(db)
    conn = connect(db)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "work_items" in tables
    finally:
        conn.close()


def test_connect_ro_opens_a_genuine_sentinel_bearing_db(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _sentinel_db(db)
    conn = connect_ro(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The pre-existing friendly-error path: unrelated tables, none of them
# coordination tables. This must keep triggering after the zero-table
# branch is added above it -- the fix must not swallow it.


def test_connect_still_refuses_a_genuinely_foreign_db_with_tables(
    tmp_path: Path,
) -> None:
    db = tmp_path / "coord.db"
    _foreign_db(db)
    with pytest.raises(RuntimeError, match="foreign SQLite database"):
        connect(db)


def test_connect_ro_still_refuses_a_genuinely_foreign_db_with_tables(
    tmp_path: Path,
) -> None:
    db = tmp_path / "coord.db"
    _foreign_db(db)
    with pytest.raises(RuntimeError, match="foreign SQLite database"):
        connect_ro(db)


# --------------------------------------------------------------------------
# Boundary cases the fix must leave alone


def test_connect_creates_a_fresh_db_when_nothing_exists_yet(tmp_path: Path) -> None:
    """No file at all is not the zero-table case -- `connect()` still creates
    a brand-new database rather than refusing it.
    """
    db = tmp_path / "coord.db"
    assert not db.exists()
    conn = connect(db)
    conn.close()
    assert db.exists()


def test_connect_ro_still_raises_file_not_found_for_a_missing_db(
    tmp_path: Path,
) -> None:
    db = tmp_path / "coord.db"
    with pytest.raises(FileNotFoundError):
        connect_ro(db)


def test_connect_still_refuses_a_zero_byte_file(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    db.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty/zero-byte"):
        connect(db)


def test_connect_still_refuses_a_non_sqlite_header(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    db.write_bytes(b"not a sqlite file at all, just bytes\n")
    with pytest.raises(RuntimeError, match="foreign/non-SQLite header"):
        connect(db)
