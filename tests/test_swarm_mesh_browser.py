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
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _page(context):
    page = context.new_page()
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


def _wait_for_mesh(page) -> None:
    page.locator("#mesh-canvas").wait_for(state="visible", timeout=5_000)
    # The roster is intentionally collapsed at the phone breakpoint, but it
    # must still be populated for scope accounting and desktop restoration.
    page.locator(".cluster-row").first.wait_for(state="attached", timeout=5_000)
    page.wait_for_function(
        "document.querySelector('#rail-nodes').textContent.trim() !== '—'",
        timeout=5_000,
    )


def _assert_clean(console_errors: list[str], failed_requests: list[str]) -> None:
    assert not console_errors, console_errors
    assert not failed_requests, failed_requests


def _cluster_label_geometry(page) -> dict:
    return page.evaluate(
        """
        () => {
          const viewport = document.querySelector("#mesh-viewport").getBoundingClientRect();
          const visible = [...document.querySelectorAll(".cluster-label:not([hidden])")]
            .map(element => {
              const box = element.getBoundingClientRect();
              return {
                id: element.dataset.clusterId,
                text: element.textContent,
                leader: element.dataset.leader === "true",
                left: box.left - viewport.left,
                top: box.top - viewport.top,
                right: box.right - viewport.left,
                bottom: box.bottom - viewport.top,
              };
            });
          const overlaps = [];
          for (let left = 0; left < visible.length; left += 1) {
            for (let right = left + 1; right < visible.length; right += 1) {
              const a = visible[left];
              const b = visible[right];
              if (a.left < b.right && a.right > b.left
                  && a.top < b.bottom && a.bottom > b.top) {
                overlaps.push([a.id, b.id]);
              }
            }
          }
          const overlayBoxes = [".canvas-tools", ".axis-key"]
            .map(selector => document.querySelector(selector))
            .filter(Boolean)
            .map(element => ({selector: element.className, box: element.getBoundingClientRect()}))
            .filter(value => value.box.width > 0 && value.box.height > 0);
          const obstructed = visible.flatMap(label => overlayBoxes
            .filter(value => {
              const box = value.box;
              const left = label.left + viewport.left;
              const right = label.right + viewport.left;
              const top = label.top + viewport.top;
              const bottom = label.bottom + viewport.top;
              return left < box.right && right > box.left && top < box.bottom && bottom > box.top;
            })
            .map(value => [label.id, value.selector]));
          const outside = visible
            .filter(label => label.left < -1 || label.top < -1
              || label.right > viewport.width + 1 || label.bottom > viewport.height + 1)
            .map(label => label.id);
          const receipt = document.querySelector("#label-receipt");
          return {
            total: document.querySelectorAll(".cluster-label").length,
            visible,
            overlaps,
            obstructed,
            outside,
            receipt: receipt.textContent,
            telemetry: {...receipt.dataset},
          };
        }
        """
    )


