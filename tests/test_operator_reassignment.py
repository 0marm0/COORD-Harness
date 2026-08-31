from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect


WORK_ID = "OPERATOR-REASSIGN-1"
CLAUDE_SESSION = "claude:operator-reassignment-fixture"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", tmp_path)
    # A git repository, because a resolved completion proof now means one git's
    # index carries -- for every artifact type, not only Markdown.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="Move the canonical operator assignment",
            assignee="claude",
            assigned_by="claude",
            module="coord",
            surface="job",
            done_signal="artifacts/operator-reassignment-proof.json",
            acceptance_json='["the owner changes under exact fences"]',
            note="operator reassignment fixture",
            intent_state="queued",
        )
        coord_db.register_session(conn, CLAUDE_SESSION, "claude")
    finally:
        conn.close()
    return database


def _request(conn, **overrides):
    row = conn.execute(
        "SELECT version,assignee FROM work_items WHERE work_id=?", (WORK_ID,)
    ).fetchone()
    fields = {
        "work_id": WORK_ID,
        "owner_lane": "codex",
        "target_intent": "queued",
        "task": "Take over the canonical coordination row",
        "why": "The resident controller chose a new operating lane",
        "acceptance": "Codex continues against the existing done signal",
        "refs": ["docs/operator-reassignment.md"],
        "constraints": ["preserve the declared done signal"],
        "operation_id": "operator-reassign-0001",
        "expected_version": int(row["version"]),
        "expected_assignee": str(row["assignee"]),
        "expected_head_event_ids": coord_db._typed_handoff_head_state_unlocked(
            conn, WORK_ID
        )["active_event_ids"],
        "_authority_capability": coord_db._OPERATOR_REASSIGNMENT_CAPABILITY,
    }
    fields.update(overrides)
    return fields


def _events(conn, kind: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM events WHERE work_id=? AND kind=? ORDER BY event_id",
            (WORK_ID, kind),
        )
    ]


def test_operator_reassignment_is_a_system_handoff_with_an_exact_receipt(
    board: Path,
) -> None:
    conn = connect(board)
    try:
        prior_id = coord_db.post_event(
            conn,
            kind="handoff",
            actor="codex",
            session_id="codex:prior-assignment-head",
            to_selector="actor:claude",
            work_id=WORK_ID,
            payload_json=json.dumps({"schema_version": 1, "task": "prior head"}),
            idempotency_key="fixture-prior-head",
        )
        request = _request(conn, expected_head_event_ids=[prior_id])
        receipt = coord_db.post_operator_reassignment(conn, **request)

        assert receipt["writer_contract"] == "operator_reassignment_receipt.v1"
        assert receipt["authority_channel"] == "authenticated_resident_controller"
        assert receipt["replayed"] is False
        assert receipt["active"] is True
        assert receipt["superseded_event_ids"] == [prior_id]
        assert receipt["released_claim_ids"] == []
        assert len(receipt["request_sha256"]) == 64

        work = dict(
            conn.execute(
                "SELECT * FROM work_items WHERE work_id=?", (WORK_ID,)
            ).fetchone()
        )
        assert work["assignee"] == "codex"
        assert work["assigned_by"] == "operator"
        assert work["intent_state"] == "queued"
        assert work["version"] == request["expected_version"] + 1

        event = _events(conn, "handoff")[-1]
        assert event["event_id"] == receipt["event_id"]
        assert event["actor"] == "operator"
        assert event["session_id"] is None
        assert event["to_selector"] == "actor:codex"
        assert event["trust"] == "system"
        payload = json.loads(event["payload_json"])
        assert payload["writer_contract"] == "operator_reassignment.v1"
        assert payload["authority_channel"] == coord_db.OPERATOR_AUTHORITY_CHANNEL
        assert payload["operation_request_sha256"] == receipt["request_sha256"]
        assert payload["operation_receipt"]["writer_contract"] == receipt[
            "writer_contract"
        ]
        assert payload["operation_receipt"]["request_sha256"] == receipt[
            "request_sha256"
        ]
        assert coord_db._typed_handoff_head_state_unlocked(conn, WORK_ID)[
            "active_event_ids"
        ] == [receipt["event_id"]]

        replay = coord_db.post_operator_reassignment(conn, **request)
        assert replay["replayed"] is True
        assert replay["event_id"] == receipt["event_id"]
        assert len(_events(conn, "handoff")) == 2
    finally:
        conn.close()


