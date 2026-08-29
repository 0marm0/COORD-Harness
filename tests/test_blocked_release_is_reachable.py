"""The documented blocked release, run exactly as documented.

`coord release CLAIM --status blocked` was in the handbook, in the parity
matrix, and in `--status`'s own choices, and it could not be run. `release_claim`
has always required a non-empty reason for a blocked release, and this surface
had no flag that reached the parameter and passed nothing to it -- so the
documented command refused, twice over: once on the resume trigger the printed
example omitted, and again on the reason it had no way to supply.

The tests take the command out of the Markdown rather than restating it. A test
that asserts on its own copy of a documented command proves the code works and
says nothing at all about the documentation, which is the half that was wrong.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordharness import entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

HANDBOOK = Path(__file__).resolve().parents[1] / "docs" / "operators-handbook.md"
WORK_ID = "DEMO-BLOCKED-1"
LANE_SESSION = "claude:blocked-release-fixture"


def handbook_blocked_release_command() -> list[str]:
    """The `coord release … --status blocked` example, as printed.

    Line continuations are joined and the quoted arguments are kept whole, so
    what comes back is the argv a reader typing the block would produce.
    """
    text = HANDBOOK.read_text(encoding="utf-8")
    match = re.search(
        r"^coord release CLAIM --status blocked((?:.*\\\n)*.*)$",
        text,
        re.MULTILINE,
    )
    assert match is not None, "the handbook no longer documents a blocked release"
    joined = "coord release CLAIM --status blocked" + match.group(1).replace("\\\n", " ")
    tokens = re.findall(r'"[^"]*"|\S+', joined)
    return [token.strip('"').strip() for token in tokens]


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setenv("COORD_ACTOR", "claude")
    monkeypatch.setenv("COORD_SESSION_ID", LANE_SESSION)
    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="a row whose upstream is missing",
            assignee="claude",
            module="runtime",
            surface="job",
            tier="T2",
            done_signal="artifacts/blocked.json",
            acceptance_json='["the upstream fixture exists"]',
            note="blocked-release fixture",
            intent_state="queued",
        )
    finally:
        conn.close()
    return database


@pytest.fixture
def claim(board: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert entry.main(["--db", str(board), "claim", WORK_ID]) == 0
    return str(json.loads(capsys.readouterr().out)["claim_id"])


def _claim_row(board: Path, claim_id: str) -> dict:
    conn = connect(board)
    try:
        return dict(
            conn.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def test_the_handbook_still_prints_a_blocked_release():
    """Guards the extraction, so a doc rewrite cannot silently empty this file."""
    argv = handbook_blocked_release_command()
    assert argv[:4] == ["coord", "release", "CLAIM", "--status"]
    assert argv[4] == "blocked"


def test_the_handbooks_exact_blocked_release_runs(
    board: Path, claim: str, capsys: pytest.CaptureFixture[str]
):
    """The whole defect, and its fix, in one call.

    `coord` and the `CLAIM` placeholder are substituted -- a reader supplies
    their own claim id -- and every other token is the handbook's.
    """
    argv = handbook_blocked_release_command()
    argv = ["--db", str(board), *argv[1:]]
    argv[argv.index("CLAIM")] = claim

    code = entry.main(argv)
    captured = capsys.readouterr()

    assert code == 0, captured.err
    assert captured.err == ""
    assert json.loads(captured.out)["ok"] is True


def test_it_produces_a_blocked_claim_carrying_its_reason(
    board: Path, claim: str, capsys: pytest.CaptureFixture[str]
):
    """The intended state, not merely a zero exit.

    A release that reported success and left the claim running would satisfy
    the exit code and none of the point: the block has to be visible to the
    reaper and to the next reader of the board.
    """
    argv = handbook_blocked_release_command()
    argv = ["--db", str(board), *argv[1:]]
    argv[argv.index("CLAIM")] = claim
    assert entry.main(argv) == 0
    capsys.readouterr()

    row = _claim_row(board, claim)
    assert row["status"] == "blocked"
    assert str(row["release_reason"] or "").strip(), (
        "a blocked claim with no recorded reason is the state the storage layer "
        "refuses; recording it empty would defeat the refusal instead of "
        "satisfying it"
    )
    # Blocked is not released: the row keeps its disposition rather than being
    # requeued as an abandoned running claim would be.
    conn = connect(board)
    try:
        intent = str(
            conn.execute(
                "SELECT intent_state FROM work_items WHERE work_id=?", (WORK_ID,)
            ).fetchone()["intent_state"]
        )
    finally:
        conn.close()
    assert intent != "queued"


def test_the_reason_reaches_the_storage_layer_intact(
    board: Path, claim: str, capsys: pytest.CaptureFixture[str]
):
    """`--reason` is the flag; `release_claim(reason=…)` is where it lands."""
    reason = "the ingest run has not produced the fixture yet"
    assert (
        entry.main(
            [
                "--db", str(board), "release", claim,
                "--status", "blocked",
                "--reason", reason,
                "--next-step", "rebuild it from the ingest run",
                "--resume-when", "the fixture is on disk",
                "--resume-manual",
            ]
        )
        == 0
    ), capsys.readouterr().err
    assert _claim_row(board, claim)["release_reason"] == reason


def test_a_blocked_release_without_a_reason_names_the_flag(
    board: Path, claim: str, capsys: pytest.CaptureFixture[str]
):
    """The refusal has to name something the caller can type.

    The storage layer's wording -- "requires a non-empty reason" -- is correct
    and unactionable from a terminal, because it cannot know what the flag is
    called. Adding the flag without improving the refusal would have left the
    next reader in the same place.
    """
    code = entry.main(
        [
            "--db", str(board), "release", claim,
            "--status", "blocked",
            "--next-step", "rebuild it",
            "--resume-when", "the fixture is on disk",
            "--resume-manual",
        ]
    )
    captured = capsys.readouterr()

    assert code != 0
    assert "Traceback" not in captured.err
    assert "--reason" in captured.err
    assert _claim_row(board, claim)["status"] == "running"


def test_released_and_paused_do_not_acquire_the_requirement(
    board: Path, claim: str, capsys: pytest.CaptureFixture[str]
):
    """Only blocked needs it; a plain release must stay a one-liner."""
    assert entry.main(["--db", str(board), "release", claim, "--status", "released"]) == 0
    assert capsys.readouterr().err == ""
    assert _claim_row(board, claim)["status"] == "released"
