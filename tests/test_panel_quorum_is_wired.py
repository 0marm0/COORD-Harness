"""The PANEL tier is reachable: a declared quorum actually gates completion.

`panel_quorum.py` was written, documented and covered by thirty tests, and its
own docstring said `NOT WIRED: nothing here is called by the close gate`. The
single-verdict contract meant no row could ask for a second opinion — the one
thing a two-agent harness exists to make possible.

These tests are the wiring, asserted end to end through `completion_review_state`.
"""
from __future__ import annotations

import json

import pytest

from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.bootstrap import bootstrap_database


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    c = connect(db)
    try:
        yield c
    finally:
        c.close()


def _panel_row(conn, work_id: str, quorum: int | None):
    acceptance = {"acceptance": ["ship it"]}
    if quorum is not None:
        acceptance["panel_quorum"] = quorum
    coord_db.upsert_work(
        conn, work_id, title=work_id, display=work_id, module="ops",
        assignee="claude", intent_state="running", tier="T0",
        done_signal=f"artifacts/{work_id}.json",
        acceptance_json=json.dumps(acceptance),
    )
    # The panel identifies its author by (lane, session), so the row needs a
    # real claim -- exactly as a row being completed always does.
    coord_db.register_session(conn, "claude:author", "claude")
    coord_db.claim_work(conn, "claude:author", work_id, step="authoring")
    conn.commit()


def _verdict(conn, work_id: str, actor: str, session_id: str, verdict: str = "PASS"):
    conn.execute(
        "INSERT INTO events(ts, kind, actor, session_id, work_id, verdict, title)"
        " VALUES (?,?,?,?,?,?,?)",
        (coord_db.db_now(conn), "audit_verdict", actor, session_id, work_id, verdict, "v"),
    )
    conn.commit()


def test_a_row_with_no_declared_quorum_is_untouched(conn):
    """The safety of wiring this: a panel is DECLARED, never inferred. No row on
    any live board carries a quorum, so nothing changes for work that does not
    opt in."""
    _panel_row(conn, "NO-PANEL", None)
    state = coord_db.completion_review_state(conn, "NO-PANEL")
    assert state["panel"] is None


def test_a_declared_quorum_is_unmet_until_enough_independent_assessors_agree(conn):
    _panel_row(conn, "PANEL-3", 3)

    state = coord_db.completion_review_state(conn, "PANEL-3")
    assert state["panel"] is not None, "the panel must be reachable at all"
    assert state["needs_review"] is True

    _verdict(conn, "PANEL-3", "codex", "codex:one")
    assert coord_db.completion_review_state(conn, "PANEL-3")["needs_review"] is True

    _verdict(conn, "PANEL-3", "codex", "codex:two")
    _verdict(conn, "PANEL-3", "codex", "codex:three")
    state = coord_db.completion_review_state(conn, "PANEL-3")
    assert state["panel"]["assessor_count"] >= 3


def test_one_dissent_fails_the_panel_however_many_agree(conn):
    """Unanimity, not majority: a panel that discards its minority has thrown
    away the signal it was convened to find."""
    _panel_row(conn, "PANEL-DISSENT", 3)
    _verdict(conn, "PANEL-DISSENT", "codex", "codex:one")
    _verdict(conn, "PANEL-DISSENT", "codex", "codex:two")
    _verdict(conn, "PANEL-DISSENT", "codex", "codex:three", verdict="FLAG")

    state = coord_db.completion_review_state(conn, "PANEL-DISSENT")
    assert state["panel"]["passed"] is False
    assert state["needs_review"] is True, "a dissenting panel must still gate"


def test_lane_independence_is_reported_separately_from_assessor_count(conn):
    """A two-lane harness cannot produce three lane-independent assessments, so
    a caller must never be able to read '3 assessors' as '3 lanes'."""
    _panel_row(conn, "PANEL-LANES", 2)
    _verdict(conn, "PANEL-LANES", "codex", "codex:one")
    _verdict(conn, "PANEL-LANES", "codex", "codex:two")

    panel = coord_db.completion_review_state(conn, "PANEL-LANES")["panel"]
    assert panel["assessor_count"] == 2
    assert panel["lane_independent_count"] <= 1
