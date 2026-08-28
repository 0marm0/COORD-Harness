from __future__ import annotations

import re

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server


playwright_api = pytest.importorskip("playwright.sync_api")


def _contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two ``rgb(r, g, b)`` strings.

    Recomputed here rather than pinned so a future palette change that lowers
    legibility fails on the property that matters, not on a hex literal.
    """

    def luminance(value: str) -> float:
        channels = [int(part) / 255 for part in re.findall(r"\d+", value)[:3]]
        linear = [
            c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
            for c in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


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


def _font_size(page, selector: str) -> float:
    return float(
        page.locator(selector).first.evaluate(
            "element => Number.parseFloat(getComputedStyle(element).fontSize)"
        )
    )


def test_hidden_filter_and_display_panels_take_no_layout_space(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(f"{url}/#v=overview", wait_until="networkidle")
            page.locator(".metrics").wait_for()
            page.locator("#usage-strip details").wait_for()
            for panel_id in ("filterpanel", "displaypanel"):
                state = page.locator(f"#{panel_id}").evaluate(
                    """element => ({
                      hidden: element.hidden,
                      display: getComputedStyle(element).display,
                      height: element.getBoundingClientRect().height,
                    })"""
                )
                assert state == {"hidden": True, "display": "none", "height": 0}

            location_bottom = page.locator(".locbar").evaluate(
                "element => element.getBoundingClientRect().bottom"
            )
            strip_box = page.locator("#usage-strip").evaluate(
                "element => { const box = element.getBoundingClientRect(); "
                "return {top: box.top, bottom: box.bottom}; }"
            )
            metrics_top = page.locator(".metrics").evaluate(
                "element => element.getBoundingClientRect().top"
            )
            assert 0 <= strip_box["top"] - location_bottom < 16
            assert 0 < metrics_top - strip_box["bottom"] < 32

            page.locator("#filterbtn").click()
            assert page.locator("#filterbtn").get_attribute("aria-expanded") == "true"
            assert page.locator("#filterpanel").evaluate(
                "element => !element.hidden && element.getBoundingClientRect().height > 0"
            )
        finally:
            browser.close()


def test_board_operational_and_tertiary_type_floors(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(f"{url}/#v=overview", wait_until="networkidle")
            page.locator(".metrics").wait_for()
            assert page.locator("#rail").is_hidden()
            for selector in (
                ".shell-nav a",
                ".shell-subnav a",
            ):
                assert _font_size(page, selector) >= 13, selector
            for selector in (
                ".live .ls",
                ".live .lm",
                "#filterbtn",
                "#displaybtn",
            ):
                assert _font_size(page, selector) >= 14, selector
            for selector in (
                ".shell-brand .shell-sub",
                ".shell-right .pulse",
                ".metric small",
                ".prov",
            ):
                assert _font_size(page, selector) >= 12, selector

            page.goto(f"{url}/#v=work&layout=list", wait_until="networkidle")
            page.locator(".worktable").wait_for()
            for selector in (
                ".worktable td.c-work",
                ".worktable td.c-note",
                ".worktable td.c-module",
                ".worktable td.c-eta",
            ):
                assert _font_size(page, selector) >= 14, selector
            for selector in (
                ".worktable thead th",
                ".worktable tr.grouphead th",
                ".worktable .modpill",
                ".worktable td.c-id",
            ):
                assert _font_size(page, selector) >= 12, selector
        finally:
            browser.close()


def test_work_table_overflow_is_contained_at_1024px(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1024, "height": 800})
        try:
            page.goto(f"{url}/#v=work&layout=list", wait_until="networkidle")
            page.locator(".worktable").wait_for()
            dimensions = page.evaluate(
                """() => {
                  const wrapper = document.querySelector('.tablewrap');
                  return {
                    body: document.body.scrollWidth,
                    viewport: innerWidth,
                    wrapperClient: wrapper.clientWidth,
                    wrapperScroll: wrapper.scrollWidth,
                  };
                }"""
            )
            assert dimensions["body"] <= dimensions["viewport"]
            assert dimensions["wrapperScroll"] >= dimensions["wrapperClient"]
        finally:
            browser.close()


def test_board_uses_coord_cockpit_tokens_and_dense_geometry(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(f"{url}/#v=work&layout=list", wait_until="networkidle")
            page.locator(".worktable tr[data-row]").first.wait_for()
            contract = page.evaluate(
                """() => {
                  const style = selector => getComputedStyle(document.querySelector(selector));
                  const active = document.querySelector('.shell-nav a[aria-current="page"]');
                  return {
                    bodyBackground: style('body').backgroundColor,
                    cardBackground: style('.tablewrap').backgroundColor,
                    text: style('body').color,
                    muted: style('.crumbs').color,
                    headerHeight: document.querySelector('.shellbar').getBoundingClientRect().height,
                    activeUnderline: getComputedStyle(active, '::after').backgroundColor,
                    controlHeight: document.querySelector('#filterbtn').getBoundingClientRect().height,
                    rowHeight: document.querySelector('.worktable tr[data-row]').getBoundingClientRect().height,
                    bodyFont: style('body').fontFamily,
                    headingFont: style('.shell-mark').fontFamily,
                  };
                }"""
            )
            assert contract["bodyBackground"] == "rgb(0, 0, 0)"
            assert contract["cardBackground"] == "rgb(7, 8, 11)"
            assert contract["text"] == "rgb(244, 246, 251)"
            # Raised from #6f7683 (4.25:1) to #8d95a3 (6.44:1) against the card
            # background: this token carries the monospace work-id column, the one
            # string a reader has to transcribe. Pinning the hex alone is what let
            # the old value sit below AA unnoticed, so assert the ratio too.
            assert contract["muted"] == "rgb(141, 149, 163)"
            assert _contrast(contract["muted"], contract["cardBackground"]) >= 4.5
            assert contract["headerHeight"] == 58
            assert contract["activeUnderline"] == "rgb(94, 157, 255)"
            assert 32 <= contract["controlHeight"] <= 36
            assert contract["rowHeight"] == 48
            assert "Georgia" not in contract["bodyFont"] + contract["headingFont"]
            assert "Coord Brand" not in contract["bodyFont"] + contract["headingFont"]
        finally:
            browser.close()


def test_zero_result_work_view_has_an_explicit_empty_state_at_mobile_width(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(
                f"{url}/#v=work&sel=UI-103&q=owner%3Acodex",
                wait_until="networkidle",
            )
            empty = page.locator("#work .empty")
            empty.wait_for()
            assert "No rows match this view" in empty.inner_text()
            assert page.locator("#popcount").inner_text() == "0 of 47 rows"
            assert page.locator("#detail").is_visible()
            assert "UI-103" in page.locator("#detail").inner_text()
            overflow = page.evaluate(
                """() => ({
                  body: document.body.scrollWidth,
                  viewport: innerWidth,
                  offenders: [...document.querySelectorAll('body *')]
                    .map(element => {
                      const box = element.getBoundingClientRect();
                      return {
                        selector: element.tagName.toLowerCase()
                          + (element.id ? `#${element.id}` : '')
                          + [...element.classList].slice(0, 3)
                            .map(name => `.${name}`).join(''),
                        left: Math.round(box.left),
                        right: Math.round(box.right),
                        width: Math.round(box.width),
                      };
                    })
                    .filter(item => item.left < -1 || item.right > innerWidth + 1)
                    .slice(0, 12),
                })"""
            )
            assert overflow["body"] <= overflow["viewport"], overflow
            geometry = page.evaluate(
                """() => ({
                  locationBottom: document.querySelector('.locbar')
                    .getBoundingClientRect().bottom,
                  detailTop: document.querySelector('#detail')
                    .getBoundingClientRect().top,
                  shellHeight: document.querySelector('.shellbar')
                    .getBoundingClientRect().height,
                  subnavHeight: document.querySelector('.shell-subnav')
                    .getBoundingClientRect().height,
                })"""
            )
            assert geometry["shellHeight"] == 58
            assert geometry["subnavHeight"] == 40
            assert geometry["detailTop"] >= geometry["locationBottom"] - 1, geometry
        finally:
            browser.close()
