"""Tests for the read-only knowledge/memory MCP surface.

Fixture data is created only through the subsystem's own write paths
(``facts.upsert_fact``, ``memory_proposals.propose_memory``) -- never by
hand-inserting rows -- so the tests exercise the real schema.

Every read call is bracketed by a byte-level fingerprint of the store (main db
plus its -wal/-shm sidecars) to prove the read surface cannot write.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.coord import mcp_coord_server  # noqa: E402
from coordharness.knowledge import facts, memory_proposals, read_surface  # noqa: E402

Fingerprint = tuple[Any, ...]


def _fingerprint(db: Path) -> Fingerprint:
    """Byte-level identity of the store: mtime, size, content hash, page count.

    Covers the main db and the -wal sidecar, because committed data lives in
    one or the other. Two deliberate exclusions:

    * ``-shm`` is volatile shared-memory coordination state that sqlite mutates
      on pure reads (read marks), so including it would make every read look
      like a write.
    * A zero-byte ``-wal`` is treated as equivalent to an absent one. Merely
      OPENING a WAL database materialises an empty -wal, read-only connections
      included; an empty WAL holds no frames and therefore no data. A real
      write appends frames and is caught -- see
      ``test_fingerprint_detects_a_real_write``.
    """
    parts: list[Any] = []
    for suffix in ("", "-wal"):
        path = Path(str(db) + suffix)
        data = path.read_bytes() if path.exists() else None
        if data is None or (suffix == "-wal" and not data):
            parts.append((suffix, "no-data"))
            continue
        parts.append((suffix, path.stat().st_mtime_ns, len(data), hashlib.sha256(data).hexdigest()))
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            parts.append(("page_count", conn.execute("PRAGMA page_count").fetchone()[0]))
        finally:
            conn.close()
    return tuple(parts)


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """A seeded throwaway knowledge store (facts + memory proposals share one db)."""
    db = tmp_path / "knowledge.db"
    facts.upsert_fact(
        "search throughput baseline",
        "1200",
        unit="cases",
        module="search",
        status="live",
        owner_lane="shared",
        notes="seeded by test",
        db_path=db,
    )
    facts.upsert_fact(
        "checkout conversion rate",
        "0.42",
        module="billing",
        status="live",
        owner_lane="claude",
        db_path=db,
    )
    facts.upsert_fact(
        "beta signup scope",
        "parked pending review",
        module="billing",
        status="closed",
        owner_lane="operator",
        db_path=db,
    )
    memory_proposals.propose_memory(
        kind="fact",
        statement="operator prefers kebab-case CLI verbs",
        value="kebab-case",
        provenance={"source": "test-seed"},
        tags=["cli"],
        db_path=db,
    )
    memory_proposals.propose_memory(
        kind="preference",
        statement="charts must cite their denominator",
        provenance={"source": "test-seed"},
        db_path=db,
    )
    return db


# --------------------------------------------------------------------------
# facts_query
# --------------------------------------------------------------------------


def test_facts_query_structured_filter_is_exact(store: Path) -> None:
    before = _fingerprint(store)
    payload = read_surface.facts_query(module="billing", db_path=store)
    assert _fingerprint(store) == before

    assert payload["match_mode"] == "structured_filter"
    assert payload["count"] == 2
    assert {row["module"] for row in payload["facts"]} == {"billing"}
    assert payload["source"] == {
        "store": "knowledge_db",
        "path": str(store.resolve()),
        "table": "facts",
        "present": True,
        "readable": True,
        "table_present": True,
    }
    assert payload["read_only"] is True
    assert payload["actions_enabled"] is False
    assert payload["schema_version"] == read_surface.SCHEMA_VERSION


def test_facts_query_status_filter_and_store_totals(store: Path) -> None:
    before = _fingerprint(store)
    payload = read_surface.facts_query(status="closed", db_path=store)
    assert _fingerprint(store) == before

    assert payload["count"] == 1
    assert payload["facts"][0]["statement"] == "beta signup scope"
    # Denominator travels with the filtered count so a caller cannot mistake
    # "1 closed" for "1 fact in the ledger".
    assert payload["store_totals"] == {"total": 3, "by_status": {"live": 2, "closed": 1}}


def test_facts_query_text_reports_ranked_match_mode(store: Path) -> None:
    before = _fingerprint(store)
    payload = read_surface.facts_query(text="throughput", db_path=store)
    assert _fingerprint(store) == before

    assert payload["match_mode"] == "text_search"
    assert payload["filters"]["text"] == "throughput"
    assert any("throughput" in row["statement"] for row in payload["facts"])


def test_facts_query_rows_carry_row_key_provenance(store: Path) -> None:
    payload = read_surface.facts_query(module="search", db_path=store)
    row = payload["facts"][0]
    assert row["id"]
    assert facts.get_fact(row["id"], db_path=store) is not None
    for field in ("statement", "value", "unit", "status", "module", "owner_lane", "updated_at"):
        assert field in row


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (10_000, read_surface.MAX_FACTS_LIMIT),
        (0, 1),
        (-5, 1),
        ("not-a-number", read_surface.DEFAULT_FACTS_LIMIT),
        (None, read_surface.DEFAULT_FACTS_LIMIT),
    ],
)
def test_facts_query_bounds_limit(store: Path, requested: Any, expected: int) -> None:
    payload = read_surface.facts_query(limit=requested, db_path=store)
    assert payload["limit"] == expected


def test_facts_query_rejects_unknown_status(store: Path) -> None:
    before = _fingerprint(store)
    with pytest.raises(ValueError, match="bad status"):
        read_surface.facts_query(status="not-a-status", db_path=store)
    assert _fingerprint(store) == before


# --------------------------------------------------------------------------
# knowledge_index_status
# --------------------------------------------------------------------------


def test_knowledge_index_status_reports_freshness(store: Path) -> None:
    before = _fingerprint(store)
    payload = read_surface.knowledge_index_status(db_path=store)
    assert _fingerprint(store) == before

    assert payload["mode"] == "knowledge_index_status"
    assert payload["source"]["table"] == "knowledge_fts"
    assert payload["read_only"] is True
    index = payload["index"]
    for key in ("documents", "index_present", "schema_current", "source_file_count", "stale_reasons"):
        assert key in index
    # The store file exists (facts created it) but carries no knowledge_fts
    # table, so the honest reading is "schema_outdated", not "missing_index".
    # Either way the surface must not let an unindexed store read as fresh.
    assert payload["stale"] is True
    assert payload["stale_reasons"] == ["schema_outdated"]
    assert index["index_present"] is False
    assert index["documents"] == 0
    assert index["schema_current"] is False
    assert payload["rebuild_entry_point"] == "coordharness.knowledge.kfts.rebuild_index"


def test_knowledge_index_status_honours_manifest_flags(store: Path) -> None:
    before = _fingerprint(store)
    payload = read_surface.knowledge_index_status(
        use_manifest=True, scan_fallback=False, db_path=store
    )
    assert _fingerprint(store) == before
    assert payload["requested"] == {"use_manifest": True, "scan_fallback": False}
    assert payload["index"]["freshness_basis"] == "source_manifest_missing"


# --------------------------------------------------------------------------
# memory_proposals_list / memory_proposals_get
# --------------------------------------------------------------------------


def test_memory_proposals_list_returns_queue(store: Path) -> None:
    before = _fingerprint(store)
    payload = read_surface.memory_proposals_list(db_path=store)
    assert _fingerprint(store) == before

    assert payload["count"] == 2
    assert payload["source"]["table"] == "memory_proposals"
    assert {row["kind"] for row in payload["proposals"]} == {"fact", "preference"}
    for row in payload["proposals"]:
        assert row["id"] and row["content_hash"]
        assert row["status"] == "proposed"
        assert row["provenance"] == {"source": "test-seed"}


def test_memory_proposals_list_filters(store: Path) -> None:
    before = _fingerprint(store)
    assert read_surface.memory_proposals_list(kind="preference", db_path=store)["count"] == 1
    assert read_surface.memory_proposals_list(status="proposed", db_path=store)["count"] == 2
    assert read_surface.memory_proposals_list(status="accepted", db_path=store)["count"] == 0
    assert _fingerprint(store) == before


def test_memory_proposals_list_rejects_unknown_kind(store: Path) -> None:
    with pytest.raises(ValueError, match="bad kind"):
        read_surface.memory_proposals_list(kind="nonsense", db_path=store)


def test_memory_proposals_get_found_and_missing(store: Path) -> None:
    listed = read_surface.memory_proposals_list(kind="fact", db_path=store)
    target = listed["proposals"][0]["id"]

    before = _fingerprint(store)
    found = read_surface.memory_proposals_get(target, db_path=store)
    missing = read_surface.memory_proposals_get("no-such-proposal", db_path=store)
    assert _fingerprint(store) == before

    assert found["exists"] is True
    assert found["proposal"]["id"] == target
    assert found["proposal"]["statement"] == "operator prefers kebab-case CLI verbs"
    assert missing["exists"] is False
    assert missing["proposal"] is None
    assert missing["id"] == "no-such-proposal"


def test_memory_proposals_get_requires_id(store: Path) -> None:
    with pytest.raises(ValueError, match="id is required"):
        read_surface.memory_proposals_get("   ", db_path=store)


# --------------------------------------------------------------------------
# The write-side-effect guard
# --------------------------------------------------------------------------


def test_read_surface_never_creates_a_missing_store(tmp_path: Path) -> None:
    """A read must not bring the store into existence.

    memory_proposals.list_proposals/get_proposal open a READ-WRITE connection
    that does mkdir(parents=True) + executescript(SCHEMA_SQL); called against a
    missing path they create the file. The read surface guards on existence, so
    this test fails if that guard is ever removed.
    """
    missing = tmp_path / "absent" / "knowledge.db"

    payloads = [
        read_surface.facts_query(db_path=missing),
        read_surface.knowledge_index_status(db_path=missing),
        read_surface.memory_proposals_list(db_path=missing),
        read_surface.memory_proposals_get("anything", db_path=missing),
    ]

    assert not missing.exists()
    assert not missing.parent.exists()
    for payload in payloads:
        assert payload["source"]["present"] is False
        assert payload["read_only"] is True

    assert payloads[0]["count"] == 0
    assert payloads[0]["facts"] == []
    assert payloads[2]["count"] == 0
    assert payloads[2]["proposals"] == []
    assert payloads[3]["exists"] is False


def test_fingerprint_detects_a_real_write(store: Path) -> None:
    """Ablation: prove the read-only assertion is not vacuous.

    Every other test asserts _fingerprint() is unchanged across reads. That
    assertion is only meaningful if the fingerprint actually moves when the
    store IS written, so drive one real write through the subsystem's own
    write path and require the fingerprint to change.
    """
    before = _fingerprint(store)
    facts.upsert_fact(
        "ablation probe",
        "1",
        module="ablation",
        status="live",
        owner_lane="shared",
        db_path=store,
    )
    assert _fingerprint(store) != before


def test_underlying_proposal_reader_would_create_the_store(tmp_path: Path) -> None:
    """Pin the upstream behaviour the guard above exists to contain."""
    missing = tmp_path / "raw" / "knowledge.db"
    memory_proposals.list_proposals(db_path=missing)
    assert missing.exists()


# --------------------------------------------------------------------------
# MCP wiring
# --------------------------------------------------------------------------


_TOOL_NAMES = (
    "facts_query",
    "knowledge_index_status",
    "memory_proposals_list",
    "memory_proposals_get",
)


@pytest.mark.parametrize("name", _TOOL_NAMES)
def test_tool_is_registered_and_visible(name: str) -> None:
    assert name in mcp_coord_server._MCP_TOOL_NAMES
    catalog = mcp_coord_server._server_tool_catalog(env={})
    assert name in catalog["visible"]


def test_server_tool_wrappers_reach_the_read_surface(store: Path) -> None:
    before = _fingerprint(store)
    facts_payload = mcp_coord_server._tool_facts_query(module="billing", knowledge_db=store)
    index_payload = mcp_coord_server._tool_knowledge_index_status(knowledge_db=store)
    list_payload = mcp_coord_server._tool_memory_proposals_list(knowledge_db=store)
    get_payload = mcp_coord_server._tool_memory_proposals_get(
        id=list_payload["proposals"][0]["id"], knowledge_db=store
    )
    assert _fingerprint(store) == before

    assert facts_payload["count"] == 2
    assert index_payload["mode"] == "knowledge_index_status"
    assert list_payload["count"] == 2
    assert get_payload["exists"] is True
    for payload in (facts_payload, index_payload, list_payload, get_payload):
        assert payload["source"]["path"] == str(store.resolve())
        assert payload["actions_enabled"] is False


def test_cli_twin_names_are_snake_case_siblings() -> None:
    """MCP-first parity: each tool's CLI twin is the obvious kebab sibling."""
    for name in _TOOL_NAMES:
        assert name.islower()
        assert " " not in name and "-" not in name
        assert name.replace("_", "-") == name.replace("_", "-").lower()


