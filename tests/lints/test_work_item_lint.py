from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from coordharness.lints import work_item_lint as lint


def _row(**overrides: str) -> lint.Row:
    base = dict(
        work_id="WORK-1",
        parent_id="INIT-1",
        surface="job",
        module="storage",
        sublane="",
        title="Do the thing",
        display="Do the thing",
        assignee="claude",
        intent_state="queued",
        done_signal="artifacts/out.json",
        acceptance_json=json.dumps(["artifact exists"]),
    )
    base.update(overrides)
    return lint.Row(**base)


# --- lint() -----------------------------------------------------------------


def test_lint_passes_a_fully_specified_row() -> None:
    assert lint.lint([_row()]) == []


def test_lint_flags_row_with_title_defaulted_to_work_id() -> None:
    row = _row(title="WORK-1")

    findings = lint.lint([row])

    assert len(findings) == 1
    assert findings[0].work_id == "WORK-1"
    assert any("title" in reason for reason in findings[0].missing)


def test_lint_flags_row_missing_module_and_empty_acceptance() -> None:
    row = _row(module="", acceptance_json="[]")

    findings = lint.lint([row])

    assert len(findings) == 1
    reasons = " ".join(findings[0].missing)
    assert "module" in reasons
    assert "acceptance" in reasons


def test_lint_epic_surface_does_not_require_parent_or_done_signal() -> None:
    row = _row(surface="epic", parent_id="", done_signal="")

    assert lint.lint([row]) == []


def test_lint_on_empty_input_returns_no_findings() -> None:
    assert lint.lint([]) == []


# --- dups() -------------------------------------------------------------


def test_dups_flags_near_identical_titles_in_the_same_module() -> None:
    a = _row(work_id="WORK-1", title="Migrate the storage ledger to B2")
    b = _row(work_id="WORK-2", title="Migrate the storage ledger to B2 now")

    pairs = lint.dups([a, b])

    assert len(pairs) == 1
    assert {pairs[0][0], pairs[0][1]} == {"WORK-1", "WORK-2"}


def test_dups_does_not_flag_dissimilar_titles(tmp_path: Path) -> None:
    a = _row(work_id="WORK-1", title="Migrate the storage ledger to B2")
    b = _row(work_id="WORK-2", title="Rewrite the CLI help text")

    assert lint.dups([a, b]) == []


def test_dups_does_not_compare_across_modules() -> None:
    a = _row(work_id="WORK-1", module="storage", title="Migrate the storage ledger to B2")
    b = _row(work_id="WORK-2", module="docs", title="Migrate the storage ledger to B2")

    assert lint.dups([a, b]) == []


def test_dups_on_empty_input_returns_no_pairs() -> None:
    assert lint.dups([]) == []


# --- main() / _load() against a real coord.db schema ------------------------


def _make_coord_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE work_items (
            work_id TEXT PRIMARY KEY,
            parent_id TEXT,
            surface TEXT,
            module TEXT,
            sublane TEXT,
            title TEXT,
            display TEXT,
            assignee TEXT,
            intent_state TEXT,
            done_signal TEXT,
            acceptance_json TEXT,
            archived_at TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("WORK-1", "", "job", "", "", "", "", "", "queued", "", "[]", None),
    )
    con.execute(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "WORK-2",
            "INIT-1",
            "job",
            "storage",
            "",
            "Real title",
            "Real title",
            "claude",
            "done",
            "artifacts/x.json",
            json.dumps(["ok"]),
            "2026-01-01",
        ),
    )
    con.commit()
    con.close()


def test_load_reads_only_open_non_archived_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "coord.db"
    _make_coord_db(db_path)

    rows = lint._load(str(db_path))

    assert [r.work_id for r in rows] == ["WORK-1"]


def test_main_exits_nonzero_in_strict_mode_with_malformed_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "coord.db"
    _make_coord_db(db_path)
    monkeypatch.setattr("sys.argv", ["work_item_lint", "--db", str(db_path), "--strict"])

    exit_code = lint.main()

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "WORK-1" in out