def test_operation_id_collision_is_non_echoing_and_does_not_mutate(
    board: Path,
) -> None:
    conn = connect(board)
    try:
        request = _request(conn)
        receipt = coord_db.post_operator_reassignment(conn, **request)
        secret = "DO-NOT-ECHO-CAPSULE-TEXT"
        with pytest.raises(ValueError) as caught:
            coord_db.post_operator_reassignment(
                conn, **{**request, "why": secret}
            )
        assert secret not in str(caught.value)
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE idempotency_key LIKE 'operator-reassignment:%'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT version FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()[0] == request["expected_version"] + 1
        assert receipt["event_id"] > 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_version", 99, "version CAS failed"),
        ("expected_assignee", "codex", "must differ from expected_assignee"),
        ("expected_head_event_ids", [999], "assignment-head CAS failed"),
        ("_authority_capability", object(), "resident-controller capability"),
        ("release_held_claim", 1, "explicit boolean"),
    ],
)
def test_operator_reassignment_refuses_invalid_authority_or_cas(
    board: Path, field: str, value, message: str
) -> None:
    conn = connect(board)
    try:
        request = _request(conn, **{field: value})
        with pytest.raises(ValueError, match=message):
            coord_db.post_operator_reassignment(conn, **request)
        row = conn.execute(
            "SELECT assignee,assigned_by,version FROM work_items WHERE work_id=?",
            (WORK_ID,),
        ).fetchone()
        assert tuple(row) == ("claude", "claude", 0)
        assert _events(conn, "handoff") == []
    finally:
        conn.close()


def test_operator_reassignment_never_steals_a_live_claim(board: Path) -> None:
    conn = connect(board)
    try:
        claim_id = coord_db.claim_work(
            conn, CLAUDE_SESSION, WORK_ID, step="holder is still working", lease_s=600
        )
        request = _request(conn)
        with pytest.raises(ValueError, match="refuses a live held claim"):
            coord_db.post_operator_reassignment(conn, **request)
        assert conn.execute(
            "SELECT status FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()[0] == "running"

        with pytest.raises(ValueError, match="cannot release a live claim"):
            coord_db.post_operator_reassignment(
                conn, **{**request, "release_held_claim": True}
            )
        assert conn.execute(
            "SELECT status FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()[0] == "running"
        assert _events(conn, "handoff") == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    "barrier", ["terminal", "proof", "artifact", "run", "reserved_run"]
)
def test_operator_reassignment_refuses_completed_or_live_work(
    board: Path, barrier: str
) -> None:
    conn = connect(board)
    try:
        if barrier == "terminal":
            conn.execute(
                "UPDATE work_items SET intent_state='done' WHERE work_id=?", (WORK_ID,)
            )
            expected = "terminal or archived"
        elif barrier == "proof":
            root = coord_db.HARNESS_ROOT
            proof = root / "artifacts/operator-reassignment-proof.json"
            proof.parent.mkdir(parents=True, exist_ok=True)
            proof.write_text('{"done":true}\n', encoding="utf-8")
            # A proof "resolves" only once git's index carries it -- true of
            # every artifact type since 0.1.0, not only Markdown. Writing the
            # file alone no longer raises this barrier, so the fixture stages
            # it: the barrier under test is reassignment, not custody.
            subprocess.run(
                ["git", "add", "artifacts/operator-reassignment-proof.json"],
                cwd=root,
                check=True,
            )
            expected = "resolved completion proof"
        elif barrier == "artifact":
            coord_db.store_artifact(
                conn,
                work_id=WORK_ID,
                path="artifacts/derived.json",
                kind="evidence",
            )
            expected = "derived completion artifact"
        else:
            t = coord_db.db_now(conn)
            run_state = "reserved" if barrier == "reserved_run" else "live"
            conn.execute(
                "INSERT INTO runs(run_id,work_id,session_id,runner_kind,started_at,state)"
                " VALUES (?,?,?,?,?,?)",
                (f"run-{run_state}-1", WORK_ID, CLAUDE_SESSION, "local", t, run_state),
            )
            expected = "nonterminal run"
        conn.commit()
        request = _request(conn)
        with pytest.raises(ValueError, match=expected):
            coord_db.post_operator_reassignment(conn, **request)
        assert conn.execute(
            "SELECT assignee FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()[0] == "claude"
        assert _events(conn, "handoff") == []
    finally:
        conn.close()


def test_agent_handoff_rules_are_not_relaxed_by_operator_writer(board: Path) -> None:
    conn = connect(board)
    try:
        request = _request(conn)
        with pytest.raises(ValueError, match="expected_assignee must equal"):
            coord_db.post_existing_work_handoff(
                conn,
                work_id=WORK_ID,
                actor="codex",
                session_id="codex:not-the-assignee",
                owner_lane="claude",
                target_intent="queued",
                task=request["task"],
                why=request["why"],
                acceptance=request["acceptance"],
                refs=request["refs"],
                constraints=request["constraints"],
                operation_id="agent-handoff-still-strict-1",
                expected_version=request["expected_version"],
                expected_assignee="claude",
                expected_head_event_ids=[],
            )
    finally:
        conn.close()


def test_public_event_writer_cannot_squat_operator_reassignment_namespace(
    board: Path,
) -> None:
    conn = connect(board)
    try:
        with pytest.raises(ValueError, match="reserved operator reassignment"):
            coord_db.post_event(
                conn,
                kind="note",
                actor="claude",
                work_id=WORK_ID,
                idempotency_key="operator-reassignment:blocked-by-reservation",
            )
    finally:
        conn.close()
