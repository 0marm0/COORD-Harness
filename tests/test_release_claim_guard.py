"""`release_claim` was the one claim mutator with no epoch check.

`heartbeat_claim` and `complete_claim` both refuse a claim that is no longer
`running` or whose lease has expired. `release_claim` refused neither, and it
is the verb that *writes the work item's intent_state*: a park or block
replayed against a dead claim rewrote the row from a lease nobody held. Worse,
the paused/blocked branch had no terminal-state guard -- the released/unclaimed
branch beside it did -- so parking a claim whose work had already been
completed dragged the finished row back to `paused` and it reappeared as open
work on the board.

These are the three legs: a completed work item survives a late park, a stale
lease cannot release at all, and the ordinary claim-then-park still works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

WORK_ID = "DEMO-CLA-RELEASE-GUARD"
SESSION = "claude:release-guard"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    bootstrap_database(tmp_path / ".coordharness" / "coord.db")
    return tmp_path


@pytest.fixture
def conn(project: Path):
    connection = connect(project / ".coordharness" / "coord.db")
    try:
        yield connection
    finally:
        connection.close()


def _claimed(conn) -> str:
    coord_db.register_session(conn, SESSION, "claude")
    coord_db.upsert_work(
        conn,
        WORK_ID,
        title="A work item the release guard is measured against",
        assignee="claude",
        done_signal="artifacts/release-guard.json",
        intent_state="queued",
    )
    return coord_db.claim_work(conn, SESSION, WORK_ID, step="starting")


def _intent(conn) -> str:
    return str(
        conn.execute(
            "SELECT intent_state FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()["intent_state"]
    )


def test_park_does_not_resurrect_completed_work(conn) -> None:
    """A late park against finished work must not reopen it."""
    claim_id = _claimed(conn)
    conn.execute(
        "UPDATE work_items SET intent_state='done' WHERE work_id=?", (WORK_ID,)
    )
    conn.commit()
    assert "done" in coord_db.TERMINAL_WORK_STATES

    coord_db.release_claim(
        conn,
        claim_id,
        status="paused",
        reason="parking after the row was already finished",
        next_step="nothing, the work is done",
        resume_when="never",
        resume_manual=True,
        session_id=SESSION,
        actor="claude",
    )

    assert _intent(conn) == "done"


def test_block_does_not_resurrect_completed_work(conn) -> None:
    """The blocked branch is the same UPDATE and needs the same guard."""
    claim_id = _claimed(conn)
    conn.execute(
        "UPDATE work_items SET intent_state='done' WHERE work_id=?", (WORK_ID,)
    )
    conn.commit()

    coord_db.release_claim(
        conn,
        claim_id,
        status="blocked",
        reason="blocking after the row was already finished",
        resume_when="never",
        resume_manual=True,
        session_id=SESSION,
        actor="claude",
    )

    assert _intent(conn) == "done"


def test_expired_claim_cannot_release(conn) -> None:
    claim_id = _claimed(conn)
    conn.execute("UPDATE claims SET expires_at=0 WHERE claim_id=?", (claim_id,))
    conn.commit()

    with pytest.raises(ValueError, match="expired"):
        coord_db.release_claim(
            conn,
            claim_id,
            status="released",
            reason="releasing on a dead lease",
            session_id=SESSION,
            actor="claude",
        )

    assert _intent(conn) == "running"


def test_already_released_claim_cannot_release_again(conn) -> None:
    """Same rule heartbeat states: a new epoch needs a new claim."""
    claim_id = _claimed(conn)
    coord_db.release_claim(
        conn,
        claim_id,
        status="released",
        reason="done for now",
        session_id=SESSION,
        actor="claude",
    )

    with pytest.raises(ValueError, match="ownership epoch"):
        coord_db.release_claim(
            conn,
            claim_id,
            status="paused",
            reason="replaying the release",
            next_step="whatever came next",
            resume_when="never",
            resume_manual=True,
            session_id=SESSION,
            actor="claude",
        )


def test_missing_claim_is_refused_not_ignored(conn) -> None:
    with pytest.raises(ValueError, match="missing claim"):
        coord_db.release_claim(
            conn,
            "clm_does_not_exist",
            status="released",
            reason="releasing thin air",
            session_id=SESSION,
            actor="claude",
        )


def test_happy_path_park_still_works(conn) -> None:
    claim_id = _claimed(conn)

    coord_db.release_claim(
        conn,
        claim_id,
        status="paused",
        reason="parking mid-flight",
        next_step="resume the second half",
        resume_when="the upstream artifact lands",
        resume_manual=True,
        session_id=SESSION,
        actor="claude",
    )

    assert _intent(conn) == "paused"
    row = conn.execute(
        "SELECT next_step, resume_when FROM work_items WHERE work_id=?", (WORK_ID,)
    ).fetchone()
    assert row["next_step"] == "resume the second half"
    assert row["resume_when"] == "the upstream artifact lands"
    claim = conn.execute(
        "SELECT status FROM claims WHERE claim_id=?", (claim_id,)
    ).fetchone()
    assert claim["status"] == "paused"


def test_happy_path_release_requeues(conn) -> None:
    claim_id = _claimed(conn)

    coord_db.release_claim(
        conn,
        claim_id,
        status="released",
        reason="handing it back",
        session_id=SESSION,
        actor="claude",
    )

    assert _intent(conn) == "queued"
