from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Mapping

from coordharness import config as _harness_config
from coordharness.knowledge.query_scoring import (
    normalize_search_text as _normalize_search_text,
    query_phrases as _query_phrases,
    query_tokens as _query_tokens,
)

_log = logging.getLogger(__name__)

_EPHEMERAL_ARCHIVE_PART_RE = re.compile(r"^_archive_\d{4}(?:-\d{2})?$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DOCUMENT_PREAMBLE_SLUG = "document-preamble"
_KNOWLEDGE_FTS_COLUMNS = (
    "pointer",
    "title",
    "body",
    "card_kind",
    "doc_pointer",
    "source_path",
    "heading",
    "heading_path",
    "heading_slug",
    "heading_level",
    "section_index",
    "line_start",
    "line_end",
    "line_count",
)
_SOURCE_MANIFEST_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kfts_source_manifest (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _query_mentions(raw: str, phrases: tuple[str, ...]) -> bool:
    normalized = _normalize_search_text(raw or "")
    compact = normalized.replace(" ", "")
    return any(phrase in normalized or phrase.replace(" ", "") in compact for phrase in phrases)


def _fts_query(raw: str) -> str | None:
    tokens = _query_tokens(raw or "")[:32]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)

_REPO_ROOT = _harness_config.project_root()
DEFAULT_INDEX_DB = _harness_config.knowledge_db_path()
_POINTER_PREFIX = "memory://"
REBUILD_INDEX_API = "coordharness.knowledge.kfts.rebuild_index"
_ALT_POINTER_PREFIXES = ("kfts://",)

# Accepted-memory mirrors are derived, non-authoritative inputs under an explicit neutral root.
MEMORY_MIRROR_REL = ".agents/accepted-memory"
_ALLOWED_SOURCE_ROOTS = ("docs", "src", "tests", ".agents", ".claude")
_ALLOWED_ROOT_FILES = ("AGENTS.md", "CLAUDE.md")
DEFAULT_VAULT_GLOBS = (
    "docs/**/*.md",
    "src/**/*.md",
    "tests/**/*.md",
    ".agents/**/*.md",
    ".claude/**/*.md",
    "AGENTS.md",
    "CLAUDE.md",
)

SEARCH_EXCLUDE_CONFIG_REL: str | None = None
_SEARCH_EXCLUDE_CACHE: tuple[str, ...] | None = None


def _contained_repo_or_state_path(raw: str, *, root: Path | None = None) -> Path:
    base = (root or _REPO_ROOT).resolve()
    path = Path(raw).expanduser()
    candidate = path if path.is_absolute() else base / path
    resolved = candidate.resolve(strict=False)
    allowed = (base, _harness_config.state_dir().resolve(strict=False))
    if not any(resolved == allowed_root or allowed_root in resolved.parents for allowed_root in allowed):
        raise ValueError(f"knowledge configuration path escapes project and state roots: {raw!r}")
    return resolved


def _configured_json(
    *, inline_key: str, file_key: str, env: Mapping[str, str] | None = None
) -> object | None:
    source = os.environ if env is None else env
    inline = str(source.get(inline_key) or "").strip()
    file_value = str(source.get(file_key) or "").strip()
    if inline and file_value:
        raise ValueError(f"configure only one of {inline_key} or {file_key}")
    if not inline and not file_value:
        return None
    if file_value:
        inline = _contained_repo_or_state_path(file_value).read_text(encoding="utf-8")
    try:
        return json.loads(inline)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON for {inline_key}: {exc}") from exc


def configured_vault_globs(*, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    parsed = _configured_json(
        inline_key="COORD_KNOWLEDGE_SOURCE_GLOBS_JSON",
        file_key="COORD_KNOWLEDGE_SOURCE_GLOBS_FILE",
        env=env,
    )
    if parsed is None:
        return DEFAULT_VAULT_GLOBS
    if not isinstance(parsed, list):
        raise ValueError("knowledge source globs must be a JSON list")
    out: list[str] = []
    for raw in parsed:
        pattern = str(raw).strip().replace("\\", "/")
        path = Path(pattern)
        first = path.parts[0] if path.parts else ""
        if (
            not pattern
            or path.is_absolute()
            or ".." in path.parts
            or (first not in _ALLOWED_SOURCE_ROOTS and pattern not in _ALLOWED_ROOT_FILES)
        ):
            raise ValueError(f"knowledge source glob escapes allowed roots: {raw!r}")
        out.append(pattern)
    return tuple(dict.fromkeys(out))


def load_search_excludes(*, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    global _SEARCH_EXCLUDE_CACHE
    if env is None and _SEARCH_EXCLUDE_CACHE is not None:
        return _SEARCH_EXCLUDE_CACHE
    parsed = _configured_json(
        inline_key="COORD_KFTS_SEARCH_EXCLUDES_JSON",
        file_key="COORD_KFTS_SEARCH_EXCLUDES_FILE",
        env=env,
    )
    if parsed is None:
        result: tuple[str, ...] = ()
    else:
        paths = parsed.get("exclude_paths") if isinstance(parsed, dict) else parsed
        if not isinstance(paths, list):
            raise ValueError("KFTS search excludes must be a list or an object with exclude_paths")
        result = tuple(str(path).strip() for path in paths if str(path).strip())
    if env is None:
        _SEARCH_EXCLUDE_CACHE = result
    return result


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DEFAULT_INDEX_DB
    rp = str(p.resolve())
    from coordharness.coord.config import _WAREHOUSE_MARKERS

    for marker in _WAREHOUSE_MARKERS:
        if f"/{marker}/" in rp:
            raise RuntimeError(f"knowledge index {rp!r} must stay outside the warehouse")
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    _ensure_knowledge_fts_schema(c)
    return c


def _conn_ro(db_path: Path | str | None = None) -> sqlite3.Connection | None:
    p = Path(db_path) if db_path else DEFAULT_INDEX_DB
    rp = str(p.resolve())
    from coordharness.coord.config import _WAREHOUSE_MARKERS

    for marker in _WAREHOUSE_MARKERS:
        if f"/{marker}/" in rp:
            raise RuntimeError(f"knowledge index {rp!r} must stay outside the warehouse")
    if not p.exists() or not _knowledge_fts_schema_current(p):
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.execute("PRAGMA query_only = ON")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _ensure_knowledge_fts_schema(c: sqlite3.Connection) -> None:
    columns = _knowledge_fts_columns(c)
    if columns and not set(_KNOWLEDGE_FTS_COLUMNS).issubset(columns):
        c.execute("DROP TABLE knowledge_fts")
        columns = set()
    if not columns:
        c.execute(
            "CREATE VIRTUAL TABLE knowledge_fts USING fts5("
            " pointer UNINDEXED, title, body,"
            " card_kind UNINDEXED, doc_pointer UNINDEXED, source_path UNINDEXED,"
            " heading UNINDEXED, heading_path UNINDEXED, heading_slug UNINDEXED,"
            " heading_level UNINDEXED, section_index UNINDEXED,"
            " line_start UNINDEXED, line_end UNINDEXED, line_count UNINDEXED,"
            " tokenize='porter unicode61')"
        )


def _ensure_source_manifest_schema(c: sqlite3.Connection) -> None:
    c.execute(_SOURCE_MANIFEST_TABLE_SQL)


def _knowledge_fts_columns(c: sqlite3.Connection) -> set[str]:
    try:
        rows = c.execute("PRAGMA table_info(knowledge_fts)").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row[1]) for row in rows}


def _knowledge_fts_schema_current(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path)) as c:
            columns = _knowledge_fts_columns(c)
    except sqlite3.Error:
        return False
    return bool(columns) and set(_KNOWLEDGE_FTS_COLUMNS).issubset(columns)