def _assert_readable_mesh_authority(page) -> None:
    core = page.locator(".mesh-caption-core")
    assert "layout-only; direction is not activity" in core.inner_text().lower()
    caption_style = page.locator("#mesh-caption").evaluate(
        "element => ({overflow: getComputedStyle(element).overflow, "
        "whiteSpace: getComputedStyle(element).whiteSpace})"
    )
    assert caption_style["overflow"] != "hidden"
    assert caption_style["whiteSpace"] != "nowrap"

    disclosure = page.locator(".mesh-caption-disclosure")
    assert disclosure.evaluate("element => element.tagName") == "DETAILS"
    assert disclosure.locator("summary").evaluate("element => element.tagName") == "SUMMARY"
    caveats = disclosure.locator(".mesh-caption-detail").text_content().lower()
    for instruction in (
        "arrow keys move node focus",
        "enter pins the focused node",
        "escape clears the pinned node",
    ):
        assert instruction in caveats
    disclosure.locator("summary").click()
    assert disclosure.get_attribute("open") is not None
    assert disclosure.locator(".mesh-caption-detail").is_visible()
    disclosure.locator("summary").click()

    truth = page.locator(".mesh-truth p")
    assert "layout-only; direction is not activity" in truth.text_content().lower()
    truth_style = truth.evaluate(
        "element => ({overflow: getComputedStyle(element).overflow, "
        "whiteSpace: getComputedStyle(element).whiteSpace})"
    )
    assert truth_style["overflow"] != "hidden"
    assert truth_style["whiteSpace"] != "nowrap"

    sizes = page.evaluate(
        """
        () => {
          const font = selector => parseFloat(
            getComputedStyle(document.querySelector(selector)).fontSize
          );
          const stationary = document.querySelector(".event-static");
          return {
            event: font(".event-ledger li"),
            stationary: stationary
              ? parseFloat(getComputedStyle(stationary, "::after").fontSize)
              : 12,
            rail: font(".rail-metrics dt"),
            badge: font(".truth-badge"),
            caption: font("#mesh-caption"),
            cluster: font(".cluster-label"),
            control: font(".canvas-tools button"),
          };
        }
        """
    )
    assert all(sizes[key] >= 12 for key in ("event", "stationary", "rail", "badge", "caption"))
    assert sizes["cluster"] >= 13
    assert sizes["control"] >= 14


@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 720), (390, 844)],
)
def test_short_desktop_and_phone_keep_primary_mesh_truth_and_keyboard_reachable(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": width, "height": height},
            reduced_motion="reduce",
        )
        page, console_errors, failed_requests = _page(context)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)

            assert page.evaluate("getComputedStyle(document.body).overflowY !== 'hidden'")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            assert page.evaluate("document.scrollingElement.scrollHeight > window.innerHeight")

            viewport = page.locator("#mesh-viewport")
            viewport.evaluate(
                "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
            )
            viewport_box = viewport.bounding_box()
            assert viewport_box is not None
            assert viewport_box["height"] >= (400 if width <= 520 else 250)
            assert viewport_box["y"] < height
            assert viewport_box["y"] + viewport_box["height"] > 0

            tool_boxes = page.locator(".canvas-tools button:visible").evaluate_all(
                """
                elements => elements.map(element => {
                  const box = element.getBoundingClientRect();
                  return {left: box.left, right: box.right, width: box.width};
                })
                """
            )
            assert tool_boxes
            assert all(box["width"] > 0 for box in tool_boxes)
            assert all(box["left"] >= -1 and box["right"] <= width + 1 for box in tool_boxes)
            page.locator("#camera-fit").click()
            assert page.locator("#camera-zoom").inner_text().endswith("%")

            assert viewport.get_attribute("role") == "application"
            shortcuts = viewport.get_attribute("aria-keyshortcuts") or ""
            for shortcut in ("ArrowRight", "Enter", "Escape", "Shift+ArrowRight"):
                assert shortcut in shortcuts
            _assert_readable_mesh_authority(page)

            active_before = viewport.get_attribute("aria-activedescendant")
            assert active_before
            before_option = page.locator(f'[id="{active_before}"]')
            assert before_option.get_attribute("role") == "button"
            assert before_option.evaluate("element => element.tabIndex") == -1
            before_text = before_option.inner_text()
            viewport.focus()
            active_after = active_before
            for key in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"):
                viewport.press(key)
                active_after = viewport.get_attribute("aria-activedescendant")
                if active_after != active_before:
                    break
            assert active_after and active_after != active_before
            active_option = page.locator(f'[id="{active_after}"]')
            assert active_option.get_attribute("role") == "button"
            assert active_option.evaluate("element => element.tabIndex") == -1
            assert active_option.inner_text() != before_text
            playwright_api.expect(page.locator("#mesh-status-announcer")).to_contain_text(
                "Focused", timeout=2_000
            )
            viewport.press("Enter")
            playwright_api.expect(page.locator("#mesh-status-announcer")).to_contain_text(
                "Selected", timeout=2_000
            )
            assert active_option.get_attribute("aria-current") == "true"
            assert page.locator("#selection-inspector h3").count() == 1

            evidence = page.locator("#mesh-evidence")
            assert evidence.get_attribute("open") is None
            assert page.locator(".telemetry-panel:visible").count() == 0
            page.locator("#mesh-evidence > summary").click()
            visible_telemetry = page.locator(".telemetry-panel:visible")
            assert visible_telemetry.count() >= 4
            visible_telemetry.last.evaluate(
                "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
            )
            telemetry_box = visible_telemetry.last.bounding_box()
            assert telemetry_box is not None
            assert telemetry_box["y"] < height
            assert telemetry_box["y"] + telemetry_box["height"] > 0

            truth = page.locator(".mesh-truth")
            truth.evaluate(
                "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
            )
            truth_box = truth.bounding_box()
            assert truth_box is not None
            assert truth_box["y"] < height
            assert truth_box["y"] + truth_box["height"] > 0
            assert "Projection only" in truth.inner_text()
            page.locator("#mesh-evidence > summary").click()
            assert page.locator(".telemetry-panel:visible").count() == 0
        finally:
            context.close()
            browser.close()
        _assert_clean(console_errors, failed_requests)


