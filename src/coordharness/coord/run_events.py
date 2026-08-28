
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from coordharness.coord import coord_db


ALLOWED_CATEGORIES = {
    "lifecycle",
    "message",
    "tool",
    "token",
    "middleware",
    "artifact",
    "trace",
    "error",
    "security",
}

DECLARED_INDEXES = [
    "ux_run_events_thread_seq",
    "ix_run_events_thread_category_seq",
    "ix_run_events_run_seq",
    "ix_run_events_work_run_seq",
    "ix_run_events_category_type_created",
    "ux_run_events_idempotency",
]
MAX_EVENT_JSON_BYTES = int(os.environ.get("COORD_RUNEVENT_JSON_BYTE_LIMIT", "12000"))
PRESERVED_OVERSIZE_KEYS = (
    "adapter",
    "call_id",
    "caller",
    "id",
    "idempotency_key",
    "logical_usage_id",
    "measurement_kind",
    "model",
    "source",
    "source_reliability",
    "tokens",
    "tool_call_id",
    "tool_name",
    "usage_id",
)

_DDL = """
CREATE TABLE IF NOT EXISTS run_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  work_id         TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  thread_id       TEXT,
  session_id      TEXT,
  seq             INTEGER NOT NULL,
  category        TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  content_json    TEXT NOT NULL DEFAULT '{}',
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT,
  created_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_events_thread_seq
  ON run_events(thread_id, seq) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_run_events_thread_category_seq
  ON run_events(thread_id, category, seq);
CREATE INDEX IF NOT EXISTS ix_run_events_run_seq
  ON run_events(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_run_events_work_run_seq
  ON run_events(work_id, run_id, seq);
CREATE INDEX IF NOT EXISTS ix_run_events_category_type_created
  ON run_events(category, event_type, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_events_idempotency
  ON run_events(idempotency_key) WHERE idempotency_key IS NOT NULL;
"""