def _knowledge_fts_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(db_path)) as c:
            return int(c.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0])
    except sqlite3.Error:
        return 0


def _pointer_for(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        rel = path
    return f"{_POINTER_PREFIX}{rel}"


def _section_pointer(doc_pointer: str, slug: str) -> str:
    return f"{doc_pointer}#{slug}"


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:120]
    return fallback


def _slugify_heading(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_search_text(value or "")).strip("-")
    return slug or "section"


def _split_pointer_fragment(pointer: str) -> tuple[str, str | None]:
    if "#" not in pointer:
        return pointer, None
    base, fragment = pointer.split("#", 1)
    return base, fragment or None


def _section_cards(text: str, *, doc_pointer: str, doc_title: str) -> list[dict[str, object]]:
    lines = text.splitlines()
    headings: list[dict[str, object]] = []
    slug_counts: dict[str, int] = {}
    stack: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        level = len(match.group(1))
        heading = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        if level == 1:
            continue
        base_slug = _slugify_heading(heading)
        count = slug_counts.get(base_slug, 0) + 1
        slug_counts[base_slug] = count
        slug = base_slug if count == 1 else f"{base_slug}-{count}"
        headings.append(
            {
                "line_index": idx,
                "level": level,
                "heading": heading,
                "heading_path": " > ".join(value for _, value in stack),
                "slug": slug,
            }
        )

    cards: list[dict[str, object]] = []
    if headings:
        preamble_end = int(headings[0]["line_index"])
        preamble_body = "\n".join(lines[:preamble_end]).strip()
        if preamble_body:
            used_slugs = {str(heading["slug"]) for heading in headings}
            preamble_slug = _DOCUMENT_PREAMBLE_SLUG
            suffix = 2
            while preamble_slug in used_slugs:
                preamble_slug = f"{_DOCUMENT_PREAMBLE_SLUG}-{suffix}"
                suffix += 1
            cards.append(
                {
                    "pointer": _section_pointer(doc_pointer, preamble_slug),
                    "title": f"{doc_title} > Document preamble"[:240],
                    "body": preamble_body,
                    "card_kind": "section",
                    "doc_pointer": doc_pointer,
                    "source_path": (
                        doc_pointer[len(_POINTER_PREFIX) :]
                        if doc_pointer.startswith(_POINTER_PREFIX)
                        else doc_pointer
                    ),
                    "heading": "Document preamble",
                    "heading_path": "Document preamble",
                    "heading_slug": preamble_slug,
                    "heading_level": 0,
                    "section_index": 0,
                    "line_start": 1,
                    "line_end": preamble_end,
                    "line_count": preamble_end,
                }
            )

    section_index_offset = len(cards)
    for heading_position, heading in enumerate(headings):
        section_index = heading_position + section_index_offset
        start = int(heading["line_index"])
        level = int(heading["level"])
        end = len(lines)
        for next_heading in headings[heading_position + 1 :]:
            if int(next_heading["level"]) <= level:
                end = int(next_heading["line_index"])
                break
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        heading_text = str(heading["heading"])
        heading_path = str(heading["heading_path"])
        slug = str(heading["slug"])
        cards.append(
            {
                "pointer": _section_pointer(doc_pointer, slug),
                "title": f"{doc_title} > {heading_path}"[:240],
                "body": body,
                "card_kind": "section",
                "doc_pointer": doc_pointer,
                "source_path": doc_pointer[len(_POINTER_PREFIX) :] if doc_pointer.startswith(_POINTER_PREFIX) else doc_pointer,
                "heading": heading_text,
                "heading_path": heading_path,
                "heading_slug": slug,
                "heading_level": level,
                "section_index": section_index,
                "line_start": start + 1,
                "line_end": end,
                "line_count": max(0, end - start),
            }
        )
    return cards


