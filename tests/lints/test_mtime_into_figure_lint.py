from __future__ import annotations

from pathlib import Path

import pytest

from coordharness.lints import mtime_into_figure_lint as lint


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_direct_as_of_producer_call_is_flagged(tmp_path: Path) -> None:
    source = "def render():\n    Figure.figure(as_of=as_of())\n"
    path = _write(tmp_path, source)

    rows = lint.scan_file(path, tmp_path)

    assert len(rows) == 1
    assert rows[0]["call"] == "Figure.figure"
    assert rows[0]["func"] == "render"


def test_tainted_variable_propagates_into_flag(tmp_path: Path) -> None:
    source = "def render():\n    stamp = as_of()\n    Figure.figure(as_of=stamp)\n"
    path = _write(tmp_path, source)

    rows = lint.scan_file(path, tmp_path)

    assert len(rows) == 1
    assert rows[0]["as_of_expr"] == "stamp"


def test_constant_as_of_kwarg_is_not_flagged(tmp_path: Path) -> None:
    source = "def render():\n    Figure.figure(as_of=FIXED_TIMESTAMP)\n"
    path = _write(tmp_path, source)

    assert lint.scan_file(path, tmp_path) == []


def test_call_to_unrelated_function_is_not_flagged(tmp_path: Path) -> None:
    source = "def render():\n    other_thing(as_of=as_of())\n"
    path = _write(tmp_path, source)

    assert lint.scan_file(path, tmp_path) == []


def test_scan_file_on_empty_file_returns_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, "")

    assert lint.scan_file(path, tmp_path) == []


def test_scan_file_on_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        lint.scan_file(tmp_path / "missing.py", tmp_path)
