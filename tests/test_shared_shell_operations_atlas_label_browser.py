from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server


playwright_api = pytest.importorskip("playwright.sync_api")

GLOBAL_LINKS = [
    {"text": "Work", "href": "/#v=overview"},
    {"text": "Intelligence", "href": "/map"},
    {"text": "Usage", "href": "/#v=usage"},
]
INTELLIGENCE_LINKS = [
    {"text": "Map", "href": "/map"},
    {"text": "Mesh", "href": "/mesh"},
    {"text": "Operations Atlas", "href": "/ops"},
]
WORK_LINKS = [
    {"text": "Overview", "href": "/#v=overview"},
    {"text": "Board", "href": "/#v=work&layout=list"},
    {"text": "Attention", "href": "/#v=attention"},
    {"text": "Jobs", "href": "/#v=jobs"},
    {"text": "Graph", "href": "/#v=graph"},
    {"text": "Comms", "href": "/#v=comms"},
]
ROUTES = [
    ("/", "Work", WORK_LINKS, "Overview"),
    ("/#v=work&layout=board", "Work", WORK_LINKS, "Board"),
    ("/#v=attention", "Work", WORK_LINKS, "Attention"),
    ("/#v=jobs", "Work", WORK_LINKS, "Jobs"),
    ("/#v=graph", "Work", WORK_LINKS, "Graph"),
    ("/#v=comms", "Work", WORK_LINKS, "Comms"),
    ("/#v=usage", "Usage", [], None),
    ("/map", "Intelligence", INTELLIGENCE_LINKS, "Map"),
    ("/mesh", "Intelligence", INTELLIGENCE_LINKS, "Mesh"),
    ("/ops", "Intelligence", INTELLIGENCE_LINKS, "Operations Atlas"),
]


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
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _links(page, selector: str) -> list[dict[str, str | None]]:
    return page.locator(selector).evaluate_all(
        """links => links.map(link => ({
          text: link.textContent.trim(),
          href: link.getAttribute("href"),
          current: link.getAttribute("aria-current"),
        }))"""
    )


def _plain_links(links: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    return [{"text": link["text"], "href": link["href"]} for link in links]


def test_standalone_shells_have_three_global_destinations_and_local_modes(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            for route, global_current, local_links, local_current in ROUTES:
                page.goto(f"{url}{route}", wait_until="domcontentloaded")
                page.locator(".shellbar").wait_for(state="visible")

                global_nav = _links(page, ".shellbar .shell-nav a")
                assert _plain_links(global_nav) == GLOBAL_LINKS
                assert [
                    link["text"] for link in global_nav if link["current"] == "page"
                ] == [global_current]

                geometry = page.evaluate(
                    "() => { const brand=document.querySelector(\".shell-brand\").getBoundingClientRect(); const nav=document.querySelector(\".shell-nav\").getBoundingClientRect(); const accent=document.querySelector(\".shellbar > .accent\")?.getBoundingClientRect(); const right=document.querySelector(\".shell-right\")?.getBoundingClientRect(); const rightLeft=right?.left ?? accent?.left ?? innerWidth; const rightRight=right?.right ?? nav.right; return {brandRight:brand.right, navLeft:nav.left, navRight:nav.right, rightLeft, rightRight, accentLeft:accent?.left ?? null, maxRight:Math.max(brand.right,nav.right,rightRight,accent?.right ?? 0), viewport:innerWidth}; }"
                )
                assert geometry["brandRight"] <= geometry["navLeft"] + 0.5
                assert geometry["navRight"] <= geometry["rightLeft"] + 0.5
                if geometry["accentLeft"] is not None:
                    assert geometry["rightRight"] <= geometry["accentLeft"] + 0.5
                assert geometry["maxRight"] <= geometry["viewport"] + 0.5

                secondary = page.locator(".shell-subnav")
                if local_links:
                    secondary.wait_for(state="visible")
                    local_nav = _links(page, ".shell-subnav a")
                    assert _plain_links(local_nav) == local_links
                    assert [
                        link["text"] for link in local_nav if link["current"] == "page"
                    ] == [local_current]
                else:
                    assert secondary.count() == 0
        finally:
            browser.close()


def test_work_secondary_row_reserves_space_and_list_remains_clickable(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            page.goto(f"{url}/#v=work&layout=board", wait_until="networkidle")
            page.locator("#workmodes:not([hidden])").wait_for()
            geometry = page.evaluate(
                """() => {
                  const secondary = document.querySelector('.shell-subnav').getBoundingClientRect();
                  const local = document.querySelector('.locbar').getBoundingClientRect();
                  const button = document.querySelector('#workmodes button[data-work-layout="list"]');
                  const box = button.getBoundingClientRect();
                  const hit = document.elementFromPoint(
                    box.left + box.width / 2,
                    box.top + box.height / 2,
                  );
                  return {
                    secondaryBottom: secondary.bottom,
                    localTop: local.top,
                    hitsList: hit === button || button.contains(hit),
                  };
                }"""
            )
            assert geometry["secondaryBottom"] <= geometry["localTop"] + 0.5
            assert geometry["hitsList"] is True
            list_button = page.locator('#workmodes button[data-work-layout="list"]')
            list_button.click()
            page.locator(".worktable").wait_for()
            assert list_button.get_attribute("aria-pressed") == "true"
        finally:
            browser.close()


def test_embedded_routes_hide_both_shared_navigation_rows(tmp_path: Path) -> None:
    routes = (
        "/?embedded=1#v=work&layout=board",
        "/map?embedded=1",
        "/mesh?embedded=1",
        "/ops?embedded=1",
    )
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            for route in routes:
                page.goto(f"{url}{route}", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => document.documentElement.dataset.embedded === \"1\""
                )
                shell = page.locator(".shellbar")
                shell.wait_for(state="attached")
                assert shell.is_hidden()
                assert shell.bounding_box() is None
                assert page.locator(".shell-subnav").count() == 0
        finally:
            browser.close()
