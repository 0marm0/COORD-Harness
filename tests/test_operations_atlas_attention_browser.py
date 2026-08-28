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


def _page(browser, *, width: int = 1440, height: int = 900):
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


def _force_low_information(route) -> None:
    response = route.fetch()
    bundle = response.json()
    operations = bundle["operations"]
    envelope = operations["graph_envelope"]
    assert envelope["population"]["edges"] > 0
    envelope["edges"] = []
    envelope["population"]["edges"] = 0
    envelope["eligible"]["edges"] = 0
    envelope["emitted"]["edges"] = 0
    envelope["omitted"]["edges"] = 0
    envelope["omitted"]["edge_reasons"] = []
    envelope["complete"] = True
    envelope["admission"].update(
        state="low_information",
        reason_code="authoritative_relationships_absent",
        reason=(
            "The authoritative graph publishes no relationships; topology motion "
            "and connectivity must not be inferred."
        ),
        missing_prerequisite="authoritative_graph_relationships",
        population={"nodes": envelope["population"]["nodes"], "edges": 0},
        eligible={"nodes": envelope["eligible"]["nodes"], "edges": 0},
        admitted={"nodes": envelope["emitted"]["nodes"], "edges": 0},
        omitted={
            "nodes": envelope["omitted"]["nodes"],
            "edges": 0,
            "node_reasons": envelope["omitted"]["node_reasons"],
            "edge_reasons": [],
        },
    )
    availability = operations["topology_availability"]
    availability.update(
        state="low_information",
        reason_code="authoritative_relationships_absent",
        reason=(
            "The authoritative graph publishes no relationships; topology motion "
            "and connectivity must not be inferred."
        ),
        missing_prerequisite="authoritative_graph_relationships",
    )
    availability["population"]["edges"] = 0
    availability["admitted"]["edges"] = 0
    availability["omitted"]["edges"] = 0
    operations["metrics"]["graph_edges"] = 0
    operations["metrics"]["graph_edges_emitted"] = 0
    bundle["graph"]["edges"] = []
    route.fulfill(response=response, json=bundle)


def _font_size(locator) -> float:
    return locator.evaluate("element => parseFloat(getComputedStyle(element).fontSize)")


def test_low_information_attention_state_is_primary_truthful_and_actionable(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _page(browser)
        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", _force_low_information)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            warning = page.locator("#topology-low-information")
            warning.wait_for(state="visible")

            assert page.locator(".atlas-intro").is_hidden()
            assert page.locator(".document-rail").is_hidden()
            assert page.get_by_role("heading", name="Topology not admitted").is_visible()
            for selector in (
                "#topology-low-information-title",
                "#topology-low-information-reason",
                "#topology-low-information-facts",
            ):
                box = page.locator(selector).bounding_box()
                assert box is not None
                assert box["y"] + box["height"] <= 900

            explanations = page.locator("[data-absence-explanation='primary']")
            assert explanations.count() == 1
            reason = explanations.inner_text()
            assert page.locator("body").inner_text().count(reason) == 1
            assert page.locator("#topology-scope").inner_text() == ""
            assert page.locator("#topology-receipts").inner_text() == ""
            assert page.locator("#topology-caption").inner_text() == ""
            assert page.locator(".topology-foot").is_hidden()
            facts = page.locator("#topology-low-information-facts").inner_text().lower()
            assert "admission" in facts and "low information" in facts
            assert "source" in facts and "freshness" in facts

            assert page.locator("#atlas-questions").is_hidden()
            assert page.locator(".topology-actions").is_hidden()
            for selector in (
                "#atlas-status",
                "#atlas-module",
                "#atlas-hops",
                "#atlas-zoom-out",
                "#atlas-zoom-in",
                "#atlas-zoom-fit",
            ):
                assert page.locator(selector).is_disabled()
                assert page.locator(selector).is_hidden()

            search = page.locator("#atlas-search")
            assert search.is_visible()
            roster = page.locator("#topology-node-roster")
            assert roster.is_visible()
            initial = roster.locator("li").count()
            assert initial > 0
            node_id = roster.locator("li span").first.inner_text().split(" · ")[-1]
            search.fill(node_id)
            assert roster.locator("li").count() >= 1
            assert all(
                node_id.lower() in value.lower() for value in roster.locator("li").all_inner_texts()
            )

            receipts = page.locator("#topology-technical-receipt")
            assert receipts.count() == 1
            assert not receipts.evaluate("element => element.open")
            receipts.locator("summary").click()
            technical = page.locator("#topology-technical-facts").inner_text().lower()
            assert "reason code" in technical
            assert "population" in technical
            assert "admitted" in technical
            assert "omitted" in technical
            assert "authoritative_graph_relationships" in technical

            for selector in (
                "#topology-low-information-reason",
                ".low-information-facts dd",
                "#atlas-search",
                ".topology-node-roster li b",
                "#topology-technical-receipt summary",
            ):
                assert _font_size(page.locator(selector).first) >= 14
            for selector in (
                ".low-information-facts dt",
                ".technical-facts dt",
                ".technical-facts dd",
                ".topology-node-roster li span",
            ):
                assert _font_size(page.locator(selector).first) >= 12
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_normal_topology_keeps_graph_controls_node_roles_and_behavior(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _page(browser)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            first_node = page.locator(".atlas-node").first
            first_node.wait_for()

            assert page.locator("#topology-low-information").is_hidden()
            assert page.locator(".atlas-intro").is_visible()
            assert page.locator(".document-rail").is_visible()
            assert page.locator(".atlas-node").count() > 10
            assert page.locator(".atlas-edge").count() > 0
            assert page.locator("#topology-svg[role='group']").count() == 1
            assert page.locator("#topology-svg[role='img']").count() == 0

            node_name = first_node.get_attribute("aria-label")
            exposed_node = page.get_by_role("button", name=node_name, exact=True)
            assert exposed_node.count() == 1
            exposed_node.click()
            assert page.locator("#atlas-inspector .inspector-content").count() == 1

            assert page.locator("#atlas-questions").is_visible()
            assert page.locator(".topology-actions").is_visible()
            for selector in (
                "#atlas-status",
                "#atlas-module",
                "#atlas-zoom-out",
                "#atlas-zoom-in",
                "#atlas-zoom-fit",
            ):
                assert page.locator(selector).is_enabled()
                assert page.locator(selector).is_visible()
            assert page.locator("#atlas-hops").is_visible()
            assert page.locator("#atlas-hops").is_disabled()
            assert "only in Neighbourhood" in page.locator("#atlas-hops-help").inner_text()
            page.locator("[data-mode='neighbourhood']").click()
            assert page.locator("#atlas-hops").is_enabled()
            page.locator("[data-mode='critical']").click()
            assert page.locator("#atlas-hops").is_disabled()
            assert page.locator("[data-mode='critical']").get_attribute("aria-pressed") == "true"
            page.locator("#atlas-freeze").click()
            assert page.locator("#atlas-freeze").get_attribute("aria-pressed") == "true"
            assert page.locator("#atlas-refresh").is_disabled()

            for selector in (
                ".intro-copy > p",
                ".shellbar a",
                "#atlas-search",
                ".topology-actions button",
                "#atlas-freeze",
            ):
                assert _font_size(page.locator(selector).first) >= 14
            for selector in (
                "#atlas-clock",
                ".document-stage span",
                ".atlas-node .node-meta",
            ):
                assert _font_size(page.locator(selector).first) >= 12
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
