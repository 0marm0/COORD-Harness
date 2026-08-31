from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from urllib.request import urlopen

from jsonschema import Draft202012Validator
import pytest
from referencing import Registry, Resource

from coordharness import demo
from coordharness.board.server import make_server
from coordharness.coord import coord_db
from coordharness.coord.config import connect


playwright_api = pytest.importorskip("playwright.sync_api")
REPO = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO / "src" / "coordharness" / "board"
PRIVATE_SENTINEL = "CANARY-board-comms-private-prose-7f31"
WITHHELD_EVENT_KEYS = {
    "body",
    "payload_json",
    "refs_json",
    "session_id",
    "severity",
    "verdict",
    "trust",
}


def test_continuous_height_messages_target_the_embedding_parent_origin() -> None:
    source = (SCHEMA_DIR / "static" / "cockpit.js").read_text(encoding="utf-8")
    assert "new URL(document.referrer).origin" in source
    assert '}, parentOrigin);' in source
    assert '}, location.origin);' not in source


def test_continuous_fleet_matrix_uses_compact_information_dense_geometry() -> None:
    css = (SCHEMA_DIR / "static" / "cockpit.css").read_text(encoding="utf-8")
    assert ".continuous-section-fleet #fleet .matrix th," in css
    assert "height: 30px;" in css
    assert "height: 66px;" in css
    assert "padding: .22rem .38rem;" in css
    assert ".continuous-section-fleet #fleet .legend" in css


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _nested_keys(item)
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found |= _nested_keys(item)
        return found
    return set()


@contextmanager
def _board(tmp_path: Path):
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    conn = connect(database)
    try:
        coord_db.post_event(
            conn,
            kind="handoff",
            actor="codex",
            to_selector="actor:claude",
            work_id="ML-202",
            session_id=PRIVATE_SENTINEL,
            severity="high",
            verdict="pass",
            title=f"title {PRIVATE_SENTINEL}",
            body=f"body {PRIVATE_SENTINEL}",
            refs_json=json.dumps([PRIVATE_SENTINEL]),
            payload_json=json.dumps({"private": PRIVATE_SENTINEL}),
            idempotency_key="board-comms-private-canary",
        )
        conn.commit()
    finally:
        conn.close()
    server = make_server(host="127.0.0.1", port=0, db_path=str(database))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _document(url: str, path: str) -> dict:
    with urlopen(f"{url}{path}") as response:
        return json.load(response)


def _v2_validator() -> Draft202012Validator:
    bundle = json.loads((SCHEMA_DIR / "ops_atlas_bundle_v2.schema.json").read_text())
    operations = json.loads((SCHEMA_DIR / "ops_atlas_v1.schema.json").read_text())
    graph = json.loads((SCHEMA_DIR / "graph_envelope_v1.schema.json").read_text())
    status = json.loads((SCHEMA_DIR / "read_status_v1.schema.json").read_text())
    for schema in (bundle, operations, graph, status):
        Draft202012Validator.check_schema(schema)
    registry = Registry()
    for schema in (operations, graph, status):
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    registry = registry.with_resource(
        "https://coordharness.dev/schema/graph_envelope_v1.schema.json",
        Resource.from_contents(graph),
    )
    return Draft202012Validator(bundle, registry=registry)


def test_v2_bundle_schema_and_pulse_exclude_private_event_detail(tmp_path: Path) -> None:
    with _board(tmp_path) as url:
        bundle = _document(url, "/api/v2/operations-bundle")

    _v2_validator().validate(bundle)
    pulse = bundle["pulse"]
    assert pulse["schema_version"] == "PulseV1"
    assert WITHHELD_EVENT_KEYS.isdisjoint(_nested_keys(pulse))
    assert PRIVATE_SENTINEL not in json.dumps(pulse, sort_keys=True)
    assert any(
        route["kind"] == "handoff" and route["from"] == "codex" and route["to"] == "claude"
        for route in pulse["traffic"]
    )


def test_continuous_comms_is_the_only_loopback_embeddable_board_page(tmp_path: Path) -> None:
    with _board(tmp_path) as url:
        for path in ("/", "/map", "/map?embedded=1", "/map?continuous=1"):
            with urlopen(f"{url}{path}") as response:
                assert response.headers["X-Frame-Options"] == "DENY"
                assert "frame-ancestors 'none'" in response.headers[
                    "Content-Security-Policy"
                ]

        with urlopen(f"{url}/map?embedded=1&continuous=1") as response:
            assert response.headers.get("X-Frame-Options") is None
            frame_policy = response.headers["Content-Security-Policy"]
            assert (
                "frame-ancestors 'self' http://127.0.0.1:* "
                "http://localhost:*"
            ) in frame_policy
            assert "http://127.0.0.1:65535" not in frame_policy
            assert "http://localhost:65535" not in frame_policy
            assert "frame-ancestors *" not in frame_policy
            assert "frame-ancestors 'none'" not in frame_policy