def _cards_for_file(path: Path, text: str) -> list[dict[str, object]]:
    doc_pointer = _pointer_for(path)
    doc_title = _title_of(text, path.name)
    source_path = doc_pointer[len(_POINTER_PREFIX) :] if doc_pointer.startswith(_POINTER_PREFIX) else doc_pointer
    section_cards = _section_cards(text, doc_pointer=doc_pointer, doc_title=doc_title)
    doc_body = "" if section_cards else text
    cards: list[dict[str, object]] = [
        {
            "pointer": doc_pointer,
            "title": doc_title,
            "body": doc_body,
            "card_kind": "document",
            "doc_pointer": doc_pointer,
            "source_path": source_path,
            "heading": "",
            "heading_path": "",
            "heading_slug": "",
            "heading_level": 0,
            "section_index": -1,
            "line_start": 1 if text.splitlines() else 0,
            "line_end": len(text.splitlines()),
            "line_count": len(text.splitlines()),
        }
    ]
    cards.extend(section_cards)
    return cards


def _iter_vault_files(
    vault_globs: tuple[str, ...] | None = None,
    exclude_substrings: tuple[str, ...] | None = None,
):
    vault_globs = configured_vault_globs() if vault_globs is None else tuple(vault_globs)
    if exclude_substrings is None:
        exclude_substrings = load_search_excludes()
    for pattern in vault_globs:
        for path in _REPO_ROOT.glob(pattern):
            try:
                path.resolve().relative_to(_REPO_ROOT.resolve())
            except ValueError:
                continue
            if not path.is_file():
                continue
            if any(_EPHEMERAL_ARCHIVE_PART_RE.match(part) for part in path.parts):
                continue
            sp = str(path)
            if exclude_substrings and any(x in sp for x in exclude_substrings):
                continue
            yield path


def _index_mtime(db_path: Path) -> float:
    mtimes: list[float] = []
    for path in (db_path, Path(str(db_path) + "-wal")):
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.name.endswith("-wal") and stat.st_size == 0:
            continue
        mtimes.append(stat.st_mtime)
    return max(mtimes) if mtimes else 0.0


def _newest_source_mtime(vault_globs: tuple[str, ...] | None = None) -> float:
    newest = 0.0
    for path in _iter_vault_files(vault_globs=vault_globs):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    return newest


def _source_snapshot(vault_globs: tuple[str, ...] | None = None) -> tuple[int, float]:
    count = 0
    newest = 0.0
    for path in _iter_vault_files(vault_globs=vault_globs):
        count += 1
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    return count, newest


def _write_source_manifest(c: sqlite3.Connection, *, source_count: int, newest_source_mtime: float) -> None:
    _ensure_source_manifest_schema(c)
    rows = {
        "source_file_count": str(int(source_count)),
        "newest_source_mtime": repr(float(newest_source_mtime)),
        "updated_at": repr(time.time()),
    }
    c.executemany(
        "INSERT INTO kfts_source_manifest(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        tuple(rows.items()),
    )


def _source_snapshot_from_manifest(db_path: Path) -> tuple[int, float] | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as c:
            rows = dict(c.execute("SELECT key, value FROM kfts_source_manifest").fetchall())
    except sqlite3.Error:
        return None
    try:
        return int(rows["source_file_count"]), float(rows["newest_source_mtime"])
    except (KeyError, TypeError, ValueError):
        return None


def _source_file_count(vault_globs: tuple[str, ...] | None = None) -> int:
    return _source_snapshot(vault_globs=vault_globs)[0]


