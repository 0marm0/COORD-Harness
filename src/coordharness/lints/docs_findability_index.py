#!/usr/bin/env python3
from __future__ import annotations
import re
from pathlib import Path

from coordharness import config as _harness_config

DOCS = _harness_config.project_root() / "docs"
INDEX = DOCS / "guide" / "INDEX.md"
ARCHIVE_INDEX = DOCS / "archive" / "_INDEX.md"
SKIP = {"INDEX.md", "_MANIFEST.md", "_INDEX.md", "README.md"}

LIVE_START = "<!-- AUTO-INDEX:BEGIN findability/anti-loss -->"
ARCHIVE_START = "<!-- AUTO-ARCHIVE-INDEX:BEGIN findability/anti-loss -->"
END = "<!-- AUTO-INDEX:END -->"
ARCHIVE_END = "<!-- AUTO-ARCHIVE-INDEX:END -->"
LEGACY_AUTO_INDEX_RE = re.compile(
    r"<!-- AUTO-INDEX:BEGIN(?: \([^>]*\)| [^>]*)? -->.*?<!-- AUTO-INDEX:END -->",
    re.DOTALL,
)
ARCHIVE_AUTO_INDEX_RE = re.compile(
    r"<!-- AUTO-ARCHIVE-INDEX:BEGIN(?: \([^>]*\)| [^>]*)? -->.*?<!-- AUTO-ARCHIVE-INDEX:END -->",
    re.DOTALL,
)


def _docs(*, archive: bool) -> list[str]:
    out: list[str] = []
    for p in sorted(DOCS.rglob("*.md")):
        rp = p.relative_to(DOCS).as_posix()
        is_archive = "/archive/" in f"/{rp}"
        if is_archive != archive or "/_review/" in f"/{rp}" or p.name in SKIP:
            continue
        out.append(rp)
    return out


def _block(start: str, end: str, title: str, docs: list[str], note: str) -> str:
    lines = [start, title, "", note, ""]
    lines += [f"- {rp}" for rp in docs]
    lines += ["", end]
    return "\n".join(lines)


def _without_generated_block(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub("", text)


def _replace_generated_block(path: Path, block: str, pattern: re.Pattern[str], default_head: str) -> None:
    if path.exists():
        txt = path.read_text(encoding="utf-8")
        txt = _without_generated_block(txt, pattern).rstrip()
        txt = txt + "\n\n" + block + "\n"
    else:
        txt = default_head.rstrip() + "\n\n" + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")


live_docs = _docs(archive=False)
archive_docs = _docs(archive=True)
archive_manual_text = (
    _without_generated_block(ARCHIVE_INDEX.read_text(encoding="utf-8"), ARCHIVE_AUTO_INDEX_RE)
    if ARCHIVE_INDEX.exists()
    else ""
)
archive_manual_basenames = set(re.findall(r"[\w.-]+\.md", archive_manual_text))
archive_missing_docs = [
    rp for rp in archive_docs
    if Path(rp).name not in archive_manual_basenames
]

live_block = _block(
    LIVE_START,
    END,
    "## Complete docs auto-index (findability / anti-loss)",
    live_docs,
    f"_Auto-listed {len(live_docs)} curated live docs so none is orphaned from an index. "
    "Curation lives in _MANIFEST.md; this is the completeness backstop._",
)
archive_block = _block(
    ARCHIVE_START,
    ARCHIVE_END,
    "## Complete archive auto-index (findability / anti-loss)",
    archive_missing_docs,
    f"_Auto-listed {len(archive_missing_docs)} archived docs missing from manual archive summaries. "
    "Manual archive summaries above remain the curated resolver._",
)

_replace_generated_block(
    INDEX,
    live_block,
    LEGACY_AUTO_INDEX_RE,
    "<!-- canonical: docs-index | axis: docs-index | status: live -->\n# Docs Index",
)
_replace_generated_block(
    ARCHIVE_INDEX,
    archive_block,
    ARCHIVE_AUTO_INDEX_RE,
    "<!-- canonical: archive-index | axis: archive-navigation -->\n# Archive Index",
)
print(f"auto-indexed {len(live_docs)} live docs into guide/INDEX.md")
print(f"auto-indexed {len(archive_missing_docs)} archived docs into archive/_INDEX.md")
