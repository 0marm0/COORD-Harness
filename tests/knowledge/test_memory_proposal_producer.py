from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.knowledge import kfts, memory_proposals


def _real_session(
    tmp_path: Path,
    *,
    session_id: str,
    external_thread_id: str,
) -> tuple[sqlite3.Connection, Path, Path]:
    coord_path = tmp_path / "state" / "coord.db"
    knowledge_path = tmp_path / "state" / "knowledge.db"
    bootstrap_database(coord_path)
    conn = connect(coord_path)
    coord_db.register_session(
        conn,
        session_id,
        "codex",
        external_thread_id=external_thread_id,
        lease_s=600,
    )
    return conn, coord_path, knowledge_path


def _establish_decision(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    ruling: str,
    work_id: str | None = None,
    scope: str = "global",
    memory_candidate: bool = True,
) -> int:
    if work_id:
        coord_db.upsert_work(
            conn,
            work_id,
            title="Memory proposal producer fixture",
            assignee="codex",
        )
    event_id = coord_db.post_decision_event(
        conn,
        ruling=ruling,
        actor="codex",
        session_id=session_id,
        work_id=work_id,
        scope=scope,
        refs=["docs/reports/producer-evidence.md"],
        memory_candidate=memory_candidate,
    )
    assert event_id is not None
    return int(event_id)


def _closeout(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    knowledge_path: Path,
) -> dict:
    return coord_db.session_closeout(
        conn,
        session_id=session_id,
        actor="codex",
        summary="Focused producer closeout.",
        dirty_files=[],
        knowledge_db_path=knowledge_path,
    )


def _insert_memory_card(db_path: Path, *, slug: str, body: str) -> None:
    conn = kfts._conn(db_path)
    try:
        source_path = f"{kfts.MEMORY_MIRROR_REL}/{slug}.md"
        conn.execute(
            "INSERT INTO knowledge_fts(pointer,title,body,card_kind,doc_pointer,source_path,"
            "heading,heading_path,heading_slug,heading_level,section_index,line_start,line_end,"
            "line_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"memory://{source_path}#0", slug, body, "memory", f"memory://{source_path}",
                source_path, slug, slug, slug, 1, 0, 1, 1, 1,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_fenced_closeout_proposes_typed_fact_with_exact_structured_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "state"))
    session_id = "codex:proposal-producer"
    conn, _coord_path, knowledge_path = _real_session(
        tmp_path, session_id=session_id, external_thread_id="thread-proposal-producer"
    )
    try:
        event_id = _establish_decision(
            conn,
            session_id=session_id,
            work_id="MEMORY-1",
            ruling="The producer requires an explicit typed durable-fact marker.",
        )
        result = _closeout(conn, session_id=session_id, knowledge_path=knowledge_path)
    finally:
        conn.close()

    proposal = memory_proposals.list_proposals(db_path=knowledge_path)[0]
    evidence = proposal.provenance["evidence"]
    assert result["memory_proposals"]["eligible"] == 1
    assert proposal.evidence_pointer is None
    assert proposal.source_actor == "codex"
    assert proposal.source_thread_id == "thread-proposal-producer"
    assert proposal.source_work_id == "MEMORY-1"
    assert evidence["schema"] == "coordharness.coord-event-evidence.v1"
    assert evidence["event_id"] == event_id
    assert evidence["coord_db"]["database_ref"] == "state://coord.db"
    assert len(evidence["coord_db"]["database_binding_sha256"]) == 64
    assert len(evidence["event_receipt_sha256"]) == 64


def test_module_and_initiative_facts_never_widen_to_global(tmp_path: Path) -> None:
    session_id = "codex:proposal-scopes"
    conn, _coord_path, knowledge_path = _real_session(
        tmp_path, session_id=session_id, external_thread_id="thread-proposal-scopes"
    )
    try:
        _establish_decision(
            conn, session_id=session_id, ruling="Module fact.", scope="module:retrieval"
        )
        _establish_decision(
            conn, session_id=session_id, ruling="Initiative fact.", scope="initiative:memory"
        )
        _closeout(conn, session_id=session_id, knowledge_path=knowledge_path)
    finally:
        conn.close()

    proposals = memory_proposals.list_proposals(db_path=knowledge_path)
    assert {proposal.scope for proposal in proposals} == {"project"}
    assert {proposal.provenance["authoritative_scope"] for proposal in proposals} == {
        "module:retrieval", "initiative:memory"
    }


