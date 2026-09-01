from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.board.snapshot import build_snapshot, stable_copy
from coordharness.coord import coord_db
from coordharness.coord.config import connect, connect_ro


def _tree_receipt(directory: Path) -> dict[str, tuple[int, int, int, str]]:
    return {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_stable_copy_of_clean_database_creates_no_source_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    writer = connect(db)
    try:
        # Ranked, because the snapshot's operator surface only carries queued
        # work somebody put a priority on. This test is about whether the read
        # reaches the row at all, so it seeds one the surface will keep.
        coord_db.upsert_work(
            writer, "CLEAN-1", title="clean database row", priority=1
        )
    finally:
        writer.close()

    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()
    before = _tree_receipt(tmp_path)

    document = build_snapshot(db)

    assert any(row["id"] == "CLEAN-1" for row in document["rows"])
    assert _tree_receipt(tmp_path) == before
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_stable_copy_preserves_committed_rows_that_exist_only_in_wal(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    writer = connect(db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        coord_db.upsert_work(
            writer,
            "WAL-ONLY-1",
            title="latest committed WAL row",
            # See CLEAN-1: ranked so the operator surface carries it, leaving
            # the WAL question this test asks the only one it can fail on.
            priority=1,
        )

        wal = Path(f"{db}-wal")
        assert wal.stat().st_size > 0

        immutable = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            assert immutable.execute(
                "SELECT 1 FROM work_items WHERE work_id = ?",
                ("WAL-ONLY-1",),
            ).fetchone() is None
        finally:
            immutable.close()
        before = _tree_receipt(tmp_path)

        with stable_copy(db) as copied:
            reader = connect_ro(copied)
            try:
                row = reader.execute(
                    "SELECT title FROM work_items WHERE work_id = ?",
                    ("WAL-ONLY-1",),
                ).fetchone()
            finally:
                reader.close()
        document = build_snapshot(db)

        assert row is not None and row["title"] == "latest committed WAL row"
        assert any(item["id"] == "WAL-ONLY-1" for item in document["rows"])
        assert _tree_receipt(tmp_path) == before
    finally:
        writer.close()


def test_stable_copy_fails_closed_before_copy_when_size_cap_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    before = _tree_receipt(tmp_path)
    monkeypatch.setenv("COORD_BOARD_SNAPSHOT_MAX_BYTES", str(db.stat().st_size - 1))

    with pytest.raises(RuntimeError, match="exceeds COORD_BOARD_SNAPSHOT_MAX_BYTES"):
        with stable_copy(db):
            pytest.fail("an oversized snapshot must not be yielded")

    assert _tree_receipt(tmp_path) == before
