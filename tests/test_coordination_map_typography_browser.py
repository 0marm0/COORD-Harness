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


def test_every_map_lens_holds_operational_and_tertiary_type_floors(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            page.locator(".matrix").wait_for()
            results = page.evaluate(
                """async () => {
                  const failures = [];
                  const ownText = element => {
                    if (element.tagName === 'TEXT' || element.tagName === 'TSPAN') return true;
                    return [...element.childNodes].some(node =>
                      node.nodeType === Node.TEXT_NODE && node.textContent.trim()
                    );
                  };
                  for (const tab of document.querySelectorAll('#maptabs [role=tab]')) {
                    tab.click();
                    await new Promise(resolve => setTimeout(resolve, 80));
                    const panel = document.querySelector('.panel.active');
                    for (const element of panel.querySelectorAll('*')) {
                      const rect = element.getBoundingClientRect();
                      const style = getComputedStyle(element);
                      if (!ownText(element) || rect.width <= 0 || rect.height <= 0 ||
                          style.visibility === 'hidden') continue;
                      const size = Number.parseFloat(style.fontSize);
                      if (size < 12) failures.push({
                        tab: tab.textContent.trim(),
                        tag: element.tagName,
                        className: String(element.getAttribute('class') || ''),
                        size,
                        text: element.textContent.trim().slice(0, 60),
                      });
                    }
                    for (const control of panel.querySelectorAll('button,input,select,textarea,[role=button]')) {
                      const rect = control.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) continue;
                      const size = Number.parseFloat(getComputedStyle(control).fontSize);
                      if (size < 14) failures.push({
                        tab: tab.textContent.trim(),
                        tag: control.tagName,
                        className: String(control.getAttribute('class') || ''),
                        size,
                        text: control.textContent.trim().slice(0, 60),
                      });
                    }
                    if (document.body.scrollWidth > innerWidth) failures.push({
                      tab: tab.textContent.trim(),
                      tag: 'BODY',
                      className: 'page-overflow',
                      size: document.body.scrollWidth,
                      text: `viewport ${innerWidth}`,
                    });
                  }
                  return failures;
                }"""
            )
            assert results == [], json.dumps(results, indent=2, sort_keys=True)
        finally:
            browser.close()
        assert errors == []
