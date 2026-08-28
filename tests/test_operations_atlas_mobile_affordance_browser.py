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


def _page(browser, *, width: int, height: int):
    page = browser.new_page(viewport={"width": width, "height": height})
    console_errors: list[str] = []
    failed_requests: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: console_errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: failed_requests.append(
            f"{request.url}: {request.failure or 'request failed'}"
        ),
    )
    return page, console_errors, failed_requests


def _font_size(locator) -> float:
    return locator.evaluate("element => parseFloat(getComputedStyle(element).fontSize)")


def _assert_font_floor(page, selectors: tuple[str, ...], minimum: float) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        assert locator.count() == 1, selector
        assert _font_size(locator) >= minimum, selector


def test_mobile_question_row_discloses_overflow_and_reveals_active_item(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _page(browser, width=390, height=844)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            row = page.locator("#atlas-questions")
            recent = page.locator("button[data-question='recent']")
            assert row.get_attribute("role") == "group"
            assert row.get_attribute("tabindex") == "0"
            affordance = row.evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    const cue = getComputedStyle(element.parentElement, "::after");
                    return {
                        overflowX: style.overflowX,
                        overscroll: style.overscrollBehaviorInline,
                        snap: style.scrollSnapType,
                        scrollbar: style.scrollbarColor,
                        endInset: parseFloat(style.scrollPaddingInlineEnd),
                        cueWidth: parseFloat(cue.width),
                        cueBackground: cue.backgroundImage,
                        scrollWidth: element.scrollWidth,
                        clientWidth: element.clientWidth,
                    };
                }"""
            )
            assert affordance["overflowX"] == "auto"
            assert affordance["overscroll"] == "contain"
            assert affordance["snap"] in {"inline", "inline proximity"}
            assert affordance["scrollbar"] != "auto"
            assert affordance["endInset"] >= 40
            assert affordance["cueWidth"] >= 40
            assert "linear-gradient" in affordance["cueBackground"]
            assert affordance["scrollWidth"] > affordance["clientWidth"]

            initial = page.evaluate(
                """() => {
                    const row = document.querySelector("#atlas-questions");
                    const button = row.querySelector("[data-question='recent']");
                    const rowBounds = row.getBoundingClientRect();
                    const buttonBounds = button.getBoundingClientRect();
                    return {
                        rowRight: rowBounds.right,
                        buttonRight: buttonBounds.right,
                        inset: parseFloat(
                            getComputedStyle(row).scrollPaddingInlineEnd
                        ),
                    };
                }"""
            )
            assert initial["buttonRight"] > initial["rowRight"] - initial["inset"]

            row.focus()
            for _ in range(4):
                page.keyboard.press("Tab")
            assert (
                page.evaluate("document.activeElement && document.activeElement.dataset.question")
                == "recent"
            )
            assert row.evaluate("element => element.scrollLeft > 0")
            assert page.locator(".atlas-intro").evaluate("element => element.scrollLeft") == 0

            row.evaluate("element => { element.scrollLeft = 0; }")
            recent.click()
            playwright_api.expect(recent).to_have_attribute("aria-pressed", "true")
            assert recent.evaluate("element => element.classList.contains('active')")
            revealed = page.evaluate(
                """() => {
                    const row = document.querySelector("#atlas-questions");
                    const button = row.querySelector("[data-question='recent']");
                    const rowBounds = row.getBoundingClientRect();
                    const buttonBounds = button.getBoundingClientRect();
                    const intro = document.querySelector(".atlas-intro");
                    const copyBounds = row.parentElement.getBoundingClientRect();
                    return {
                        scrollLeft: row.scrollLeft,
                        rowRight: rowBounds.right,
                        buttonRight: buttonBounds.right,
                        inset: parseFloat(
                            getComputedStyle(row).scrollPaddingInlineEnd
                        ),
                        introScrollLeft: intro.scrollLeft,
                        introLeft: intro.getBoundingClientRect().left,
                        copyLeft: copyBounds.left,
                    };
                }"""
            )
            assert revealed["scrollLeft"] > 0
            assert revealed["buttonRight"] <= (revealed["rowRight"] - revealed["inset"] + 1)
            assert revealed["introScrollLeft"] == 0
            assert revealed["copyLeft"] >= revealed["introLeft"] + 10
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_inspector_releases_topology_estate_until_selection(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _page(browser, width=1440, height=1000)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            first_node = page.locator(".atlas-node").first
            first_node.wait_for()

            inspector = page.locator("#atlas-inspector")
            workspace = page.locator(".atlas-workspace")
            topology = page.locator(".topology-card")
            assert inspector.is_hidden()
            assert (
                inspector.evaluate("element => parseFloat(getComputedStyle(element).minHeight)")
                == 0
            )
            assert (
                workspace.evaluate(
                    """element => getComputedStyle(element).gridTemplateColumns
                    .split(" ").filter(Boolean).length"""
                )
                == 1
            )
            full_width = topology.bounding_box()
            assert full_width is not None
            assert not page.locator("body").evaluate(
                "element => element.classList.contains('atlas-has-selection')"
            )

            first_node.click()
            page.locator("body.atlas-has-selection.atlas-inspector-open").wait_for()
            assert inspector.is_visible()
            assert (
                inspector.evaluate("element => parseFloat(getComputedStyle(element).minHeight)")
                >= 590
            )
            assert (
                workspace.evaluate(
                    """element => getComputedStyle(element).gridTemplateColumns
                    .split(" ").filter(Boolean).length"""
                )
                == 2
            )
            selected_width = topology.bounding_box()
            assert selected_width is not None
            assert selected_width["width"] < full_width["width"] - 250

            page.keyboard.press("Escape")
            inspector.wait_for(state="hidden")
            assert not page.locator("body").evaluate(
                """element => element.classList.contains("atlas-has-selection")
                    || element.classList.contains("atlas-inspector-open")"""
            )
            assert (
                workspace.evaluate(
                    """element => getComputedStyle(element).gridTemplateColumns
                    .split(" ").filter(Boolean).length"""
                )
                == 1
            )
            restored_width = topology.bounding_box()
            assert restored_width is not None
            assert restored_width["width"] >= full_width["width"] - 1
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_atlas_operational_and_evidence_text_respects_readability_floors(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _page(browser, width=1440, height=1000)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            _assert_font_floor(
                page,
                (
                    ".authority-seal b",
                    ".authority-seal small",
                    ".eyebrow",
                    ".document-stage span",
                    ".atlas-metric small",
                    ".truth-receipt",
                    ".search-field kbd",
                    ".zoom-controls output",
                    ".topology-head .meta",
                    ".topology-foot p",
                    ".edge-legend",
                    ".panel-stat",
                    ".atlas-column-label",
                    ".atlas-node .node-meta",
                ),
                12,
            )
            _assert_font_floor(
                page,
                (
                    ".document-stage b",
                    ".atlas-metric span",
                    ".atlas-toolbar label > span",
                ),
                13,
            )
            _assert_font_floor(
                page,
                (
                    ".question-row button",
                    ".topology-actions button",
                    ".zoom-controls button",
                    "#atlas-search",
                    ".atlas-toolbar select",
                    ".atlas-node .node-title",
                ),
                14,
            )
            assert page.locator(".atlas-metric").evaluate_all(
                "elements => elements.every(element => element.scrollHeight <= element.clientHeight + 1)"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
