"""The lease and proof gates on `heartbeat`/`complete`, measured rather than asserted.

`release_claim` has a test for each of its epoch checks; the two verbs beside it
did not. Ablating `heartbeat`'s expiry check, `complete`'s status check,
`complete`'s expiry check and `complete`'s proof requirement each left the
suite green -- so the guarantee this product is mostly made of ("a completion
names an artifact, and a dead lease cannot write anything") rested on nothing
that would notice its removal.

The proof gate is the load-bearing one: `complete_claim` refuses a claim whose
work item declares no `done_signal`, which is what stops "done" from being an
assertion an agent can simply make about itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

WORK_ID = "DEMO-CLA-COMPLETION-GATE"
SESSION = "claude:completion-gate"
PROOF = "artifacts/completion-gate.json"


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
        title="A work item the completion gate is measured against",
        assignee="claude",
        done_signal=PROOF,
        acceptance_json=json.dumps(["the proof artifact exists"]),
        intent_state="queued",
    )
    return coord_db.claim_work(conn, SESSION, WORK_ID, step="starting")


def _expire(conn, claim_id: str) -> None:
    with coord_db.tx(conn):
        conn.execute(
            "UPDATE claims SET expires_at=? WHERE claim_id=?",
            (coord_db.db_now(conn) - 7200.0, claim_id),
        )


def _write_proof(project: Path) -> None:
    artifact = project / PROOF
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"rows": 3}), encoding="utf-8")


def _claim_status(conn, claim_id: str) -> str:
    return str(
        conn.execute(
            "SELECT status FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()["status"]
    )


def _intent(conn) -> str:
    return str(
        conn.execute(
            "SELECT intent_state FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()["intent_state"]
    )


def test_an_expired_lease_cannot_heartbeat_itself_back_to_life(conn) -> None:
    claim_id = _claimed(conn)
    _expire(conn, claim_id)
    before = conn.execute(
        "SELECT expires_at FROM claims WHERE claim_id=?", (claim_id,)
    ).fetchone()["expires_at"]

    with pytest.raises(ValueError) as excinfo:
        coord_db.heartbeat_claim(
            conn, claim_id, step="still here", session_id=SESSION, actor="claude"
        )

    assert "expired" in str(excinfo.value) and claim_id in str(excinfo.value)
    after = conn.execute(
        "SELECT expires_at FROM claims WHERE claim_id=?", (claim_id,)
    ).fetchone()["expires_at"]
    assert after == before, "the refused heartbeat still extended the lease"


def test_a_released_claim_cannot_be_completed(conn, project: Path) -> None:
    claim_id = _claimed(conn)
    coord_db.release_claim(
        conn,
        claim_id,
        status="released",
        reason="handing it back",
        session_id=SESSION,
        actor="claude",
    )
    _write_proof(project)

    with pytest.raises(ValueError) as excinfo:
        coord_db.complete_claim(
            conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
        )

    assert claim_id in str(excinfo.value)
    assert _claim_status(conn, claim_id) == "released"
    assert _intent(conn) not in coord_db.TERMINAL_WORK_STATES


def test_an_expired_lease_cannot_complete_the_work(conn, project: Path) -> None:
    claim_id = _claimed(conn)
    _write_proof(project)
    _expire(conn, claim_id)

    with pytest.raises(ValueError) as excinfo:
        coord_db.complete_claim(
            conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
        )

    assert "expired" in str(excinfo.value)
    assert _intent(conn) not in coord_db.TERMINAL_WORK_STATES


def test_completion_requires_a_declared_proof_artifact(conn, project: Path) -> None:
    """`done` is not an assertion an agent gets to make about itself."""
    claim_id = _claimed(conn)
    _write_proof(project)
    with coord_db.tx(conn):
        conn.execute(
            "UPDATE work_items SET done_signal='' WHERE work_id=?", (WORK_ID,)
        )

    with pytest.raises(ValueError) as excinfo:
        coord_db.complete_claim(
            conn,
            claim_id,
            artifact_path=PROOF,
            proof_root=project,
            session_id=SESSION,
            actor="claude",
        )

    # Naming the artifact at completion time is not the same as the controller
    # having declared it, and the refusal has to say so.
    assert "done_signal" in str(excinfo.value)
    assert _claim_status(conn, claim_id) == "running"
    assert _intent(conn) not in coord_db.TERMINAL_WORK_STATES


def test_the_gate_opens_for_a_live_claim_with_its_declared_proof(
    conn, project: Path
) -> None:
    claim_id = _claimed(conn)
    _write_proof(project)

    coord_db.complete_claim(
        conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
    )

    assert _claim_status(conn, claim_id) == "completed"
    assert _intent(conn) in coord_db.TERMINAL_WORK_STATES
