
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from coordharness.knowledge import facts
from coordharness.knowledge.kfts import DEFAULT_INDEX_DB


KINDS = ("correction", "preference", "fact", "procedure", "context")
STATUSES = ("proposed", "accepted", "rejected", "parked", "superseded")
SCOPES = ("project", "agent", "global", "work")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_proposals (
    id               TEXT PRIMARY KEY,
    content_hash     TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL CHECK (kind IN ('correction','preference','fact','procedure','context')),
    statement        TEXT NOT NULL,
    value            TEXT,
    confidence       REAL NOT NULL,
    scope            TEXT NOT NULL CHECK (scope IN ('project','agent','global','work')),
    status           TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected','parked','superseded')),
    evidence_pointer TEXT,
    provenance_json  TEXT NOT NULL,
    tags_json        TEXT NOT NULL,
    source_actor     TEXT,
    source_thread_id TEXT,
    source_work_id   TEXT,
    seen_count       INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    reviewed_by      TEXT,
    reviewed_at      TEXT,
    review_note      TEXT
);
CREATE INDEX IF NOT EXISTS ix_memory_proposals_status_updated ON memory_proposals(status, updated_at);
CREATE INDEX IF NOT EXISTS ix_memory_proposals_kind_status ON memory_proposals(kind, status);
CREATE INDEX IF NOT EXISTS ix_memory_proposals_source_work ON memory_proposals(source_work_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("provenance_json must decode to an object")
    return parsed


def _json_loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("tags_json must decode to a list")
    return [str(item) for item in parsed]


def _boundary_checked_path(db_path: Path | str | None) -> Path:
    path = Path(db_path) if db_path else DEFAULT_INDEX_DB
    resolved = str(path.resolve())
    from coordharness.coord.config import _WAREHOUSE_MARKERS

    for marker in _WAREHOUSE_MARKERS:
        if f"/{marker}/" in resolved:
            raise RuntimeError(f"memory proposal queue {resolved!r} must stay outside the warehouse")
    return path


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = _boundary_checked_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_SQL)
    return conn


