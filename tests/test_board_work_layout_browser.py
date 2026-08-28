from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server
from coordharness.coord import coord_db
from coordharness.coord.config import connect

playwright_api = pytest.importorskip("playwright.sync_api")


@contextmanager
def _board(tmp_path: Path, *, extra_events: int = 0):
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    if extra_events:
        conn = connect(database)
        try:
            for index in range(extra_events):
                coord_db.post_event(
                    conn,
                    kind="heartbeat",
                    actor="codex",
                    work_id="ML-202",
                    idempotency_key=f"browser-timeline-{index}",
                )
            conn.commit()
        finally:
            conn.close()
    server = make_server(host="127.0.0.1", port=0, db_path=str(database))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _row_ids(page):
    return page.locator("#work [data-row]").evaluate_all(
        "elements => elements.map(element => element.dataset.row)"
    )


def test_persistent_modes_share_population_selection_sort_and_csp_progress(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script("localStorage.removeItem('coord.display')")
        try:
            page.goto(f"{url}/#v=work", wait_until="networkidle")
            page.locator('.kanban-lane[data-lane="running"] .workcard').first.wait_for()
            assert page.locator("#workmodes").is_visible()
            assert page.locator("#workmodes button").all_inner_texts() == [
                "List 1",
                "Cards 2",
                "Timeline 3",
            ]
            board_ids = _row_ids(page)
            membership = set(board_ids)
            assert (
                page.locator('#workmodes [data-work-layout="board"]').get_attribute("aria-pressed")
                == "true"
            )
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('layout')")
                == "board"
            )
            lane_labels = page.locator(".kanban-lane > h3 > span").evaluate_all(
                "elements => elements.map(element => element.textContent.trim())"
            )
            assert lane_labels[:4] == ["Running", "Blocked", "Queued / Next", "Done / Recent"]
            assert "Needs attention" in lane_labels
            lane_counts = page.locator(".kanban-lane").evaluate_all(
                "elements => elements.map(element => Number(element.dataset.rowCount))"
            )
            assert sum(lane_counts) == len(board_ids)
            assert page.locator(".kanban").get_attribute("data-population-count") == str(
                len(board_ids)
            )
            assert page.locator(".kanban").evaluate(
                "element => element.scrollWidth >= element.clientWidth"
            )
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            card = page.locator(".workcard").first
            assert card.locator(".workcard-title").count() == 1
            assert card.locator(".workcard-byline code").count() == 1
            assert card.locator(".workcard-owner").count() == 1
            assert card.locator(".workcard-signals").count() == 1
            assert page.locator(".workcard p, .workcard-step").count() == 0
            assert "scoring candidate checkpoints" not in page.locator("#work").inner_text()
            assert page.locator("#work [draggable=true]").count() == 0

            truth = page.locator(".work-contract")
            assert truth.count() == 1
            assert truth.get_attribute("open") is None
            assert not page.locator(".work-contract-copy").is_visible()
            assert "claim fences" not in page.locator("#work").inner_text()
            truth.locator("summary").click()
            assert page.locator(".work-contract-copy").is_visible()
            assert "claim fences" in page.locator(".work-contract-copy").inner_text()
            truth.locator("summary").click()

            progress_values = page.locator(".workcard progress[value]").evaluate_all(
                "elements => elements.map(element => Number(element.value)).filter(value => value > 0)"
            )
            assert progress_values

            page.locator(".kanban").focus()
            page.keyboard.press("j")
            assert page.locator("#detail").is_visible()
            selected_id = page.locator(".workcard.sel").get_attribute("data-row")
            assert selected_id in page.locator("#detail").inner_text()
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('sel')")
                == selected_id
            )
            assert page.locator(".split.showdetail").count() == 1
            page.locator("#dclose").click()

            page.locator('#workmodes [data-work-layout="list"]').click()
            page.locator(".worktable").wait_for()
            assert set(_row_ids(page)) == membership
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('layout')") == "list"
            )
            page.reload(wait_until="networkidle")
            page.locator(".worktable").wait_for()
            assert (
                page.locator('#workmodes [data-work-layout="list"]').get_attribute("aria-pressed")
                == "true"
            )

            page.keyboard.press("2")
            page.locator(".kanban").wait_for()
            assert set(_row_ids(page)) == membership
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('layout')")
                == "board"
            )
            page.locator("#filterbtn").click()
            assert page.locator("#filterpanel").is_visible()
            page.keyboard.press("Escape")
            page.locator("#displaybtn").click()
            page.locator('[data-sort-opt="priority"]').click()
            assert set(_row_ids(page)) == membership
            assert '"sort":"priority"' in page.evaluate("localStorage.getItem('coord.display')")
            assert page.evaluate("location.hash").startswith("#v=work&layout=board")
        finally:
            browser.close()


