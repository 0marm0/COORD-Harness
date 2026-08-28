from __future__ import annotations

import sqlite3
from pathlib import Path

from coordharness.knowledge import kfts

MIRROR = kfts.MEMORY_MIRROR_REL


def _insert_card(c: sqlite3.Connection, source_path: str, title: str, body: str) -> None:
    pointer = f"memory://{source_path}#0"
    c.execute(
        "INSERT INTO knowledge_fts(pointer, title, body, card_kind, doc_pointer, source_path,"
        " heading, heading_path, heading_slug, heading_level, section_index,"
        " line_start, line_end, line_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pointer, title, body, "memory", f"memory://{source_path}", source_path,
         title, title, "h", 1, 0, 1, 1, 1),
    )


def _build_index(tmp_path: Path) -> Path:
    db = tmp_path / "k.db"
    c = kfts._conn(db)
    try:
        _insert_card(c, f"{MIRROR}/corpus-fulltext-do-not-delete.md",
                     "corpus fulltext do not delete",
                     "corpus_fulltext.duckdb is a large raw source-text store, load-bearing for stage-four embeddings; never delete.")
        _insert_card(c, f"{MIRROR}/mlx-local-job-optimization.md",
                     "mlx local job optimization",
                     "right-size model + LARGE flattened batches + crash-safe incremental flush before buffering all.")
        _insert_card(c, f"{MIRROR}/MEMORY.md", "Memory Index",
                     "corpus fulltext do not delete index hook pointer line.")
        _insert_card(c, "docs/strategy/SYNTHETIC_PLAN.md",
                     "synthetic plan",
                     "corpus_fulltext correction: it is load-bearing, do not delete.")
        c.commit()
    finally:
        c.close()
    return db


def test_gate_finds_matching_content_memory(tmp_path: Path) -> None:
    db = _build_index(tmp_path)
    hits = kfts.find_similar_memory("corpus_fulltext do not delete large source text", db_path=db)
    slugs = [h["slug"] for h in hits]
    assert "corpus-fulltext-do-not-delete" in slugs, slugs
    assert "SYNTHETIC_PLAN" not in slugs, slugs
    assert "MEMORY" not in slugs, slugs


def test_gate_returns_empty_when_no_neardup(tmp_path: Path) -> None:
    db = _build_index(tmp_path)
    hits = kfts.find_similar_memory("synthetic forecast market RSA key funding paper mode", db_path=db)
    assert hits == [], hits


def test_gate_threshold_excludes_weak_matches(tmp_path: Path) -> None:
    db = _build_index(tmp_path)
    weak = kfts.find_similar_memory("synthetic blockchain quantum delete", db_path=db, min_coverage=0.9)
    assert weak == [], weak