@pytest.mark.parametrize(
    ("width", "height"),
    [(1600, 1000), (390, 844)],
)
def test_cluster_labels_are_complete_collision_free_and_stable_during_motion(
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": height})
        page, console_errors, failed_requests = _page(context)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)
            assert page.locator('[data-motion="direction"]').get_attribute("aria-pressed") == "true"
            playwright_api.expect(page.locator("#mesh-canvas")).to_have_attribute(
                "data-motion-source", "operations.graph_envelope.edges"
            )

            signatures = []
            for _sample in range(3):
                page.wait_for_timeout(120)
                geometry = _cluster_label_geometry(page)
                assert geometry["total"] >= 3
                assert 0 < len(geometry["visible"]) <= geometry["total"] <= 8
                assert geometry["overlaps"] == []
                assert geometry["obstructed"] == []
                assert geometry["outside"] == []
                assert geometry["telemetry"]["overlapCount"] == "0"
                assert int(geometry["telemetry"]["rosterCount"]) == (
                    geometry["total"] - len(geometry["visible"])
                )
                assert geometry["telemetry"]["visibleCount"] == str(len(geometry["visible"]))
                assert int(geometry["telemetry"]["leaderCount"]) >= 1
                assert "LABELS SHOWN" in geometry["receipt"]
                signatures.append(
                    [
                        (
                            label["id"],
                            round(label["left"], 2),
                            round(label["top"], 2),
                            label["leader"],
                        )
                        for label in geometry["visible"]
                    ]
                )
            assert signatures[1:] == signatures[:-1]
        finally:
            context.close()
            browser.close()
        _assert_clean(console_errors, failed_requests)


def _mutate_with_arrival(route, *, actor_match: bool, rolling: bool = False) -> None:
    response = route.fetch()
    bundle = response.json()
    envelope = bundle["operations"].get("graph_envelope") or bundle["graph"]
    graph_ids = {str(value.get("id", "")) for value in envelope.get("nodes", [])}
    row = next(
        value
        for value in bundle["snapshot"]["rows"]
        if value.get("owner")
        and not str(value["id"]).startswith("job:")
        and (str(value["id"]) in graph_ids or f"work:{value['id']}" in graph_ids)
    )
    recorded_actor = str(row["owner"]).split(":", 1)[0]
    actor = (
        recorded_actor
        if actor_match
        else ("reviewer" if recorded_actor != "reviewer" else "observer")
    )
    activity = bundle["operations"]["activity"]
    if rolling and activity:
        activity.pop(0)
        activity.reverse()
    activity.append(
        {
            "at": "2099-01-01T00:00:00Z",
            "id": row["id"],
            "kind": "note",
            "actor": actor,
        }
    )
    route.fulfill(response=response, json=bundle)


def _mutate_with_reordered_history(route) -> None:
    response = route.fetch()
    bundle = response.json()
    bundle["operations"]["activity"].reverse()
    route.fulfill(response=response, json=bundle)


