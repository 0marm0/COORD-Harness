"""Tests for coordharness.coord.panel_quorum.

Standalone by construction: the module under test has no coord.db dependency in
its core, so the predicate tests need no database at all. The one adapter test
builds the events columns it reads in an in-memory SQLite.
"""

import pytest

from coordharness.coord.panel_quorum import (
    PanelContractError,
    classify_panel_status,
    classify_panel_status_for_work,
    normalize_quorum,
    quorum_from_acceptance,
)


def verdict(event_id, actor, session_id, v="PASS", kind="audit_verdict"):
    return {
        "event_id": event_id,
        "kind": kind,
        "actor": actor,
        "session_id": session_id,
        "verdict": v,
    }


_core_classify_panel_status = classify_panel_status


def classify_panel_status(
    events,
    *,
    author_lane,
    quorum,
    barrier_event_id=0,
    author_session_id="claude:author",
):
    """Exercise the fail-closed core with explicit safe test identities."""
    return _core_classify_panel_status(
        events,
        author_lane=author_lane,
        quorum=quorum,
        barrier_event_id=barrier_event_id,
        author_session_id=author_session_id,
    )


# --- quorum declaration -------------------------------------------------


def test_no_declaration_is_not_a_panel():
    assert normalize_quorum(None) is None
    assert normalize_quorum("") is None


def test_quorum_of_one_is_refused_not_degraded():
    with pytest.raises(PanelContractError) as exc:
        normalize_quorum(1)
    assert "ordinary T0 audit" in str(exc.value)


def test_quorum_rejects_bool_and_junk():
    with pytest.raises(PanelContractError):
        normalize_quorum(True)
    with pytest.raises(PanelContractError):
        normalize_quorum("three")
    with pytest.raises(PanelContractError):
        normalize_quorum(2.9)


def test_panel_requires_author_and_barrier_inputs():
    with pytest.raises(TypeError):
        _core_classify_panel_status([], author_lane="claude", quorum=2)


@pytest.mark.parametrize(
    ("author_lane", "author_session_id"),
    [
        ("claude", ""),
        ("", ""),
        ("reviewer", "reviewer:author"),
        ("claude", "codex:wrong"),
    ],
)
def test_panel_rejects_invalid_author_identity(author_lane, author_session_id):
    with pytest.raises(PanelContractError):
        _core_classify_panel_status(
            [],
            author_lane=author_lane,
            quorum=2,
            barrier_event_id=0,
            author_session_id=author_session_id,
        )


@pytest.mark.parametrize("barrier", [True, 1.5, "1", -1])
def test_panel_rejects_invalid_barrier(barrier):
    with pytest.raises(PanelContractError):
        _core_classify_panel_status(
            [],
            author_lane="claude",
            quorum=2,
            barrier_event_id=barrier,
            author_session_id="claude:author",
        )


def test_quorum_ceiling():
    assert normalize_quorum(8) == 8
    with pytest.raises(PanelContractError):
        normalize_quorum(9)


def test_quorum_from_acceptance_mapping_and_json():
    assert quorum_from_acceptance({"panel_quorum": 3}) == 3
    assert quorum_from_acceptance('{"panel_quorum": 2}') == 2
    assert quorum_from_acceptance('{"criteria": "x"}') is None
    assert quorum_from_acceptance("not json at all") is None
    assert quorum_from_acceptance(None) is None


# --- the core predicate -------------------------------------------------


