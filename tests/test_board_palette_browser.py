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
    server = make_server(host="127.0.0.1", port=0, db_path=str(db))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _rgb(hex_colour: str) -> list[int]:
    value = hex_colour.removeprefix("#")
    return [int(value[index:index + 2], 16) for index in (0, 2, 4)]


def _violations(page, forbidden: list[int]) -> list[dict[str, str]]:
    return page.evaluate(
        """
        ([red, green, blue]) => {
          const properties = [
            "color", "backgroundColor", "backgroundImage", "borderTopColor",
            "borderRightColor", "borderBottomColor", "borderLeftColor", "outlineColor",
            "fill", "stroke", "boxShadow", "textShadow", "caretColor",
            "textDecorationColor",
          ];
          const channels = value => [...String(value).matchAll(
            /rgba?\\(\\s*(\\d+)\\s*[, ]\\s*(\\d+)\\s*[, ]\\s*(\\d+)/g
          )].map(match => match.slice(1, 4).map(Number));
          const selector = element => element.tagName.toLowerCase()
            + (element.id ? `#${element.id}` : "")
            + [...element.classList].slice(0, 3).map(name => `.${name}`).join("");
          const failures = [];
          for (const element of document.querySelectorAll("*")) {
            if (!element.getClientRects().length) continue;
            for (const pseudo of [null, "::before", "::after"]) {
              const style = getComputedStyle(element, pseudo);
              for (const property of properties) {
                const value = style[property];
                if (channels(value).some(rgb => rgb[0] === red && rgb[1] === green && rgb[2] === blue)) {
                  failures.push({ selector: selector(element), pseudo: pseudo || "element", property, value });
                }
              }
            }
          }
          return failures;
        }
        """,
        forbidden,
    )


def _activate_palette(page, name: str) -> None:
    page.locator(f'[data-accent-option="{name}"]').click()
    page.wait_for_function(
        "name => document.documentElement.dataset.accent === name",
        arg=name,
    )


def _activate_map_lens(page, name: str) -> None:
    control = page.locator(f'#maptabs button[data-tab="{name}"]')
    if not control.is_visible():
        page.locator("#map-more-trigger").click()
        control.wait_for(state="visible")
    control.click()
    page.locator(f"#{name}.panel.active").wait_for()


@pytest.mark.parametrize("palette,other", [("green", "blue"), ("blue", "green")])
def test_every_surface_uses_only_the_active_accent(tmp_path: Path, palette: str, other: str) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        failures: list[dict[str, str]] = []
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#health").get_by_text("Live · gen", exact=False).wait_for()
            _activate_palette(page, palette)
            forbidden = _rgb(page.evaluate(f"ACCENTS.{other}.hue"))
            # The shell intentionally keeps only Board and Attention visible.
            # Sweep the compatibility views through their supported hashes so
            # consolidating navigation cannot silently drop their palette QA.
            for capsule, panel in (
                ("v=attention", "attention"),
                ("v=overview", "overview"),
                ("v=work&layout=board", "work"),
                ("v=work&layout=list", "work"),
                ("v=jobs", "jobs"),
                ("v=graph", "graph"),
                ("v=usage", "usage"),
                ("v=activity", "work"),
            ):
                page.goto("about:blank")
                page.goto(f"{url}/#{capsule}", wait_until="networkidle")
                page.locator(f"#{panel}.panel.active").wait_for()
                assert page.locator("#rail").is_hidden()
                assert page.evaluate("document.documentElement.dataset.accent") == palette
                failures.extend(_violations(page, forbidden))

            page.goto(f"{url}/map", wait_until="networkidle")
            page.locator(".matrix").wait_for()
            _activate_palette(page, palette)
            for tab in ("fleet", "deps", "shape", "crossings", "context"):
                _activate_map_lens(page, tab)
                failures.extend(_violations(page, forbidden))
        finally:
            browser.close()
        assert not failures, f"{palette} rendered {other} accent: {failures[:12]}"


def test_palette_sweep_detects_a_reintroduced_literal(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#health").get_by_text("Live · gen", exact=False).wait_for()
            _activate_palette(page, "blue")
            green = page.evaluate("ACCENTS.green.hue")
            page.locator(".pulse span").evaluate("(element, colour) => element.style.background = colour", green)
            failures = _violations(page, _rgb(green))
        finally:
            browser.close()
        assert any(
            item["selector"].startswith("span") and item["property"] == "backgroundColor"
            for item in failures
        ), failures


def test_global_search_keyboard_opens_and_closes_a_row(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            page.locator(".matrix").wait_for()
            _activate_map_lens(page, "shape")
            page.keyboard.press("/")
            assert page.locator(".coordsearch-input").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.type("UI-103")
            page.locator(".coordsearch-live").get_by_text(
                "1 matching row.", exact=True
            ).wait_for()
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.locator("#drawer").get_by_text("UI-103", exact=True).wait_for()
            assert page.locator("#drawer").get_attribute("aria-hidden") == "false"
            page.keyboard.press("Escape")
            assert page.locator("#drawer").get_attribute("aria-hidden") == "true"
        finally:
            browser.close()
