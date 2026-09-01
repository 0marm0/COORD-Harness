"""``coord lint-work-items`` is the driver the work-item lint never had.

The lint module was ported without one: nothing outside its own tests imported
it, so a board full of rows with no ``done_signal`` and no acceptance rubric
read exactly like a clean one, and the only thing that would have said so was a
module nobody could reach. These cases pin the three properties that make the
verb worth having: it is advisory by default, it goes red on demand, and a
board built only through ``coord create`` is clean under it -- the last one
matters most, because a gate whose green state no real board can reach is a
gate that gets waved through.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import cli
from coordharness.coord.config import connect


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(argv)
    return code, json.loads(buf.getvalue())


def _seed_well_formed(db: Path) -> None:
    """An initiative and a job under it, both written through the product path."""
    _run([
        "create", "DOCS-CLA-LINT-EPIC", "--db", str(db), "--surface", "epic",
        "--title", "Documentation custody initiative", "--module", "docs",
        "--done-signal", "docs/epic.md", "--acceptance", "the initiative is closed",
        "--note", "parent for the job below",
    ])
    _run([
        "create", "DOCS-CLA-LINT-DEMO", "--db", str(db),
        "--title", "Demonstrate the work-item lint", "--module", "docs",
        "--parent", "DOCS-CLA-LINT-EPIC",
        "--done-signal", "docs/demo.md", "--acceptance", "the document exists",
        "--note", "the clean baseline this lint is measured against",
    ])


def _plant_under_specified(db: Path) -> None:
    """A row with nothing but an id and a state, as an older writer would leave it."""
    conn = connect(str(db))
    now = time.time()
    conn.execute(
        "INSERT INTO work_items (work_id, surface, title, intent_state, acceptance_json,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("DOCS-CLA-LINT-PLANTED", "job", "DOCS-CLA-LINT-PLANTED", "queued", "[]", now, now),
    )
    conn.commit()
    conn.close()


def test_an_empty_board_is_clean_even_under_strict(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)

    code, payload = _run(["lint-work-items", "--db", str(db), "--strict"])

    assert code == 0
    assert payload["open_items"] == 0
    assert payload["malformed"] == 0


def test_a_board_built_through_coord_create_is_clean_under_strict(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    _seed_well_formed(db)

    code, payload = _run(["lint-work-items", "--db", str(db), "--strict"])

    assert code == 0, payload["findings"]
    assert payload["open_items"] == 2
    assert payload["malformed"] == 0


def test_an_under_specified_row_is_reported_without_refusing(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    _seed_well_formed(db)
    _plant_under_specified(db)

    code, payload = _run(["lint-work-items", "--db", str(db)])

    assert code == 0
    assert payload["malformed"] == 1
    finding = payload["findings"][0]
    assert finding["work_id"] == "DOCS-CLA-LINT-PLANTED"
    joined = " ".join(finding["missing"])
    assert "done_signal" in joined
    assert "acceptance" in joined


def test_strict_turns_the_same_finding_into_a_nonzero_exit(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    _seed_well_formed(db)
    _plant_under_specified(db)

    code, payload = _run(["lint-work-items", "--db", str(db), "--strict"])

    assert code == 1
    assert payload["malformed"] == 1


def test_counts_stay_complete_when_the_listing_is_truncated(tmp_path: Path) -> None:
    """``--limit`` bounds what is printed; it must not bound what is counted.

    A truncated listing that also truncated the count would let a malformed
    board report as a small problem, which is the failure mode a bounded
    report exists to avoid.
    """
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    _plant_under_specified(db)

    code, payload = _run(["lint-work-items", "--db", str(db), "--limit", "0"])

    assert code == 0
    assert payload["malformed"] == 1
    assert payload["shown"] == 0
    assert payload["findings"] == []


def test_near_identical_titles_in_one_module_are_surfaced(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    _seed_well_formed(db)
    conn = connect(str(db))
    now = time.time()
    conn.execute(
        "INSERT INTO work_items (work_id, parent_id, surface, module, title, display,"
        " assignee, intent_state, done_signal, acceptance_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "DOCS-CLA-LINT-DEMO-2", "DOCS-CLA-LINT-EPIC", "job", "docs",
            "Demonstrate the work-item lint", "Demonstrate the work-item lint",
            "claude", "queued", "docs/demo2.md", '["the document exists"]', now, now,
        ),
    )
    conn.commit()
    conn.close()

    code, payload = _run(["lint-work-items", "--db", str(db)])

    assert code == 0
    assert payload["possible_duplicates"] == 1
    pair = payload["duplicates"][0]
    assert {pair["work_id"], pair["other_work_id"]} == {
        "DOCS-CLA-LINT-DEMO", "DOCS-CLA-LINT-DEMO-2"
    }


def test_the_lint_does_not_write_to_the_database(tmp_path: Path) -> None:
    """A read-only question must stay read-only, planted findings included."""
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    _seed_well_formed(db)
    _plant_under_specified(db)
    before = db.read_bytes()

    _run(["lint-work-items", "--db", str(db), "--strict"])

    assert db.read_bytes() == before
