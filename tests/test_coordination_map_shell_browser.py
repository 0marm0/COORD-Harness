from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse

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
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _page(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors: list[str] = []
    page.on(
        "console",
        lambda message: errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    return page, errors


def _wait_for_map(page) -> None:
    page.wait_for_function(
        "() => document.querySelector('#mapmeta')?.textContent.includes('cache generation')"
    )


def _activate_lens(page, lens: str) -> None:
    control = page.locator(f"#tab-{lens}")
    if not control.is_visible():
        page.locator("#map-more-trigger").click()
        control.wait_for(state="visible")
    control.click()
    page.locator(f"#{lens}.panel.active").wait_for()


@pytest.mark.parametrize(("width", "height"), ((1280, 720), (390, 844)))
def test_map_uses_the_exact_shared_board_shell_contract(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    def shell_contract(page) -> dict:
        return page.evaluate(
            """() => {
              const shell = document.querySelector('.shellbar');
              const mark = document.querySelector('.shell-mark');
              const active = document.querySelector('.shell-nav a[aria-current="page"]');
              const style = getComputedStyle(active);
              const underline = getComputedStyle(active, '::after');
              const visibleTextSizes = [...shell.querySelectorAll('*')]
                .filter(element => {
                  const computed = getComputedStyle(element);
                  const box = element.getBoundingClientRect();
                  return element.children.length === 0
                    && element.textContent.trim()
                    && computed.display !== 'none'
                    && computed.visibility !== 'hidden'
                    && box.width > 0
                    && box.height > 0;
                })
                .map(element => Number.parseFloat(getComputedStyle(element).fontSize));
              return {
                height: shell.getBoundingClientRect().height,
                mark: {
                  family: getComputedStyle(mark).fontFamily,
                  weight: getComputedStyle(mark).fontWeight,
                  spacing: getComputedStyle(mark).letterSpacing,
                },
                active: {
                  background: style.backgroundColor,
                  border: style.borderTopWidth,
                  underlineColor: underline.backgroundColor,
                  underlineHeight: underline.height,
                },
                minVisibleText: Math.min(...visibleTextSizes),
                widths: {
                  viewport: innerWidth,
                  document: document.documentElement.scrollWidth,
                  body: document.body.scrollWidth,
                },
              };
            }"""
        )

    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        page.set_viewport_size({"width": width, "height": height})
        try:
            page.goto(f"{url}/#v=work", wait_until="networkidle")
            page.locator("#work [data-row]").first.wait_for()
            board = shell_contract(page)

            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)
            coordination_map = shell_contract(page)

            assert board["height"] == coordination_map["height"] == 58
            assert board["mark"] == coordination_map["mark"]
            assert board["active"] == coordination_map["active"]
            assert coordination_map["active"] == {
                "background": "rgba(0, 0, 0, 0)",
                "border": "0px",
                "underlineColor": "rgb(94, 157, 255)",
                "underlineHeight": "2px",
            }
            assert board["minVisibleText"] >= 12
            assert coordination_map["minVisibleText"] >= 12
            assert board["widths"] == {
                "viewport": width,
                "document": width,
                "body": width,
            }
            assert coordination_map["widths"] == board["widths"]
            if width == 390:
                accent = page.locator(".shellbar > .accent")
                assert accent.locator('[data-accent-option="green"]').is_visible()
                assert accent.locator('[data-accent-option="blue"]').is_visible()
        finally:
            browser.close()
        assert not errors, errors


@pytest.mark.parametrize(("width", "height"), ((1280, 720), (390, 844)))
def test_map_shell_and_fleet_stay_truthful_at_laptop_and_mobile_widths(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        page.set_viewport_size({"width": width, "height": height})
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)

            expected_verticals = page.evaluate(
                """() => [...new Set(snapshot.rows
                  .filter(row => row.bucket !== 'epic')
                  .map(row => row.module)
                  .filter(Boolean))].sort()"""
            )
            # Every vertical must be a module some work row actually owns. `local`
            # used to appear here as a ninth: the bucket job telemetry fell into
            # when a sidecar named no work row, which is the same unowned-telemetry
            # state `coord doctor` reported as sidecar_work_binding_missing. The
            # seeder now binds each sidecar to its row, so the phantom is gone.
            # Asserting the invariant rather than a count keeps this honest if the
            # demo estate grows.
            assert "local" not in expected_verticals
            assert len(expected_verticals) == 8
            accessible_headers = {
                header.casefold()
                for header in page.locator("#fleet").get_by_role("columnheader").all_inner_texts()
            }
            assert {vertical.casefold() for vertical in expected_verticals} <= accessible_headers

            page_widths = page.evaluate(
                """() => ({
                  viewport: innerWidth,
                  document: document.documentElement.scrollWidth,
                  body: document.body.scrollWidth,
                })"""
            )
            assert page_widths == {"viewport": width, "document": width, "body": width}

            accent = page.locator(".shellbar > .accent")
            accent_box = accent.bounding_box()
            assert accent_box is not None
            assert accent_box["x"] >= 0
            assert accent_box["x"] + accent_box["width"] <= width + 0.5
            assert accent.locator('[data-accent-option="green"]').is_visible()
            assert accent.locator('[data-accent-option="blue"]').is_visible()
            assert accent.locator("button").all_inner_texts() == ["Green", "Blue"]
            assert all(
                size >= 12
                for size in accent.locator("button").evaluate_all(
                    "buttons => buttons.map(button => parseFloat(getComputedStyle(button).fontSize))"
                )
            )
            if width == 390:
                assert page.locator("#mapmeta").is_hidden()
                assert accent.locator(".meta").is_hidden()
            else:
                assert page.locator("#mapmeta").is_visible()
                assert accent.locator(".meta").is_visible()
            page.locator('[data-accent-option="green"]').click()
            assert page.locator("html").get_attribute("data-accent") == "green"

            tablist = page.locator("#maptabs")
            if width == 390:
                tablewrap = page.locator("#fleet .tablewrap")
                table_scroll = tablewrap.evaluate(
                    "element => ({client: element.clientWidth, scroll: element.scrollWidth})"
                )
                assert table_scroll["scroll"] > table_scroll["client"]
                tablewrap.hover()
                page.mouse.wheel(200, 0)
                page.wait_for_function(
                    "element => element.scrollLeft > 0", arg=tablewrap.element_handle()
                )

            assert page.locator("#maptabs-help").count() == 0

            direct = tablist.locator('[role="tab"]')
            assert direct.count() == 4
            assert direct.evaluate_all("controls => controls.map(control => control.id)") == [
                "tab-fleet",
                "tab-pulse",
                "tab-deps",
                "map-more-trigger",
            ]
            assert all(direct.nth(index).is_visible() for index in range(4))
            assert tablist.locator('[role="menuitemradio"]').count() == 9
            assert page.locator("#map-more-menu").is_hidden()

            page.locator("#tab-fleet").focus()
            page.keyboard.press("End")
            more = page.locator("#map-more-trigger")
            assert more.evaluate("element => element === document.activeElement")
            page.keyboard.press("Enter")
            assert more.get_attribute("aria-expanded") == "true"
            assert page.locator("#map-more-menu").is_visible()
            assert page.locator("#tab-flowpath").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.press("End")
            context = page.locator("#tab-context")
            assert context.evaluate("element => element === document.activeElement")
            page.keyboard.press("Enter")
            assert page.locator("#context").get_attribute("hidden") is None
            assert context.get_attribute("aria-checked") == "true"
            assert page.locator("#map-more-menu").is_hidden()
            assert more.get_attribute("aria-selected") == "true"
            assert more.locator(".map-more-label").inner_text() == "More · Context"
            assert "lens=context" in page.url
            assert more.evaluate("element => element === document.activeElement")

            map_controls = tablist.evaluate(
                "element => ({client: element.clientWidth, scroll: element.scrollWidth})"
            )
            assert map_controls["scroll"] <= map_controls["client"] + 1
            more_box = more.bounding_box()
            assert more_box is not None
            assert more_box["x"] + more_box["width"] <= width + 0.5
        finally:
            browser.close()
        assert not errors, errors


def test_projection_receipt_is_compact_with_accessible_exact_populations(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)
            expected = page.evaluate(
                """() => ({
                  rows: bundleProjection.snapshot_rows.published,
                  edges: bundleProjection.graph_edges.published,
                })"""
            )
            receipt = page.locator("#fleet > details.projection-receipt")
            assert receipt.get_attribute("data-projection-state") == "complete"
            assert receipt.get_attribute("open") is None
            summary = receipt.locator("summary")
            assert summary.inner_text().startswith(
                f"Projection complete · {expected['rows']} rows · {expected['edges']} edges"
            )
            assert "exact populations" in summary.inner_text().lower()
            summary.click()
            exact = receipt.locator(".projection-detail").inner_text().lower()
            for label in (
                "snapshot rows",
                "graph nodes",
                "graph edges",
                "context rows",
                "timeline rows",
            ):
                assert f"{label}:" in exact
                assert "published of" in exact
            assert "these counts scope the visible map" in exact
            assert "larger snapshot summary totals do not label the projected matrix" in exact
            for lens in ("shape", "context", "pulse", "deps"):
                _activate_lens(page, lens)
                lens_receipt = page.locator(f"#{lens} > details.projection-receipt")
                assert lens_receipt.count() == 1
                assert lens_receipt.get_attribute("open") is None
        finally:
            browser.close()
        assert not errors, errors


@pytest.mark.parametrize(
    ("width", "height", "mode"),
    ((1600, 1000, "wide"), (390, 844, "compact")),
)
def test_orbit_uses_full_estate_without_nested_clipping(
    tmp_path: Path,
    width: int,
    height: int,
    mode: str,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        page.set_viewport_size({"width": width, "height": height})
        try:
            page.goto(f"{url}/map?lens=orbit", wait_until="networkidle")
            _wait_for_map(page)
            page.locator("#orbit .ob-map").wait_for()
            geometry = page.evaluate(
                """() => {
                  const wrap = document.querySelector('#orbit .gwrap');
                  const svg = wrap.querySelector('.ob-map');
                  const svgBox = svg.getBoundingClientRect();
                  const nodeBoxes = [...svg.querySelectorAll('.ob-node')].map(node => {
                    const box = node.getBoundingClientRect();
                    return {left: box.left, right: box.right, top: box.top, bottom: box.bottom};
                  });
                  return {
                    mode: svg.dataset.layoutMode,
                    layoutWidth: Number(svg.dataset.layoutWidth),
                    r1: Number(svg.dataset.ringOneRadius),
                    wrapClient: wrap.clientWidth,
                    wrapScroll: wrap.scrollWidth,
                    svgWidth: svgBox.width,
                    bodyWidth: document.body.scrollWidth,
                    viewport: innerWidth,
                    labels: [...svg.querySelectorAll('.ob-id,.ob-rootid')].map(label =>
                      parseFloat(getComputedStyle(label).fontSize)),
                    nodesInside: nodeBoxes.every(box => box.left >= svgBox.left - 1 &&
                      box.right <= svgBox.right + 1 && box.top >= svgBox.top - 1 &&
                      box.bottom <= svgBox.bottom + 1),
                  };
                }"""
            )
            assert geometry["mode"] == mode
            assert geometry["svgWidth"] >= geometry["wrapClient"] - 1
            assert geometry["wrapScroll"] <= geometry["wrapClient"] + 1
            assert geometry["bodyWidth"] == geometry["viewport"] == width
            assert geometry["nodesInside"]
            assert geometry["labels"] and min(geometry["labels"]) >= 12
            if mode == "wide":
                assert geometry["layoutWidth"] >= 1200
                assert geometry["r1"] >= 150
            else:
                assert geometry["layoutWidth"] <= 366
                assert geometry["r1"] <= 92
        finally:
            browser.close()
        assert not errors, errors


def test_map_reads_one_coherent_bundle_and_pulse_is_explicitly_separate(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        requested: list[str] = []
        page.on("request", lambda request: requested.append(urlparse(request.url).path))
        try:
            page.route(
                "**/api/v2/operations-bundle",
                lambda route: route.fulfill(status=404, body="{}"),
            )
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)

            assert "/api/v1/operations-bundle" in requested
            assert "/api/v1/pulse" in requested
            for forbidden in (
                "/api/v1/snapshot",
                "/api/v1/graph",
                "/api/v1/context",
                "/api/v1/timeline",
                "/api/v1/operations",
                "/api/v1/read-status",
            ):
                assert forbidden not in requested

            trigger = page.locator("#fleet [data-row]").first
            trigger.focus()
            page.keyboard.press("Enter")
            page.locator("#drawer .tlblock").wait_for()
            assert "/api/v1/timeline" not in requested
            page.keyboard.press("Escape")
            page.wait_for_function(
                "element => element === document.activeElement", arg=trigger.element_handle()
            )

            page.locator("#tab-pulse").click()
            receipt = page.locator(".pulse-refresh-receipt").inner_text().lower()
            assert "compatibility pulse receipt" in receipt
            assert "refreshed independently from topology" in receipt
        finally:
            browser.close()
        errors[:] = [value for value in errors if "404" not in value]
        assert not errors, errors


def test_failed_bundle_refresh_retains_last_good_rows_and_discloses_degradation(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        try:
            page.route(
                "**/api/v2/operations-bundle",
                lambda route: route.fulfill(status=404, body="{}"),
            )
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)
            before = page.locator(".matrix tr").count()

            page.route(
                "**/api/v1/operations-bundle",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"error":"test failure"}',
                ),
            )
            page.evaluate("refresh()")
            alert = page.locator("#mapreadalert:not([hidden])")
            alert.wait_for()
            disclosure = alert.inner_text().lower()
            assert "degraded read" in disclosure
            assert "last-good rows retained" in disclosure
            assert "does not claim complete live state" in disclosure
            assert page.locator(".matrix tr").count() == before
            assert "DEGRADED" in page.locator("#mapmeta").inner_text()
            receipt = page.locator("#fleet > details.projection-receipt")
            assert receipt.get_attribute("data-projection-state") == "degraded"
            assert receipt.get_attribute("open") is not None
            assert "projection degraded" in receipt.locator("summary").inner_text().lower()
        finally:
            browser.close()
        errors[:] = [value for value in errors if "404" not in value]
        assert not errors, errors


def test_embedded_mesh_reserves_zero_shell_height_and_keeps_route_actions(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        try:
            page.goto(f"{url}/mesh?embedded=1", wait_until="networkidle")
            page.wait_for_function("document.querySelector('#coverage-summary')?.dataset.shown")
            shell = page.locator(".shellbar")
            shell_metrics = shell.evaluate(
                "element => ({height: element.getBoundingClientRect().height, "
                "display: getComputedStyle(element).display, "
                "minHeight: getComputedStyle(element).minHeight})"
            )
            assert shell_metrics == {
                "height": 0,
                "display": "none",
                "minHeight": "0px",
            }
            command = page.locator(".mesh-command")
            for selector in ("#mesh-freeze", "#mesh-refresh"):
                action = command.locator(selector)
                assert action.is_visible()
                assert action.bounding_box()["height"] > 0
            assert page.locator(".mesh-rail").is_hidden()
        finally:
            browser.close()
        assert not errors, errors


def test_tab_keyboard_reload_native_map_and_drawer_focus_persist(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        try:
            page.goto(f"{url}/map?native_map=1", wait_until="networkidle")
            _wait_for_map(page)
            assert page.locator("html").get_attribute("data-embedded") == "1"
            assert page.locator(".shellbar").is_hidden()
            assert (
                page.locator("#tab-fleet").evaluate(
                    "element => parseFloat(getComputedStyle(element).fontSize)"
                )
                >= 14
            )
            assert (
                page.locator("#fleet .wisp b").first.evaluate(
                    "element => parseFloat(getComputedStyle(element).fontSize)"
                )
                >= 14
            )

            page.locator("#tab-fleet").focus()
            page.keyboard.press("ArrowRight")
            assert page.locator("#tab-pulse").get_attribute("aria-selected") == "true"
            assert page.locator("#tab-pulse").get_attribute("tabindex") == "0"
            assert page.locator("#pulse").get_attribute("hidden") is None
            assert page.locator("#fleet").get_attribute("hidden") == ""
            assert "native_map=1" in page.url
            assert (
                page.locator(".pulse-refresh-receipt").evaluate(
                    "element => parseFloat(getComputedStyle(element).fontSize)"
                )
                >= 12
            )
            assert "lens=pulse" in page.url

            page.reload(wait_until="networkidle")
            _wait_for_map(page)
            assert page.locator("#tab-pulse").get_attribute("aria-selected") == "true"
            assert page.locator("#pulse").get_attribute("hidden") is None
            assert page.locator("html").get_attribute("data-embedded") == "1"
        finally:
            browser.close()
        assert not errors, errors


def test_one_vertical_has_no_duplicate_all_column_and_no_overclaim_text(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)

        def one_vertical(route) -> None:
            response = route.fetch()
            bundle = response.json()
            for row in bundle["snapshot"]["rows"]:
                row["module"] = "only-vertical"
            bundle["snapshot"]["rows"][0]["status"] = "failed"
            bundle["snapshot"]["rows"][1]["status"] = "future_lifecycle_state"
            projection = bundle["bundle_projection"]
            published = len(bundle["snapshot"]["rows"])
            projection.update(
                {
                    "applied": True,
                    "snapshot_rows": {"published": published, "omitted": 7},
                    "graph_nodes": {
                        "published": len(bundle["graph"]["nodes"]),
                        "omitted": 3,
                    },
                    "graph_edges": {
                        "published": len(bundle["graph"]["edges"]),
                        "omitted": 2,
                    },
                    "context": {
                        "published": len(bundle["context"]["items"]),
                        "omitted": 5,
                    },
                    "timeline": {
                        "published": len(bundle["timeline"]["items"]),
                        "omitted": 4,
                    },
                }
            )
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", one_vertical)
        try:
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)

            headers = page.locator("#fleet .matrix tr").first.locator("th").all_inner_texts()
            assert [header.lower() for header in headers] == ["agent \\ vertical", "only-vertical"]
            assert page.locator("#fleet .matrix tr").nth(1).locator("td").count() == 1

            receipt = page.locator("#fleet > details.projection-receipt")
            published = page.evaluate("snapshot.rows.length")
            assert receipt.get_attribute("data-projection-state") == "capped"
            assert receipt.get_attribute("open") is not None
            summary = receipt.locator("summary").inner_text().lower()
            assert "projection capped" in summary
            assert f"{published} rows" in summary
            exact = receipt.locator(".projection-detail").inner_text().lower()
            assert f"snapshot rows: {published} published of {published + 7}" in exact
            assert (
                f"{published} of {published + 7} rows published"
                in page.locator("#mapmeta").inner_text().lower()
            )

            failed_id, unknown_id = page.evaluate("[snapshot.rows[0].id, snapshot.rows[1].id]")
            assert page.evaluate("stateOf(snapshot.rows[0])") == "attention"
            assert page.evaluate("stateOf(snapshot.rows[1])") == "unknown"
            page.evaluate("id => select(id)", failed_id)
            assert page.locator("#drawer .badge.attention").inner_text().lower() == "failed"
            page.evaluate("id => select(id)", unknown_id)
            assert (
                page.locator("#drawer .badge.unknown").inner_text().lower()
                == "future_lifecycle_state"
            )
            page.keyboard.press("Escape")

            for lens in ("shape", "context", "pulse", "deps"):
                _activate_lens(page, lens)
                assert page.locator(f"#{lens} > .projection-receipt").count() == 1

            visible_text = page.locator("body").inner_text().lower()
            for banned in ("unblocks", "minimum duration", "duration floor", "moving"):
                assert banned not in visible_text
            assert "recorded downstream reach" in visible_text
            assert "recorded prerequisite order" in visible_text
        finally:
            browser.close()
        errors[:] = [value for value in errors if "404" not in value]
        assert not errors, errors


def test_board_hash_capsule_selection_preserves_lens_native_and_other_fields(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        try:
            page.goto(
                f"{url}/map?native_map=1&lens=shape#v=work&sel=UI-103&q=owner%3Acodex",
                wait_until="networkidle",
            )
            _wait_for_map(page)
            page.locator("#drawer .close").wait_for()
            assert page.evaluate("selected") == "UI-103"
            assert page.locator("#tab-shape").get_attribute("aria-checked") == "true"
            assert page.locator("#map-more-trigger").get_attribute("aria-selected") == "true"
            assert page.locator("#map-more-trigger .map-more-label").inner_text() == "More · Shape"
            assert "native_map=1" in page.url

            page.evaluate("select('PLT-302')")
            parsed = urlparse(page.url)
            capsule = parse_qs(parsed.fragment)
            assert capsule == {"v": ["work"], "sel": ["PLT-302"], "q": ["owner:codex"]}
            assert parse_qs(parsed.query) == {"native_map": ["1"], "lens": ["shape"]}

            page.keyboard.press("Escape")
            parsed = urlparse(page.url)
            assert parse_qs(parsed.fragment) == {"v": ["work"], "q": ["owner:codex"]}
            assert parse_qs(parsed.query) == {"native_map": ["1"], "lens": ["shape"]}
        finally:
            browser.close()
        assert not errors, errors


def test_zero_edge_receipt_disables_relationship_lenses_and_pulse_zero_is_truthful(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)

        def zero_edges(route) -> None:
            response = route.fetch()
            bundle = response.json()
            bundle["graph"]["edges"] = []
            bundle["bundle_projection"]["graph_edges"] = {"published": 0, "omitted": 0}
            bundle["operations"]["topology_availability"] = {
                "schema_version": "TopologyAvailabilityV1",
                "state": "low_information",
                "reason_code": "authoritative_relationships_absent",
                "missing_prerequisite": "authoritative_graph_relationships",
                "population": {"edges": 0},
                "admitted": {"edges": 0},
                "omitted": {"edges": 0},
                "source": {"graph_declared": "coord.db+job_progress"},
            }
            route.fulfill(response=response, json=bundle)

        def zero_pulse(route) -> None:
            response = route.fetch()
            document = response.json()
            document["counts"]["events"] = 0
            document["counts"]["rows"] = 0
            document["counts"]["rows_with_events"] = 0
            for key in ("kinds", "days", "traffic", "traffic_undirected", "recent"):
                document[key] = []
            route.fulfill(response=response, json=document)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", zero_edges)
        page.route("**/api/v1/pulse", zero_pulse)
        try:
            page.goto(f"{url}/map?lens=deps", wait_until="networkidle")
            _wait_for_map(page)

            assert page.locator("#tab-fleet").get_attribute("aria-selected") == "true"
            for lens in ("ceiling", "deps", "crossings"):
                tab = page.locator(f"#tab-{lens}")
                assert tab.is_disabled()
                assert tab.get_attribute("aria-disabled") == "true"
            relationship = page.locator("#fleet .relationship-receipt").inner_text().lower()
            assert "coord.db+job_progress" in relationship
            assert "admits 0 recorded edges" in relationship
            assert "no relationships are inferred" in relationship

            populated = page.eval_on_selector_all(
                "main .panel", "panels => panels.filter(p => p.innerHTML.trim()).map(p => p.id)"
            )
            assert populated == ["fleet"]

            page.locator("#tab-pulse").click()
            pulse_text = page.locator("#pulse").inner_text().lower()
            assert "zero pulse occurrences, not zero rows" in pulse_text
            assert "snapshot bundle publishes" in pulse_text
            assert "0 rows exist" not in pulse_text
            assert page.locator("#shape").inner_html() == ""
        finally:
            browser.close()
        errors[:] = [value for value in errors if "404" not in value]
        assert not errors, errors


def test_slow_older_bundle_cannot_overwrite_newer_refresh(tmp_path: Path) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, errors = _page(browser)
        try:
            page.route(
                "**/api/v2/operations-bundle",
                lambda route: route.fulfill(status=404, body="{}"),
            )
            page.goto(f"{url}/map", wait_until="networkidle")
            _wait_for_map(page)
            result = page.evaluate(
                """
                async () => {
                  const base = {
                    schema_version: "OpsAtlasBundleV1",
                    cache_generation: readStatus.cache_generation,
                    snapshot: structuredClone(snapshot),
                    graph: structuredClone(graph),
                    context: structuredClone(context),
                    timeline: structuredClone(timeline),
                    operations: structuredClone(operations),
                    read_status: structuredClone(readStatus),
                    bundle_projection: structuredClone(bundleProjection),
                  };
                  let releaseSlow;
                  let firstSignal;
                  let calls = 0;
                  fetchDocument = async (path, options = {}) => {
                    if (path === "/api/v2/operations-bundle") throw new Error("HTTP 404");
                    if (path !== "/api/v1/operations-bundle") throw new Error("unexpected path");
                    calls += 1;
                    const answer = structuredClone(base);
                    if (calls === 1) {
                      firstSignal = options.signal;
                      answer.snapshot.rows[0].title = "STALE RESPONSE";
                      await new Promise(resolve => { releaseSlow = resolve; });
                    } else {
                      answer.snapshot.rows[0].title = "NEWER RESPONSE";
                    }
                    return answer;
                  };
                  const slow = refreshCore();
                  await new Promise(resolve => setTimeout(resolve, 0));
                  const fast = refreshCore();
                  await fast;
                  const aborted = firstSignal.aborted;
                  releaseSlow();
                  await slow;
                  return { title: snapshot.rows[0].title, aborted, calls };
                }
                """
            )
            assert result == {"title": "NEWER RESPONSE", "aborted": True, "calls": 2}
            page.evaluate("select(snapshot.rows[0].id)")
            assert "NEWER RESPONSE" in page.locator("#drawer").inner_text()
            assert "STALE RESPONSE" not in page.locator("#drawer").inner_text()
        finally:
            browser.close()
        errors[:] = [value for value in errors if "404" not in value]
        assert not errors, errors