def _manifest_updated_at(db_path: Path | str | None = None) -> float | None:
    p = Path(db_path) if db_path else DEFAULT_INDEX_DB
    if not p.exists():
        return None
    try:
        with sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True) as c:
            row = c.execute(
                "SELECT value FROM kfts_source_manifest WHERE key = 'updated_at'"
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _indexed_source_path_count(db_path: Path | str | None = None) -> int:
    if not _index_mtime(Path(db_path) if db_path else DEFAULT_INDEX_DB):
        return 0
    c = _conn_ro(db_path)
    if c is None:
        return 0
    try:
        row = c.execute("SELECT COUNT(DISTINCT source_path) FROM knowledge_fts").fetchone()
        return int(row[0] or 0)
    finally:
        c.close()


def ensure_fresh_index(db_path: Path | str | None = None) -> bool:
    db = Path(db_path) if db_path else DEFAULT_INDEX_DB
    if _index_mtime(db) and not _knowledge_fts_schema_current(db):
        rebuild_index(db_path=db)
        return True
    newest_index = _index_mtime(db)
    newest_source = _newest_source_mtime()
    if newest_source and newest_index and _knowledge_fts_count(db) == 0:
        rebuild_index(db_path=db)
        return True
    if newest_index and _indexed_source_path_count(db) != _source_file_count():
        rebuild_index(db_path=db)
        return True
    if newest_index and (not newest_source or newest_source <= newest_index):
        return False
    rebuild_index(db_path=db)
    return True


def _source_tier(pointer: str) -> int:
    rel = pointer[len(_POINTER_PREFIX):] if pointer.startswith(_POINTER_PREFIX) else pointer
    rel = rel.replace("\\", "/").lower()
    if rel in {name.lower() for name in _ALLOWED_ROOT_FILES}:
        return 0
    if rel.startswith("docs/archive/"):
        return 5
    if rel.startswith("docs/_review/"):
        return 4
    if rel.startswith("docs/"):
        return 0
    if rel.startswith("src/"):
        return 1
    if rel.startswith(("tests/", ".agents/", ".claude/")):
        return 2
    return 3


def _rank_quality(query: str, pointer: str, title: str) -> int:
    tokens = _query_tokens(query or "")
    phrase = " ".join(tokens)
    phrases = _query_phrases(query or "")
    title_l = _normalize_search_text(title or "")
    pointer_l = _normalize_search_text(pointer)
    stem_l = _normalize_search_text(Path(pointer).stem)
    basename_l = _normalize_search_text(Path(pointer).name)
    if phrase and phrase in {stem_l, basename_l}:
        return 0
    if phrase and phrase in title_l:
        return 1
    if phrase and phrase in pointer_l:
        return 2
    if _query_mentions(query, ("codegraph", "code graph", "contextgraph", "context graph")) and any(
        p in title_l or p in pointer_l or p in stem_l
        for p in ("context graph", "code graph", "contextgraph", "codegraph")
    ):
        return 1
    if _query_mentions(query, ("runjournal", "run journal", "runeventstore", "run event store")) and any(
        p in title_l or p in pointer_l or p in stem_l
        for p in ("run journal", "run event store", "runjournal", "runeventstore")
    ):
        return 1
    if any(p and p in title_l for p in phrases):
        return 2
    if any(p and (p in pointer_l or p in stem_l or p in basename_l) for p in phrases):
        return 3
    if tokens and all(t in title_l for t in tokens):
        return 4
    if tokens and all(t in pointer_l for t in tokens):
        return 5
    return 6


def _match_metadata(query: str, *values: object) -> tuple[list[str], float]:
    tokens = list(dict.fromkeys(_query_tokens(query or "")))[:12]
    if not tokens:
        return [], 0.0
    haystack = _normalize_search_text(" ".join(str(value or "") for value in values))
    matched = [token for token in tokens if token in haystack][:8]
    return matched, round(len(matched) / max(1, len(tokens)), 3)


def _stale_source_penalty(pointer: str) -> int:
    return 1 if _source_tier(pointer) >= 4 else 0


def _card_kind_rank(card_kind: str) -> int:
    return 0 if card_kind == "section" else 1


def rebuild_index(db_path: Path | str | None = None,
                  vault_globs: tuple[str, ...] | None = None,
                  exclude_substrings: tuple[str, ...] | None = None) -> dict:
    c = _conn(db_path)
    try:
        c.execute("DELETE FROM knowledge_fts")
        n = 0
        source_count = 0
        newest_source = 0.0
        for path in _iter_vault_files(vault_globs=vault_globs, exclude_substrings=exclude_substrings):
            source_count += 1
            try:
                newest_source = max(newest_source, path.stat().st_mtime)
            except OSError:
                pass
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            for card in _cards_for_file(path, text):
                c.execute(
                    "INSERT INTO knowledge_fts("
                    "pointer, title, body, card_kind, doc_pointer, source_path,"
                    "heading, heading_path, heading_slug, heading_level, section_index"
                    ", line_start, line_end, line_count"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        card["pointer"],
                        card["title"],
                        card["body"],
                        card["card_kind"],
                        card["doc_pointer"],
                        card["source_path"],
                        card["heading"],
                        card["heading_path"],
                        card["heading_slug"],
                        card["heading_level"],
                        card["section_index"],
                        card["line_start"],
                        card["line_end"],
                        card["line_count"],
                    ),
                )
                n += 1
        _write_source_manifest(c, source_count=source_count, newest_source_mtime=newest_source)
        c.commit()
        return {"indexed": n, "cards": n, "db": str(Path(db_path or DEFAULT_INDEX_DB).resolve())}
    finally:
        c.close()


def find_similar_memory(
    topic: str,
    *,
    limit: int = 5,
    min_coverage: float = 0.5,
    db_path: Path | str | None = None,
) -> list[dict]:
    fq = _fts_query(topic)
    if not fq:
        return []
    c = _conn_ro(db_path)
    if c is None:
        return []
    try:
        rows = c.execute(
            "SELECT pointer, title,"
            " snippet(knowledge_fts, 2, '[', ']', ' … ', 14) AS snippet, source_path"
            " FROM knowledge_fts"
            " WHERE knowledge_fts MATCH ? AND source_path LIKE ?"
            " ORDER BY rank LIMIT ?",
            (fq, MEMORY_MIRROR_REL + "/%", max(limit * 6, 30)),
        ).fetchall()
    finally:
        c.close()
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        pointer, title, snippet, sp = r[0], r[1], r[2], str(r[3] or "")
        slug = Path(sp).name[:-3] if sp.endswith(".md") else Path(sp).stem
        if slug in seen or slug.upper() == "MEMORY":
            continue
        _matched, cov = _match_metadata(topic, pointer, title, snippet)
        if cov < min_coverage:
            continue
        seen.add(slug)
        out.append({
            "slug": slug,
            "pointer": pointer,
            "title": title,
            "term_coverage": round(float(cov), 3),
            "snippet": snippet,
            "source_path": sp,
            "authority": "derived_non_authoritative",
        })
        if len(out) >= limit:
            break
    return out


