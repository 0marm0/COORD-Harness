"""Three concurrent sessions in one lane must not render as one row of "claude".

``coord board`` emitted five fields per row, and the only ownership among them
was ``assignee`` -- the LANE. A fleet of three Claude sessions holding three
different rows therefore printed the same owner three times, which is the whole
board for that fleet reduced to one name. The information was already derived:
``v_work_owner`` computes ``owner_session_label`` as human_label, then
conversation_title, then the actor, so it degrades back to the lane name only
when nothing better was ever registered.

The fix is additive on purpose. ``assignee`` still means the lane and is still
emitted, because everything downstream reads it; the session fields sit beside
it. Nothing here makes independence a session property: review attribution is a
lane property and is untouched by these fields.
"""

from __future__ import annotations

import json
import os
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

FLEET = ("claude:alpha", "claude:beta", "claude:gamma")


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
def fleet_board(project: Path) -> Path:
    """Three same-lane sessions, each holding one row of its own."""
    for index, session in enumerate(FLEET):
        work_id = f"DEMO-CLA-FLEET-{index}"
        out(
            coord(
                project,
                "create",
                work_id,
                "--title",
                f"fleet member {index} work",
                "--module",
                "runtime",
                "--tier",
                "T1",
                "--done-signal",
                f"artifacts/{work_id.lower()}.json",
                "--acceptance",
                "the board says which session holds this",
                "--note",
                "three sessions, one lane",
                session=session,
            )
        )
        out(coord(project, "claim", work_id, session=session))
    return project


def test_board_rows_name_the_session_not_only_the_lane(fleet_board: Path) -> None:
    board = out(coord(fleet_board, "board"))

    rows = {row["work_id"]: row for row in board["rows"]}
    assert len(rows) == 3
    # The defect: one lane name for the whole fleet.
    assert {row["assignee"] for row in rows.values()} == {"claude"}
    # The fix: three distinguishable owners.
    assert {row["owner_session_id"] for row in rows.values()} == set(FLEET)
    assert len({row["owner_session_label"] for row in rows.values()}) == 3


def test_the_existing_board_shape_is_preserved(fleet_board: Path) -> None:
    """Fields were added, not renamed or removed."""
    board = out(coord(fleet_board, "board"))

    assert set(board) == {"count", "rows"}
    for row in board["rows"]:
        assert {"work_id", "title", "status", "group", "assignee"} <= set(row)
        # assignee still answers "which lane", unchanged.
        assert row["assignee"] == "claude"


def test_an_unclaimed_row_carries_no_session_owner(project: Path) -> None:
    """No claim means no holder, and the fields say so rather than guessing."""
    work_id = "DEMO-CLA-UNHELD"
    out(
        coord(
            project,
            "create",
            work_id,
            "--title",
            "nobody is holding this",
            "--module",
            "runtime",
            "--tier",
            "T1",
            "--done-signal",
            f"artifacts/{work_id.lower()}.json",
            "--acceptance",
            "an unheld row has no owning session",
            "--note",
            "queued, unclaimed",
        )
    )

    row = out(coord(project, "board"))["rows"][0]

    assert row["assignee"] == "claude"
    assert row["owner_session_id"] is None
    assert row["owner_session_label"] is None


def test_the_label_falls_back_to_the_actor_when_nothing_better_exists(
    tmp_path: Path,
) -> None:
    """``owner_session_label`` is COALESCE(human_label, conversation_title, actor).

    A session registered with no label at all still resolves to something, so
    the field is never the empty string masquerading as an unknown owner.
    """
    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            "DEMO-CLA-BARE",
            title="a row held by an unlabelled session",
            assignee="claude",
            module="runtime",
            surface="job",
            tier="T1",
            done_signal="artifacts/bare.json",
            acceptance_json='["the label degrades to the actor"]',
            note="no human label registered",
            intent_state="queued",
        )
        coord_db.register_session(conn, "claude:bare", "claude")
        coord_db.claim_work(conn, "claude:bare", "DEMO-CLA-BARE")
        row = coord_db.board_rows(conn)[0]
    finally:
        conn.close()

    assert row["owner_session_id"] == "claude:bare"
    assert row["owner_session_label"] == "claude"


def test_the_mcp_board_card_also_carries_the_session_label(tmp_path: Path) -> None:
    """The compact card an agent reads must not narrow back to the lane."""
    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            "DEMO-CLA-MCPBOARD",
            title="a row an agent reads over MCP",
            assignee="claude",
            module="runtime",
            surface="job",
            tier="T1",
            done_signal="artifacts/mcpboard.json",
            acceptance_json='["the card names the holding session"]',
            note="compact board card",
            intent_state="queued",
        )
        coord_db.register_session(
            conn, "claude:alpha", "claude", human_label="Claude alpha"
        )
        coord_db.claim_work(conn, "claude:alpha", "DEMO-CLA-MCPBOARD")
    finally:
        conn.close()

    board = mcp_coord_server._tool_board(db_path=str(database))

    card = board["rows"][0]
    assert card["assignee"] == "claude"
    assert card["owner_session_label"] == "Claude alpha"
    assert card["owner_session_actor"] == "claude"
