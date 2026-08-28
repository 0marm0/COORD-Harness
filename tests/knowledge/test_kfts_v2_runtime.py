from __future__ import annotations

import sqlite3

from coordharness.knowledge import kfts


def test_shadow_v2_search_is_structurally_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(kfts, "_REPO_ROOT", tmp_path)
    db = tmp_path / "shadow.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE sources(
      source_id TEXT PRIMARY KEY,logical_path TEXT,resolved_path TEXT,plane TEXT,
      module TEXT,lifecycle TEXT,instruction_effect TEXT,canonical_disposition TEXT,
      classification_state TEXT,sensitivity TEXT,actor_allowlist_json TEXT);
    CREATE TABLE cards(
      card_id INTEGER PRIMARY KEY,source_id TEXT,pointer TEXT,title TEXT,body TEXT,
      heading TEXT,heading_path TEXT,heading_level INTEGER,section_index INTEGER,
      line_start INTEGER,line_end INTEGER);
    CREATE VIRTUAL TABLE cards_fts USING fts5(title,body,content='cards',content_rowid='card_id');
    INSERT INTO schema_meta VALUES('schema_version','shadow-kfts-v1');
    """)
    source = tmp_path / "source.md"
    source.write_text("# Canonical\\nneedle context\\n")
    conn.execute(
        "INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("s1","source.md",str(source),"shared","ops","current","advisory",
         "reference","complete","internal",'["*"]'),
    )
    conn.execute(
        "INSERT INTO cards VALUES(1,'s1','shadow://source.md#canonical','Canonical',"
        "'needle context','Canonical','Canonical',1,0,1,2)"
    )
    conn.execute("INSERT INTO cards_fts(rowid,title,body) VALUES(1,'Canonical','needle context')")
    conn.commit()
    conn.close()
    rows = kfts.search("needle", db_path=db, limit=4)
    assert len(rows) == 1
    assert rows[0]["freshness_basis"] == "immutable_kfts_v2r_snapshot"
    assert rows[0]["plane"] == "shared"
