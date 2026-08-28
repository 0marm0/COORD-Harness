"""Palette regression: no accent's colours survive a switch to the other accent.

Two colours once escaped the accent switch and were found only by sweeping
computed styles in a live browser: an inline literal (which outranks any token,
so a token-level audit reads clean) and a `var()` fallback whose variable was
never defined (which no stylesheet grep for the hue would find, because the
leaked value only appears once the browser resolves the fallback). Both are
invisible to source-text checks and visible to this one.

The hue and glow values are parsed out of the *served* `accent.js` rather than
written down here. Restating them would only create a second place for the
palette to be true, and the copy would rot silently the first time taste changed.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Iterator
from urllib.request import urlopen

import pytest

from coordharness import demo
from coordharness.board.server import make_server

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright  # noqa: E402

VIEWPORT = {"width": 1600, "height": 1000}
BOARD_ROUTES = (
    ("v=attention", "attention"),
    ("v=overview", "overview"),
    ("v=work&layout=board", "work"),
    ("v=work&layout=list", "work"),
    ("v=jobs", "jobs"),
    ("v=graph", "graph"),
    ("v=usage", "usage"),
    ("v=activity", "work"),
)
MAP_TABS = ("fleet", "deps", "shape", "crossings", "context")
# The palette entries that must be visible for the sweep to have proved anything.
# A neutral may legitimately go unused on a given page; the accent hue and its
# ambient glow are on every page by construction.
REQUIRED_ON_SCREEN = ("hue", "glow")

# Every property that can carry a colour to the screen. `backgroundImage` and
# `boxShadow` are in the list because the ambient accent glow reaches the page
# through a radial-gradient, never through `background-color`: a sweep of the
# plain colour properties would report a clean page while the glow of the other
# accent sat behind the masthead.
COLOUR_PROPERTIES = (
    "color",
    "backgroundColor",
    "backgroundImage",
    "boxShadow",
    "borderTopColor",
    "borderRightColor",
    "borderBottomColor",
    "borderLeftColor",
    "outlineColor",
    "textDecorationColor",
    "textShadow",
    "caretColor",
    "fill",
    "stroke",
)

# Walks the rendered document -- elements and their ::before/::after boxes --
# and reports every declaration whose resolved colour matches a forbidden RGB
# triple. Alpha is ignored on purpose: the glow ships at 0.53 and 0.67 alpha
# depending on the accent, and the browser's own rounding of those is not a
# fact worth asserting. The triple is the identity of the colour.
SWEEP_JS = """
(input) => {
  const props = input.properties;
  const rgbAt = /rgba?\\(\\s*([\\d.]+)[\\s,]+([\\d.]+)[\\s,]+([\\d.]+)/g;
  const key = (r, g, b) => r + "," + g + "," + b;
  const forbidden = new Map(input.forbidden.map(c => [key(...c.rgb), c.label]));
  const expected = new Map(input.expected.map(c => [key(...c.rgb), c.label]));
  const describe = (el) => {
    let out = el.tagName.toLowerCase();
    if (el.id) out += "#" + el.id;
    if (el.classList.length) out += "." + Array.from(el.classList).join(".");
    const row = el.closest("[data-row]");
    if (row) out += " [data-row=" + row.getAttribute("data-row") + "]";
    return out;
  };
  const findings = [];
  const hits = {};
  let elements = 0;
  let declarations = 0;
  for (const el of document.querySelectorAll("*")) {
    elements += 1;
    for (const pseudo of [null, "::before", "::after"]) {
      const style = getComputedStyle(el, pseudo);
      for (const prop of props) {
        const value = style[prop];
        if (!value || value === "none") continue;
        declarations += 1;
        rgbAt.lastIndex = 0;
        let match;
        while ((match = rgbAt.exec(value)) !== null) {
          const k = key(
            Math.round(parseFloat(match[1])),
            Math.round(parseFloat(match[2])),
            Math.round(parseFloat(match[3])));
          if (forbidden.has(k)) {
            findings.push({
              colour: forbidden.get(k),
              element: describe(el) + (pseudo || ""),
              property: prop,
              value: value.slice(0, 160),
            });
          }
          if (expected.has(k)) {
            const label = expected.get(k);
            hits[label] = (hits[label] || 0) + 1;
          }
        }
      }
    }
  }
  return {findings, hits, elements, declarations};
}
"""


def _launch(playwright: Any) -> Any:
    """Chromium, or a clean skip when only the runtime is missing."""
    try:
        return playwright.chromium.launch()
    except PlaywrightError as exc:  # pragma: no cover - depends on the host install
        pytest.skip(f"playwright chromium runtime unavailable: {exc}")


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """`#rrggbb` or `#rrggbbaa` -> the RGB triple, alpha discarded."""
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    if len(digits) not in (6, 8):
        raise ValueError(f"unparseable colour literal: {value!r}")
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def _parse_accents(source: str) -> dict[str, dict[str, tuple[int, int, int]]]:
    """Read the accent palettes out of the served accent.js text.

    Each palette is its own complete ground, not one hue swapped in, so the
    neutrals are read too: a panel or hairline carrying the other palette's
    bias is the same defect as a leaked hue, just quieter.

    A parse that quietly returns nothing would turn every assertion below into
    a check with no content, so this raises rather than returning empty.
    """
    pattern = re.compile(
        r"(\w+)\s*:\s*\{\s*"
        r"label\s*:\s*\"[^\"]*\"\s*,\s*"
        r"hue\s*:\s*\"(#[0-9a-fA-F]{3,8})\"\s*,\s*"
        r"glow\s*:\s*\"(#[0-9a-fA-F]{3,8})\"\s*,\s*"
        r"tokens\s*:\s*\{(.*?)\}",
        re.DOTALL,
    )
    token_pattern = re.compile(r"\"(--[\w-]+)\"\s*:\s*\"(#[0-9a-fA-F]{3,8})\"")
    accents: dict[str, dict[str, tuple[int, int, int]]] = {}
    for name, hue, glow, tokens in pattern.findall(source):
        palette = {"hue": _hex_to_rgb(hue), "glow": _hex_to_rgb(glow)}
        palette.update(
            {token: _hex_to_rgb(value) for token, value in token_pattern.findall(tokens)}
        )
        accents[name] = palette
    if len(accents) < 2 or any(len(p) <= len(REQUIRED_ON_SCREEN) for p in accents.values()):
        raise AssertionError(
            f"accent.js parse found {len(accents)} palette(s) "
            f"{ {k: len(v) for k, v in accents.items()} }; the palette shape changed and "
            "this test is measuring nothing until the parse is updated"
        )
    return accents


def _parse_store_key(source: str) -> str:
    match = re.search(r"const\s+STORE\s*=\s*\"([^\"]+)\"", source)
    if not match:
        raise AssertionError("accent.js no longer declares a STORE key for the preference")
    return match.group(1)


@pytest.fixture(scope="module")
def board(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A seeded throwaway board on an ephemeral port."""
    db: Path = tmp_path_factory.mktemp("palette") / "coord.db"
    demo.seed(db, quiet=True)
    server = make_server(port=0, db_path=str(db), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def accents(board: str) -> dict[str, dict[str, tuple[int, int, int]]]:
    with urlopen(f"{board}/static/accent.js", timeout=5) as response:
        return _parse_accents(response.read().decode("utf-8"))


@pytest.fixture(scope="module")
def store_key(board: str) -> str:
    with urlopen(f"{board}/static/accent.js", timeout=5) as response:
        return _parse_store_key(response.read().decode("utf-8"))


def _sweep(page: Page, forbidden: list[dict[str, Any]], expected: list[dict[str, Any]]) -> dict:
    return page.evaluate(
        SWEEP_JS,
        {
            "properties": list(COLOUR_PROPERTIES),
            "forbidden": forbidden,
            "expected": expected,
        },
    )


def _open_every_surface(
    page: Page, url: str, tabs: tuple[str, ...], selector: str, attribute: str = "data-tab"
) -> None:
    """Click through the destinations so panels that mount on demand are in the DOM.

    The board's compatibility destinations are exercised by direct hash below;
    this helper remains for the map's route-local lens controls. The attribute
    is a parameter rather than a guess so a renamed control fails loudly here
    instead of quietly sweeping a page whose panels never mounted.
    """
    for tab in tabs:
        control = page.locator(f'{selector}button[{attribute}="{tab}"]')
        if selector == "#maptabs " and not control.is_visible():
            page.locator("#map-more-trigger").click()
            control.wait_for(state="visible")
        control.click()
        page.locator(f"#{tab}.panel.active").wait_for()


def _swatch(name: str, kind: str, rgb: tuple[int, int, int]) -> dict[str, Any]:
    return {"label": f"{name} {kind}", "rgb": list(rgb)}


def _forbidden(
    accents: dict[str, dict[str, tuple[int, int, int]]], active: str
) -> list[dict[str, Any]]:
    """Every colour of every other palette, minus any the active palette shares.

    Two palettes are free to agree on a value. Reporting a shared colour as a
    leak would make this test fail on a page that is entirely correct.
    """
    own = set(accents[active].values())
    return [
        _swatch(name, kind, rgb)
        for name, colours in accents.items()
        if name != active
        for kind, rgb in colours.items()
        if rgb not in own
    ]


def _expected(
    accents: dict[str, dict[str, tuple[int, int, int]]], active: str
) -> list[dict[str, Any]]:
    """The colours that must be on screen for the sweep to be worth believing."""
    return [_swatch(active, kind, accents[active][kind]) for kind in REQUIRED_ON_SCREEN]


def test_no_foreign_accent_colour_survives_the_accent_switch(
    board: str,
    accents: dict[str, dict[str, tuple[int, int, int]]],
    store_key: str,
) -> None:
    """With one accent selected, no colour of any other accent reaches the screen."""
    findings: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, int]] = {}
    with sync_playwright() as playwright:
        browser = _launch(playwright)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            color_scheme="dark",
            locale="en-US",
            timezone_id="UTC",
            reduced_motion="reduce",
        )
        page = context.new_page()
        try:
            for name in accents:
                forbidden = _forbidden(accents, name)
                expected = _expected(accents, name)
                hits: dict[str, int] = {}

                page.goto(board, wait_until="networkidle")
                page.evaluate(
                    "([key, value]) => localStorage.setItem(key, value)", [store_key, name]
                )
                for capsule, panel in BOARD_ROUTES:
                    url = f"{board}/#{capsule}"
                    page.goto("about:blank")
                    page.goto(url, wait_until="networkidle")
                    assert page.evaluate("document.documentElement.dataset.accent") == name, (
                        f"{url} did not adopt the stored accent {name!r}"
                    )
                    page.locator(f"#{panel}.panel.active").wait_for()
                    assert page.locator("#rail").is_hidden()
                    result = _sweep(page, forbidden, expected)
                    assert result["elements"] > 100, f"{url} rendered {result['elements']} elements"
                    for finding in result["findings"]:
                        findings.append({"accent": name, "url": url, **finding})
                    for label, count in result["hits"].items():
                        hits[label] = hits.get(label, 0) + count

                url = f"{board}/map"
                page.goto(url, wait_until="networkidle")
                # A silent fallback to the default accent would make every
                # assertion below pass by describing a page we never rendered.
                assert page.evaluate("document.documentElement.dataset.accent") == name, (
                    f"{url} did not adopt the stored accent {name!r}"
                )
                _open_every_surface(page, url, MAP_TABS, "#maptabs ", "data-tab")
                result = _sweep(page, forbidden, expected)
                assert result["elements"] > 100, f"{url} rendered {result['elements']} elements"
                for finding in result["findings"]:
                    findings.append({"accent": name, "url": url, **finding})
                for label, count in result["hits"].items():
                    hits[label] = hits.get(label, 0) + count
                coverage[name] = hits
        finally:
            context.close()
            browser.close()

    # A sweep that found nothing because it can see nothing is not evidence.
    # Under each accent, that accent's own hue and glow must be on the page.
    for name in accents:
        for kind in REQUIRED_ON_SCREEN:
            label = f"{name} {kind}"
            assert coverage[name].get(label, 0) > 0, (
                f"sweep saw no {label} while {name} was the active accent -- "
                f"the sweep is not reading the palette it claims to police "
                f"(hits: {coverage[name]})"
            )

    assert findings == [], (
        "colours escaped the accent switch:\n" + json.dumps(findings, indent=2)
    )


