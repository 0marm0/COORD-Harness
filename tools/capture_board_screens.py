#!/usr/bin/env python3
"""Capture declared board views from a running synthetic demo server."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

BOARD_VIEWS = ("overview", "work", "jobs", "graph", "activity")
MAP_VIEWS = ("fleet", "pulse", "flowpath", "ceiling", "topology", "deps", "shape", "crossings", "chronicle", "subjects", "orbit", "context")
ATLAS_VIEWS = ("operations-atlas-overview", "operations-atlas-topology")
MESH_VIEWS = (
    "swarm-mesh-context",
    "swarm-mesh-owners",
    "swarm-mesh-critical",
    "swarm-mesh-traversal",
    "swarm-mesh-mobile",
)
VIEWS = BOARD_VIEWS + MAP_VIEWS + ATLAS_VIEWS + MESH_VIEWS
VIEWPORT = {"width": 1600, "height": 1000}


def capture(url: str, output_dir: Path, views: Sequence[str]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - optional documentation tool
        raise SystemExit(
            "Playwright is required: install it and its Chromium runtime before capture"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            color_scheme="dark",
            locale="en-US",
            timezone_id="UTC",
            reduced_motion="reduce",
        )
        page = context.new_page()
        browser_errors: list[str] = []
        page.on(
            "console",
            lambda message: browser_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: browser_errors.append(str(error)))

        board = [v for v in views if v in BOARD_VIEWS]
        if board:
            page.goto(url, wait_until="networkidle")
            page.wait_for_function(
                """() => {
                  const health = document.querySelector('#health');
                  const alert = document.querySelector('#boardreadalert');
                  return health?.textContent.startsWith('Live · gen ') && alert?.hidden;
                }"""
            )
            required = {
                "overview": "#overview .metrics .metric, #overview [data-row]",
                "work": "#work .worktable [data-row]",
                "jobs": "#jobs .card",
                "graph": "#graph .gnode",
                "activity": "#activity .card",
            }
            for view in board:
                page.locator(f'#rail button[data-view="{view}"]').click()
                page.locator(f"#{view}.panel.active").wait_for()
                page.evaluate("window.scrollTo(0, 0)")
                if page.locator(required[view]).count() == 0:
                    raise RuntimeError(f"board capture refused: {view} rendered nothing")
                if view == "graph" and page.locator("#graph path.gedge").count() == 0:
                    raise RuntimeError("graph capture refused: no rendered relationship edges")
                page.screenshot(
                    path=output_dir / f"board-{view}.png",
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                )

        # The map is its own page, and each view refuses to be captured empty:
        # a screenshot of a surface that failed to load looks like a design
        # decision rather than a failure, which is the worst kind of artifact.
        wanted = [v for v in views if v in MAP_VIEWS]
        if wanted:
            page.goto(f"{url.rstrip('/')}/map", wait_until="networkidle")
            page.locator("#maptabs").wait_for()
            # Each view names a mark that must be present. A capture of a
            # surface that failed to render reads as a design decision rather
            # than a failure, which is the worst kind of artifact.
            required = {
                "fleet": "#fleet td.c",
                "pulse": "#pulse .pl-stat",
                "flowpath": "#flowpath [data-key]",
                "ceiling": "#ceiling svg",
                "topology": "#topology .tp-lane",
                "deps": "#deps .gnode",
                "shape": "#shape .dvz-tile",
                "crossings": "#crossings [data-row], #crossings .xnode",
                "chronicle": "#chronicle .ord-mark",
                "subjects": "#subjects .tz-cell",
                "orbit": "#orbit .ob-node",
                "context": "#context .note",
            }
            for view in wanted:
                page.locator(f'#maptabs button[data-tab="{view}"]').click()
                page.locator(f"#{view}.panel.active").wait_for()
                page.evaluate("window.scrollTo(0, 0)")
                if page.locator(required[view]).count() == 0:
                    raise RuntimeError(f"map capture refused: {view} rendered nothing")
                page.screenshot(
                    path=output_dir / f"map-{view}.png",
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                )

        atlas = [view for view in views if view in ATLAS_VIEWS]
        if atlas:
            page.goto(f"{url.rstrip('/')}/ops", wait_until="networkidle")
            page.locator(".document-stage").nth(5).wait_for()
            page.locator(".atlas-node").first.wait_for()
            future_signal = page.locator(".health-item").filter(has_text="future_events")
            if (
                not page.locator("#atlas-clock").inner_text().startswith("LIVE")
                or page.locator("#atlas-alert").is_visible()
                or future_signal.locator(".health-count").inner_text().strip() != "0"
            ):
                raise RuntimeError(
                    "operations capture refused: read or temporal integrity is degraded"
                )
            if page.locator(".atlas-edge").count() == 0:
                raise RuntimeError("operations capture refused: topology has no relationships")
            for view in atlas:
                if view == "operations-atlas-overview":
                    page.evaluate("window.scrollTo(0, 0)")
                else:
                    page.locator("#atlas-zoom-fit").click()
                    page.locator(".topology-card").scroll_into_view_if_needed()
                page.screenshot(
                    path=output_dir / f"{view}.png",
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                )

        mesh = [view for view in views if view in MESH_VIEWS]
        if mesh:
            page.set_viewport_size(VIEWPORT)
            page.goto(f"{url.rstrip('/')}/mesh", wait_until="networkidle")
            page.locator("#mesh-canvas").wait_for()
            page.wait_for_function(
                """() => {
                  const nodes = Number.parseInt(document.querySelector('#rail-nodes')?.textContent || '', 10);
                  const edges = Number.parseInt(document.querySelector('#rail-edges')?.textContent || '', 10);
                  return Number.isFinite(nodes) && Number.isFinite(edges);
                }"""
            )
            nodes = int(page.locator("#rail-nodes").inner_text())
            edges = int(page.locator("#rail-edges").inner_text())
            if nodes <= 0 or edges <= 0 or page.locator("#mesh-topology-receipt").is_visible():
                raise RuntimeError(
                    "mesh capture refused: populated authoritative topology was not admitted"
                )
            if page.locator(".cluster-row").count() == 0:
                raise RuntimeError("mesh capture refused: cluster roster rendered nothing")

            def choose_layout(name: str) -> None:
                control = page.locator(f'[data-layout="{name}"]')
                control.click()
                page.wait_for_function(
                    "name => document.querySelector(`[data-layout=\"${name}\"]`)?.getAttribute('aria-pressed') === 'true'",
                    arg=name,
                )

            def choose_perspective() -> None:
                control = page.locator('[data-projection="perspective"]')
                control.click()
                page.wait_for_function(
                    "() => document.querySelector('[data-projection=\"perspective\"]')?.getAttribute('aria-pressed') === 'true'"
                )
                if "LAYOUT-ONLY Z" not in page.locator("#projection-truth").inner_text():
                    raise RuntimeError("mesh capture refused: perspective truth receipt missing")

            for view in mesh:
                page.set_viewport_size(
                    {"width": 390, "height": 844} if view == "swarm-mesh-mobile" else VIEWPORT
                )
                choose_layout("context")
                choose_perspective()
                page.locator("#clear-selection").click() if page.locator(
                    "#clear-selection:not([hidden])"
                ).count() else None

                if view == "swarm-mesh-owners":
                    choose_layout("swarm")
                elif view == "swarm-mesh-critical":
                    choose_layout("critical")
                elif view == "swarm-mesh-traversal":
                    page.locator('[data-question="impact"]').click()
                    page.locator("#selection-inspector h3").wait_for()
                    if page.locator("#path-ledger li").count() == 0:
                        raise RuntimeError("mesh capture refused: traversal rendered no path")

                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(
                    path=output_dir / f"{view}.png",
                    full_page=False,
                    animations="disabled",
                    caret="hide",
                )

            page.set_viewport_size(VIEWPORT)
        if browser_errors:
            raise RuntimeError(
                "capture refused: browser emitted errors: " + " | ".join(browser_errors)
            )
        context.close()
        browser.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7870")
    parser.add_argument("--output-dir", type=Path, default=Path("docs/assets/screens"))
    parser.add_argument("--views", nargs="+", choices=VIEWS, default=list(VIEWS))
    args = parser.parse_args(argv)
    capture(args.url, args.output_dir, args.views)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
