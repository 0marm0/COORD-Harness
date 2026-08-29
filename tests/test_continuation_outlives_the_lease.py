"""Auto-requeue has to outlive the lease of the claim that blocked the work.

A claim's lease is about an hour. Blocks routinely last longer than that: the
whole point of blocking on an upstream artifact is that you do not know when it
lands. When the lease expires, `_release_expired_claims_unlocked` releases the
claim and deliberately leaves the work item sticky-blocked -- an expired lease
must not silently unblock work.

`evaluate_continuations` then looked for a still-held `status='blocked'` claim
before it would requeue anything. After the first expiry there is no such claim
and there never will be again, so the auto-requeue path stopped matching the
row -- permanently, and without saying so. Nothing else sets
`continuation_ready_at`, so the predicate could come true a hundred times and
the board would show the row blocked on a blocker that cleared days ago.

The repair decides on what is true now rather than on the claim's survival: an
unheld blocked row whose predicate is satisfied is requeued on its own state.
Where the row *is* held by someone else it is left alone -- but the readiness is
still recorded, because the silence was the defect.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db, reaper
from coordharness.coord.config import connect

WORK_ID = "DEMO-CLA-CONTINUATION"
SESSION = "claude:worker"
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


def _blocked_on_upstream(conn) -> str:
    """A real block: claimed, released as blocked, classified for auto-requeue."""
    coord_db.register_session(conn, SESSION, "claude")
    coord_db.upsert_work(
        conn, WORK_ID,
        title="Blocked on an upstream artifact",
        assignee="claude",
        done_signal="artifacts/result.json",
        acceptance_json=json.dumps(["the downstream proof exists"]),
        intent_state="queued",
    )
    claim_id = coord_db.claim_work(conn, SESSION, WORK_ID, step="starting")
    coord_db.release_claim(
        conn, claim_id, status="blocked",
        reason="waiting on the upstream artifact",
        next_step="resume once upstream lands",
        resume_when="artifacts/upstream.json exists",
        resume_predicate_json=json.dumps(PREDICATE),
        session_id=SESSION, actor="claude",
    )
    version = conn.execute(
        "SELECT version FROM work_items WHERE work_id=?", (WORK_ID,)
    ).fetchone()["version"]
    coord_db.classify_blocked_work(
        conn, work_id=WORK_ID, reason_class="upstream_artifact_not_ready",
        expected_version=int(version), expected_reason_class="blocked",
        actor="claude", session_id=SESSION,
        note="the upstream job has not written its artifact yet",
    )
    return claim_id


def _expire_the_lease(conn, claim_id: str) -> None:
    with coord_db.tx(conn):
        conn.execute(
            "UPDATE claims SET expires_at=? WHERE claim_id=?",
            (coord_db.db_now(conn) - 7200.0, claim_id),
        )
    released = coord_db.release_expired_claims_batch(conn)
    assert released["released_count"] == 1
    row = released["released_rows"][0]
    assert row["prior_claim_status"] == "blocked"
    assert row["result_work_state"] == "blocked", (
        "the expiry sweep is supposed to leave the work item sticky-blocked; "
        "if that changed, this test is no longer describing the real state"
    )


def _the_blocker_clears(project: Path) -> None:
    artifact = project / "artifacts" / "upstream.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"rows": list(range(32))}), encoding="utf-8")


def _work(conn) -> dict:
    return dict(conn.execute(
        "SELECT intent_state, blocked_reason_class, continuation_ready_at"
        " FROM work_items WHERE work_id=?", (WORK_ID,)
    ).fetchone())


def _claims(conn) -> list[tuple[str, str]]:
    return [
        (str(row["claim_id"]), str(row["status"]))
        for row in conn.execute(
            "SELECT claim_id, status FROM claims WHERE work_id=? ORDER BY claim_id",
            (WORK_ID,),
        )
    ]


def _continuation_events(conn) -> list[dict]:
    return [
        {"body": str(row["body"]), **json.loads(row["payload_json"])}
        for row in conn.execute(
            "SELECT body, payload_json FROM events WHERE kind='continuation_ready'"
            " AND work_id=? ORDER BY event_id", (WORK_ID,)
        )
    ]


def test_auto_requeue_survives_the_blocking_claim_s_lease(project: Path, conn) -> None:
    claim_id = _blocked_on_upstream(conn)
    _expire_the_lease(conn, claim_id)
    assert _claims(conn) == [(claim_id, "unclaimed")]
    assert _work(conn)["intent_state"] == "blocked"

    _the_blocker_clears(project)

    first = reaper.evaluate_continuations(conn)
    assert first["ready_count"] == 1, (
        "the blocker cleared and nothing happened; auto-requeue went dark when "
        "the claim's lease expired"
    )
    assert first["ready"][0]["work_id"] == WORK_ID
    assert first["ready"][0]["requeued"] is True

    after = _work(conn)
    assert after["intent_state"] == "queued"
    assert after["blocked_reason_class"] is None
    assert after["continuation_ready_at"] is not None

    # And it settles: a second sweep must not re-fire on an already-ready row.
    assert reaper.evaluate_continuations(conn)["ready_count"] == 0

    events = _continuation_events(conn)
    assert len(events) == 1
    assert events[0]["requeued"] is True
    assert events[0]["released_claim_id"] is None, (
        "there was no claim to release, and the event must not imply one"
    )
    assert "lease had already expired" in events[0]["body"]


def test_a_block_whose_claim_is_still_held_is_requeued_against_that_claim(
    project: Path, conn
) -> None:
    """The path that already worked still works, and still releases the claim.

    Without this the repair is indistinguishable from deleting the claim check.
    """
    claim_id = _blocked_on_upstream(conn)
    _the_blocker_clears(project)

    result = reaper.evaluate_continuations(conn)

    assert result["ready_count"] == 1
    assert _work(conn)["intent_state"] == "queued"
    assert _claims(conn) == [(claim_id, "released")]
    events = _continuation_events(conn)
    assert events[0]["released_claim_id"] == claim_id


def test_a_row_another_holder_has_taken_is_not_yanked_but_is_not_silent(
    project: Path, conn
) -> None:
    """Requeuing is for unheld rows only -- and skipping still leaves a record.

    The state here is fabricated with SQL on purpose: `claim_work` refuses
    blocked work, so a live running claim over a blocked row cannot be reached
    through the verbs. It is reachable through repair paths and races, and the
    branch that handles it is worth pinning.
    """
    claim_id = _blocked_on_upstream(conn)
    with coord_db.tx(conn):
        conn.execute(
            "UPDATE claims SET status='running', version=version+1 WHERE claim_id=?",
            (claim_id,),
        )
    _the_blocker_clears(project)

    result = reaper.evaluate_continuations(conn)

    assert result["ready_count"] == 1, "the predicate came true and nothing recorded it"
    assert result["ready"][0]["requeued"] is False
    after = _work(conn)
    assert after["intent_state"] == "blocked", "a row someone is holding was requeued"
    assert after["continuation_ready_at"] is not None
    assert _claims(conn) == [(claim_id, "running")], "another holder's claim was released"


def test_a_reason_class_outside_the_allowlist_is_still_never_requeued(
    project: Path, conn
) -> None:
    """Only the mechanical reason classes auto-requeue; the rest are announced."""
    claim_id = _blocked_on_upstream(conn)
    version = conn.execute(
        "SELECT version FROM work_items WHERE work_id=?", (WORK_ID,)
    ).fetchone()["version"]
    coord_db.classify_blocked_work(
        conn, work_id=WORK_ID, reason_class="operator_decision",
        expected_version=int(version),
        expected_reason_class="upstream_artifact_not_ready",
        actor="claude", session_id=SESSION, note="needs a human call",
    )
    _expire_the_lease(conn, claim_id)
    _the_blocker_clears(project)

    result = reaper.evaluate_continuations(conn)

    assert result["ready_count"] == 1
    assert result["ready"][0]["requeued"] is False
    assert _work(conn)["intent_state"] == "blocked"
