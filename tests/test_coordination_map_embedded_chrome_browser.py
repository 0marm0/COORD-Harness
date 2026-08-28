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
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    server = make_server(
        host="127.0.0.1",
        port=0,
        db_path=str(db),
        refresh_interval=3600,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _geometry(page) -> dict:
    return page.evaluate(
        """() => {
          const rect = selector => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return {top: box.top, bottom: box.bottom, left: box.left, right: box.right};
          };
          return {
            tabs: rect('#maptabs'),
            search: rect('#global-search'),
            shell: rect('.shellbar'),
            widths: {
              viewport: innerWidth,
              document: document.documentElement.scrollWidth,
              body: document.body.scrollWidth,
            },
          };
        }"""
    )


@pytest.mark.parametrize(("width", "height"), ((1600, 1000), (1280, 720), (390, 844)))
def test_embedded_map_rail_starts_at_native_chrome_edge_without_covering_search(
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
            page.goto(f"{url}/map?embedded=1", wait_until="networkidle")
            page.locator("#fleet .matrix").wait_for()
            embedded = _geometry(page)
            assert page.locator("html").get_attribute("data-embedded") == "1"
            assert "embedded" not in (page.locator("body").get_attribute("class") or "").split()
            assert embedded["tabs"]["top"] <= 1
            assert embedded["search"]["top"] >= embedded["tabs"]["bottom"]
            assert embedded["widths"] == {
                "viewport": width,
                "document": width,
                "body": width,
            }

            page.goto(f"{url}/map", wait_until="networkidle")
            page.locator("#fleet .matrix").wait_for()
            standalone = _geometry(page)
            assert standalone["shell"]["bottom"] == pytest.approx(58, abs=0.5)
            assert standalone["tabs"]["top"] >= standalone["shell"]["bottom"]
            assert standalone["widths"] == embedded["widths"]
        finally:
            browser.close()
        assert errors == []
