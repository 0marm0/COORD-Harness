"""Tests for tools/render_terminal_cast.py and the shipped proof-gated-done SVG.

Three things matter for a rendered terminal cast that is supposed to be the
README's headline picture: it must be reproducible (the same transcript always
produces the same bytes), it must be accessible (title/desc plus a
reduced-motion branch that freezes on the finished transcript instead of
hiding it), and the committed SVG must actually be what the renderer produces
from the committed transcript today -- not a stale capture from before the
renderer changed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RENDERER_PATH = REPO / "tools" / "render_terminal_cast.py"
TRANSCRIPT_PATH = REPO / "docs" / "assets" / "casts" / "proof-gated-done.json"
SVG_PATH = REPO / "docs" / "assets" / "proof-gated-done.svg"

SVG_NS = "http://www.w3.org/2000/svg"


def _load_renderer():
    """Import tools/render_terminal_cast.py without requiring tools/ on sys.path."""
    spec = importlib.util.spec_from_file_location("render_terminal_cast", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rtc = _load_renderer()


@pytest.fixture(scope="module")
def transcript() -> dict:
    return rtc.load_transcript(TRANSCRIPT_PATH)


def test_transcript_is_a_real_captured_run(transcript: dict) -> None:
    """The raw capture this module renders must name what produced it and
    show the actual refuse/refuse/accept sequence, not placeholder text."""
    assert "run.py" in transcript["header"]
    stdout = "\n".join(transcript["stdout_lines"])
    assert "does not exist" in stdout
    assert "not carried by git's index" in stdout
    assert '"ok": true' in stdout


def test_render_is_byte_deterministic(transcript: dict) -> None:
    first = rtc.render_svg_bytes(transcript)
    second = rtc.render_svg_bytes(transcript)
    assert first == second
    # Also true across a fresh module import (no hidden module-level state).
    reloaded = _load_renderer()
    third = reloaded.render_svg_bytes(transcript)
    assert first == third


def test_render_has_no_obvious_nondeterminism_sources() -> None:
    source = RENDERER_PATH.read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "time.time", "uuid.uuid4", "random.", "os.urandom"):
        assert forbidden not in source, f"renderer must not use {forbidden}"


def test_svg_is_well_formed_and_accessible(transcript: dict) -> None:
    svg_bytes = rtc.render_svg_bytes(transcript)
    root = ET.fromstring(svg_bytes)
    assert root.tag == f"{{{SVG_NS}}}svg"
    assert root.get("role") == "img"

    labelledby = (root.get("aria-labelledby") or "").split()
    assert len(labelledby) == 2

    by_id = {el.get("id"): el for el in root.iter() if el.get("id")}
    title_id, desc_id = labelledby
    title_el = by_id[title_id]
    desc_el = by_id[desc_id]
    assert title_el.tag == f"{{{SVG_NS}}}title"
    assert desc_el.tag == f"{{{SVG_NS}}}desc"
    assert "".join(title_el.itertext()).strip()
    assert "".join(desc_el.itertext()).strip()


def test_svg_has_a_reduced_motion_branch_that_freezes_on_final_frame(transcript: dict) -> None:
    svg_text = rtc.render_svg_bytes(transcript).decode("utf-8")

    match = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\}\s*\}", svg_text)
    assert match, "expected a prefers-reduced-motion block in the embedded <style>"
    assert "animation: none" in match.group(1)

    # The reduced-motion rule disables `animation`, which reverts every
    # animated element to its *base* (non-keyframe) declared style. That base
    # style must be the fully-revealed state, never a hidden one:
    #   - command clip-rects carry their full (non-zero) width as a plain
    #     attribute (the "from { width: 0 }" zero only exists inside the
    #     @keyframes rule, which no longer applies once disabled);
    #   - the `.cast-fade` and `.cast-cursor` base classes declare opacity 1,
    #     not 0 -- the zero only appears inside their @keyframes' `from`.
    for rect_match in re.finditer(r'<rect x="0" y="[^"]+" width="([0-9.]+)"[^>]*/>', svg_text):
        assert float(rect_match.group(1)) > 0, "a clip rect's base width must be its full width"
    assert ".cast-fade { opacity: 1; transform: translateY(0); }" in svg_text
    assert ".cast-cursor { opacity: 1; }" in svg_text
    # Confirm the zero-opacity values that DO exist are confined to
    # @keyframes blocks (the pre-animation "from" state), not base rules.
    style_block = re.search(r"<style>(.*?)</style>", svg_text, re.DOTALL).group(1)
    non_keyframe_text = re.sub(r"@keyframes\s+\w+\s*\{.*?\}\s*\}", "", style_block, flags=re.DOTALL)
    assert re.search(r"opacity:\s*0\s*;", non_keyframe_text) is None, non_keyframe_text


def test_svg_content_traces_to_the_real_transcript(transcript: dict) -> None:
    """Every command/output line rendered must be a (possibly truncated)
    prefix of an actual transcript line -- nothing invented."""
    svg_text = rtc.render_svg_bytes(transcript).decode("utf-8")
    lines = rtc.build_display_lines(transcript["stdout_lines"])
    for line in lines:
        if line.kind in ("command", "output"):
            core = line.text[:-1] if line.truncated else line.text
            assert core[:20] in svg_text or rtc._xml_escape(core[:20]) in svg_text


def test_committed_svg_matches_a_fresh_render_of_the_committed_transcript() -> None:
    if not SVG_PATH.is_file():
        pytest.skip("docs/assets/proof-gated-done.svg not present in this checkout")
    transcript = rtc.load_transcript(TRANSCRIPT_PATH)
    expected = rtc.render_svg_bytes(transcript)
    actual = SVG_PATH.read_bytes()
    assert actual == expected, (
        "the committed SVG is stale relative to tools/render_terminal_cast.py "
        "and/or docs/assets/casts/proof-gated-done.json -- regenerate it with "
        "`render_terminal_cast.py docs/assets/casts/proof-gated-done.json "
        "docs/assets/proof-gated-done.svg`"
    )


def test_transcript_json_is_registered_in_provenance() -> None:
    provenance_path = REPO / "docs" / "assets" / "provenance.json"
    document = json.loads(provenance_path.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in document["assets"] if isinstance(entry, dict)}
    assert "docs/assets/proof-gated-done.svg" in paths