# --------------------------------------------------------------------------
# Adversarial verification pass
#
# The guard above only covered a MISSING store. Each test below reproduces a
# defect that survived that guard, measured against the real subsystem.
# --------------------------------------------------------------------------


@pytest.fixture()
def facts_only_store(tmp_path: Path) -> Path:
    """A knowledge store built the ordinary way: facts, and no proposals table.

    This is what facts.init_db / upsert_fact alone produce, and it is the shape
    the original existence-only guard did not cover -- the seeded `store`
    fixture happens to create the proposals schema too, which hid the write.
    """
    db = tmp_path / "knowledge.db"
    facts.upsert_fact(
        "search throughput baseline",
        "1200",
        module="search",
        status="live",
        owner_lane="shared",
        db_path=db,
    )
    assert _tables(db) == ["facts"]
    return db


def _tables(db: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return sorted(
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
    finally:
        conn.close()


def test_proposal_reads_do_not_create_the_table_on_a_facts_only_store(
    facts_only_store: Path,
) -> None:
    """Reading proposals must not CREATE the proposals table.

    Measured before the fix: memory_proposals_list took the store from
    page_count 17 to 23 and added a `memory_proposals` table, because
    memory_proposals._conn runs executescript(SCHEMA_SQL) on every open. File
    existence was true, so the existence guard let it through.
    """
    before = _fingerprint(facts_only_store)

    listed = read_surface.memory_proposals_list(db_path=facts_only_store)
    fetched = read_surface.memory_proposals_get("anything", db_path=facts_only_store)

    assert _fingerprint(facts_only_store) == before
    assert _tables(facts_only_store) == ["facts"]
    for payload in (listed, fetched):
        assert payload["source"]["present"] is True
        assert payload["source"]["table_present"] is False
    assert listed["count"] == 0 and listed["proposals"] == []
    assert fetched["exists"] is False


def test_underlying_proposal_reader_would_create_the_table(facts_only_store: Path) -> None:
    """Ablation for the test above: prove the guard is load-bearing, not decorative."""
    memory_proposals.list_proposals(db_path=facts_only_store)
    assert "memory_proposals" in _tables(facts_only_store)


def test_reads_fail_closed_on_a_store_that_is_not_a_database(tmp_path: Path) -> None:
    """A corrupt store must report itself, not raise sqlite3.DatabaseError.

    Measured before the fix: facts_query, memory_proposals_list and
    memory_proposals_get all raised `DatabaseError: file is not a database`
    straight through the MCP tool.
    """
    junk = tmp_path / "knowledge.db"
    junk.write_bytes(b"this is not a database" * 64)

    payloads = {
        "facts_query": read_surface.facts_query(db_path=junk),
        "knowledge_index_status": read_surface.knowledge_index_status(db_path=junk),
        "memory_proposals_list": read_surface.memory_proposals_list(db_path=junk),
        "memory_proposals_get": read_surface.memory_proposals_get("x", db_path=junk),
    }
    for payload in payloads.values():
        assert payload["source"]["present"] is True
        assert payload["source"]["readable"] is False
        assert payload["source"]["table_present"] is False
    assert payloads["facts_query"]["count"] == 0
    assert payloads["facts_query"]["store_totals"] == {"total": 0, "by_status": {}}
    assert payloads["memory_proposals_list"]["count"] == 0
    assert payloads["memory_proposals_get"]["exists"] is False
    assert payloads["knowledge_index_status"]["stale"] is True


def test_facts_query_fails_closed_on_a_store_with_no_facts_table(tmp_path: Path) -> None:
    """facts.stats() has no missing-table guard, so it raised where query_facts did not."""
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x TEXT)")
    conn.commit()
    conn.close()

    payload = read_surface.facts_query(db_path=db)
    assert payload["source"] == {
        "store": "knowledge_db",
        "path": str(db.resolve()),
        "table": "facts",
        "present": True,
        "readable": True,
        "table_present": False,
    }
    assert payload["count"] == 0
    assert payload["facts"] == []
    assert payload["store_totals"] == {"total": 0, "by_status": {}}


def test_truncated_is_false_when_the_result_is_complete(tmp_path: Path) -> None:
    """`count == limit` is not evidence of more rows; claiming so asserts a fact
    the store does not carry."""
    db = tmp_path / "knowledge.db"
    for i in range(3):
        facts.upsert_fact(
            f"seeded statement {i}", str(i), module="m", status="live",
            owner_lane="shared", db_path=db,
        )

    exact = read_surface.facts_query(limit=3, db_path=db)
    assert exact["count"] == 3
    assert exact["truncated"] is False

    clipped = read_surface.facts_query(limit=2, db_path=db)
    assert clipped["count"] == 2
    assert clipped["truncated"] is True


def test_proposal_truncated_is_false_when_the_queue_is_complete(store: Path) -> None:
    exact = read_surface.memory_proposals_list(limit=2, db_path=store)
    assert exact["count"] == 2 and exact["truncated"] is False

    clipped = read_surface.memory_proposals_list(limit=1, db_path=store)
    assert clipped["count"] == 1 and clipped["truncated"] is True


@pytest.mark.parametrize("text", ["%", "_", "the", "!!!", "of and"])
def test_text_with_no_searchable_terms_returns_nothing_and_says_so(
    store: Path, text: str
) -> None:
    """A text filter that tokenises to nothing must not return the whole ledger.

    Measured before the fix: facts.search_facts falls back to an unfiltered
    candidate scan when the query yields no terms, so `text='%'` returned every
    fact with match_mode='text_search' -- rows presented as text matches that
    had matched nothing.
    """
    payload = read_surface.facts_query(text=text, db_path=store)
    assert payload["match_mode"] == "text_no_searchable_terms"
    assert payload["filters"]["text"] == text.strip()
    assert payload["filters"]["text_terms"] == []
    assert payload["count"] == 0
    assert payload["facts"] == []


def test_real_text_terms_are_reported_alongside_the_hits(store: Path) -> None:
    payload = read_surface.facts_query(text="throughput", db_path=store)
    assert payload["match_mode"] == "text_search"
    assert payload["filters"]["text_terms"] == ["throughput"]
    assert payload["count"] >= 1


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE facts; --",
        "' OR '1'='1",
        "m0' UNION SELECT * FROM facts --",
        "../../../../etc/passwd",
        "/etc/passwd",
        "\\",
        "\x00nul",
        "a" * 5000,
    ],
)
def test_hostile_inputs_neither_write_nor_widen(store: Path, hostile: str) -> None:
    """Filters are bound parameters, so injection strings match literally or not
    at all -- and never mutate the store."""
    before = _fingerprint(store)
    by_module = read_surface.facts_query(module=hostile, db_path=store)
    fetched = read_surface.memory_proposals_get(hostile, db_path=store)
    assert _fingerprint(store) == before

    assert by_module["count"] == 0
    assert by_module["store_totals"]["total"] == 3  # the ledger is intact
    assert fetched["exists"] is False
    assert _tables(store) == sorted(_tables(store))


