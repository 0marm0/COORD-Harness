"""Produce review-queue memory proposals from explicitly marked durable facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from coordharness import config as harness_config
from coordharness.knowledge import kfts, memory_proposals


MEMORY_CANDIDATE_SCHEMA = "coordharness.memory-candidate.v1"
COORD_DB_BINDING_SCHEMA = "coordharness.coord-db-binding.v1"
COORD_EVENT_EVIDENCE_SCHEMA = "coordharness.coord-event-evidence.v1"


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _clean_session_ids(session_ids: Iterable[str]) -> list[str]:
    return sorted({str(value or "").strip() for value in session_ids if str(value or "").strip()})


def coordination_database_binding(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return an opaque exact-file binding plus a portable logical reference."""

    row = next(
        (entry for entry in conn.execute("PRAGMA database_list") if str(entry[1]) == "main"),
        None,
    )
    raw_path = str(row[2] if row is not None else "").strip()
    path = Path(raw_path).resolve(strict=False) if raw_path else None
    stat = path.stat() if path is not None and path.exists() else None
    database_ref = harness_config.public_path_ref(path) if path is not None else "memory://coord.db"
    exact_material = {
        "resolved_path": str(path) if path is not None else ":memory:",
        "device": int(stat.st_dev) if stat is not None else None,
        "inode": int(stat.st_ino) if stat is not None else None,
        "deployment_profile": harness_config.deployment_profile(),
    }
    return {
        "schema": COORD_DB_BINDING_SCHEMA,
        "database_ref": database_ref,
        "database_binding_sha256": _canonical_sha256(exact_material),
        "deployment_profile": harness_config.deployment_profile(),
    }


def _source_thread_id(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_ids: list[str],
) -> str:
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"SELECT session_id, external_thread_id FROM agent_sessions"
        f" WHERE session_id IN ({placeholders})",
        session_ids,
    ).fetchall()
    external_by_session = {
        str(row["session_id"]): str(row["external_thread_id"] or "").strip() for row in rows
    }
    return external_by_session.get(session_id) or next(
        (external_by_session[sid] for sid in session_ids if external_by_session.get(sid)),
        session_id,
    )


def _is_typed_fact_candidate(payload: dict[str, Any]) -> bool:
    return payload.get("memory_candidate") == {
        "schema": MEMORY_CANDIDATE_SCHEMA,
        "kind": "fact",
    }


