from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server


playwright_api = pytest.importorskip("playwright.sync_api")


@contextmanager
def _board(tmp_path: Path):
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(host="127.0.0.1", port=0, db_path=str(database))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _assert_panel(page, name: str, *, open_: bool) -> None:
    button = page.locator(f"#{name}btn")
    panel = page.locator(f"#{name}panel")
    assert panel.is_hidden() is not open_
    assert button.get_attribute("aria-expanded") == str(open_).lower()


def test_transient_panel_shortcuts_guards_and_operations_atlas_destination(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            page.goto(url, wait_until="networkidle")
            page.get_by_role("navigation", name="Work views").get_by_role(
                "link", name="Overview", exact=True
            ).wait_for()

            page.keyboard.press("f")
            _assert_panel(page, "filter", open_=True)
            _assert_panel(page, "display", open_=False)

            page.keyboard.press("Shift+V")
            _assert_panel(page, "filter", open_=False)
            _assert_panel(page, "display", open_=True)

            page.keyboard.press("Escape")
            _assert_panel(page, "filter", open_=False)
            _assert_panel(page, "display", open_=False)

            page.locator("#filterbtn").click()
            page.locator("#displaybtn").click()
            _assert_panel(page, "filter", open_=False)
            _assert_panel(page, "display", open_=True)

            find = page.locator("#find")
            find.focus()
            page.keyboard.press("f")
            page.keyboard.press("Shift+V")
            _assert_panel(page, "filter", open_=False)
            _assert_panel(page, "display", open_=True)
            assert find.input_value() == "fV"

            page.keyboard.press("Escape")
            _assert_panel(page, "display", open_=False)
            assert find.evaluate("element => element === document.activeElement")
            page.keyboard.press("Escape")
            assert not find.evaluate("element => element === document.activeElement")

            page.keyboard.press("Control+f")
            page.keyboard.press("Alt+f")
            page.keyboard.press("Control+Shift+V")
            _assert_panel(page, "filter", open_=False)
            _assert_panel(page, "display", open_=False)

            page.keyboard.press("Control+k")
            palette = page.locator("#cmdk-list")
            atlas = palette.get_by_text("Open Operations Atlas", exact=True)
            assert atlas.count() == 1
            assert palette.get_by_text("Open Atlas", exact=True).count() == 0
            atlas.click()
            page.wait_for_url(f"{url}/ops")
        finally:
            browser.close()