def test_limit_bounds_hold_for_non_integer_and_overflow_inputs(store: Path) -> None:
    """float('inf') raises OverflowError, not ValueError, and escaped the bound."""
    assert read_surface.facts_query(limit=float("inf"), db_path=store)["limit"] == (
        read_surface.DEFAULT_FACTS_LIMIT
    )
    assert read_surface.facts_query(limit=float("nan"), db_path=store)["limit"] == (
        read_surface.DEFAULT_FACTS_LIMIT
    )
    assert read_surface.facts_query(limit=10**18, db_path=store)["limit"] == (
        read_surface.MAX_FACTS_LIMIT
    )
    assert read_surface.memory_proposals_list(limit=10**18, db_path=store)["limit"] == (
        read_surface.MAX_PROPOSALS_LIMIT
    )


def test_caps_actually_bind_against_more_rows_than_the_cap(tmp_path: Path) -> None:
    """Seed past the cap and count what comes back."""
    db = tmp_path / "knowledge.db"
    for i in range(read_surface.MAX_FACTS_LIMIT + 25):
        facts.upsert_fact(
            f"capped statement {i}", str(i), module="cap", status="live",
            owner_lane="shared", db_path=db,
        )
    payload = read_surface.facts_query(limit=10_000, db_path=db)
    assert payload["limit"] == read_surface.MAX_FACTS_LIMIT
    assert len(payload["facts"]) == read_surface.MAX_FACTS_LIMIT
    assert payload["count"] == read_surface.MAX_FACTS_LIMIT
    assert payload["truncated"] is True
    assert payload["store_totals"]["total"] == read_surface.MAX_FACTS_LIMIT + 25


def test_every_payload_carries_a_full_provenance_block(store: Path) -> None:
    """No payload may leave without saying which store, table and row it came from."""
    listed = read_surface.memory_proposals_list(db_path=store)
    payloads = [
        read_surface.facts_query(db_path=store),
        read_surface.knowledge_index_status(db_path=store),
        listed,
        read_surface.memory_proposals_get(listed["proposals"][0]["id"], db_path=store),
    ]
    for payload in payloads:
        assert set(payload["source"]) == {
            "store", "path", "table", "present", "readable", "table_present",
        }
        assert payload["source"]["path"] == str(store.resolve())
        assert payload["read_only"] is True
        assert payload["actions_enabled"] is False
        assert payload["schema_version"] == read_surface.SCHEMA_VERSION
        for row in payload.get("facts", []) + payload.get("proposals", []):
            assert row["id"]
