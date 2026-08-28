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


def _zero_relationship_bundle(route) -> None:
    response = route.fetch()
    bundle = response.json()
    operations = bundle["operations"]
    envelope = operations["graph_envelope"]
    nodes = envelope["nodes"][:13]
    assert len(nodes) == 13

    envelope["nodes"] = nodes
    envelope["edges"] = []
    for field in ("population", "eligible", "emitted"):
        envelope[field]["nodes"] = 13
        envelope[field]["edges"] = 0
    envelope["omitted"]["nodes"] = 0
    envelope["omitted"]["edges"] = 0
    envelope["omitted"]["node_reasons"] = []
    envelope["omitted"]["edge_reasons"] = []
    envelope["complete"] = True
    envelope["admission"].update(
        state="low_information",
        reason_code="authoritative_relationships_absent",
        reason=(
            "The authoritative graph publishes no relationships; topology "
            "motion and connectivity must not be inferred."
        ),
        missing_prerequisite="authoritative_graph_relationships",
        population={"nodes": 13, "edges": 0},
        eligible={"nodes": 13, "edges": 0},
        admitted={"nodes": 13, "edges": 0},
        omitted={
            "nodes": 0,
            "edges": 0,
            "node_reasons": [],
            "edge_reasons": [],
        },
    )

    availability = operations["topology_availability"]
    availability.update(
        state="low_information",
        reason_code="authoritative_relationships_absent",
        reason=(
            "The authoritative graph publishes no relationships; topology "
            "motion and connectivity must not be inferred."
        ),
        missing_prerequisite="authoritative_graph_relationships",
    )
    availability["population"]["nodes"] = 13
    availability["population"]["edges"] = 0
    availability["admitted"]["nodes"] = 13
    availability["admitted"]["edges"] = 0
    availability["omitted"]["nodes"] = 0
    availability["omitted"]["edges"] = 0
    availability["omitted"]["node_reasons"] = []
    availability["omitted"]["edge_reasons"] = []

    operations["metrics"]["graph_nodes"] = 13
    operations["metrics"]["graph_nodes_emitted"] = 13
    operations["metrics"]["graph_edges"] = 0
    operations["metrics"]["graph_edges_emitted"] = 0
    bundle["graph"]["nodes"] = nodes
    bundle["graph"]["edges"] = []
    route.fulfill(response=response, json=bundle)