def _search_general(query: str, db_path: Path | str | None = None, limit: int = 8) -> list[dict]:
    fq = _fts_query(query)
    if not fq:
        return []
    limit = max(1, int(limit))
    candidate_limit = max(limit * 8, 40)
    tokens = _query_tokens(query or "")
    phrase = " ".join(tokens)
    c = _conn_ro(db_path)
    if c is None:
        return []
    try:
        fts_rows = c.execute(
            "SELECT pointer, title,"
            " snippet(knowledge_fts, 2, '[', ']', ' … ', 14) AS snippet,"
            " rank, card_kind, doc_pointer, source_path, heading, heading_path,"
            " heading_slug, heading_level, section_index, line_start, line_end, line_count"
            " FROM knowledge_fts WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
            (fq, candidate_limit)).fetchall()
        exact_rows = []
        if phrase:
            like = f"%{phrase}%"
            exact_rows = c.execute(
                "SELECT pointer, title, substr(body, 1, 220) AS snippet, 0.0 AS rank,"
                " card_kind, doc_pointer, source_path, heading, heading_path,"
                " heading_slug, heading_level, section_index, line_start, line_end, line_count"
                " FROM knowledge_fts"
                " WHERE lower(replace(replace(replace(replace(title, '_', ' '), '-', ' '), '/', ' '), '.', ' ')) LIKE ?"
                " OR lower(replace(replace(replace(replace(pointer, '_', ' '), '-', ' '), '/', ' '), '.', ' ')) LIKE ?"
                " LIMIT ?",
                (like, like, candidate_limit),
            ).fetchall()
        by_pointer = {str(r[0]): r for r in fts_rows}
        for row in exact_rows:
            by_pointer.setdefault(str(row[0]), row)
        rows = list(by_pointer.values())
        ranked = sorted(
            rows,
            key=lambda r: (
                _stale_source_penalty(str(r[0])),
                _source_tier(str(r[0])),
                _rank_quality(query, str(r[0]), str(r[1] or "")),
                _card_kind_rank(str(r[4] or "")),
                float(r[3] or 0.0),
                str(r[0]),
            ),
        )
        out = []
        for r in ranked[:limit]:
            matched_terms, term_coverage = _match_metadata(query, r[0], r[1], r[2])
            tier = _source_tier(str(r[0]))
            out.append({
                "pointer": r[0],
                "title": r[1],
                "snippet": r[2],
                "matched_terms": matched_terms,
                "term_coverage": term_coverage,
                "card_kind": r[4],
                "doc_pointer": r[5],
                "source_path": r[6],
                "heading": r[7],
                "heading_path": r[8],
                "heading_slug": r[9],
                "heading_level": int(r[10] or 0),
                "section_index": int(r[11] or 0),
                "line_start": int(r[12] or 0),
                "line_end": int(r[13] or 0),
                "line_count": int(r[14] or 0),
                "source_tier": tier,
                "stale_source": tier >= 4,
                "freshness_basis": "kfts_index_manifest",
            })
        return out
    finally:
        c.close()


MAX_SEARCH_RESULTS = 24
MAX_SEARCH_RESPONSE_BYTES = 32_768


def _memory_intent(query: str) -> bool:

    ordered_tokens = tuple(_query_tokens(query or ""))
    tokens = set(ordered_tokens)
    personal = bool(tokens & {"operator", "my", "our", "ours", "we", "personal", "user"})
    if "remember" in tokens and (personal or (ordered_tokens and ordered_tokens[0] == "remember")):
        return True
    if tokens & {"preference", "preferences", "taste"} and personal:
        return True
    if "memory" in tokens and (
        personal
        or bool(
            tokens
            & {
                "accepted",
                "agent",
                "context",
                "coordination",
                "harness",
                "note",
                "notes",
                "project",
                "protocol",
                "retrieval",
                "shared",
                "system",
            }
        )
    ):
        return True
    return personal and bool(
        tokens & {"instruction", "instructions", "ruling", "rulings", "decision", "decisions"}
    )


def _memory_scoped_search(
    query: str,
    *,
    db_path: Path | str | None,
    limit: int,
) -> list[dict]:
    rows = find_similar_memory(
        query,
        limit=max(1, int(limit)),
        min_coverage=0.25,
        db_path=db_path,
    )
    out: list[dict] = []
    for row in rows:
        pointer = str(row.get("pointer") or "")
        source_path = str(row.get("source_path") or "")
        out.append(
            {
                "pointer": pointer,
                "title": row.get("title"),
                "snippet": row.get("snippet"),
                "matched_terms": _match_metadata(
                    query, pointer, row.get("title"), row.get("snippet")
                )[0],
                "term_coverage": row.get("term_coverage"),
                "card_kind": "document",
                "doc_pointer": pointer.split("#", 1)[0],
                "source_path": source_path,
                "heading": None,
                "heading_path": None,
                "heading_slug": None,
                "heading_level": 0,
                "section_index": 0,
                "line_start": 0,
                "line_end": 0,
                "line_count": 0,
                "source_tier": _source_tier(pointer),
                "stale_source": False,
                "freshness_basis": "kfts_index_manifest",
                "retrieval_route": "memory_scoped_explicit_intent",
            }
        )
    return out


