"""A `session:` selector was readable by everything and writable by nothing.

Every inbox read path already resolved ``session:<id>`` -- ``read_inbox``,
``unread_inbox_counts`` and ``inbox_recent`` all add it to the selector set, and
``inbox_cursors`` is keyed ``(recipient, session_id)`` so a per-session cursor
was already possible. No writer ever produced one. The note writer hardcoded the
opposite lane, so the narrowest address the system could express was
``actor:claude`` -- which, for a fleet of three Claude sessions, is a message
addressed to all of them and answered by none.

The two things that make the writer safe are asserted here alongside the
delivery itself:

  * the lane default is untouched, so a caller that never passes a session sees
    exactly the behaviour it saw before; and
  * a target that does not exist, or whose lease has lapsed, is refused. An
    unchecked selector would be accepted, stored, and read by nobody -- and the
    sender would spend the rest of the session believing the message landed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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

WORK_ID = "DEMO-CLA-FLEET-ROW"
FLEET = ("claude:alpha", "claude:beta")


# --------------------------------------------------------------------------
# In-process board
# --------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One row, two live Claude sessions, and one live Codex sender."""
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="a row two sessions are both near",
            assignee="claude",
            module="runtime",
            surface="job",
            tier="T1",
            done_signal="artifacts/fleet-row.json",
            acceptance_json='["the named session reads it"]',
            note="exercise session-addressed notes",
            intent_state="queued",
        )
        for session in (*FLEET, "codex:sender"):
            coord_db.register_session(conn, session, session.split(":", 1)[0])
    finally:
        conn.close()
    return database


def directed_titles(database: Path, *, actor: str, session_id: str) -> list[str]:
    result = mcp_coord_server._tool_inbox(
        actor=actor, session_id=session_id, limit=20, db_path=str(database)
    )
    return [
        str(message.get("title") or "")
        for message in result["messages"]
        if message.get("directed")
    ]


def test_a_session_addressed_note_reaches_only_that_session(board: Path) -> None:
    posted = mcp_coord_server._tool_note(
        work_id=WORK_ID,
        body="you are the one editing the retry path, not your peer",
        title="pen split",
        actor="codex",
        session_id="codex:sender",
        to_session_id="claude:beta",
        db_path=str(board),
    )

    assert posted["to_selector"] == "session:claude:beta"
    assert posted["target_session"] == "claude:beta"
    assert directed_titles(board, actor="claude", session_id="claude:beta") == [
        "pen split"
    ]
    # The sibling in the same lane is not a recipient. Under the old writer it
    # would have been, because the only address available was the lane.
    assert directed_titles(board, actor="claude", session_id="claude:alpha") == []


def test_the_lane_default_is_unchanged(board: Path) -> None:
    """A caller that passes no session must see exactly what it saw before."""
    posted = mcp_coord_server._tool_note(
        work_id=WORK_ID,
        body="board-wide context for the other lane",
        title="lane note",
        actor="codex",
        session_id="codex:sender",
        db_path=str(board),
    )

    assert posted["to_selector"] == "actor:claude"
    assert posted["target_lane"] == "claude"
    assert posted["target_session"] is None
    # Reaches every session in the lane, which is what a lane address means.
    for session in FLEET:
        assert directed_titles(board, actor="claude", session_id=session) == [
            "lane note"
        ]


def test_an_unknown_session_is_refused_rather_than_posted(board: Path) -> None:
    with pytest.raises(ValueError, match="unknown session 'claude:ghost'"):
        mcp_coord_server._tool_note(
            work_id=WORK_ID,
            body="into the void",
            actor="codex",
            session_id="codex:sender",
            to_session_id="claude:ghost",
            db_path=str(board),
        )

    conn = sqlite3.connect(board)
    try:
        written = conn.execute(
            "SELECT COUNT(*) FROM events WHERE to_selector LIKE 'session:%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert written == 0


def test_a_dead_session_is_refused_loudly(board: Path) -> None:
    """An expired lease is exactly the case that looks delivered and is not."""
    conn = connect(board)
    try:
        conn.execute(
            "UPDATE agent_sessions SET lease_until=0 WHERE session_id=?",
            ("claude:alpha",),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="is not live"):
        mcp_coord_server._tool_note(
            work_id=WORK_ID,
            body="nobody is home",
            actor="codex",
            session_id="codex:sender",
            to_session_id="claude:alpha",
            db_path=str(board),
        )


def test_an_ended_session_is_refused_too(board: Path) -> None:
    conn = connect(board)
    try:
        coord_db.end_session(conn, "claude:alpha")
    finally:
        conn.close()

    with pytest.raises(ValueError, match="is not live"):
        mcp_coord_server._tool_note(
            work_id=WORK_ID,
            body="that session closed",
            actor="codex",
            session_id="codex:sender",
            to_session_id="claude:alpha",
            db_path=str(board),
        )


def test_the_same_text_to_a_lane_and_to_a_session_are_two_requests(
    board: Path,
) -> None:
    """The selector is part of the idempotency hash, so the narrower address is
    not swallowed as a replay of the broader one."""
    lane = mcp_coord_server._tool_note(
        work_id=WORK_ID,
        body="identical body",
        title="identical title",
        actor="codex",
        session_id="codex:sender",
        db_path=str(board),
    )
    session = mcp_coord_server._tool_note(
        work_id=WORK_ID,
        body="identical body",
        title="identical title",
        actor="codex",
        session_id="codex:sender",
        to_session_id="claude:beta",
        db_path=str(board),
    )

    assert lane["replayed"] is False
    assert session["replayed"] is False
    assert lane["event_id"] != session["event_id"]


def test_post_note_keeps_its_lane_contract_when_both_are_given(board: Path) -> None:
    """The narrower address wins; widening it back would deliver to peers the
    sender deliberately did not name."""
    conn = connect(board)
    try:
        receipt = coord_db.post_note(
            conn,
            work_id=WORK_ID,
            actor="codex",
            session_id="codex:sender",
            to_actor="claude",
            to_session_id="claude:beta",
            body="for one of you",
        )
    finally:
        conn.close()

    assert receipt["to_selector"] == "session:claude:beta"


# --------------------------------------------------------------------------
# The CLI surface
# --------------------------------------------------------------------------


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@invalid",
        },
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def coord(
    project: Path, *args: str, session: str = "claude:alpha"
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
        "COORD_ACTOR": session.split(":", 1)[0],
        "COORD_SESSION_ID": session,
    }
    for leaked in (
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_WORKTREE_ID",
        "CODEX_CONVERSATION_ID",
        "STARSHIP_SESSION_KEY",
        "COORD_PARENT_SESSION_ID",
    ):
        env.pop(leaked, None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "coordharness.coord.cli",
            "--db",
            str(project / ".coordharness" / "coord.db"),
            *args,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )


