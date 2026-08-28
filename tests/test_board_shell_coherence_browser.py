from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import threading
from urllib.request import urlopen

import pytest

from coordharness import demo
from coordharness.board.server import make_server


playwright_api = pytest.importorskip("playwright.sync_api")


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


def _bundle(url: str) -> dict:
    with urlopen(f"{url}/api/v2/operations-bundle") as response:
        return json.load(response)


def test_board_prefers_one_coherent_bundle_and_names_its_generation(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#health").get_by_text("Live · gen", exact=False).wait_for()
            assert any(value.endswith("/api/v2/operations-bundle") for value in requests)
            assert not any(value.endswith("/api/v1/snapshot") for value in requests)
            assert not any(value.endswith("/api/v1/graph") for value in requests)
            assert page.locator("#boardreadalert").is_hidden()
        finally:
            browser.close()


def test_newer_board_read_wins_and_aborts_the_obsolete_request(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        source = _bundle(url)
        older = deepcopy(source)
        newer = deepcopy(source)
        for document, generation in ((older, 101), (newer, 202)):
            document["cache_generation"] = generation
            document["read_status"]["cache_generation"] = generation

        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """
            const nativeFetch = window.fetch.bind(window);
            window.__boardReads = [];
            window.fetch = (input, init = {}) => {
              if (!String(input).endsWith('/api/v2/operations-bundle')) {
                return nativeFetch(input, init);
              }
              return new Promise((resolvePromise, reject) => {
                const read = {
                  aborted: false,
                  finish(payload) {
                    resolvePromise(new Response(JSON.stringify(payload), {
                      status: 200,
                      headers: {'Content-Type': 'application/json'},
                    }));
                  },
                };
                window.__boardReads.push(read);
                init.signal?.addEventListener('abort', () => {
                  read.aborted = true;
                  reject(new DOMException('Aborted', 'AbortError'));
                }, {once: true});
              });
            };
            """
        )
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("window.__boardReads.length === 1")
            page.evaluate("void refreshBoard()")
            page.wait_for_function("window.__boardReads.length === 2")
            assert page.evaluate("window.__boardReads[0].aborted") is True

            page.evaluate("payload => window.__boardReads[1].finish(payload)", newer)
            page.locator("#health").get_by_text("Live · gen 202", exact=True).wait_for()
            page.evaluate("payload => window.__boardReads[0].finish(payload)", older)
            page.wait_for_timeout(100)
            assert page.locator("#health").inner_text() == "Live · gen 202"
        finally:
            browser.close()


def test_failed_refresh_retains_last_good_and_revokes_live_claim(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#health").get_by_text("Live · gen", exact=False).wait_for()
            population = page.locator("#popcount").inner_text()
            page.evaluate(
                """
                window.__healthyFetch = window.fetch;
                window.fetch = (input, init) => String(input).endsWith('/api/v2/operations-bundle')
                  ? Promise.resolve(new Response('unavailable', {status: 503}))
                  : window.__healthyFetch(input, init);
                refreshBoard();
                """
            )
            page.locator("#health").get_by_text("DEGRADED · last good", exact=True).wait_for()
            alert = page.locator("#boardreadalert")
            assert alert.is_visible()
            assert "does not claim current live state" in alert.inner_text()
            assert float(alert.evaluate(
                "element => Number.parseFloat(getComputedStyle(element).fontSize)"
            )) >= 14
            assert page.locator("#popcount").inner_text() == population
        finally:
            browser.close()


def test_command_palette_has_complete_dialog_and_combobox_keyboard_model(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#filterbtn").focus()
            page.keyboard.press("Control+k")

            dialog = page.get_by_role("dialog", name="Command palette")
            dialog.wait_for()
            query = page.get_by_role("combobox", name="Palette query")
            assert query.evaluate("element => element === document.activeElement")
            assert query.get_attribute("aria-controls") == "cmdk-list"
            assert query.get_attribute("aria-expanded") == "true"
            selected = page.locator("#cmdk-list [role='option'][aria-selected='true']")
            assert selected.count() == 1
            first_active = query.get_attribute("aria-activedescendant")
            assert first_active == selected.get_attribute("id")

            page.keyboard.press("ArrowDown")
            assert query.get_attribute("aria-activedescendant") != first_active
            assert page.locator("#cmdk-list [role='option'][aria-selected='true']").count() == 1

            page.keyboard.press("Tab")
            assert page.locator("#cmdk-close").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.press("Tab")
            assert query.evaluate("element => element === document.activeElement")
            page.keyboard.press("Shift+Tab")
            assert page.locator("#cmdk-close").evaluate(
                "element => element === document.activeElement"
            )

            page.keyboard.press("Escape")
            assert dialog.is_hidden()
            assert page.locator("#cmdk-input").get_attribute("aria-expanded") == "false"
            assert page.locator("#filterbtn").evaluate(
                "element => element === document.activeElement"
            )

            page.keyboard.press("Control+k")
            query.fill("board")
            page.keyboard.press("Enter")
            page.locator("#work.panel.active").wait_for()
            assert page.get_by_role("navigation", name="Product areas").get_by_role(
                "link", name="Work", exact=True
            ).get_attribute("aria-current") == "page"
            assert page.get_by_role("navigation", name="Work views").get_by_role(
                "link", name="Board", exact=True
            ).get_attribute("aria-current") == "page"
            assert dialog.is_hidden()

            page.locator("#filterbtn").focus()
            page.keyboard.press("Control+k")
            page.locator("#cmdk").click(position={"x": 4, "y": 4})
            assert dialog.is_hidden()
            assert page.locator("#filterbtn").evaluate(
                "element => element === document.activeElement"
            )
        finally:
            browser.close()


def test_root_restores_operational_overview_and_keeps_cards_optional(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script("localStorage.removeItem('coord.display')")
        try:
            page.goto(url, wait_until="networkidle")
            page.locator("#overview.panel.active .metrics").wait_for()
            assert page.locator("#work.panel.active").count() == 0
            assert "Running now" in page.locator("#overview").inner_text()
            assert "Status-based attention" in page.locator("#overview").inner_text()

            work_nav = page.get_by_role("navigation", name="Work views")
            assert work_nav.get_by_role("link").all_inner_texts() == [
                "Overview", "Board", "Attention", "Jobs", "Graph", "Comms",
            ]
            assert work_nav.get_by_role("link", name="Overview", exact=True).get_attribute(
                "aria-current"
            ) == "page"

            work_nav.get_by_role("link", name="Board", exact=True).click()
            page.locator("#work.panel.active .worktable").wait_for()
            assert page.locator("#workmodes").is_visible()
            assert page.locator("#workmodes button").all_inner_texts() == [
                "List 1", "Cards 2", "Timeline 3",
            ]
            page.locator('[data-work-layout="board"]').click()
            page.locator("#work .kanban").wait_for()
            assert page.evaluate(
                "new URLSearchParams(location.hash.slice(1)).get('layout')"
            ) == "board"

            work_nav.get_by_role("link", name="Overview", exact=True).click()
            page.locator("#overview.panel.active .metrics").wait_for()
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 720, 1024, 1440])
def test_shell_contains_page_overflow_and_keeps_terminal_content_reachable(
    tmp_path: Path, width: int
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 844})
        try:
            page.goto(f"{url}/#v=work&layout=list", wait_until="networkidle")
            page.locator(".worktable").wait_for()
            dimensions = page.evaluate(
                """() => {
                  const workspace = document.querySelector('.workspace');
                  const panes = document.querySelector('.panes');
                  const table = document.querySelector('.tablewrap');
                  panes.scrollTop = panes.scrollHeight;
                  table.scrollLeft = table.scrollWidth;
                  return {
                    bodyWidth: document.body.scrollWidth,
                    bodyHeight: document.body.scrollHeight,
                    viewportWidth: innerWidth,
                    viewportHeight: innerHeight,
                    workspaceBottom: workspace.getBoundingClientRect().bottom,
                    panesClient: panes.clientHeight,
                    panesScroll: panes.scrollHeight,
                    panesAtEnd: Math.abs(
                      panes.scrollHeight - panes.clientHeight - panes.scrollTop
                    ) < 2,
                    tableClient: table.clientWidth,
                    tableScroll: table.scrollWidth,
                    tableAtEnd: Math.abs(
                      table.scrollWidth - table.clientWidth - table.scrollLeft
                    ) < 2,
                  };
                }"""
            )
            assert dimensions["bodyWidth"] <= dimensions["viewportWidth"]
            assert dimensions["bodyHeight"] <= dimensions["viewportHeight"] + 1
            assert dimensions["workspaceBottom"] <= dimensions["viewportHeight"] + 1
            assert dimensions["panesClient"] > 0
            assert dimensions["panesScroll"] >= dimensions["panesClient"]
            assert dimensions["panesAtEnd"] is True
            assert dimensions["tableScroll"] >= dimensions["tableClient"]
            assert dimensions["tableAtEnd"] is True
        finally:
            browser.close()


def test_attention_rows_are_keyboard_controls_but_group_headers_are_not(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{url}/#v=attention", wait_until="networkidle")
            decision = page.locator("#attention [data-row][role='button']").first
            decision.wait_for()
            assert decision.get_attribute("tabindex") == "0"
            assert decision.get_attribute("aria-label").startswith("Open ")
            assert page.locator(".worktable tr.grouphead[role='button']").count() == 0
            attention_destination = page.get_by_role(
                "navigation", name="Work views"
            ).get_by_role("link", name="Attention", exact=True)
            assert attention_destination.is_visible()
            assert attention_destination.get_attribute("aria-current") == "page"
            assert page.locator("#rail").is_hidden()
            assert "decision rows" in page.locator("#popcount").inner_text()
            assert "reasons" in page.locator("#popcount").inner_text()

            decision.focus()
            page.keyboard.press("Space")
            page.locator("#detail:not([hidden])").wait_for()
            assert decision.evaluate("element => element.classList.contains('sel')")
        finally:
            browser.close()
