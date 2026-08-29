
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "UngrantableScopeError",
    "UnclaimedFleetError",
    "ScopeKind",
    "WriteScope",
    "CompletionRecord",
    "OverlapFinding",
    "WriteSetOverlapReport",
    "ChildAttempt",
    "ensure_schema",
    "canonical_spec_json",
    "producer_identity",
    "done_signal_identity",
    "record_completion",
    "lookup_completion",
    "invalidate_completion",
    "normalize_scope",
    "declare_write_set",
    "declared_write_set",
    "write_set_overlaps",
    "record_child_attempt",
    "record_child_outcome",
    "child_attempts",
    "fleet_records_missing_model",
]


class UngrantableScopeError(ValueError):
    pass


class UnclaimedFleetError(ValueError):
    pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_contract_done_signals (
  signal_sha256      TEXT NOT NULL,
  work_id            TEXT NOT NULL,
  spec_json          TEXT NOT NULL,
  input_refs_json    TEXT NOT NULL,
  producer_sha256    TEXT NOT NULL,
  completed_ref      TEXT NOT NULL,
  recorded_at        REAL NOT NULL,
  invalidated_at     REAL,
  invalidated_reason TEXT,
  PRIMARY KEY (signal_sha256, work_id)
);
CREATE INDEX IF NOT EXISTS ix_work_contract_signal
  ON work_contract_done_signals(signal_sha256, invalidated_at);

CREATE TABLE IF NOT EXISTS work_contract_write_sets (
  claim_id     TEXT NOT NULL,
  work_id      TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  scope_kind   TEXT NOT NULL,
  scope_value  TEXT NOT NULL,
  declared_at  REAL NOT NULL,
  PRIMARY KEY (claim_id, scope_kind, scope_value)
);
CREATE INDEX IF NOT EXISTS ix_work_contract_write_set_work
  ON work_contract_write_sets(work_id);

CREATE TABLE IF NOT EXISTS work_contract_child_attempts (
  attempt_id        TEXT PRIMARY KEY,
  claim_id          TEXT NOT NULL,
  work_id           TEXT NOT NULL,
  parent_session_id TEXT NOT NULL,
  child_label       TEXT NOT NULL,
  executed_by       TEXT NOT NULL,
  model             TEXT,
  spawned_at        REAL NOT NULL,
  outcome           TEXT,
  outcome_ref       TEXT,
  outcome_at        REAL
);
CREATE INDEX IF NOT EXISTS ix_work_contract_children_claim
  ON work_contract_child_attempts(claim_id);
CREATE INDEX IF NOT EXISTS ix_work_contract_children_work
  ON work_contract_child_attempts(work_id);
