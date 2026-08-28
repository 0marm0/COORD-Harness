from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server
from coordharness.coord import coord_db
from coordharness.coord.config import connect


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
        yield server, db, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _browser_page(browser, *, width: int = 1440, height: int = 1050):
    try:
        page = browser.new_page(viewport={"width": width, "height": height})
    except TypeError:
        # BrowserContext receives its viewport at construction.
        page = browser.new_page()
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


def _first_owned_graph_row(server):
    graph_ids = {node["id"] for node in server.graph()["nodes"]}
    return next(
        value
        for value in server.snapshot()["rows"]
        if value.get("owner")
        and not value["id"].startswith("job:")
        and (value["id"] in graph_ids or f"work:{value['id']}" in graph_ids)
    )


def _post_occurrence(db: Path, *, row_id: str, actor: str, key: str) -> None:
    connection = connect(db)
    try:
        coord_db.post_event(
            connection,
            kind="note",
            actor=actor,
            work_id=row_id,
            idempotency_key=key,
        )
        connection.commit()
    finally:
        connection.close()


def _rebuild(server) -> None:
    generation = server.read_status()["cache_generation"]
    server._next_refresh = 0.0
    server.service_actions()
    assert server.read_status()["cache_generation"] == generation + 1


def test_operations_atlas_is_interactive_keyboard_safe_and_responsive(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            assert page.locator(".document-stage").count() == 6
            assert page.locator(".atlas-metric").count() == 8
            assert page.locator(".atlas-node").count() > 10
            assert page.locator(".atlas-edge").count() > 0
            assert page.locator("#topology-low-information").is_hidden()
            assert "Server envelope emitted" in page.locator("#topology-scope").inner_text()
            assert "cache generation" in page.locator("#atlas-clock").inner_text()

            page.keyboard.press("/")
            assert page.locator("#atlas-search").evaluate(
                "element => element === document.activeElement"
            )
            page.keyboard.press("Escape")

            first_node = page.locator(".atlas-node").first
            first_node.focus()
            page.keyboard.press("Enter")
            assert page.locator("#atlas-inspector .inspector-content").count() == 1

            page.locator('[data-mode="critical"]').click()
            page.locator('[data-mode="critical"].active').wait_for()
            if page.locator("#critical-strip button").count() > 1:
                page.locator(".atlas-edge.walking").wait_for(timeout=2500)

            page.locator("#atlas-freeze").click()
            assert page.locator("#atlas-freeze").get_attribute("aria-pressed") == "true"
            assert page.locator("#atlas-refresh").is_disabled()
            frozen_clock = page.locator("#atlas-clock").inner_text()
            page.locator("#atlas-refresh").evaluate("element => element.click()")
            assert page.locator("#atlas-clock").inner_text() == frozen_clock
            assert page.locator("body").evaluate(
                "element => element.classList.contains('atlas-frozen')"
            )
            page.locator("#atlas-freeze").click()

            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#atlas-zoom-fit").click()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_operating_focus_graph_first_geometry_and_embedded_controls(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser, width=1600, height=1000)
        try:
            page.goto(f"{url}/ops?embedded=1", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            assert page.locator(".shellbar").is_hidden()
            assert page.locator("#atlas-freeze").is_visible()
            assert page.locator("#atlas-refresh").is_visible()
            assert not page.locator("#atlas-prelude").evaluate("element => element.open")
            assert page.locator("#atlas-hops").is_disabled()
            assert "only in Neighbourhood" in page.locator("#atlas-hops-help").inner_text()

            page.locator("#atlas-status").select_option("all")
            focused_nodes = page.locator(".atlas-node").count()
            assert 10 < focused_nodes <= 60
            assert (
                "Operating focus admits at most 60" in page.locator("#topology-scope").inner_text()
            )
            page.locator("#atlas-expand").click()
            expanded_nodes = page.locator(".atlas-node").count()
            expanded_scope = page.locator("#topology-scope").inner_text()
            assert expanded_nodes >= focused_nodes
            assert page.locator("#atlas-expand").get_attribute("aria-pressed") == "true"
            assert "Expanded Operating view admits every" in expanded_scope
            assert "view focus withholds 0 nodes" in expanded_scope
            if expanded_nodes == focused_nodes:
                assert focused_nodes < 60
                assert f"This view admits {focused_nodes} of {focused_nodes}" in expanded_scope
            page.locator("#atlas-expand").click()
            assert page.locator(".atlas-node").count() == focused_nodes
            assert page.locator("#atlas-expand").get_attribute("aria-pressed") == "false"
            assert (
                "Operating focus admits at most 60"
                in page.locator("#topology-scope").inner_text()
            )

            selected = page.locator(".atlas-node").first
            selected.click()
            page.locator('[data-mode="neighbourhood"]').click()
            assert page.locator("#atlas-hops").is_enabled()
            page.locator("#atlas-hops").select_option("0")
            assert page.locator(".atlas-node").count() == 1
            assert "0-hop undirected neighbourhood" in page.locator("#topology-scope").inner_text()
            page.locator('[data-mode="operating"]').click()
            assert page.locator("#atlas-hops").is_disabled()

            for viewport, maximum_y in [
                ({"width": 1280, "height": 720}, 480),
                ({"width": 390, "height": 844}, 560),
            ]:
                page.set_viewport_size(viewport)
                page.wait_for_timeout(150)
                assert not page.locator("#atlas-prelude").evaluate("element => element.open")
                topology_y = page.locator("#topology-viewport").evaluate(
                    "element => element.getBoundingClientRect().y"
                )
                assert topology_y < maximum_y
                assert viewport["height"] - topology_y >= 240
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
                scope_box = page.locator("#topology-scope").bounding_box()
                assert scope_box is not None
                command_boxes = page.locator(".topology-command-surface button").evaluate_all(
                    "elements => elements.filter(element => element.checkVisibility())"
                    ".map(element => { const rect = element.getBoundingClientRect(); "
                    "return {left: rect.left, right: rect.right, top: rect.top, "
                    "bottom: rect.bottom}; })"
                )
                for command_box in command_boxes:
                    assert (
                        command_box["right"] <= scope_box["x"]
                        or command_box["left"] >= scope_box["x"] + scope_box["width"]
                        or command_box["bottom"] <= scope_box["y"]
                        or command_box["top"] >= scope_box["y"] + scope_box["height"]
                    )
                for selector in ["#atlas-expand", "#atlas-freeze", "#atlas-refresh"]:
                    control_box = page.locator(selector).bounding_box()
                    assert control_box is not None
                    assert control_box["x"] >= 0
                    assert control_box["x"] + control_box["width"] <= viewport["width"]

            page.locator("#atlas-prelude summary").click()
            metrics = page.locator("#atlas-metrics")
            metric_overflow = metrics.evaluate(
                "element => ({overflow: getComputedStyle(element).overflowX, "
                "scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})"
            )
            assert metric_overflow["overflow"] == "auto"
            assert metric_overflow["scrollWidth"] > metric_overflow["clientWidth"]
            assert page.locator(".metrics-hint").is_visible()
            metrics.evaluate("element => { element.scrollLeft = 500; }")
            assert metrics.evaluate("element => element.scrollLeft") > 0

            evidence_fonts = page.locator(
                ".activity-ledger small, .activity-ledger time, "
                ".activity-ledger .event-kind, .panel-stat, .truth-receipt, "
                ".atlas-node .node-meta, .atlas-metric small, "
                "#topology-scope, .hops-field small"
            ).evaluate_all(
                "elements => elements.map(element => "
                "parseFloat(getComputedStyle(element).fontSize))"
            )
            assert evidence_fonts
            assert min(evidence_fonts) >= 12
            assert page.locator(".activity-point").count() > 0
            assert page.locator(".activity-axis-label").count() == 2
            assert "recorded occurrences" in page.locator(".activity-point").first.get_attribute(
                "aria-label"
            )
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_future_event_integrity_degrades_live_badge_and_names_the_clock_error(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)

        def future_event_bundle(route) -> None:
            response = route.fetch()
            bundle = response.json()
            health = bundle["operations"]["health"]
            health["ok"] = False
            health["future_events"] = ["UI-101"]
            signal = next(row for row in health["signals"] if row["key"] == "future_events")
            signal["count"] = 2
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", future_event_bundle)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            assert "DEGRADED" in page.locator("#atlas-clock").inner_text()
            assert "after the document read clock" in page.locator("#atlas-alert").inner_text()
            integrity = page.locator(".atlas-metric").filter(has_text="Integrity")
            assert "CHECK" in integrity.inner_text()
            assert "2 events after read clock" in integrity.inner_text()
            future_signal = page.locator(".health-item").filter(has_text="future_events")
            assert "Recorded events occur after" in future_signal.inner_text()
            assert "2" in future_signal.inner_text()
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_low_information_receipt_replaces_zero_heavy_panels_without_inference(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser, width=1440, height=1050)

        def zero_relationship_bundle(route) -> None:
            response = route.fetch()
            bundle = response.json()
            operations = bundle["operations"]
            envelope = operations["graph_envelope"]
            relationship_count = envelope["population"]["edges"]
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
                reason="The authoritative graph publishes no relationships; topology motion and connectivity must not be inferred.",
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
                reason="The authoritative graph publishes no relationships; topology motion and connectivity must not be inferred.",
                missing_prerequisite="authoritative_graph_relationships",
            )
            availability["population"]["edges"] = 0
            availability["admitted"]["edges"] = 0
            availability["omitted"]["edges"] = 0
            operations["metrics"]["graph_edges"] = 0
            operations["metrics"]["graph_edges_emitted"] = 0
            bundle["graph"]["edges"] = []
            assert relationship_count > 0
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", zero_relationship_bundle)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            low_state = page.locator("#topology-low-information")
            low_state.wait_for(state="visible")

            assert (
                "publishes no relationships"
                in page.locator("#topology-low-information-reason").inner_text()
            )
            facts = page.locator("#topology-low-information-facts").inner_text().lower()
            assert "admission" in facts
            assert "freshness" in facts
            assert "source" in facts
            receipt = page.locator("#topology-technical-receipt")
            assert not receipt.evaluate("element => element.open")
            receipt.locator("summary").click()
            technical = page.locator("#topology-technical-facts").inner_text().lower()
            assert "population" in technical
            assert "0 edges" in technical
            assert "omitted" in technical
            assert "authoritative_graph_relationships" in technical
            assert page.locator(".topology-actions").is_hidden()
            assert page.locator("#atlas-questions").is_hidden()
            assert page.locator(".atlas-toolbar").is_visible()
            assert page.locator("#atlas-search").is_visible()
            for selector in (
                "#atlas-status",
                "#atlas-module",
                "#atlas-hops",
                "#atlas-zoom-out",
                "#atlas-zoom-in",
                "#atlas-zoom-fit",
            ):
                assert page.locator(selector).is_disabled()
            assert page.locator("body").evaluate(
                "element => element.classList.contains('atlas-low-information')"
            )
            assert page.locator("#topology-viewport").is_hidden()
            assert page.locator(".atlas-metrics").is_hidden()
            assert page.locator(".atlas-lower").is_hidden()
            for selector in (
                ".low-information-facts dd",
                ".low-information-reason",
            ):
                assert (
                    page.locator(selector).first.evaluate(
                        "element => parseFloat(getComputedStyle(element).fontSize)"
                    )
                    >= 14
                )
            for selector in (
                ".low-information-facts dt",
                ".technical-facts dt",
                ".technical-facts dd",
            ):
                assert (
                    page.locator(selector).first.evaluate(
                        "element => parseFloat(getComputedStyle(element).fontSize)"
                    )
                    >= 12
                )
            assert page.locator(".atlas-node").count() == 0
            assert page.locator(".atlas-edge").count() == 0
            assert page.locator(".atlas-motion-token").count() == 0
            assert page.locator("#topology-scope").inner_text() == ""
            assert page.locator("#topology-caption").inner_text() == ""

            screenshot_dir = tmp_path / "coord_atlas_low_info"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(screenshot_dir / "ops-atlas-low-information-1440.png"),
                full_page=True,
            )
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_freeze_invalidates_an_in_flight_refresh_until_unfrozen(tmp_path: Path) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)
        calls = 0
        mutated_generation = 0

        def changed_bundle(route) -> None:
            nonlocal calls, mutated_generation
            calls += 1
            response = route.fetch()
            bundle = response.json()
            if calls > 1:
                generation = bundle["cache_generation"] + calls
                mutated_generation = generation
                bundle["cache_generation"] = generation
                bundle["read_status"]["cache_generation"] = generation
                bundle["read_status"]["source_generated_at"] = "2099-01-01T00:00:00Z"
                row = next(
                    value for value in bundle["snapshot"]["rows"] if value["id"] == "PLT-302"
                )
                row["current_step"] = "held response must wait for unfreeze"
            route.fulfill(response=response, json=bundle)

        page.add_init_script(
            """
            (() => {
              const originalFetch = window.fetch.bind(window);
              let bundleCalls = 0;
              window.__atlasFreezeGate = { started: false, release: null };
              window.fetch = async (...args) => {
                const response = await originalFetch(...args);
                const url = String(args[0]);
                if (url.includes('/api/v1/operations-bundle') && ++bundleCalls === 2) {
                  window.__atlasFreezeGate.started = true;
                  document.documentElement.dataset.atlasFreezeGate = 'started';
                  await new Promise(resolve => { window.__atlasFreezeGate.release = resolve; });
                }
                return response;
              };
            })();
            """
        )
        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", changed_bundle)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            original_clock = page.locator("#atlas-clock").inner_text()

            page.locator("#atlas-refresh").click()
            page.locator("html[data-atlas-freeze-gate='started']").wait_for()
            held_generation = mutated_generation
            page.locator("#atlas-freeze").click()
            assert page.locator("#atlas-freeze").get_attribute("aria-pressed") == "true"
            frozen_clock = page.locator("#atlas-clock").inner_text()
            assert "FROZEN" in frozen_clock
            assert f"cache generation {held_generation}" not in frozen_clock

            page.evaluate("window.__atlasFreezeGate.release()")
            page.wait_for_timeout(250)
            assert page.locator("#atlas-clock").inner_text() == frozen_clock
            assert page.locator(".node-change-label").count() == 0

            page.locator("#atlas-freeze").click()
            page.locator(".node-change-label").first.wait_for(state="attached", timeout=2500)
            live_clock = page.locator("#atlas-clock").inner_text()
            assert f"cache generation {mutated_generation}" in live_clock
            assert live_clock != original_clock
            assert "current_step" in page.locator(".node-change-label").first.text_content()
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_new_occurrence_moves_a_receipt_along_a_recorded_hold(tmp_path: Path) -> None:
    with _board(tmp_path) as (server, db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            row = _first_owned_graph_row(server)
            actor = str(row["owner"]).split(":", 1)[0]
            _post_occurrence(
                db,
                row_id=row["id"],
                actor=actor,
                key="ops-atlas-live-motion-receipt",
            )
            _rebuild(server)

            page.locator("#atlas-refresh").click()
            token = page.locator(".atlas-motion-token").first
            token.wait_for(state="attached", timeout=2500)
            assert "New recorded note occurrence" in token.get_attribute("aria-label")
            assert "This refresh animated" in page.locator("#topology-caption").inner_text()
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_actor_mismatch_is_stationary_and_never_reassigned(tmp_path: Path) -> None:
    with _board(tmp_path) as (server, db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            row = _first_owned_graph_row(server)
            recorded_actor = str(row["owner"]).split(":", 1)[0]
            mismatched_actor = "reviewer" if recorded_actor != "reviewer" else "observer"
            _post_occurrence(
                db,
                row_id=row["id"],
                actor=mismatched_actor,
                key="ops-atlas-actor-mismatch-receipt",
            )
            _rebuild(server)

            page.locator("#atlas-refresh").click()
            token = page.locator(".atlas-stationary-token").first
            token.wait_for(state="attached", timeout=2500)
            label = token.get_attribute("aria-label")
            assert "stationary" in label
            assert "no owner attribution was inferred" in label
            assert page.locator(".atlas-motion-token").count() == 0
            assert "actor-mismatched" in page.locator("#topology-caption").inner_text()
        finally:
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_present_empty_envelope_never_falls_back_to_raw_graph(tmp_path: Path) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)

        def empty_envelope(route) -> None:
            response = route.fetch()
            bundle = response.json()
            envelope = bundle["operations"]["graph_envelope"]
            node_count = envelope["population"]["nodes"]
            edge_count = envelope["population"]["edges"]
            envelope["nodes"] = []
            envelope["edges"] = []
            envelope["emitted"]["nodes"] = 0
            envelope["emitted"]["edges"] = 0
            envelope["omitted"]["nodes"] = node_count
            envelope["omitted"]["edges"] = edge_count
            envelope["omitted"]["node_reasons"] = [{"reason": "node_cap", "count": node_count}]
            envelope["omitted"]["edge_reasons"] = [{"reason": "edge_cap", "count": edge_count}]
            envelope["complete"] = False
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", empty_envelope)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            assert page.locator('[data-node-id^="work:"]').count() == 0
            assert page.locator('[data-node-id^="job:"]').count() == 0
            assert page.locator('[data-node-id^="agent:"]').count() > 0
            assert "emitted 0 of" in page.locator("#topology-scope").inner_text()
            assert (
                "Node omission reasons: node_cap" in page.locator("#topology-receipts").inner_text()
            )
            assert page.locator("#health-state").inner_text() == "PARTIAL"
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_changed_tuple_names_fields_and_never_calls_pulse_progress(tmp_path: Path) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)
        calls = 0

        def change_step(route) -> None:
            nonlocal calls
            calls += 1
            response = route.fetch()
            bundle = response.json()
            if calls > 1:
                row = next(
                    value for value in bundle["snapshot"]["rows"] if value["id"] == "PLT-302"
                )
                row["current_step"] = "verifying generation receipts"
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", change_step)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            page.locator("#atlas-refresh").click()
            label = page.locator(".node-change-label").first
            label.wait_for(state="attached", timeout=2500)
            assert "current_step" in label.text_content()
            assert "not progress" in label.text_content()
            node_label = page.locator('[data-node-id="work:PLT-302"]').get_attribute("aria-label")
            assert "Changed fields on this refresh: current_step" in node_label
            assert "not progress" in page.locator("#topology-caption").inner_text()
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_cycle_limited_metrics_are_component_scoped_and_visibly_withheld(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)

        def cycle_limited_bundle(route) -> None:
            response = route.fetch()
            bundle = response.json()
            execution = bundle["operations"]["execution"]
            execution["topology_metrics_status"] = "withheld_cycle"
            execution["cycle_tainted"] = ["PLT-301", "PLT-302"]
            execution["cycle_tainted_total"] = 2
            execution["cycle_tainted_emitted"] = 2
            execution["cycle_tainted_truncated"] = False
            execution["cycle_impact"] = [
                {
                    "members": ["PLT-301", "PLT-302"],
                    "members_total": 2,
                    "members_truncated": False,
                    "downstream_after_component": 4,
                }
            ]
            execution["cycle_components_total"] = 1
            execution["cycle_components_emitted"] = 1
            execution["cycle_impact_truncated"] = False
            execution["cycle_member_ids_total"] = 2
            execution["cycle_member_ids_emitted"] = 2
            execution["cycle_member_ids_truncated"] = False
            execution["analysis_boundary_dependencies_total"] = 0
            execution["analysis_boundary_tainted_total"] = 0
            execution["missing_dependencies_total"] = 0
            execution["missing_dependency_tainted_total"] = 0
            execution["unresolved_tainted_total"] = 0
            execution["critical_path"] = []
            execution["layers"] = []
            execution["impact"] = []
            bundle["operations"]["metrics"]["critical_path_steps"] = 0
            bundle["operations"]["metrics"]["max_parallel_width"] = 0
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", cycle_limited_bundle)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            assert page.locator("#impact-title").inner_text() == "Acyclic downstream reach"
            assert "2 cycle-withheld rows" in page.locator("#path-length").inner_text().lower()
            assert (
                "cycle-tainted rows are withheld"
                in page.locator("#critical-strip").inner_text().lower()
            )
            receipt = page.locator(".cycle-impact-receipt")
            receipt_text = receipt.inner_text().lower()
            assert "plt-301 + plt-302" in receipt_text
            assert "4 downstream rows in recorded component reach" in receipt_text
            assert "does not prove an immediate unlock" in receipt_text
            assert (
                "cycle-tainted rows withheld"
                in page.locator(".atlas-metric").nth(5).inner_text().lower()
            )
            assert page.locator("#impact-ledger [data-select-row='PLT-301']").count() == 0
            assert page.locator("#impact-ledger [data-select-row='PLT-302']").count() == 0
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_population_bounded_metrics_state_exact_scope_without_cycle_language(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)

        def bounded_bundle(route) -> None:
            response = route.fetch()
            bundle = response.json()
            execution = bundle["operations"]["execution"]
            execution.update(
                topology_metrics_status="partial_population",
                analysis_population_total=1_200,
                analysis_population_emitted=400,
                analysis_population_omitted=800,
                analysis_population_truncated=True,
                analysis_boundary_dependencies_total=0,
                analysis_boundary_tainted_total=0,
                missing_dependencies_total=0,
                missing_dependency_tainted_total=0,
                unresolved_tainted_total=0,
                cycle_tainted=[],
                cycle_tainted_total=0,
                cycle_tainted_emitted=0,
            )
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", bounded_bundle)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            assert page.locator("#impact-title").inner_text() == "Bounded downstream reach"
            assert "800 outside rows" in page.locator("#path-length").inner_text().lower()
            strip = page.locator("#critical-strip").inner_text().lower()
            assert "emitted 400 of 1,200 work rows" in strip
            assert "cycle" not in strip
            metric = page.locator(".atlas-metric").nth(5).inner_text().lower()
            assert "400 of 1,200 rows analyzed" in metric
            assert "cycle" not in metric
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_unresolved_metrics_are_distinct_from_population_truncation(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, _db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page, console_errors, failed_requests = _browser_page(browser)

        def unresolved_bundle(route) -> None:
            response = route.fetch()
            bundle = response.json()
            execution = bundle["operations"]["execution"]
            execution.update(
                topology_metrics_status="partial_unresolved",
                analysis_population_total=7,
                analysis_population_emitted=7,
                analysis_population_omitted=0,
                analysis_population_truncated=False,
                analysis_boundary_dependencies_total=0,
                analysis_boundary_tainted_total=0,
                missing_dependencies_total=2,
                missing_dependencies_emitted=1,
                missing_dependencies_truncated=True,
                missing_dependency_tainted_total=3,
                unresolved_tainted_total=3,
                unresolved_tainted_emitted=3,
                unresolved_tainted_truncated=False,
                cycle_tainted=[],
                cycle_tainted_total=0,
                cycle_tainted_emitted=0,
            )
            route.fulfill(response=response, json=bundle)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", unresolved_bundle)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()

            assert page.locator("#impact-title").inner_text() == "Dependency-safe downstream reach"
            path_text = page.locator("#path-length").inner_text().lower()
            assert "3 unresolved-withheld rows" in path_text
            assert "outside row" not in path_text
            strip = page.locator("#critical-strip").inner_text().lower()
            assert "3 unique rows are withheld" in strip
            assert "emitted 1 of 2 missing prerequisite edge identities" in strip
            metric = page.locator(".atlas-metric").nth(5).inner_text().lower()
            assert "0 boundary + 2 missing edges; 3 unique rows withheld" in metric
            assert "of 7 rows analyzed" not in metric
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests


def test_reduced_motion_keeps_receipt_text_without_moving_token(tmp_path: Path) -> None:
    with _board(tmp_path) as (server, db, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            reduced_motion="reduce",
            viewport={"width": 1440, "height": 1050},
        )
        page, console_errors, failed_requests = _browser_page(context)
        try:
            page.goto(f"{url}/ops", wait_until="networkidle")
            page.locator(".atlas-node").first.wait_for()
            row = _first_owned_graph_row(server)
            actor = str(row["owner"]).split(":", 1)[0]
            _post_occurrence(
                db,
                row_id=row["id"],
                actor=actor,
                key="ops-atlas-reduced-motion-receipt",
            )
            _rebuild(server)

            page.locator("#atlas-refresh").click()
            page.wait_for_timeout(250)
            assert page.locator(".atlas-motion-token").count() == 0
            caption = page.locator("#topology-caption").inner_text()
            assert "motion is suppressed" in caption
            assert "no progress is implied" in caption
        finally:
            context.close()
            browser.close()

        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
