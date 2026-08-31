"""The inbox has to answer "did anything arrive for me?" truthfully.

Two failures made that answer wrong, and both are invisible to a test that reads
a small queue or drains the whole of a large one:

  * a directed message and a board-wide broadcast were summed into one unread
    total, so the single event addressed to this actor arrived buried in forty
    that were addressed to nobody, and
  * the MCP reader -- the surface agents actually use -- took the oldest-first
    default and acknowledged that window, leaving the cursor a dozen calls
    behind the message under test.

Every test here therefore reads with a ``limit`` smaller than the backlog. That
is not incidental: at a limit large enough to drain the queue both defects
disappear, which is exactly why they survived.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.bootstrap import bootstrap_database  # noqa: E402
from coordharness.coord import coord_db, mcp_coord_server  # noqa: E402
from coordharness.coord.config import connect  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

BROADCASTS = 40
ACTOR = "claude"
SESSION = "claude:inbox-regression"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    """A board whose broadcast traffic dwarfs what was addressed to one actor.

    The proportion is the point. It is taken from a freshly seeded demo board,
    where forty of fifty events carry no recipient at all.
    """
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    conn = sqlite3.connect(db)
    now = time.time()
    for index in range(BROADCASTS):
        conn.execute(
            "INSERT INTO events(ts,kind,actor,to_selector,title)"
            " VALUES (?,'activity','codex',NULL,?)",
            (now + index, f"board activity {index}"),
        )
    # One message addressed to this actor, oldest, and one newest: the first is
    # reachable by an oldest-first read, the second only by a newest-first one.
    conn.execute(
        "INSERT INTO events(ts,kind,actor,to_selector,title)"
        " VALUES (?,'note','codex','actor:claude','early directed message')",
        (now - 1,),
    )
    conn.execute(
        "INSERT INTO events(ts,kind,actor,to_selector,title)"
        " VALUES (?,'note','codex','actor:claude','STOP: the denominator moved')",
        (now + BROADCASTS + 1,),
    )
    # Addressed to somebody else. It must count towards neither leg here.
    conn.execute(
        "INSERT INTO events(ts,kind,actor,to_selector,title)"
        " VALUES (?,'note','claude','actor:codex','not for this actor')",
        (now + BROADCASTS + 2,),
    )
    conn.commit()
    conn.close()
    return db


def _newest_directed(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(
            conn.execute(
                "SELECT MAX(event_id) FROM events WHERE to_selector='actor:claude'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _cursor(db: Path, session_id: str = SESSION) -> int:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT last_seen_event_id FROM inbox_cursors"
            " WHERE recipient=? AND session_id=?",
            (ACTOR, session_id),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Defect 1: a directed message must be distinguishable from a broadcast.
# --------------------------------------------------------------------------


def test_unread_counts_split_directed_from_broadcast(board: Path) -> None:
    conn = connect(board)
    try:
        counts = coord_db.unread_inbox_counts(conn, recipient_actor=ACTOR)

        assert counts["directed"] == 2
        assert counts["broadcast"] == BROADCASTS
        # The sum is the contract the previous single number satisfied, so a
        # caller that only knew ``unread_total`` keeps reading the same figure.
        assert counts["directed"] + counts["broadcast"] == counts["total"]
        assert coord_db.unread_inbox_count(conn, recipient_actor=ACTOR) == counts["total"]
    finally:
        conn.close()


def test_unread_counts_ignore_messages_addressed_to_another_actor(board: Path) -> None:
    """A message to codex is neither this actor's directed mail nor a broadcast."""
    conn = connect(board)
    try:
        counts = coord_db.unread_inbox_counts(conn, recipient_actor=ACTOR)
        total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        assert counts["total"] == total_events - 1
    finally:
        conn.close()


def test_each_message_says_whether_it_was_addressed_here(board: Path) -> None:
    conn = connect(board)
    try:
        window = coord_db.read_inbox(
            conn, recipient_actor=ACTOR, limit=5, newest_first=True
        )

        by_id = {m["event_id"]: m for m in window}
        interrupt = by_id[_newest_directed(board)]
        assert interrupt["directed"] is True
        assert interrupt["to_selector"] == "actor:claude"
        broadcasts = [m for m in window if m["to_selector"] is None]
        assert broadcasts, "the window should still contain broadcasts"
        assert all(m["directed"] is False for m in broadcasts)
    finally:
        conn.close()


def test_directed_only_is_opt_in_and_leaves_the_default_set_alone(board: Path) -> None:
    """Broadcasts are legitimate; the default reading must keep returning them."""
    conn = connect(board)
    try:
        # A limit above the whole backlog, so this compares SETS rather than
        # which end a window truncates.
        default = coord_db.read_inbox(conn, recipient_actor=ACTOR, limit=500)
        directed = coord_db.read_inbox(
            conn, recipient_actor=ACTOR, limit=500, directed_only=True
        )

        counts = coord_db.unread_inbox_counts(conn, recipient_actor=ACTOR)
        assert len(default) == counts["total"]
        assert {m["event_id"] for m in directed} < {m["event_id"] for m in default}
        assert len(directed) == counts["directed"]
        assert all(m["directed"] is True for m in directed)
    finally:
        conn.close()


def test_directed_only_still_truncates_from_the_newest_end(board: Path) -> None:
    conn = connect(board)
    try:
        window = coord_db.read_inbox(
            conn, recipient_actor=ACTOR, limit=1, newest_first=True,
            directed_only=True,
        )

        assert [m["event_id"] for m in window] == [_newest_directed(board)]
    finally:
        conn.close()


