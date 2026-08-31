from __future__ import annotations

import json
import sqlite3

import pytest

from coordharness.lints.stall_detector import (
    detect_stall,
    evaluate_labeled_examples,
    scan_coord_db,
)


# --- pure detect_stall() -------------------------------------------------


def test_waiting_language_without_managed_job_flags_stall() -> None:
    verdict = detect_stall(
        "I spawned the subagent and will wait while it keeps working.",
        tool_uses=["Task"],
        duration_seconds=18,
        has_live_child=True,
    )

    assert verdict.is_stall
    assert "waiting_language_without_managed_job" in verdict.reasons
    assert verdict.should_auto_nudge is False


def test_spawn_with_low_followup_tool_count_flags_stall() -> None:
    verdict = detect_stall(
        "Agent is running; I will report back when it returns.",
        tool_uses=["spawn_agent"],
    )

    assert verdict.is_stall
    assert "spawned_agent_with_low_followup_tool_count" in verdict.reasons


def test_short_parent_with_live_child_flags_stall() -> None:
    verdict = detect_stall(
        "The workflow is in progress.",
        duration_seconds=12,
        has_live_child=True,
    )

    assert verdict.is_stall
    assert "short_parent_turn_with_live_child" in verdict.reasons


def test_managed_job_evidence_exempts_tracked_background_job() -> None:
    verdict = detect_stall(
        "The tracked background job is still running; see its sidecar job progress file.",
        tool_uses=["exec_command"],
        duration_seconds=30,
        has_live_child=True,
    )

    assert verdict.is_stall is False
    assert verdict.reasons == ()


def test_complete_final_message_is_not_stall() -> None:
    verdict = detect_stall(
        "Implemented the fix, wrote the report, and reran the checks with no new errors.",
        tool_uses=["exec_command", "apply_patch", "exec_command"],
    )

    assert verdict.is_stall is False


def test_auto_nudge_stays_disabled_unless_caller_opts_in() -> None:
    verdict = detect_stall(
        "Agent is running; I will report back when it returns.",
        tool_uses=["spawn_agent"],
    )
    assert verdict.is_stall
    assert verdict.auto_nudge_enabled is False
    assert verdict.should_auto_nudge is False

    opted_in = detect_stall(
        "Agent is running; I will report back when it returns.",
        tool_uses=["spawn_agent"],
        auto_nudge_enabled=True,
    )
    assert opted_in.should_auto_nudge is True


def test_waiting_language_pattern_does_not_match_benign_completion_prose() -> None:
    """Regression for the `.*`-spanning pattern that matched a subject word
    anywhere earlier in the message followed eventually by running/working/
    in progress, even across an intervening completion verb. MEASURED: with
    the old pattern all 5 of these benign, already-finished messages
    false-positived (via _has_waiting_language); with the fix, none do.
    """

    benign_completion_messages = [
        "The agent finished running the full regression suite; every test passed.",
        "Task completed: the workflow no longer needs monitoring and nothing is running elsewhere.",
        "The subagent's summary was written after the workflow finished; no processes remain in progress elsewhere.",
        "Agent handoff document explains that once a task begins working normally, the run closes automatically.",
        "The workflow you flagged is well documented; every task in the working set already ran to completion.",
    ]

    for message in benign_completion_messages:
        verdict = detect_stall(message)
        assert verdict.is_stall is False, f"false positive on: {message!r}"
        assert verdict.reasons == ()


def test_waiting_language_pattern_still_matches_genuine_ongoing_state() -> None:
    # Contiguous subject + copula + state word must still fire.
    assert detect_stall("Agent is running; I will report back when it returns.").is_stall
    assert detect_stall("The workflow is in progress.").is_stall
    assert detect_stall("The task is still working through the queue.").is_stall


def test_labeled_fixture_precision_and_recall() -> None:
    examples = [
        {
            "final_message": "I spawned the agent and will wait for it to finish.",
            "tool_uses": ["Task"],
            "duration_seconds": 20,
            "has_live_child": True,
            "is_stall": True,
        },
        {
            "final_message": "The subagent is still running, I will check back.",
            "tool_uses": ["Agent"],
            "is_stall": True,
        },
        {
            "final_message": "Workflow is in progress.",
            "duration_seconds": 15,
            "has_live_child": True,
            "is_stall": True,
        },
        {
            "final_message": "Waiting on the spawned task; nothing else to report until it returns.",
            "tool_uses": ["spawn_task"],
            "is_stall": True,
        },
        {
            "final_message": "Tracked background job still running; see its job progress sidecar.",
            "duration_seconds": 40,
            "has_live_child": True,
            "is_stall": False,
        },
        {
            "final_message": "The job is running with a heartbeat and a done_signal artifact.",
            "tool_uses": ["exec_command"],
            "is_stall": False,
        },
        {
            "final_message": "done_signal exists and the focused tests passed.",
            "tool_uses": ["exec_command", "exec_command", "apply_patch"],
            "is_stall": False,
        },
        {
            "final_message": "Blocked on missing API credentials; no child process remains live.",
            "tool_uses": ["exec_command", "exec_command"],
            "is_stall": False,
        },
        {
            "final_message": "I launched the tracked background job and will monitor its job progress sidecar.",
            "tool_uses": ["exec_command"],
            "has_live_child": True,
            "is_stall": False,
        },
        {
            "final_message": "Report written; findings are unchanged and documented.",
            "tool_uses": ["exec_command", "exec_command"],
            "is_stall": False,
        },
    ]

    metrics = evaluate_labeled_examples(examples)

    assert metrics["examples"] == 10
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0


