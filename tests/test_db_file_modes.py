"""What mode the coordination database and its SQLite sidecars actually carry.

The threat model names the filesystem as the trust boundary, which makes the
mode on these files the boundary itself. Everything asserted here was measured
first and is written to hold the measured behaviour, not the hoped-for one --
including the two residuals at the bottom, which exist so that a later change
that quietly widens them has to delete a test that says why.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord.config import (
    COORD_DB_FILE_MODE,
    DB_SIDECAR_SUFFIXES,
    connect,
    connect_ro,
    enforce_db_file_modes,
)

WRITE = "INSERT INTO events(ts,kind,actor,trust,title) VALUES (?,?,?,?,?)"
WRITE_ARGS = (1.0, "note", "claude", "agent", "a write, so the WAL exists")


def _modes(db: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for suffix in ("", *DB_SIDECAR_SUFFIXES):
        target = Path(str(db) + suffix)
        out[suffix or "db"] = (
            oct(stat.S_IMODE(target.stat().st_mode)) if target.exists() else None
        )
    return out


@pytest.fixture
def loose_umask() -> int:
    """Run under the common permissive umask, which is the case that bites.

    A deployment whose umask is already 077 would pass these assertions for a
    reason that has nothing to do with this code.
    """
    previous = os.umask(0o022)
    try:
        yield 0o022
    finally:
        os.umask(previous)


@pytest.fixture
def board(tmp_path: Path, loose_umask: int) -> Path:
    db = tmp_path / "coord.db"
    bootstrap_database(str(db))
    return db


# --------------------------------------------------------------------------
# What creation produces
# --------------------------------------------------------------------------


def test_a_freshly_created_database_is_not_world_readable(board: Path):
    assert stat.S_IMODE(board.stat().st_mode) == COORD_DB_FILE_MODE
    assert _modes(board)["db"] == "0o600"


def test_the_sidecars_carry_the_same_mode_as_the_database(board: Path):
    """`-wal` and `-shm` hold page images, so they disclose what the db does."""
    conn = connect(board)
    try:
        conn.execute(WRITE, WRITE_ARGS)
        observed = _modes(board)
    finally:
        conn.close()
    assert observed["-wal"] == "0o600", observed
    assert observed["-shm"] == "0o600", observed


def test_sidecars_recreated_after_a_close_are_still_tight(board: Path):
    """The case a chmod-at-creation would have missed.

    Measured: SQLite deletes `-wal`/`-shm` on the last clean close and creates
    them again on the next write. Whatever mode they had is gone with them.
    """
    first = connect(board)
    try:
        first.execute(WRITE, WRITE_ARGS)
    finally:
        first.close()
    assert _modes(board)["-wal"] is None, "the close should have removed the WAL"

    second = connect(board)
    try:
        second.execute(WRITE, (2.0, *WRITE_ARGS[1:]))
        observed = _modes(board)
    finally:
        second.close()
    assert observed["-wal"] == "0o600", observed
    assert observed["-shm"] == "0o600", observed


# --------------------------------------------------------------------------
# Why the enforcement is placed on the database rather than on the sidecars
# --------------------------------------------------------------------------


def test_sqlite_takes_the_sidecar_mode_from_the_database_file(tmp_path: Path,
                                                              loose_umask: int):
    """The measurement the design rests on, pinned against raw SQLite.

    Run without `connect()` on purpose: this is a statement about SQLite, and
    proving it through the code that now enforces the mode would prove nothing.
    A chmod of the sidecars alone does not survive; a chmod of the database is
    what SQLite copies forward.
    """
    db = tmp_path / "plain.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    os.chmod(str(db) + "-wal", 0o600)
    os.chmod(str(db) + "-shm", 0o600)
    assert _modes(db)["-wal"] == "0o600"
    conn.close()

    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("INSERT INTO t VALUES (2)")
    widened = _modes(db)
    conn.close()
    assert widened["db"] == "0o644", "control: the database itself is untouched"
    assert widened["-wal"] == "0o644", (
        "the sidecar chmod did not survive recreation -- the database's mode is "
        "what SQLite copies onto a new sidecar"
    )

    os.chmod(db, 0o600)
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("INSERT INTO t VALUES (3)")
    inherited = _modes(db)
    conn.close()
    assert inherited["-wal"] == "0o600" and inherited["-shm"] == "0o600", inherited


# --------------------------------------------------------------------------
# The enforcement function itself
# --------------------------------------------------------------------------


def test_an_existing_loose_database_is_tightened_on_open(board: Path):
    """A database created before this code existed is not left as it was."""
    os.chmod(board, 0o644)
    conn = connect(board)
    try:
        conn.execute(WRITE, WRITE_ARGS)
        observed = _modes(board)
    finally:
        conn.close()
    assert observed["db"] == "0o600"
    assert observed["-wal"] == "0o600"


def test_the_report_names_every_file_it_touched(board: Path):
    conn = connect(board)
    try:
        conn.execute(WRITE, WRITE_ARGS)
        os.chmod(str(board) + "-wal", 0o644)
        report = enforce_db_file_modes(board)
    finally:
        conn.close()
    assert report["db"] == "ok"
    assert report["-wal"] == "tightened"
    assert "-journal" not in report, "absent files are not reported on"


def test_a_chmod_this_process_cannot_perform_is_reported_not_raised(
    board: Path, monkeypatch: pytest.MonkeyPatch
):
    """A database owned by another account is a residual, not a crash.

    Refusing to open it would be worse than opening it, so the failure has to
    be legible in the return value instead.
    """
    os.chmod(board, 0o644)

    def _refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", _refuse)
    report = enforce_db_file_modes(board)
    assert report["db"] == "refused:1"


def test_a_symlink_in_the_database_position_is_not_chmodded(tmp_path: Path):
    """chmod follows symlinks; widening the target is not this call's business."""
    real = tmp_path / "real.db"
    real.write_bytes(b"SQLite format 3\x00")
    os.chmod(real, 0o644)
    link = tmp_path / "link.db"
    link.symlink_to(real)
    report = enforce_db_file_modes(link)
    assert report["db"] == "skipped:symlink"
    assert stat.S_IMODE(real.stat().st_mode) == 0o644


