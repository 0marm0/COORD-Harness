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


def _wait_for_mesh(page) -> None:
    page.locator("#mesh-canvas").wait_for(state="visible", timeout=5_000)
    page.locator(".cluster-row").first.wait_for(state="attached", timeout=5_000)
    page.wait_for_function(
        "document.querySelector('#rail-nodes').textContent.trim() !== '—'",
        timeout=5_000,
    )


def _geometry(page) -> dict:
    return page.evaluate(
        """() => {
          const rect = selector => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return {top: box.top, bottom: box.bottom, width: box.width, height: box.height};
          };
          return {
            shell: rect('.mesh-shell'),
            viewport: rect('#mesh-viewport'),
            canvas: rect('#mesh-canvas'),
            page: {
              viewportWidth: innerWidth,
              documentWidth: document.documentElement.scrollWidth,
              bodyWidth: document.body.scrollWidth,
              viewportHeight: innerHeight,
              documentHeight: document.documentElement.scrollHeight,
              overflowY: getComputedStyle(document.body).overflowY,
            },
          };
        }"""
    )


@pytest.mark.parametrize(("width", "height"), ((1600, 1000), (1280, 720), (390, 844)))
def test_embedded_mesh_fills_tall_desktop_without_breaking_short_or_mobile_contracts(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        errors: list[str] = []
        failed_requests: list[str] = []
        page.on(
            "console",
            lambda message: errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(
                f"{request.url}: {request.failure or 'request failed'}"
            ),
        )
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)
            standalone = _geometry(page)

            page.goto(f"{url}/mesh?embedded=1", wait_until="networkidle")
            _wait_for_mesh(page)
            embedded = _geometry(page)

            assert embedded["page"]["documentWidth"] <= width + 1
            assert embedded["page"]["bodyWidth"] <= width + 1
            if width >= 901 and height >= 821:
                assert abs(embedded["shell"]["bottom"] - height) <= 24
                assert embedded["viewport"]["height"] >= standalone["viewport"]["height"] + 48
                assert embedded["page"]["overflowY"] == "hidden"
            elif width >= 901:
                assert embedded["page"]["documentHeight"] >= height
                assert embedded["page"]["overflowY"] == "auto"
            else:
                assert embedded["shell"]["height"] > height
                assert embedded["viewport"]["height"] >= 400
        finally:
            browser.close()
        assert errors == []
        assert failed_requests == []