def test_mesh_layout_camera_search_selection_and_cluster_receipts(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page, console_errors, failed_requests = _page(context)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)

            canvas_box = page.locator("#mesh-viewport").bounding_box()
            assert canvas_box is not None
            assert canvas_box["y"] < 310
            assert canvas_box["width"] >= 850
            assert canvas_box["height"] >= 420
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            assert page.evaluate("getComputedStyle(document.body).overflowY === 'hidden'")

            coverage = page.locator("#coverage-summary")
            page.wait_for_function(
                "Number(document.querySelector('#coverage-summary').dataset.shown) > 0"
            )
            focus_shown = int(coverage.get_attribute("data-shown") or "0")
            admitted = int(coverage.get_attribute("data-admitted") or "0")
            whole = int(coverage.get_attribute("data-whole") or "0")
            assert coverage.get_attribute("data-mode") == "focus"
            assert 0 < focus_shown <= 60
            assert focus_shown <= admitted <= whole
            for term in ("shown", "admitted", "whole graph", "omitted by envelope"):
                assert term in coverage.inner_text().lower()
            page.wait_for_function(
                "document.querySelector('#mesh-canvas').dataset.totalGraphLabelCount"
            )
            assert (
                int(
                    page.locator("#mesh-canvas").get_attribute("data-total-graph-label-count")
                    or "0"
                )
                <= 12
            )
            assert page.locator("#selection-inspector .selection-empty").count() == 0
            assert page.locator("#selection-inspector h3").count() == 1
            assert "BOARD PULSE" in page.locator("#selection-inspector").inner_text()

            expand = page.locator('[data-population="admitted"]')
            expand.click()
            assert expand.get_attribute("aria-pressed") == "true"
            assert coverage.get_attribute("data-mode") == "admitted"
            assert int(coverage.get_attribute("data-shown") or "0") == admitted
            page.locator('[data-population="focus"]').click()
            assert coverage.get_attribute("data-mode") == "focus"
            assert int(coverage.get_attribute("data-shown") or "0") <= 60

            labels = page.locator(".cluster-label:not([hidden])")
            assert labels.count() >= 3
            assert all(value.strip() for value in labels.all_inner_texts())
            label_box = labels.first.bounding_box()
            assert label_box is not None
            assert label_box["width"] >= 40
            assert label_box["height"] >= 14
            assert (
                float(
                    labels.first.evaluate(
                        "element => parseFloat(getComputedStyle(element).fontSize)"
                    )
                )
                >= 13
            )
            assert "nodes in" in page.locator("#mesh-a11y").inner_text()

            # Use still live mode so canvas differences below come only from
            # camera movement, never schematic direction particles.
            page.locator('[data-motion="live"]').click()
            assert "FIRST READ SILENT" in page.locator("#motion-truth").inner_text()
            still_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(220)
            still_b = page.locator("#mesh-canvas").screenshot()
            assert still_a == still_b, "initial live mode rendered unrecorded motion"

            page.locator("#camera-plus").click()
            assert page.locator("#camera-zoom").inner_text() != "100%"
            zoomed = page.locator("#mesh-canvas").screenshot()
            assert zoomed != still_a
            page.locator("#camera-fit").click()
            fit_zoom = int(page.locator("#camera-zoom").inner_text().rstrip("%"))
            assert 25 <= fit_zoom <= 250
            assert page.locator("#mesh-canvas").screenshot() != zoomed

            viewport = page.locator("#mesh-viewport")
            box = viewport.bounding_box()
            assert box is not None
            viewport.dispatch_event(
                "pointerdown",
                {
                    "pointerId": 7,
                    "clientX": box["x"] + box["width"] * 0.45,
                    "clientY": box["y"] + box["height"] * 0.45,
                    "button": 0,
                    "shiftKey": True,
                },
            )
            viewport.dispatch_event(
                "pointermove",
                {
                    "pointerId": 7,
                    "clientX": box["x"] + box["width"] * 0.55,
                    "clientY": box["y"] + box["height"] * 0.52,
                    "button": 0,
                    "shiftKey": True,
                },
            )
            viewport.dispatch_event(
                "pointerup",
                {
                    "pointerId": 7,
                    "clientX": box["x"] + box["width"] * 0.55,
                    "clientY": box["y"] + box["height"] * 0.52,
                    "button": 0,
                    "shiftKey": True,
                },
            )
            panned = page.locator("#mesh-canvas").screenshot()
            assert panned != still_a
            page.locator("#camera-reset").click()

            exact_row = next(
                value
                for value in server.snapshot()["rows"]
                if value.get("title") and not str(value["id"]).startswith("job:")
            )
            page.locator("#mesh-search").fill(str(exact_row["id"]))
            page.locator("#mesh-search").press("Enter")
            inspector = page.locator("#selection-inspector")
            assert str(exact_row["id"]) in inspector.inner_text()
            assert not page.locator("#clear-selection").is_hidden()

            page.locator("#clear-selection").click()
            viewport.focus()
            viewport.press("ArrowRight")
            viewport.press("Enter")
            assert page.locator("#selection-inspector h3").count() == 1

            initial_context = page.locator("#cluster-roster").inner_text()
            page.locator('[data-layout="swarm"]').click()
            assert page.locator('[data-layout="swarm"]').get_attribute("aria-pressed") == "true"
            swarm_roster = page.locator("#cluster-roster").inner_text()
            assert swarm_roster != initial_context
            page.locator('[data-layout="critical"]').click()
            assert page.locator('[data-layout="critical"]').get_attribute("aria-pressed") == "true"
            assert "Dependency layer" in page.locator("#cluster-roster").inner_text()
            page.locator('[data-layout="context"]').click()
            assert page.locator("#cluster-roster").inner_text() == initial_context

            baseline_hidden = page.locator("#hidden-receipt").inner_text()
            cluster = page.locator(".cluster-row").first
            cluster.click()
            hidden = page.locator("#hidden-receipt").inner_text()
            assert "NODES ·" in hidden
            assert "EDGES HIDDEN" in hidden
            assert hidden.endswith(" HIDDEN")
            assert hidden != "0 NODES · 0 EDGES HIDDEN"
            assert "Visible edges" in page.locator("#scope-ledger").inner_text()
            cluster.click()
            assert page.locator("#hidden-receipt").inner_text() == baseline_hidden
        finally:
            context.close()
            browser.close()
        _assert_clean(console_errors, failed_requests)