def _response_bytes(rows: list[dict]) -> int:
    return len(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def _apply_response_caps(rows: list[dict], *, limit: int) -> list[dict]:
    selected: list[dict] = []
    for row in rows:
        if len(selected) >= min(MAX_SEARCH_RESULTS, max(1, int(limit))):
            break
        candidate = [*selected, row]
        if _response_bytes(candidate) > MAX_SEARCH_RESPONSE_BYTES:
            continue
        selected.append(row)
    return selected


def _distinct_sources(rows: list[dict]) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        source_path = str(row.get("source_path") or "")
        identity = source_path or str(row.get("doc_pointer") or row.get("pointer") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        selected.append(row)
    return selected


def _is_shadow_v2_db(db_path: Path | str | None) -> bool:
    if db_path is None:
        return False
    path = Path(db_path)
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True) as conn:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        return {"sources", "cards", "cards_fts", "schema_meta"} <= names
    except sqlite3.Error:
        return False


def _search_shadow_v2(
    query: str, *, db_path: Path | str, limit: int
) -> list[dict]:
    fts = _fts_query(query)
    if not fts:
        return []
    path = Path(db_path).resolve()
    sql = """
        SELECT c.pointer,c.title,snippet(cards_fts,1,'[',']',' … ',18),
               c.heading,c.heading_path,c.heading_level,c.section_index,
               c.line_start,c.line_end,s.logical_path,s.resolved_path,s.plane,
               s.module,s.lifecycle,s.canonical_disposition,bm25(cards_fts)
        FROM sources s
        JOIN cards c ON c.source_id=s.source_id
        JOIN cards_fts ON cards_fts.rowid=c.card_id
        WHERE s.plane='shared' AND s.lifecycle='current'
          AND s.instruction_effect IN ('binding','advisory','non_instructional')
          AND s.canonical_disposition IN ('canonical_current','reference')
          AND s.classification_state='complete'
          AND s.sensitivity NOT IN ('secret','pii','secret_and_pii')
          AND EXISTS(
              SELECT 1 FROM json_each(s.actor_allowlist_json)
              WHERE json_each.value IN ('codex','*')
          )
          AND cards_fts MATCH ?
        ORDER BY bm25(cards_fts),c.pointer
        LIMIT ?
    """
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (fts, max(40, int(limit) * 8))).fetchall()
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        resolved = Path(str(row["resolved_path"])).resolve()
        try:
            rel = resolved.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            continue
        key = (str(row["logical_path"]), int(row["section_index"]))
        if key in seen:
            continue
        seen.add(key)
        fragment = str(row["pointer"]).split("#", 1)[1] if "#" in str(row["pointer"]) else ""
        pointer = f"memory://{rel}" + (f"#{fragment}" if fragment else "")
        out.append({
            "pointer": pointer,
            "title": row["title"],
            "snippet": row[2],
            "card_kind": "section",
            "doc_pointer": pointer.split("#", 1)[0],
            "source_path": rel,
            "heading": row["heading"],
            "heading_path": row["heading_path"],
            "heading_slug": fragment,
            "heading_level": int(row["heading_level"] or 0),
            "section_index": int(row["section_index"] or 0),
            "line_start": int(row["line_start"] or 0),
            "line_end": int(row["line_end"] or 0),
            "line_count": max(0, int(row["line_end"] or 0) - int(row["line_start"] or 0) + 1),
            "source_tier": _source_tier(f"memory://{row['logical_path']}"),
            "stale_source": False,
            "freshness_basis": "immutable_kfts_v2r_snapshot",
            "plane": row["plane"],
            "module": row["module"],
            "lifecycle": row["lifecycle"],
            "canonical_disposition": row["canonical_disposition"],
        })
        if len(out) >= limit:
            break
    return out