def test_continuous_comms_section_embeds_publish_one_canonical_surface(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 960, "height": 800})
        try:
            for section, selector in (
                ("fleet", "#fleet .matrix"),
                ("deps", '#deps .gwrap svg[aria-label="Dependency graph"]'),
                ("pulse", "#pulse .pulse-refresh-receipt"),
            ):
                page.goto(
                    f"{url}/map?embedded=1&continuous=1&section={section}",
                    wait_until="networkidle",
                )
                page.locator(selector).wait_for()
                assert page.locator("body").get_attribute("class").split().count(
                    f"continuous-section-{section}"
                ) == 1
                assert page.locator("#maptabs:visible").count() == 0
                assert page.locator("main > .panel").evaluate_all(
                    "panels => panels.filter(panel => !panel.hidden).map(panel => panel.id)"
                ) == [section]
                assert page.locator("main > .panel").evaluate_all(
                    "(panels, section) => panels.every(panel => panel.id === section || panel.hidden)",
                    section,
                )
                receipt = page.locator(f"#{section} .projection-receipt-compact")
                assert receipt.count() == 1
                assert not receipt.evaluate("details => details.open")
                summary = receipt.locator("summary").inner_text()
                assert summary.startswith("Coverage ")
                assert "Projection capped" not in summary
                assert "Exact counts" in summary
                assert "published of" in receipt.locator(".projection-detail").text_content()
                if section == "fleet":
                    fleet = page.locator("#fleet")
                    assert "Who is working where" not in fleet.inner_text()
                    assert fleet.locator(".continuous-fleet-label").count() == 0
                    assert fleet.locator(".lanes").count() == 0
                    assert fleet.locator("#fleet-matrix-label").inner_text() == (
                        "Agent by vertical matrix"
                    )
                    matrix = fleet.locator(".matrix")
                    assert matrix.get_attribute("aria-labelledby") == "fleet-matrix-label"
                    assert matrix.get_attribute("aria-describedby") == "fleet-matrix-description"
                    assert matrix.evaluate(
                        "(matrix, receipt) => Boolean(matrix.compareDocumentPosition(receipt) & Node.DOCUMENT_POSITION_FOLLOWING)",
                        receipt.element_handle(),
                    )
                    assert matrix.bounding_box()["y"] < receipt.bounding_box()["y"]
                    assert fleet.locator("#fleet-matrix-description").evaluate(
                        "node => node.classList.contains('visually-hidden')"
                    )
                if section == "deps":
                    deps = page.locator("#deps")
                    assert deps.get_by_role("heading", name="What waits on what").count() == 1
                    assert deps.locator(".structural-focus-receipt").count() == 1
                    assert deps.get_by_role(
                        "heading", name="Recorded downstream reach"
                    ).count() == 1
        finally:
            browser.close()


