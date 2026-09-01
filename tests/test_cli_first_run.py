"""A newcomer's first ten minutes: does each refusal say what to try next.

The install path is proven elsewhere. This covers the small frictions that
still cost a first-time user their first debugging loop: a bare `coord` with
no subcommand, an empty board before anything has been seeded, and the
lifecycle verbs run out of order (`done` before `claim`, `heartbeat-claim` /
`release` given a work id instead of the claim id `coord claim` hands back).

Every guidance string here is asserted against real behavior, not just
presence: the commands a message points at are run and shown to work, not
merely grepped for. The counterpart to all of this is
``test_board_json_is_byte_stable_when_not_a_tty`` in ``test_board_table.py``
-- this file adds one more golden-byte check of its own, because a change
here that widened a human-facing message must not have also touched the
machine-readable one.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from coordharness import entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.board_format import render_board_table
from coordharness.coord.config import connect

def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = entry.main(argv)
        except SystemExit as exc:
            # argparse's own usage errors (unknown subcommand, missing
            # required flag) exit the process directly rather than returning
            # a code -- entry.main deliberately lets that through unchanged
            # (see its docstring), so this is the one place a test harness
            # has to catch it instead.
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COORD_ACTOR", "claude")
    monkeypatch.setenv("COORD_SESSION_ID", "first-run-test")


# --------------------------------------------------------------------------
# Bare invocation
# --------------------------------------------------------------------------


def test_bare_invocation_summary_lists_commands_that_exist(tmp_path: Path, lane):
    """`coord` with no subcommand names real commands, and stays exit 2."""
    db = tmp_path / "coord.db"
    rc, out, err = _run(["--db", str(db)])

    assert rc == 2, "argparse's own missing-subcommand exit code, unchanged"
    assert out == "", "nothing on stdout -- this is a usage refusal, not data"
    assert "Traceback" not in err

    # Every command named in the curated summary must be a real, working
    # subcommand -- not just a plausible-looking string.
    named = {"demo", "board", "claim", "done"}
    for word in named:
        assert word in err, f"summary should mention {word!r}: {err!r}"

    # And "real" is proven by running them, not by grepping cli.py's argparse
    # setup for the same literal this test would otherwise just echo back.
    rc_demo = entry.main(["--db", str(db), "demo", "--quiet"])
    assert rc_demo == 0
    rc_board = entry.main(["--db", str(db), "board", "--json"])
    assert rc_board == 0


def test_bare_invocation_does_not_dump_the_full_subcommand_list(tmp_path: Path, lane):
    """The curated message is short -- it is a pointer, not the --help dump."""
    db = tmp_path / "coord.db"
    _, _, err = _run(["--db", str(db)])
    # The full-dump argparse error names every subcommand comma-separated on
    # one line, including obscure ones a newcomer has no use for on day one.
    assert "declare-write-set" not in err
    assert "heartbeat-claim" not in err


def test_unknown_subcommand_still_names_every_real_choice(tmp_path: Path, lane):
    """An unknown subcommand keeps argparse's own exhaustive choice list.

    Unlike the bare case, "what do I run instead" here is already answered by
    naming every real subcommand -- so this is a regression guard that the
    ``required=False`` change did not also swallow *this* message.
    """
    db = tmp_path / "coord.db"
    rc, out, err = _run(["--db", str(db), "frobnicate"])
    assert rc == 2
    assert "invalid choice: 'frobnicate'" in err
    assert "board" in err and "claim" in err and "demo" in err


# --------------------------------------------------------------------------
# Empty board
# --------------------------------------------------------------------------


def test_empty_board_table_names_a_command_that_seeds_it(tmp_path: Path):
    out = render_board_table([], group_by="module", now=1_700_000_000.0)
    assert "coord demo" in out

    # Prove it: an empty database, then the exact command the message names,
    # then a non-empty board.
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    rc = entry.main(["--db", str(db), "demo", "--quiet"])
    assert rc == 0
    conn = connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM work_items").fetchone()["n"]
    finally:
        conn.close()
    assert count > 0, "'coord demo', the command the empty board points at, must seed rows"


def test_empty_board_json_is_unaffected_by_the_table_wording(tmp_path: Path):
    """The guidance lives in the human table only; the JSON shape is untouched."""
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = entry.main(["--db", str(db), "board"])
    assert rc == 0
    assert out.getvalue() == '{"count": 0, "rows": []}\n'


# --------------------------------------------------------------------------
# Lifecycle verbs run out of order
# --------------------------------------------------------------------------


@pytest.fixture
def one_row_board(tmp_path: Path) -> Path:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn, "FIRSTRUN-1", title="A row to run the lifecycle verbs on",
            module="demo", assignee="claude", intent_state="queued",
        )
    finally:
        conn.close()
    return db


def test_done_before_claim_names_claim_as_the_predecessor(one_row_board, lane):
    rc, out, err = _run(["--db", str(one_row_board), "done", "FIRSTRUN-1"])
    assert rc != 0
    assert out == ""
    assert "claim the work before completing it" in err
    assert "coord: " in err and "Traceback" not in err


def test_heartbeat_on_a_work_id_names_claim_as_the_fix(one_row_board, lane):
    """`coord heartbeat-claim` takes a claim id, not the work id `coord claim`
    took -- an easy mix-up for a newcomer who has not seen a claim id yet.
    The refusal should say to run `coord claim` and hand back a real id, not
    just "unknown claim_id"."""
    rc, out, err = _run(["--db", str(one_row_board), "heartbeat-claim", "FIRSTRUN-1"])
    assert rc != 0
    assert out == ""
    assert "coord claim FIRSTRUN-1" in err
    assert "not a claim id" in err


def test_release_on_an_unknown_claim_names_claim_as_the_fix(one_row_board, lane):
    rc, out, err = _run(["--db", str(one_row_board), "release", "not-a-real-claim"])
    assert rc != 0
    assert out == ""
    assert "coord claim" in err


def test_claim_then_heartbeat_then_release_actually_works(one_row_board, lane):
    """The happy path the refusals above are steering toward is real."""
    rc, out, _ = _run(["--db", str(one_row_board), "claim", "FIRSTRUN-1", "--step", "starting"])
    assert rc == 0
    import json

    claim_id = json.loads(out)["claim_id"]

    rc, _, _ = _run(["--db", str(one_row_board), "heartbeat-claim", claim_id])
    assert rc == 0
    rc, _, _ = _run(["--db", str(one_row_board), "release", claim_id])
    assert rc == 0


# --------------------------------------------------------------------------
# Golden byte-stability: --json output for `coord board` must not move
# --------------------------------------------------------------------------


def test_board_json_bytes_are_unchanged_on_a_seeded_database(
    tmp_path: Path, lane, monkeypatch: pytest.MonkeyPatch
):
    """None of the human-facing wording changes above may touch this string."""
    import itertools

    counter = itertools.count()
    base = 1_700_000_000.0
    monkeypatch.setattr(coord_db, "db_now", lambda _conn: base + next(counter))

    db = tmp_path / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn, "FIRSTRUN-JSON-1", title="Golden row one", module="demo",
            assignee="claude", intent_state="queued", priority=1,
        )
        coord_db.upsert_work(
            conn, "FIRSTRUN-JSON-2", title="Golden row two", module="other",
            intent_state="planned",
        )
    finally:
        conn.close()

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = entry.main(["--db", str(db), "board", "--group-by", "module", "--json"])
    assert rc == 0
    assert err.getvalue() == ""
    expected = (
        '{"count": 2, "rows": [{"work_id": "FIRSTRUN-JSON-2", "title": "Golden '
        'row two", "status": "planned", "group": "other", "assignee": null, '
        '"owner_session_id": null, "owner_session_actor": null, '
        '"owner_session_label": null}, {"work_id": "FIRSTRUN-JSON-1", "title": '
        '"Golden row one", "status": "queued", "group": "demo", "assignee": '
        '"claude", "owner_session_id": null, "owner_session_actor": null, '
        '"owner_session_label": null}]}\n'
    )
    assert out.getvalue() == expected