def _decision_candidates(
    conn: sqlite3.Connection,
    *,
    session_ids: list[str],
    current_decision_ids: set[int],
    source_thread_id: str,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"SELECT event_id, session_id, actor, work_id, refs_json, payload_json"
        f" FROM events WHERE kind='decision' AND session_id IN ({placeholders})"
        " ORDER BY event_id",
        session_ids,
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        event_id = int(row["event_id"])
        if event_id not in current_decision_ids:
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not _is_typed_fact_candidate(payload):
            continue
        ruling = str(payload.get("ruling") or "").strip()
        if not ruling:
            continue
        try:
            refs = json.loads(row["refs_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            refs = []
        if not isinstance(refs, list):
            refs = []
        candidates.append(
            {
                "schema": MEMORY_CANDIDATE_SCHEMA,
                "kind": "fact",
                "event_id": event_id,
                "session_id": str(row["session_id"] or "").strip(),
                "actor": str(row["actor"] or "").strip().lower(),
                "source_thread_id": source_thread_id,
                "work_id": str(row["work_id"] or "").strip() or None,
                "statement": ruling,
                "decision_scope": str(payload.get("scope") or "global").strip(),
                "binds": [str(value) for value in payload.get("binds") or []],
                "refs": [str(value) for value in refs],
            }
        )
    return candidates


def _proposal_scope(candidate: dict[str, Any]) -> str:
    if candidate.get("work_id"):
        return "work"
    if str(candidate.get("decision_scope") or "") == "global":
        return "global"
    return "project"


def _evidence_contract(
    candidate: dict[str, Any],
    coordination_source: dict[str, Any],
) -> dict[str, Any]:
    event_receipt = {
        "event_id": int(candidate["event_id"]),
        "session_id": candidate["session_id"],
        "actor": candidate["actor"],
        "work_id": candidate.get("work_id"),
        "statement": candidate["statement"],
        "decision_scope": candidate["decision_scope"],
        "refs": candidate["refs"],
        "coord_db_binding_sha256": coordination_source["database_binding_sha256"],
    }
    return {
        "schema": COORD_EVENT_EVIDENCE_SCHEMA,
        "coord_db": dict(coordination_source),
        "event_id": int(candidate["event_id"]),
        "event_receipt_sha256": _canonical_sha256(event_receipt),
    }


def produce_memory_candidate(
    candidate: dict[str, Any],
    *,
    db_path: Path | str,
    coordination_source: dict[str, Any],
) -> dict[str, Any]:
    """Attempt one already-fenced typed candidate without hiding failures."""

    event_id = int(candidate["event_id"])
    try:
        duplicate_hits = kfts.find_similar_memory(candidate["statement"], db_path=db_path)
    except Exception as exc:
        return {
            "status": "failed",
            "event_id": event_id,
            "reason": "dedup_gate_failed",
            "detail": str(exc),
            "candidate": candidate,
            "coordination_source": coordination_source,
        }
    if duplicate_hits:
        return {
            "status": "rejected",
            "event_id": event_id,
            "reason": "existing_memory_near_duplicate",
            "matches": duplicate_hits,
        }

    evidence = _evidence_contract(candidate, coordination_source)
    provenance = {
        "producer": "coordharness.knowledge.proposal_producer",
        "source": "session_closeout",
        "evidence": evidence,
        "source_event_id": event_id,
        "source_event_kind": "decision",
        "source_session_id": candidate["session_id"],
        "authoritative_scope": candidate["decision_scope"],
        "binds": candidate["binds"],
        "refs": candidate["refs"],
    }
    try:
        proposal = memory_proposals.propose_memory(
            kind="fact",
            statement=candidate["statement"],
            scope=_proposal_scope(candidate),
            evidence_pointer=None,
            provenance=provenance,
            tags=["session-established", "typed-durable-fact"],
            source_actor=candidate["actor"] or None,
            source_thread_id=candidate["source_thread_id"],
            source_work_id=candidate.get("work_id"),
            db_path=db_path,
        )
    except ValueError as exc:
        return {
            "status": "rejected",
            "event_id": event_id,
            "reason": "proposal_store_rejected",
            "detail": str(exc),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "event_id": event_id,
            "reason": "proposal_store_failed",
            "detail": str(exc),
            "candidate": candidate,
            "coordination_source": coordination_source,
        }
    return {
        "status": "emitted",
        "event_id": event_id,
        "proposal_id": proposal.id,
        "seen_count": proposal.seen_count,
    }


def produce_session_memory_proposals(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_ids: Iterable[str],
    db_path: Path | str,
    current_decision_ids: Iterable[int],
) -> dict[str, Any]:
    """Propose current decisions explicitly marked as durable facts."""

    primary_session_id = str(session_id or "").strip()
    family = _clean_session_ids(session_ids)
    if primary_session_id and primary_session_id not in family:
        family.append(primary_session_id)
        family.sort()
    if not primary_session_id or not family:
        return {"eligible": 0, "emitted": [], "rejected": [], "failed": []}

    thread_id = _source_thread_id(
        conn,
        session_id=primary_session_id,
        session_ids=family,
    )
    candidates = _decision_candidates(
        conn,
        session_ids=family,
        current_decision_ids={int(value) for value in current_decision_ids},
        source_thread_id=thread_id,
    )
    coordination_source = coordination_database_binding(conn)
    result: dict[str, Any] = {
        "eligible": len(candidates),
        "emitted": [],
        "rejected": [],
        "failed": [],
    }
    for candidate in candidates:
        outcome = produce_memory_candidate(
            candidate,
            db_path=db_path,
            coordination_source=coordination_source,
        )
        result[{"emitted": "emitted", "rejected": "rejected"}.get(outcome["status"], "failed")].append(
            {key: value for key, value in outcome.items() if key != "status"}
        )
    return result
