"""The agent-facing MCP surface must not answer from a store that does not exist.

A missing fact ledger and a ledger with no matching rows are different answers.
Rendering both as ``count: 0`` makes the absence unreportable, so these tests pin
the store condition as an error and pin the clean-install path that creates it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.bootstrap import bootstrap_database  # noqa: E402
from coordharness.coord import mcp_coord_server  # noqa: E402
from coordharness.knowledge import facts  # noqa: E402


def _ready_store(path: Path) -> Path:
    facts.init_db(path)
    return path


def test_fact_store_state_separates_absence_from_emptiness(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    schemaless = tmp_path / "schemaless.db"
    conn = sqlite3.connect(schemaless)
    conn.execute("CREATE TABLE unrelated(x)")
    conn.commit()
    conn.close()
    ready = _ready_store(tmp_path / "ready.db")

    assert mcp_coord_server.fact_store_state(absent)["state"] == "absent"
    assert mcp_coord_server.fact_store_state(schemaless)["state"] == "schema_missing"
    assert mcp_coord_server.fact_store_state(ready)["state"] == "ready"
    # Classifying must never create the store it is asked about.
    assert not absent.exists()


def test_facts_lookup_refuses_to_answer_from_an_absent_store(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"

    with pytest.raises(mcp_coord_server.FactStoreUnavailable) as excinfo:
        mcp_coord_server._tool_facts_lookup(query="anything", knowledge_db=absent)

    assert excinfo.value.state["state"] == "absent"


def test_facts_lookup_refuses_a_store_missing_the_facts_table(tmp_path: Path) -> None:
    schemaless = tmp_path / "schemaless.db"
    conn = sqlite3.connect(schemaless)
    conn.execute("CREATE TABLE unrelated(x)")
    conn.commit()
    conn.close()

    with pytest.raises(mcp_coord_server.FactStoreUnavailable) as excinfo:
        mcp_coord_server._tool_facts_lookup(query="anything", knowledge_db=schemaless)

    assert excinfo.value.state["state"] == "schema_missing"


def test_facts_lookup_attributes_a_real_zero_to_a_ready_store(tmp_path: Path) -> None:
    ready = _ready_store(tmp_path / "ready.db")

    payload = mcp_coord_server._tool_facts_lookup(
        query="no-such-fact-anywhere", knowledge_db=ready
    )

    assert payload["count"] == 0
    assert payload["store"]["state"] == "ready"
    assert payload["store"]["path"] == str(ready)


def test_facts_lookup_returns_a_seeded_fact_from_a_ready_store(tmp_path: Path) -> None:
    ready = _ready_store(tmp_path / "ready.db")
    facts.upsert_fact(
        "coordination spine is coord.db",
        value="coord.db",
        module="coord",
        db_path=ready,
    )

    payload = mcp_coord_server._tool_facts_lookup(query="coordination", knowledge_db=ready)

    assert payload["count"] >= 1
    assert payload["store"]["state"] == "ready"


def test_knowledge_search_reports_the_fact_store_condition_not_a_zero(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent.db"

    payload = mcp_coord_server._tool_knowledge_search(
        query="coordination", knowledge_db=absent, include_diagnostics=True
    )

    facts_results = [
        result
        for result in payload["context"]["provider_results"]
        if result.get("source") == "facts"
    ]
    assert facts_results, "the facts provider must appear in the composite reply"
    assert "absent" in str(facts_results[0]["error"])
    assert any(
        entry.get("source") == "facts" for entry in payload["context"]["errors"]
    )


def test_knowledge_search_leaves_a_ready_store_unflagged(tmp_path: Path) -> None:
    ready = _ready_store(tmp_path / "ready.db")

    payload = mcp_coord_server._tool_knowledge_search(
        query="coordination", knowledge_db=ready, include_diagnostics=True
    )

    facts_results = [
        result
        for result in payload["context"]["provider_results"]
        if result.get("source") == "facts"
    ]
    assert facts_results
    assert facts_results[0]["error"] is None
    assert not any(
        entry.get("source") == "facts" for entry in payload["context"]["errors"]
    )


def test_build_server_creates_the_fact_store_on_a_clean_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COORD_DEPLOYMENT_PROFILE", "generic")
    db = tmp_path / "state" / "coord.db"
    knowledge = tmp_path / "state" / "knowledge.db"
    assert not knowledge.exists()

    mcp_coord_server.build_server(str(db), knowledge_db=knowledge)

    assert knowledge.exists()
    assert mcp_coord_server.fact_store_state(knowledge)["state"] == "ready"


def test_ensure_fact_store_is_idempotent(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge.db"

    first = mcp_coord_server.ensure_fact_store(knowledge)
    second = mcp_coord_server.ensure_fact_store(knowledge)

    assert first["created"] is True
    assert second["created"] is False
    assert second["state"] == "ready"


def test_generic_agent_surface_answers_on_a_fresh_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The clean-install contract: the read verbs an agent boots with all answer."""

    monkeypatch.setenv("COORD_DEPLOYMENT_PROFILE", "generic")
    db = tmp_path / "state" / "coord.db"
    bootstrap_database(db)
    knowledge = _ready_store(tmp_path / "state" / "knowledge.db")

    preflight = mcp_coord_server._tool_preflight(
        actor="probe", session_id="probe:clean-install", db_path=str(db)
    )
    board = mcp_coord_server._tool_board(db_path=str(db))
    next_work = mcp_coord_server._tool_next_work(actor="probe", db_path=str(db))
    inbox = mcp_coord_server._tool_inbox(actor="probe", db_path=str(db))
    runs = mcp_coord_server._tool_runs(db_path=str(db))
    facts_payload = mcp_coord_server._tool_facts_lookup(
        query="coordination", knowledge_db=knowledge
    )

    assert preflight["actor"] == "probe"
    assert preflight["capability_handshake"]["lifecycle_authority"] == "coord.db"
    assert board["rows"] == []
    assert next_work["items"] == []
    assert inbox["messages"] == []
    assert runs["count"] == 0
    assert facts_payload["store"]["state"] == "ready"