def search(query: str, db_path: Path | str | None = None, limit: int = 8) -> list[dict]:

    bounded_limit = min(MAX_SEARCH_RESULTS, max(1, int(limit)))
    if _is_shadow_v2_db(db_path):
        return _apply_response_caps(
            _search_shadow_v2(query, db_path=db_path, limit=bounded_limit),
            limit=bounded_limit,
        )
    if not _memory_intent(query):
        general = _search_general(query, db_path=db_path, limit=bounded_limit)
        return _apply_response_caps(general, limit=bounded_limit)
    memory_limit = min(bounded_limit, max(1, (bounded_limit + 1) // 2))
    memory = _distinct_sources(
        _memory_scoped_search(
            query,
            db_path=db_path,
            limit=memory_limit,
        )
    )
    if not memory:
        general = _search_general(query, db_path=db_path, limit=bounded_limit)
        return _apply_response_caps(general, limit=bounded_limit)
    general = _search_general(
        query,
        db_path=db_path,
        limit=max(40, bounded_limit * 8),
    )
    documentary = _distinct_sources(
        [
            row
            for row in general
            if not str(row.get("source_path") or "").startswith(MEMORY_MIRROR_REL + "/")
        ]
    )
    memory = memory[:memory_limit]
    documentary_needed = max(0, bounded_limit - len(memory))
    selected = [*memory, *documentary[:documentary_needed]]
    return _apply_response_caps(selected, limit=bounded_limit)


def _normalize_pointer(pointer: str) -> str:
    for alt in _ALT_POINTER_PREFIXES:
        if pointer.startswith(alt):
            return _POINTER_PREFIX + pointer[len(alt):]
    return pointer


DEFAULT_READ_NOTE_BYTES = 12_000


def _indexed_section(pointer: str, db_path: Path | str | None = None) -> dict[str, object] | None:
    c = _conn_ro(db_path)
    if c is None:
        return None
    columns = (
        "pointer",
        "doc_pointer",
        "heading",
        "heading_path",
        "heading_slug",
        "heading_level",
        "section_index",
        "line_start",
        "line_end",
        "line_count",
    )
    try:
        row = c.execute(
            "SELECT "
            + ", ".join(columns)
            + " FROM knowledge_fts WHERE pointer=? AND card_kind='section' LIMIT 1",
            (pointer,),
        ).fetchone()
        return dict(zip(columns, row)) if row else None
    finally:
        c.close()


def _read_line_range(path: Path, *, line_start: int, line_end: int) -> str:
    if line_start <= 0 or line_end <= 0 or line_end < line_start:
        return ""
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no < line_start:
                continue
            if line_no > line_end:
                break
            lines.append(line.rstrip("\n"))
    return "\n".join(lines).strip()


def _index_fresh_for_source(path: Path, db_path: Path | str | None = None) -> bool:
    db = Path(db_path) if db_path else DEFAULT_INDEX_DB
    try:
        return db.exists() and path.stat().st_mtime <= db.stat().st_mtime
    except OSError:
        return False


def read_note(pointer: str, max_bytes: int = DEFAULT_READ_NOTE_BYTES, db_path: Path | str | None = None) -> dict:
    pointer = _normalize_pointer(pointer)
    if not pointer.startswith(_POINTER_PREFIX):
        raise ValueError(f"not a knowledge pointer: {pointer!r}")
    doc_pointer, fragment = _split_pointer_fragment(pointer)
    rel = doc_pointer[len(_POINTER_PREFIX):]
    path = (_REPO_ROOT / rel).resolve()
    if _REPO_ROOT not in path.parents and path != _REPO_ROOT:
        raise ValueError("pointer escapes the repo root")
    if not path.is_file():
        return {"pointer": pointer, "exists": False, "body": None}
    if fragment and _index_fresh_for_source(path, db_path=db_path):
        indexed = _indexed_section(pointer, db_path=db_path)
        if indexed:
            section_body = _read_line_range(
                path,
                line_start=int(indexed.get("line_start") or 0),
                line_end=int(indexed.get("line_end") or 0),
            )
            bounded = _bounded_text_bytes(section_body, max_bytes)
            return {
                "pointer": pointer,
                "doc_pointer": doc_pointer,
                "exists": True,
                "doc_exists": True,
                "section_found": True,
                "truncated": len(section_body.encode("utf-8")) > max_bytes,
                "body": bounded,
                "heading": indexed.get("heading"),
                "heading_path": indexed.get("heading_path"),
                "heading_slug": indexed.get("heading_slug"),
                "heading_level": int(indexed.get("heading_level") or 0),
                "section_index": int(indexed.get("section_index") or 0),
                "line_start": int(indexed.get("line_start") or 0),
                "line_end": int(indexed.get("line_end") or 0),
                "line_count": int(indexed.get("line_count") or 0),
                "read_basis": "indexed_line_range",
            }
    body = path.read_text(errors="ignore")
    if fragment:
        for section in _section_cards(body, doc_pointer=doc_pointer, doc_title=_title_of(body, path.name)):
            if section["heading_slug"] == fragment:
                section_body = str(section["body"])
                bounded = _bounded_text_bytes(section_body, max_bytes)
                return {
                    "pointer": pointer,
                    "doc_pointer": doc_pointer,
                    "exists": True,
                    "doc_exists": True,
                    "section_found": True,
                    "truncated": len(section_body.encode("utf-8")) > max_bytes,
                    "body": bounded,
                    "heading": section["heading"],
                    "heading_path": section["heading_path"],
                    "heading_slug": section["heading_slug"],
                    "heading_level": section["heading_level"],
                    "section_index": section["section_index"],
                    "line_start": section["line_start"],
                    "line_end": section["line_end"],
                    "line_count": section["line_count"],
                }
        return {
            "pointer": pointer,
            "doc_pointer": doc_pointer,
            "exists": False,
            "doc_exists": True,
            "section_found": False,
            "body": None,
            "reason": "section_not_found",
        }
    lines = body.splitlines()
    return {
        "pointer": pointer,
        "exists": True,
        "truncated": len(body.encode("utf-8")) > max_bytes,
        "body": _bounded_text_bytes(body, max_bytes),
        "line_start": 1 if lines else 0,
        "line_end": len(lines),
        "line_count": len(lines),
    }


def _bounded_text_bytes(value: str, max_bytes: int) -> str:
    max_bytes = max(1, int(max_bytes))
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _indexed_neighbor_sections(
    pointer: str,
    *,
    radius: int,
    max_snippet_bytes: int,
    db_path: Path | str | None = None,
) -> list[dict[str, object]] | None:
    indexed = _indexed_section(pointer, db_path=db_path)
    if not indexed:
        return None
    doc_pointer = str(indexed.get("doc_pointer") or "")
    section_index = int(indexed.get("section_index") or 0)
    if not doc_pointer or section_index <= 0:
        return None

    rel = doc_pointer[len(_POINTER_PREFIX):] if doc_pointer.startswith(_POINTER_PREFIX) else ""
    path = (_REPO_ROOT / rel).resolve()
    if (_REPO_ROOT not in path.parents and path != _REPO_ROOT) or not path.is_file():
        return None
    if not _index_fresh_for_source(path, db_path=db_path):
        return None

    c = _conn_ro(db_path)
    if c is None:
        return None
    columns = (
        "pointer",
        "heading",
        "heading_path",
        "heading_slug",
        "heading_level",
        "section_index",
        "line_start",
        "line_end",
        "line_count",
    )
    try:
        rows = c.execute(
            "SELECT "
            + ", ".join(columns)
            + " FROM knowledge_fts"
            " WHERE doc_pointer=? AND card_kind='section'"
            " AND section_index BETWEEN ? AND ?"
            " AND section_index != ?"
            " ORDER BY section_index",
            (doc_pointer, section_index - radius, section_index + radius, section_index),
        ).fetchall()
    finally:
        c.close()
    if not rows:
        return []

    neighbors: list[dict[str, object]] = []
    for row in rows:
        section = dict(zip(columns, row))
        idx = int(section.get("section_index") or 0)
        relation = "previous" if idx < section_index else "next"
        body = _read_line_range(
            path,
            line_start=int(section.get("line_start") or 0),
            line_end=int(section.get("line_end") or 0),
        )
        section["body"] = body
        neighbors.append(_neighbor_payload(section, relation=relation, max_snippet_bytes=max_snippet_bytes))
    return sorted(
        neighbors,
        key=lambda item: (
            0 if item.get("relation") == "previous" else 1,
            int(item.get("section_index") or 0),
        ),
    )


def neighbor_sections(
    pointer: str,
    *,
    radius: int = 1,
    max_snippet_bytes: int = 220,
    db_path: Path | str | None = None,
) -> list[dict[str, object]]:
    pointer = _normalize_pointer(pointer)
    if not pointer.startswith(_POINTER_PREFIX):
        return []
    doc_pointer, fragment = _split_pointer_fragment(pointer)
    if not fragment:
        return []
    radius = max(0, int(radius))
    if radius == 0:
        return []
    max_snippet_bytes = max(1, int(max_snippet_bytes))

    indexed_neighbors = _indexed_neighbor_sections(
        pointer,
        radius=radius,
        max_snippet_bytes=max_snippet_bytes,
        db_path=db_path,
    )
    if indexed_neighbors is not None:
        return indexed_neighbors

    rel = doc_pointer[len(_POINTER_PREFIX):]
    path = (_REPO_ROOT / rel).resolve()
    if _REPO_ROOT not in path.parents and path != _REPO_ROOT:
        return []
    if not path.is_file():
        return []
    body = path.read_text(errors="ignore")
    sections = _section_cards(body, doc_pointer=doc_pointer, doc_title=_title_of(body, path.name))
    index = next((idx for idx, section in enumerate(sections) if section.get("heading_slug") == fragment), None)
    if index is None:
        return []
    neighbors: list[dict[str, object]] = []
    for offset in range(radius, 0, -1):
        previous_idx = index - offset
        if previous_idx >= 0:
            neighbors.append(
                _neighbor_payload(
                    sections[previous_idx],
                    relation="previous",
                    max_snippet_bytes=max_snippet_bytes,
                )
            )
    for offset in range(1, radius + 1):
        next_idx = index + offset
        if next_idx < len(sections):
            neighbors.append(
                _neighbor_payload(
                    sections[next_idx],
                    relation="next",
                    max_snippet_bytes=max_snippet_bytes,
                )
            )
    return neighbors


def _neighbor_payload(section: dict[str, object], *, relation: str, max_snippet_bytes: int) -> dict[str, object]:
    pointer = str(section.get("pointer") or "")
    snippet = _bounded_text_bytes(" ".join(str(section.get("body") or "").split()), max_snippet_bytes)
    return {
        "relation": relation,
        "pointer": pointer,
        "heading": section.get("heading") or "",
        "heading_path": section.get("heading_path") or "",
        "heading_slug": section.get("heading_slug") or "",
        "heading_level": int(section.get("heading_level") or 0),
        "section_index": int(section.get("section_index") or 0),
        "line_start": int(section.get("line_start") or 0),
        "line_end": int(section.get("line_end") or 0),
        "line_count": int(section.get("line_count") or 0),
        "snippet": snippet,
        "expand_api": "coordharness.knowledge.context_federator.read_context_pointer",
        "expand_pointer": pointer,
        "max_bytes": 12000,
    }


def index_stats(db_path: Path | str | None = None, *, use_manifest: bool = False, scan_fallback: bool = True) -> dict:
    db = Path(db_path) if db_path else DEFAULT_INDEX_DB
    manifest_snapshot = _source_snapshot_from_manifest(db) if use_manifest else None
    if manifest_snapshot is not None:
        source_count, newest_source = manifest_snapshot
        freshness_basis = "source_manifest"
    elif use_manifest and not scan_fallback:
        source_count, newest_source = 0, None
        freshness_basis = "source_manifest_missing"
    else:
        source_count, newest_source = _source_snapshot()
        freshness_basis = "source_scan"
    newest_index = _index_mtime(db)
    schema_current = _knowledge_fts_schema_current(db) if newest_index else False
    c = _conn_ro(db_path)
    documents = 0
    indexed_source_count = 0
    if c is not None:
        try:
            documents = int(c.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] or 0)
            indexed_source_count = int(
                c.execute("SELECT COUNT(DISTINCT source_path) FROM knowledge_fts").fetchone()[0] or 0
            )
        finally:
            c.close()

    stale_reasons: list[str] = []
    if not newest_index:
        stale_reasons.append("missing_index")
    if freshness_basis == "source_manifest_missing":
        stale_reasons.append("source_manifest_missing")
    elif not schema_current:
        stale_reasons.append("schema_outdated")
    else:
        if source_count and documents == 0:
            stale_reasons.append("empty_index")
        if indexed_source_count != source_count:
            stale_reasons.append("source_count_mismatch")
        if newest_source and newest_index and newest_source > newest_index:
            stale_reasons.append("source_newer_than_index")

    return {
        "documents": documents,
        "cards": documents,
        "index_present": bool(newest_index and schema_current),
        "schema_current": schema_current,
        "source_file_count": source_count,
        "indexed_source_path_count": indexed_source_count,
        "newest_source_mtime": newest_source,
        "newest_index_mtime": newest_index,
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "freshness_basis": freshness_basis,
    }


_EXPLAIN_PATH_CAP = 20


def explain_source_diff(
    db_path: Path | str | None = None,
    *,
    vault_globs: tuple[str, ...] | None = None,
    max_paths: int = _EXPLAIN_PATH_CAP,
) -> dict:
    db = Path(db_path) if db_path else DEFAULT_INDEX_DB
    newest_index = _index_mtime(db)

    live_paths: dict[str, float] = {}
    for path in _iter_vault_files(vault_globs=vault_globs):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        try:
            rel = str(path.resolve().relative_to(_REPO_ROOT))
        except ValueError:
            rel = str(path)
        live_paths[rel] = mtime

    indexed_paths: set[str] = set()
    c = _conn_ro(db)
    if c is not None:
        try:
            rows = c.execute("SELECT DISTINCT source_path FROM knowledge_fts").fetchall()
            indexed_paths = {str(row[0]) for row in rows if row[0]}
        finally:
            c.close()

    missing = sorted(p for p in live_paths if p not in indexed_paths)
    extra = sorted(p for p in indexed_paths if p not in live_paths)
    newer = sorted(
        (p for p in live_paths if newest_index and live_paths[p] > newest_index),
        key=lambda p: live_paths[p],
        reverse=True,
    )

    def _bounded(items: list[str]) -> tuple[list[str], int]:
        return items[:max_paths], max(0, len(items) - max_paths)

    missing_bounded, missing_truncated = _bounded(missing)
    extra_bounded, extra_truncated = _bounded(extra)
    newer_bounded, newer_truncated = _bounded(newer)

    return {
        "missing_source_paths": missing_bounded,
        "missing_source_paths_truncated": missing_truncated,
        "extra_indexed_paths": extra_bounded,
        "extra_indexed_paths_truncated": extra_truncated,
        "newer_source_paths": newer_bounded,
        "newer_source_paths_truncated": newer_truncated,
        "live_source_count": len(live_paths),
        "indexed_source_count": len(indexed_paths),
        "newest_index_mtime": newest_index,
        "cap": max_paths,
    }