# --------------------------------------------------------------------------
# Residual exposure, asserted so the threat model's claim stays true
# --------------------------------------------------------------------------


def test_a_read_only_open_does_not_tighten_anything(board: Path):
    """`connect_ro` is a read accessor and stays one.

    A process that only ever reads therefore leaves a loose database loose.
    The threat model records this; the assertion is here so that if the
    behaviour changes, the document has to change with it.
    """
    os.chmod(board, 0o644)
    conn = connect_ro(board)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    assert _modes(board)["db"] == "0o644"


def test_creation_is_not_atomic_with_the_tightening(
    tmp_path: Path, loose_umask: int, monkeypatch: pytest.MonkeyPatch
):
    """The window is real and this names its size.

    The file is created by SQLite at the umask default and tightened afterwards,
    so there is an interval -- bounded by schema application, not by a single
    syscall -- in which a new database is readable by anyone. Nothing in the
    Python DB-API surface opens a database file with an explicit mode, so this
    is not closable at this layer.
    """
    db = tmp_path / "fresh.db"
    created_modes: list[int] = []
    real_chmod = os.chmod

    def _observe(path, mode, *args, **kwargs):
        if str(path) == str(db):
            created_modes.append(stat.S_IMODE(Path(path).stat().st_mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _observe)
    bootstrap_database(str(db))
    monkeypatch.undo()
    assert created_modes and created_modes[0] == 0o644, (
        "measured: the database exists at the umask default before it is tightened"
    )
    assert stat.S_IMODE(db.stat().st_mode) == COORD_DB_FILE_MODE
