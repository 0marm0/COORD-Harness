"""What a refusal looks like when it reaches a terminal.

The refusals themselves are the product: a second agent asking for work that is
already assigned is *supposed* to be told no. Until the boundary in
``coordharness.entry`` existed, every one of those noes arrived as an eleven-line
Python traceback with the sentence that mattered on the last line, which reads as
a crash -- and in the unknown-id case the last line was not even a sentence, it
was ``sqlite3.IntegrityError: FOREIGN KEY constraint failed``.

So these tests assert on the *delivery*, not the wording: no ``Traceback`` in the
output, no storage-layer vocabulary, a non-zero exit that scripts can still
branch on, and an escape hatch for anyone who actually wants the stack. The last
two guard the boundary against becoming a blanket ``except``: an error nobody
anticipated must still look like the bug it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness import entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect


def _board(tmp_path: Path, **work: str) -> Path:
    """A one-row board, so a refusal has something concrete to refuse."""
    db = tmp_path / "state" / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        for work_id, assignee in work.items():
            coord_db.upsert_work(
                conn,
                work_id,
                title=f"Synthetic row {work_id}",
                assignee=assignee,
            )
    finally:
        conn.close()
    return db


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch):
    """Run as an exact lane, because the refusals under test are lane-scoped."""
    monkeypatch.setenv("COORD_ACTOR", "claude")
    monkeypatch.setenv("COORD_SESSION_ID", "boundary-test")


def test_cross_lane_refusal_is_a_sentence_not_a_stack(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    db = _board(tmp_path, ML204="codex")
    code = entry.main(["--db", str(db), "claim", "ML204"])
    captured = capsys.readouterr()

    assert code != 0, "a refusal that exits zero is a refusal scripts cannot see"
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    assert captured.err.startswith("coord: ")
    assert (
        "cannot claim work assigned to 'codex' from 'claude'" in captured.err
    ), captured.err
    # Nothing on stdout: the JSON-emitting verbs share this stream, and a
    # consumer piping it should get an empty document, not half an error.
    assert captured.out == ""


def test_unknown_work_id_names_the_id_and_never_the_storage_layer(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    db = _board(tmp_path, ML204="claude")
    code = entry.main(["--db", str(db), "claim", "NOPE-999"])
    captured = capsys.readouterr()

    assert code != 0
    assert "unknown work id 'NOPE-999'" in captured.err
    # The hint is the point. "Unknown id" tells a first-time user they were
    # wrong; it does not tell them where the right ids live.
    assert "coord board" in captured.err
    combined = captured.err + captured.out
    assert "IntegrityError" not in combined
    assert "FOREIGN KEY" not in combined
    assert "Traceback" not in combined


def test_already_claimed_refusal_names_the_holder_not_the_index(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    db = _board(tmp_path, UI102="claude")
    conn = connect(db)
    try:
        coord_db.register_session(conn, "claude:someone-else", "claude", lease_s=600)
        coord_db.claim_work(conn, "claude:someone-else", "UI102", lease_s=600)
    finally:
        conn.close()

    code = entry.main(["--db", str(db), "claim", "UI102"])
    captured = capsys.readouterr()

    assert code != 0
    assert "already claimed by session 'claude:someone-else'" in captured.err
    combined = captured.err + captured.out
    assert "UNIQUE constraint" not in combined
    assert "Traceback" not in combined


def test_missing_artifact_refusal_survives_the_boundary_intact(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    db = _board(tmp_path, PLT301="claude")
    assert entry.main(["--db", str(db), "claim", "PLT301"]) == 0
    capsys.readouterr()

    code = entry.main(
        ["--db", str(db), "done", "PLT301", "--artifact", "docs/reports/absent.md"]
    )
    captured = capsys.readouterr()

    assert code != 0
    assert "Traceback" not in captured.err
    # The wording is coord_db's, unchanged; only the delivery is this module's.
    assert "artifact proof" in captured.err
    assert captured.err.count("\n") == 1, "a refusal is one line"


def test_unknown_work_id_read_is_a_report_not_a_key_error(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    """work-context answers in JSON, so its miss has to be JSON too.

    Its not-found branch tested ``row is None`` while ``_work_row`` returns an
    empty dict, so the branch never fired and the read fell through to a bare
    ``KeyError: 'version'``.
    """
    db = _board(tmp_path, ML204="claude")
    code = entry.main(["--db", str(db), "work-context", "NOPE-999"])
    captured = capsys.readouterr()

    assert code != 0
    assert "Traceback" not in captured.err
    assert "KeyError" not in captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "work_not_found"


def test_traceback_flag_restores_the_stack(tmp_path: Path, lane):
    db = _board(tmp_path, ML204="codex")
    with pytest.raises(ValueError, match="cannot claim work assigned to 'codex'"):
        entry.main(["--traceback", "--db", str(db), "claim", "ML204"])


def test_traceback_flag_is_accepted_after_the_subcommand(tmp_path: Path, lane):
    """Where a debugging flag actually gets typed.

    Nobody reaches for this until a command has already failed, and then they
    append it to the line they just ran. argparse would only accept it ahead of
    the subcommand, so the entry point takes it out of argv wherever it sits.
    """
    db = _board(tmp_path, ML204="codex")
    with pytest.raises(ValueError, match="cannot claim work assigned to 'codex'"):
        entry.main(["--db", str(db), "claim", "ML204", "--traceback"])


def test_unanticipated_error_still_crashes(
    tmp_path: Path, lane, monkeypatch: pytest.MonkeyPatch
):
    """The boundary is a filter, not a blanket.

    A bare RuntimeError in this package marks a broken invariant -- "new claim is
    missing its exact custody fence" -- not something the caller did. Swallowing
    it into a tidy one-liner would hide a bug behind the same surface that
    reports a refusal, so it keeps its stack.
    """
    db = _board(tmp_path, ML204="claude")

    def boom(_argv):
        raise RuntimeError("invariant broken")

    monkeypatch.setattr(entry.coord_cli, "main", boom)
    with pytest.raises(RuntimeError, match="invariant broken"):
        entry.main(["--db", str(db), "claim", "ML204"])


def test_success_path_still_prints_json_to_stdout_and_exits_zero(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    """The boundary has to be invisible when nothing is wrong."""
    db = _board(tmp_path, SRCH401="claude")
    assert entry.main(["--db", str(db), "claim", "SRCH401"]) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert json.loads(captured.out)["work_id"] == "SRCH401"


def test_route_refuses_a_usage_ledger_that_is_not_there(
    tmp_path: Path, lane, capsys: pytest.CaptureFixture[str]
):
    """--usage-db is read-only, so a path that is absent is a typo.

    UsageLedger would otherwise try to create it and surface the failure as an
    OSError naming the parent directory, which is not what the caller got wrong.
    """
    db = _board(tmp_path, ML204="claude")
    missing = tmp_path / "no-such-dir" / "usage.db"
    code = entry.main(["--db", str(db), "route", "--usage-db", str(missing)])
    captured = capsys.readouterr()

    assert code != 0
    assert "Traceback" not in captured.err
    assert "does not exist" in captured.err
