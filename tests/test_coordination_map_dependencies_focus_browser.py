from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from urllib.parse import urlparse

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


def _large_graph_bundle(route, call: int) -> None:
    response = route.fetch()
    bundle = response.json()
    nodes = [
        {
            "id": f"work:N{index:03d}",
            "label": "job_progress",
            "kind": "work",
            "status": "planned",
        }
        for index in range(400)
    ]
    edges = []
    for index in range(1, 81):
        edges.append(
            {
                "id": f"hub-{index:03d}",
                "source": "work:N000",
                "target": f"work:N{index:03d}",
                "kind": "depends_on",
                "relationship_state": "source_bound",
            }
        )
    for index in range(1, 182):
        edges.append(
            {
                "id": f"chain-{index:03d}",
                "source": f"work:N{index:03d}",
                "target": f"work:N{index + 1:03d}",
                "kind": "parent",
                "relationship_state": "source_bound",
            }
        )
    edges.append(
        {
            "id": "island-200-201",
            "source": "work:N200",
            "target": "work:N201",
            "kind": "depends_on",
            "relationship_state": "source_bound",
        }
    )
    assert len(nodes) == 400
    assert len(edges) == 262
    if call % 2 == 0:
        nodes.reverse()
        edges.reverse()

    bundle["graph"]["nodes"] = nodes
    bundle["graph"]["edges"] = edges
    bundle["bundle_projection"]["graph_nodes"] = {"published": 400, "omitted": 0}
    bundle["bundle_projection"]["graph_edges"] = {"published": 262, "omitted": 0}
    bundle["operations"]["topology_availability"] = {
        "state": "available",
        "admitted": {"nodes": 400, "edges": 262},
        "source": {"graph_declared": "fixture graph"},
    }
    route.fulfill(response=response, json=bundle)


def _receipt(page) -> dict[str, int | str]:
    receipt = page.locator("#deps .structural-focus-receipt")
    return {
        "mode": receipt.get_attribute("data-deps-mode") or "",
        "shown_nodes": int(receipt.get_attribute("data-shown-nodes") or "-1"),
        "admitted_nodes": int(receipt.get_attribute("data-admitted-nodes") or "-1"),
        "hidden_nodes": int(receipt.get_attribute("data-hidden-nodes") or "-1"),
        "shown_edges": int(receipt.get_attribute("data-shown-edges") or "-1"),
        "admitted_edges": int(receipt.get_attribute("data-admitted-edges") or "-1"),
        "hidden_edges": int(receipt.get_attribute("data-hidden-edges") or "-1"),
    }


def _node_ids(page) -> list[str]:
    return page.locator("#deps .gnode[data-node]").evaluate_all(
        "nodes => nodes.map(node => node.dataset.node)"
    )