def test_ablation_the_sweep_catches_an_injected_literal(
    board: str,
    accents: dict[str, dict[str, tuple[int, int, int]]],
    store_key: str,
) -> None:
    """Self-test: plant the defect and prove the sweep above goes red on it.

    The literal is inserted through the CSSOM, not `page.add_style_tag`. The
    board serves `style-src 'self'` with no unsafe-inline, so an injected
    <style> element is refused by the browser -- an ablation written that way
    would change nothing and its "the sweep found it" assertion would be
    testing the injection failure, not the sweep. `insertRule` on an
    already-served sheet is the same defect the real regression was: a literal
    in a stylesheet that outranks every token.
    """
    name = "green" if "green" in accents else sorted(accents)[0]
    other = next(k for k in accents if k != name)
    planted = accents[other]["hue"]
    forbidden = _forbidden(accents, name)

    with sync_playwright() as playwright:
        browser = _launch(playwright)
        context = browser.new_context(
            viewport=VIEWPORT, color_scheme="dark", reduced_motion="reduce"
        )
        page = context.new_page()
        try:
            page.goto(board, wait_until="networkidle")
            page.evaluate("([key, value]) => localStorage.setItem(key, value)", [store_key, name])
            page.goto(board, wait_until="networkidle")
            assert page.evaluate("document.documentElement.dataset.accent") == name

            clean = _sweep(page, forbidden, [])
            assert clean["findings"] == [], "the page was already dirty before the ablation"

            literal = "#%02x%02x%02x" % planted
            moved = page.evaluate(
                """(literal) => {
                  const target = document.querySelector("nav") || document.body;
                  target.classList.add("palette-ablation");
                  const before = getComputedStyle(target).color;
                  const sheet = document.styleSheets[0];
                  sheet.insertRule(
                    ".palette-ablation{color:" + literal + "}", sheet.cssRules.length);
                  return {before, after: getComputedStyle(target).color};
                }""",
                literal,
            )
            # An ablation that failed to move its own input is indistinguishable
            # from a guard that works, so prove the colour actually changed.
            assert moved["before"] != moved["after"], (
                f"ablation did not change anything: {moved}"
            )
            assert moved["after"] == "rgb(%d, %d, %d)" % planted, (
                f"ablation planted an unexpected colour: {moved}"
            )

            dirty = _sweep(page, forbidden, [])
        finally:
            context.close()
            browser.close()

    caught = [f for f in dirty["findings"] if f["colour"] == f"{other} hue"]
    assert caught, (
        f"the sweep did not catch a planted {other} literal "
        f"({moved['before']} -> {moved['after']}); findings: {dirty['findings']}"
    )