def test_low_topology_keeps_nodes_static_searchable_and_auditable(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1024, "height": 1000})
        page = context.new_page()
        console_errors: list[str] = []
        failed_requests: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(
                f"{request.url}: {request.failure or 'request failed'}"
            ),
        )
        page.add_init_script(
            """
            (() => {
              const original = window.requestAnimationFrame.bind(window);
              window.__meshRafCallbacks = 0;
              window.requestAnimationFrame = callback => original(timestamp => {
                window.__meshRafCallbacks += 1;
                return callback(timestamp);
              });
            })();
            """
        )
        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", _zero_relationship_bundle)

        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            page.locator("#mesh-topology-receipt").wait_for(state="visible")
            page.locator("#static-node-roster button").first.wait_for(state="visible")

            assert page.locator("body").evaluate(
                "element => element.classList.contains('mesh-low-information')"
            )
            assert page.locator("#rail-nodes").inner_text() == "13"
            assert page.locator("#rail-edges").inner_text() == "0"
            viewport = page.locator("#mesh-viewport")
            assert viewport.get_attribute("role") == "application"
            assert page.locator("#static-node-roster button").count() == 13
            assert "13 nodes" in page.locator("#mesh-a11y").inner_text()
            assert page.locator('#mesh-a11y [role="button"]').count() == 13

            facts = page.locator("#mesh-topology-facts").inner_text().lower()
            for label in ("population", "admission", "source", "freshness"):
                assert label in facts
            assert "13 nodes" in facts
            assert "0 edges" in facts
            assert (
                "authoritative_graph_relationships"
                in page.locator("#mesh-topology-prerequisite").inner_text()
            )

            assert page.locator(".telemetry-panel:visible").count() == 0
            assert page.locator("[data-motion]").count() == 4
            assert page.locator("[data-motion]:disabled").count() == 4
            assert page.locator("[data-question]").count() == 4
            assert page.locator("[data-question]:disabled").count() == 4
            assert "MOTION UNAVAILABLE" in page.locator("#motion-truth").inner_text()
            critical = page.locator('[data-layout="critical"]')
            assert critical.is_disabled()
            assert critical.get_attribute("aria-describedby") == "mesh-caption"
            assert page.locator('[data-layout="context"]').get_attribute("aria-pressed") == "true"
            perspective = page.locator('[data-projection="perspective"]')
            flat = page.locator('[data-projection="flat"]')
            assert perspective.is_disabled()
            assert perspective.get_attribute("aria-describedby") == "mesh-caption"
            assert flat.get_attribute("aria-pressed") == "true"
            assert "0 ADMITTED EDGES" in page.locator("#projection-truth").inner_text()
            low_caption = page.locator("#mesh-caption").text_content().lower()
            assert "perspective and critical flow are unavailable" in low_caption
            assert "no links, particles, paths, or motion are synthesized" in low_caption
            assert "arrow keys move node focus" in low_caption
            assert "enter pins the focused node" in low_caption
            active_before = viewport.get_attribute("aria-activedescendant")
            assert active_before
            viewport.focus()
            active_after = active_before
            for key in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"):
                viewport.press(key)
                active_after = viewport.get_attribute("aria-activedescendant")
                if active_after != active_before:
                    break
            assert active_after and active_after != active_before
            playwright_api.expect(page.locator("#mesh-status-announcer")).to_contain_text(
                "Focused", timeout=2_000
            )
            viewport.press("Enter")
            playwright_api.expect(page.locator("#mesh-status-announcer")).to_contain_text(
                "Selected", timeout=2_000
            )
            active_node = page.locator(f'[id="{active_after}"]')
            assert active_node.get_attribute("role") == "button"
            assert active_node.evaluate("element => element.tabIndex") == -1
            assert active_node.get_attribute("aria-current") == "true"
            page.locator("#clear-selection").click()
            perspective.evaluate("element => element.click()")
            assert flat.get_attribute("aria-pressed") == "true"
            assert page.locator("#mesh-canvas").get_attribute("data-motion-edge-count") == "0"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

            register = page.locator("#static-node-roster")
            last_roster_node = page.locator("#static-node-roster button").last
            last_roster_node.scroll_into_view_if_needed()
            register_box = register.bounding_box()
            last_box = last_roster_node.bounding_box()
            assert register_box is not None and last_box is not None
            assert last_box["y"] >= register_box["y"] - 1
            assert last_box["y"] + last_box["height"] <= (
                register_box["y"] + register_box["height"] + 1
            )

            page.wait_for_timeout(250)
            raf_before = page.evaluate("window.__meshRafCallbacks")
            still_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(300)
            raf_after = page.evaluate("window.__meshRafCallbacks")
            still_b = page.locator("#mesh-canvas").screenshot()
            assert raf_after == raf_before
            assert still_b == still_a

            first = page.locator("#static-node-roster button").first
            node_id = first.get_attribute("data-static-node")
            assert node_id
            page.locator("#mesh-search").fill(node_id)
            page.locator("#mesh-search").press("Enter")
            assert node_id in page.locator("#selection-inspector").inner_text()
            playwright_api.expect(page.locator("#mesh-status-announcer")).to_contain_text(
                "Selected"
            )
            assert page.locator("#static-node-roster button").count() == 13
            assert page.locator("#static-node-roster .search-match").count() >= 1

            page.locator("#camera-fit").click()
            assert page.locator("#camera-zoom").inner_text().endswith("%")
            critical.evaluate("element => element.click()")
            assert page.locator('[data-layout="context"]').get_attribute("aria-pressed") == "true"
            assert page.locator("#mesh-topology-receipt").is_visible()
            assert page.locator("#static-node-roster button").count() == 13

            for selector in (
                "#mesh-search",
                "#camera-fit",
                "#camera-zoom",
                "#clear-selection",
            ):
                assert (
                    page.locator(selector).evaluate(
                        "element => parseFloat(getComputedStyle(element).fontSize)"
                    )
                    >= 14
                )
            for selector in (
                ".topology-facts dt",
                ".topology-facts dd",
                ".topology-prerequisite",
                ".static-node-register button span",
            ):
                assert (
                    page.locator(selector).first.evaluate(
                        "element => parseFloat(getComputedStyle(element).fontSize)"
                    )
                    >= 12
                )
        finally:
            context.close()
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
