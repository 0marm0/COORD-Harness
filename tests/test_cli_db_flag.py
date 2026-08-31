"""``--db`` now works after the subcommand name, not only before it.

Measured defect: ``coord doctor --db /path`` died with argparse's own
"unrecognized arguments: --db /path", because the global ``--db`` was only
ever declared on the top-level parser and argparse subparsers do not fall
through to a parent's options once the subcommand token is consumed. The
README discussed the flag without ever saying where it has to go, and the
natural way to append a flag to a command you have already typed -- onto the
end -- was exactly the one position that failed.

Every subcommand that reads the database now declares its own ``--db`` too
(see ``cli._add_subparser`` and ``cli._resolve_db_path``), deferring to the
global one when both are given and agreeing, and refusing outright, naming
both positions, when they disagree.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from coordharness import entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import cli, coord_db
from coordharness.coord.config import connect


def _new_db(tmp_path: Path, name: str = "coord.db") -> Path:
    db = tmp_path / name
    bootstrap_database(db)
    return db


def _run(argv: list[str]) -> tuple[int, str]:
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


@pytest.mark.parametrize(
    "trailing_argv",
    [
        ["doctor"],
        ["board", "--group-by", "module", "--json"],
        ["conflicts"],
    ],
    ids=["doctor", "board", "conflicts"],
)
def test_db_flag_after_the_subcommand_is_accepted(
    tmp_path: Path, trailing_argv: list[str]
) -> None:
    """The exact regression: `<subcommand> --db PATH` must not be a parse error.

    Before the fix this raised SystemExit(2) from argparse itself --
    "unrecognized arguments: --db ..." -- before the subcommand's own handler
    ever ran. A clean int return (whatever status the handler reports) proves
    argparse accepted the flag in this position.
    """
    db = _new_db(tmp_path)
    rc, _out = _run([*trailing_argv, "--db", str(db)])
    assert isinstance(rc, int)


def test_db_after_subcommand_reads_the_named_database_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not just parsed -- actually threaded through to the handler.

    A default-looking database sits where ``COORD_DB`` points; the explicit
    ``--db`` given after `board` names a different one with a distinguishing
    row. The board command must read the row from the path it was given.
    """
    default_db = _new_db(tmp_path, "default.db")
    monkeypatch.setenv("COORD_DB", str(default_db))

    explicit_db = _new_db(tmp_path, "explicit.db")
    conn = connect(explicit_db)
    try:
        coord_db.upsert_work(
            conn, "EXPLICIT-ROW", title="Only in the explicit database", module="demo",
        )
    finally:
        conn.close()

    rc, out = _run(["board", "--group-by", "module", "--json", "--db", str(explicit_db)])
    assert rc == 0
    parsed = json.loads(out)
    assert [row["work_id"] for row in parsed["rows"]] == ["EXPLICIT-ROW"]


def test_db_before_the_subcommand_still_works(tmp_path: Path) -> None:
    """The original, global position is unchanged by adding the new one."""
    db = _new_db(tmp_path)
    rc, out = _run(["--db", str(db), "board", "--group-by", "module", "--json"])
    assert rc == 0
    assert json.loads(out) == {"count": 0, "rows": []}


def test_db_given_both_positions_with_the_same_value_is_not_a_conflict(
    tmp_path: Path,
) -> None:
    db = _new_db(tmp_path)
    rc, out = _run(
        ["--db", str(db), "board", "--group-by", "module", "--json", "--db", str(db)]
    )
    assert rc == 0
    assert json.loads(out) == {"count": 0, "rows": []}


def test_db_given_both_positions_with_different_values_is_refused(
    tmp_path: Path,
) -> None:
    global_db = _new_db(tmp_path, "global.db")
    sub_db = _new_db(tmp_path, "sub.db")

    with pytest.raises(ValueError) as excinfo:
        cli.main(
            ["--db", str(global_db), "board", "--group-by", "module", "--db", str(sub_db)]
        )
    message = str(excinfo.value)
    # Names both positions, so the caller can tell which one to drop, rather
    # than silently guessing and running against a database nobody asked for.
    assert str(global_db) in message
    assert str(sub_db) in message
    assert "before the subcommand" in message
    assert "after" in message


def test_db_conflict_surfaces_as_a_one_line_refusal_through_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same conflict, through the process entry point's error boundary."""
    global_db = _new_db(tmp_path, "global.db")
    sub_db = _new_db(tmp_path, "sub.db")

    code = entry.main(
        ["--db", str(global_db), "doctor", "--db", str(sub_db)]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err
    assert "conflicting --db values" in captured.err


def test_claim_accepts_db_after_the_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real lifecycle verb, not just a read-only one, honours the new position."""
    db = _new_db(tmp_path)
    conn = connect(db)
    try:
        coord_db.upsert_work(conn, "CLAIM-DB-FLAG-1", title="Claimable row", assignee="claude")
    finally:
        conn.close()

    monkeypatch.setenv("COORD_ACTOR", "claude")
    monkeypatch.setenv("COORD_SESSION_ID", "db-flag-test")

    rc, out = _run(["claim", "CLAIM-DB-FLAG-1", "--step", "testing --db placement", "--db", str(db)])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["work_id"] == "CLAIM-DB-FLAG-1"
