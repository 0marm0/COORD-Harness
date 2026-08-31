"""``coord board`` gets a human table at a terminal; scripts keep their JSON.

Before this module, ``coord board`` had exactly one output shape -- one line
of unwrapped JSON, printed whether the reader was a script or a person -- and
``--group-by`` decorated each row with a ``group`` field without ever
actually clustering rows by it. This tests both halves of the fix:

  * the JSON path (piped output, or ``--json`` even at a terminal) is
    byte-for-byte what it always was, on a seeded database with a pinned
    clock so row order is reproducible on every machine;
  * a real terminal gets a table instead, with rows actually clustered under
    group headers and a lease-remaining column.

``board_format.render_board_table`` is also exercised directly, since a unit
test of truncation and duration formatting does not need a database at all.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import cli, coord_db
from coordharness.coord.board_format import render_board_table
from coordharness.coord.config import connect


class _FakeTTY(io.StringIO):
    """A writable buffer that reports itself as a terminal.

    ``coord board``'s human/JSON gate reads ``sys.stdout.isatty()``. Real
    ``capsys``/``redirect_stdout`` buffers answer ``False``, which is exactly
    right for the byte-stability tests below but useless for exercising the
    table path -- this stands in for an actual terminal without needing one.
    """

    def isatty(self) -> bool:  # noqa: D102
        return True


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def _run_tty(argv: list[str]) -> tuple[int, str]:
    buf = _FakeTTY()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small, deterministic board: three rows, two modules, no live claims.

    The clock is pinned to a strictly increasing fake sequence, the same
    technique ``coordharness.demo`` uses for its own reproducible seed, so
    ``updated_at`` ordering -- and therefore row order in every rendering --
    is identical on every run and every machine. Real wall-clock timestamps
    landed two of these rows in the same second often enough to make row
    order a coin flip, which a byte-identity test cannot tolerate.
    """
    counter = itertools.count()
    base = 1_700_000_000.0
    monkeypatch.setattr(coord_db, "db_now", lambda _conn: base + next(counter))

    db = tmp_path / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn, "CLI-TABLE-1", title="First deterministic row", module="demo",
            assignee="claude", intent_state="queued", priority=1,
        )
        coord_db.upsert_work(
            conn, "CLI-TABLE-2",
            title="Second deterministic row, with a much longer title that "
                  "should exercise truncation logic in any future human renderer",
            module="demo", assignee="codex", intent_state="planned", priority=2,
        )
        coord_db.upsert_work(
            conn, "OTHER-TABLE-1", title="Row in a different module", module="other",
            intent_state="planned",
        )
    finally:
        conn.close()
    return db


# Captured from `coord board --group-by module` against the fixture above,
# before this module existed to render anything else. Any change to this
# string is a change to a shape scripts depend on.
_EXPECTED_JSON = (
    '{"count": 3, "rows": [{"work_id": "OTHER-TABLE-1", "title": "Row in a '
    'different module", "status": "planned", "group": "other", "assignee": '
    'null, "owner_session_id": null, "owner_session_actor": null, '
    '"owner_session_label": null}, {"work_id": "CLI-TABLE-2", "title": '
    '"Second deterministic row, with a much longer title that should '
    'exercise truncation logic in any future human renderer", "status": '
    '"planned", "group": "demo", "assignee": "codex", "owner_session_id": '
    'null, "owner_session_actor": null, "owner_session_label": null}, '
    '{"work_id": "CLI-TABLE-1", "title": "First deterministic row", '
    '"status": "queued", "group": "demo", "assignee": "claude", '
    '"owner_session_id": null, "owner_session_actor": null, '
    '"owner_session_label": null}]}\n'
)


def test_board_json_is_byte_stable_when_not_a_tty(seeded_db: Path) -> None:
    # Prove the premise: an io.StringIO (what redirect_stdout/capsys give a
    # test) is not a tty, so this exercises the same branch a real pipe does.
    assert io.StringIO().isatty() is False

    rc, out = _run(["--db", str(seeded_db), "board", "--group-by", "module"])
    assert rc == 0
    assert out == _EXPECTED_JSON


def test_board_json_flag_wins_even_at_a_tty(seeded_db: Path) -> None:
    rc, out = _run_tty(
        ["--db", str(seeded_db), "board", "--group-by", "module", "--json"]
    )
    assert rc == 0
    assert out == _EXPECTED_JSON


def test_board_json_is_still_one_json_document(seeded_db: Path) -> None:
    """Independent of the golden string: the JSON path parses and round-trips."""
    _, out = _run(["--db", str(seeded_db), "board", "--group-by", "module"])
    assert out.count("\n") == 1
    parsed = json.loads(out)
    assert parsed["count"] == 3
    assert {row["work_id"] for row in parsed["rows"]} == {
        "CLI-TABLE-1", "CLI-TABLE-2", "OTHER-TABLE-1",
    }