def _flag_enabled() -> bool:
    raw = str(os.environ.get("COORD_RUNEVENTSTORE_V2", "1") or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _writer_enabled_and_source(enabled: bool | None = None) -> tuple[bool, str]:
    if enabled is not None:
        return bool(enabled), "explicit"
    raw = os.environ.get("COORD_RUNEVENTSTORE_V2")
    if raw is not None and str(raw).strip():
        return _flag_enabled(), "env:COORD_RUNEVENTSTORE_V2"
    return True, "default_on"


def canonical_run_id(run_id: str | None) -> str | None:
    raw = str(run_id or "").strip()
    if not raw:
        return None
    sidecar_prefix = "run:sidecar:"
    if raw.startswith(sidecar_prefix):
        suffix = raw[len(sidecar_prefix) :].strip()
        return f"job:{suffix}" if suffix else raw
    return raw


def run_id_aliases(run_id: str | None) -> tuple[str, ...]:
    raw = str(run_id or "").strip()
    if not raw:
        return ()
    aliases = {raw}
    sidecar_prefix = "run:sidecar:"
    job_prefix = "job:"
    if raw.startswith(sidecar_prefix):
        suffix = raw[len(sidecar_prefix) :].strip()
        if suffix:
            aliases.add(f"job:{suffix}")
    elif raw.startswith(job_prefix):
        suffix = raw[len(job_prefix) :].strip()
        if suffix:
            aliases.add(f"run:sidecar:{suffix}")
    return tuple(sorted(aliases))


def run_event_writer_contract(
    conn: sqlite3.Connection,
    *,
    enabled: bool | None = None,
) -> dict[str, Any]:
    writer_enabled, enabled_source = _writer_enabled_and_source(enabled)
    table_present = run_events_table_exists(conn)
    return {
        "schema_version": 1,
        "mode": "run_events_writer_contract",
        "read_only_report": True,
        "writer_enabled": writer_enabled,
        "enabled_source": enabled_source,
        "env_flag": "COORD_RUNEVENTSTORE_V2",
        "table_present": table_present,
        "would_initialize_schema_on_write": bool(writer_enabled and not table_present),
        "authority": "append_only_evidence",
        "replaces_coord_events": False,
        "writes_coord_events": False,
        "mutates_work_items": False,
        "mutates_lifecycle_state": False,
        "allowed_categories": sorted(ALLOWED_CATEGORIES),
        "declared_indexes": list(DECLARED_INDEXES),
    }


def _json(value: Any) -> str:
    if value is None:
        value = {}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_bytes(value: Any) -> int:
    return len(_json(value).encode("utf-8"))


def bound_event_payload(
    value: Any,
    *,
    field: str = "payload",
    byte_limit: int = MAX_EVENT_JSON_BYTES,
) -> Any:
    if value is None:
        value = {}
    raw = _json(value)
    raw_bytes = len(raw.encode("utf-8"))
    if raw_bytes <= byte_limit:
        return value
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if isinstance(value, dict):
        preserved = {
            key: value[key]
            for key in PRESERVED_OVERSIZE_KEYS
            if key in value and _json_bytes(value[key]) <= max(512, byte_limit // 4)
        }
        return {
            **preserved,
            "_truncated": True,
            "_truncation_field": field,
            "_original_bytes": raw_bytes,
            "_sha256": digest,
            "_keys_sample": sorted(str(key) for key in value.keys())[:50],
        }
    if isinstance(value, list):
        return {
            "_truncated": True,
            "_truncation_field": field,
            "_original_bytes": raw_bytes,
            "_sha256": digest,
            "_list_length": len(value),
            "items_sample": [str(item)[:200] for item in value[:5]],
        }
    text = str(value)
    return {
        "_truncated": True,
        "_truncation_field": field,
        "_original_bytes": raw_bytes,
        "_sha256": digest,
        "text_sample": text[: min(1000, max(0, byte_limit // 2))],
    }


def _storage_json(value: Any, *, field: str) -> str:
    return _json(bound_event_payload(value, field=field))


@dataclass(frozen=True)
class RunEventInput:
    work_id: str
    run_id: str
    category: str
    event_type: str
    content: Any | None = None
    metadata: Any | None = None
    thread_id: str | None = None
    session_id: str | None = None
    idempotency_key: str | None = None


class RunEventStore(Protocol):
    def append_event(self, event: RunEventInput) -> int | None:
        pass

    def append_batch(self, events: list[RunEventInput]) -> list[int | None]:
        pass

    def query_events(
        self,
        *,
        run_id: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        pass


class NoopRunEventStore:
    def append_event(self, event: RunEventInput) -> int | None:
        return None

    def append_batch(self, events: list[RunEventInput]) -> list[int | None]:
        return [None for _event in events]

    def query_events(
        self,
        *,
        run_id: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return []


class MemoryRunEventStore:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._next_id = 1
        self._seq_by_scope: dict[tuple[str, str | None], int] = {}

    def append_event(self, event: RunEventInput) -> int | None:
        event_id = self._next_id
        self._next_id += 1
        scope = (event.thread_id or event.run_id, event.thread_id)
        seq = self._seq_by_scope.get(scope, 0) + 1
        self._seq_by_scope[scope] = seq
        self._rows.append(
            {
                "id": event_id,
                "work_id": event.work_id,
                "run_id": event.run_id,
                "thread_id": event.thread_id,
                "session_id": event.session_id,
                "seq": seq,
                "category": event.category,
                "event_type": event.event_type,
                "content_json": _storage_json(event.content, field="content"),
                "metadata_json": _storage_json(event.metadata, field="metadata"),
                "idempotency_key": event.idempotency_key,
                "created_at": float(event_id),
            }
        )
        return event_id

    def append_batch(self, events: list[RunEventInput]) -> list[int | None]:
        return [self.append_event(event) for event in events]

    def query_events(
        self,
        *,
        run_id: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = list(self._rows)
        if run_id:
            aliases = set(run_id_aliases(run_id))
            rows = [row for row in rows if row.get("run_id") in aliases]
        if category:
            rows = [row for row in rows if row.get("category") == category]
        if event_type:
            rows = [row for row in rows if row.get("event_type") == event_type]
        if after_id is not None:
            rows = [row for row in rows if int(row.get("id") or 0) > int(after_id)]
        rows = sorted(rows, key=lambda row: int(row.get("id") or 0))
        return [dict(row) for row in rows[: max(0, int(limit))]]


class SQLiteRunEventStore:
    def __init__(self, conn: Any, *, enabled: bool | None = None) -> None:
        self.conn = conn
        self.enabled = enabled

    def append_event(self, event: RunEventInput) -> int | None:
        return record_run_event(
            self.conn,
            work_id=event.work_id,
            run_id=event.run_id,
            thread_id=event.thread_id,
            session_id=event.session_id,
            category=event.category,
            event_type=event.event_type,
            content=event.content,
            metadata=event.metadata,
            idempotency_key=event.idempotency_key,
            enabled=self.enabled,
        )

    def append_batch(self, events: list[RunEventInput]) -> list[int | None]:
        return [self.append_event(event) for event in events]

    def query_events(
        self,
        *,
        run_id: str | None = None,
        category: str | None = None,
        event_type: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return list_run_events(
            self.conn,
            run_id=run_id,
            category=category,
            event_type=event_type,
            after_id=after_id,
            limit=limit,
        )


def run_events_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_events'"
    ).fetchone()
    return row is not None


def ensure_run_events_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def _validate_event_args(*, work_id: str, run_id: str, category: str, event_type: str) -> None:
    if not str(work_id or "").strip():
        raise ValueError("work_id is required")
    if not str(run_id or "").strip():
        raise ValueError("run_id is required")
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(ALLOWED_CATEGORIES)}")
    if not str(event_type or "").strip():
        raise ValueError("event_type is required")


def _next_seq(conn: sqlite3.Connection, *, run_id: str, thread_id: str | None) -> int:
    if thread_id:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=? AND thread_id IS NULL",
            (run_id,),
        ).fetchone()
    return int(row[0])


def record_run_event(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    category: str,
    event_type: str,
    content: Any | None = None,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    enabled: bool | None = None,
) -> int | None:
    if enabled is None:
        enabled, _source = _writer_enabled_and_source()
    if not enabled:
        return None
    _validate_event_args(work_id=work_id, run_id=run_id, category=category, event_type=event_type)
    ensure_run_events_schema(conn)
    idempotency_key = str(idempotency_key).strip() if idempotency_key else None
    with coord_db.tx(conn):
        if idempotency_key:
            existing = conn.execute(
                "SELECT id FROM run_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"] if isinstance(existing, sqlite3.Row) else existing[0])
        seq = _next_seq(conn, run_id=run_id, thread_id=thread_id)
        cur = conn.execute(
            "INSERT INTO run_events(work_id, run_id, thread_id, session_id, seq, category,"
            " event_type, content_json, metadata_json, idempotency_key, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                work_id,
                run_id,
                thread_id,
                session_id,
                seq,
                category,
                event_type,
                _storage_json(content, field="content"),
                _storage_json(metadata, field="metadata"),
                idempotency_key,
                coord_db.db_now(conn),
            ),
        )
        return int(cur.lastrowid)


def record_tool_event(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    tool_name: str,
    phase: str,
    content: Any | None = None,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    enabled: bool | None = None,
) -> int | None:
    payload = {"tool_name": tool_name, **(content if isinstance(content, dict) else {"content": content})}
    return record_run_event(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        category="tool",
        event_type=f"tool.{str(phase or '').strip() or 'event'}",
        content=payload,
        metadata=metadata,
        idempotency_key=idempotency_key,
        enabled=enabled,
    )


def coord_lifecycle_idempotency_key(
    *,
    work_id: str,
    session_id: str,
    verb: str,
    action: str,
    source_event_id: int | str | None = None,
    claim_id: str | None = None,
) -> str:
    payload = _json(
        {
            "action": _required_text(action, "action"),
            "claim_id": str(claim_id or ""),
            "session_id": _required_text(session_id, "session_id"),
            "source_event_id": str(source_event_id or ""),
            "verb": _required_text(verb, "verb"),
            "work_id": _required_text(work_id, "work_id"),
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"coord_lifecycle:v1:{digest}"


def record_coord_lifecycle_event(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    session_id: str,
    actor: str,
    verb: str,
    action: str,
    status: str,
    run_id: str | None = None,
    step: str | None = None,
    claim_id: str | None = None,
    source: str = "coord",
    source_event_id: int | str | None = None,
    metadata: Any | None = None,
    enabled: bool | None = None,
) -> int | None:
    work_id = _required_text(work_id, "work_id")
    session_id = _required_text(session_id, "session_id")
    verb = _required_text(verb, "verb")
    action = _required_text(action, "action")
    run_id = run_id or f"coord:{session_id}:{work_id}"
    content = {
        "actor": _required_text(actor, "actor"),
        "work_id": work_id,
        "verb": verb,
        "action": action,
        "status": _required_text(status, "status"),
        "claim_id": claim_id,
        "source": source,
        "source_event_id": source_event_id,
        "step": str(step or "")[:300],
    }
    event_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    event_metadata.update(
        {
            "adapter": "coord_lifecycle.v1",
            "append_only_evidence": True,
            "replaces_coord_events": False,
            "mutates_lifecycle_state": False,
        }
    )
    return record_run_event(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=session_id,
        session_id=session_id,
        category="lifecycle",
        event_type=f"coord.{verb}",
        content=content,
        metadata=event_metadata,
        idempotency_key=coord_lifecycle_idempotency_key(
            work_id=work_id,
            session_id=session_id,
            verb=verb,
            action=action,
            source_event_id=source_event_id,
            claim_id=claim_id,
        ),
        enabled=enabled,
    )


def record_token_usage(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    model: str,
    caller: str,
    tokens: int,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    enabled: bool | None = None,
) -> int | None:
    return record_run_event(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        category="token",
        event_type="token.usage",
        content={"model": model, "caller": caller, "tokens": int(tokens)},
        metadata=metadata,
        idempotency_key=idempotency_key,
        enabled=enabled,
    )


def _required_text(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def token_usage_idempotency_key(*, work_id: str, source: str, logical_usage_id: str) -> str:
    payload = _json(
        {
            "logical_usage_id": _required_text(logical_usage_id, "logical_usage_id"),
            "source": _required_text(source, "source"),
            "work_id": _required_text(work_id, "work_id"),
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"token_usage:v1:{digest}"


def record_measured_token_usage(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    caller: str,
    model: str,
    tokens: int,
    source: str,
    logical_usage_id: str,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    source_reliability: str = "exact",
    enabled: bool | None = None,
) -> dict[str, Any]:
    work_id = _required_text(work_id, "work_id")
    run_id = _required_text(run_id, "run_id")
    caller = _required_text(caller, "caller")
    model = _required_text(model, "model")
    source = _required_text(source, "source")
    logical_usage_id = _required_text(logical_usage_id, "logical_usage_id")
    if int(tokens) <= 0:
        raise ValueError(f"tokens must be > 0, got {tokens}")
    if str(source_reliability or "").strip().lower() != "exact":
        raise ValueError("record_measured_token_usage only accepts measured exact token usage")

    idempotency_key = token_usage_idempotency_key(
        work_id=work_id,
        source=source,
        logical_usage_id=logical_usage_id,
    )
    if metadata is None:
        adapter_metadata: dict[str, Any] = {}
    elif isinstance(metadata, dict):
        adapter_metadata = dict(metadata)
    else:
        adapter_metadata = {"extra_metadata": metadata}
    adapter_metadata.update(
        {
            "adapter": "run_events_measured_token_usage.v1",
            "logical_usage_id": logical_usage_id,
            "measurement_kind": "measured",
            "source": source,
            "source_reliability": "exact",
        }
    )
    normalized = {
        "work_id": work_id,
        "run_id": run_id,
        "caller": caller,
        "model": model,
        "tokens": int(tokens),
        "source": source,
        "logical_usage_id": logical_usage_id,
        "idempotency_key": idempotency_key,
        "metadata": adapter_metadata,
    }
    if enabled is None:
        enabled, _source = _writer_enabled_and_source()
    if not enabled:
        return {
            "enabled": False,
            "deduped": False,
            "event_id": None,
            "inserted": False,
            "would_write": True,
            **normalized,
        }
    if run_events_table_exists(conn):
        existing = conn.execute(
            "SELECT id FROM run_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {
                "enabled": True,
                "deduped": True,
                "event_id": int(existing["id"] if isinstance(existing, sqlite3.Row) else existing[0]),
                "inserted": False,
                "would_write": False,
                **normalized,
            }
    event_id = record_token_usage(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        model=model,
        caller=caller,
        tokens=int(tokens),
        metadata=adapter_metadata,
        idempotency_key=idempotency_key,
        enabled=True,
    )
    return {
        "enabled": True,
        "deduped": False,
        "event_id": event_id,
        "inserted": event_id is not None,
        "would_write": True,
        **normalized,
    }


_AGENT_TOKEN_SOURCES = {
    "codex": "codex_coord",
    "claude": "claude_coord",
}


def record_agent_measured_token_usage(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    logical_usage_id: str,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    agent_key = str(agent or "").strip().lower()
    source = _AGENT_TOKEN_SOURCES.get(agent_key)
    if source is None:
        raise ValueError(f"agent must be one of {sorted(_AGENT_TOKEN_SOURCES)}")
    try:
        input_count = int(input_tokens)
        output_count = int(output_tokens)
    except (TypeError, ValueError) as exc:
        raise ValueError("input_tokens and output_tokens must be integers") from exc
    if input_count < 0 or output_count < 0:
        raise ValueError("input_tokens and output_tokens must be >= 0")
    if input_count + output_count <= 0:
        raise ValueError("total measured tokens must be > 0")

    if metadata is None:
        adapter_metadata: dict[str, Any] = {}
    elif isinstance(metadata, dict):
        adapter_metadata = dict(metadata)
    else:
        adapter_metadata = {"extra_metadata": metadata}
    adapter_metadata.update(
        {
            "agent": agent_key,
            "input_tokens": input_count,
            "output_tokens": output_count,
        }
    )
    return record_measured_token_usage(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        caller=source,
        model=model,
        tokens=input_count + output_count,
        source=source,
        logical_usage_id=logical_usage_id,
        metadata=adapter_metadata,
        source_reliability="exact",
        enabled=enabled,
    )


def record_artifact_event(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    path: str,
    artifact_kind: str,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    enabled: bool | None = None,
) -> int | None:
    return record_run_event(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        category="artifact",
        event_type="artifact.created",
        content={"path": path, "artifact_kind": artifact_kind},
        metadata=metadata,
        idempotency_key=idempotency_key,
        enabled=enabled,
    )


def record_budget_event(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    run_id: str,
    budget_name: str,
    used: int | float,
    limit: int | float | None,
    metadata: Any | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    enabled: bool | None = None,
) -> int | None:
    return record_run_event(
        conn,
        work_id=work_id,
        run_id=run_id,
        thread_id=thread_id,
        session_id=session_id,
        category="trace",
        event_type="budget.usage",
        content={"budget_name": budget_name, "used": used, "limit": limit},
        metadata=metadata,
        idempotency_key=idempotency_key,
        enabled=enabled,
    )


def _list_sql(
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    work_id: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    after_id: int | None = None,
    limit: int = 100,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if thread_id:
        where.append("thread_id=?")
        params.append(thread_id)
    if run_id:
        aliases = run_id_aliases(run_id)
        if len(aliases) == 1:
            where.append("run_id=?")
            params.append(aliases[0])
        else:
            where.append(f"run_id IN ({','.join('?' for _alias in aliases)})")
            params.extend(aliases)
    if work_id:
        where.append("work_id=?")
        params.append(work_id)
    if category:
        where.append("category=?")
        params.append(category)
    if event_type:
        where.append("event_type=?")
        params.append(event_type)
    if after_id is not None:
        where.append("id>?")
        params.append(int(after_id))
    clause = " WHERE " + " AND ".join(where) if where else ""
    if thread_id:
        order = "seq, id"
    elif run_id and len(run_id_aliases(run_id)) > 1:
        order = "created_at, id"
    elif run_id:
        order = "seq, id"
    else:
        order = "id"
    params.append(int(limit))
    return f"SELECT * FROM run_events{clause} ORDER BY {order} LIMIT ?", params


def list_run_events(
    conn: sqlite3.Connection,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    work_id: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    after_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not run_events_table_exists(conn):
        return []
    sql, params = _list_sql(
        thread_id=thread_id,
        run_id=run_id,
        work_id=work_id,
        category=category,
        event_type=event_type,
        after_id=after_id,
        limit=limit,
    )
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def explain_list_query(
    conn: sqlite3.Connection,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    work_id: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    after_id: int | None = None,
    limit: int = 100,
) -> list[str]:
    ensure_run_events_schema(conn)
    sql, params = _list_sql(
        thread_id=thread_id,
        run_id=run_id,
        work_id=work_id,
        category=category,
        event_type=event_type,
        after_id=after_id,
        limit=limit,
    )
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return [str(row["detail"] if isinstance(row, sqlite3.Row) else row[-1]) for row in rows]


def _loads(text: str) -> Any:
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def reconstruct_run_timeline(conn: sqlite3.Connection, *, run_id: str, limit: int = 500) -> dict[str, Any]:
    rows = list_run_events(conn, run_id=run_id, limit=limit)
    events: list[dict[str, Any]] = []
    token_total = 0
    work_id = None
    for row in rows:
        content = _loads(str(row.get("content_json") or "{}"))
        metadata = _loads(str(row.get("metadata_json") or "{}"))
        content = bound_event_payload(content, field="content")
        metadata = bound_event_payload(metadata, field="metadata")
        if work_id is None:
            work_id = row.get("work_id")
        if row.get("category") == "token" and isinstance(content, dict):
            try:
                token_total += int(content.get("tokens") or 0)
            except (TypeError, ValueError):
                pass
        events.append({
            "id": row.get("id"),
            "seq": row.get("seq"),
            "run_id": row.get("run_id"),
            "category": row.get("category"),
            "event_type": row.get("event_type"),
            "thread_id": row.get("thread_id"),
            "session_id": row.get("session_id"),
            "created_at": row.get("created_at"),
            "content": content,
            "metadata": metadata,
        })
    return {
        "run_id": run_id,
        "canonical_run_id": canonical_run_id(run_id),
        "run_id_aliases": list(run_id_aliases(run_id)),
        "work_id": work_id,
        "event_count": len(events),
        "token_total": token_total,
        "events": events,
    }
