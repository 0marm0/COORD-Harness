"""Three admission guards on `claim_work` that no test was watching.

`claim_work` is the front door, and it refuses three things before it will mint
a lease. Ablating each one -- the partial unique index that permits one held
claim per work item, the terminal/archived refusal, and the refusal to claim a
row whose declared resume predicate has not fired -- left the rest of the suite
green, so all three were claims about the code rather than measured properties.

Each test here is shaped against its own ablation: the exception is required
*and* the state that the guard exists to protect is asserted afterwards, so a
permissive rewrite cannot pass by raising something incidental or by raising
and mutating anyway.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

WORK_ID = "DEMO-CLA-CLAIM-ADMISSION"
HOLDER = "claude:admission-holder"
INTRUDER = "claude:admission-intruder"
PROOF = "artifacts/admission-proof.json"
PREDICATE = {"type": "artifact_exists", "path": "artifacts/upstream.json"}


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


def _seed(conn, **overrides) -> None:
    fields = dict(
        title="A work item the admission guards are measured against",
        assignee="claude",
        done_signal=PROOF,
        acceptance_json=json.dumps(["the proof artifact exists"]),
        intent_state="queued",
    )
    fields.update(overrides)
    coord_db.upsert_work(conn, WORK_ID, **fields)


def _live_claims(conn) -> list[tuple[str, str]]:
    return [
        (str(row["session_id"]), str(row["status"]))
        for row in conn.execute(
            "SELECT session_id, status FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked') ORDER BY acquired_at",
            (WORK_ID,),
        ).fetchall()
    ]


def _intent(conn) -> str:
    return str(
        conn.execute(
            "SELECT intent_state FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()["intent_state"]
    )


def test_two_unrelated_sessions_cannot_hold_the_same_work_at_once(conn) -> None:
    """The lease is exclusive, and the database is what makes it so.

    Two sessions of the same lane that share no family suffix, thread or
    worktree are two independent orchestrators. The first holds the row; the
    second must not be handed a second live lease on it.
    """
    coord_db.register_session(conn, HOLDER, "claude")
    coord_db.register_session(conn, INTRUDER, "claude")
    _seed(conn)

    held = coord_db.claim_work(conn, HOLDER, WORK_ID, step="starting")
    assert _live_claims(conn) == [(HOLDER, "running")]

    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        coord_db.claim_work(conn, INTRUDER, WORK_ID, step="taking it anyway")

    # The refusal has to leave the holder's lease intact and alone -- a second
    # live row here is the whole defect, exception or no exception.
    assert _live_claims(conn) == [(HOLDER, "running")]
    assert (
        str(
            conn.execute(
                "SELECT claim_id FROM claims WHERE work_id=? AND status='running'",
                (WORK_ID,),
            ).fetchone()["claim_id"]
        )
        == held
    )


def test_completed_work_cannot_be_reclaimed(conn, project: Path) -> None:
    coord_db.register_session(conn, HOLDER, "claude")
    _seed(conn)
    claim_id = coord_db.claim_work(conn, HOLDER, WORK_ID, step="starting")

    artifact = project / PROOF
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"rows": 3}), encoding="utf-8")
    coord_db.complete_claim(
        conn,
        claim_id,
        proof_root=project,
        session_id=HOLDER,
        actor="claude",
    )
    assert _intent(conn) in coord_db.TERMINAL_WORK_STATES

    with pytest.raises(ValueError) as excinfo:
        coord_db.claim_work(conn, HOLDER, WORK_ID, step="doing it again")

    message = str(excinfo.value)
    assert "terminal" in message and WORK_ID in message
    # A refused claim must not drag the finished row back to running.
    assert _intent(conn) in coord_db.TERMINAL_WORK_STATES
    assert _live_claims(conn) == []


def test_archived_work_cannot_be_reclaimed(conn) -> None:
    coord_db.register_session(conn, HOLDER, "claude")
    _seed(conn)
    with coord_db.tx(conn):
        conn.execute(
            "UPDATE work_items SET archived_at=? WHERE work_id=?",
            (coord_db.db_now(conn), WORK_ID),
        )

    with pytest.raises(ValueError) as excinfo:
        coord_db.claim_work(conn, HOLDER, WORK_ID, step="starting")

    assert "archived" in str(excinfo.value)
    assert _live_claims(conn) == []
    assert _intent(conn) == "queued"


def test_a_queued_row_still_waiting_on_its_predicate_cannot_be_claimed(conn) -> None:
    """A conditional park is a promise that the row reopens when it is ready.

    The row sits in `queued` so the board can show it, but its resume predicate
    has not emitted `continuation_ready_at` yet. Claiming it now is claiming
    work whose stated precondition is still false.
    """
    coord_db.register_session(conn, HOLDER, "claude")
    _seed(conn, resume_predicate_json=json.dumps(PREDICATE))

    with pytest.raises(ValueError) as excinfo:
        coord_db.claim_work(conn, HOLDER, WORK_ID, step="starting early")

    assert "resume predicate" in str(excinfo.value)
    assert _live_claims(conn) == []
    assert _intent(conn) == "queued"

    # Once the continuation fires, the same claim is admitted -- the guard is a
    # gate, not a wall, and a test that only proved the refusal could not tell
    # the difference.
    with coord_db.tx(conn):
        conn.execute(
            "UPDATE work_items SET continuation_ready_at=? WHERE work_id=?",
            (coord_db.db_now(conn), WORK_ID),
        )
    claim_id = coord_db.claim_work(conn, HOLDER, WORK_ID, step="starting for real")
    assert _live_claims(conn) == [(HOLDER, "running")]
    assert claim_id
