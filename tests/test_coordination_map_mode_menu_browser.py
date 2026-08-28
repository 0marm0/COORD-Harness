from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server


playwright_api = pytest.importorskip("playwright.sync_api")

DIRECT_IDS = ["tab-fleet", "tab-pulse", "tab-deps", "map-more-trigger"]
DIRECT_LABELS = ["Fleet", "Pulse", "Dependencies"]
MORE_LABELS = [
    "Stations",
    "Ceiling",
    "Lanes",
    "Shape",
    "Crossings",
    "Order",
    "Subjects",
    "Orbit",
    "Context",
]


@contextmanager
def _board(tmp_path: Path):
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(
        host="127.0.0.1",
        port=0,
        db_path=str(database),
        refresh_interval=3600,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait_for_map(page) -> None:
    page.wait_for_function(
        "() => document.querySelector('#mapmeta')?.textContent.includes('cache generation')"
    )


@pytest.mark.parametrize(("width", "height"), ((1280, 720), (390, 844)))
def test_map_mode_menu_is_compact_keyboard_complete_and_deep_linkable(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        errors: list[str] = []
        page.on(
            "console",
            lambda message: errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)

            direct = page.locator("#maptabs [role=tab]")
            assert direct.evaluate_all(
                "controls => controls.map(control => control.id)"
            ) == DIRECT_IDS
            assert direct.nth(0).inner_text() == DIRECT_LABELS[0]
            assert direct.nth(1).inner_text() == DIRECT_LABELS[1]
            assert direct.nth(2).inner_text() == DIRECT_LABELS[2]
            assert page.locator("#map-more-trigger .map-more-label").inner_text() == "More"
            assert all(direct.nth(index).is_visible() for index in range(4))

            radios = page.locator("#map-more-menu [role=menuitemradio]")
            assert radios.count() == 9
            assert radios.all_inner_texts() == MORE_LABELS
            assert all(radios.nth(index).is_hidden() for index in range(9))

            more = page.locator("#map-more-trigger")
            more.focus()
            page.keyboard.press("ArrowDown")
            assert page.locator("#map-more-menu").is_visible()
            assert page.locator("#tab-flowpath").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.press("End")
            assert page.locator("#tab-context").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.press("Home")
            assert page.locator("#tab-flowpath").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.press("Escape")
            assert page.locator("#map-more-menu").is_hidden()
            assert more.evaluate("element => element === document.activeElement")

            page.keyboard.press("ArrowDown")
            page.keyboard.press("End")
            page.keyboard.press("Enter")
            assert page.locator("#context").get_attribute("hidden") is None
            assert page.locator("#tab-context").get_attribute("aria-checked") == "true"
            assert more.get_attribute("aria-selected") == "true"
            assert more.locator(".map-more-label").inner_text() == "More · Context"
            assert "lens=context" in page.url

            page.goto(f"{url}/map?lens=shape", wait_until="networkidle")
            _wait_for_map(page)
            assert page.locator("#tab-shape").get_attribute("aria-checked") == "true"
            assert page.locator("#shape").get_attribute("hidden") is None
            assert page.locator("#map-more-trigger .map-more-label").inner_text() == "More · Shape"

            page.goto(f"{url}/map#lens=orbit", wait_until="networkidle")
            _wait_for_map(page)
            assert page.locator("#tab-orbit").get_attribute("aria-checked") == "true"
            assert page.locator("#orbit").get_attribute("hidden") is None
            assert page.locator("#map-more-trigger .map-more-label").inner_text() == "More · Orbit"
            assert page.evaluate(
                "() => ({viewport: innerWidth, body: document.body.scrollWidth, "
                "document: document.documentElement.scrollWidth})"
            ) == {"viewport": width, "body": width, "document": width}
        finally:
            browser.close()
        assert not errors, errors
