from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from urllib.parse import quote

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


def test_atlas_board_selection_hash_is_bidirectional_and_source_bound(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as (server, url), playwright_api.sync_playwright() as playwright:
        source_bundle = server.operations_bundle()
        envelope_nodes = source_bundle["operations"]["graph_envelope"]["nodes"]
        work_ids = [
            str(node["id"])
            for node in envelope_nodes
            if str(node.get("id", "")).startswith("work:")
        ]
        assert len(work_ids) >= 3
        initial_id, hashchange_id, omitted_id = work_ids[0], work_ids[1], work_ids[-1]

        def omit_one_admitted_node(route) -> None:
            response = route.fetch()
            bundle = response.json()
            operations = bundle["operations"]
            envelope = operations["graph_envelope"]
            envelope["nodes"] = [
                node for node in envelope["nodes"] if str(node.get("id")) != omitted_id
            ]
            envelope["edges"] = [
                edge
                for edge in envelope["edges"]
                if omitted_id not in (str(edge.get("source")), str(edge.get("target")))
            ]
            envelope["emitted"]["nodes"] = len(envelope["nodes"])
            envelope["emitted"]["edges"] = len(envelope["edges"])
            envelope["omitted"]["nodes"] = max(
                1, int(envelope["population"]["nodes"]) - len(envelope["nodes"])
            )
            envelope["omitted"]["edges"] = max(
                0, int(envelope["population"]["edges"]) - len(envelope["edges"])
            )
            envelope["omitted"]["node_reasons"] = [{"reason": "url_selection_fixture", "count": 1}]
            envelope["complete"] = False
            operations["metrics"]["graph_nodes_emitted"] = len(envelope["nodes"])
            operations["metrics"]["graph_edges_emitted"] = len(envelope["edges"])
            bundle["graph"]["nodes"] = [
                node for node in bundle["graph"]["nodes"] if str(node.get("id")) != omitted_id
            ]
            bundle["graph"]["edges"] = [
                edge
                for edge in bundle["graph"]["edges"]
                if omitted_id not in (str(edge.get("source")), str(edge.get("target")))
            ]
            route.fulfill(response=response, json=bundle)

        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
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
        page.route(
            "**/api/v2/operations-bundle", lambda route: route.fulfill(status=404, body="{}")
        )
        page.route("**/api/v1/operations-bundle", omit_one_admitted_node)

        try:
            page.goto(
                f"{url}/ops?surface=board&keep=1#sel={quote(initial_id, safe='')}",
                wait_until="networkidle",
            )
            playwright_api.expect(page.locator("#atlas-inspector code").first).to_have_text(
                initial_id
            )
            assert page.evaluate("window.location.search") == "?surface=board&keep=1"
            assert (
                page.evaluate("document.activeElement && document.activeElement.dataset.nodeId")
                == initial_id
            )

            page.evaluate(
                "id => { window.location.hash = 'sel=' + encodeURIComponent(id); }",
                hashchange_id,
            )
            playwright_api.expect(page.locator("#atlas-inspector code").first).to_have_text(
                hashchange_id
            )
            assert (
                page.evaluate("document.activeElement && document.activeElement.dataset.nodeId")
                == hashchange_id
            )

            page.evaluate(
                "window.__atlasHashChanges = 0; window.addEventListener('hashchange', () => { window.__atlasHashChanges += 1; });"
            )
            clickable = page.locator(".atlas-node").filter(has_not_text=hashchange_id).first
            clicked_id = clickable.get_attribute("data-node-id")
            assert clicked_id
            clickable.click()
            playwright_api.expect(page.locator("#atlas-inspector code").first).to_have_text(
                clicked_id
            )
            assert (
                page.evaluate("new URLSearchParams(window.location.hash.slice(1)).get('sel')")
                == clicked_id
            )
            assert page.evaluate("window.location.search") == "?surface=board&keep=1"
            assert page.evaluate("window.__atlasHashChanges") == 0

            page.keyboard.press("Escape")
            playwright_api.expect(page.locator("#inspector-title")).to_have_text("Walk the graph.")
            assert (
                page.evaluate("new URLSearchParams(window.location.hash.slice(1)).has('sel')")
                is False
            )
            assert page.evaluate("window.location.search") == "?surface=board&keep=1"

            page.evaluate(
                "id => { window.location.hash = 'sel=' + encodeURIComponent(id); }",
                omitted_id,
            )
            unavailable = page.locator("#atlas-inspector .inspector-empty")
            playwright_api.expect(unavailable).to_contain_text("not admitted in this projection")
            assert omitted_id in unavailable.inner_text()
            assert "operations.graph_envelope" in unavailable.inner_text()
            assert "does not establish" in unavailable.inner_text().lower()
            assert "coord.db" in unavailable.inner_text()
            assert page.locator(".atlas-node.selected").count() == 0
            assert page.evaluate("window.location.search") == "?surface=board&keep=1"
        finally:
            browser.close()

        console_errors[:] = [value for value in console_errors if "404" not in value]
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