# --- coord.db scanner ------------------------------------------------------

_RUNS_DDL = """
CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,
  work_id           TEXT,
  session_id        TEXT,
  parent_session_id TEXT,
  runner_kind       TEXT NOT NULL DEFAULT 'test',
  started_at        REAL NOT NULL,
  finished_at       REAL,
  state             TEXT NOT NULL DEFAULT 'live'
);
"""

_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  work_id         TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  thread_id       TEXT,
  session_id      TEXT,
  seq             INTEGER NOT NULL,
  category        TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  content_json    TEXT NOT NULL DEFAULT '{}',
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT,
  created_at      REAL NOT NULL
);
"""


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_RUNS_DDL + _RUN_EVENTS_DDL)
    conn.commit()
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    work_id: str,
    session_id: str,
    parent_session_id: str = "",
    started_at: float = 0.0,
    finished_at: float | None = None,
    state: str = "live",
) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, work_id, session_id, parent_session_id, started_at,"
        " finished_at, state) VALUES (?,?,?,?,?,?,?)",
        (run_id, work_id, session_id, parent_session_id, started_at, finished_at, state),
    )
    conn.commit()


def _insert_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    work_id: str,
    seq: int,
    category: str,
    event_type: str,
    content: dict | str,
) -> None:
    conn.execute(
        "INSERT INTO run_events (work_id, run_id, seq, category, event_type, content_json,"
        " created_at) VALUES (?,?,?,?,?,?,0)",
        (work_id, run_id, seq, category, event_type, json.dumps(content)),
    )
    conn.commit()


def test_scan_flags_finished_parent_with_live_untracked_child(tmp_path) -> None:
    db_path = tmp_path / "coord.db"
    conn = _make_db(str(db_path))

    _insert_run(
        conn,
        run_id="run-parent",
        work_id="WORK-1",
        session_id="sess-parent",
        started_at=0.0,
        finished_at=10.0,
        state="done",
    )
    _insert_run(
        conn,
        run_id="run-child",
        work_id="WORK-1",
        session_id="sess-child",
        parent_session_id="sess-parent",
        started_at=5.0,
        finished_at=None,
        state="live",
    )
    _insert_event(
        conn,
        run_id="run-parent",
        work_id="WORK-1",
        seq=1,
        category="tool",
        event_type="tool.start",
        content={"tool_name": "spawn_agent"},
    )
    _insert_event(
        conn,
        run_id="run-parent",
        work_id="WORK-1",
        seq=2,
        category="message",
        event_type="message.final",
        content={"text": "Agent is running; I will check back when it returns."},
    )
    conn.close()

    candidates = scan_coord_db(str(db_path))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.run_id == "run-parent"
    assert candidate.work_id == "WORK-1"
    assert candidate.verdict.is_stall
    assert candidate.verdict.should_auto_nudge is False


def test_scan_does_not_flag_run_with_managed_job_evidence(tmp_path) -> None:
    db_path = tmp_path / "coord.db"
    conn = _make_db(str(db_path))

    _insert_run(
        conn,
        run_id="run-parent",
        work_id="WORK-2",
        session_id="sess-parent-2",
        started_at=0.0,
        finished_at=10.0,
        state="done",
    )
    _insert_run(
        conn,
        run_id="run-child",
        work_id="WORK-2",
        session_id="sess-child-2",
        parent_session_id="sess-parent-2",
        started_at=5.0,
        finished_at=None,
        state="live",
    )
    _insert_event(
        conn,
        run_id="run-parent",
        work_id="WORK-2",
        seq=1,
        category="message",
        event_type="message.final",
        content={
            "text": (
                "The tracked background job is still running with a heartbeat; "
                "see its job progress sidecar and done_signal."
            )
        },
    )
    conn.close()

    candidates = scan_coord_db(str(db_path))

    assert candidates == []


def test_scan_ignores_finished_run_with_no_live_children(tmp_path) -> None:
    db_path = tmp_path / "coord.db"
    conn = _make_db(str(db_path))

    _insert_run(
        conn,
        run_id="run-solo",
        work_id="WORK-3",
        session_id="sess-solo",
        started_at=0.0,
        finished_at=10.0,
        state="done",
    )
    _insert_event(
        conn,
        run_id="run-solo",
        work_id="WORK-3",
        seq=1,
        category="message",
        event_type="message.final",
        content={"text": "Agent is running; I will check back when it returns."},
    )
    conn.close()

    candidates = scan_coord_db(str(db_path))

    assert candidates == []


def test_scan_ignores_still_live_runs_regardless_of_message(tmp_path) -> None:
    db_path = tmp_path / "coord.db"
    conn = _make_db(str(db_path))

    _insert_run(
        conn,
        run_id="run-live",
        work_id="WORK-4",
        session_id="sess-live",
        started_at=0.0,
        finished_at=None,
        state="live",
    )
    _insert_run(
        conn,
        run_id="run-child",
        work_id="WORK-4",
        session_id="sess-child-4",
        parent_session_id="sess-live",
        started_at=1.0,
        finished_at=None,
        state="live",
    )
    conn.close()

    candidates = scan_coord_db(str(db_path))

    assert candidates == []


@pytest.mark.parametrize("child_state", ["running", "waiting", "reserved"])
def test_scan_flags_done_parent_with_nonlive_nonterminal_child(tmp_path, child_state) -> None:
    """Regression: coord_db.py's own runs use state IN ('live','running',
    'waiting'), and roadmap_binding.py inserts 'reserved' -- all three are
    non-terminal (still-active) child states, not just 'live'. MEASURED under
    the old `state == "live"`-only check: a finished (done) parent with a
    'running'/'waiting'/'reserved' child produced 0 candidates -- the stall
    was missed.
    """

    db_path = tmp_path / "coord.db"
    conn = _make_db(str(db_path))

    _insert_run(
        conn,
        run_id="run-parent",
        work_id="WORK-6",
        session_id="sess-parent-6",
        started_at=0.0,
        finished_at=10.0,
        state="done",
    )
    _insert_run(
        conn,
        run_id="run-child",
        work_id="WORK-6",
        session_id="sess-child-6",
        parent_session_id="sess-parent-6",
        started_at=5.0,
        finished_at=None,
        state=child_state,
    )
    _insert_event(
        conn,
        run_id="run-parent",
        work_id="WORK-6",
        seq=1,
        category="message",
        event_type="message.final",
        content={"text": "Agent is running; I will report back when it returns."},
    )
    conn.close()

    candidates = scan_coord_db(str(db_path))

    assert len(candidates) == 1
    assert candidates[0].run_id == "run-parent"


@pytest.mark.parametrize("parent_state", ["running", "waiting", "reserved"])
def test_scan_does_not_flag_still_active_parent_as_finished(tmp_path, parent_state) -> None:
    """Regression: the old `if run.state == "live": continue` skip let a
    parent in any other non-terminal state ('running'/'waiting'/'reserved')
    fall through and be treated as finished. MEASURED under the old code: a
    parent that never actually finished (state='running'/'waiting'/'reserved')
    with a live child produced 1 (false-positive) candidate per run.
    """

    db_path = tmp_path / "coord.db"
    conn = _make_db(str(db_path))

    _insert_run(
        conn,
        run_id="run-parent",
        work_id="WORK-7",
        session_id="sess-parent-7",
        started_at=0.0,
        finished_at=None,
        state=parent_state,
    )
    _insert_run(
        conn,
        run_id="run-child",
        work_id="WORK-7",
        session_id="sess-child-7",
        parent_session_id="sess-parent-7",
        started_at=5.0,
        finished_at=None,
        state="live",
    )
    _insert_event(
        conn,
        run_id="run-parent",
        work_id="WORK-7",
        seq=1,
        category="message",
        event_type="message.final",
        content={"text": "Agent is running; I will report back when it returns."},
    )
    conn.close()

    candidates = scan_coord_db(str(db_path))

    assert candidates == []


def test_scan_handles_missing_run_events_table(tmp_path) -> None:
    db_path = tmp_path / "coord.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_RUNS_DDL)
    conn.commit()

    _insert_run(
        conn,
        run_id="run-parent",
        work_id="WORK-5",
        session_id="sess-parent-5",
        started_at=0.0,
        finished_at=125.0,
        state="done",
    )
    _insert_run(
        conn,
        run_id="run-child",
        work_id="WORK-5",
        session_id="sess-child-5",
        parent_session_id="sess-parent-5",
        started_at=5.0,
        finished_at=None,
        state="live",
    )
    conn.close()

    # No run_events table at all: no final message, no tool evidence, and the
    # duration (125s) is well over the short-turn window, so nothing should fire.
    candidates = scan_coord_db(str(db_path))

    assert candidates == []