def test_unmarked_normative_decision_is_not_a_fact_candidate(tmp_path: Path) -> None:
    session_id = "codex:proposal-normative"
    conn, _coord_path, knowledge_path = _real_session(
        tmp_path, session_id=session_id, external_thread_id="thread-proposal-normative"
    )
    try:
        _establish_decision(
            conn,
            session_id=session_id,
            ruling="Use blue buttons for primary actions.",
            memory_candidate=False,
        )
        result = _closeout(conn, session_id=session_id, knowledge_path=knowledge_path)
    finally:
        conn.close()
    assert result["memory_proposals"]["eligible"] == 0
    assert not knowledge_path.exists()


def test_blocked_closeout_runs_no_producer(tmp_path: Path) -> None:
    session_id = "codex:proposal-blocked"
    conn, _coord_path, knowledge_path = _real_session(
        tmp_path, session_id=session_id, external_thread_id="thread-proposal-blocked"
    )
    try:
        _establish_decision(
            conn, session_id=session_id, work_id="MEMORY-BLOCK", ruling="Blocked fact."
        )
        coord_db.claim_work(conn, session_id, "MEMORY-BLOCK", step="still running")
        with pytest.raises(ValueError, match="held claims"):
            _closeout(conn, session_id=session_id, knowledge_path=knowledge_path)
        state = conn.execute(
            "SELECT state FROM agent_sessions WHERE session_id=?", (session_id,)
        ).fetchone()["state"]
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind LIKE 'memory_proposal_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert state == "active"
    assert receipt_count == 0
    assert not knowledge_path.exists()


def test_empty_session_closeout_is_a_storage_noop(tmp_path: Path) -> None:
    session_id = "codex:proposal-empty"
    conn, _coord_path, knowledge_path = _real_session(
        tmp_path, session_id=session_id, external_thread_id="thread-proposal-empty"
    )
    try:
        result = _closeout(conn, session_id=session_id, knowledge_path=knowledge_path)
    finally:
        conn.close()
    assert result["memory_proposals"]["eligible"] == 0
    assert result["memory_proposals"]["emitted"] == []
    assert not knowledge_path.exists()


def test_existing_semantic_gate_rejects_duplicate(tmp_path: Path) -> None:
    ruling = "corpus_fulltext.duckdb is load-bearing for embeddings; never delete."
    knowledge_path = tmp_path / "state" / "knowledge.db"
    _insert_memory_card(knowledge_path, slug="corpus-fulltext-do-not-delete", body=ruling)
    session_id = "codex:proposal-duplicate"
    conn, _coord_path, _ = _real_session(
        tmp_path, session_id=session_id, external_thread_id="thread-proposal-duplicate"
    )
    try:
        _establish_decision(conn, session_id=session_id, work_id="MEMORY-2", ruling=ruling)
        result = _closeout(conn, session_id=session_id, knowledge_path=knowledge_path)
    finally:
        conn.close()
    production = result["memory_proposals"]
    assert production["emitted"] == []
    assert production["rejected"][0]["reason"] == "existing_memory_near_duplicate"
    assert memory_proposals.list_proposals(db_path=knowledge_path) == []