@pytest.mark.parametrize("width", [1440, 1024])
def test_populated_perspective_orbits_and_particles_follow_admitted_edges(
    tmp_path: Path,
    width: int,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": 900})
        page, console_errors, failed_requests = _page(context)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)

            perspective = page.locator('[data-projection="perspective"]')
            flat = page.locator('[data-projection="flat"]')
            assert perspective.is_enabled()
            assert perspective.get_attribute("aria-pressed") == "true"
            assert "LAYOUT-ONLY Z" in page.locator("#projection-truth").inner_text()
            caption = page.locator("#mesh-caption").text_content().lower()
            assert "not measured time, priority, certainty, or activity" in caption
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            assert int(page.locator("#rail-edges").inner_text()) > 0

            page.locator('[data-motion="direction"]').evaluate("element => element.click()")
            canvas = page.locator("#mesh-canvas")
            playwright_api.expect(canvas).to_have_attribute(
                "data-motion-source", "operations.graph_envelope.edges"
            )
            assert int(canvas.get_attribute("data-motion-edge-count") or "0") > 0
            moving_a = canvas.screenshot()
            page.wait_for_timeout(240)
            moving_b = canvas.screenshot()
            assert moving_a != moving_b, "no particle visibly travelled an admitted edge"

            # Live is still until a new exact admitted ownership event arrives,
            # making the following image differences camera-only receipts.
            page.locator('[data-motion="live"]').evaluate("element => element.click()")
            viewport = page.locator("#mesh-viewport")
            box = viewport.bounding_box()
            assert box is not None
            before_orbit = page.locator("#mesh-canvas").screenshot()
            viewport.dispatch_event(
                "pointerdown",
                {
                    "pointerId": 17,
                    "clientX": box["x"] + box["width"] * 0.42,
                    "clientY": box["y"] + box["height"] * 0.44,
                    "button": 0,
                },
            )
            viewport.dispatch_event(
                "pointermove",
                {
                    "pointerId": 17,
                    "clientX": box["x"] + box["width"] * 0.58,
                    "clientY": box["y"] + box["height"] * 0.53,
                    "button": 0,
                },
            )
            viewport.dispatch_event(
                "pointerup",
                {
                    "pointerId": 17,
                    "clientX": box["x"] + box["width"] * 0.58,
                    "clientY": box["y"] + box["height"] * 0.53,
                    "button": 0,
                },
            )
            orbit = page.locator("#mesh-canvas").screenshot()
            assert orbit != before_orbit

            viewport.focus()
            viewport.press("Shift+ArrowRight")
            keyboard_orbit = page.locator("#mesh-canvas").screenshot()
            assert keyboard_orbit != orbit

            flat.click()
            assert flat.get_attribute("aria-pressed") == "true"
            assert "2D" in page.locator("#projection-truth").inner_text()
            assert "flattens" in page.locator("#mesh-caption").text_content().lower()
            flattened = page.locator("#mesh-canvas").screenshot()
            assert flattened != keyboard_orbit

            perspective.click()
            page.locator("#camera-fit").click()
            assert perspective.get_attribute("aria-pressed") == "true"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            for selector in ('[data-projection="perspective"]', "#camera-fit"):
                assert (
                    page.locator(selector).evaluate(
                        "element => parseFloat(getComputedStyle(element).fontSize)"
                    )
                    >= 14
                )
        finally:
            context.close()
            browser.close()
        _assert_clean(console_errors, failed_requests)


