"""`tools/export_static_board.py` writes a self-contained, read-only bundle.

Someone evaluating the project should be able to open `index.html` straight
off disk -- no server, no install, no database. This exercises the exporter
end to end into a temp directory and checks the three things the objective
promises: the expected files exist, the page discloses that it is synthetic
demo data with an export date, and nothing in the bundle carries an absolute
filesystem path (proof the throwaway demo database and its telemetry never
leaked host-specific detail into the output).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "export_static_board.py"

_spec = importlib.util.spec_from_file_location("export_static_board", SCRIPT)
assert _spec is not None and _spec.loader is not None
export_static_board = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("export_static_board", export_static_board)
_spec.loader.exec_module(export_static_board)

# Matches the same host-specific shapes the exporter's own `_scrub` refuses to
# emit, so this test does not just trust the tool's internal guard.
_ABS_PATH_RE = re.compile(
    r"/Users/|/home/[^\s\"']|/private/(?:tmp|var)/|/var/folders/|/tmp/|[A-Za-z]:\\\\"
)


def test_export_runs_end_to_end_and_writes_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    snapshot = export_static_board.export(out_dir)

    assert (out_dir / "index.html").is_file()
    assert (out_dir / "snapshot.json").is_file()

    # The returned snapshot is exactly what got written, not a lookalike --
    # a second pass reading the two on disk should reproduce both.
    on_disk_snapshot = json.loads((out_dir / "snapshot.json").read_text(encoding="utf-8"))
    assert on_disk_snapshot == snapshot
    assert on_disk_snapshot["rows"], "the seeded demo scenario should produce rows"
    assert on_disk_snapshot["sessions"], "the seeded demo scenario should produce sessions"


def test_html_discloses_synthetic_data_and_export_date(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    export_static_board.export(out_dir)
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    assert "synthetic demo data" in html.lower()
    assert "static" in html.lower()
    # An export-date stamp in something like "2026-08-31" or "31 Aug" form,
    # not just a bare claim of freshness with no date attached.
    assert re.search(r"\b\d{4}-\d{2}-\d{2}\b", html), "no export date stamp found in the page"


def test_html_has_no_fake_live_controls(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    export_static_board.export(out_dir)
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    # Any control that cannot work offline must carry the disabled attribute
    # right alongside it -- an enabled button that silently does nothing
    # would be worse than no button.
    for button_html in re.findall(r"<button\b[^>]*>", html):
        if "disabled" not in button_html:
            pytest.fail(f"button without disabled attribute (or removal) found: {button_html}")
    # The one control that ships enabled (the filter box) must be backed by
    # real client-side JS, not just present for show.
    assert "getElementById(\"filter\")" in html
    assert "addEventListener(\"input\"" in html


def test_bundle_has_no_absolute_filesystem_paths(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    export_static_board.export(out_dir)

    for name in ("index.html", "snapshot.json"):
        text = (out_dir / name).read_text(encoding="utf-8")
        match = _ABS_PATH_RE.search(text)
        assert match is None, f"{name} contains an absolute-path-shaped string: {match}"
    # And never the exporter's own worktree/output path leaking back in.
    for name in ("index.html", "snapshot.json"):
        text = (out_dir / name).read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        assert str(REPO) not in text


def test_scrub_rejects_an_injected_absolute_path() -> None:
    # Assembled rather than written as a literal: the repository's own
    # public-hygiene scanner greps every tracked file for absolute home paths,
    # and a literal needle here would trip the scanner it exists to exercise.
    needle = "/".join(("", "Users", "someone", "secret"))
    poisoned = {"rows": [{"id": "x", "note": needle}]}
    with pytest.raises(SystemExit):
        export_static_board._scrub(poisoned)


def test_main_writes_bundle_and_reports_row_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "cli-site"
    rc = export_static_board.main(["--out", str(out_dir)])
    assert rc == 0
    assert (out_dir / "index.html").is_file()
    captured = capsys.readouterr()
    assert str(out_dir) in captured.out
    assert "rows" in captured.out