def test_cli_inbox_reports_the_split(board: Path, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(tmp_path),
        "COORD_HOME": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": SESSION,
    }
    for key in (
        "COORD_ACTOR", "COORD_SESSION_ID", "CODEX_SESSION_ID",
        "CODEX_THREAD_ID", "CODEX_WORKTREE_ID", "CODEX_CONVERSATION_ID",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-m", "coordharness.coord.cli", "--db", str(board),
         "inbox", "--limit", "5"],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["directed_unread"] == 2
    assert payload["broadcast_unread"] == BROADCASTS
    assert payload["directed_unread"] + payload["broadcast_unread"] == payload["unread_total"]
    # Five of forty-two unread shown, and the one addressed here is among them
    # only because the reading is newest-first.
    assert payload["count"] == 5
    assert payload["not_shown"] == payload["unread_total"] - 5
    shown = {m["id"]: m for m in payload["messages"]}
    assert shown[_newest_directed(board)]["directed"] is True


def test_cli_directed_scope_does_not_count_broadcasts_as_withheld(
    board: Path, tmp_path: Path
) -> None:
    """``not_shown`` under ``--directed`` must describe the queue being read."""
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(tmp_path),
        "COORD_HOME": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": SESSION,
    }
    for key in (
        "COORD_ACTOR", "COORD_SESSION_ID", "CODEX_SESSION_ID",
        "CODEX_THREAD_ID", "CODEX_WORKTREE_ID", "CODEX_CONVERSATION_ID",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-m", "coordharness.coord.cli", "--db", str(board),
         "inbox", "--limit", "1", "--directed"],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["scope"] == "directed"
    assert payload["count"] == 1
    assert payload["not_shown"] == payload["directed_unread"] - 1
    # The broadcast backlog is still reported, so choosing the narrow reading
    # never hides how much of the board went unread.
    assert payload["broadcast_unread"] == BROADCASTS
    assert payload["unread_total"] == BROADCASTS + 2


# --------------------------------------------------------------------------
# Defect 2: the MCP reader must acknowledge the end of the queue it showed.
# --------------------------------------------------------------------------


def test_mcp_inbox_shows_the_newest_arrival_at_a_limit_below_the_backlog(
    board: Path,
) -> None:
    result = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, db_path=str(board)
    )

    assert result["count"] == 5
    assert _newest_directed(board) in {m["event_id"] for m in result["messages"]}
    assert result["order"] == "newest_first"


def test_mcp_inbox_acks_the_newest_event_in_the_window_it_returned(
    board: Path,
) -> None:
    """The whole defect: the acknowledgement must not lag what was shown."""
    result = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, advance=True, db_path=str(board)
    )

    shown = [m["event_id"] for m in result["messages"]]
    assert len(shown) == 5, "the limit must stay below the backlog for this to bite"
    assert result["acked_through"] == max(shown)
    assert _cursor(board) == max(shown)
    # And a second read does not hand back what the first already acknowledged.
    again = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, db_path=str(board)
    )
    assert not ({m["event_id"] for m in again["messages"]} & set(shown))


def test_mcp_inbox_ack_never_lands_behind_a_message_it_showed(board: Path) -> None:
    """Holds in both readings, which ``msgs[-1]`` does not.

    Under queue order the last element is the newest shown; flip the reading and
    the same expression names the oldest. A test that only exercised one order
    would pass against the broken form.
    """
    for backlog in (False, True):
        conn = sqlite3.connect(board)
        conn.execute("DELETE FROM inbox_cursors")
        conn.commit()
        conn.close()

        result = mcp_coord_server._tool_inbox(
            actor=ACTOR, session_id=SESSION, limit=5, advance=True,
            backlog=backlog, db_path=str(board),
        )

        cursor = _cursor(board)
        assert cursor >= max(m["event_id"] for m in result["messages"]), (
            f"cursor {cursor} is behind a message shown under backlog={backlog}"
        )


def test_mcp_backlog_mode_still_drains_in_queue_order(board: Path) -> None:
    """The oldest-first reading stays available and stays incremental."""
    first = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, advance=True, backlog=True,
        db_path=str(board),
    )
    second = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, advance=True, backlog=True,
        db_path=str(board),
    )

    first_ids = [m["event_id"] for m in first["messages"]]
    second_ids = [m["event_id"] for m in second["messages"]]
    assert first_ids == sorted(first_ids)
    assert min(second_ids) > max(first_ids)
    assert first["order"] == "backlog"
    # Draining from the old end must not silently sweep the rest of the board.
    assert first["skipped_by_ack"] == 0
    assert _cursor(board) == max(second_ids)


def test_mcp_inbox_discloses_what_the_acknowledgement_swept_past(board: Path) -> None:
    """The cursor is a watermark, so acking the newest also marks older seen.

    That is the honest cost of reading newest-first, and it has to be reported
    rather than discovered later as a queue that emptied itself.
    """
    result = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, advance=True, db_path=str(board)
    )

    assert result["skipped_by_ack"] == result["unread_total"] - result["count"]
    assert result["skipped_by_ack"] > 0


def test_mcp_inbox_reports_the_split_and_reads_before_it_acknowledges(
    board: Path,
) -> None:
    result = mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, advance=True, db_path=str(board)
    )

    assert result["directed_unread"] == 2
    assert result["broadcast_unread"] == BROADCASTS
    assert result["directed_unread"] + result["broadcast_unread"] == result["unread_total"]
    # Counted against the queue the caller was handed, not the one its own read
    # had already drained -- otherwise the numbers describe the wrong instant.
    assert result["not_shown"] == result["unread_total"] - result["count"]


def test_mcp_inbox_without_advance_leaves_the_cursor_alone(board: Path) -> None:
    mcp_coord_server._tool_inbox(
        actor=ACTOR, session_id=SESSION, limit=5, db_path=str(board)
    )

    assert _cursor(board) == 0