def test_timeline_uses_bundle_metadata_newest_first_caps_and_shortcut_three(tmp_path: Path) -> None:
    with _board(tmp_path, extra_events=300) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            page.goto(f"{url}/#v=work", wait_until="networkidle")
            list_ids = set(_row_ids(page))
            page.keyboard.press("3")
            page.locator(".timeline-receipt").wait_for()
            timeline_ids = set(_row_ids(page))
            assert timeline_ids and timeline_ids <= list_ids
            receipt = page.locator(".timeline-receipt")
            assert receipt.get_attribute("data-timeline-shown") == "240"
            assert "capped" in receipt.inner_text()
            assert "Metadata source: coord.db" in receipt.inner_text()
            assert "event bodies are not published" in receipt.inner_text()
            timestamps = page.locator(".timeline-event").evaluate_all(
                "elements => elements.map(element => Date.parse(element.dataset.at))"
            )
            assert timestamps == sorted(timestamps, reverse=True)
            assert page.locator(".timeline-day").count() >= 1

            event = page.locator(".timeline-event").first
            event_id = event.get_attribute("data-row")
            event.click()
            assert event_id in page.locator("#detail").inner_text()
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('sel')") == event_id
            )
            assert page.locator(".timeline-event.sel").count() >= 1
        finally:
            browser.close()


def test_graph_click_and_keyboard_share_url_selection_and_detail(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            page.goto(f"{url}/#v=graph", wait_until="networkidle")
            assert page.locator("#rail").is_hidden()
            node = page.locator(".gnode.selectable[data-row]").first
            node.wait_for()
            selected_id = node.get_attribute("data-row")
            node.click(force=True)
            assert selected_id in page.locator("#detail").inner_text()
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('sel')")
                == selected_id
            )
            assert page.locator(".gnode.selectable.sel").count() == 1
            assert page.locator(".gnode.graph-only[data-row]").count() == 0

            keyboard_node = page.locator(".gnode.selectable[data-row]").nth(1)
            keyboard_id = keyboard_node.get_attribute("data-row")
            keyboard_node.focus()
            page.keyboard.press("Enter")
            assert keyboard_id in page.locator("#detail").inner_text()
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('sel')")
                == keyboard_id
            )
        finally:
            browser.close()


def test_timeline_is_compact_safe_and_404_compatibility_is_honest(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.add_init_script("localStorage.removeItem('coord.display')")
        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route(
            "**/api/v1/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/timeline", lambda route: route.fulfill(status=404, body="{}"))
        try:
            page.goto(f"{url}/#v=work", wait_until="networkidle")
            page.locator(".kanban").wait_for()
            assert page.locator(".kanban-lane").count() >= 5
            assert page.locator(".kanban").evaluate(
                "element => element.scrollWidth > element.clientWidth"
            )
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert page.locator(".kanban").evaluate(
                "element => element.getBoundingClientRect().right <= innerWidth + 1"
            )
            first_lane = page.locator('.kanban-lane[data-lane="running"]')
            assert first_lane.evaluate(
                "element => element.getBoundingClientRect().right <= innerWidth + 1"
            )
            assert first_lane.locator(".workcard").first.evaluate(
                "element => element.getBoundingClientRect().height <= 110"
            )
            first_lane.locator(".workcard").first.click()
            page.locator("#detail").wait_for()
            assert page.locator("#detail").evaluate(
                "element => { const box=element.getBoundingClientRect(); "
                "return box.left >= -1 && box.right <= innerWidth + 1; }"
            )
            page.locator("#dclose").click()

            page.keyboard.press("3")
            empty = page.locator(".timeline-empty")
            empty.wait_for()
            assert "Timeline unavailable in compatibility mode" in empty.inner_text()
            assert "no event times are inferred" in empty.inner_text()
            assert "COMPATIBILITY READ" in page.locator("#boardreadalert").inner_text()
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert (
                page.evaluate("new URLSearchParams(location.hash.slice(1)).get('layout')")
                == "timeline"
            )
            assert page.locator("#workmodes").evaluate(
                "element => element.getBoundingClientRect().right <= innerWidth + 1"
            )
        finally:
            browser.close()


def test_static_read_only_mode_contracts():
    static = Path(__file__).parents[1] / "src/coordharness/board/static"
    source = (static / "app.js").read_text()
    styles = (static / "app.css").read_text()
    board = source[source.index("function renderWorkBoard()") : source.index("const TIMELINE_CAP")]
    assert "shown.forEach(row=>rendered.push(row.id))" in board
    assert "STATE.order=rendered" in board
    assert "STATE.order=rows.map" not in board
    assert "current_step" not in board
    assert "draggable=" not in board
    assert "<progress" in source
    assert 'layout","board"' not in source  # layout is state, never a hard-coded capsule rewrite
    assert 'raw.set("layout",DISPLAY.layout)' in source
    assert "TIMELINE_CAP=240" in source
    assert "bundle.timeline" in source
    assert "data-w=" in source
    assert '<i style="width:' not in source
    assert "drag-to-status" in source
    assert "drag-to-lifecycle mutation" in source
    assert 'body[data-view="work"][data-work-layout="board"] .panes' in styles
    assert ".work-board-shell" in styles
    assert "overflow-x:auto" in styles
    assert "function graphRowId(node)" in source
    assert '" graph-only"' in source
    assert 'data-graph-only="true"' in source
    assert "rowId?` data-row=" in source