def test_embedded_board_comms_keeps_complete_single_page_composition(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        try:
            page.goto(f"{url}/?embedded=1#v=comms", wait_until="networkidle")
            traffic = page.locator("#comms.active [data-comms-traffic]")
            traffic.locator(
                '[aria-label="Recorded-direction traffic visualization"] svg'
            ).wait_for()
            fleet_frame = page.frame_locator('[data-comms-frame="fleet"]')
            deps_frame = page.frame_locator('[data-comms-frame="deps"]')
            pulse_frame = page.frame_locator('[data-comms-frame="pulse"]')
            fleet_frame.locator("#fleet .matrix").wait_for()
            deps_frame.locator('#deps .gwrap svg[aria-label="Dependency graph"]').wait_for()
            pulse_frame.locator("#pulse .pulse-refresh-receipt").wait_for()

            assert page.locator("html").get_attribute("data-embedded") == "1"
            assert page.locator("#comms.active [data-comms-continuous]").count() == 3
            assert {
                path.split("section=")[-1]
                for path in requests
                if "/map?embedded=1&continuous=1&section=" in path
            } == {"fleet", "deps", "pulse"}
            assert page.locator("#comms .comms-shell > *").evaluate_all(
                """nodes => nodes.map(node =>
                  node.classList.contains("comms-jump") ? "jump" :
                  (node.dataset.commsSurface ||
                  (node.dataset.commsTraffic === "" ? "traffic" :
                  (node.matches("[data-comms-fleet-status]") ? "status" : "unexpected")))
                )"""
            ) == ["jump", "fleet", "traffic", "deps", "pulse", "status"]
            assert traffic.locator(":scope > .comms-kpis").count() == 1
            assert fleet_frame.locator(".continuous-fleet-label").count() == 0
            assert fleet_frame.locator(".lanes").count() == 0
            assert page.locator(".comms-jump [data-comms-jump]").evaluate_all(
                "buttons => buttons.map(button => button.dataset.commsJump)"
            ) == [
                "#comms-fleet",
                "#comms-traffic",
                "#comms-dependencies",
                "#comms-pulse",
            ]
            jump_box = page.locator(".comms-jump").bounding_box()
            assert jump_box is not None and jump_box["y"] < 900
            page.get_by_role("button", name="Dependencies", exact=True).click()
            assert page.url.endswith("#v=comms")
            assert page.locator(".panes").evaluate("pane => pane.scrollTop") > 0

            page.evaluate(
                """() => {
                  window.__embeddedTraffic = document.querySelector('[data-comms-traffic]');
                  window.__embeddedFrames = [...document.querySelectorAll('[data-comms-continuous]')];
                }"""
            )
            with page.expect_response(
                lambda response: response.url.endswith("/api/v2/operations-bundle")
            ):
                page.evaluate("refreshBoard()")
            page.wait_for_timeout(200)
            assert page.evaluate(
                "window.__embeddedTraffic === document.querySelector('[data-comms-traffic]')"
            )
            assert page.evaluate(
                """() => window.__embeddedFrames.length === 3 &&
                window.__embeddedFrames.every(
                  (frame, index) => frame === document.querySelectorAll(
                    '[data-comms-continuous]'
                  )[index]
                )"""
            )
        finally:
            browser.close()


def test_board_comms_is_one_stable_continuous_destination_at_wide_width(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.add_init_script(
            """
            window.__continuousHeightMessages = [];
            addEventListener("message", event => {
              if (event.data?.type === "coord.continuous-comms.height") {
                window.__continuousHeightMessages.push(event.data.height);
              }
            });
            """
        )
        try:
            page.goto(f"{url}/#v=comms", wait_until="networkidle")
            frame_elements = page.locator("#comms.active [data-comms-continuous]")
            assert frame_elements.count() == 3
            fleet_frame = page.frame_locator('[data-comms-frame="fleet"]')
            deps_frame = page.frame_locator('[data-comms-frame="deps"]')
            pulse_frame = page.frame_locator('[data-comms-frame="pulse"]')
            fleet_frame.locator("#fleet .matrix").wait_for()
            deps_frame.locator('#deps .gwrap svg[aria-label="Dependency graph"]').wait_for()
            pulse_frame.locator("#pulse .pulse-refresh-receipt").wait_for()

            assert page.locator(".shell-subnav [data-shell-destination=comms]:visible").count() == 1
            assert page.locator("[data-shell-destination=fleet], [data-shell-destination=pulse]").count() == 0
            assert page.locator("#comms.active").count() == 1
            assert page.locator("#crumbs").inner_text() == "Work/Comms"
            assert fleet_frame.locator("#maptabs:visible").count() == 0
            assert deps_frame.locator("#maptabs:visible").count() == 0
            assert pulse_frame.locator("#maptabs:visible").count() == 0
            assert fleet_frame.locator("main > .panel").evaluate_all(
                "panels => panels.filter(panel => !panel.hidden).map(panel => panel.id)"
            ) == ["fleet"]
            assert pulse_frame.locator("main > .panel").evaluate_all(
                "panels => panels.filter(panel => !panel.hidden).map(panel => panel.id)"
            ) == ["pulse"]
            assert deps_frame.locator("main > .panel").evaluate_all(
                "panels => panels.filter(panel => !panel.hidden).map(panel => panel.id)"
            ) == ["deps"]
            assert page.locator("#comms .comms-shell > section").evaluate_all(
                "nodes => nodes.map(node => node.dataset.commsSurface || (node.dataset.commsTraffic === '' ? 'traffic' : ''))"
            ) == ["fleet", "traffic", "deps", "pulse"]
            assert page.locator("#comms .comms-shell > *").evaluate_all(
                """nodes => nodes.map(node =>
                  node.classList.contains("comms-jump") ? "jump" :
                  (node.dataset.commsSurface ||
                  (node.dataset.commsTraffic === "" ? "traffic" :
                  (node.matches("[data-comms-fleet-status]") ? "status" : "unexpected")))
                )"""
            ) == ["jump", "fleet", "traffic", "deps", "pulse", "status"]
            fleet_box = page.locator('[data-comms-surface="fleet"]').bounding_box()
            kpi_box = page.locator(".comms-kpis").bounding_box()
            traffic_box = page.locator("[data-comms-traffic]").bounding_box()
            deps_box = page.locator('[data-comms-surface="deps"]').bounding_box()
            pulse_box = page.locator('[data-comms-surface="pulse"]').bounding_box()
            assert (
                fleet_box["y"]
                < traffic_box["y"]
                < kpi_box["y"]
                < deps_box["y"]
                < pulse_box["y"]
            )
            assert page.evaluate(
                "document.querySelector('[data-comms-surface=fleet]').nextElementSibling.matches('[data-comms-traffic]')"
            )
            assert fleet_frame.locator(".continuous-fleet-label").count() == 0
            assert fleet_frame.locator(".lanes").count() == 0
            page.locator("[data-comms-fleet-status]").wait_for(state="visible")
            assert "recorded running in this projection" in page.locator(
                "[data-comms-fleet-status]"
            ).inner_text()

            traffic_workspace = page.locator("[data-comms-traffic]")
            assert traffic_workspace.locator(
                '[aria-label="Recorded-direction traffic visualization"] svg'
            ).count() == 1
            assert traffic_workspace.locator(".comms-edge").count() > 0
            assert traffic_workspace.locator("[data-comms-filter]").count() == 3
            assert traffic_workspace.locator("[data-comms-event]").count() > 1
            assert traffic_workspace.locator(".comms-event-detail").count() == 1
            semantics = traffic_workspace.inner_text().lower()
            assert "not a live stream" in semantics
            assert "no delivery, read, accepted, or response state is inferred" in semantics
            second_event = traffic_workspace.locator("[data-comms-event]").nth(1)
            second_event.click()
            assert second_event.get_attribute("aria-pressed") == "true"
            traffic_workspace.locator('[data-comms-filter="kind"]').select_option("handoff")
            assert traffic_workspace.locator('[data-comms-filter="kind"]').input_value() == "handoff"
            assert traffic_workspace.locator("[data-comms-event]").count() >= 1
            assert all(
                "handoff" in text.lower()
                for text in traffic_workspace.locator(".comms-event small").all_text_contents()
            )
            traffic_workspace.locator("[data-comms-reset]").click()
            assert all(
                value == ""
                for value in traffic_workspace.locator("[data-comms-filter]").evaluate_all(
                    "filters => filters.map(filter => filter.value)"
                )
            )

            assert any(path.endswith("/api/v2/operations-bundle") for path in requests)
            assert not any(path.endswith("/api/v1/operations-bundle") for path in requests)
            assert not any(path.endswith("/api/v1/pulse") for path in requests)
            visible = page.locator("#comms").inner_text()
            assert PRIVATE_SENTINEL not in visible
            assert PRIVATE_SENTINEL not in page.locator("#comms").inner_html()
            assert PRIVATE_SENTINEL not in fleet_frame.locator("main").inner_text()
            assert PRIVATE_SENTINEL not in deps_frame.locator("main").inner_text()
            assert PRIVATE_SENTINEL not in pulse_frame.locator("main").inner_text()
            assert "recorded direction only" in visible.lower()
            assert "delivery status" in visible.lower()

            initial_map_reads = sum("/map?embedded=1&continuous=1" in path for path in requests)
            page.evaluate(
                """() => {
                  window.__continuousFrameIdentity =
                    [...document.querySelectorAll("[data-comms-continuous]")];
                }"""
            )
            with page.expect_response(
                lambda response: response.url.endswith("/api/v2/operations-bundle")
            ):
                page.evaluate("refreshBoard()")
            page.wait_for_timeout(300)
            assert page.evaluate(
                """() => window.__continuousFrameIdentity.length === 3 &&
                window.__continuousFrameIdentity.every(
                  (frame, index) => frame === document.querySelectorAll(
                    "[data-comms-continuous]"
                  )[index]
                )"""
            )
            assert sum("/map?embedded=1&continuous=1" in path for path in requests) == initial_map_reads

            page.wait_for_timeout(500)
            height_count = page.evaluate("window.__continuousHeightMessages.length")
            heights = frame_elements.evaluate_all(
                "frames => frames.map(frame => frame.style.height)"
            )
            page.wait_for_timeout(900)
            assert page.evaluate("window.__continuousHeightMessages.length") == height_count
            assert frame_elements.evaluate_all(
                "frames => frames.map(frame => frame.style.height)"
            ) == heights

            for legacy in ("fleet", "pulse"):
                page.goto(f"{url}/#v={legacy}", wait_until="networkidle")
                page.wait_for_url(f"{url}/#v=comms")
                assert page.url.endswith("#v=comms")
                assert page.locator("#comms.active [data-comms-continuous]").count() == 3
        finally:
            browser.close()


def test_board_comms_keeps_full_fleet_dependencies_and_pulse_at_narrow_width(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(f"{url}/#v=comms", wait_until="networkidle")
            fleet_frame = page.frame_locator('[data-comms-frame="fleet"]')
            deps_frame = page.frame_locator('[data-comms-frame="deps"]')
            pulse_frame = page.frame_locator('[data-comms-frame="pulse"]')
            fleet_frame.locator("#fleet .matrix").wait_for()
            deps_frame.locator('#deps .gwrap svg[aria-label="Dependency graph"]').wait_for()
            pulse_frame.locator("#pulse .pulse-refresh-receipt").wait_for()

            assert page.locator(".shell-subnav [data-shell-destination=comms]:visible").count() == 1
            assert fleet_frame.locator("#maptabs:visible").count() == 0
            assert deps_frame.locator("#maptabs:visible").count() == 0
            assert pulse_frame.locator("#maptabs:visible").count() == 0
            assert page.locator(".comms-jump [data-comms-jump]").all_inner_texts() == [
                "Fleet",
                "Traffic",
                "Dependencies",
                "Pulse",
            ]
            assert fleet_frame.locator("#fleet .matrix th").evaluate_all(
                "cells => cells.every(cell => getComputedStyle(cell).display !== 'none')"
            )
            assert fleet_frame.locator("#fleet .tablewrap").evaluate(
                "element => element.scrollWidth >= element.clientWidth"
            )
            traffic_workspace = page.locator("[data-comms-traffic]")
            assert traffic_workspace.locator("[data-comms-filter]").count() == 3
            direction = traffic_workspace.locator(".comms-direction").bounding_box()
            feed = traffic_workspace.locator(".comms-feed").bounding_box()
            detail = traffic_workspace.locator(".comms-detail").bounding_box()
            assert direction["y"] < feed["y"] < detail["y"]
            assert traffic_workspace.bounding_box()["width"] <= 390
            assert page.locator('[data-comms-surface="deps"]').bounding_box()["width"] <= 390
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            )
        finally:
            browser.close()


def test_board_comms_keeps_v1_server_compatibility(tmp_path: Path) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 850})
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.route(
            "**/api/v2/operations-bundle",
            lambda route: route.fulfill(
                status=404,
                content_type="application/json",
                body="{}",
            ),
        )
        try:
            page.goto(f"{url}/#v=comms", wait_until="networkidle")
            fleet_frame = page.frame_locator('[data-comms-frame="fleet"]')
            deps_frame = page.frame_locator('[data-comms-frame="deps"]')
            pulse_frame = page.frame_locator('[data-comms-frame="pulse"]')
            fleet_frame.locator("#fleet .matrix").wait_for()
            deps_frame.locator('#deps .gwrap svg[aria-label="Dependency graph"]').wait_for()
            pulse_frame.locator("#pulse .pulse-refresh-receipt").wait_for()

            assert any(path.endswith("/api/v2/operations-bundle") for path in requests)
            assert any(path.endswith("/api/v1/operations-bundle") for path in requests)
            assert any(path.endswith("/api/v1/pulse") for path in requests)
            assert PRIVATE_SENTINEL not in page.locator("#comms").inner_text()
            assert PRIVATE_SENTINEL not in fleet_frame.locator("main").inner_text()
            assert PRIVATE_SENTINEL not in deps_frame.locator("main").inner_text()
            assert PRIVATE_SENTINEL not in pulse_frame.locator("main").inner_text()
            assert "Compatibility Pulse receipt" in pulse_frame.locator(
                "#pulse .pulse-refresh-receipt"
            ).inner_text()
        finally:
            browser.close()