def test_unanimous_panel_with_opposite_lane_passes():
    events = [
        verdict(10, "codex", "codex:review-a"),
        verdict(11, "codex", "codex:review-b"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["passed"] is True
    assert out["reason"] == "panel_unanimous"
    assert out["assessor_count"] == 2
    assert out["lane_independent_count"] == 1  # two-lane harness ceiling


def test_lane_independent_count_never_exceeds_one_opposite_lane():
    """The honest-limit guarantee: 3 assessors is not 3 independent lanes."""
    events = [
        verdict(10, "codex", "codex:a"),
        verdict(11, "codex", "codex:b"),
        verdict(12, "codex", "codex:c"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=3)
    assert out["passed"] is True
    assert out["assessor_count"] == 3
    assert out["lane_independent_count"] == 1


def test_one_dissent_fails_the_panel_even_when_outnumbered():
    events = [
        verdict(10, "codex", "codex:a"),
        verdict(11, "codex", "codex:b"),
        verdict(12, "codex", "codex:c", v="BLOCKED"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["passed"] is False
    assert out["reason"] == "panel_dissent"
    assert [d["session_id"] for d in out["dissenters"]] == ["codex:c"]


def test_dissent_outranks_unmet_quorum():
    """A BLOCKED is a finding now, not 'not enough reviewers yet'."""
    events = [verdict(10, "codex", "codex:a", v="FLAG")]
    out = classify_panel_status(events, author_lane="claude", quorum=3)
    assert out["reason"] == "panel_dissent"


def test_unmet_quorum():
    events = [verdict(10, "codex", "codex:a")]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["passed"] is False
    assert out["reason"] == "panel_quorum_unmet"
    assert out["assessor_count"] == 1


def test_same_assessor_twice_counts_once():
    events = [
        verdict(10, "codex", "codex:a"),
        verdict(11, "codex", "codex:a"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["assessor_count"] == 1
    assert out["reason"] == "panel_quorum_unmet"


def test_latest_verdict_per_assessor_wins():
    events = [
        verdict(10, "codex", "codex:a", v="BLOCKED"),
        verdict(20, "codex", "codex:a", v="PASS"),
        verdict(21, "codex", "codex:b", v="PASS"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["passed"] is True


def test_verdicts_at_or_before_the_barrier_are_dropped():
    events = [
        verdict(10, "codex", "codex:a"),
        verdict(15, "codex", "codex:b"),
        verdict(16, "codex", "codex:c"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2, barrier_event_id=15)
    assert out["assessor_count"] == 1
    assert out["reason"] == "panel_quorum_unmet"


def test_author_session_self_verdict_excluded():
    events = [
        verdict(10, "claude", "claude:author"),
        verdict(11, "codex", "codex:a"),
    ]
    out = classify_panel_status(
        events, author_lane="claude", quorum=2, author_session_id="claude:author"
    )
    assert out["assessor_count"] == 1
    assert out["reason"] == "panel_quorum_unmet"


def test_same_lane_different_session_counts_but_is_not_lane_independent():
    events = [
        verdict(10, "claude", "claude:other-session"),
        verdict(11, "claude", "claude:third-session"),
    ]
    out = classify_panel_status(
        events, author_lane="claude", quorum=2, author_session_id="claude:author"
    )
    assert out["assessor_count"] == 2
    assert out["lane_independent_count"] == 0
    assert out["passed"] is False
    assert out["reason"] == "panel_lacks_opposite_lane"


def test_non_verdict_events_and_junk_verdicts_ignored():
    events = [
        verdict(10, "codex", "codex:a", kind="note"),
        verdict(11, "codex", "codex:b", v="LGTM"),
        verdict(12, "codex", "codex:c"),
        {"event_id": "bad", "kind": "audit_verdict", "actor": "codex",
         "session_id": "codex:d", "verdict": "PASS"},
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["assessor_count"] == 1


def test_actor_casefolding():
    events = [
        verdict(10, "CODEX", "Codex:A"),
        verdict(11, "codex", "codex:a"),
    ]
    out = classify_panel_status(events, author_lane="claude", quorum=2)
    assert out["assessor_count"] == 1


def test_zero_quorum_is_not_a_panel_not_a_free_pass():
    out = classify_panel_status([], author_lane="claude", quorum=None)
    assert out["passed"] is False
    assert out["reason"] == "not_a_panel"


def test_empty_events_never_passes():
    out = classify_panel_status([], author_lane="claude", quorum=2)
    assert out["passed"] is False


# --- the coord.db adapter -----------------------------------------------


def test_adapter_over_real_sqlite():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events (event_id INTEGER PRIMARY KEY, work_id TEXT, kind TEXT,"
        " actor TEXT, session_id TEXT, verdict TEXT)"
    )
    conn.executemany(
        "INSERT INTO events(event_id, work_id, kind, actor, session_id, verdict)"
        " VALUES (?,?,?,?,?,?)",
        [
            (10, "W-1", "audit_verdict", "codex", "codex:a", "PASS"),
            (11, "W-1", "audit_verdict", "codex", "codex:b", "PASS"),
            (12, "W-2", "audit_verdict", "codex", "codex:c", "BLOCKED"),
            (13, "W-1", "note", "codex", "codex:d", None),
        ],
    )
    out = classify_panel_status_for_work(
        conn, "W-1", author_lane="claude", quorum=2, barrier_event_id=0,
        author_session_id="claude:author"
    )
    assert out["work_id"] == "W-1"
    assert out["passed"] is True
    assert out["assessor_count"] == 2
    conn.close()


def test_adapter_against_the_shipped_schema():
    """The regression guard the hand-built table above cannot be.

    test_adapter_over_real_sqlite creates its own events table, so it would keep
    passing if schema.sql renamed or dropped session_id -- the exact column this
    module needs and review_integrity._own_row_audit_verdict_events does not
    select. This one builds the database from the shipped schema instead.
    """
    import pathlib
    import sqlite3
    import time

    import coordharness.coord as coord_pkg

    schema = (pathlib.Path(coord_pkg.__file__).parent / "schema.sql").read_text()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    now = time.time()
    conn.executemany(
        "INSERT INTO events(event_id, ts, kind, actor, session_id, work_id, verdict)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            (1, now, "audit_verdict", "codex", "codex:a", "W-REAL", "PASS"),
            (2, now, "audit_verdict", "codex", "codex:b", "W-REAL", "PASS"),
            (3, now, "note", "codex", "codex:c", "W-REAL", None),
        ],
    )
    out = classify_panel_status_for_work(
        conn, "W-REAL", author_lane="claude", quorum=2, barrier_event_id=0,
        author_session_id="claude:author"
    )
    assert out["passed"] is True
    assert out["assessor_count"] == 2
    assert out["lane_independent_count"] == 1
    conn.close()
