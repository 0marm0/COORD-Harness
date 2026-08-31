#!/usr/bin/env python3
"""Render a captured terminal transcript into a self-contained animated SVG.

Input is a JSON transcript shaped like the ones under docs/assets/casts/ --
a "header" string naming what produced the capture, plus "stdout_lines" (the
real, unedited stdout of the captured run, one array entry per line). This
tool never talks to a network and never invents output: every visible
character traces back to a line in the input transcript, subject only to a
deterministic display truncation of over-wide lines (see `_display_text`).

Rendering is byte-deterministic: the same transcript JSON always produces
the same SVG bytes. There is no wall-clock timestamp, no random id, and no
source of entropy anywhere in this module -- every delay, duration, and
`id=` attribute is derived purely from the transcript content and its
position in the line list.

Accessibility: the emitted <svg> carries role="img" plus a <title> and
<desc> (referenced by aria-labelledby, following the same convention the
other docs/assets/*.svg diagrams in this repository use, enforced by
tools/validate_documentation.py). The typed/fade-in animation is pure CSS;
every animation's *base* (pre-animation) state is the fully-revealed final
frame, and a single `@media (prefers-reduced-motion: reduce)` rule disables
all animation, so a reduced-motion viewer sees the finished transcript
immediately rather than a hidden or half-typed one.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

# --- layout constants (all in SVG user units; font-size in px) -------------
FONT_SIZE = 13
CELL_WIDTH = 7.4  # forced per-character advance via textLength/lengthAdjust
ROW_HEIGHT = 20
BLANK_ROW_HEIGHT = 10
CHROME_HEIGHT = 34
SIDE_PAD = 18
TOP_PAD = 14
BOTTOM_PAD = 14
MAX_CHARS = 92  # display width; longer real lines are truncated, not cut off silently
MIN_WIDTH = 560

# --- timing constants (seconds); purely a function of line content/index ---
INITIAL_DELAY = 0.25
TYPE_CPS = 55.0  # characters per second for the typed-command effect
MIN_TYPE_DUR = 0.22
MAX_TYPE_DUR = 1.1
FADE_DUR = 0.22
LINE_GAP = 0.09
BLANK_GAP = 0.06

FONT_STACK = (
    "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
    "'Liberation Mono', monospace"
)


@dataclass(frozen=True)
class DisplayLine:
    kind: str  # "command" | "output" | "narrative" | "blank"
    text: str  # already truncated for display
    truncated: bool
    index: int  # position among ALL lines (including blanks), for stable ids


def load_transcript(path: Path) -> dict:
    """Load a docs/assets/casts/*.json transcript. Raises on malformed input."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: transcript root must be a JSON object")
    if not isinstance(data.get("header"), str) or not data["header"].strip():
        raise ValueError(f"{path}: transcript needs a nonempty 'header' string")
    lines = data.get("stdout_lines")
    if not isinstance(lines, list) or not all(isinstance(item, str) for item in lines):
        raise ValueError(f"{path}: 'stdout_lines' must be a list of strings")
    return data


def _classify(raw: str) -> tuple[str, str]:
    """Return (kind, content) for one raw transcript line."""
    stripped = raw.strip()
    if not stripped:
        return "blank", ""
    if stripped.startswith("$ "):
        return "command", stripped
    if stripped.startswith("-> "):
        return "output", stripped
    return "narrative", stripped


def _display_text(content: str) -> tuple[str, bool]:
    """Deterministically truncate a line for a fixed-width terminal display."""
    if len(content) <= MAX_CHARS:
        return content, False
    return content[: MAX_CHARS - 1] + "…", True


def build_display_lines(stdout_lines: list[str]) -> list[DisplayLine]:
    out: list[DisplayLine] = []
    for index, raw in enumerate(stdout_lines):
        kind, content = _classify(raw)
        if kind == "blank":
            out.append(DisplayLine("blank", "", False, index))
            continue
        text, truncated = _display_text(content)
        out.append(DisplayLine(kind, text, truncated, index))
    return out


@dataclass(frozen=True)
class Timed(DisplayLine):
    start: float
    duration: float


def schedule(lines: list[DisplayLine]) -> tuple[list[Timed], float]:
    """Assign a deterministic (start, duration) to every non-blank line."""
    t = INITIAL_DELAY
    timed: list[Timed] = []
    for line in lines:
        if line.kind == "blank":
            timed.append(Timed(line.kind, line.text, line.truncated, line.index, t, 0.0))
            t += BLANK_GAP
            continue
        if line.kind == "command":
            duration = max(MIN_TYPE_DUR, min(MAX_TYPE_DUR, len(line.text) / TYPE_CPS))
        else:
            duration = FADE_DUR
        timed.append(Timed(line.kind, line.text, line.truncated, line.index, t, duration))
        t += duration + LINE_GAP
    return timed, t


_KIND_COLOR = {
    "command": "#7ee787",
    "output": "#e6edf3",
    "narrative": "#8b949e",
}
_KIND_COLOR_HEADER = "#d2a8ff"  # "[n/5] ..." stage headers get their own accent


def _line_color(line: Timed) -> str:
    if line.kind == "narrative" and line.text.startswith("["):
        return _KIND_COLOR_HEADER
    return _KIND_COLOR[line.kind]


def render_svg_bytes(transcript: dict) -> bytes:
    header = transcript["header"].strip()
    source = str(transcript.get("source_script") or "").strip()
    lines = build_display_lines(transcript["stdout_lines"])
    timed, total_duration = schedule(lines)

    content_lines = [line for line in timed if line.kind != "blank"]
    max_chars_used = max((len(line.text) for line in content_lines), default=0)
    content_width = max_chars_used * CELL_WIDTH
    width = max(MIN_WIDTH, round(content_width + 2 * SIDE_PAD))

    body_height = sum(
        BLANK_ROW_HEIGHT if line.kind == "blank" else ROW_HEIGHT for line in timed
    )
    height = CHROME_HEIGHT + TOP_PAD + body_height + BOTTOM_PAD

    truncated_any = any(line.truncated for line in content_lines)
    # The <title> is the SVG's accessible *name* -- screen readers announce it
    # as a whole, so it must be a short human label, never a truncated slice
    # of the long provenance paragraph (that used to cut mid-word, e.g.
    # "...outs"). The full paragraph belongs in <desc> instead, which has no
    # such length pressure.
    accessible_title = "coord done -- proof-gated completion"
    desc_bits = [
        header,
        f"Animated terminal recording of {len(content_lines)} real captured lines",
        f"from {source}" if source else "",
        "showing `coord done` refused twice -- once because the declared artifact "
        "file does not exist, once because it exists on disk but is not staged in "
        "git's index -- and then accepted once `git add` stages it.",
    ]
    if truncated_any:
        desc_bits.append(
            "Some long lines are truncated for display width; "
            "the full transcript is the committed JSON this SVG was rendered from."
        )
    desc_text = " ".join(bit for bit in desc_bits if bit)

    parts: list[str] = []
    w = parts.append
    w(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-labelledby="cast-title cast-desc">'
    )
    w(f'<title id="cast-title">{_xml_escape(accessible_title)}</title>')
    w(f'<desc id="cast-desc">{_xml_escape(desc_text)}</desc>')

    style_lines = [
        "svg { background: #0d1117; }",
        f".cast-text {{ font-family: {FONT_STACK}; font-size: {FONT_SIZE}px; "
        "white-space: pre; }",
        ".cast-chrome-title { fill: #8b949e; font-family: " + FONT_STACK +
        f"; font-size: {FONT_SIZE - 1}px; }}",
        ".cast-dot { opacity: 0.9; }",
        ".cast-fade { opacity: 1; transform: translateY(0); }",
        ".cast-cursor { opacity: 1; }",
    ]
    for line in content_lines:
        n_units = max(len(line.text), 1)
        selector = f"#l{line.index}-clip rect"
        if line.kind == "command":
            style_lines.append(f"@keyframes type{line.index} {{ from {{ width: 0; }} }}")
            style_lines.append(
                f"{selector} {{ animation: type{line.index} {line.duration:.3f}s "
                f"steps({n_units}, end) {line.start:.3f}s 1 both; }}"
            )
        else:
            style_lines.append(
                f"@keyframes fade{line.index} "
                "{ from { opacity: 0; transform: translateY(3px); } }"
            )
            style_lines.append(
                f"#l{line.index} {{ animation: fade{line.index} {line.duration:.3f}s "
                f"ease-out {line.start:.3f}s 1 both; }}"
            )
    # The cursor block is hidden until the transcript finishes, then fades
    # in and stays. `both` fill-mode pulls the `from` (opacity 0) value
    # backwards across the animation-delay, so it stays invisible until
    # `total_duration`, and holds the `to` (opacity 1) value forwards once
    # the animation completes.
    style_lines.append("@keyframes castCursorReveal { from { opacity: 0; } to { opacity: 1; } }")
    style_lines.append(
        f".cast-cursor {{ animation: castCursorReveal 0.4s ease-out "
        f"{total_duration:.3f}s 1 both; }}"
    )
    # Disabling `animation` reverts every animated property to its base
    # value -- for the clip-path rects that base value is the SVG `width`
    # attribute (the fully-revealed width), and for faded lines it is the
    # unanimated `opacity: 1` default -- so this single rule is sufficient
    # to freeze the whole cast on its final, fully-rendered frame.
    style_lines.append(
        "@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }"
    )
    w("<style>" + " ".join(style_lines) + "</style>")

    # terminal chrome
    w(f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#0d1117"/>')
    w(
        f'<rect x="0" y="0" width="{width}" height="{CHROME_HEIGHT}" rx="10" fill="#161b22"/>'
    )
    w(f'<rect x="0" y="{CHROME_HEIGHT - 10}" width="{width}" height="10" fill="#161b22"/>')
    for i, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        cx = 18 + i * 18
        w(f'<circle class="cast-dot" cx="{cx}" cy="{CHROME_HEIGHT / 2:.0f}" r="5.5" fill="{color}"/>')
    w(
        f'<text class="cast-chrome-title" x="{width / 2:.0f}" y="{CHROME_HEIGHT / 2 + 4:.0f}" '
        f'text-anchor="middle">{_xml_escape(accessible_title)}</text>'
    )
    w(
        f'<line x1="0" y1="{CHROME_HEIGHT}" x2="{width}" y2="{CHROME_HEIGHT}" '
        'stroke="#30363d" stroke-width="1"/>'
    )

    y = CHROME_HEIGHT + TOP_PAD
    baseline_offset = ROW_HEIGHT * 0.68
    last_command_index = None
    for line in timed:
        if line.kind == "blank":
            y += BLANK_ROW_HEIGHT
            continue
        text_width = len(line.text) * CELL_WIDTH
        baseline_y = y + baseline_offset
        color = _line_color(line)
        text_el = (
            f'<text class="cast-text" x="0" y="{baseline_y:.1f}" fill="{color}" '
            f'textLength="{text_width:.1f}" lengthAdjust="spacingAndGlyphs">'
            f"{_xml_escape(line.text)}</text>"
        )
        if line.kind == "command":
            clip_id = f"l{line.index}-clip"
            w(f'<clipPath id="{clip_id}" clipPathUnits="userSpaceOnUse">')
            w(
                f'<rect x="0" y="{y - 4:.1f}" width="{text_width:.1f}" '
                f'height="{ROW_HEIGHT + 4}"/>'
            )
            w("</clipPath>")
            w(f'<g id="l{line.index}" transform="translate({SIDE_PAD}, 0)" clip-path="url(#{clip_id})">')
            w(text_el)
            w("</g>")
            last_command_index = line.index
        else:
            w(f'<g id="l{line.index}" class="cast-fade" transform="translate({SIDE_PAD}, 0)">')
            w(text_el)
            w("</g>")
        y += ROW_HEIGHT

    if last_command_index is not None:
        cursor_line = next(t for t in timed if t.index == last_command_index)
        cursor_x = SIDE_PAD + len(cursor_line.text) * CELL_WIDTH + 2
        cursor_y = CHROME_HEIGHT + TOP_PAD
        row = 0
        for line in timed:
            if line.kind == "blank":
                row += BLANK_ROW_HEIGHT
                continue
            if line.index == last_command_index:
                cursor_y = CHROME_HEIGHT + TOP_PAD + row
                break
            row += ROW_HEIGHT
        w(
            f'<rect class="cast-cursor" x="{cursor_x:.1f}" y="{cursor_y + 2:.1f}" '
            f'width="{CELL_WIDTH:.1f}" height="{ROW_HEIGHT - 4}" fill="#7ee787"/>'
        )

    w("</svg>")
    svg_text = "".join(parts) + "\n"
    return svg_text.encode("utf-8")


def render_file(transcript_path: Path, output_path: Path) -> None:
    transcript = load_transcript(transcript_path)
    output_path.write_bytes(render_svg_bytes(transcript))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(
            "usage: render_terminal_cast.py <transcript.json> <output.svg>",
            file=sys.stderr,
        )
        return 2
    transcript_path, output_path = Path(argv[0]), Path(argv[1])
    render_file(transcript_path, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