def out(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def cli_board(project: Path) -> Path:
    out(
        coord(
            project,
            "create",
            WORK_ID,
            "--title",
            "a row two sessions are both near",
            "--module",
            "runtime",
            "--tier",
            "T1",
            "--done-signal",
            "artifacts/fleet-row.json",
            "--acceptance",
            "the named session reads it",
            "--note",
            "exercise session-addressed notes",
            session="claude:alpha",
        )
    )
    for session in (*FLEET, "codex:sender"):
        out(coord(project, "session", "start", session=session))
    return project


def test_the_cli_offers_a_session_target(cli_board: Path) -> None:
    note_help = coord(cli_board, "note", "--help")
    assert note_help.returncode == 0, note_help.stderr
    assert "--to-session" in note_help.stdout


def test_the_cli_delivers_to_the_named_session_only(cli_board: Path) -> None:
    posted = out(
        coord(
            cli_board,
            "note",
            WORK_ID,
            "--body",
            "you hold the pen on the retry path",
            "--title",
            "pen split",
            "--to-session",
            "claude:beta",
            session="codex:sender",
        )
    )

    assert posted["to_selector"] == "session:claude:beta"
    assert posted["to_session"] == "claude:beta"

    # The CLI inbox binds to the reading session, so the addressed message is
    # visible from the shell and not only over MCP.
    to_target = out(coord(cli_board, "inbox", "--directed", session="claude:beta"))
    assert [message["title"] for message in to_target["messages"]] == ["pen split"]
    to_sibling = out(coord(cli_board, "inbox", "--directed", session="claude:alpha"))
    assert to_sibling["messages"] == []


def test_the_cli_lane_default_is_unchanged(cli_board: Path) -> None:
    posted = out(
        coord(
            cli_board,
            "note",
            WORK_ID,
            "--body",
            "context for the other lane",
            "--title",
            "lane note",
            session="codex:sender",
        )
    )

    assert posted["to"] == "claude"
    assert posted["to_selector"] == "actor:claude"
    assert posted["to_session"] is None
    for session in FLEET:
        seen = out(coord(cli_board, "inbox", "--directed", session=session))
        assert [message["title"] for message in seen["messages"]] == ["lane note"]


def test_the_cli_refuses_an_unknown_target(cli_board: Path) -> None:
    refused = coord(
        cli_board,
        "note",
        WORK_ID,
        "--body",
        "into the void",
        "--to-session",
        "claude:ghost",
        session="codex:sender",
    )

    assert refused.returncode != 0
    assert "unknown session" in refused.stderr
    # Nothing on stdout, which the JSON-consuming callers parse.
    assert refused.stdout == ""


def test_the_shipped_command_delivers_the_refusal_as_one_line(
    cli_board: Path,
) -> None:
    """Through the entry point, where the CLI's error boundary lives.

    A refusal is the harness doing its job. Reaching the terminal as a
    traceback makes it read like a crash, which is the difference between an
    agent correcting its address and an agent reporting a broken tool.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(cli_board),
        "COORD_HOME": str(cli_board / ".coordharness"),
        "COORD_ACTOR": "codex",
        "COORD_SESSION_ID": "codex:sender",
    }
    for leaked in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "STARSHIP_SESSION_KEY"):
        env.pop(leaked, None)

    refused = subprocess.run(
        [
            sys.executable, "-m", "coordharness.entry",
            "--db", str(cli_board / ".coordharness" / "coord.db"),
            "note", WORK_ID, "--body", "into the void",
            "--to-session", "claude:ghost",
        ],
        cwd=cli_board, capture_output=True, text=True, env=env,
    )

    assert refused.returncode == 1
    assert len(refused.stderr.strip().splitlines()) == 1
    assert refused.stderr.startswith("coord: unknown session 'claude:ghost'")
