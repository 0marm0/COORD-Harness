"""What a reader actually sees on the repaired board.

Browser leg of tests/test_board_first_contact.py. Skipped wholesale when
Playwright is absent, which is why nothing here is the only assertion for a
behaviour: the contract each one renders is also asserted headlessly there.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server

playwright_api = pytest.importorskip("playwright.sync_api")

@contextmanager
def _board(database: Path):
    server = make_server(host="127.0.0.1", port=0, db_path=str(database))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _page(url: str):
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#health").get_by_text("Live · gen", exact=False).wait_for()
            yield page, errors
        finally:
            browser.close()


def _row_with(database: Path, kind: str) -> str:
    server = make_server(port=0, db_path=str(database))
    try:
        timeline = server.timeline()
    finally:
        server.server_close()
    for item in timeline["items"]:
        if any(event["kind"] == kind for event in item["events"]):
            return item["id"]
    raise AssertionError(f"no seeded row carries a {kind}")


def _row_without_events(database: Path) -> str:
    server = make_server(port=0, db_path=str(database))
    try:
        snapshot = server.snapshot()
        timeline = server.timeline()
    finally:
        server.server_close()
    noisy = {item["id"] for item in timeline["items"] if item["events"]}
    for row in snapshot["rows"]:
        if row["id"] not in noisy:
            return row["id"]
    raise AssertionError("every seeded row carries an event")


def test_the_detail_plane_shows_the_handoff_that_happened_on_the_row(
    tmp_path: Path,
) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    row = _row_with(database, "handoff")
    with _board(database) as url, _page(f"{url}/#v=work&layout=list&sel={row}") as (page, errors):
        page.goto(f"{url}/#v=work&layout=list&sel={row}", wait_until="networkidle")
        plane = page.locator(".dplane", has=page.locator("h3", has_text="Exchanges"))
        plane.wait_for()
        text = plane.inner_text()
        assert "handoff" in text.lower()
        assert "recorded act" in text
        # Actor, and the destination where the projection publishes one.
        assert plane.locator(".dexch").count() >= 1
        assert not errors, errors


def test_a_row_with_no_coordination_says_so_rather_than_looking_broken(
    tmp_path: Path,
) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    row = _row_without_events(database)
    with _board(database) as url, _page(url) as (page, errors):
        page.goto(f"{url}/#v=work&layout=list&sel={row}", wait_until="networkidle")
        plane = page.locator(".dplane", has=page.locator("h3", has_text="Exchanges"))
        plane.wait_for()
        text = plane.inner_text()
        assert "No coordination act is recorded against this row" in text
        assert "That is the record, not a gap in it." in text
        assert plane.locator(".dexch").count() == 0
        assert not errors, errors


def test_the_owner_legend_renders_marks_and_their_meanings(tmp_path: Path) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    with _board(database) as url, _page(url) as (page, errors):
        legend = page.locator(".marklegend")
        legend.wait_for()
        text = legend.inner_text()
        for meaning in ("chat agent", "code agent", "local compute",
                        "local accelerator", "a person, not an agent"):
            assert meaning in text
        assert legend.locator(".mark").count() >= 4
        assert not errors, errors


def test_an_empty_board_names_the_command_that_fills_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordharness.bootstrap import bootstrap_database

    database = tmp_path / "coord.db"
    bootstrap_database(database)
    # An empty database is not by itself an empty board. Job telemetry is read
    # from ``config.job_progress_dir()``, which resolves from COORD_HOME or the
    # project root and never from the database being served -- so a board opened
    # on a fresh database still shows whatever sidecars happen to sit in the
    # ambient state tree, and the empty state is unreachable while any exist.
    # Point COORD_HOME at this test's own directory so both halves of the board
    # come from the same place. The mixing itself is a real defect in the
    # product, tracked separately; this test must not depend on it either way.
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "state"))
    with _board(database) as url, _page(url) as (page, errors):
        page.goto(f"{url}/#v=work&layout=list", wait_until="networkidle")
        empty = page.locator("#work .empty")
        empty.wait_for()
        text = empty.inner_text()
        assert "This board has no rows yet." in text
        assert "python -m coordharness.demo" in text
        assert "Clear Find" not in text
        assert not errors, errors


def test_no_navigation_element_is_painted_that_nobody_can_see(tmp_path: Path) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    with _board(database) as url, _page(url) as (page, errors):
        assert page.locator("#rail").count() == 0
        assert page.locator("#activity").count() == 0
        # The one visible switcher lists exactly the declared destinations.
        labels = page.locator(".shell-subnav a").all_inner_texts()
        assert labels == ["Overview", "Board", "Attention", "Jobs", "Graph", "Comms"]
        crumbs = json.dumps(page.locator("#crumbs").inner_text())
        assert "Overview" in crumbs
        assert not errors, errors