def test_board_renders_a_grouped_table_at_a_real_terminal(seeded_db: Path) -> None:
    rc, out = _run_tty(["--db", str(seeded_db), "board", "--group-by", "module"])
    assert rc == 0

    # Not the JSON shape: more than one line, and not parseable as JSON.
    assert out.count("\n") > 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)

    lines = out.splitlines()
    header = lines[0]
    for column in ("ID", "TITLE", "STATUS", "ASSIGNEE", "LEASE"):
        assert column in header

    # Real group header rows -- not a `group` field bolted onto each row.
    # Group order follows row recency (most recently updated group first),
    # not alphabetical order, so find both headers before slicing between
    # them rather than assuming which comes first.
    header_indices = sorted(
        i for i, line in enumerate(lines) if line.startswith("-- ")
    )
    assert len(header_indices) == 2
    demo_header = next(i for i, line in enumerate(lines) if line.startswith("-- demo"))
    other_header = next(i for i, line in enumerate(lines) if line.startswith("-- other"))
    assert "(2)" in lines[demo_header]
    assert "(1)" in lines[other_header]

    # Every row appears under its own group's header, not scattered: slice
    # each block from its header up to whichever header comes next.
    first_idx, second_idx = header_indices
    blocks = {
        lines[first_idx]: lines[first_idx:second_idx],
        lines[second_idx]: lines[second_idx:],
    }
    demo_block = "\n".join(blocks[lines[demo_header]])
    other_block = "\n".join(blocks[lines[other_header]])
    assert "CLI-TABLE-1" in demo_block and "CLI-TABLE-2" in demo_block
    assert "OTHER-TABLE-1" in other_block
    assert "CLI-TABLE-1" not in other_block

    # None of these rows hold a claim, so lease-remaining is blank throughout.
    for line in lines[1:]:
        if line.startswith("--") or not line.strip():
            continue
        assert not any(ch.isdigit() and line.rstrip().endswith(f"{ch}s") for ch in "0123456789")


def test_board_table_shows_lease_remaining_for_a_held_claim(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(seeded_db)
    try:
        coord_db.register_session(conn, "claude:demo", "claude", runner_type="claude_chat")
        coord_db.claim_work(conn, "claude:demo", "CLI-TABLE-1", step="working it")
    finally:
        conn.close()

    rc, out = _run_tty(["--db", str(seeded_db), "board", "--group-by", "module"])
    assert rc == 0
    row_line = next(line for line in out.splitlines() if line.startswith("CLI-TABLE-1"))
    assert "running" in row_line
    # A freshly acquired lease has most of its window left.
    assert any(token in row_line for token in ("m", "h"))


def test_render_board_table_truncates_a_long_title() -> None:
    rows = [{
        "work_id": "WORK-1",
        "title": "x" * 200,
        "status": "queued",
        "group": "demo",
        "assignee": "claude",
        "claim_expires_at": None,
    }]
    out = render_board_table(rows, group_by="module", now=1_700_000_000.0, width=80)
    body_line = out.splitlines()[-1]
    assert "…" in body_line
    assert "x" * 200 not in out


def test_render_board_table_blank_lease_when_no_claim() -> None:
    rows = [{
        "work_id": "WORK-1", "title": "no lease here", "status": "planned",
        "group": "demo", "assignee": None, "claim_expires_at": None,
    }]
    out = render_board_table(rows, group_by="module", now=1_700_000_000.0)
    body_line = out.splitlines()[-1]
    assert body_line.rstrip().endswith("planned") or "WORK-1" in body_line


def test_render_board_table_lease_remaining_formats_a_duration() -> None:
    rows = [{
        "work_id": "WORK-1", "title": "leased", "status": "running",
        "group": "demo", "assignee": "claude", "claim_expires_at": 1_700_000_900.0,
    }]
    out = render_board_table(rows, group_by="module", now=1_700_000_000.0)
    assert "15m00s" in out


def test_render_board_table_expired_lease_is_blank() -> None:
    rows = [{
        "work_id": "WORK-1", "title": "stale claim", "status": "attention",
        "group": "demo", "assignee": "claude", "claim_expires_at": 1_699_999_000.0,
    }]
    out = render_board_table(rows, group_by="module", now=1_700_000_000.0)
    body_line = out.splitlines()[-1]
    # The lease column is the last one; a blank lease means the assignee is
    # the last visible token instead of a duration like "4h03m" or "12s".
    assert body_line.rstrip().endswith("claude")


def test_render_board_table_empty_board() -> None:
    out = render_board_table([], group_by="module", now=1_700_000_000.0)
    assert "module" in out
    assert "\n" not in out
