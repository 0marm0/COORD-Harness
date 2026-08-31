"""The filtered board must not read the whole table to hand back fifty rows.

``status`` is derived in Python, not stored, so ``board_rows`` could not push a
status filter into SQL: it fetched every work item, derived a status for each,
then threw away all but the first N matches. At board size that is the whole
table scanned and enriched -- with a per-row re-SELECT of ``work_items`` on top
-- to answer a question about fifty rows.

The bounded scan walks the same order in chunks and stops at the Nth match, so
the only thing that may change is how much work is done. These tests pin the
part that must NOT change: the rows, their order, and their contents have to be
identical to what an unbounded scan filtered in Python would have produced --
including across a chunk boundary, which is where a keyset cursor would drop or
repeat a row if it were wrong.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

LANE_SESSION = "claude:bounded-board-fixture"
PEER_SESSION = "codex:bounded-board-fixture"
ROW_COUNT = 60
# Frozen inside the fixture's lease window, so a claimed row derives as
# "running" rather than the expired-lease "attention".
AT = time.time()


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A small board carrying several derived statuses at once.

    A filter test on a board that derives one status proves nothing, so the
    rows are seeded to land in different branches of ``derive_work_status``:
    claimed-and-live, failed, done, and the untouched backlog.
    """
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", tmp_path)

    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    coord_db.register_session(conn, LANE_SESSION, "claude")
    coord_db.register_session(conn, PEER_SESSION, "codex")
    for i in range(ROW_COUNT):
        if i % 5 == 0:
            intent = "queued"
        elif i % 5 == 1:
            intent = "failed"
        elif i % 5 == 2:
            intent = "done"
        else:
            intent = "planned"
        coord_db.upsert_work(
            conn,
            f"BND-{i:03d}",
            title=f"bounded board row {i}",
            assignee="claude" if i % 2 else "codex",
            module=f"module{i % 4}",
            surface="job",
            done_signal=f"artifacts/bnd-{i:03d}.json",
            acceptance_json='["the row is countable"]',
            intent_state=intent,
        )
    for i in range(0, ROW_COUNT, 7):
        if i % 5 in (1, 2):  # failed/done rows are terminal and cannot be claimed
            continue
        session = LANE_SESSION if i % 2 else PEER_SESSION
        coord_db.claim_work(conn, session, f"BND-{i:03d}", lease_s=600)
    yield conn
    conn.close()


def _reference(conn, status_filter=None, limit=None, group_by="module"):
    """What the unbounded scan produced: fetch everything, then filter."""
    rows = coord_db.board_rows(conn, at=AT, group_by=group_by)
    if status_filter is not None:
        wanted = (
            {status_filter}
            if isinstance(status_filter, str)
            else {str(s) for s in status_filter}
        )
        rows = [r for r in rows if str(r.get("status") or "").lower() in wanted]
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return rows


def _statuses(conn) -> list[str]:
    return sorted(
        {str(r["status"]) for r in coord_db.board_rows(conn, at=AT)}
    )


def test_every_derived_status_filters_to_the_same_rows(board) -> None:
    seen = _statuses(board)
    assert len(seen) >= 3, f"fixture derived too few statuses to test: {seen}"
    for status in seen:
        expected = _reference(board, status_filter=status)
        assert coord_db.board_rows(board, at=AT, status_filter=status) == expected


@pytest.mark.parametrize("limit", [0, 1, 3, 5, 50, 1000])
def test_a_limited_filter_returns_the_reference_prefix(board, limit: int) -> None:
    for status in _statuses(board):
        got = coord_db.board_rows(board, at=AT, status_filter=status, limit=limit)
        assert got == _reference(board, status_filter=status, limit=limit)
        assert len(got) <= limit


def test_the_scan_is_identical_across_chunk_boundaries(
    board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyset cursor that is off by one drops or repeats a row at the seam.

    Chunk sizes are chosen to be coprime-ish with the row count so the boundary
    lands in different places, including a chunk of one.
    """
    baseline_all = coord_db.board_rows(board, at=AT)
    baseline_by_status = {
        status: _reference(board, status_filter=status) for status in _statuses(board)
    }
    for chunk in (1, 2, 7, 13, ROW_COUNT - 1, ROW_COUNT, ROW_COUNT + 1, 500):
        monkeypatch.setattr(coord_db, "_BOARD_SCAN_CHUNK", chunk)
        assert coord_db.board_rows(board, at=AT) == baseline_all, chunk
        for status, expected in baseline_by_status.items():
            assert (
                coord_db.board_rows(board, at=AT, status_filter=status) == expected
            ), (chunk, status)
            assert (
                coord_db.board_rows(board, at=AT, status_filter=status, limit=2)
                == expected[:2]
            ), (chunk, status)


def test_an_unmatched_filter_still_reads_the_whole_board(board) -> None:
    """Early stop must not become early exit: no match means keep scanning."""
    assert coord_db.board_rows(board, at=AT, status_filter="archived", limit=5) == []
    coord_db.upsert_work(
        board,
        "BND-ARCHIVED",
        title="the only archived row, seeded last",
        assignee="claude",
        module="module0",
        surface="job",
        intent_state="archived",
    )
    got = coord_db.board_rows(board, at=AT, status_filter="archived", limit=5)
    assert [r["work_id"] for r in got] == ["BND-ARCHIVED"]


def test_a_bad_group_by_is_still_refused_under_a_filter(board) -> None:
    with pytest.raises(ValueError, match="cannot group the board by 'nope'"):
        coord_db.board_rows(board, at=AT, group_by="nope")
    # The refusal is about the shape of a row, so it must fire even when the
    # filter would have matched nothing -- otherwise a typo is visible or not
    # depending on the filter.
    with pytest.raises(ValueError, match="cannot group the board by 'nope'"):
        coord_db.board_rows(
            board, at=AT, group_by="nope", status_filter="archived", limit=5
        )


def test_operator_ok_still_validates_from_the_row_the_board_already_holds(
    board,
) -> None:
    """The N+1 removal hands the helper a v_work_owner row instead of re-reading.

    A validator that silently answers False for every row would look like a
    speedup and pass every filter test above, so pin a row that must answer
    True -- and pin it against the helper's own by-primary-key path.
    """
    work_id = "BND-000"
    row = board.execute(
        "SELECT * FROM work_items WHERE work_id=?", (work_id,)
    ).fetchone()
    coord_db.record_operator_sign_off(
        board,
        work_id=work_id,
        reason="the reviewing lane is offline and I read the artifact myself",
        refs=["artifacts/bnd-000.json"],
        operation_id="bounded-board-signoff-1",
        expected_version=int(row["version"]),
    )
    assert coord_db._has_valid_operator_ok_unlocked(board, work_id) is True

    by_work_id = {r["work_id"]: r for r in coord_db.board_rows(board, at=AT)}
    assert by_work_id[work_id]["operator_ok_validated"] is True
    for wid, r in by_work_id.items():
        assert r["operator_ok_validated"] == coord_db._has_valid_operator_ok_unlocked(
            board, wid
        ), wid


def test_the_fixture_is_not_degenerate(board) -> None:
    """Guards the tests above: a board of one status filters vacuously."""
    import collections

    counts = collections.Counter(
        str(r["status"]) for r in coord_db.board_rows(board, at=AT)
    )
    assert len(counts) >= 3, counts
    assert max(counts.values()) > 5, counts  # a filter that truncation can bite
    assert "running" in counts, counts