def test_producer_failure_gets_durable_receipt_and_next_closeout_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_session = "codex:proposal-failure"
    conn, coord_path, knowledge_path = _real_session(
        tmp_path, session_id=first_session, external_thread_id="thread-proposal-failure"
    )
    monkeypatch.setattr(kfts, "find_similar_memory", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gate offline")))
    try:
        _establish_decision(
            conn, session_id=first_session, work_id="MEMORY-FAIL", ruling="Replayable fact."
        )
        failed = _closeout(conn, session_id=first_session, knowledge_path=knowledge_path)
        receipt_id = failed["memory_proposals"]["failure_receipts"][0]
        receipt = conn.execute(
            "SELECT kind,payload_json FROM events WHERE event_id=?", (receipt_id,)
        ).fetchone()
    finally:
        conn.close()
    assert receipt["kind"] == "memory_proposal_failure"
    assert not knowledge_path.exists()

    monkeypatch.setattr(kfts, "find_similar_memory", lambda *_args, **_kwargs: [])
    second_session = "codex:proposal-replay"
    conn = connect(coord_path)
    coord_db.register_session(conn, second_session, "codex", lease_s=600)
    try:
        replayed = _closeout(conn, session_id=second_session, knowledge_path=knowledge_path)
    finally:
        conn.close()
    replay = replayed["memory_proposals"]["replay"]
    assert replay["attempted"] == 1
    assert replay["resolved"][0]["failure_event_id"] == receipt_id
    assert replay["resolved"][0]["outcome"]["status"] == "emitted"
    assert len(memory_proposals.list_proposals(db_path=knowledge_path)) == 1


def test_candidate_replay_stale_resolves_a_superseded_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_session = "codex:candidate-stale-source"
    conn, coord_path, knowledge_path = _real_session(
        tmp_path, session_id=first_session, external_thread_id="thread-candidate-stale"
    )
    monkeypatch.setattr(
        kfts,
        "find_similar_memory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("gate offline")),
    )
    try:
        original_event_id = _establish_decision(
            conn,
            session_id=first_session,
            work_id="MEMORY-STALE-CANDIDATE",
            ruling="This fact will be superseded before replay.",
        )
        failed = _closeout(conn, session_id=first_session, knowledge_path=knowledge_path)
        receipt_id = failed["memory_proposals"]["failure_receipts"][0]
    finally:
        conn.close()

    monkeypatch.setattr(kfts, "find_similar_memory", lambda *_args, **_kwargs: [])
    second_session = "codex:candidate-stale-replay"
    conn = connect(coord_path)
    coord_db.register_session(conn, second_session, "codex", lease_s=600)
    try:
        coord_db.post_decision_event(
            conn,
            ruling="The prior fact is no longer current.",
            actor="codex",
            session_id=second_session,
            work_id="MEMORY-STALE-CANDIDATE",
            supersedes_event_id=original_event_id,
            memory_candidate=False,
        )
        replayed = _closeout(conn, session_id=second_session, knowledge_path=knowledge_path)
    finally:
        conn.close()

    replay = replayed["memory_proposals"]["replay"]["resolved"][0]
    assert replay["failure_event_id"] == receipt_id
    assert replay["outcome"] == {
        "status": "stale",
        "event_id": original_event_id,
        "reason": "decision_superseded_or_no_longer_current",
    }
    assert not knowledge_path.exists()


def test_session_scan_replay_intersects_stored_ids_with_current_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordharness.knowledge import proposal_producer

    first_session = "codex:scan-stale-source"
    conn, coord_path, knowledge_path = _real_session(
        tmp_path, session_id=first_session, external_thread_id="thread-scan-stale"
    )
    original_producer = proposal_producer.produce_session_memory_proposals
    monkeypatch.setattr(
        proposal_producer,
        "produce_session_memory_proposals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("producer offline")),
    )
    try:
        original_event_id = _establish_decision(
            conn,
            session_id=first_session,
            work_id="MEMORY-STALE-SCAN",
            ruling="This session-scan fact will be superseded.",
        )
        failed = _closeout(conn, session_id=first_session, knowledge_path=knowledge_path)
        receipt_id = failed["memory_proposals"]["failure_receipts"][0]
    finally:
        conn.close()

    monkeypatch.setattr(proposal_producer, "produce_session_memory_proposals", original_producer)
    second_session = "codex:scan-stale-replay"
    conn = connect(coord_path)
    coord_db.register_session(conn, second_session, "codex", lease_s=600)
    try:
        coord_db.post_decision_event(
            conn,
            ruling="The session-scan predecessor is no longer current.",
            actor="codex",
            session_id=second_session,
            work_id="MEMORY-STALE-SCAN",
            supersedes_event_id=original_event_id,
            memory_candidate=False,
        )
        replayed = _closeout(conn, session_id=second_session, knowledge_path=knowledge_path)
    finally:
        conn.close()

    replay = replayed["memory_proposals"]["replay"]["resolved"][0]
    assert replay["failure_event_id"] == receipt_id
    assert replay["outcome"]["status"] == "batch_resolved"
    assert replay["outcome"]["stale_decision_ids"] == [original_event_id]
    assert replay["outcome"]["production"]["eligible"] == 0
    assert not knowledge_path.exists()


def test_mcp_server_bound_knowledge_db_reaches_closeout(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from coordharness.coord.mcp_coord_server import build_server

    coord_path = tmp_path / "mcp-state" / "coord.db"
    knowledge_path = tmp_path / "mcp-state" / "custom-knowledge.db"
    server = build_server(str(coord_path), knowledge_db=knowledge_path)
    decision = server._tool_manager.get_tool("decision").fn
    closeout = server._tool_manager.get_tool("session_closeout").fn
    decision(
        ruling="MCP-bound durable fact.",
        actor="codex",
        session_id="codex:mcp-memory",
        memory_candidate=True,
    )
    result = closeout(
        summary="MCP custom knowledge binding.",
        actor="codex",
        session_id="codex:mcp-memory",
        ack_dirty=True,
    )
    assert result["memory_proposals"]["eligible"] == 1
    assert len(memory_proposals.list_proposals(db_path=knowledge_path)) == 1
