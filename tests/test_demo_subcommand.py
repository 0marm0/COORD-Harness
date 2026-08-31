"""`coord demo` wraps the same seeder `python -m coordharness.demo` always has.

Measured defect: the only documented way to populate a fictional board was
`python -m coordharness.demo` -- the one quickstart line in the README that
does not look like the rest of the product, which is otherwise `coord
<verb>` throughout -- and `demo.py`'s own docstring promised a `coord demo
seed` subcommand that did not exist. This adds `coord demo [--db PATH]
[--quiet]` as a thin subparser over `coordharness.demo.seed`, printing
exactly what the module form prints, and keeps `python -m coordharness.demo`
working unchanged for anything that already invokes it that way.
"""

from __future__ import annotations

import contextlib
import io
import runpy
import sys
from pathlib import Path

import pytest

from coordharness.coord import cli
from coordharness.coord.config import connect_ro


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_coord_demo_seeds_a_full_fictional_board(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    rc, out = _run(["demo", "--db", str(db)])
    assert rc == 0
    assert db.exists()
    assert f"seeding demo board at {db}" in out

    conn = connect_ro(db)
    try:
        rows = conn.execute("SELECT work_id FROM work_items").fetchall()
    finally:
        conn.close()
    work_ids = {str(r["work_id"]) for r in rows}
    # A handful of rows from the fixed fictional scenario -- proof this ran
    # the real seeder, not a stub.
    assert {"UI-101", "ML-201", "PLT-301", "INIT-UI"} <= work_ids


def test_coord_demo_db_before_the_subcommand_also_works(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    rc, out = _run(["--db", str(db), "demo"])
    assert rc == 0
    assert db.exists()
    assert f"seeding demo board at {db}" in out


def test_coord_demo_quiet_suppresses_the_seed_counts(tmp_path: Path) -> None:
    loud_db = tmp_path / "loud.db"
    rc, loud_out = _run(["demo", "--db", str(loud_db)])
    assert rc == 0
    # The loud run reports at least the row-count lines `seed()` prints.
    assert "work_items" in loud_out

    quiet_db = tmp_path / "quiet.db"
    rc, quiet_out = _run(["demo", "--db", str(quiet_db), "--quiet"])
    assert rc == 0
    assert "seeding demo board at" in quiet_out
    assert "work_items" not in quiet_out


def test_coord_demo_output_matches_the_module_form(tmp_path: Path) -> None:
    """`coord demo` and `python -m coordharness.demo` print the same thing.

    Not letter-for-letter identical (each names its own db path), but the
    same message shape and the same per-table seed counts -- proof this is
    one seeder behind two doors, not two behaviors that can drift apart.
    """
    cli_db = tmp_path / "cli.db"
    rc, cli_out = _run(["demo", "--db", str(cli_db)])
    assert rc == 0

    module_db = tmp_path / "module.db"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        from coordharness import demo as demo_module

        exit_code = demo_module.main(["--db", str(module_db)])
    module_out = buf.getvalue()
    assert exit_code == 0

    def _shape(text: str, db_path: Path) -> str:
        return text.replace(str(db_path), "<DB>")

    assert _shape(cli_out, cli_db) == _shape(module_out, module_db)


def test_python_dash_m_coordharness_demo_still_works(tmp_path: Path, monkeypatch) -> None:
    """The module entry point this replaces stays a valid way to invoke it."""
    db = tmp_path / "module_invocation.db"
    monkeypatch.setattr(sys, "argv", ["coordharness.demo", "--db", str(db)])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("coordharness.demo", run_name="__main__")
    assert excinfo.value.code == 0
    assert db.exists()
    assert f"seeding demo board at {db}" in buf.getvalue()