def test_motion_modes_separate_direction_replay_and_new_live_arrivals(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page, console_errors, failed_requests = _page(context)
        bundle_calls = 0

        def operations_bundle(route) -> None:
            nonlocal bundle_calls
            bundle_calls += 1
            if bundle_calls == 1:
                route.continue_()
            else:
                _mutate_with_arrival(route, actor_match=True)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", operations_bundle)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)

            page.locator('[data-motion="direction"]').click()
            direction_truth = page.locator("#motion-truth").inner_text()
            assert direction_truth == "SCHEMATIC DIRECTION · NOT ACTIVITY"
            direction_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(240)
            direction_b = page.locator("#mesh-canvas").screenshot()
            assert direction_a != direction_b, "direction particles were not visibly moving"

            page.locator('[data-motion="replay"]').click()
            assert "RECORDED REPLAY" in page.locator("#motion-truth").inner_text()
            assert "STATIONARY HISTORY" in page.locator("#motion-truth").inner_text()
            assert (
                "position is order, not duration"
                in page.locator("#traversal-note").text_content().lower()
            )

            page.locator('[data-motion="live"]').click()
            assert "FIRST READ SILENT" in page.locator("#motion-truth").inner_text()
            initial_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(180)
            initial_b = page.locator("#mesh-canvas").screenshot()
            assert initial_a == initial_b

            page.locator("#mesh-refresh").click()
            playwright_api.expect(page.locator("#motion-truth")).to_contain_text(
                "NEW", timeout=5_000
            )
            assert "1 NEW" in page.locator("#motion-truth").inner_text()
            arrival_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(260)
            arrival_b = page.locator("#mesh-canvas").screenshot()
            assert arrival_a != arrival_b, "exact actor-matched arrival did not visibly travel"
        finally:
            context.close()
            browser.close()
        console_errors[:] = [value for value in console_errors if "404" not in value]
        _assert_clean(console_errors, failed_requests)


def test_reorder_and_rolling_window_do_not_reanimate_retained_history(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page, console_errors, failed_requests = _page(context)
        bundle_calls = 0

        def operations_bundle(route) -> None:
            nonlocal bundle_calls
            bundle_calls += 1
            if bundle_calls == 1:
                route.continue_()
            elif bundle_calls == 2:
                _mutate_with_reordered_history(route)
            else:
                _mutate_with_arrival(route, actor_match=True, rolling=True)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", operations_bundle)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)
            page.locator('[data-motion="live"]').click()
            assert "FIRST READ SILENT" in page.locator("#motion-truth").inner_text()

            page.locator("#mesh-refresh").click()
            page.wait_for_timeout(260)
            assert "FIRST READ SILENT" in page.locator("#motion-truth").inner_text()
            reorder_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(220)
            reorder_b = page.locator("#mesh-canvas").screenshot()
            assert reorder_a == reorder_b, "reordered history was misclassified as live"

            page.locator("#mesh-refresh").click()
            playwright_api.expect(page.locator("#motion-truth")).to_contain_text(
                "NEW", timeout=5_000
            )
            assert "1 NEW" in page.locator("#motion-truth").inner_text()
            rolling_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(260)
            rolling_b = page.locator("#mesh-canvas").screenshot()
            assert rolling_a != rolling_b, "the one new rolling-window arrival did not travel"
        finally:
            context.close()
            browser.close()
        console_errors[:] = [value for value in console_errors if "404" not in value]
        _assert_clean(console_errors, failed_requests)


def test_actor_mismatch_is_stationary_history_and_never_live_motion(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page, console_errors, failed_requests = _page(context)
        bundle_calls = 0

        def operations_bundle(route) -> None:
            nonlocal bundle_calls
            bundle_calls += 1
            if bundle_calls == 1:
                route.continue_()
            else:
                _mutate_with_arrival(route, actor_match=False)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", operations_bundle)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)
            page.locator('[data-motion="live"]').click()
            page.locator("#mesh-refresh").click()
            playwright_api.expect(page.locator("#motion-truth")).to_contain_text(
                "NEW", timeout=5_000
            )
            assert "1 NEW" in page.locator("#motion-truth").inner_text()
            assert page.locator("#event-ledger .event-static").count() >= 1
            stationary_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(240)
            stationary_b = page.locator("#mesh-canvas").screenshot()
            assert stationary_a == stationary_b, "actor mismatch was reassigned to a live track"
        finally:
            context.close()
            browser.close()
        console_errors[:] = [value for value in console_errors if "404" not in value]
        _assert_clean(console_errors, failed_requests)


def test_reduced_motion_is_still_and_mobile_layout_has_no_page_overflow(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (_server, url), playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            reduced_motion="reduce",
        )
        page, console_errors, failed_requests = _page(context)
        try:
            page.goto(f"{url}/mesh", wait_until="networkidle")
            _wait_for_mesh(page)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            viewport_box = page.locator("#mesh-viewport").bounding_box()
            assert viewport_box is not None
            assert viewport_box["width"] >= 350
            assert viewport_box["height"] >= 400
            assert page.locator(".mesh-fleet").is_hidden()

            # Phone chrome collapses the segmented motion control. Invoke the
            # same button event without requiring the hidden control to be
            # pointer-visible; the canvas remains the observable contract.
            page.locator('[data-motion="direction"]').evaluate("element => element.click()")
            assert "NOT ACTIVITY" in page.locator("#motion-truth").inner_text()
            still_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(280)
            still_b = page.locator("#mesh-canvas").screenshot()
            assert still_a == still_b

            page.locator('[data-motion="replay"]').evaluate("element => element.click()")
            assert "STATIONARY HISTORY" in page.locator("#motion-truth").inner_text()
            replay_a = page.locator("#mesh-canvas").screenshot()
            page.wait_for_timeout(1150)
            replay_b = page.locator("#mesh-canvas").screenshot()
            assert replay_a == replay_b
            playwright_api.expect(page.locator("#rail-fps")).to_have_text("STILL", timeout=5_000)
        finally:
            context.close()
            browser.close()
        _assert_clean(console_errors, failed_requests)
