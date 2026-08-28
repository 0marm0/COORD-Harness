from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

import pytest

from coordharness import demo
from coordharness.board import server as board_server


playwright_api = pytest.importorskip("playwright.sync_api")


@contextmanager
def _board(tmp_path: Path):
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    server = board_server.make_server(
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


@pytest.mark.parametrize("viewport", ((1440, 1000), (390, 844)))
def test_map_keeps_its_styles_and_type_floors_with_the_running_server_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    viewport: tuple[int, int],
) -> None:
    # Model a process that imported the allowlist before map-readability.css
    # existed. Every stylesheet the current Map needs must still be available.
    legacy_allowlist = set(board_server._STATIC_ALLOWLIST)
    legacy_allowlist.discard("map-readability.css")
    assert "motion.css" in legacy_allowlist
    monkeypatch.setattr(board_server, "_STATIC_ALLOWLIST", legacy_allowlist)

    width, height = viewport
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        stylesheets: list[tuple[str, int]] = []
        failed_stylesheets: list[str] = []
        errors: list[str] = []

        def record_response(response) -> None:
            if response.request.resource_type == "stylesheet":
                stylesheets.append((urlparse(response.url).path, response.status))

        def record_failed_request(request) -> None:
            if request.resource_type == "stylesheet":
                failed_stylesheets.append(f"{request.url}: {request.failure}")

        page.on("response", record_response)
        page.on("requestfailed", record_failed_request)
        page.on(
            "console",
            lambda message: errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            page.locator("#fleet .matrix").wait_for()

            assert stylesheets
            assert ("/static/motion.css", 200) in stylesheets
            assert all(status < 400 for _path, status in stylesheets), stylesheets
            assert all(path != "/static/map-readability.css" for path, _status in stylesheets)
            assert failed_stylesheets == []

            findings = page.evaluate(
                """async () => {
                  const failures = [];
                  const ownText = element => [...element.childNodes].some(node =>
                    node.nodeType === Node.TEXT_NODE && node.textContent.trim()
                  );
                  const visible = element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                      style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const check = (element, floor, lens, kind) => {
                    if (!visible(element)) return;
                    const size = Number.parseFloat(getComputedStyle(element).fontSize);
                    if (Number.isFinite(size) && size < floor) failures.push({
                      lens,
                      kind,
                      floor,
                      size,
                      tag: element.tagName,
                      className: String(element.getAttribute('class') || ''),
                      text: element.textContent.trim().slice(0, 80),
                    });
                  };

                  for (const control of document.querySelectorAll(
                    '#maptabs [role=tab], #global-search input, #global-search button'
                  )) check(control, 14, 'Map shell', 'operational');

                  for (const tab of document.querySelectorAll('#maptabs [role=tab]')) {
                    if (tab.disabled) continue;
                    tab.click();
                    await new Promise(resolve => setTimeout(resolve, 60));
                    const panel = document.querySelector('.panel.active');
                    const lens = tab.textContent.trim();
                    for (const element of panel.querySelectorAll('*')) {
                      if (ownText(element)) check(element, 12, lens, 'tertiary');
                    }
                    for (const control of panel.querySelectorAll(
                      'button,input,select,textarea,[role=button]'
                    )) check(control, 14, lens, 'operational');
                  }
                  return failures;
                }"""
            )
            assert findings == [], json.dumps(findings, indent=2, sort_keys=True)
        finally:
            browser.close()
        assert errors == []