def _normalize_text(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _default_confidence(kind: str) -> float:
    if kind == "correction":
        return 0.95
    if kind == "preference":
        return 0.90
    return 0.80


def _content_hash(
    *,
    kind: str,
    statement: str,
    value: str | None,
    scope: str,
    source_work_id: str | None,
) -> str:
    payload = {
        "kind": kind,
        "scope": scope,
        "source_work_id": _normalize_text(source_work_id).lower() or None,
        "statement": _normalize_text(statement).lower(),
        "value": _normalize_text(value).lower() or None,
    }
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _proposal_id(content_hash: str) -> str:
    return f"memory-proposal-{content_hash[:16]}"


@dataclass(frozen=True)
class MemoryProposal:
    id: str
    content_hash: str
    kind: str
    statement: str
    value: str | None
    confidence: float
    scope: str
    status: str
    evidence_pointer: str | None
    provenance: dict[str, Any]
    tags: list[str]
    source_actor: str | None
    source_thread_id: str | None
    source_work_id: str | None
    seen_count: int
    created_at: str
    updated_at: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryProposal":
        data = dict(row)
        return cls(
            id=data["id"],
            content_hash=data["content_hash"],
            kind=data["kind"],
            statement=data["statement"],
            value=data["value"],
            confidence=float(data["confidence"]),
            scope=data["scope"],
            status=data["status"],
            evidence_pointer=data["evidence_pointer"],
            provenance=_json_loads_object(data["provenance_json"]),
            tags=_json_loads_list(data["tags_json"]),
            source_actor=data["source_actor"],
            source_thread_id=data["source_thread_id"],
            source_work_id=data["source_work_id"],
            seen_count=int(data["seen_count"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            reviewed_by=data["reviewed_by"],
            reviewed_at=data["reviewed_at"],
            review_note=data["review_note"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def init_db(db_path: Path | str | None = None) -> dict[str, str]:
    conn = _conn(db_path)
    try:
        conn.commit()
        return {"db": str(_boundary_checked_path(db_path).resolve())}
    finally:
        conn.close()


def propose_memory(
    *,
    kind: str,
    statement: str,
    value: str | None = None,
    confidence: float | None = None,
    scope: str = "project",
    evidence_pointer: str | None = None,
    provenance: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source_actor: str | None = None,
    source_thread_id: str | None = None,
    source_work_id: str | None = None,
    db_path: Path | str | None = None,
) -> MemoryProposal:
    kind = kind.strip().lower()
    scope = scope.strip().lower()
    statement = _normalize_text(statement)
    value = _normalize_text(value) or None
    evidence_pointer = _normalize_text(evidence_pointer) or None
    provenance = dict(provenance or {})
    tags = sorted({_normalize_text(tag) for tag in (tags or []) if _normalize_text(tag)})
    if kind not in KINDS:
        raise ValueError(f"bad kind {kind!r}; allowed: {KINDS}")
    if scope not in SCOPES:
        raise ValueError(f"bad scope {scope!r}; allowed: {SCOPES}")
    if not statement:
        raise ValueError("statement is required")
    if not evidence_pointer and not provenance:
        raise ValueError("memory proposals require evidence_pointer or provenance")
    conf = _default_confidence(kind) if confidence is None else float(confidence)
    if not 0.0 <= conf <= 1.0:
        raise ValueError("confidence must be between 0 and 1")

    content_hash = _content_hash(
        kind=kind,
        statement=statement,
        value=value,
        scope=scope,
        source_work_id=source_work_id,
    )
    proposal_id = _proposal_id(content_hash)
    now = _now()
    conn = _conn(db_path)
    try:
        if source_thread_id:
            exists = conn.execute(
                "SELECT 1 FROM memory_proposals WHERE content_hash=?", (content_hash,)
            ).fetchone()
            if exists is None:
                recent = conn.execute(
                    "SELECT COUNT(*) FROM memory_proposals"
                    " WHERE source_thread_id=? AND created_at >= datetime('now','-1 day')",
                    (source_thread_id,),
                ).fetchone()[0]
                if recent >= 5:
                    raise ValueError(
                        "per-session proposal cap (5/24h) reached for"
                        f" source_thread_id={source_thread_id!r}; review the queue before proposing more"
                    )
        conn.execute(
            """
            INSERT INTO memory_proposals(
                id, content_hash, kind, statement, value, confidence, scope,
                status, evidence_pointer, provenance_json, tags_json,
                source_actor, source_thread_id, source_work_id,
                seen_count, created_at, updated_at
            )
            VALUES (?,?,?,?,?,?,?,'proposed',?,?,?,?,?,?,1,?,?)
            ON CONFLICT(content_hash) DO UPDATE SET
                updated_at=excluded.updated_at,
                seen_count=memory_proposals.seen_count + 1,
                evidence_pointer=COALESCE(memory_proposals.evidence_pointer, excluded.evidence_pointer),
                provenance_json=excluded.provenance_json,
                tags_json=excluded.tags_json,
                source_actor=COALESCE(memory_proposals.source_actor, excluded.source_actor),
                source_thread_id=COALESCE(memory_proposals.source_thread_id, excluded.source_thread_id),
                source_work_id=COALESCE(memory_proposals.source_work_id, excluded.source_work_id)
            """,
            (
                proposal_id,
                content_hash,
                kind,
                statement,
                value,
                conf,
                scope,
                evidence_pointer,
                _json_dumps(provenance),
                _json_dumps(tags),
                source_actor,
                source_thread_id,
                source_work_id,
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memory_proposals WHERE content_hash=?", (content_hash,)).fetchone()
        if row is None:
            raise RuntimeError("memory proposal insert failed")
        return MemoryProposal.from_row(row)
    finally:
        conn.close()


def list_proposals(
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[MemoryProposal]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        status = status.strip().lower()
        if status not in STATUSES:
            raise ValueError(f"bad status {status!r}; allowed: {STATUSES}")
        clauses.append("status=?")
        params.append(status)
    if kind:
        kind = kind.strip().lower()
        if kind not in KINDS:
            raise ValueError(f"bad kind {kind!r}; allowed: {KINDS}")
        clauses.append("kind=?")
        params.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM memory_proposals{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [MemoryProposal.from_row(row) for row in rows]
    finally:
        conn.close()


def get_proposal(id: str, *, db_path: Path | str | None = None) -> MemoryProposal | None:
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM memory_proposals WHERE id=?", (id,)).fetchone()
        return MemoryProposal.from_row(row) if row else None
    finally:
        conn.close()


def review_proposal(
    id: str,
    *,
    status: str,
    reviewer: str,
    note: str | None = None,
    db_path: Path | str | None = None,
) -> MemoryProposal:
    status = status.strip().lower()
    reviewer = _normalize_text(reviewer)
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}; allowed: {STATUSES}")
    if status == "proposed":
        raise ValueError("review status must be terminal/reviewed, not proposed")
    if not reviewer:
        raise ValueError("reviewer is required")
    now = _now()
    conn = _conn(db_path)
    try:
        src = conn.execute(
            "SELECT source_actor FROM memory_proposals WHERE id=?", (id,)
        ).fetchone()
        if src is None:
            raise KeyError(f"unknown memory proposal: {id}")
        source_actor = (src[0] or "").strip().lower()
        if source_actor and reviewer.lower() == source_actor:
            raise ValueError(
                f"reviewer {reviewer!r} may not review its own proposal (source_actor={source_actor!r})"
            )
        cur = conn.execute(
            """
            UPDATE memory_proposals
               SET status=?, reviewed_by=?, reviewed_at=?, review_note=?, updated_at=?
             WHERE id=?
            """,
            (status, reviewer, now, _normalize_text(note) or None, now, id),
        )
        if cur.rowcount != 1:
            raise KeyError(f"unknown memory proposal: {id}")
        conn.commit()
        row = conn.execute("SELECT * FROM memory_proposals WHERE id=?", (id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown memory proposal: {id}")
        return MemoryProposal.from_row(row)
    finally:
        conn.close()


def fact_draft_from_proposal(proposal: MemoryProposal, *, owner_lane: str = "shared") -> dict[str, Any]:
    if proposal.kind not in {"correction", "fact", "preference"}:
        raise ValueError(f"{proposal.kind!r} proposals are not fact-index drafts")
    return {
        "statement": proposal.statement,
        "value": proposal.value,
        "unit": "memory",
        "status": "live" if proposal.status == "accepted" else "parked",
        "module": "harness_memory",
        "evidence_pointer": proposal.evidence_pointer,
        "owner_lane": owner_lane,
        "notes": _json_dumps(
            {
                "memory_proposal_id": proposal.id,
                "kind": proposal.kind,
                "scope": proposal.scope,
                "confidence": proposal.confidence,
                "provenance": proposal.provenance,
            }
        ),
    }


def promote_proposal_to_fact(
    id: str,
    *,
    owner_lane: str = "shared",
    db_path: Path | str | None = None,
    fact_id: str | None = None,
) -> facts.Fact:
    proposal = get_proposal(id, db_path=db_path)
    if proposal is None:
        raise KeyError(f"unknown memory proposal: {id}")
    if proposal.status != "accepted":
        raise ValueError(f"memory proposal {id} is {proposal.status!r}; accept it before promotion")
    draft = fact_draft_from_proposal(proposal, owner_lane=owner_lane)
    return facts.upsert_fact(id=fact_id, db_path=db_path, **draft)