def test_dependencies_default_structural_focus_is_bounded_truthful_and_reversible(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        failed_requests: list[str] = []
        calls = 0

        def fulfill(route) -> None:
            nonlocal calls
            calls += 1
            _large_graph_bundle(route, calls)

        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", fulfill)
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_requests.append(
                f"{request.url}: {request.failure or 'request failed'}"
            ),
        )
        try:
            page.goto(f"{url}/map?lens=deps", wait_until="networkidle")
            page.locator("#deps .structural-focus-receipt").wait_for()

            focused = _receipt(page)
            assert focused["mode"] == "focus"
            assert focused["shown_nodes"] == 60
            assert focused["admitted_nodes"] == 400
            assert focused["hidden_nodes"] == 340
            assert focused["shown_edges"] + focused["hidden_edges"] == 262
            assert focused["admitted_edges"] == 262
            assert page.locator("#deps .gnode[data-node]").count() == focused["shown_nodes"]
            assert page.locator("#deps .gwrap svg > path.edge").count() == focused["shown_edges"]
            focus_button = page.locator("[data-deps-scope='focus']")
            expand_button = page.locator("[data-deps-scope='admitted']")
            assert focus_button.get_attribute("aria-pressed") == "true"
            assert "active" in (focus_button.get_attribute("class") or "").split()
            assert expand_button.get_attribute("aria-pressed") == "false"
            assert "active" not in (expand_button.get_attribute("class") or "").split()
            receipt_text = page.locator("#deps .structural-focus-receipt").inner_text()
            assert "Focus chose work:N000" in receipt_text
            assert "80 admitted incident relationships are highest" in receipt_text
            assert "stable ID breaks ties" in receipt_text
            assert "No row selection was created" in receipt_text
            assert urlparse(page.url).fragment == ""
            assert page.locator("#drawer").get_attribute("aria-hidden") == "true"

            focused_ids = _node_ids(page)
            assert len(focused_ids) == len(set(focused_ids)) == 60
            assert focused_ids[0] == "work:N000"
            visible_labels = page.locator("#deps .gnode text").evaluate_all(
                "labels => labels.map(label => label.textContent)"
            )
            assert len(visible_labels) == len(set(visible_labels)) == 60
            assert max(map(len, visible_labels)) <= 28
            assert all(label.startswith("job_progress") for label in visible_labels)
            halo = page.locator("#deps .gnode text").first
            assert halo.get_attribute("paint-order") == "stroke fill"
            assert halo.get_attribute("stroke") == "var(--map-surface-1)"
            assert 2 <= float(halo.get_attribute("stroke-width") or "0") <= 4
            assert halo.get_attribute("stroke-linejoin") == "round"
            seed_title = page.locator("#deps .gnode[data-node='work:N000'] title").evaluate(
                "title => title.textContent"
            )
            assert "job_progress" in seed_title
            assert "work:N000" in seed_title
            overlaps = page.locator("#deps .gnode text").evaluate_all(
                """labels => {
                  const boxes = labels.map(label => {
                    const box = label.getBoundingClientRect();
                    return {text: label.textContent, left: box.left, right: box.right,
                      top: box.top, bottom: box.bottom};
                  });
                  const pairs = [];
                  for (let left = 0; left < boxes.length; left += 1) {
                    for (let right = left + 1; right < boxes.length; right += 1) {
                      const a = boxes[left], b = boxes[right];
                      if (a.left < b.right && a.right > b.left
                          && a.top < b.bottom && a.bottom > b.top) {
                        pairs.push([a.text, b.text]);
                      }
                    }
                  }
                  return pairs;
                }"""
            )
            assert overlaps == []
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

            page.get_by_role("button", name="Expand admitted").click()
            page.wait_for_function(
                "() => document.querySelectorAll('#deps .gnode[data-node]').length === 400"
            )
            expanded = _receipt(page)
            assert expanded == {
                "mode": "admitted",
                "shown_nodes": 400,
                "admitted_nodes": 400,
                "hidden_nodes": 0,
                "shown_edges": 262,
                "admitted_edges": 262,
                "hidden_edges": 0,
            }
            assert page.locator("#deps .gwrap svg > path.edge").count() == 262
            assert expand_button.get_attribute("aria-pressed") == "true"
            assert "active" in (expand_button.get_attribute("class") or "").split()
            assert focus_button.get_attribute("aria-pressed") == "false"
            assert "active" not in (focus_button.get_attribute("class") or "").split()
            assert urlparse(page.url).fragment == ""
            assert page.locator("#drawer").get_attribute("aria-hidden") == "true"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

            page.get_by_role("button", name="Focus ≤60").click()
            page.wait_for_function(
                "() => document.querySelectorAll('#deps .gnode[data-node]').length === 60"
            )
            assert _receipt(page) == focused
            assert _node_ids(page) == focused_ids
            focus_button = page.locator("[data-deps-scope='focus']")
            expand_button = page.locator("[data-deps-scope='admitted']")
            assert focus_button.get_attribute("aria-pressed") == "true"
            assert "active" in (focus_button.get_attribute("class") or "").split()
            assert expand_button.get_attribute("aria-pressed") == "false"
            assert "active" not in (expand_button.get_attribute("class") or "").split()

            page.reload(wait_until="networkidle")
            page.locator("#deps .structural-focus-receipt").wait_for()
            assert calls >= 2
            assert _receipt(page) == focused
            assert _node_ids(page) == focused_ids
            assert urlparse(page.url).fragment == ""
            assert page.locator("#drawer").get_attribute("aria-hidden") == "true"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        finally:
            browser.close()
        errors[:] = [value for value in errors if "404" not in value]
        assert errors == []
        assert failed_requests == []
