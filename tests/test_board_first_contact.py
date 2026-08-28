"""What the board says to someone who has never seen it.

Four of these are first-contact defects -- a traceback instead of an
instruction, empty-board copy that names the wrong control, an identity mark
with no legend, navigation kept in two disagreeing lists -- and one is the
substantive one: the detail plane described a task and never described the
coordination, which is the only thing this product records that a task tracker
does not.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from coordharness import demo
from coordharness.board import server as board_server
from coordharness.board.server import make_server


STATIC = Path(board_server.__file__).parent / "static"


def _static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. A first run has no database. That is the expected state, not a fault.
# ---------------------------------------------------------------------------


def test_board_without_a_database_names_the_command_that_seeds_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    code = board_server.main(["--port", "0"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "python -m coordharness.demo" in captured.err
    # The default path is derived from COORD_PROJECT_ROOT; a reader who does
    # not know that cannot tell which file the board went looking for.
    assert str(tmp_path) in captured.err


def test_board_with_a_database_still_starts(tmp_path: Path) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database))
    try:
        assert server.snapshot()["rows"]
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# 2. Dead navigation: one destination list, and no element nothing writes to.
# ---------------------------------------------------------------------------


def test_the_dead_rail_and_the_dead_activity_panel_are_gone() -> None:
    markup = _static("index.html")
    script = _static("app.js")
    assert 'id="rail"' not in markup
    assert 'id="activity"' not in markup
    assert "renderRail" not in script
    assert "#rail" not in script


def test_destinations_are_declared_once_in_the_shared_shell() -> None:
    shell = _static("shell.js")
    script = _static("app.js")
    # shell.js owns the list because it paints navigation on every page.
    assert "window.CoordNav" in shell
    assert "boardPanels" in shell
    # app.js reads it rather than restating it: no second literal list.
    assert "window.CoordNav" in script
    assert 'label:"Comms"' not in script
    assert 'group:"More"' not in script


def test_every_declared_board_panel_has_a_mount_point() -> None:
    shell = _static("shell.js")
    markup = _static("index.html")
    panels = set(re.findall(r'panel:\s*"([a-z]+)"', shell))
    assert panels, "shell.js declares no board panels"
    for panel in panels | {"usage"}:
        assert f'id="{panel}" class="panel' in markup, panel
    # And nothing is mounted that no destination can reach.
    mounted = set(re.findall(r'<section id="([a-z]+)" class="panel', markup))
    assert mounted == panels | {"usage"}


def test_the_capture_tool_no_longer_drives_a_hidden_element() -> None:
    tool = (Path(board_server.__file__).parents[3] / "tools" / "capture_board_screens.py")
    if not tool.exists():  # pragma: no cover - installed layouts omit tools/
        pytest.skip("tools/ is not part of this checkout")
    text = tool.read_text(encoding="utf-8")
    # The selector, not the word: the comment above the fix names the defect.
    assert "#rail button" not in text
    board_views = re.search(r"BOARD_VIEWS = \(([^)]*)\)", text).group(1)
    assert "activity" not in board_views
    # The one surface that shows coordination had never been captured.
    assert "comms" in board_views
    assert '"#comms' in text


# ---------------------------------------------------------------------------
# 3. The owner mark is the primary identity signal and had no legend.
# ---------------------------------------------------------------------------


def test_the_owner_legend_reuses_the_row_mark_function() -> None:
    script = _static("app.js")
    assert "OWNER_LEGEND" in script
    assert "ownerLegendHTML" in script
    # Drawn by calling ownerMark(), so a legend entry and a row mark cannot
    # disagree: one function decides both.
    assert "ownerMark(owner)" in script
    for lane in ("claude", "codex", "local", "local:gpu", "operator"):
        assert f'["{lane}",' in script


# ---------------------------------------------------------------------------
# 4. Contrast. --faint carries 11-12px text, including the id column.
# ---------------------------------------------------------------------------


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    first, second = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def test_faint_clears_the_normal_text_contrast_floor_on_every_ground() -> None:
    styles = _static("app.css")
    faint = re.search(r"--faint:(#[0-9a-f]{6})", styles).group(1)
    grounds = {
        name: re.search(rf"--{name}:(#[0-9a-f]{{3,6}})", styles).group(1)
        for name in ("c-bg", "c-panel", "c-card-a", "c-card-b", "c-hover")
    }
    grounds["c-bg"] = "#000000"
    failures = {
        name: round(_contrast(faint, value), 2)
        for name, value in grounds.items()
        if _contrast(faint, value) < 4.5
    }
    assert not failures, f"{faint} under the 4.5:1 floor on {failures}"


# ---------------------------------------------------------------------------
# 5. The headline: coordination attached to the row it happened on.
# ---------------------------------------------------------------------------


def test_exchanges_are_keyed_from_per_row_events_not_the_board_wide_window(
    tmp_path: Path,
) -> None:
    """The plane's population must be TimelineV1, not PulseV1's `recent`.

    `recent` publishes the newest twelve events board-wide. On the seeded board
    that names ten of the twenty-six rows that have events, and it excludes
    both the handoff and the audit exchange a reader is most likely to open. A
    plane built on it renders empty exactly where it matters most, so this
    asserts the gap rather than trusting the source.
    """
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database))
    try:
        timeline = server.timeline()
        pulse = server.pulse()
    finally:
        server.server_close()

    windowed = {event["row"] for event in pulse["recent"]}
    with_events = {item["id"] for item in timeline["items"] if item["events"]}
    assert with_events - windowed, "the recent window happens to cover every row here"

    by_row = {item["id"]: item["events"] for item in timeline["items"]}
    handoffs = {
        row for row, events in by_row.items()
        if any(event["kind"] == "handoff" for event in events)
    }
    audits = {
        row for row, events in by_row.items()
        if any(event["kind"] == "audit_verdict" for event in events)
    }
    assert handoffs and audits
    # The rows whose coordination the plane exists to show are reachable from
    # the timeline; at least one of them is outside the recent window.
    assert (handoffs | audits) - windowed