"""

_SCHEMA_TABLES = (
    "work_contract_done_signals",
    "work_contract_write_sets",
    "work_contract_child_attempts",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError(
            "work_contracts.ensure_schema must run outside an open transaction"
        )
    conn.executescript(SCHEMA_SQL)


def _tables_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name IN"
        " (?,?,?)",
        _SCHEMA_TABLES,
    ).fetchone()
    return int(row[0] if not isinstance(row, sqlite3.Row) else row["n"]) == len(
        _SCHEMA_TABLES
    )


def _ensure(conn: sqlite3.Connection) -> None:
    if not _tables_present(conn):
        ensure_schema(conn)


def _readable(conn: sqlite3.Connection) -> bool:
    """True when the write-set tables can be read on this connection.

    The two callers below are queries, and the honest answer to a query is a
    result, not a schema migration. A read-only connection -- the board's
    materialized snapshot, `connect_ro`, anything holding `PRAGMA query_only` --
    cannot run the CREATE the lazy path attempts, and the failure surfaced as
    "attempt to write a readonly database": a caller asking whether two claims
    collide got a write error about a table it never mentioned.

    The tables are part of the bootstrapped schema now, so absence here means a
    database predating that and not yet re-bootstrapped. Creating them is still
    attempted, because a writable caller should self-repair; when that is not
    possible the answer is an empty declaration rather than an exception, and
    ``WriteSetOverlapReport.schema_present`` says which of the two happened so
    "no conflicts" is never confused with "nothing could be read".
    """
    if _tables_present(conn):
        return True
    try:
        ensure_schema(conn)
    except (sqlite3.Error, RuntimeError):
        return False
    return True


def _now(at: float | None) -> float:
    return time.time() if at is None else float(at)


@dataclass(frozen=True)
class CompletionRecord:

    signal_sha256: str
    work_id: str
    completed_ref: str
    producer_sha256: str
    spec: Mapping[str, Any]
    input_refs: tuple[str, ...]
    recorded_at: float
    invalidated_at: float | None = None
    invalidated_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.invalidated_at is None


def canonical_spec_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _normalized_refs(input_refs: Iterable[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in input_refs or ():
        ref = str(raw).strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    return tuple(sorted(out))


def producer_identity(
    paths: Sequence[str | Path],
    *,
    root: str | Path | None = None,
) -> str:
    if not paths:
        raise ValueError(
            "producer_identity requires at least one producing-code path"
            " (an empty producer set defeats the replay guard)"
        )
    base = Path(root) if root is not None else None
    parts: list[tuple[str, str]] = []
    for raw in paths:
        rel = str(raw)
        target = Path(rel) if base is None else base / rel
        try:
            payload = target.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            digest = "<absent>"
        parts.append((rel, digest))
    parts.sort()
    return hashlib.sha256(
        canonical_spec_json(parts).encode("utf-8")
    ).hexdigest()


def done_signal_identity(
    *,
    spec: Mapping[str, Any],
    input_refs: Iterable[str] | None = None,
    producer_sha256: str,
) -> str:
    producer = str(producer_sha256 or "").strip()
    if not producer:
        raise ValueError(
            "done_signal_identity requires producer_sha256 (the replay guard)"
        )
    payload = {
        "spec": dict(spec or {}),
        "input_refs": list(_normalized_refs(input_refs)),
        "producer_sha256": producer,
    }
    return hashlib.sha256(
        canonical_spec_json(payload).encode("utf-8")
    ).hexdigest()


def record_completion(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    spec: Mapping[str, Any],
    producer_sha256: str,
    completed_ref: str,
    input_refs: Iterable[str] | None = None,
    at: float | None = None,
) -> str:
    work = str(work_id or "").strip()
    ref = str(completed_ref or "").strip()
    if not work:
        raise ValueError("record_completion requires work_id (completion provenance)")
    if not ref:
        raise ValueError(
            "record_completion requires completed_ref (a row/commit/artifact pointer)"
        )
    _ensure(conn)
    identity = done_signal_identity(
        spec=spec, input_refs=input_refs, producer_sha256=producer_sha256
    )
    refs = _normalized_refs(input_refs)
    conn.execute(
        "INSERT INTO work_contract_done_signals"
        "(signal_sha256, work_id, spec_json, input_refs_json, producer_sha256,"
        " completed_ref, recorded_at, invalidated_at, invalidated_reason)"
        " VALUES (?,?,?,?,?,?,?,NULL,NULL)"
        " ON CONFLICT(signal_sha256, work_id) DO UPDATE SET"
        " completed_ref=excluded.completed_ref, recorded_at=excluded.recorded_at,"
        " invalidated_at=NULL, invalidated_reason=NULL",
        (
            identity,
            work,
            canonical_spec_json(dict(spec or {})),
            canonical_spec_json(list(refs)),
            str(producer_sha256).strip(),
            ref,
            _now(at),
        ),
    )
    conn.commit()
    return identity


def _completion_from_row(row: sqlite3.Row) -> CompletionRecord:
    return CompletionRecord(
        signal_sha256=str(row["signal_sha256"]),
        work_id=str(row["work_id"]),
        completed_ref=str(row["completed_ref"]),
        producer_sha256=str(row["producer_sha256"]),
        spec=json.loads(row["spec_json"]),
        input_refs=tuple(json.loads(row["input_refs_json"])),
        recorded_at=float(row["recorded_at"]),
        invalidated_at=(
            None if row["invalidated_at"] is None else float(row["invalidated_at"])
        ),
        invalidated_reason=row["invalidated_reason"],
    )


def lookup_completion(
    conn: sqlite3.Connection,
    *,
    spec: Mapping[str, Any] | None = None,
    input_refs: Iterable[str] | None = None,
    producer_sha256: str | None = None,
    signal_sha256: str | None = None,
    include_invalidated: bool = False,
) -> CompletionRecord | None:
    _ensure(conn)
    if signal_sha256 is None:
        if spec is None or producer_sha256 is None:
            raise ValueError(
                "lookup_completion needs either signal_sha256 or (spec, producer_sha256)"
            )
        signal_sha256 = done_signal_identity(
            spec=spec, input_refs=input_refs, producer_sha256=producer_sha256
        )
    sql = "SELECT * FROM work_contract_done_signals WHERE signal_sha256=?"
    if not include_invalidated:
        sql += " AND invalidated_at IS NULL"
    sql += " ORDER BY recorded_at ASC, work_id ASC LIMIT 1"
    row = conn.execute(sql, (signal_sha256,)).fetchone()
    return None if row is None else _completion_from_row(row)


def invalidate_completion(
    conn: sqlite3.Connection,
    *,
    signal_sha256: str,
    reason: str,
    work_id: str | None = None,
    at: float | None = None,
) -> int:
    text = str(reason or "").strip()
    if not text:
        raise ValueError("invalidate_completion requires a reason (audit trail)")
    _ensure(conn)
    params: list[Any] = [_now(at), text, str(signal_sha256)]
    sql = (
        "UPDATE work_contract_done_signals SET invalidated_at=?, invalidated_reason=?"
        " WHERE signal_sha256=? AND invalidated_at IS NULL"
    )
    if work_id is not None:
        sql += " AND work_id=?"
        params.append(str(work_id))
    cur = conn.execute(sql, params)
    conn.commit()
    return int(cur.rowcount or 0)


ScopeKind = str

SCOPE_KINDS: tuple[ScopeKind, ...] = ("path", "table", "service")

UNGRANTABLE_PATH_CLASSES: tuple[str, ...] = (
    ".coordharness/runtime/current",
    ".coordharness/runtime/previous",
    ".coordharness/runtime/releases",
)


@dataclass(frozen=True)
class WriteScope:

    kind: ScopeKind
    value: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.kind, self.value)


def _normalize_path_value(value: str) -> str:
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    parts = [seg for seg in text.split("/") if seg not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"path scope must not traverse upward: {value!r}")
    normalized = "/".join(parts)
    if text.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _path_covers(prefix: str, other: str) -> bool:
    if prefix == other:
        return True
    return other.startswith(prefix.rstrip("/") + "/")


def _repo_relative(value: str) -> str:
    from coordharness import config as _harness_config

    root = str(_harness_config.project_root())
    if value.startswith(root):
        return value[len(root):].lstrip("/")
    return value.lstrip("/")


def normalize_scope(kind: str, value: str) -> WriteScope:
    scope_kind = str(kind or "").strip().lower()
    if scope_kind not in SCOPE_KINDS:
        raise ValueError(f"unknown scope kind {kind!r}; expected one of {SCOPE_KINDS}")
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{scope_kind} scope value must be non-empty")
    if scope_kind == "path":
        normalized = _normalize_path_value(raw)
        if not normalized.strip("/"):
            raise UngrantableScopeError(
                "a whole-filesystem path scope grants the deploy pointer;"
                " declare a narrower prefix"
            )
        probe = _repo_relative(normalized)
        for excluded in UNGRANTABLE_PATH_CLASSES:
            if _path_covers(probe, excluded) or _path_covers(excluded, probe):
                raise UngrantableScopeError(
                    f"path scope {value!r} is ungrantable: it overlaps the"
                    f" deploy-pointer/release-tree class {excluded!r}"
                    " (incident 2026-08-03: an agent was handed a live release symlink)"
                )
        return WriteScope(kind="path", value=normalized)
    return WriteScope(kind=scope_kind, value=raw.lower())


_HELD_CLAIM_STATES: tuple[str, ...] = ("running", "paused", "blocked")


def declare_write_set(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    scopes: Iterable[tuple[str, str] | WriteScope | Mapping[str, str]],
    at: float | None = None,
) -> tuple[WriteScope, ...]:
    cid = str(claim_id or "").strip()
    if not cid:
        raise ValueError("declare_write_set requires a claim_id")
    _ensure(conn)
    row = conn.execute(
        "SELECT claim_id, work_id, session_id, status FROM claims WHERE claim_id=?",
        (cid,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown claim_id {claim_id!r}")

    normalized: list[WriteScope] = []
    for entry in scopes:
        if isinstance(entry, WriteScope):
            normalized.append(normalize_scope(entry.kind, entry.value))
        elif isinstance(entry, Mapping):
            normalized.append(normalize_scope(entry["kind"], entry["value"]))
        else:
            kind, value = entry
            normalized.append(normalize_scope(kind, value))

    stamp = _now(at)
    conn.executemany(
        "INSERT OR REPLACE INTO work_contract_write_sets"
        "(claim_id, work_id, session_id, scope_kind, scope_value, declared_at)"
        " VALUES (?,?,?,?,?,?)",
        [
            (cid, str(row["work_id"]), str(row["session_id"]), s.kind, s.value, stamp)
            for s in normalized
        ],
    )
    conn.commit()
    return tuple(normalized)


def declared_write_set(
    conn: sqlite3.Connection, *, claim_id: str
) -> tuple[WriteScope, ...]:
    if not _readable(conn):
        return ()
    rows = conn.execute(
        "SELECT scope_kind, scope_value FROM work_contract_write_sets"
        " WHERE claim_id=? ORDER BY scope_kind, scope_value",
        (str(claim_id),),
    ).fetchall()
    return tuple(
        WriteScope(kind=str(r["scope_kind"]), value=str(r["scope_value"])) for r in rows
    )


@dataclass(frozen=True)
class OverlapFinding:

    kind: ScopeKind
    claim_a: str
    work_a: str
    session_a: str
    scope_a: str
    claim_b: str
    work_b: str
    session_b: str
    scope_b: str

    def describe(self) -> str:
        return (
            f"{self.kind} scope collision: {self.work_a} ({self.claim_a}) declares"
            f" {self.scope_a!r} while {self.work_b} ({self.claim_b}) declares"
            f" {self.scope_b!r}"
        )


@dataclass(frozen=True)
class WriteSetOverlapReport:

    findings: tuple[OverlapFinding, ...]
    scanned_claims: int
    undeclared_claims: tuple[str, ...] = ()
    # False only when the write-set tables could not be read at all. It defaults
    # to True so every existing construction keeps its meaning, and it is
    # carried because an empty finding list otherwise reports the same thing
    # whether the board is clean or the query never ran.
    schema_present: bool = True

    @property
    def count(self) -> int:
        return len(self.findings)

    @property
    def is_red(self) -> bool:
        return bool(self.findings)

    def as_dict(self) -> dict[str, Any]:
        """The report as plain data, for the CLI and MCP surfaces to emit."""
        return {
            "count": self.count,
            "scanned_claims": self.scanned_claims,
            "undeclared_claims": list(self.undeclared_claims),
            "schema_present": self.schema_present,
            "findings": [
                {
                    "kind": finding.kind,
                    "describe": finding.describe(),
                    "claim_a": finding.claim_a,
                    "work_a": finding.work_a,
                    "session_a": finding.session_a,
                    "scope_a": finding.scope_a,
                    "claim_b": finding.claim_b,
                    "work_b": finding.work_b,
                    "session_b": finding.session_b,
                    "scope_b": finding.scope_b,
                }
                for finding in self.findings
            ],
        }


def write_set_overlaps(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    include_expired: bool = False,
) -> WriteSetOverlapReport:
    if not _readable(conn):
        # No table to read, and no way to create one on this connection. Say
        # that in the report instead of returning a clean bill of health that
        # is indistinguishable from a scanned board with nothing colliding.
        return WriteSetOverlapReport(
            findings=(), scanned_claims=0, schema_present=False
        )
    at = _now(now)
    placeholders = ",".join("?" for _ in _HELD_CLAIM_STATES)
    sql = (
        "SELECT claim_id, work_id, session_id, expires_at FROM claims"
        f" WHERE status IN ({placeholders})"
    )
    params: list[Any] = list(_HELD_CLAIM_STATES)
    if not include_expired:
        sql += " AND expires_at > ?"
        params.append(at)
    active = conn.execute(sql + " ORDER BY claim_id", params).fetchall()

    declared: list[tuple[str, str, str, WriteScope]] = []
    undeclared: list[str] = []
    for row in active:
        scopes = declared_write_set(conn, claim_id=str(row["claim_id"]))
        if not scopes:
            undeclared.append(str(row["claim_id"]))
            continue
        for scope in scopes:
            declared.append(
                (
                    str(row["claim_id"]),
                    str(row["work_id"]),
                    str(row["session_id"]),
                    scope,
                )
            )

    findings: list[OverlapFinding] = []
    for i in range(len(declared)):
        claim_a, work_a, session_a, scope_a = declared[i]
        for j in range(i + 1, len(declared)):
            claim_b, work_b, session_b, scope_b = declared[j]
            if claim_a == claim_b or session_a == session_b:
                continue
            if scope_a.kind != scope_b.kind:
                continue
            if scope_a.kind == "path":
                hit = _path_covers(scope_a.value, scope_b.value) or _path_covers(
                    scope_b.value, scope_a.value
                )
            else:
                hit = scope_a.value == scope_b.value
            if hit:
                findings.append(
                    OverlapFinding(
                        kind=scope_a.kind,
                        claim_a=claim_a,
                        work_a=work_a,
                        session_a=session_a,
                        scope_a=scope_a.value,
                        claim_b=claim_b,
                        work_b=work_b,
                        session_b=session_b,
                        scope_b=scope_b.value,
                    )
                )
    return WriteSetOverlapReport(
        findings=tuple(findings),
        scanned_claims=len(active),
        undeclared_claims=tuple(undeclared),
    )


@dataclass(frozen=True)
class ChildAttempt:

    attempt_id: str
    claim_id: str
    work_id: str
    parent_session_id: str
    child_label: str
    executed_by: str
    model: str | None
    spawned_at: float
    outcome: str | None = None
    outcome_ref: str | None = None
    outcome_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.outcome is None


def _held_claim_row(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT claim_id, work_id, session_id, status, expires_at FROM claims"
        " WHERE claim_id=?",
        (str(claim_id),),
    ).fetchone()
    if row is None:
        raise UnclaimedFleetError(
            f"unknown claim_id {claim_id!r}: a fleet must be recorded under an"
            " already-claimed job row — a fleet spawned under no live claim leaves"
            " no trace of the work it did, so claim the row before spawning"
        )
    if str(row["status"]) not in _HELD_CLAIM_STATES:
        raise UnclaimedFleetError(
            f"claim {claim_id!r} is {row['status']!r}, not held; record child"
            " attempts only under a live claim"
        )
    return row


def record_child_attempt(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    child_label: str,
    executed_by: str,
    model: str | None = None,
    spawned_at: float | None = None,
    attempt_id: str | None = None,
) -> ChildAttempt:
    label = str(child_label or "").strip()
    actor = str(executed_by or "").strip()
    if not label:
        raise ValueError("record_child_attempt requires a child_label")
    if not actor:
        raise ValueError(
            "record_child_attempt requires executed_by (the identity the"
            " incident loses)"
        )
    _ensure(conn)
    row = _held_claim_row(conn, claim_id)
    attempt = ChildAttempt(
        attempt_id=str(attempt_id or f"cat_{uuid.uuid4().hex[:16]}"),
        claim_id=str(row["claim_id"]),
        work_id=str(row["work_id"]),
        parent_session_id=str(row["session_id"]),
        child_label=label,
        executed_by=actor,
        model=(str(model).strip() or None) if model is not None else None,
        spawned_at=_now(spawned_at),
    )
    conn.execute(
        "INSERT INTO work_contract_child_attempts"
        "(attempt_id, claim_id, work_id, parent_session_id, child_label,"
        " executed_by, model, spawned_at, outcome, outcome_ref, outcome_at)"
        " VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
        (
            attempt.attempt_id,
            attempt.claim_id,
            attempt.work_id,
            attempt.parent_session_id,
            attempt.child_label,
            attempt.executed_by,
            attempt.model,
            attempt.spawned_at,
        ),
    )
    conn.commit()
    return attempt


def record_child_outcome(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    outcome: str,
    outcome_ref: str | None = None,
    at: float | None = None,
) -> bool:
    result = str(outcome or "").strip()
    if not result:
        raise ValueError("record_child_outcome requires a non-empty outcome")
    _ensure(conn)
    cur = conn.execute(
        "UPDATE work_contract_child_attempts SET outcome=?, outcome_ref=?, outcome_at=?"
        " WHERE attempt_id=?",
        (result, outcome_ref, _now(at), str(attempt_id)),
    )
    conn.commit()
    return bool(cur.rowcount)


def child_attempts(
    conn: sqlite3.Connection,
    *,
    work_id: str | None = None,
    claim_id: str | None = None,
    open_only: bool = False,
) -> tuple[ChildAttempt, ...]:
    _ensure(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if work_id is not None:
        clauses.append("work_id=?")
        params.append(str(work_id))
    if claim_id is not None:
        clauses.append("claim_id=?")
        params.append(str(claim_id))
    if open_only:
        clauses.append("outcome IS NULL")
    sql = "SELECT * FROM work_contract_child_attempts"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(sql + " ORDER BY spawned_at, attempt_id", params).fetchall()
    return tuple(
        ChildAttempt(
            attempt_id=str(r["attempt_id"]),
            claim_id=str(r["claim_id"]),
            work_id=str(r["work_id"]),
            parent_session_id=str(r["parent_session_id"]),
            child_label=str(r["child_label"]),
            executed_by=str(r["executed_by"]),
            model=r["model"],
            spawned_at=float(r["spawned_at"]),
            outcome=r["outcome"],
            outcome_ref=r["outcome_ref"],
            outcome_at=(None if r["outcome_at"] is None else float(r["outcome_at"])),
        )
        for r in rows
    )


def fleet_records_missing_model(
    conn: sqlite3.Connection, *, work_id: str | None = None
) -> tuple[ChildAttempt, ...]:
    return tuple(
        attempt
        for attempt in child_attempts(conn, work_id=work_id)
        if not (attempt.model or "").strip()
    )
