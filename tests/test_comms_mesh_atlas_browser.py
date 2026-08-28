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
    server = make_server(
        host="127.0.0.1",
        port=0,
        db_path=str(database),
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


def test_mesh_and_atlas_sources_prefer_v2_and_disclose_traffic() -> None:
    root = Path(__file__).parents[1] / "src/coordharness/board/static"
    mesh = (root / "swarm-mesh.js").read_text(encoding="utf-8")
    atlas = (root / "ops-atlas.js").read_text(encoding="utf-8")
    mesh_html = (root / "swarm-mesh.html").read_text(encoding="utf-8")
    atlas_html = (root / "ops-atlas.html").read_text(encoding="utf-8")
    for source in (mesh, atlas):
        assert source.index('"/api/v2/operations-bundle"') < source.index('"/api/v1/operations-bundle"')
        assert 'schema_version !== "PulseV1"' in source
    assert 'data-motion="traffic"' in mesh_html
    assert "recorded direction, not current activity" in mesh_html
    assert '/map?lens=pulse' in atlas_html
    assert '/map?lens=topology' in atlas_html


def test_v2_pulse_renders_mesh_traffic_and_compact_atlas_with_v1_fallback(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("request", lambda request: requests.append(request.url))
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            assert any("/api/v2/operations-bundle" in request for request in requests)
            assert page.locator("#atlas-traffic").get_attribute("data-state") == "available"
            assert "Directed acts" in page.locator("#traffic-facts").inner_text()
            assert 0 < page.locator(".traffic-route").count() <= 4
            assert page.locator('.traffic-links a[href="/map?lens=pulse"]').count() == 1

            page.goto(f"{url}/mesh", wait_until="networkidle")
            page.locator("body:not(.mesh-loading)").wait_for()
            traffic = page.locator('[data-motion="traffic"]')
            assert not traffic.is_disabled()
            traffic.click()
            playwright_api.expect(page.locator("#motion-truth")).to_contain_text("NOT CURRENT ACTIVITY")
            assert int(page.locator("#mesh-canvas").get_attribute("data-comms-route-count") or 0) > 0
            assert "recorded direction, not current activity" in page.locator("#mesh-comms-truth").inner_text().lower()

            fallback = browser.new_page(viewport={"width": 1200, "height": 900})
            fallback_errors: list[str] = []
            fallback.on("pageerror", lambda error: fallback_errors.append(str(error)))
            fallback.on("console", lambda message: fallback_errors.append(message.text) if message.type == "error" else None)
            fallback.route("**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="not found"))
            fallback.goto(f"{url}/ops", wait_until="networkidle")
            fallback.locator(".atlas-node").first.wait_for()
            assert fallback.locator("#atlas-traffic").get_attribute("data-state") == "unavailable"
            assert "V1 compatibility" in fallback.locator("#traffic-facts").inner_text()
            unexpected_fallback_errors = [value for value in fallback_errors if "404" not in value]
            assert not unexpected_fallback_errors, unexpected_fallback_errors
            fallback.close()
        finally:
            browser.close()
        assert not errors, errors
