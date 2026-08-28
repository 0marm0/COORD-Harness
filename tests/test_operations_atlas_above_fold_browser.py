from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server


playwright_api = pytest.importorskip("playwright.sync_api")


@contextmanager
def _board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    db = state / "coord.db"
    monkeypatch.setenv("COORD_HOME", str(state))
    monkeypatch.setenv("COORD_DB", str(db))
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1772442000")
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


@pytest.mark.parametrize("width,height", [(1280, 720), (390, 844)])
def test_atlas_topology_enters_first_viewport_without_hiding_summary_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    height: int,
) -> None:
    with _board(tmp_path, monkeypatch) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        console_errors: list[str] = []
        failed_requests: list[str] = []
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(
                f"{request.url}: {request.failure or 'request failed'}"
            ),
        )
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            topology = page.locator(".topology-card").bounding_box()
            assert topology is not None
            assert topology["y"] < height
            assert page.locator("#topology-title").is_visible()
            assert page.locator(".document-stage").count() == 6
            assert page.locator(".atlas-metric").count() == 8
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            )

            if width <= 620:
                assert page.locator(".rail-hint").is_visible()
                for selector in ("#document-stages", "#atlas-metrics"):
                    region = page.locator(selector)
                    assert region.evaluate(
                        "element => element.scrollWidth > element.clientWidth"
                    )
                    region.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
                    assert region.evaluate("element => element.scrollLeft > 0")
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
