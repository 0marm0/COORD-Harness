
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import socket
import sqlite3
import time
import uuid
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from coordharness.config import HARNESS_ROOT, state_dir
from coordharness.jobs.status import done_signal_custodied, done_signal_exists

from .config import (
    configured_lanes as _configured_lanes,
    counterpart_lane as _counterpart_lane,
    lane_set as _lane_set,
    lanes_display as _lanes_display,
)
from .continuation_contract import (
    normalize_resume_trigger_contract,
    require_park_resume_contract,
)
from .process_liveness import pid_matches, pid_start_time
from .staleness import ACTIVE_INTENT_STALE_SECS

_logger = logging.getLogger(__name__)

LEASE_DEFAULT_S = (
    3600.0
)
LEASE_SHORT_S = 900.0
REAP_GRACE_S = 60.0
_BODY_CAP = 2048
_FLAG_REPAIR_NEGATIVE_VERDICTS = frozenset({"FLAG", "BLOCKED"})
_FLAG_REPAIR_REMEDIATION_KINDS = frozenset(
    {"evidence", "milestone", "note", "remediation_evidence"}
)
_TYPED_HANDOFF_EVENT_ID_PREVIEW_LIMIT = 64
_TYPED_HANDOFF_WORK_FIELD_INLINE_BYTES = 512
OPEN_AUDIT_REQUEST_PREVIEW_LIMIT = 8
_AGENT_IDENTITY_ENV_KEYS = (
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_CONVERSATION_ID",
    "CODEX_WORKTREE_ID",
    "CLAUDE_CODE_SESSION_ID",
    "COORD_PARENT_SESSION_ID",
)
PROJECTION_PROTECTED_WORK_FIELDS = frozenset(
    {
        "acceptance_json",
        "assignee",
        "assigned_by",
        "done_signal",
        "intent_state",
        "rubric_verdict",
        "parent_id",
        "depends_on",
    }
)
TERMINAL_WORK_STATES = frozenset(
    {
        "done",
        "complete",
        "completed",
        "finished",
        "success",
        "succeeded",
        "skipped",
        "failed",
        "archived",
        "superseded",
        "cancelled",
        "canceled",
        "closed",
    }
)


def now() -> float:
    return time.time()


def db_now(conn: sqlite3.Connection) -> float:
    return float(
        conn.execute("SELECT (julianday('now') - 2440587.5) * 86400.0").fetchone()[0]
    )


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def local_host_id() -> str:
    """The machine's current network hostname.

    Kept as the legacy comparison point in ``pid_liveness_is_meaningful`` for
    rows written before ``stable_host_id`` existed, and as the fallback
    ``stable_host_id`` itself returns when it cannot persist an id. Do not
    stamp a new row with this directly -- ``socket.gethostname()`` drifts on a
    macOS Bonjour/DHCP rename, which is exactly the bug ``stable_host_id``
    fixes.
    """
    return socket.gethostname()


_HOST_ID_FILENAME = "host_id"


def stable_host_id() -> str:
    """A rename-stable identity for rows this process writes.

    ``local_host_id()`` (``socket.gethostname()``) drifts on a macOS
    Bonjour/DHCP rename: after a rename every row this machine wrote earlier
    carries a hostname nothing here answers to any more, so
    ``pid_liveness_is_meaningful`` starts reading all of them as foreign and a
    genuinely dead local run reads as running forever. This persists a random
    id once, as a file named ``host_id`` in the same state directory that
    holds ``coord.db`` (``coordharness.config.state_dir()``), and returns it
    on every later call -- stable across a rename because nothing here reads
    the hostname.

    Read/create is write-once: an existing file's contents win, a missing one
    gets a fresh ``uuid4().hex`` written with mode ``0600``. Any failure along
    the way -- an unwritable state dir, an unreadable file, a race with
    another process creating the same file -- is logged and answered with
    ``local_host_id()`` instead of raised, so a locked-down or read-only state
    directory degrades to the pre-existing rename-fragile behaviour rather
    than breaking every write path.
    """
    path = state_dir() / _HOST_ID_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            existing = ""
        if existing:
            return existing
        fresh = uuid.uuid4().hex
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Lost a create race to another process writing concurrently;
            # its value is authoritative, not ours.
            raced = path.read_text(encoding="utf-8").strip()
            return raced or local_host_id()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(fresh)
        os.chmod(str(path), 0o600)
        return fresh
    except Exception:
        _logger.warning(
            "stable host id unavailable at %s; falling back to gethostname()",
            path,
            exc_info=True,
        )
        return local_host_id()


def pid_liveness_is_meaningful(host_id: object) -> bool:
    """Whether a local pid probe can answer for a row recorded on ``host_id``.

    A pid only means something on the machine that recorded it. NULL/empty is
    the pre-migration and single-machine case and is read as local: every
    database written before host_id existed keeps its current behaviour. A row
    is also local when its ``host_id`` matches this machine's current
    ``stable_host_id()`` (new writes) or its current ``socket.gethostname()``
    (writes made before ``stable_host_id`` existed) -- the OR is what keeps a
    legacy hostname-stamped row local across the same rename that
    ``stable_host_id`` protects new rows from. Any other host_id makes the
    probe unanswerable here -- the caller must report UNKNOWN and fall back to
    the lease/heartbeat, never "dead", or a healthy remote run reads as a
    crash.
    """
    recorded = str(host_id or "").strip()
    if not recorded:
        return True
    return recorded == stable_host_id() or recorded == local_host_id()


def new_work_quarantine_declaration(
    work_id: str, *, source_kind: str = "legacy_grouping_quarantine"
) -> str:

    from .exact_authority import new_work_quarantine_declaration as _declaration

    return _declaration(
        work_id,
        writer="coord_db.public_writer_doorway",
        source_kind=source_kind,
    )


@contextmanager
def tx(conn: sqlite3.Connection):
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            conn.execute("BEGIN IMMEDIATE")
            last = None
            break
        except sqlite3.OperationalError as exc:
            if "lock" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            time.sleep(0.02 * (2**attempt))
    if last is not None:
        raise last
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def expected_actor_for_session_id(session_id: str) -> str | None:
    sid = str(session_id or "").strip().lower()
    prefix, separator, _suffix = sid.partition(":")
    if not separator:
        return None
    try:
        from coordharness.config import actor_name

        return actor_name(prefix)
    except ValueError:
        return None


def _validate_session_actor(session_id: str, actor: str) -> None:
    expected = expected_actor_for_session_id(session_id)
    actual = str(actor or "").strip().lower()
    if expected and actual != expected:
        raise ValueError(
            f"session_id {session_id!r} requires actor={expected!r}, got {actor!r}"
        )


def _session_family_suffix(session_id: str, actor: str | None) -> str:
    sid = str(session_id or "").strip()
    actor = str(actor or "").strip().lower()
    if not sid or actor not in _lane_set():
        return ""
    prefix = f"{actor}:"
    if sid.startswith(prefix):
        suffix = sid[len(prefix) :].strip()
        return suffix if suffix else ""
    if ":" not in sid:
        return sid
    return ""


def _related_session_ids_unlocked(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    actor: str | None = None,
) -> list[str]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    row = conn.execute(
        "SELECT actor, external_thread_id, worktree_id FROM agent_sessions WHERE session_id=?",
        (sid,),
    ).fetchone()
    actual_actor = str(actor or (row["actor"] if row else "") or "").strip().lower()
    if not actual_actor:
        actual_actor = expected_actor_for_session_id(sid) or ""
    suffix = _session_family_suffix(sid, actual_actor)
    external_thread_id = str(
        row["external_thread_id"] if row and row["external_thread_id"] else ""
    ).strip()
    worktree_id = str(row["worktree_id"] if row and row["worktree_id"] else "").strip()
    if not suffix and not external_thread_id and not worktree_id:
        return [sid]
    related: list[str] = []
    candidates = {
        sid,
        suffix,
        f"{actual_actor}:{suffix}",
    }
    for session_row in conn.execute(
        "SELECT session_id, external_thread_id, worktree_id FROM agent_sessions WHERE actor=?",
        (actual_actor,),
    ).fetchall():
        candidate = str(session_row["session_id"] or "").strip()
        candidate_external_thread_id = str(
            session_row["external_thread_id"] or ""
        ).strip()
        candidate_worktree_id = str(session_row["worktree_id"] or "").strip()
        if (
            candidate in candidates
            or (suffix and candidate.endswith(f":{suffix}"))
            or (
                external_thread_id
                and candidate_external_thread_id == external_thread_id
            )
            or (worktree_id and candidate_worktree_id == worktree_id)
        ):
            related.append(candidate)
    if sid not in related:
        related.append(sid)
    return sorted(set(related))


def related_session_ids(
    conn, session_id: str, *, actor: str | None = None
) -> list[str]:
    return _related_session_ids_unlocked(conn, session_id, actor=actor)


def _renew_sessions_and_claims_unlocked(
    conn: sqlite3.Connection,
    session_ids: Iterable[str],
    *,
    at: float,
    lease_s: float,
) -> None:
    ids = sorted({str(s or "").strip() for s in session_ids if str(s or "").strip()})
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE agent_sessions SET last_heartbeat=?, lease_until=? "
        f"WHERE session_id IN ({placeholders})",
        (at, at + lease_s, *ids),
    )
    conn.execute(
        f"UPDATE claims SET heartbeat_at=?, expires_at=? "
        f"WHERE session_id IN ({placeholders})"
        " AND status IN ('running','paused','blocked')",
        (at, at + lease_s, *ids),
    )


def register_session(
    conn,
    session_id: str,
    actor: str,
    *,
    actor_id: str | None = None,
    parent_session_id: str | None = None,
    runner_type: str | None = None,
    cwd: str | None = None,
    pid: int | None = None,
    pid_started_at: float | None = None,
    human_label: str | None = None,
    external_thread_id: str | None = None,
    conversation_title: str | None = None,
    worktree_id: str | None = None,
    label_source: str | None = None,
    lease_s: float = LEASE_DEFAULT_S,
) -> str:
    if not session_id:
        raise ValueError("register_session requires a non-empty session_id")
    actor = str(actor or "").strip().lower()
    if not actor:
        raise ValueError("register_session requires a non-empty actor")
    _validate_session_actor(session_id, actor)
    with tx(conn):
        t = db_now(conn)
        if pid is not None and pid_started_at is None:
            pid_started_at = pid_start_time(pid)
        existing = conn.execute(
            "SELECT session_id, state, actor FROM agent_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        label_updated_at = (
            t
            if any(
                v is not None
                for v in (
                    human_label,
                    external_thread_id,
                    conversation_title,
                    worktree_id,
                    label_source,
                )
            )
            else None
        )
        if existing is None:
            conn.execute(
                "INSERT INTO agent_sessions(session_id, actor, actor_id, parent_session_id,"
                " runner_type, human_label, external_thread_id, conversation_title, worktree_id,"
                " label_source, label_updated_at, cwd, pid, pid_started_at, host_id, started_at,"
                " last_heartbeat, lease_until, state, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', 0)",
                (
                    session_id,
                    actor,
                    actor_id or actor,
                    parent_session_id,
                    runner_type,
                    human_label,
                    external_thread_id,
                    conversation_title,
                    worktree_id,
                    label_source,
                    label_updated_at,
                    cwd,
                    pid,
                    pid_started_at,
                    stable_host_id(),
                    t,
                    t,
                    t + lease_s,
                ),
            )
        else:
            existing_actor = str(existing["actor"] or "").strip()
            actor_mismatch = bool(existing_actor and existing_actor != actor)
            if actor_mismatch:
                raise ValueError(
                    f"session {session_id!r} is already actor={existing_actor!r}; "
                    f"refusing relabel to actor={actor!r}"
                )
            stored_actor = existing_actor if actor_mismatch else actor
            conn.execute(
                "UPDATE agent_sessions SET actor=?, actor_id=COALESCE(?,actor_id),"
                " parent_session_id=COALESCE(?,parent_session_id), runner_type=COALESCE(?,runner_type),"
                " human_label=COALESCE(?,human_label),"
                " external_thread_id=COALESCE(?,external_thread_id),"
                " conversation_title=COALESCE(?,conversation_title),"
                " worktree_id=COALESCE(?,worktree_id),"
                " label_source=COALESCE(?,label_source),"
                " label_updated_at=COALESCE(?,label_updated_at),"
                " cwd=COALESCE(?,cwd), pid=COALESCE(?,pid),"
                " pid_started_at=COALESCE(?,pid_started_at), last_heartbeat=?, lease_until=?,"
                " state='active', ended_at=NULL, version=version+1 WHERE session_id=?",
                (
                    stored_actor,
                    None if actor_mismatch else actor_id,
                    parent_session_id,
                    None if actor_mismatch else runner_type,
                    human_label,
                    external_thread_id,
                    conversation_title,
                    worktree_id,
                    label_source,
                    label_updated_at,
                    cwd,
                    pid,
                    pid_started_at,
                    t,
                    t + lease_s,
                    session_id,
                ),
            )
        session_ids = _related_session_ids_unlocked(conn, session_id, actor=actor)
        _renew_sessions_and_claims_unlocked(conn, session_ids, at=t, lease_s=lease_s)
    return session_id


def renew_lease(conn, session_id: str, lease_s: float = LEASE_DEFAULT_S) -> None:
    with tx(conn):
        t = db_now(conn)
        session_ids = _related_session_ids_unlocked(conn, session_id)
        _renew_sessions_and_claims_unlocked(conn, session_ids, at=t, lease_s=lease_s)


def renew_claim_from_sidecar(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    work_id: str,
    session_id: str,
    lease_s: float = LEASE_DEFAULT_S,
    at: float | None = None,
) -> bool:

    claim_id = str(claim_id or "").strip()
    work_id = str(work_id or "").strip()
    session_id = str(session_id or "").strip()
    if not claim_id or not work_id or not session_id:
        raise ValueError("sidecar claim renewal requires claim_id, work_id, and session_id")
    if lease_s <= 0:
        raise ValueError("sidecar claim renewal requires a positive lease")
    with tx(conn):
        t = db_now(conn) if at is None else float(at)
        changed = conn.execute(
            "UPDATE claims SET heartbeat_at=?,expires_at=?"
            " WHERE claim_id=? AND work_id=? AND session_id=? AND status='running'"
            " AND (expires_at IS NULL OR expires_at>?)",
            (t, t + lease_s, claim_id, work_id, session_id, t),
        )
        return changed.rowcount == 1


def running_claim_binding(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    at: float | None = None,
) -> dict[str, Any] | None:

    work_id = str(work_id or "").strip()
    if not work_id:
        raise ValueError("sidecar claim binding requires work_id")
    t = db_now(conn) if at is None else float(at)
    rows = conn.execute(
        "SELECT claim_id,work_id,session_id,heartbeat_at,expires_at"
        " FROM claims WHERE work_id=? AND status='running'"
        " AND (expires_at IS NULL OR expires_at>?) ORDER BY acquired_at DESC LIMIT 2",
        (work_id, t),
    ).fetchall()
    if len(rows) != 1:
        return None
    return dict(rows[0])


def held_claim_id_for_session_family(
    conn: sqlite3.Connection,
    work_id: str,
    session_id: str | None = None,
    *,
    actor: str | None = None,
) -> str | None:
    wid = str(work_id or "").strip()
    if not wid:
        return None
    params: list[Any] = [wid]
    where = "work_id=? AND status IN ('running','paused','blocked')"
    sid = str(session_id or "").strip()
    if sid:
        related = _related_session_ids_unlocked(conn, sid, actor=actor)
        if not related:
            related = [sid]
        placeholders = ",".join("?" for _ in related)
        where += f" AND session_id IN ({placeholders})"
        params.extend(related)
    row = conn.execute(
        f"SELECT claim_id FROM claims WHERE {where} ORDER BY heartbeat_at DESC LIMIT 1",
        params,
    ).fetchone()
    return str(row["claim_id"]) if row and row["claim_id"] else None


def end_session(conn, session_id: str) -> None:
    with tx(conn):
        t = db_now(conn)
        conn.execute(
            "UPDATE agent_sessions SET state='ended', ended_at=? WHERE session_id=? AND state='active'",
            (t, session_id),
        )
        conn.execute(
            "UPDATE runs SET state='orphaned', finished_at=?, version=version+1"
            " WHERE (session_id=? OR parent_session_id=?) AND state='live'",
            (t, session_id, session_id),
        )


def reap_zombie_sessions(
    conn,
    *,
    at: float | None = None,
    grace_s: float = REAP_GRACE_S,
    dead_pids: Iterable[int] | None = None,
    dead_sessions: Iterable[str] | None = None,
) -> dict:
    at = at if at is not None else db_now(conn)
    dead = set(dead_pids) if dead_pids is not None else None
    dead_sid = set(dead_sessions) if dead_sessions is not None else None
    reaped: list[str] = []
    released = 0
    with tx(conn):
        rows = conn.execute(
            "SELECT session_id, pid FROM agent_sessions"
            " WHERE state='active' AND pause_at IS NULL AND lease_until < ?",
            (at - grace_s,),
        ).fetchall()
        for r in rows:
            sid, pid = r["session_id"], r["pid"]
            if dead_sid is not None and sid not in dead_sid:
                continue
            if (
                dead_sid is None
                and dead is not None
                and pid is not None
                and pid not in dead
            ):
                continue
            claims = conn.execute(
                "SELECT claim_id, work_id FROM claims WHERE session_id=?"
                " AND status IN ('running','paused','blocked')",
                (sid,),
            ).fetchall()
            for c in claims:
                conn.execute(
                    "UPDATE claims SET status='unclaimed', release_reason='reaped',"
                    " version=version+1 WHERE claim_id=?",
                    (c["claim_id"],),
                )
                terminal_placeholders = ",".join("?" for _ in TERMINAL_WORK_STATES)
                conn.execute(
                    "UPDATE work_items SET intent_state='queued', updated_at=?, version=version+1"
                    f" WHERE work_id=? AND intent_state NOT IN ({terminal_placeholders})",
                    (at, c["work_id"], *TERMINAL_WORK_STATES),
                )
                released += 1
            conn.execute(
                "UPDATE runs SET state='orphaned', finished_at=?, version=version+1"
                " WHERE (session_id=? OR parent_session_id=?) AND state='live'",
                (at, sid, sid),
            )
            conn.execute(
                "UPDATE agent_sessions SET state='reaped', ended_at=? WHERE session_id=?",
                (at, sid),
            )
            reaped.append(sid)
    return {"reaped": reaped, "claims_released": released}


_HELD_CLAIM_STATUSES = ("running", "paused", "blocked")
RELEASABLE_CLAIM_STATUSES = frozenset({"released", "unclaimed", "paused", "blocked"})
_NON_PROOF_ARTIFACT_KINDS = frozenset({"context_pack"})


def _release_expired_claims_unlocked(
    conn: sqlite3.Connection,
    *,
    at: float,
    work_id: str | None = None,
    released_rows: list[dict[str, Any]] | None = None,
) -> int:
    where = "status IN ('running','paused','blocked') AND expires_at IS NOT NULL AND expires_at < ?"
    args: list[Any] = [at]
    if work_id:
        where += " AND work_id=?"
        args.append(work_id)
    rows = conn.execute(
        f"SELECT claim_id, work_id, session_id, status, expires_at, version"
        f" FROM claims WHERE {where} ORDER BY expires_at, claim_id",
        tuple(args),
    ).fetchall()
    released = 0
    for row in rows:
        changed = conn.execute(
            "UPDATE claims SET status='unclaimed', release_reason='expired',"
            " version=version+1 WHERE claim_id=? AND version=? AND status=?"
            " AND expires_at=? AND expires_at < ?",
            (
                row["claim_id"],
                row["version"],
                row["status"],
                row["expires_at"],
                at,
            ),
        )
        if changed.rowcount != 1:
            continue
        released += 1
        other_live = conn.execute(
            "SELECT 1 FROM claims WHERE work_id=? AND claim_id!=?"
            " AND status IN ('running','paused','blocked')"
            " AND (expires_at IS NULL OR expires_at >= ?) LIMIT 1",
            (row["work_id"], row["claim_id"], at),
        ).fetchone()
        before = conn.execute(
            "SELECT intent_state,blocked_reason_class FROM work_items WHERE work_id=?",
            (row["work_id"],),
        ).fetchone()
        work_requeued = False
        terminal_placeholders = ",".join("?" for _ in TERMINAL_WORK_STATES)
        prior_work_state = str(before["intent_state"] if before else "").strip().lower()
        sticky_reason = str(
            (before["blocked_reason_class"] if before else "") or ""
        ).strip()
        sticky_disposition = prior_work_state in {"blocked", "paused"} or bool(sticky_reason)
        if other_live is None and not sticky_disposition:
            updated = conn.execute(
                "UPDATE work_items SET intent_state='queued', updated_at=?, version=version+1"
                f" WHERE work_id=? AND intent_state!='queued'"
                f" AND intent_state NOT IN ({terminal_placeholders})",
                (at, row["work_id"], *TERMINAL_WORK_STATES),
            )
            work_requeued = updated.rowcount == 1
        if released_rows is not None:
            after = conn.execute(
                "SELECT intent_state FROM work_items WHERE work_id=?",
                (row["work_id"],),
            ).fetchone()
            released_rows.append(
                {
                    "claim_id": str(row["claim_id"]),
                    "work_id": str(row["work_id"]),
                    "session_id": str(row["session_id"]),
                    "prior_claim_status": str(row["status"]),
                    "expires_at": float(row["expires_at"]),
                    "prior_work_state": str(before["intent_state"] if before else ""),
                    "result_work_state": str(after["intent_state"] if after else ""),
                    "work_requeued": work_requeued,
                    "other_live_claim_preserved": other_live is not None,
                    "sticky_disposition_preserved": sticky_disposition,
                }
            )
    return released


def release_expired_claims(
    conn: sqlite3.Connection,
    *,
    work_id: str | None = None,
    at: float | None = None,
) -> int:
    with tx(conn):
        t = at if at is not None else db_now(conn)
        return _release_expired_claims_unlocked(conn, at=t, work_id=work_id)


def release_expired_claims_batch(
    conn: sqlite3.Connection,
    *,
    at: float | None = None,
) -> dict[str, Any]:
    released_rows: list[dict[str, Any]] = []
    event_id: int | None = None
    with tx(conn):
        t = at if at is not None else db_now(conn)
        count = _release_expired_claims_unlocked(
            conn,
            at=t,
            released_rows=released_rows,
        )
        canonical = json.dumps(
            released_rows,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        if count:
            payload = {
                "schema": "coordharness.stale-claim-auto-release.v1",
                "observed_at": t,
                "released_count": count,
                "released_rows_sha256": digest,
                "work_requeued_count": sum(
                    1 for row in released_rows if row["work_requeued"]
                ),
                "terminal_or_queued_preserved_count": sum(
                    1
                    for row in released_rows
                    if not row["work_requeued"]
                    and not row["other_live_claim_preserved"]
                ),
                "other_live_claim_preserved_count": sum(
                    1 for row in released_rows if row["other_live_claim_preserved"]
                ),
                "sticky_disposition_preserved_count": sum(
                    1 for row in released_rows if row["sticky_disposition_preserved"]
                ),
                "sample_work_ids": sorted(
                    {row["work_id"] for row in released_rows}
                )[:20],
            }
            cursor = conn.execute(
                "INSERT INTO events(ts,kind,actor,trust,body,payload_json,idempotency_key)"
                " VALUES (?,?,'coord','agent',?,?,?)",
                (
                    t,
                    "stale_claim_batch_release",
                    f"released {count} expired held claim(s)",
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    f"stale-claim-batch:{digest}",
                ),
            )
            event_id = int(cursor.lastrowid)
    return {
        "schema": "coordharness.stale-claim-auto-release.v1",
        "observed_at": t,
        "released_count": count,
        "released_rows_sha256": digest,
        "released_rows": released_rows,
        "event_id": event_id,
    }


def renew_claims_from_live_fleets(
    conn: sqlite3.Connection,
    *,
    at: float | None = None,
    lease_s: float = LEASE_DEFAULT_S,
) -> dict[str, Any]:

    if lease_s <= 0:
        raise ValueError("lease_s must be positive")
    renewed_rows: list[dict[str, Any]] = []
    event_id: int | None = None
    with tx(conn):
        t = at if at is not None else db_now(conn)
        threshold = t + (lease_s / 2.0)
        candidates = conn.execute(
            "SELECT c.claim_id,c.work_id,c.session_id,c.expires_at"
            " FROM claims c"
            " WHERE c.status='running' AND c.expires_at IS NOT NULL"
            " AND c.expires_at<?"
            " AND EXISTS (SELECT 1 FROM runs r"
            "   WHERE r.work_id=c.work_id AND r.state='live'"
            "   AND r.runner_kind IN ('subagent','workflow'))"
            " ORDER BY c.expires_at,c.claim_id",
            (threshold,),
        ).fetchall()
        expires_at = t + lease_s
        for row in candidates:
            changed = conn.execute(
                "UPDATE claims SET heartbeat_at=?,expires_at=?"
                " WHERE claim_id=? AND status='running'"
                " AND EXISTS (SELECT 1 FROM runs r"
                "   WHERE r.work_id=claims.work_id AND r.state='live'"
                "   AND r.runner_kind IN ('subagent','workflow'))",
                (t, expires_at, row["claim_id"]),
            )
            if changed.rowcount != 1:
                continue
            renewed_rows.append(
                {
                    "claim_id": str(row["claim_id"]),
                    "work_id": str(row["work_id"]),
                    "session_id": str(row["session_id"]),
                    "prior_expires_at": float(row["expires_at"]),
                    "expires_at": expires_at,
                }
            )
        digest = hashlib.sha256(
            json.dumps(
                renewed_rows,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if renewed_rows:
            payload = {
                "schema": "coordharness.fleet-backed-claim-renewal.v1",
                "observed_at": t,
                "renewed_count": len(renewed_rows),
                "lease_s": lease_s,
                "renewed_rows_sha256": digest,
                "sample_work_ids": sorted(
                    {row["work_id"] for row in renewed_rows}
                )[:20],
            }
            cursor = conn.execute(
                "INSERT INTO events(ts,kind,actor,trust,body,payload_json,"
                "idempotency_key) VALUES (?,?,'coord','agent',?,?,?)",
                (
                    t,
                    "fleet_claim_lease_renewal",
                    f"renewed {len(renewed_rows)} fleet-backed claim lease(s)",
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    f"fleet-claim-renewal:{digest}",
                ),
            )
            event_id = int(cursor.lastrowid)
    return {
        "schema": "coordharness.fleet-backed-claim-renewal.v1",
        "observed_at": t,
        "renewed_count": len(renewed_rows),
        "renewed_rows_sha256": digest,
        "event_id": event_id,
        "renewed_rows": renewed_rows,
    }


_WORK_COLS = {
    "parent_id",
    "surface",
    "domain",
    "module",
    "lane",
    "sublane",
    "title",
    "display",
    "assignee",
    "assigned_by",
    "intent_state",
    "blocked_reason_class",
    "completion_requested_at",
    "next_step",
    "resume_when",
    "resume_predicate_json",
    "continuation_ready_at",
    "operator_ok_event_id",
    "tier_correction_event_id",
    "done_signal",
    "acceptance_json",
    "rubric_verdict",
    "resource_class",
    "token_budget",
    "priority",
    "visibility",
    "context_pack_ref",
    "due_date",
    "created_by_session_id",
    "note",
    "depends_on",
    "kind",
    "tier",
    "operator_state",
    "authority_declaration_json",
}


def supersede_prior_handoffs(
    conn,
    work_id: str,
    new_event_id,
    *,
    actor: str,
    session_id: str | None = None,
    owner_lane: str | None = None,
) -> list[str]:
    rows = list(
        conn.execute(
            "SELECT event_id, payload_json FROM events WHERE kind IN ('handoff','audit_request')"
            " AND work_id=? ORDER BY event_id",
            (work_id,),
        )
    )
    already: set[str] = set()
    for r in conn.execute(
        "SELECT payload_json FROM events WHERE kind='handoff_superseded'"
    ):
        try:
            sid_ = (json.loads(r["payload_json"] or "{}") or {}).get("supersedes")
        except Exception:
            sid_ = None
        if sid_ and str(sid_).strip():
            already.add(str(sid_).strip())
    sel = f"actor:{owner_lane}" if owner_lane else None
    out: list[str] = []
    for r in rows:
        try:
            structured = (json.loads(r["payload_json"] or "{}") or {}).get(
                "schema_version"
            ) is not None
        except Exception:
            structured = False
        eid = str(r["event_id"])
        if not structured or eid == str(new_event_id) or eid in already:
            continue
        post_event(
            conn,
            kind="handoff_superseded",
            actor=actor,
            session_id=session_id,
            to_selector=sel,
            work_id=work_id,
            refs_json=json.dumps([f"event:{eid}"]),
            payload_json=json.dumps(
                {"supersedes": eid, "by_event_id": new_event_id, "schema_version": 1}
            ),
        )
        out.append(eid)
    return out


class WorkRelationInvariantError(ValueError):
    pass


class SelfParentError(WorkRelationInvariantError):
    pass


class ParentCycleError(WorkRelationInvariantError):
    pass


class SelfDependencyError(WorkRelationInvariantError):
    pass


class WorkIdCollisionError(ValueError):
    """A create asked for a work_id that already belongs to another row.

    Raised from inside the creating transaction, not from a read taken before
    it. Two sessions picking the same date-and-lane suffix is not exotic -- it
    is the expected outcome of two agents choosing an id without talking to
    each other -- and the old shape of this path let the second one overwrite
    the first while being told it had created the row.
    """


_PARENT_CYCLE_WALK_MAX_DEPTH = 200


def _depends_on_ids(raw: Any) -> list[str]:
    value = raw
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, dict):
        value = list(value.keys())
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value).strip()
    return [text] if text else []


def _walk_parent_chain_for_cycle_unlocked(
    conn: sqlite3.Connection, start_parent_id: str, *, work_id: str
) -> None:
    seen: set[str] = set()
    current = str(start_parent_id or "").strip()
    depth = 0
    while current:
        if current == work_id:
            raise ParentCycleError(
                f"parent_cycle: assigning parent_id={start_parent_id!r} to "
                f"work_id={work_id!r} would close a cycle in the parent_id graph"
            )
        if current in seen:
            return
        seen.add(current)
        depth += 1
        if depth > _PARENT_CYCLE_WALK_MAX_DEPTH:
            return
        row = conn.execute(
            "SELECT parent_id FROM work_items WHERE work_id=?", (current,)
        ).fetchone()
        current = str(row["parent_id"] or "").strip() if row else ""


def _validate_relation_invariants_unlocked(
    conn: sqlite3.Connection,
    work_id: str,
    changed: dict[str, Any],
) -> None:
    if "parent_id" in changed:
        parent = str(changed["parent_id"] or "").strip()
        if parent:
            if parent == work_id:
                raise SelfParentError(
                    f"self_parent: work_id={work_id!r} cannot be its own parent_id"
                )
            _walk_parent_chain_for_cycle_unlocked(conn, parent, work_id=work_id)
    if "depends_on" in changed:
        for dep in _depends_on_ids(changed["depends_on"]):
            if dep == work_id:
                raise SelfDependencyError(
                    f"self_dependency: work_id={work_id!r} cannot list itself in depends_on"
                )
            if not conn.execute(
                "SELECT 1 FROM work_items WHERE work_id=? LIMIT 1", (dep,)
            ).fetchone():
                _logger.warning(
                    "coord_db: work_id=%s depends_on references unseeded work_id=%s"
                    " (not rejected — rows may be seeded before their prerequisites)",
                    work_id,
                    dep,
                )


def _upsert_work_unlocked(
    conn, work_id: str, fields: dict[str, Any], *, require_new: bool = False
) -> bool:
    """Insert or amend one work row; return True only when a row was inserted.

    ``require_new`` is the create path's guarantee. The default stays an upsert
    because the amend branch is load-bearing elsewhere -- claim-time
    born-complete metadata legitimately fills fields on a row that already
    exists -- and only a caller claiming authorship of a *new* row asks for the
    collision to be surfaced instead of merged.
    """
    t = db_now(conn)
    existing = conn.execute(
        "SELECT * FROM work_items WHERE work_id=?", (work_id,)
    ).fetchone()
    if existing is not None and require_new:
        raise WorkIdCollisionError(_work_id_collision_message(existing, work_id))
    if existing is None:
        fields = dict(fields)
        _validate_relation_invariants_unlocked(conn, work_id, fields)
        cols = ["work_id", "title", "created_at", "updated_at"]
        vals = [work_id, fields.get("title", work_id), t, t]
        for key, value in fields.items():
            if key == "title":
                continue
            cols.append(key)
            vals.append(value)
        try:
            conn.execute(
                f"INSERT INTO work_items({','.join(cols)}) VALUES ({','.join('?' * len(vals))})",
                vals,
            )
        except sqlite3.IntegrityError as exc:
            # Belt and braces. The SELECT above runs inside BEGIN IMMEDIATE, so
            # no writer can land between it and this INSERT; if the primary key
            # ever does collide anyway, it surfaces as the collision it is
            # rather than as storage-layer vocabulary. Only the work_id key is
            # translated -- a foreign key failure is a different bug and keeps
            # its own error.
            message = str(exc).lower()
            if "work_items.work_id" in message or "work_items primary key" in message:
                raise WorkIdCollisionError(
                    f"work_id_collision: {work_id!r} already exists; the create was "
                    "refused and nothing was overwritten"
                ) from exc
            raise
        inserted = True
    else:
        inserted = False
    if existing is not None and fields:
        changed = {
            key: value for key, value in fields.items() if existing[key] != value
        }
        _validate_relation_invariants_unlocked(conn, work_id, changed)
        sets = ", ".join(f"{key}=?" for key in changed)
        if changed:
            conn.execute(
                f"UPDATE work_items SET {sets}, updated_at=?, version=version+1 WHERE work_id=?",
                [*changed.values(), t, work_id],
            )
    display = str(fields.get("display") or "").strip()
    if display:
        conn.execute(
            "INSERT INTO display_titles(key, display, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET display=excluded.display, updated_at=excluded.updated_at"
            " WHERE display_titles.display!=excluded.display",
            (work_id, display, t),
        )
    return inserted


def _work_id_collision_message(existing: sqlite3.Row, work_id: str) -> str:
    owner = str(existing["assignee"] or "").strip() or "an unrecorded lane"
    session = str(existing["created_by_session_id"] or "").strip()
    title = str(existing["title"] or "").strip() or work_id
    by = f" by session {session}" if session else ""
    return (
        f"work_id_collision: {work_id!r} already exists -- {title!r}, owned by "
        f"{owner}{by}. The create was refused and nothing was overwritten. "
        "Choose a different work id; to add to the existing row, address it by "
        "id with the verb for the change you want."
    )


def _public_writer_creation(fields: dict[str, Any]) -> bool:
    raw = fields.get("authority_declaration_json")
    try:
        declaration = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    return str(declaration.get("writer") or "").strip() == "coord_db.public_writer_doorway"


def _referenced_event_rows_unlocked(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> list[sqlite3.Row]:
    from .review_tier import event_ref_ids

    ids = event_ref_ids(
        (
            row.get("context_pack_ref"),
            row.get("depends_on"),
            row.get("note"),
        )
    )
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return list(
        conn.execute(
            f"SELECT * FROM events WHERE event_id IN ({placeholders}) ORDER BY event_id",
            ids,
        ).fetchall()
    )


def _tier_correction_event_authorizes_unlocked(
    event: sqlite3.Row,
    work_id: str,
    row: dict[str, Any],
) -> bool:
    try:
        payload = json.loads(str(event["payload_json"] or "{}"))
        refs = json.loads(str(event["refs_json"] or "[]"))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(refs, list) or not refs:
        return False
    correction_actor = str(event["actor"] or "").strip().lower()
    event_session = str(event["session_id"] or "").strip()
    current_assignee = str(row.get("assignee") or "").strip().lower()
    payload_assignee = str(
        payload.get("assignee_at_correction") or ""
    ).strip().lower()
    corrected_assignee = payload_assignee or current_assignee
    from_tier = str(payload.get("from_tier") or "").strip().upper()
    to_tier = str(payload.get("to_tier") or "").strip().upper()
    declared_tier = str(row.get("tier") or "").strip().upper()
    reason = str(payload.get("reason") or "").strip()
    idempotency_key = str(event["idempotency_key"] or "").strip()
    expected_version = payload.get("expected_version")
    rank = {"T0": 0, "T1": 1, "T2": 2}
    return bool(
        str(event["kind"] or "").strip().lower() == "tier_corrected"
        and str(event["trust"] or "").strip().lower() == "agent"
        and str(event["work_id"] or "").strip() == work_id
        and correction_actor in _lane_set()
        and expected_actor_for_session_id(event_session) == correction_actor
        and corrected_assignee in _lane_set()
        and current_assignee == corrected_assignee
        and correction_actor != corrected_assignee
        and str(event["to_selector"] or "").strip().lower()
        == f"actor:{corrected_assignee}"
        and payload.get("schema_version") == 1
        and payload.get("review_intensity_lowered") is True
        and from_tier in rank
        and to_tier in rank
        and rank[to_tier] > rank[from_tier]
        and to_tier == declared_tier
        and isinstance(expected_version, int)
        and not isinstance(expected_version, bool)
        and expected_version >= 0
        and int(row.get("version") or 0) >= expected_version + 1
        and reason
        and str(event["body"] or "").strip() == reason
        and str(event["title"] or "").strip()
        == f"Review tier corrected {from_tier} -> {to_tier}"
        and idempotency_key.startswith(f"tier-correction:{correction_actor}:")
        and len(idempotency_key) > len(f"tier-correction:{correction_actor}:")
    )


def _tier_down_authorized_unlocked(
    conn: sqlite3.Connection,
    work_id: str,
    row: dict[str, Any],
) -> bool:
    assignee = str(row.get("assignee") or "").strip().lower()
    bound_correction_id = row.get("tier_correction_event_id")
    if "tier_correction_event_id" not in row:
        binding_row = conn.execute(
            "SELECT tier_correction_event_id FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
        bound_correction_id = (
            binding_row["tier_correction_event_id"] if binding_row is not None else None
        )
    try:
        bound_correction_id = int(bound_correction_id or 0)
    except (TypeError, ValueError):
        bound_correction_id = 0
    if bound_correction_id > 0:
        correction = conn.execute(
            "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key"
            " FROM events WHERE event_id=?",
            (bound_correction_id,),
        ).fetchone()
        if correction is not None and _tier_correction_event_authorizes_unlocked(
            correction, work_id, row
        ):
            return True
    for event in _referenced_event_rows_unlocked(conn, row):
        kind = str(event["kind"] or "").strip().lower()
        actor = str(event["actor"] or "").strip().lower()
        verdict = str(event["verdict"] or "").strip().upper()
        event_work_id = str(event["work_id"] or "").strip()
        bound_operator_event_id = int(row.get("operator_ok_event_id") or 0)
        if (
            kind == "operator_ok"
            and bound_operator_event_id == int(event["event_id"])
            and _operator_ok_event_is_valid_unlocked(event, work_id, row)
        ):
            return True
        opposite_lane = (
            assignee in _lane_set()
            and actor in _lane_set()
            and actor != assignee
        )
        if opposite_lane and (
            kind == "tier_ack"
            or (kind == "audit_verdict" and verdict == "PASS")
        ):
            if event_work_id == work_id:
                return True
            payload = str(event["payload_json"] or "")
            refs = str(event["refs_json"] or "")
            if work_id in payload or work_id in refs:
                return True
    return False


def tier_down_authorized_for_fields(
    conn: sqlite3.Connection,
    work_id: str,
    row: dict[str, Any],
) -> bool:
    return _tier_down_authorized_unlocked(conn, str(work_id), dict(row))


def _valid_cross_lane_audit_request_ref_unlocked(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> bool:
    assignee = str(row.get("assignee") or "").strip().lower()
    if assignee not in _lane_set():
        return False
    for event in _referenced_event_rows_unlocked(conn, row):
        if str(event["kind"] or "").strip().lower() != "audit_request":
            continue
        actor = str(event["actor"] or "").strip().lower()
        target = str(event["to_selector"] or "").strip().lower()
        if actor in _lane_set() and actor != assignee and target == f"actor:{assignee}":
            return True
    return False


def _validate_work_policy_unlocked(
    conn: sqlite3.Connection,
    work_id: str,
    existing: sqlite3.Row | None,
    fields: dict[str, Any],
) -> None:
    from .creation_lint import (
        CreationLintError,
        acceptance_requires_assignee_pass,
        validate_creation_policy,
    )
    from .review_tier import (
        ReviewTierPolicyError,
        is_review_row,
        validate_done_signal_grammar,
        validate_tier_declaration,
    )

    merged = dict(existing) if existing is not None else {"work_id": work_id}
    merged.update(fields)
    existing_intent = (
        str(existing["intent_state"] or "").strip().lower()
        if existing is not None
        else ""
    )
    proposed_intent = str(fields.get("intent_state") or "").strip().lower()
    if proposed_intent == "paused" and existing_intent != "paused":
        require_park_resume_contract(
            next_step=fields.get("next_step"),
            resume_when=fields.get("resume_when"),
        )
    elif existing_intent == "paused" and {
        "next_step",
        "resume_when",
    }.intersection(fields):
        require_park_resume_contract(
            next_step=merged.get("next_step"),
            resume_when=merged.get("resume_when"),
        )
    existing_done_signal = (
        str(existing["done_signal"] or "").strip() if existing is not None else ""
    )
    proposed_done_signal = str(fields.get("done_signal") or "").strip()
    public_writer_row = _public_writer_creation(fields) or (
        existing is not None and _public_writer_creation(dict(existing))
    )
    protected_receipt_writes = {
        key
        for key in (
            "rubric_verdict",
            "operator_ok_event_id",
            "tier_correction_event_id",
        )
        if key in fields
        and fields.get(key) not in (None, "")
        and (existing is None or existing[key] != fields.get(key))
    }
    if protected_receipt_writes:
        names = ", ".join(sorted(protected_receipt_writes))
        raise ReviewTierPolicyError(
            f"{names} are typed receipt fields and cannot be written through "
            "upsert_work; use post_audit_verdict or the authenticated"
            " resident operator controller"
        )
    _CLEANUP_TERMINAL = {"closed", "superseded", "archived", "canceled", "cancelled"}
    is_cleanup_close = str(merged.get("intent_state") or "").strip().lower() in _CLEANUP_TERMINAL
    if "done_signal" in fields and (
        existing is None or proposed_done_signal != existing_done_signal
    ) and public_writer_row and not is_cleanup_close:
        validate_done_signal_grammar(fields.get("done_signal"))
    if ("acceptance_json" in fields or "assignee" in fields) and not is_cleanup_close:
        if acceptance_requires_assignee_pass(
            merged.get("acceptance_json"),
            merged.get("assignee"),
        ):
            lane = str(merged.get("assignee") or "").strip().lower()
            raise ReviewTierPolicyError(
                f"acceptance_json requires assignee lane {lane} to PASS its own row; "
                "same-lane PASS is unconditionally forbidden. Use opposite-lane T0 review "
                "or an operator-ok event; see "
                "docs/review-tiers.md"
            )

    policy_keys = {
        "tier",
        "title",
        "acceptance_json",
        "done_signal",
        "context_pack_ref",
        "depends_on",
        "kind",
        "module",
        "sublane",
        "assignee",
    }
    if existing is None or policy_keys.intersection(fields):
        tier_down_authorized = _tier_down_authorized_unlocked(conn, work_id, merged)
        validate_tier_declaration(
            merged,
            tier_down_authorized=tier_down_authorized,
        )

    if existing is None and _public_writer_creation(fields):
        try:
            review_tier = validate_creation_policy(
                work_id,
                merged,
                tier_down_authorized=_tier_down_authorized_unlocked(
                    conn, work_id, merged
                ),
            )
        except CreationLintError as exc:
            raise ReviewTierPolicyError(str(exc)) from exc
        if is_review_row(work_id, merged) and (
            review_tier != "T0"
            or not _valid_cross_lane_audit_request_ref_unlocked(conn, merged)
        ):
            raise ReviewTierPolicyError(
                "review-row creation is T0-only and requires an exact referenced "
                "cross-lane audit_request event; see "
                "docs/review-tiers.md"
            )


def upsert_work(conn, work_id: str, **fields) -> str:
    bad = set(fields) - _WORK_COLS
    if bad:
        raise ValueError(f"unknown work_items columns: {bad}")
    with tx(conn):
        existing = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        _validate_work_policy_unlocked(conn, work_id, existing, fields)
        _upsert_work_unlocked(conn, work_id, dict(fields))
    return work_id


def create_work(conn, work_id: str, **fields) -> bool:
    """Insert a genuinely new work row, or refuse.

    This is the difference between ``coord create`` and every other writer in
    here. An upsert that finds the id taken merges into the row it found and
    the caller cannot tell; a create that finds the id taken has been beaten to
    it, and the only honest answer is a refusal. The existence check runs
    inside the same ``BEGIN IMMEDIATE`` transaction as the insert, so a second
    session cannot slip between them -- which a check taken before the
    transaction opened could not promise.

    Returns True, the measured outcome of the insert branch, so a caller
    reporting ``created`` reports what happened rather than what it intended.
    Raises WorkIdCollisionError when the id is already someone else's row.
    """
    bad = set(fields) - _WORK_COLS
    if bad:
        raise ValueError(f"unknown work_items columns: {bad}")
    with tx(conn):
        existing = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        if existing is not None:
            raise WorkIdCollisionError(_work_id_collision_message(existing, work_id))
        _validate_work_policy_unlocked(conn, work_id, existing, fields)
        return _upsert_work_unlocked(conn, work_id, dict(fields), require_new=True)


def apply_legacy_handoff_work(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    fields: dict[str, Any],
    intent_state: str | None = None,
    transition_reason: str | None = None,
) -> list[str]:

    clean_work_id = str(work_id or "").strip()
    if not clean_work_id:
        raise ValueError("legacy handoff requires work_id")
    effective = dict(fields or {})
    bad = set(effective) - _WORK_COLS
    if bad:
        raise ValueError(f"unknown work_items columns: {bad}")
    target = str(intent_state or "").strip().lower()
    if target and target not in {"blocked", "queued"}:
        raise ValueError(
            f"legacy controller transition requires blocked|queued, got {target!r}"
        )
    reason = str(transition_reason or "").strip()
    if target and not reason:
        raise ValueError("legacy controller transition requires a reason")

    with tx(conn):
        existing = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        conflicts: list[str] = []
        if existing is not None:
            current_assignee = str(existing["assignee"] or "").strip().lower()
            proposed_assignee = str(effective.get("assignee") or "").strip().lower()
            if (
                "assignee" in effective
                and current_assignee
                and current_assignee != proposed_assignee
            ):
                conflicts.append("assignee")

            current_acceptance = str(existing["acceptance_json"] or "").strip()
            proposed_acceptance = str(
                effective.get("acceptance_json") or ""
            ).strip()
            current_acceptance_nonempty = bool(current_acceptance)
            if current_acceptance_nonempty:
                try:
                    current_acceptance_nonempty = bool(
                        json.loads(current_acceptance)
                    )
                except (TypeError, json.JSONDecodeError):
                    current_acceptance_nonempty = True
            if (
                "acceptance_json" in effective
                and current_acceptance_nonempty
                and current_acceptance != proposed_acceptance
            ):
                conflicts.append("acceptance_json")

        if conflicts:
            raise ValueError(
                "legacy handoff refuses destructive existing-row amendment of "
                f"{', '.join(conflicts)} for {clean_work_id}; use the typed "
                "CAS-fenced handoff-existing path for an assignment transfer, "
                "and preserve the row's declared acceptance contract"
            )

        if target:
            effective["intent_state"] = target
        _validate_work_policy_unlocked(conn, clean_work_id, existing, effective)

        held_ids: list[str] = []
        if target:
            held = conn.execute(
                "SELECT claim_id FROM claims WHERE work_id=? "
                "AND status IN ('running','paused','blocked')",
                (clean_work_id,),
            ).fetchall()
            held_ids = [str(row["claim_id"]) for row in held]
            if held_ids:
                placeholders = ",".join("?" for _ in held_ids)
                conn.execute(
                    f"UPDATE claims SET status='released', release_reason=?, "
                    f"version=version+1 WHERE claim_id IN ({placeholders})",
                    (reason, *held_ids),
                )
        _upsert_work_unlocked(conn, clean_work_id, effective)
        return held_ids


def backfill_work_context(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    expected_assignee: str,
    note: str,
    operation_id: str,
    sublane: str | None = None,
) -> dict[str, Any]:

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_assignee = str(expected_assignee or "").strip().lower()
    clean_note = str(note or "").strip()
    clean_sublane = str(sublane or "").strip() or None
    clean_operation = str(operation_id or "").strip()
    if not clean_work_id:
        raise ValueError("context backfill requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"context backfill actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if clean_assignee not in _lane_set():
        raise ValueError(f"context backfill expected_assignee must be {_lanes_display()}")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError("context backfill expected_version must be a non-negative integer")
    if not clean_note or len(clean_note) > 2048:
        raise ValueError("context backfill note must contain 1-2048 characters")
    if clean_sublane and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,63}", clean_sublane):
        raise ValueError("context backfill sublane must be a 2-64 character lowercase slug")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", clean_operation):
        raise ValueError("context backfill operation_id must be an 8-128 character stable id")

    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "actor": clean_actor,
        "session_id": clean_session,
        "expected_version": expected_version,
        "expected_assignee": clean_assignee,
        "note": clean_note,
        "sublane": clean_sublane,
        "operation_id": clean_operation,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"work-context-backfill:{clean_operation}"

    with tx(conn):
        replay = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            payload = json.loads(str(replay["payload_json"] or "{}"))
            if payload.get("request_sha256") != request_sha:
                raise ValueError("context backfill operation_id collision")
            return {
                "work_id": clean_work_id,
                "event_id": int(replay["event_id"]),
                "replayed": True,
                "version": payload.get("new_version"),
            }

        row = conn.execute(
            "SELECT work_id,assignee,intent_state,version,note,module,sublane "
            "FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"context backfill work item not found: {clean_work_id!r}")
        if int(row["version"] or 0) != expected_version:
            raise ValueError(
                f"context backfill version mismatch for {clean_work_id}: "
                f"expected {expected_version}, observed {row['version']}"
            )
        observed_assignee = str(row["assignee"] or "").strip().lower()
        if observed_assignee != clean_assignee:
            raise ValueError(
                f"context backfill assignee mismatch for {clean_work_id}: "
                f"expected {clean_assignee}, observed {observed_assignee or '<empty>'}"
            )
        if str(row["intent_state"] or "").strip().lower() in TERMINAL_WORK_STATES:
            raise ValueError("context backfill refuses a terminal work item")
        if str(row["note"] or "").strip():
            raise ValueError("context backfill refuses to replace an existing note")
        if clean_sublane and str(row["sublane"] or "").strip():
            raise ValueError("context backfill refuses to replace an existing sublane")

        updates: dict[str, Any] = {"note": clean_note}
        if clean_sublane:
            updates["sublane"] = clean_sublane
        t = db_now(conn)
        sets = ",".join(f"{key}=?" for key in updates)
        conn.execute(
            f"UPDATE work_items SET {sets},updated_at=?,version=version+1 WHERE work_id=?",
            (*updates.values(), t, clean_work_id),
        )
        new_version = expected_version + 1
        payload = {
            **request,
            "request_sha256": request_sha,
            "prior_module": row["module"],
            "prior_sublane": row["sublane"],
            "new_version": new_version,
            "lifecycle_mutation": False,
            "claim_mutation": False,
            "ownership_mutation": False,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,payload_json,"
            "idempotency_key) VALUES (?,?,?,?,?,'agent',?,?)",
            (
                t,
                "work_context_backfilled",
                clean_actor,
                clean_session,
                clean_work_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "event_id": int(cur.lastrowid),
            "replayed": False,
            "version": new_version,
            "sublane": clean_sublane or row["sublane"],
        }


@contextmanager
def _active_acceptance_repair_transaction(conn: sqlite3.Connection):
    if not conn.in_transaction:
        raise RuntimeError("acceptance repair internal writer requires an active transaction")
    yield conn


def _repair_work_acceptance_contract_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_old_sha256: str,
    replacement_acceptance_json: str,
    source_ref: str,
    source_sha256: str,
    repair_kind: str,
    operation_id: str,
    source_root: str | Path | None = None,
) -> dict[str, Any]:

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_assignee = str(expected_assignee or "").strip().lower()
    clean_old_sha = str(expected_old_sha256 or "").strip().lower()
    clean_source_ref = str(source_ref or "").strip()
    clean_source_sha = str(source_sha256 or "").strip().lower()
    clean_kind = str(repair_kind or "").strip().lower()
    clean_operation = str(operation_id or "").strip()
    replacement_raw = str(replacement_acceptance_json or "")

    if not clean_work_id:
        raise ValueError("acceptance repair requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"acceptance repair actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if clean_assignee not in _lane_set():
        raise ValueError(f"acceptance repair expected_assignee must be {_lanes_display()}")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError("acceptance repair expected_version must be a non-negative integer")
    if not re.fullmatch(r"[0-9a-f]{64}", clean_old_sha):
        raise ValueError("acceptance repair expected_old_sha256 must be lowercase sha256")
    if not clean_source_ref or len(clean_source_ref) > 2048:
        raise ValueError("acceptance repair source_ref must contain 1-2048 characters")
    if not re.fullmatch(r"[0-9a-f]{64}", clean_source_sha):
        raise ValueError("acceptance repair source_sha256 must be lowercase sha256")
    if clean_kind != "truncation_extension":
        raise ValueError("acceptance repair kind must be truncation_extension")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", clean_operation):
        raise ValueError("acceptance repair operation_id must be an 8-128 character stable id")
    if not replacement_raw or len(replacement_raw.encode("utf-8")) > 1_000_000:
        raise ValueError("acceptance repair replacement must contain 1-1000000 UTF-8 bytes")
    try:
        replacement_parsed = json.loads(replacement_raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("acceptance repair replacement is not valid JSON") from exc
    if not isinstance(replacement_parsed, dict) or not isinstance(
        replacement_parsed.get("acceptance"), str
    ):
        raise ValueError(
            "acceptance repair replacement must be an object with string acceptance"
        )
    replacement_text = replacement_parsed["acceptance"]
    if not replacement_text.strip():
        raise ValueError("acceptance repair replacement acceptance is empty")
    replacement_sha = hashlib.sha256(replacement_raw.encode("utf-8")).hexdigest()

    root = Path(source_root or Path(HARNESS_ROOT).parent).resolve()
    source_rel = Path(clean_source_ref)
    if source_rel.is_absolute() or ".." in source_rel.parts:
        raise ValueError("acceptance repair source_ref must be repo-relative")
    source_candidate = root / source_rel
    if source_candidate.is_symlink():
        raise ValueError("acceptance repair source_ref must not be a symlink")
    source_path = source_candidate.resolve()
    if source_path != root and root not in source_path.parents:
        raise ValueError("acceptance repair source_ref resolves outside source_root")
    if not source_path.is_file():
        raise ValueError("acceptance repair source_ref must resolve to a regular file")
    observed_source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if observed_source_sha != clean_source_sha:
        raise ValueError(
            "acceptance repair source-sha mismatch: "
            f"expected {clean_source_sha}, observed {observed_source_sha}"
        )

    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "actor": clean_actor,
        "session_id": clean_session,
        "expected_version": expected_version,
        "expected_assignee": clean_assignee,
        "expected_old_sha256": clean_old_sha,
        "replacement_sha256": replacement_sha,
        "source_ref": clean_source_ref,
        "source_sha256": clean_source_sha,
        "repair_kind": clean_kind,
        "operation_id": clean_operation,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"acceptance-contract-repair:{clean_operation}"

    with _active_acceptance_repair_transaction(conn):
        replay = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            payload = json.loads(str(replay["payload_json"] or "{}"))
            if payload.get("request_sha256") != request_sha:
                raise ValueError("acceptance repair operation_id collision")
            return {
                "work_id": clean_work_id,
                "event_id": int(replay["event_id"]),
                "replayed": True,
                "version": payload.get("new_version"),
                "changed_fields": payload.get("changed_fields", []),
            }

        row = conn.execute(
            "SELECT work_id,assignee,intent_state,version,acceptance_json,"
            "rubric_verdict,operator_ok_event_id,completion_requested_at "
            "FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"acceptance repair work item not found: {clean_work_id!r}")
        if int(row["version"] or 0) != expected_version:
            raise ValueError(
                f"acceptance repair version mismatch for {clean_work_id}: "
                f"expected {expected_version}, observed {row['version']}"
            )
        observed_assignee = str(row["assignee"] or "").strip().lower()
        if observed_assignee != clean_assignee:
            raise ValueError(
                f"acceptance repair assignee mismatch for {clean_work_id}: "
                f"expected {clean_assignee}, observed {observed_assignee or '<empty>'}"
            )
        prior_raw = str(row["acceptance_json"] or "")
        prior_sha = hashlib.sha256(prior_raw.encode("utf-8")).hexdigest()
        if prior_sha != clean_old_sha:
            raise ValueError(
                f"acceptance repair old-sha mismatch for {clean_work_id}: "
                f"expected {clean_old_sha}, observed {prior_sha}"
            )
        try:
            prior_parsed = json.loads(prior_raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("acceptance repair refuses malformed prior JSON") from exc
        if not isinstance(prior_parsed, dict) or not isinstance(
            prior_parsed.get("acceptance"), str
        ):
            raise ValueError(
                "acceptance repair prior value must be an object with string acceptance"
            )
        prior_text = prior_parsed["acceptance"]
        if len(replacement_text) <= len(prior_text):
            raise ValueError("acceptance repair refuses a non-extending or shorter contract")
        if not replacement_text.startswith(prior_text):
            raise ValueError(
                "acceptance truncation repair requires replacement to extend prior text"
            )

        changed_fields = ["acceptance_json"]
        updates: dict[str, Any] = {"acceptance_json": replacement_raw}
        prior_verdict = str(row["rubric_verdict"] or "").strip().lower()
        if prior_verdict == "pass":
            updates["rubric_verdict"] = None
            changed_fields.append("rubric_verdict")
        if row["operator_ok_event_id"] is not None:
            updates["operator_ok_event_id"] = None
            changed_fields.append("operator_ok_event_id")
        if row["completion_requested_at"] is not None:
            updates["completion_requested_at"] = None
            changed_fields.append("completion_requested_at")

        t = db_now(conn)
        sets = ",".join(f"{key}=?" for key in updates)
        conn.execute(
            f"UPDATE work_items SET {sets},updated_at=?,version=version+1 "
            "WHERE work_id=?",
            (*updates.values(), t, clean_work_id),
        )
        new_version = expected_version + 1
        payload = {
            **request,
            "request_sha256": request_sha,
            "prior_acceptance_chars": len(prior_text),
            "replacement_acceptance_chars": len(replacement_text),
            "prior_intent_state": row["intent_state"],
            "prior_rubric_verdict": row["rubric_verdict"],
            "new_version": new_version,
            "changed_fields": changed_fields,
            "field_mutation": True,
            "lifecycle_mutation": False,
            "claim_mutation": False,
            "ownership_mutation": False,
            "prior_pass_invalidated": prior_verdict == "pass",
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            "title,body,refs_json,payload_json,idempotency_key) "
            "VALUES (?,?,?,?,?,?, 'agent',?,?,?,?,?)",
            (
                t,
                "acceptance_contract_repaired",
                clean_actor,
                clean_session,
                f"actor:{clean_assignee}",
                clean_work_id,
                "Acceptance contract restored from exact source",
                (
                    f"FIELD repair {clean_kind}: {len(prior_text)} -> "
                    f"{len(replacement_text)} acceptance characters; prior PASS "
                    "invalidated when present."
                ),
                json.dumps([clean_source_ref]),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "event_id": int(cur.lastrowid),
            "replayed": False,
            "version": new_version,
            "changed_fields": changed_fields,
            "prior_acceptance_chars": len(prior_text),
            "replacement_acceptance_chars": len(replacement_text),
        }


def repair_work_acceptance_contract(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_old_sha256: str,
    replacement_acceptance_json: str,
    source_ref: str,
    source_sha256: str,
    repair_kind: str,
    operation_id: str,
    source_root: str | Path | None = None,
) -> dict[str, Any]:

    with tx(conn):
        return _repair_work_acceptance_contract_in_transaction(
            conn,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            expected_version=expected_version,
            expected_assignee=expected_assignee,
            expected_old_sha256=expected_old_sha256,
            replacement_acceptance_json=replacement_acceptance_json,
            source_ref=source_ref,
            source_sha256=source_sha256,
            repair_kind=repair_kind,
            operation_id=operation_id,
            source_root=source_root,
        )


def repair_work_acceptance_contract_batch(
    conn: sqlite3.Connection,
    *,
    repairs: Iterable[dict[str, Any]],
    source_root: str | Path | None = None,
) -> list[dict[str, Any]]:

    requests = [dict(item) for item in repairs]
    if not requests:
        raise ValueError("acceptance repair batch requires at least one row")
    with tx(conn):
        return [
            _repair_work_acceptance_contract_in_transaction(
                conn,
                source_root=source_root,
                **request,
            )
            for request in requests
        ]


def _validated_repo_context_pointer(
    value: str,
    *,
    repo_root: Path | str | None = None,
) -> tuple[str, int, Path]:

    clean = str(value or "").strip()
    match = re.fullmatch(r"([^:\n]+):([1-9][0-9]*)", clean)
    if match is None:
        raise ValueError("context pointer must be an exact repo-local path:line")
    rel = Path(match.group(1))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("context pointer must remain inside the repository")
    root = Path(repo_root or Path(HARNESS_ROOT).parent).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError("context pointer resolves outside the repository")
    if not target.is_file():
        raise ValueError(f"context pointer target does not exist: {rel.as_posix()}")
    line_no = int(match.group(2))
    with target.open(errors="replace") as handle:
        line_count = sum(1 for _line in handle)
    if line_no > line_count:
        raise ValueError(
            f"context pointer line {line_no} is outside {rel.as_posix()} "
            f"(line_count={line_count})"
        )
    return clean, line_no, target


def correct_work_context_pointer(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_old_ref: str,
    new_ref: str,
    evidence_refs: Iterable[str],
    operation_id: str,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_assignee = str(expected_assignee or "").strip().lower()
    clean_old = str(expected_old_ref or "").strip()
    clean_new, line_no, target = _validated_repo_context_pointer(
        new_ref,
        repo_root=repo_root,
    )
    clean_evidence = sorted(
        {str(ref).strip() for ref in evidence_refs if str(ref).strip()}
    )
    clean_operation = str(operation_id or "").strip()
    if not clean_work_id:
        raise ValueError("context pointer correction requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"context pointer correction actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if clean_assignee not in _lane_set():
        raise ValueError(
            f"context pointer correction expected_assignee must be {_lanes_display()}"
        )
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError(
            "context pointer correction expected_version must be a non-negative integer"
        )
    if not clean_old:
        raise ValueError("context pointer correction requires a nonempty expected old ref")
    if clean_old == clean_new:
        raise ValueError("context pointer correction requires different old/new refs")
    try:
        _validated_repo_context_pointer(clean_old, repo_root=repo_root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "context pointer correction refuses to replace a valid existing pointer"
        )
    if not clean_evidence:
        raise ValueError("context pointer correction requires at least one evidence ref")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", clean_operation):
        raise ValueError(
            "context pointer correction operation_id must be an 8-128 character stable id"
        )

    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "actor": clean_actor,
        "session_id": clean_session,
        "expected_version": expected_version,
        "expected_assignee": clean_assignee,
        "expected_old_ref": clean_old,
        "new_ref": clean_new,
        "evidence_refs": clean_evidence,
        "operation_id": clean_operation,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"work-context-pointer-correction:{clean_operation}"

    with tx(conn):
        replay = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            payload = json.loads(str(replay["payload_json"] or "{}"))
            if payload.get("request_sha256") != request_sha:
                raise ValueError("context pointer correction operation_id collision")
            return {
                "work_id": clean_work_id,
                "event_id": int(replay["event_id"]),
                "replayed": True,
                "version": payload.get("new_version"),
                "context_pack_ref": clean_new,
            }

        row = conn.execute(
            "SELECT assignee,intent_state,version,context_pack_ref "
            "FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"context pointer correction work item not found: {clean_work_id!r}"
            )
        if int(row["version"] or 0) != expected_version:
            raise ValueError(
                f"context pointer correction version mismatch for {clean_work_id}: "
                f"expected {expected_version}, observed {row['version']}"
            )
        observed_assignee = str(row["assignee"] or "").strip().lower()
        if observed_assignee != clean_assignee:
            raise ValueError(
                f"context pointer correction assignee mismatch for {clean_work_id}: "
                f"expected {clean_assignee}, observed {observed_assignee or '<empty>'}"
            )
        observed_old = str(row["context_pack_ref"] or "").strip()
        if observed_old != clean_old:
            raise ValueError(
                f"context pointer correction old-ref mismatch for {clean_work_id}: "
                f"expected {clean_old!r}, observed {observed_old!r}"
            )

        t = db_now(conn)
        cur = conn.execute(
            "UPDATE work_items SET context_pack_ref=?,updated_at=?,version=version+1 "
            "WHERE work_id=? AND version=? AND lower(COALESCE(assignee,''))=? "
            "AND context_pack_ref=?",
            (
                clean_new,
                t,
                clean_work_id,
                expected_version,
                clean_assignee,
                clean_old,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError("context pointer correction lost its CAS fence")
        new_version = expected_version + 1
        payload = {
            **request,
            "request_sha256": request_sha,
            "new_version": new_version,
            "validated_line": line_no,
            "validated_repo_path": clean_new.rsplit(":", 1)[0],
            "validated_size_bytes": target.stat().st_size,
            "lifecycle_mutation": False,
            "claim_mutation": False,
            "ownership_mutation": False,
            "changed_fields": ["context_pack_ref"],
        }
        event = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,refs_json,"
            "payload_json,idempotency_key) VALUES (?,?,?,?,?,'agent',?,?,?)",
            (
                t,
                "work_context_pointer_corrected",
                clean_actor,
                clean_session,
                clean_work_id,
                json.dumps(clean_evidence),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "event_id": int(event.lastrowid),
            "replayed": False,
            "version": new_version,
            "context_pack_ref": clean_new,
        }


def reconcile_invalid_projection_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    expected_intent_state: str,
    expected_title: str,
    expected_display: str,
    expected_assignee: str,
    expected_module: str,
    expected_done_signal: str,
    expected_claim_ids: Iterable[str],
    reason: str,
    evidence_refs: Iterable[str],
    operation_id: str,
    apply: bool = False,
    proof_root: Path | str | None = None,
) -> dict[str, Any]:

    from .creation_lint import row_quality_missing_fields

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_intent = str(expected_intent_state or "").strip().lower()
    clean_claim_ids = sorted(
        {str(claim_id).strip() for claim_id in expected_claim_ids if str(claim_id).strip()}
    )
    clean_reason = str(reason or "").strip()
    clean_evidence = sorted(
        {str(ref).strip() for ref in evidence_refs if str(ref).strip()}
    )
    clean_operation = str(operation_id or "").strip()
    if not clean_work_id:
        raise ValueError("invalid projection reconciliation requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"invalid projection reconciliation actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError(
            "invalid projection reconciliation expected_version must be non-negative"
        )
    if clean_intent not in {"planned", "queued"}:
        raise ValueError(
            "invalid projection reconciliation requires planned|queued preimage"
        )
    if not clean_claim_ids:
        raise ValueError(
            "invalid projection reconciliation requires exact expired claim ids"
        )
    if not clean_reason or len(clean_reason) > 2048:
        raise ValueError(
            "invalid projection reconciliation reason must contain 1-2048 characters"
        )
    if not clean_evidence:
        raise ValueError(
            "invalid projection reconciliation requires at least one evidence ref"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", clean_operation):
        raise ValueError(
            "invalid projection reconciliation operation_id must be an 8-128 character stable id"
        )

    expected = {
        "intent_state": clean_intent,
        "title": str(expected_title or ""),
        "display": str(expected_display or ""),
        "assignee": str(expected_assignee or ""),
        "module": str(expected_module or ""),
        "done_signal": str(expected_done_signal or ""),
    }
    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "actor": clean_actor,
        "session_id": clean_session,
        "expected_version": expected_version,
        "expected": expected,
        "expected_claim_ids": clean_claim_ids,
        "reason": clean_reason,
        "evidence_refs": clean_evidence,
        "operation_id": clean_operation,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"invalid-projection-reconciliation:{clean_operation}"

    with tx(conn):
        replay = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            payload = json.loads(str(replay["payload_json"] or "{}"))
            if payload.get("request_sha256") != request_sha:
                raise ValueError(
                    "invalid projection reconciliation operation_id collision"
                )
            return {
                "status": "replayed",
                "work_id": clean_work_id,
                "event_id": int(replay["event_id"]),
                "replayed": True,
                "version": payload.get("new_version"),
            }

        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"invalid projection reconciliation work item not found: {clean_work_id!r}"
            )
        observed = dict(row)
        if int(observed.get("version") or 0) != expected_version:
            raise ValueError(
                f"invalid projection reconciliation version mismatch for {clean_work_id}: "
                f"expected {expected_version}, observed {observed.get('version')}"
            )
        for field, value in expected.items():
            observed_value = str(observed.get(field) or "")
            if observed_value != value:
                raise ValueError(
                    f"invalid projection reconciliation {field} mismatch for "
                    f"{clean_work_id}: expected {value!r}, observed {observed_value!r}"
                )

        quality_missing = row_quality_missing_fields(
            observed,
            require_policy_id=True,
        )
        if not quality_missing:
            raise ValueError(
                "invalid projection reconciliation refuses a well-formed work row"
            )
        claims = [
            dict(claim)
            for claim in conn.execute(
                "SELECT claim_id,status,release_reason FROM claims "
                "WHERE work_id=? ORDER BY claim_id",
                (clean_work_id,),
            ).fetchall()
        ]
        observed_claim_ids = [str(claim["claim_id"]) for claim in claims]
        if observed_claim_ids != clean_claim_ids:
            raise ValueError(
                "invalid projection reconciliation claim-set mismatch: "
                f"expected {clean_claim_ids}, observed {observed_claim_ids}"
            )
        if any(
            str(claim.get("status") or "") != "unclaimed"
            or str(claim.get("release_reason") or "") != "expired"
            for claim in claims
        ):
            raise ValueError(
                "invalid projection reconciliation requires all exact claims expired"
            )
        counts = {
            table: int(
                conn.execute(
                    f"SELECT count(*) FROM {table} WHERE work_id=?",
                    (clean_work_id,),
                ).fetchone()[0]
            )
            for table in ("runs", "events", "artifacts")
        }
        if any(counts.values()):
            raise ValueError(
                "invalid projection reconciliation requires zero runs/events/artifacts: "
                f"{counts}"
            )
        if done_signal_exists(
            observed.get("done_signal"),
            Path(proof_root or HARNESS_ROOT),
        ):
            raise ValueError(
                "invalid projection reconciliation refuses a custodied done signal"
            )

        preimage = {
            "version": expected_version,
            **expected,
            "quality_missing": quality_missing,
            "claims": claims,
            "counts": counts,
        }
        if not apply:
            return {
                "status": "dry_run_ready",
                "work_id": clean_work_id,
                "replayed": False,
                "preimage": preimage,
                "request_sha256": request_sha,
            }

        t = db_now(conn)
        cur = conn.execute(
            "UPDATE work_items SET intent_state='superseded',updated_at=?,"
            "version=version+1 WHERE work_id=? AND version=? "
            "AND COALESCE(intent_state,'')=? AND COALESCE(title,'')=? "
            "AND COALESCE(display,'')=? AND COALESCE(assignee,'')=? "
            "AND COALESCE(module,'')=? AND COALESCE(done_signal,'')=?",
            (
                t,
                clean_work_id,
                expected_version,
                expected["intent_state"],
                expected["title"],
                expected["display"],
                expected["assignee"],
                expected["module"],
                expected["done_signal"],
            ),
        )
        if cur.rowcount != 1:
            raise ValueError("invalid projection reconciliation lost its CAS fence")
        new_version = expected_version + 1
        payload = {
            **request,
            "request_sha256": request_sha,
            "preimage": preimage,
            "new_version": new_version,
            "new_intent_state": "superseded",
            "lifecycle_mutation": True,
            "history_deleted": False,
            "claim_mutation": False,
            "artifact_mutation": False,
        }
        event = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,refs_json,"
            "payload_json,idempotency_key) VALUES (?,?,?,?,?,'agent',?,?,?)",
            (
                t,
                "invalid_projection_reconciled",
                clean_actor,
                clean_session,
                clean_work_id,
                json.dumps(clean_evidence),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "status": "applied",
            "work_id": clean_work_id,
            "event_id": int(event.lastrowid),
            "replayed": False,
            "version": new_version,
            "intent_state": "superseded",
        }


def adjudicate_live_authority(
    conn,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    subject_plane: str,
    domain: str,
    program_id: str,
    workstream_id: str,
    episode_id: str,
    span_id: str,
    pinned: bool,
    expected_plane_head_sha256: str,
    operation_id: str,
    compensates_plane_head_sha256: str | None = None,
) -> dict[str, Any]:
    from .exact_authority import (
        canonical_bytes,
        live_authority_adjudication_declaration,
        sha256_bytes,
    )

    work_id = str(work_id or "").strip()
    actor = str(actor or "").strip().lower()
    session_id = str(session_id or "").strip()
    operation_id = str(operation_id or "").strip()
    expected_sha = str(expected_plane_head_sha256 or "").strip().lower()
    compensates_sha = str(compensates_plane_head_sha256 or "").strip().lower() or None
    if not work_id:
        raise ValueError("authority adjudication requires work_id")
    if actor not in _lane_set():
        raise ValueError(f"authority adjudication actor must be {_lanes_display()}")
    _validate_session_actor(session_id, actor)
    if expected_actor_for_session_id(session_id) != actor:
        raise ValueError(
            "authority adjudication requires an actor-namespaced session_id"
        )
    if isinstance(pinned, bool) is False:
        raise ValueError("authority adjudication pinned must be an explicit boolean")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise ValueError("authority adjudication requires expected plane-head SHA-256")
    if compensates_sha is not None and not re.fullmatch(
        r"[a-f0-9]{64}", compensates_sha
    ):
        raise ValueError(
            "authority compensation requires an exact prior plane-head SHA-256"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", operation_id):
        raise ValueError(
            "authority adjudication operation_id must be 8-200 safe characters"
        )

    declaration_json = live_authority_adjudication_declaration(
        work_id,
        subject_plane=str(subject_plane or "").strip(),
        domain=str(domain or "").strip(),
        program_id=str(program_id or "").strip(),
        workstream_id=str(workstream_id or "").strip(),
        episode_id=str(episode_id or "").strip(),
        span_id=str(span_id or "").strip(),
        pinned=pinned,
        writer=session_id,
    )
    declaration = json.loads(declaration_json)
    request = {
        "schema_version": 1,
        "writer_contract": "live_authority_adjudication.v1",
        "work_id": work_id,
        "actor": actor,
        "session_id": session_id,
        "operation_id": operation_id,
        "expected_plane_head_sha256": expected_sha,
        "compensates_plane_head_sha256": compensates_sha,
        "declaration": declaration,
    }
    request_sha = sha256_bytes(canonical_bytes(request))
    generation_id = f"coord-live-declarations-r1:{request_sha[:32]}"
    idempotency_key = f"authority-adjudication:{actor}:{operation_id}"

    with tx(conn):
        t = db_now(conn)
        session = conn.execute(
            "SELECT actor FROM agent_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if session is None or str(session["actor"] or "").strip().lower() != actor:
            raise ValueError(
                "authority adjudication caller session is not registered to actor"
            )
        related = set(
            _related_session_ids_unlocked(conn, session_id, actor=actor) or [session_id]
        )
        held = conn.execute(
            "SELECT claim_id,session_id,status FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked')"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY acquired_at,claim_id",
            (work_id, t),
        ).fetchall()
        own = [row for row in held if str(row["session_id"] or "") in related]
        if not own:
            raise ValueError(
                "authority adjudication requires a current held claim by caller"
            )
        foreign = [row for row in held if str(row["session_id"] or "") not in related]
        if foreign:
            raise ValueError("authority adjudication refuses a foreign held claim")

        work = conn.execute(
            "SELECT authority_declaration_json,version FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if work is None:
            raise ValueError(f"authority adjudication work_id not found: {work_id}")
        try:
            current_declaration = json.loads(
                work["authority_declaration_json"] or "null"
            )
        except json.JSONDecodeError as exc:
            raise ValueError(
                "authority adjudication found malformed work declaration"
            ) from exc
        if not isinstance(current_declaration, dict):
            raise ValueError(
                "authority adjudication requires an existing typed declaration"
            )

        plane_head = conn.execute(
            "SELECT h.authority_version_id,h.generation_id,h.content_sha256,"
            " v.head_version,v.payload_json FROM coord_authority_heads h"
            " JOIN coord_authority_versions v"
            " ON v.authority_version_id=h.authority_version_id"
            " WHERE h.authority_kind='plane' AND h.work_id=?",
            (work_id,),
        ).fetchone()
        if plane_head is None:
            raise ValueError("authority adjudication requires a current plane head")
        observed_sha = str(plane_head["content_sha256"] or "").lower()
        if observed_sha != expected_sha:
            raise ValueError(
                f"authority plane-head CAS failed for {work_id}: "
                f"expected {expected_sha}, observed {observed_sha}"
            )
        declaration_sha = sha256_bytes(canonical_bytes(current_declaration))
        if declaration_sha != observed_sha:
            raise ValueError("work declaration and current plane head are inconsistent")
        current_state = str(current_declaration.get("classification_state") or "")
        if current_state == "needs_review":
            if compensates_sha is not None:
                raise ValueError(
                    "initial needs_review adjudication cannot be a compensation"
                )
            operation_kind = "adjudication"
        elif current_state == "adjudicated":
            if compensates_sha != observed_sha:
                raise ValueError(
                    "re-adjudication requires --compensates-plane-head-sha256 "
                    "matching the current head; rollback is a successor version"
                )
            operation_kind = "compensating_adjudication"
        else:
            raise ValueError(
                f"unsupported authority classification state: {current_state!r}"
            )

        live_stream = conn.execute(
            "SELECT schema_version FROM coord_authority_generations"
            " WHERE generation_id='coord-live-declarations-r1'",
        ).fetchone()
        if (
            live_stream is None
            or str(live_stream["schema_version"] or "") != "coord-live-declarations.r1"
        ):
            raise ValueError("coord-live-declarations-r1 stream is not activated")
        conn.execute(
            "INSERT INTO coord_authority_generations("
            "generation_id,schema_version,manifest_sha256,sources_json,counts_json,"
            "published_by,published_at) VALUES (?,?,?,?,?,?,?)",
            (
                generation_id,
                "coord-live-declarations.r1",
                request_sha,
                canonical_bytes(
                    {
                        "stream": "coord-live-declarations-r1",
                        "operation_id": operation_id,
                        "request_sha256": request_sha,
                    }
                ).decode(),
                canonical_bytes({"plane": 1, "lineage": 1, "value_pin": 1}).decode(),
                session_id,
                t,
            ),
        )

        content_sha = sha256_bytes(canonical_bytes(declaration))
        evidence_ref = (
            f"controller_adjudication:{declaration['authority_source_sha256']}"
        )
        prior_heads: dict[str, str | None] = {}
        new_heads: dict[str, str] = {}
        for kind in ("plane", "lineage", "value_pin"):
            prior = conn.execute(
                "SELECT h.content_sha256,v.head_version FROM coord_authority_heads h"
                " JOIN coord_authority_versions v"
                " ON v.authority_version_id=h.authority_version_id"
                " WHERE h.authority_kind=? AND h.work_id=?",
                (kind, work_id),
            ).fetchone()
            prior_heads[kind] = str(prior["content_sha256"]) if prior else None
            next_version = int(prior["head_version"] or 0) + 1 if prior else 1
            cursor = conn.execute(
                "INSERT INTO coord_authority_versions("
                "authority_kind,work_id,head_version,generation_id,payload_json,"
                "content_sha256,evidence_ref,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    kind,
                    work_id,
                    next_version,
                    generation_id,
                    declaration_json,
                    content_sha,
                    evidence_ref,
                    t,
                ),
            )
            version_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO coord_authority_heads("
                "authority_kind,work_id,authority_version_id,generation_id,"
                "content_sha256,updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(authority_kind,work_id) DO UPDATE SET"
                " authority_version_id=excluded.authority_version_id,"
                " generation_id=excluded.generation_id,"
                " content_sha256=excluded.content_sha256,updated_at=excluded.updated_at",
                (kind, work_id, version_id, generation_id, content_sha, t),
            )
            new_heads[kind] = content_sha

        conn.execute(
            "UPDATE work_items SET authority_declaration_json=?,updated_at=?,"
            " version=version+1 WHERE work_id=?",
            (declaration_json, t, work_id),
        )
        event_payload = {
            **request,
            "operation_kind": operation_kind,
            "request_sha256": request_sha,
            "generation_id": generation_id,
            "prior_heads": prior_heads,
            "new_heads": new_heads,
            "claim_ids": [str(row["claim_id"]) for row in own],
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,payload_json,"
            "idempotency_key) VALUES (?,?,?,?,?,'agent',?,?)",
            (
                t,
                "authority_adjudication",
                actor,
                session_id,
                work_id,
                canonical_bytes(event_payload).decode(),
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)

    return {
        "status": "applied",
        "work_id": work_id,
        "operation_kind": operation_kind,
        "operation_id": operation_id,
        "request_sha256": request_sha,
        "generation_id": generation_id,
        "prior_plane_head_sha256": observed_sha,
        "new_plane_head_sha256": content_sha,
        "event_id": event_id,
    }


def upsert_projection_work(
    conn,
    work_id: str,
    *,
    seed_done_signal_artifact: bool = False,
    **fields,
) -> dict[str, Any]:
    effective, _changed = upsert_projection_work_if_changed(
        conn,
        work_id,
        seed_done_signal_artifact=seed_done_signal_artifact,
        **fields,
    )
    return effective


def upsert_projection_work_if_changed(
    conn,
    work_id: str,
    *,
    seed_done_signal_artifact: bool = False,
    **fields,
) -> tuple[dict[str, Any], bool]:
    bad = set(fields) - _WORK_COLS
    if bad:
        raise ValueError(f"unknown work_items columns: {bad}")
    with tx(conn):
        existing = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        effective = dict(fields)
        work_columns = (
            {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(work_items)").fetchall()
            }
            if existing is None
            else set()
        )
        if (
            existing is None
            and "authority_declaration_json" in work_columns
            and "authority_declaration_json" not in effective
        ):
            from .exact_authority import new_work_quarantine_declaration

            effective["authority_declaration_json"] = new_work_quarantine_declaration(
                work_id,
                writer="coord_db.upsert_projection_work",
                source_kind="legacy_projection_quarantine",
            )
        if existing is not None:
            effective = {
                key: value
                for key, value in effective.items()
                if key not in PROJECTION_PROTECTED_WORK_FIELDS
            }
        changed_fields = effective
        if existing is not None:
            changed_fields = {
                key: value for key, value in effective.items() if existing[key] != value
            }
        display = str(effective.get("display") or "").strip()
        display_row = (
            conn.execute(
                "SELECT display FROM display_titles WHERE key=?", (work_id,)
            ).fetchone()
            if display
            else None
        )
        display_needs_sync = bool(
            display
            and (
                display_row is None
                or str(display_row["display"] or "").strip() != display
            )
        )
        changed = existing is None or bool(changed_fields) or display_needs_sync
        if existing is None or changed_fields:
            _upsert_work_unlocked(conn, work_id, changed_fields)
        elif display_needs_sync:
            t = db_now(conn)
            conn.execute(
                "INSERT INTO display_titles(key, display, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " display=excluded.display, updated_at=excluded.updated_at",
                (work_id, display, t),
            )
        done_signal = str(effective.get("done_signal") or "").strip()
        if (
            seed_done_signal_artifact
            and effective.get("intent_state") == "done"
            and done_signal
        ):
            exact = conn.execute(
                "SELECT 1 FROM artifacts WHERE work_id=? AND path=? AND kind='done_signal' LIMIT 1",
                (work_id, done_signal),
            ).fetchone()
            if exact is None:
                conn.execute(
                    "INSERT INTO artifacts(artifact_id, work_id, path, kind,"
                    " validation_json, created_at) VALUES (?,?,?,?,?,?)",
                    (
                        new_id("art"),
                        work_id,
                        done_signal,
                        "done_signal",
                        "{}",
                        db_now(conn),
                    ),
                )
                changed = True
    return effective, changed


def archive_projection_work_if_unowned(
    conn,
    work_id: str,
    *,
    lifecycle_event_kinds: Iterable[str],
) -> bool:

    event_kinds = tuple(
        sorted({str(kind) for kind in lifecycle_event_kinds if str(kind)})
    )
    allowed_intents = {"planned", "queued"}
    with tx(conn):
        row = conn.execute(
            "SELECT intent_state FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        if row is None:
            return False
        intent = str(row["intent_state"] or "").strip().lower()
        if intent not in allowed_intents:
            return False
        if (
            conn.execute(
                "SELECT 1 FROM claims WHERE work_id=? LIMIT 1", (work_id,)
            ).fetchone()
            is not None
        ):
            return False
        if (
            conn.execute(
                "SELECT 1 FROM runs WHERE work_id=? AND state='live' LIMIT 1",
                (work_id,),
            ).fetchone()
            is not None
        ):
            return False
        if (
            conn.execute(
                "SELECT 1 FROM artifacts WHERE work_id=? LIMIT 1", (work_id,)
            ).fetchone()
            is not None
        ):
            return False
        if event_kinds:
            event_placeholders = ",".join("?" for _ in event_kinds)
            if (
                conn.execute(
                    "SELECT 1 FROM events WHERE work_id=?"
                    f" AND kind IN ({event_placeholders}) LIMIT 1",
                    (work_id, *event_kinds),
                ).fetchone()
                is not None
            ):
                return False
        t = db_now(conn)
        cur = conn.execute(
            "UPDATE work_items SET intent_state='archived', updated_at=?,"
            " version=version+1 WHERE work_id=? AND COALESCE(intent_state,'')!='archived'",
            (t, work_id),
        )
        return bool(cur.rowcount)


def apply_controller_work_transition(
    conn,
    work_id: str,
    *,
    intent_state: str,
    reason: str,
    **fields,
) -> list[str]:
    bad = set(fields) - _WORK_COLS
    if bad:
        raise ValueError(f"unknown work_items columns: {bad}")
    target = str(intent_state or "").strip().lower()
    if target not in {"blocked", "queued"}:
        raise ValueError(
            f"controller transition requires blocked|queued, got {target!r}"
        )
    with tx(conn):
        held = conn.execute(
            "SELECT claim_id FROM claims WHERE work_id=? "
            "AND status IN ('running','paused','blocked')",
            (work_id,),
        ).fetchall()
        held_ids = [str(row["claim_id"]) for row in held]
        if held_ids:
            placeholders = ",".join("?" for _ in held_ids)
            conn.execute(
                f"UPDATE claims SET status='released', release_reason=?, version=version+1 "
                f"WHERE claim_id IN ({placeholders})",
                (reason, *held_ids),
            )
        effective = dict(fields)
        effective["intent_state"] = target
        _upsert_work_unlocked(conn, work_id, effective)
    return held_ids


class _ContinuationCASMismatch(RuntimeError):
    pass


def _insert_continuation_ready_event(
    conn,
    *,
    now: float,
    work_id: str,
    lane: str,
    predicate: dict,
    scanned_version: int,
    requeued: bool,
    released_claim_id: str | None,
) -> int:
    payload = {
        "schema_version": 2,
        "predicate": predicate,
        "requeued": requeued,
        "released_claim_id": released_claim_id,
        "work_version": scanned_version,
    }
    if requeued and released_claim_id:
        body = (
            "The typed resume predicate evaluated true and the exact "
            "blocked work/claim pair was requeued atomically."
        )
    elif requeued:
        # The blocking claim's lease had already expired and been released,
        # leaving the work item sticky-blocked with no holder. Requeuing it on
        # its own state is what keeps auto-requeue alive past one lease.
        body = (
            "The typed resume predicate evaluated true. The blocking claim's "
            "lease had already expired and been released, so the unheld work "
            "item was requeued on its own state."
        )
    else:
        body = (
            "The typed resume predicate evaluated true. Authority-bearing "
            "or non-allowlisted blocked work remains blocked."
        )
    cur = conn.execute(
        "INSERT INTO events(ts,kind,actor,to_selector,work_id,trust,title,body,"
        "refs_json,payload_json,idempotency_key)"
        " VALUES (?,'continuation_ready','system',?,?,'system',?,?, '[]',?,?)",
        (
            now,
            f"actor:{lane}" if lane in _lane_set() else None,
            work_id,
            f"Continuation ready: {work_id}",
            body,
            json.dumps(payload, sort_keys=True),
            f"continuation-ready:{work_id}:{scanned_version}",
        ),
    )
    return int(cur.lastrowid)


def apply_continuation_ready_transition(
    conn,
    *,
    work_id: str,
    lane: str,
    predicate: dict,
    expected_work_version: int,
    expected_intent: str,
    expected_claim_id: str | None,
    expected_claim_version: int | None,
    requeue: bool,
) -> dict[str, object] | None:
    # Either one exact blocked claim to release, or none at all -- never half a
    # claim. "None at all" is the row whose block outlived its lease: the expiry
    # sweep released the claim and left the work item sticky-blocked, so there
    # is no claim to compare against and no holder to disturb. The transaction
    # below re-checks that emptiness rather than trusting this argument.
    if requeue and bool(expected_claim_id) != (expected_claim_version is not None):
        raise ValueError(
            "continuation requeue takes one exact blocked claim (id and version) "
            "or neither"
        )
    try:
        with tx(conn):
            current = conn.execute(
                "SELECT version,intent_state,continuation_ready_at FROM work_items"
                " WHERE work_id=?",
                (work_id,),
            ).fetchone()
            current_claims = conn.execute(
                "SELECT claim_id,version FROM claims WHERE work_id=?"
                " AND status='blocked'",
                (work_id,),
            ).fetchall()
            current_claim = current_claims[0] if len(current_claims) == 1 else None
            current_claim_id = (
                str(current_claim["claim_id"] or "").strip()
                if current_claim is not None
                else None
            )
            current_claim_version = (
                current_claim["version"] if current_claim is not None else None
            )
            if (
                current is None
                or int(current["version"]) != int(expected_work_version)
                or str(current["intent_state"] or "").strip().lower()
                != str(expected_intent or "").strip().lower()
                or current["continuation_ready_at"] is not None
                or current_claim_id != expected_claim_id
                or current_claim_version != expected_claim_version
            ):
                raise _ContinuationCASMismatch
            if requeue and expected_claim_id is None:
                # Requeuing an unheld row is only safe while it stays unheld.
                # A claim acquired between the scan and this transaction makes
                # someone the owner, and requeuing would take the row out from
                # under them.
                if conn.execute(
                    "SELECT 1 FROM claims WHERE work_id=?"
                    " AND status IN ('running','paused','blocked') LIMIT 1",
                    (work_id,),
                ).fetchone() is not None:
                    raise _ContinuationCASMismatch
            now = db_now(conn)
            changed = conn.execute(
                "UPDATE work_items SET continuation_ready_at=?,updated_at=?,"
                " intent_state=CASE WHEN ? THEN 'queued' ELSE intent_state END,"
                " blocked_reason_class=CASE WHEN ? THEN NULL ELSE blocked_reason_class END,"
                " version=version+1 WHERE work_id=? AND version=?"
                " AND continuation_ready_at IS NULL",
                (
                    now,
                    now,
                    int(requeue),
                    int(requeue),
                    work_id,
                    expected_work_version,
                ),
            )
            if changed.rowcount != 1:
                raise _ContinuationCASMismatch
            if requeue and expected_claim_id is not None:
                released = conn.execute(
                    "UPDATE claims SET status='released',"
                    " release_reason='resume_predicate_satisfied',version=version+1"
                    " WHERE claim_id=? AND version=? AND status='blocked'",
                    (expected_claim_id, expected_claim_version),
                )
                if released.rowcount != 1:
                    raise _ContinuationCASMismatch
            event_id = _insert_continuation_ready_event(
                conn,
                now=now,
                work_id=work_id,
                lane=lane,
                predicate=predicate,
                scanned_version=expected_work_version,
                requeued=requeue,
                released_claim_id=expected_claim_id if requeue else None,
            )
    except _ContinuationCASMismatch:
        return None
    return {
        "work_id": work_id,
        "lane": lane,
        "event_id": event_id,
        "requeued": requeue,
    }


def close_review_as_policy_moot(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_intent_state: str,
    expected_last_event_id: int | None,
    expected_last_event_at: float | None,
    skip_if_events_after: float,
    packet_sha256: str,
    actor: str,
) -> dict[str, Any]:
    from .review_tier import is_review_row, t0_predicate_reasons

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_packet_sha256 = str(packet_sha256 or "").strip().lower()
    if not clean_work_id:
        raise ValueError("policy-moot close requires work_id")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("policy-moot close requires a non-negative expected_version")
    if not re.fullmatch(r"[a-f0-9]{64}", clean_packet_sha256):
        raise ValueError("policy-moot close requires a packet sha256")
    if clean_actor not in _lane_set():
        raise ValueError(f"policy-moot close actor must be {_lanes_display()}")
    try:
        watermark = float(skip_if_events_after)
    except (TypeError, ValueError) as exc:
        raise ValueError("policy-moot close requires skip_if_events_after") from exc
    if watermark != watermark or watermark in {float("inf"), float("-inf")}:
        raise ValueError("policy-moot close requires a finite event watermark")
    if expected_last_event_at is None:
        expected_latest = None
    else:
        try:
            expected_latest = float(expected_last_event_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("policy-moot close expected_last_event_at is invalid") from exc
        if not math.isfinite(expected_latest):
            raise ValueError("policy-moot close expected_last_event_at must be finite")
    if expected_last_event_id is None:
        expected_event_id = None
    elif (
        isinstance(expected_last_event_id, bool)
        or not isinstance(expected_last_event_id, int)
        or expected_last_event_id <= 0
    ):
        raise ValueError("policy-moot close expected_last_event_id is invalid")
    else:
        expected_event_id = expected_last_event_id
    if (expected_event_id is None) != (expected_latest is None):
        raise ValueError("policy-moot close expected event head is incomplete")

    request = {
        "schema_version": 1,
        "writer_contract": "board_hygiene_policy_moot_close.v1",
        "work_id": clean_work_id,
        "expected_version": expected_version,
        "expected_assignee": str(expected_assignee or ""),
        "expected_intent_state": str(expected_intent_state or ""),
        "expected_last_event_id": expected_event_id,
        "expected_last_event_at": expected_latest,
        "skip_if_events_after": watermark,
        "packet_sha256": clean_packet_sha256,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    idempotency_key = f"board-hygiene-policy-moot:{clean_packet_sha256}:{clean_work_id}"

    with tx(conn):
        prior = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            try:
                prior_payload = json.loads(str(prior["payload_json"] or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("policy-moot close replay receipt is malformed") from exc
            if prior_payload.get("operation_request_sha256") != request_sha256:
                raise ValueError("policy-moot close idempotency key was reused for a different request")
            retired = prior_payload.get("retired_audit_request_event_ids", [])
            if not isinstance(retired, list) or any(
                _strict_positive_event_id(event_id) is None for event_id in retired
            ):
                raise ValueError("policy-moot close replay receipt has malformed request retirement")
            return {
                "applied": True,
                "event_id": int(prior["event_id"]),
                "work_id": clean_work_id,
                "retired_audit_request_event_ids": sorted(
                    {int(event_id) for event_id in retired}
                ),
                "replayed": True,
            }

        work = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (clean_work_id,)
        ).fetchone()
        if work is None:
            return {"applied": False, "reason": "work_item_missing", "work_id": clean_work_id}
        row = dict(work)
        if int(row.get("version") or 0) != expected_version:
            return {"applied": False, "reason": "version_drift", "work_id": clean_work_id}
        if str(row.get("assignee") or "") != request["expected_assignee"]:
            return {"applied": False, "reason": "assignee_drift", "work_id": clean_work_id}
        if str(row.get("intent_state") or "") != request["expected_intent_state"]:
            return {"applied": False, "reason": "intent_state_drift", "work_id": clean_work_id}
        if str(row.get("intent_state") or "").strip().lower() in TERMINAL_WORK_STATES:
            return {"applied": False, "reason": "terminal_state", "work_id": clean_work_id}
        if str(row.get("assignee") or "").strip().lower() == "operator":
            return {"applied": False, "reason": "operator_owned", "work_id": clean_work_id}
        if not is_review_row(clean_work_id, row):
            return {"applied": False, "reason": "not_review_row", "work_id": clean_work_id}
        max_ts_row = conn.execute(
            "SELECT MAX(ts) AS max_event_at FROM events WHERE work_id=?", (clean_work_id,)
        ).fetchone()
        max_event_at = max_ts_row["max_event_at"] if max_ts_row else None
        if max_event_at is not None and float(max_event_at) > watermark:
            return {"applied": False, "reason": "newer_event_after_packet", "work_id": clean_work_id}
        head = conn.execute(
            "SELECT event_id,ts FROM events WHERE work_id=? ORDER BY event_id DESC LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        current_event_id = int(head["event_id"]) if head else None
        current_event_at = float(head["ts"]) if head else None
        if current_event_id != expected_event_id:
            return {"applied": False, "reason": "event_head_drift", "work_id": clean_work_id}
        if (current_event_at is None) != (expected_latest is None) or (
            current_event_at is not None
            and abs(float(current_event_at) - float(expected_latest)) > 1e-6
        ):
            return {"applied": False, "reason": "event_watermark_drift", "work_id": clean_work_id}
        live_claim = conn.execute(
            "SELECT 1 FROM claims WHERE work_id=? AND status IN ('running','paused','blocked')"
            " AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
            (clean_work_id, db_now(conn)),
        ).fetchone()
        if live_claim is not None:
            return {"applied": False, "reason": "live_lease", "work_id": clean_work_id}
        tier = effective_review_tier_for_work(conn, clean_work_id, row=row)
        if tier == "T0":
            return {
                "applied": False,
                "reason": "effective_t0",
                "tier_reasons": t0_predicate_reasons(row),
                "work_id": clean_work_id,
            }

        head_state = _typed_handoff_head_state_unlocked(conn, clean_work_id)
        active_ids = [int(event_id) for event_id in head_state["active_event_ids"]]
        if active_ids:
            placeholders = ",".join("?" for _ in active_ids)
            active_audit_request_ids = sorted(
                int(event["event_id"])
                for event in conn.execute(
                    f"SELECT event_id FROM events WHERE event_id IN ({placeholders})"
                    " AND kind='audit_request'",
                    active_ids,
                )
            )
        else:
            active_audit_request_ids = []

        t = db_now(conn)
        updated = conn.execute(
            "UPDATE work_items SET intent_state='closed',updated_at=?,version=version+1"
            " WHERE work_id=? AND version=?",
            (t, clean_work_id, expected_version),
        )
        if updated.rowcount != 1:
            return {"applied": False, "reason": "version_drift", "work_id": clean_work_id}
        payload = {
            **request,
            "operation_request_sha256": request_sha256,
            "closed_from_intent_state": request["expected_intent_state"],
            "closed_to_intent_state": "closed",
            "no_claim_mutation": True,
            "no_rubric_mutation": True,
            "no_artifact_mutation": True,
            "retired_audit_request_event_ids": active_audit_request_ids,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,work_id,trust,title,body,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                t,
                "board_hygiene_policy_moot_closed",
                clean_actor,
                clean_work_id,
                "system",
                f"Policy-moot closure for {clean_work_id}",
                "Dated reversible board-hygiene closure; no deletion or review verdict.",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)
        retired = _supersede_active_handoff_heads_unlocked(
            conn,
            work_id=clean_work_id,
            by_event_id=event_id,
            prior_kinds={"audit_request"},
        )
        if retired:
            placeholders = ",".join("?" for _ in retired)
            conn.execute(
                "UPDATE request_consumption SET consumed_event_id=?,consumed_at=?"
                " WHERE work_id=? AND consumed_at IS NULL"
                f" AND request_event_id IN ({placeholders})",
                (event_id, t, clean_work_id, *retired),
            )
        return {
            "applied": True,
            "event_id": event_id,
            "work_id": clean_work_id,
            "retired_audit_request_event_ids": retired,
            "replayed": False,
        }


#: The one env knob that decides what a *not-ready* row does at claim time.
#: Unset keeps each surface's historical default (MCP refuses, CLI warns);
#: "1" makes both refuse, "0" makes both warn. Read only by
#: :func:`claim_readiness_enforcement` so the two surfaces cannot drift.
CLAIM_STRICT_ENV = "COORD_CLAIM_STRICT"

CLAIM_READINESS_REFUSE = "refuse"
CLAIM_READINESS_WARN = "warn"

_CLAIM_STRICT_TRUE = frozenset({"1", "true", "yes", "on"})
_CLAIM_STRICT_FALSE = frozenset({"0", "false", "no", "off"})


class ClaimReadinessError(ValueError):
    """A claim refused because its work row is not fit to claim.

    Carries the field list so a surface can answer structurally instead of
    re-parsing its own message.
    """

    def __init__(self, work_id: str, missing: Iterable[str], message: str) -> None:
        super().__init__(message)
        self.work_id = work_id
        self.missing = [str(field) for field in missing]


def claim_readiness(
    work_id: str,
    row: dict[str, Any] | None,
    *,
    actor: str | None = None,
) -> list[str]:
    """Fields a work row is missing before it is fit to claim.

    Empty list means ready. This is the single definition both the MCP surface
    and the CLI consult; they differ only in what they *do* with the answer
    (see :func:`claim_readiness_enforcement`).
    """

    try:
        from coordharness.coord.creation_lint import claim_rubric_missing_fields
    except Exception:  # pragma: no cover - lint module optional by construction
        return []
    fields = {key: value for key, value in (row or {}).items() if value not in (None, "")}
    fields.setdefault("id", work_id)
    fields.setdefault("work_id", work_id)
    fields["status"] = "running"
    fields["intent_state"] = "running"
    if actor:
        fields.setdefault("assignee", actor)
    return claim_rubric_missing_fields(fields)


def claim_readiness_enforcement(*, default: str) -> str:
    """``"refuse"`` or ``"warn"`` for this process, from ``COORD_CLAIM_STRICT``."""

    if default not in (CLAIM_READINESS_REFUSE, CLAIM_READINESS_WARN):
        raise ValueError(f"unknown claim readiness default {default!r}")
    raw = str(os.environ.get(CLAIM_STRICT_ENV) or "").strip().lower()
    if raw in _CLAIM_STRICT_TRUE:
        return CLAIM_READINESS_REFUSE
    if raw in _CLAIM_STRICT_FALSE:
        return CLAIM_READINESS_WARN
    return default


def claim_readiness_message(work_id: str, missing: Iterable[str]) -> str:
    """The one sentence both surfaces print, refusing or warning."""

    names = ", ".join(str(field) for field in missing)
    return (
        f"incomplete claim {work_id}: missing {names}; "
        "create/repair the work item with a descriptive title, valid done_signal, "
        "and T0/T1 acceptance before claiming; see docs/review-tiers.md"
    )


def claim_work(
    conn,
    session_id: str,
    work_id: str,
    step: str | None = None,
    lease_s: float = LEASE_DEFAULT_S,
    work_fields: dict[str, Any] | None = None,
) -> str:
    if not session_id:
        raise ValueError(
            "claim_work requires a non-empty session_id (the N=2 clobber guard)"
        )
    claim_id = new_id("clm")
    with tx(conn):
        if work_fields:
            bad = set(work_fields) - _WORK_COLS
            if bad:
                raise ValueError(f"unknown work_items columns: {bad}")
            existing = conn.execute(
                "SELECT * FROM work_items WHERE work_id=?", (work_id,)
            ).fetchone()
            _validate_work_policy_unlocked(conn, work_id, existing, work_fields)
            _upsert_work_unlocked(conn, work_id, dict(work_fields))
        t = db_now(conn)
        _release_expired_claims_unlocked(conn, at=t, work_id=work_id)
        wrow = conn.execute(
            "SELECT intent_state, archived_at, assignee, blocked_reason_class,"
            " resume_predicate_json, continuation_ready_at"
            " FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if wrow is None:
            # Refuse the id here, while its name is still in hand. Every path
            # below tolerates a missing row, so a typo would fall through to the
            # INSERT and come back as "FOREIGN KEY constraint failed" -- true,
            # and useless: it names neither the id that was wrong nor anywhere
            # to find the right one. A caller that means to create the row
            # passes work_fields, which was upserted above, so this cannot fire
            # on a create-and-claim.
            raise ValueError(
                f"unknown work id {work_id!r}; run 'coord board' to see the "
                "work ids on this board"
            )
        session_row = conn.execute(
            "SELECT actor FROM agent_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        session_actor = str(session_row["actor"] if session_row else "").strip().lower()
        assigned_actor = str(wrow["assignee"] if wrow else "").strip().lower()
        actor_assignment_is_private = bool(
            assigned_actor and assigned_actor not in {"shared", "operator", "any", "unassigned"}
        )
        if session_actor and actor_assignment_is_private and session_actor != assigned_actor:
            raise ValueError(
                f"cannot claim work assigned to {assigned_actor!r} from {session_actor!r} "
                f"session {session_id!r}; use a typed handoff/controller transition"
            )
        running_claim = conn.execute(
            "SELECT claim_id,session_id FROM claims WHERE work_id=? AND status='running'"
            " AND (expires_at IS NULL OR expires_at>?) ORDER BY acquired_at DESC LIMIT 1",
            (work_id, t),
        ).fetchone()
        if running_claim is not None:
            holder = str(running_claim["session_id"] or "").strip()
            family = set(
                _related_session_ids_unlocked(conn, session_id, actor=session_actor) or []
            )
            family.add(session_id)
            if holder in family:
                conn.execute(
                    "UPDATE claims SET step=COALESCE(?,step),heartbeat_at=?,expires_at=?,"
                    " version=version+1 WHERE claim_id=? AND status='running'",
                    (step, t, t + lease_s, running_claim["claim_id"]),
                )
                conn.execute(
                    "UPDATE work_items SET intent_state='running',updated_at=?,version=version+1"
                    " WHERE work_id=?",
                    (t, work_id),
                )
                return str(running_claim["claim_id"])
        if wrow and (
            wrow["archived_at"] is not None
            or str(wrow["intent_state"] or "").strip().lower() in TERMINAL_WORK_STATES
        ):
            raise ValueError(
                f"cannot claim terminal/archived work {work_id!r} "
                f"(state={wrow['intent_state']}, archived={wrow['archived_at'] is not None})"
            )
        intent_lc = str(wrow["intent_state"] or "").strip().lower() if wrow else ""
        if (
            wrow
            and intent_lc in {"queued", "planned"}
            and str(wrow["resume_predicate_json"] or "").strip()
            and wrow["continuation_ready_at"] is None
        ):
            raise ValueError(
                f"cannot claim conditionally parked work {work_id!r}; "
                "its resume predicate has not emitted continuation_ready"
            )
        if wrow and intent_lc in {"blocked", "paused", "held", "hold"}:
            resumed_claim_id: str | None = None
            if intent_lc in {"blocked", "paused"}:
                held_claim = conn.execute(
                    "SELECT claim_id, session_id FROM claims WHERE work_id=?"
                    " AND status IN ('paused','blocked')"
                    " AND (expires_at IS NULL OR expires_at > ?)"
                    " ORDER BY heartbeat_at DESC, acquired_at DESC, claim_id DESC LIMIT 1",
                    (work_id, t),
                ).fetchone()
                if held_claim is not None:
                    holder_sid = str(held_claim["session_id"] or "").strip()
                    same_owner = holder_sid == session_id
                    if not same_owner and holder_sid:
                        family = set(
                            _related_session_ids_unlocked(
                                conn, session_id, actor=session_actor
                            )
                            or []
                        )
                        family.add(session_id)
                        same_owner = holder_sid in family
                    if same_owner:
                        resumed_claim_id = str(held_claim["claim_id"])
            if resumed_claim_id is None:
                has_live_claim = conn.execute(
                    "SELECT 1 FROM claims WHERE work_id=?"
                    " AND status IN ('running','paused','blocked')"
                    " AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
                    (work_id, t),
                ).fetchone()
                if (
                    intent_lc == "blocked"
                    and not str(wrow["blocked_reason_class"] or "").strip()
                    and has_live_claim is None
                ):
                    raise ValueError(
                        f"cannot claim orphaned blocked work {work_id!r}; "
                        "call recover_blocked to explicitly requeue the exact "
                        "blocked/no-live-claim/null-reason state with a receipt"
                    )
                raise ValueError(
                    f"cannot claim held work {work_id!r} (state={wrow['intent_state']}); "
                    "a controller must release it to queued/planned first"
                )
            conn.execute(
                "UPDATE claims SET status='running', step=COALESCE(?,step),"
                " heartbeat_at=?, expires_at=?, release_reason=NULL, version=version+1"
                " WHERE claim_id=?",
                (step, t, t + lease_s, resumed_claim_id),
            )
            conn.execute(
                "UPDATE work_items SET intent_state='running', updated_at=?,"
                " version=version+1 WHERE work_id=?",
                (t, work_id),
            )
            return resumed_claim_id
        conn.execute(
            "INSERT INTO claims(claim_id, work_id, session_id, lease_token, status, step,"
            " acquired_at, heartbeat_at, expires_at, version)"
            " VALUES (?,?,?,?, 'running', ?, ?, ?, ?, 0)",
            (claim_id, work_id, session_id, new_id("ls"), step, t, t, t + lease_s),
        )
        conn.execute(
            "UPDATE work_items SET intent_state='running', updated_at=?, version=version+1"
            " WHERE work_id=?",
            (t, work_id),
        )
    return claim_id


def correct_work_tier(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    expected_tier: str,
    new_tier: str,
    reason: str,
    refs: list[str],
    operation_id: str,
) -> dict[str, Any]:

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_expected = str(expected_tier or "").strip().upper()
    clean_new = str(new_tier or "").strip().upper()
    clean_reason = str(reason or "").strip()
    clean_operation = str(operation_id or "").strip()
    clean_refs = [str(ref).strip() for ref in refs if str(ref).strip()]
    if not clean_work_id:
        raise ValueError("tier correction requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"tier correction actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if expected_actor_for_session_id(clean_session) != clean_actor:
        raise ValueError("tier correction requires an actor-namespaced session_id")
    if clean_expected not in {"T0", "T1", "T2"} or clean_new not in {"T0", "T1", "T2"}:
        raise ValueError("tier correction tiers must be T0|T1|T2")
    if clean_expected == clean_new:
        raise ValueError("tier correction requires a changed tier")
    if not clean_reason:
        raise ValueError("tier correction requires a reason")
    if not clean_refs:
        raise ValueError("tier correction requires evidence refs")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", clean_operation):
        raise ValueError("tier correction operation_id must be 8-200 safe characters")
    idempotency_key = f"tier-correction:{clean_actor}:{clean_operation}"
    rank = {"T0": 0, "T1": 1, "T2": 2}

    with tx(conn):
        prior = conn.execute(
            "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key"
            " FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            try:
                payload = json.loads(prior["payload_json"] or "{}")
                prior_refs = json.loads(prior["refs_json"] or "[]")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("tier correction replay receipt is malformed") from exc
            if (
                str(prior["work_id"] or "") != clean_work_id
                or payload.get("from_tier") != clean_expected
                or payload.get("to_tier") != clean_new
                or payload.get("reason") != clean_reason
                or payload.get("expected_version") != int(expected_version)
                or prior_refs != clean_refs
            ):
                raise ValueError("tier correction operation_id reused for different request")
            current = conn.execute(
                "SELECT * FROM work_items WHERE work_id=?",
                (clean_work_id,),
            ).fetchone()
            if current is None:
                raise ValueError(
                    f"tier correction work item not found: {clean_work_id}"
                )
            current_row = dict(current)
            if current_row.get("archived_at") is not None or str(
                current_row.get("intent_state") or ""
            ).strip().lower() in TERMINAL_WORK_STATES:
                raise ValueError("tier correction replay refuses terminal or archived work")
            if str(current_row.get("tier") or "T1").strip().upper() != clean_new:
                raise ValueError("tier correction replay current tier drift")
            if not _tier_correction_event_authorizes_unlocked(
                prior, clean_work_id, current_row
            ):
                raise ValueError(
                    "tier correction replay receipt does not authorize the current row"
                )
            event_id = int(prior["event_id"])
            current_binding = int(
                current_row.get("tier_correction_event_id") or 0
            )
            binding_backfilled = False
            current_version = int(current_row.get("version") or 0)
            if current_binding not in {0, event_id}:
                raise ValueError("tier correction replay conflicts with existing binding")
            if current_binding == 0:
                t = db_now(conn)
                updated = conn.execute(
                    "UPDATE work_items SET tier_correction_event_id=?,updated_at=?,"
                    " version=version+1 WHERE work_id=? AND version=?"
                    " AND tier_correction_event_id IS NULL"
                    " AND upper(COALESCE(tier,'T1'))=?",
                    (
                        event_id,
                        t,
                        clean_work_id,
                        current_version,
                        clean_new,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("tier correction replay binding CAS drift")
                current_version += 1
                binding_backfilled = True
            return {
                "work_id": clean_work_id,
                "event_id": event_id,
                "from_tier": clean_expected,
                "to_tier": clean_new,
                "version": current_version,
                "binding_backfilled": binding_backfilled,
                "replayed": True,
            }
        row = conn.execute(
            "SELECT version,tier,assignee,intent_state,archived_at"
            " FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"tier correction work item not found: {clean_work_id}")
        if int(row["version"] or 0) != int(expected_version):
            raise ValueError("tier correction version drift")
        observed = str(row["tier"] or "T1").strip().upper()
        if observed != clean_expected:
            raise ValueError(
                f"tier correction expected {clean_expected}, observed {observed}"
            )
        if row["archived_at"] is not None or str(
            row["intent_state"] or ""
        ).strip().lower() in TERMINAL_WORK_STATES:
            raise ValueError("tier correction refuses terminal or archived work")
        assignee = str(row["assignee"] or "").strip().lower()
        if rank[clean_new] > rank[clean_expected] and assignee == clean_actor:
            raise ValueError(
                "lowering review intensity requires the opposite lane"
            )
        t = db_now(conn)
        updated = conn.execute(
            "UPDATE work_items SET tier=?,updated_at=?,version=version+1"
            " WHERE work_id=? AND version=? AND upper(COALESCE(tier,'T1'))=?",
            (clean_new, t, clean_work_id, int(expected_version), clean_expected),
        )
        if updated.rowcount != 1:
            raise ValueError("tier correction CAS drift")
        payload = {
            "schema_version": 1,
            "from_tier": clean_expected,
            "to_tier": clean_new,
            "reason": clean_reason,
            "expected_version": int(expected_version),
            "review_intensity_lowered": rank[clean_new] > rank[clean_expected],
            "assignee_at_correction": assignee,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "tier_corrected",
                clean_actor,
                clean_session,
                f"actor:{assignee}" if assignee in _lane_set() else None,
                clean_work_id,
                "agent",
                f"Review tier corrected {clean_expected} -> {clean_new}",
                clean_reason,
                json.dumps(clean_refs, sort_keys=True, separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)
        bound = conn.execute(
            "UPDATE work_items SET tier_correction_event_id=?"
            " WHERE work_id=? AND version=? AND upper(COALESCE(tier,'T1'))=?",
            (
                event_id,
                clean_work_id,
                int(expected_version) + 1,
                clean_new,
            ),
        )
        if bound.rowcount != 1:
            raise ValueError("tier correction receipt binding drift")
        return {
            "work_id": clean_work_id,
            "event_id": event_id,
            "from_tier": clean_expected,
            "to_tier": clean_new,
            "version": int(expected_version) + 1,
            "replayed": False,
        }


def resume_parked_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    expected_version: int,
    reason: str,
    refs: list[str],
    operation_id: str,
) -> dict[str, Any]:

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_reason = str(reason or "").strip()
    clean_operation = str(operation_id or "").strip()
    clean_refs = [str(ref).strip() for ref in refs if str(ref).strip()]
    if not clean_work_id:
        raise ValueError("resume parked work requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"resume parked work actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if expected_actor_for_session_id(clean_session) != clean_actor:
        raise ValueError("resume parked work requires an actor-namespaced session_id")
    if not clean_reason or not clean_refs:
        raise ValueError("resume parked work requires reason and evidence refs")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", clean_operation):
        raise ValueError("resume parked work operation_id must be 8-200 safe characters")
    idempotency_key = f"resume-parked:{clean_actor}:{clean_operation}"

    with tx(conn):
        prior = conn.execute(
            "SELECT event_id,work_id FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            if str(prior["work_id"] or "") != clean_work_id:
                raise ValueError("resume parked operation_id reused for different work")
            return {
                "work_id": clean_work_id,
                "event_id": int(prior["event_id"]),
                "intent_state": "queued",
                "replayed": True,
            }
        row = conn.execute(
            "SELECT version,assignee,intent_state,archived_at,next_step,resume_when,"
            " resume_predicate_json,continuation_ready_at"
            " FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"resume parked work item not found: {clean_work_id}")
        if int(row["version"] or 0) != int(expected_version):
            raise ValueError("resume parked work version drift")
        if str(row["assignee"] or "").strip().lower() != clean_actor:
            raise ValueError("only the assigned lane may resume parked work")
        if row["archived_at"] is not None or str(
            row["intent_state"] or ""
        ).strip().lower() != "paused":
            raise ValueError("resume parked work requires a non-archived paused row")
        require_park_resume_contract(
            next_step=row["next_step"], resume_when=row["resume_when"]
        )
        held = conn.execute(
            "SELECT 1 FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked') LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        if held is not None:
            raise ValueError(
                "resume parked work refuses a held claim; same-owner claim_work handles it"
            )
        predicate: dict[str, Any] | None = None
        raw_predicate = str(row["resume_predicate_json"] or "").strip()
        if raw_predicate:
            try:
                decoded_predicate = json.loads(raw_predicate)
            except json.JSONDecodeError as exc:
                raise ValueError("resume parked work found invalid resume predicate") from exc
            if isinstance(decoded_predicate, dict):
                predicate = decoded_predicate
        manual_authorized = (
            predicate == {"type": "manual"}
            and row["continuation_ready_at"] is None
        )
        t = db_now(conn)
        updated = conn.execute(
            "UPDATE work_items SET intent_state='queued',updated_at=?,"
            " continuation_ready_at=CASE WHEN ? THEN ? ELSE continuation_ready_at END,"
            " version=version+1"
            " WHERE work_id=? AND version=? AND lower(intent_state)='paused'",
            (
                t,
                int(manual_authorized),
                t,
                clean_work_id,
                int(expected_version),
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("resume parked work CAS drift")
        payload = {
            "schema_version": 1,
            "from_intent_state": "paused",
            "to_intent_state": "queued",
            "reason": clean_reason,
            "expected_version": int(expected_version),
            "claim_mutation": False,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "work_resumed",
                clean_actor,
                clean_session,
                f"actor:{clean_actor}",
                clean_work_id,
                "agent",
                f"Paused work requeued: {clean_work_id}",
                clean_reason,
                json.dumps(clean_refs, sort_keys=True, separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        continuation_event_id = None
        if manual_authorized:
            continuation_payload = {
                "schema_version": 2,
                "predicate": predicate,
                "manual_authorization": True,
                "authorized_actor": clean_actor,
                "authorized_session_id": clean_session,
                "requeued": True,
                "released_claim_id": None,
                "work_version": int(expected_version),
            }
            continuation = conn.execute(
                "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
                " title,body,refs_json,payload_json,idempotency_key)"
                " VALUES (?,'continuation_ready',?,?,?,?,?,?,?,?,?,?)",
                (
                    t,
                    clean_actor,
                    clean_session,
                    f"actor:{clean_actor}",
                    clean_work_id,
                    "agent",
                    f"Manual continuation authorized: {clean_work_id}",
                    "The assigned lane explicitly resumed its manual-predicate row "
                    "from an actor-matching session.",
                    json.dumps(clean_refs, sort_keys=True, separators=(",", ":")),
                    json.dumps(
                        continuation_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    f"manual-continuation-ready:{clean_actor}:{clean_operation}",
                ),
            )
            continuation_event_id = int(continuation.lastrowid)
        return {
            "work_id": clean_work_id,
            "event_id": int(cur.lastrowid),
            "continuation_event_id": continuation_event_id,
            "intent_state": "queued",
            "version": int(expected_version) + 1,
            "replayed": False,
        }


#: The classes of caller sanctioned to mutate a claim they do not hold.
#:
#: Every one of these is a real behaviour of this module, but each is currently
#: implemented as its own typed operation with its own SQL and its own guard --
#: ``_release_expired_claims_unlocked``, ``reap_zombie_sessions`` and
#: ``post_existing_work_handoff``. None of them routes through
#: ``heartbeat_claim`` / ``release_claim`` / ``complete_claim``, so nothing in
#: this tree passes ``system_caller`` today. The enum exists so that if one of
#: them is ever refactored onto these functions it arrives through a named,
#: enumerated door rather than by omitting an argument -- which is exactly how
#: the holder check went missing in the first place.
CLAIM_MUTATION_SYSTEM_CALLERS = frozenset(
    {
        "reaper:expired_lease",
        "reaper:zombie_session",
        "handoff:ownership_transfer",
    }
)


def _assert_claim_holder_unlocked(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    action: str,
    session_id: str | None,
    actor: str | None = None,
    system_caller: str | None = None,
) -> sqlite3.Row | None:
    """Refuse to mutate a claim on behalf of a session that does not hold it.

    A ``claim_id`` is not a secret and was never a capability: ``claim`` returns
    it, the board prints it, and handoff payloads carry it. Any surface that
    accepted one as sufficient authority let a peer block, park or *complete*
    another session's work -- and because the claim row keeps the holder's
    ``session_id``, the resulting row still read as the holder's own action.
    There was no trace at all distinguishing "the owner did this" from "somebody
    else did it for them".

    The check lives here rather than only at the MCP surface because the CLI
    reaches the same three mutators, and so would any future face. This is the
    one place every path goes through.

    The caller's identity is asserted, not proven -- that is the trust model the
    rest of this module already uses. What this closes is the far larger hole of
    needing to assert nothing whatsoever.
    """

    declared_system_caller = str(system_caller or "").strip()
    if declared_system_caller and declared_system_caller not in CLAIM_MUTATION_SYSTEM_CALLERS:
        raise ValueError(
            f"{action} refuses an undeclared system_caller {declared_system_caller!r}; "
            f"acting on a claim you do not hold requires one of "
            f"{sorted(CLAIM_MUTATION_SYSTEM_CALLERS)}"
        )

    row = conn.execute(
        "SELECT c.claim_id, c.work_id, c.session_id, s.actor FROM claims c"
        " LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
        " WHERE c.claim_id=?",
        (claim_id,),
    ).fetchone()
    if row is None:
        # Nothing is held, so nothing can be taken. Leave the missing-claim
        # message to the caller, which already has one.
        return None
    if declared_system_caller:
        return row

    holder_sid = str(row["session_id"] or "").strip()
    holder_actor = (
        str(row["actor"] or "").strip().lower()
        or expected_actor_for_session_id(holder_sid)
        or ""
    )
    caller_sid = str(session_id or "").strip()
    caller_actor = (
        str(actor or "").strip().lower()
        or expected_actor_for_session_id(caller_sid)
        or ""
    )

    if not caller_sid:
        raise ValueError(
            f"{action} requires the calling session's identity: claim {claim_id!r} "
            f"on {str(row['work_id'] or '')!r} is held by "
            f"{holder_actor or '?'}/{holder_sid or '?'}, and this call named no "
            "session at all. Pass session_id so the board can tell the holder's "
            "own action apart from a peer acting on its row"
        )

    if caller_actor:
        _validate_session_actor(caller_sid, caller_actor)

    if caller_sid == holder_sid:
        return row
    # Same orchestrator, re-registered session row: the work_id resolution path
    # already treats these as one owner, and so must this.
    if holder_sid and holder_sid in _related_session_ids_unlocked(
        conn, caller_sid, actor=caller_actor or None
    ):
        return row

    raise ValueError(
        f"{action} cannot touch a claim this session does not hold: claim "
        f"{claim_id!r} on {str(row['work_id'] or '')!r} is held by "
        f"{holder_actor or '?'}/{holder_sid or '?'}, and the call came from "
        f"{caller_actor or '?'}/{caller_sid}. A claim id is printed on the board "
        "and carried in handoff payloads, so holding one proves nothing -- ask "
        "the holder to act, take the row over with a typed handoff, or wait for "
        "the lease to expire and be reaped"
    )


def assert_claim_holder(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    action: str,
    session_id: str | None,
    actor: str | None = None,
    system_caller: str | None = None,
) -> sqlite3.Row | None:
    """Public, read-only form of the claim-ownership guard.

    The mutators call the ``_unlocked`` variant from inside their own
    transaction so the check and the write cannot be separated. This wrapper is
    for surfaces that want to refuse before they get that far.
    """

    return _assert_claim_holder_unlocked(
        conn,
        claim_id,
        action=action,
        session_id=session_id,
        actor=actor,
        system_caller=system_caller,
    )


def heartbeat_claim(
    conn,
    claim_id: str,
    lease_s: float = LEASE_DEFAULT_S,
    step: str | None = None,
    *,
    session_id: str | None,
    actor: str | None = None,
    system_caller: str | None = None,
) -> None:
    with tx(conn):
        t = db_now(conn)
        _assert_claim_holder_unlocked(
            conn,
            claim_id,
            action="heartbeat",
            session_id=session_id,
            actor=actor,
            system_caller=system_caller,
        )
        row = conn.execute(
            "SELECT c.session_id, c.status, c.expires_at, s.actor FROM claims c"
            " LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
            " WHERE c.claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"cannot heartbeat missing claim {claim_id!r}")
        if str(row["status"] or "").strip().lower() != "running":
            raise ValueError(
                f"cannot heartbeat claim {claim_id!r} with status {row['status']!r}; "
                "claim the work explicitly to start a new ownership epoch"
            )
        if row["expires_at"] is not None and float(row["expires_at"]) <= t:
            raise ValueError(
                f"cannot heartbeat expired claim {claim_id!r}; claim the work explicitly"
            )
        conn.execute(
            "UPDATE claims SET heartbeat_at=?, expires_at=?, step=COALESCE(?,step) WHERE claim_id=?",
            (t, t + lease_s, step, claim_id),
        )
        if row and row["session_id"]:
            session_ids = _related_session_ids_unlocked(
                conn, row["session_id"], actor=row["actor"]
            )
            _renew_sessions_and_claims_unlocked(
                conn, session_ids, at=t, lease_s=lease_s
            )


def release_claim(
    conn,
    claim_id: str,
    status: str = "released",
    reason: str | None = None,
    *,
    next_step: str | None = None,
    resume_when: str | None = None,
    resume_predicate_json: str | None = None,
    resume_manual: bool = False,
    session_id: str | None,
    actor: str | None = None,
    system_caller: str | None = None,
) -> None:
    if status not in RELEASABLE_CLAIM_STATUSES:
        raise ValueError(
            f"release_claim status must be one of {sorted(RELEASABLE_CLAIM_STATUSES)}; "
            "use complete_claim for completed claims"
        )
    if status == "blocked" and not str(reason or "").strip():
        raise ValueError(
            "blocked claim release requires a non-empty reason naming the criterion"
        )
    frees = status in ("released", "unclaimed")
    with tx(conn):
        _assert_claim_holder_unlocked(
            conn,
            claim_id,
            action=f"release(status={status})",
            session_id=session_id,
            actor=actor,
            system_caller=system_caller,
        )
        parked_next_step = str(next_step or "").strip()
        parked_resume_when = str(resume_when or "").strip()
        if status == "paused":
            parked_next_step, parked_resume_when = require_park_resume_contract(
                next_step=next_step,
                resume_when=resume_when,
            )
        canonical_resume_predicate = None
        if status in {"paused", "blocked"}:
            canonical_resume_predicate = normalize_resume_trigger_contract(
                resume_when=parked_resume_when,
                resume_predicate=resume_predicate_json,
                resume_manual=resume_manual,
            )
        t = db_now(conn)
        row = conn.execute(
            "SELECT work_id, status, expires_at FROM claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        # The epoch check heartbeat_claim and complete_claim make, widened to the
        # statuses a claim is still *held* in: unlike those two verbs, release is
        # how a holder moves between running, paused and blocked. What it must
        # refuse is a claim whose epoch already ended -- otherwise a replayed
        # release rewrites the work item's intent_state from a lease nobody
        # holds any more.
        if row is None:
            raise ValueError(f"cannot release missing claim {claim_id!r}")
        if str(row["status"] or "").strip().lower() not in _HELD_CLAIM_STATUSES:
            raise ValueError(
                f"cannot release claim {claim_id!r} with status {row['status']!r}; "
                "claim the work explicitly to start a new ownership epoch"
            )
        if row["expires_at"] is not None and float(row["expires_at"]) <= t:
            raise ValueError(
                f"cannot release expired claim {claim_id!r}; claim the work explicitly"
            )
        conn.execute(
            "UPDATE claims SET status=?, release_reason=?, version=version+1 WHERE claim_id=?",
            (status, reason, claim_id),
        )
        if row and frees:
            terminal_placeholders = ",".join("?" for _ in TERMINAL_WORK_STATES)
            conn.execute(
                "UPDATE work_items SET intent_state='queued', updated_at=?, version=version+1"
                f" WHERE work_id=? AND intent_state NOT IN ({terminal_placeholders})",
                (t, row["work_id"], *TERMINAL_WORK_STATES),
            )
        elif row and status in {"paused", "blocked"}:
            # Terminal work never goes back to paused/blocked: a late park or
            # block against a done row would otherwise resurrect it as open work.
            terminal_placeholders = ",".join("?" for _ in TERMINAL_WORK_STATES)
            conn.execute(
                "UPDATE work_items SET intent_state=?,"
                " blocked_reason_class=CASE WHEN ?='blocked'"
                " THEN COALESCE(NULLIF(blocked_reason_class,''),'blocked')"
                " ELSE blocked_reason_class END,"
                " next_step=COALESCE(NULLIF(?,''),next_step),"
                " resume_when=COALESCE(NULLIF(?,''),resume_when),"
                " resume_predicate_json=COALESCE(NULLIF(?,''),resume_predicate_json),"
                " continuation_ready_at=CASE WHEN NULLIF(?, '') IS NOT NULL"
                " THEN NULL ELSE continuation_ready_at END,"
                " updated_at=?,version=version+1 WHERE work_id=?"
                f" AND intent_state NOT IN ({terminal_placeholders})",
                (
                    status,
                    status,
                    parked_next_step,
                    parked_resume_when,
                    canonical_resume_predicate or "",
                    canonical_resume_predicate or "",
                    t,
                    row["work_id"],
                    *TERMINAL_WORK_STATES,
                ),
            )


BLOCKED_REASON_CLASSES = frozenset(
    {
        "awaiting_author_claim",
        "awaiting_independent_reviewer",
        "awaiting_operator_or_t0_review",
        "awaiting_reverification",
        "awaiting_review",
        "awaiting_t0_review",
        "blocked",
        "concurrent_foreign_work",
        "contract_mismatch_requires_successor",
        "custody_tracking_resolved",
        "dependency_missing_predicate",
        "external_dependency",
        "external_reviews_and_deploy_architecture",
        "independent_reviewer_capacity",
        "interactive_auth",
        "numeric_acceptance_failed",
        "operator_decision",
        "ready_for_owner_resume",
        "scope_split_partially_done",
        "source_contract_invalid",
        "source_missing_legal_designations",
        "source_unpublished_or_inadequate",
        "specification_mismatch",
        "specification_missing",
        "stale_resolved_upstream",
        "stale_state_needs_mapping",
        "stale_terminal_sidecar_incomplete",
        "superseding_epistemic_blocker",
        "terminal_artifact_unbindable_missing_acceptance",
        "upstream_artifact_not_ready",
    }
)


def classify_blocked_work(
    conn,
    *,
    work_id: str,
    reason_class: str,
    expected_version: int,
    expected_reason_class: str | None = None,
    actor: str,
    session_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    clean_work_id = str(work_id or "").strip()
    clean_reason = str(reason_class or "").strip().lower()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_note = str(note or "").strip()
    clean_expected_reason = (
        str(expected_reason_class or "").strip().lower() or None
    )
    if not clean_work_id:
        raise ValueError("blocked classification requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"blocked classification actor must be {_lanes_display()}")
    if not clean_session:
        raise ValueError("blocked classification requires session_id")
    _validate_session_actor(clean_session, clean_actor)
    if clean_reason not in BLOCKED_REASON_CLASSES:
        raise ValueError(
            f"blocked reason class is outside the sanctioned enum: {clean_reason!r}"
        )
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise ValueError("blocked classification expected_version must be an integer")
    if expected_version < 0:
        raise ValueError("blocked classification expected_version must be non-negative")

    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "reason_class": clean_reason,
        "expected_version": expected_version,
        "expected_reason_class": clean_expected_reason,
        "actor": clean_actor,
        "session_id": clean_session,
        "note": clean_note or None,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"blocked-reason:{clean_work_id}:{request_sha[:24]}"

    with tx(conn):
        session = conn.execute(
            "SELECT actor FROM agent_sessions WHERE session_id=?",
            (clean_session,),
        ).fetchone()
        if session is None:
            raise ValueError(
                f"blocked classification session is not registered: {clean_session!r}"
            )
        if str(session["actor"] or "").strip().lower() != clean_actor:
            raise ValueError(
                "blocked classification session actor does not match requested actor"
            )
        row = conn.execute(
            "SELECT intent_state,blocked_reason_class,version FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"blocked classification work item not found: {clean_work_id!r}"
            )
        if str(row["intent_state"] or "").strip().lower() != "blocked":
            raise ValueError(
                f"blocked classification requires intent_state=blocked; "
                f"{clean_work_id!r} is {row['intent_state']!r}"
            )

        replay = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            return {
                "work_id": clean_work_id,
                "reason_class": clean_reason,
                "event_id": int(replay["event_id"]),
                "replayed": True,
            }

        prior_reason = str(row["blocked_reason_class"] or "").strip() or None
        if int(row["version"] or 0) != expected_version:
            raise ValueError(
                f"blocked classification version CAS failed for {clean_work_id!r}: "
                f"expected {expected_version}, observed {row['version']}"
            )
        if prior_reason != clean_expected_reason:
            raise ValueError(
                f"blocked classification reason CAS failed for {clean_work_id!r}: "
                f"expected {clean_expected_reason!r}, observed {prior_reason!r}"
            )
        t = db_now(conn)
        updated = conn.execute(
            "UPDATE work_items SET blocked_reason_class=?,"
            " note=COALESCE(NULLIF(?,''),note),updated_at=?,"
            " version=version+1 WHERE work_id=? AND version=?",
            (clean_reason, clean_note, t, clean_work_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ValueError(
                f"blocked classification version CAS lost for {clean_work_id!r}"
            )
        payload = {
            **request,
            "request_sha256": request_sha,
            "prior_reason_class": prior_reason,
            "version_before": expected_version,
            "version_after": expected_version + 1,
            "lifecycle_mutation": False,
            "claim_mutation": False,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,payload_json,"
            "idempotency_key) VALUES (?,?,?,?,?,'agent',?,?)",
            (
                t,
                "blocked_reason_classified",
                clean_actor,
                clean_session,
                clean_work_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "reason_class": clean_reason,
            "prior_reason_class": prior_reason,
            "version_before": expected_version,
            "version_after": expected_version + 1,
            "event_id": int(cur.lastrowid),
            "replayed": False,
        }


def migrate_blocked_resume_predicate(
    conn,
    *,
    work_id: str,
    expected_version: int,
    expected_reason_class: str,
    expected_resume_when: str | None,
    resume_when: str,
    resume_predicate: str,
    operation_id: str,
    refs: Iterable[str],
    note: str,
    actor: str,
    session_id: str,
) -> dict[str, Any]:
    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_reason = str(expected_reason_class or "").strip().lower()
    clean_resume_when = str(resume_when or "").strip()
    clean_operation_id = str(operation_id or "").strip()
    clean_note = str(note or "").strip()
    clean_refs = tuple(
        ref for ref in (str(item or "").strip() for item in refs or ()) if ref
    )
    clean_expected_resume_when = (
        None
        if expected_resume_when is None
        else str(expected_resume_when).strip()
    )

    if not clean_work_id:
        raise ValueError("resume predicate migration requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"resume predicate migration actor must be {_lanes_display()}")
    if not clean_session:
        raise ValueError("resume predicate migration requires session_id")
    _validate_session_actor(clean_session, clean_actor)
    if clean_reason not in BLOCKED_REASON_CLASSES:
        raise ValueError(
            "resume predicate migration expected reason is outside the sanctioned "
            f"enum: {clean_reason!r}"
        )
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise ValueError("resume predicate migration expected_version must be an integer")
    if expected_version < 0:
        raise ValueError("resume predicate migration expected_version must be non-negative")
    if not clean_resume_when:
        raise ValueError("resume predicate migration requires non-empty resume_when")
    if not clean_operation_id:
        raise ValueError("resume predicate migration requires operation_id")
    if not clean_refs:
        raise ValueError("resume predicate migration requires at least one ref")
    if not clean_note:
        raise ValueError("resume predicate migration requires a receipt note")

    canonical_predicate = normalize_resume_trigger_contract(
        resume_when=clean_resume_when,
        resume_predicate=resume_predicate,
    )
    if canonical_predicate is None:
        raise ValueError("resume predicate migration requires a typed predicate")
    parsed_predicate = json.loads(canonical_predicate)
    if parsed_predicate.get("type") == "manual":
        raise ValueError("resume predicate migration refuses a manual marker")

    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "expected_version": expected_version,
        "expected_reason_class": clean_reason,
        "expected_resume_when": clean_expected_resume_when,
        "resume_when": clean_resume_when,
        "resume_predicate": parsed_predicate,
        "operation_id": clean_operation_id,
        "refs": list(clean_refs),
        "note": clean_note,
        "actor": clean_actor,
        "session_id": clean_session,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"resume-predicate-migration:{clean_work_id}:{request_sha[:24]}"

    with tx(conn):
        session = conn.execute(
            "SELECT actor FROM agent_sessions WHERE session_id=?",
            (clean_session,),
        ).fetchone()
        if session is None:
            raise ValueError(
                f"resume predicate migration session is not registered: {clean_session!r}"
            )
        if str(session["actor"] or "").strip().lower() != clean_actor:
            raise ValueError(
                "resume predicate migration session actor does not match requested actor"
            )

        row = conn.execute(
            "SELECT assignee,intent_state,blocked_reason_class,resume_when,"
            "resume_predicate_json,continuation_ready_at,version FROM work_items"
            " WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"resume predicate migration work item not found: {clean_work_id!r}"
            )
        observed_assignee = str(row["assignee"] or "").strip().lower()
        if observed_assignee != clean_actor:
            raise ValueError(
                "resume predicate migration requires actor to match the current "
                f"assignee: actor={clean_actor!r} assignee={observed_assignee!r}"
            )

        replay = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            return {
                "work_id": clean_work_id,
                "event_id": int(replay["event_id"]),
                "replayed": True,
            }

        if str(row["intent_state"] or "").strip().lower() != "blocked":
            raise ValueError(
                "resume predicate migration requires intent_state=blocked; "
                f"{clean_work_id!r} is {row['intent_state']!r}"
            )
        observed_reason = str(row["blocked_reason_class"] or "").strip().lower()
        if observed_reason != clean_reason:
            raise ValueError(
                f"resume predicate migration reason CAS failed for {clean_work_id!r}: "
                f"expected {clean_reason!r}, observed {observed_reason!r}"
            )
        if int(row["version"] or 0) != expected_version:
            raise ValueError(
                f"resume predicate migration version CAS failed for {clean_work_id!r}: "
                f"expected {expected_version}, observed {row['version']}"
            )
        observed_resume_when_raw = row["resume_when"]
        observed_resume_when = (
            None
            if observed_resume_when_raw is None
            else str(observed_resume_when_raw).strip()
        )
        if observed_resume_when != clean_expected_resume_when:
            raise ValueError(
                f"resume predicate migration resume_when CAS failed for {clean_work_id!r}: "
                f"expected {clean_expected_resume_when!r}, observed {observed_resume_when!r}"
            )
        if row["resume_predicate_json"] is not None:
            raise ValueError(
                "resume predicate migration requires resume_predicate_json IS NULL"
            )
        if row["continuation_ready_at"] is not None:
            raise ValueError(
                "resume predicate migration requires continuation_ready_at IS NULL"
            )

        held = conn.execute(
            "SELECT claim_id FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked') LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        if held is not None:
            raise ValueError(
                f"resume predicate migration refuses held claim {held['claim_id']!r}"
            )
        live_run = conn.execute(
            "SELECT run_id FROM runs WHERE work_id=?"
            " AND state IN ('live','running','waiting') LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        if live_run is not None:
            raise ValueError(
                f"resume predicate migration refuses active run {live_run['run_id']!r}"
            )

        t = db_now(conn)
        updated = conn.execute(
            "UPDATE work_items SET resume_when=?,resume_predicate_json=?,"
            " continuation_ready_at=NULL,updated_at=?,version=version+1"
            " WHERE work_id=? AND assignee=? AND intent_state='blocked'"
            " AND blocked_reason_class=? AND version=? AND resume_when IS ?"
            " AND resume_predicate_json IS NULL AND continuation_ready_at IS NULL",
            (
                clean_resume_when,
                canonical_predicate,
                t,
                clean_work_id,
                clean_actor,
                clean_reason,
                expected_version,
                observed_resume_when_raw,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(
                f"resume predicate migration CAS lost for {clean_work_id!r}"
            )
        payload = {
            **request,
            "request_sha256": request_sha,
            "predicate_type": parsed_predicate["type"],
            "from_predicate": None,
            "to_predicate": parsed_predicate,
            "version_before": expected_version,
            "version_after": expected_version + 1,
            "lifecycle_mutation": False,
            "claim_mutation": False,
            "predicate_evaluated": False,
            "readiness_reset": False,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,title,body,"
            "refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,'agent',?,?,?,?,?)",
            (
                t,
                "resume_predicate_migrated",
                clean_actor,
                clean_session,
                clean_work_id,
                "Typed blocked-row predicate migrated",
                clean_note,
                json.dumps(list(clean_refs), separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "resume_when": clean_resume_when,
            "resume_predicate": parsed_predicate,
            "version_before": expected_version,
            "version_after": expected_version + 1,
            "event_id": int(cur.lastrowid),
            "replayed": False,
        }


def release_classified_block(
    conn,
    *,
    work_id: str,
    expected_reason_class: str,
    actor: str,
    session_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    clean_work_id = str(work_id or "").strip()
    clean_reason = str(expected_reason_class or "").strip().lower()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_note = str(note or "").strip()
    releasable_classes = {
        "ready_for_owner_resume",
        "custody_tracking_resolved",
    }
    if clean_reason not in releasable_classes:
        raise ValueError(
            "classified block release is limited to "
            f"{sorted(releasable_classes)}"
        )
    if clean_actor not in _lane_set():
        raise ValueError(f"classified block release actor must be {_lanes_display()}")
    if not clean_session:
        raise ValueError("classified block release requires session_id")
    _validate_session_actor(clean_session, clean_actor)

    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "expected_reason_class": clean_reason,
        "actor": clean_actor,
        "session_id": clean_session,
        "note": clean_note or None,
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"blocked-release:{clean_work_id}:{request_sha[:24]}"

    with tx(conn):
        replay = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            return {
                "work_id": clean_work_id,
                "prior_reason_class": clean_reason,
                "event_id": int(replay["event_id"]),
                "released_claim_ids": [],
                "replayed": True,
            }
        session = conn.execute(
            "SELECT actor FROM agent_sessions WHERE session_id=?",
            (clean_session,),
        ).fetchone()
        if (
            session is None
            or str(session["actor"] or "").strip().lower() != clean_actor
        ):
            raise ValueError(
                "classified block release requires a registered matching session"
            )
        row = conn.execute(
            "SELECT intent_state,blocked_reason_class FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"classified block release work item not found: {clean_work_id!r}"
            )
        if str(row["intent_state"] or "").strip().lower() != "blocked":
            raise ValueError(
                f"classified block release requires intent_state=blocked; "
                f"{clean_work_id!r} is {row['intent_state']!r}"
            )
        observed_reason = str(row["blocked_reason_class"] or "").strip().lower()
        if observed_reason != clean_reason:
            raise ValueError(
                f"classified block release reason CAS failed: expected "
                f"{clean_reason!r}, observed {observed_reason!r}"
            )
        live_run = conn.execute(
            "SELECT run_id FROM runs WHERE work_id=?"
            " AND state IN ('live','running','waiting') LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        if live_run is not None:
            raise ValueError(
                f"classified block release refuses active run {live_run['run_id']!r}"
            )

        held = conn.execute(
            "SELECT claim_id FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked')",
            (clean_work_id,),
        ).fetchall()
        held_ids = [str(item["claim_id"]) for item in held]
        if held_ids:
            placeholders = ",".join("?" for _ in held_ids)
            conn.execute(
                f"UPDATE claims SET status='released',release_reason=?,"
                f" version=version+1 WHERE claim_id IN ({placeholders})",
                (
                    "classified block released for owner resume",
                    *held_ids,
                ),
            )
        t = db_now(conn)
        conn.execute(
            "UPDATE work_items SET intent_state='queued',blocked_reason_class=NULL,"
            " updated_at=?,version=version+1 WHERE work_id=?",
            (t, clean_work_id),
        )
        payload = {
            **request,
            "request_sha256": request_sha,
            "released_claim_ids": held_ids,
            "lifecycle_transition": "blocked_to_queued",
            "completion_bypass": False,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,payload_json,"
            "idempotency_key) VALUES (?,?,?,?,?,'agent',?,?)",
            (
                t,
                "classified_block_released",
                clean_actor,
                clean_session,
                clean_work_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "prior_reason_class": clean_reason,
            "event_id": int(cur.lastrowid),
            "released_claim_ids": held_ids,
            "replayed": False,
        }


def recover_orphaned_block(
    conn,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    note: str,
) -> dict[str, Any]:
    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_note = str(note or "").strip()
    if not clean_work_id:
        raise ValueError("orphaned block recovery requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"orphaned block recovery actor must be {_lanes_display()}")
    if not clean_session:
        raise ValueError("orphaned block recovery requires session_id")
    if not clean_note:
        raise ValueError("orphaned block recovery requires an explanatory note")
    _validate_session_actor(clean_session, clean_actor)
    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "actor": clean_actor,
        "session_id": clean_session,
        "note": clean_note,
        "expected_state": "blocked_no_claim_no_reason",
    }
    request_sha = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"orphaned-block-recovery:{clean_work_id}:{request_sha[:24]}"

    with tx(conn):
        session = conn.execute(
            "SELECT actor FROM agent_sessions WHERE session_id=?",
            (clean_session,),
        ).fetchone()
        if (
            session is None
            or str(session["actor"] or "").strip().lower() != clean_actor
        ):
            raise ValueError(
                "orphaned block recovery requires a registered matching session"
            )
        replay = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if replay is not None:
            return {
                "work_id": clean_work_id,
                "prior_state": "blocked_no_claim_no_reason",
                "event_id": int(replay["event_id"]),
                "replayed": True,
            }
        now = db_now(conn)
        _release_expired_claims_unlocked(conn, at=now, work_id=clean_work_id)
        row = conn.execute(
            "SELECT intent_state,blocked_reason_class FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"orphaned block recovery work item not found: {clean_work_id!r}"
            )
        state = str(row["intent_state"] or "").strip().lower()
        reason = str(row["blocked_reason_class"] or "").strip()
        if state != "blocked" or reason:
            raise ValueError(
                "recover_blocked requires the exact blocked/no-live-claim/"
                f"null-reason state; observed state={state!r} reason={reason!r}"
            )
        held = conn.execute(
            "SELECT claim_id FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked')"
            " AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
            (clean_work_id, now),
        ).fetchone()
        if held is not None:
            raise ValueError(
                f"recover_blocked refuses held claim {held['claim_id']!r}"
            )
        active_run = conn.execute(
            "SELECT run_id FROM runs WHERE work_id=?"
            " AND state IN ('live','running','waiting') LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        if active_run is not None:
            raise ValueError(
                f"recover_blocked refuses active run {active_run['run_id']!r}"
            )
        conn.execute(
            "UPDATE work_items SET intent_state='queued',blocked_reason_class=NULL,"
            " note=?,updated_at=?,version=version+1 WHERE work_id=?",
            (clean_note, now, clean_work_id),
        )
        payload = {
            **request,
            "request_sha256": request_sha,
            "prior_state": "blocked_no_claim_no_reason",
            "lifecycle_transition": "orphaned_block_to_queued",
            "completion_bypass": False,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,payload_json,"
            "idempotency_key) VALUES (?,?,?,?,?,'agent',?,?)",
            (
                now,
                "orphaned_block_recovered",
                clean_actor,
                clean_session,
                clean_work_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "work_id": clean_work_id,
            "prior_state": "blocked_no_claim_no_reason",
            "event_id": int(cur.lastrowid),
            "replayed": False,
        }


_PARENT_COMPLETE_AGENT_RUNNER_KINDS = frozenset(
    {
        "subagent",
        "workflow",
        "background",
        "claude",
        "codex",
    }
)


def _terminalize_child_agent_state_for_completed_claim_unlocked(
    conn: sqlite3.Connection,
    *,
    parent_session_id: str | None,
    work_id: str,
    at: float,
) -> None:
    parent_sid = str(parent_session_id or "").strip()
    if not parent_sid:
        return
    child_rows = conn.execute(
        "SELECT session_id FROM agent_sessions WHERE parent_session_id=? AND state='active'",
        (parent_sid,),
    ).fetchall()
    child_session_ids = [
        str(row["session_id"]) for row in child_rows if row["session_id"]
    ]
    if child_session_ids:
        placeholders = ",".join("?" for _sid in child_session_ids)
        conn.execute(
            "UPDATE agent_sessions SET state='ended', ended_at=?, version=version+1"
            f" WHERE session_id IN ({placeholders}) AND state='active'",
            (at, *child_session_ids),
        )
    session_ids = [parent_sid, *child_session_ids]
    session_placeholders = ",".join("?" for _sid in session_ids)
    kind_placeholders = ",".join("?" for _kind in _PARENT_COMPLETE_AGENT_RUNNER_KINDS)
    conn.execute(
        "UPDATE runs SET state='orphaned', finished_at=?, version=version+1"
        f" WHERE state='live' AND runner_kind IN ({kind_placeholders})"
        " AND (parent_session_id=?"
        f" OR session_id IN ({session_placeholders})"
        " OR work_id=?)",
        (
            at,
            *_PARENT_COMPLETE_AGENT_RUNNER_KINDS,
            parent_sid,
            *session_ids,
            work_id,
        ),
    )


def _insert_completion_receipt_unlocked(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    work_id: str,
    session_id: str,
    artifact_path: str,
    artifact_kind: str,
    at: float,
    body: str | None = None,
    source: str = "coord_db.complete_claim",
) -> int:

    session = conn.execute(
        "SELECT actor FROM agent_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    actor = str(session["actor"] if session else "").strip().lower()
    if actor not in _lane_set():
        actor = expected_actor_for_session_id(session_id) or "coord"
    kind = f"{actor}_done" if actor in _lane_set() else "coord_done"
    payload_json = json.dumps(
        {
            "action": "done",
            "artifact_kind": artifact_kind,
            "artifact_path": artifact_path,
            "canonical_lifecycle_event": True,
            "claim_id": claim_id,
            "schema_version": 1,
            "source": str(source or "coord_db.complete_claim"),
            "status": "applied",
            "verb": "done",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    clean_body = str(body or "")[:_BODY_CAP] or None
    idempotency_key = f"coord-complete:{claim_id}"
    try:
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,work_id,trust,body,payload_json,"
            " idempotency_key) VALUES (?,?,?,?,?,'agent',?,?,?)",
            (
                at,
                kind,
                actor,
                session_id,
                work_id,
                clean_body,
                payload_json,
                idempotency_key,
            ),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        existing = conn.execute(
            "SELECT 1 FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        failure = (
            "key collision" if existing is not None else "insertion integrity failure"
        )
        raise ValueError(
            f"canonical completion receipt {failure} for claim {claim_id!r}"
        ) from exc


def _proof_root_relative(path: Path, root: str | Path | None = None) -> str | None:
    """Return ``path`` as a POSIX pointer under ``root``, or None if it escapes.

    Artifact pointers are stored the way the controller declares them --
    repo-relative -- so a database stays portable and every containment check
    can prove the pointer without being handed a host-specific absolute path.
    """

    try:
        base = Path(root or HARNESS_ROOT).expanduser().resolve(strict=False)
        resolved = Path(path).expanduser().resolve(strict=False)
        relative = resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    text = relative.as_posix()
    return text or None


def done_signal_satisfied(
    conn: sqlite3.Connection,
    signal: str | None,
    root: str | Path | None = None,
) -> bool:
    from .review_tier import coord_event_done_signal_id

    event_id = coord_event_done_signal_id(signal)
    if event_id is not None:
        return (
            conn.execute(
                "SELECT 1 FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            is not None
        )
    return done_signal_custodied(signal, Path(root or HARNESS_ROOT))


_DONE_SIGNAL_STATE_RE = re.compile(
    r"(?<![A-Z0-9])(?:NOT[\s_-]?READY|FAILED|BLOCKED|INCOMPLETE)(?![A-Z0-9])",
    re.IGNORECASE,
)
_DONE_SIGNAL_LEADING_RE = re.compile(
    r"^\s*(?:[*_]+\s*)?(?:VERDICT|DECISION|STATUS)\s*:\s*(.+)$",
    re.IGNORECASE,
)


def done_signal_blocking_declaration(
    signal: str | None,
    root: str | Path | None = None,
) -> dict[str, Any] | None:
    raw_signal = str(signal or "").strip()
    if not raw_signal or raw_signal.startswith("coord:event:"):
        return None
    base = Path(root or HARNESS_ROOT)
    path = Path(raw_signal).expanduser()
    if not path.is_absolute():
        parts = path.parts
        if parts and parts[0] == "coordharness" and base.name == "coordharness":
            path = base.joinpath(*parts[1:])
        else:
            path = base / path
    if path.suffix.lower() not in {".md", ".txt", ".rst"}:
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(65_536)
    except (OSError, UnicodeError):
        return None
    for line_number, line in enumerate(text.splitlines()[:12], start=1):
        stripped = line.strip()
        candidates: list[tuple[str, bool]] = []
        if "<!--" in stripped and "canonical:" in stripped.lower():
            match = re.search(r"\bstatus\s*:\s*(.*?)(?:-->)", stripped, re.IGNORECASE)
            if match:
                candidates.append((match.group(1).strip(), True))
        leading = _DONE_SIGNAL_LEADING_RE.match(stripped)
        if leading:
            candidates.append((leading.group(1).strip(), False))
        for candidate, canonical in candidates:
            cleaned = candidate.lstrip("*_` ")
            if canonical:
                machine_status = re.split(r"[\s;—]", cleaned, maxsplit=1)[0]
                state_match = _DONE_SIGNAL_STATE_RE.search(machine_status)
            else:
                state_match = _DONE_SIGNAL_STATE_RE.match(cleaned)
            if state_match:
                state = re.sub(r"[\s-]+", "_", state_match.group(0).upper())
                return {
                    "state": state,
                    "quote": stripped[:512],
                    "line_number": line_number,
                    "path": str(path.resolve(strict=False)),
                }
    return None


def reopen_done_signal_status_mismatches(
    conn: sqlite3.Connection,
    *,
    actor: str,
    session_id: str,
    authority_work_id: str,
    root: str | Path | None = None,
    apply: bool = False,
) -> list[dict[str, Any]]:
    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_authority = str(authority_work_id or "").strip()
    if clean_actor not in _lane_set():
        raise ValueError(f"done-signal status sweep actor must be {_lanes_display()}")
    authority = conn.execute(
        "SELECT c.status,w.assignee,w.intent_state FROM claims c "
        "JOIN work_items w ON w.work_id=c.work_id "
        "WHERE c.work_id=? AND c.session_id=? ORDER BY c.acquired_at DESC LIMIT 1",
        (clean_authority, clean_session),
    ).fetchone()
    if (
        authority is None
        or str(authority["status"] or "").lower() != "running"
        or str(authority["assignee"] or "").lower() != clean_actor
        or str(authority["intent_state"] or "").lower() != "running"
    ):
        raise ValueError("done-signal status sweep requires its exact running authority claim")

    base = Path(root or HARNESS_ROOT)
    rows = conn.execute(
        "SELECT work_id,assignee,done_signal,version FROM work_items "
        "WHERE intent_state='done' AND done_signal IS NOT NULL ORDER BY work_id"
    ).fetchall()
    findings: list[dict[str, Any]] = []
    for row in rows:
        declaration = done_signal_blocking_declaration(row["done_signal"], base)
        if declaration is None:
            continue
        findings.append(
            {
                "work_id": str(row["work_id"]),
                "assignee": str(row["assignee"] or ""),
                "done_signal": str(row["done_signal"]),
                "work_version": int(row["version"] or 0),
                **declaration,
            }
        )
    if not apply:
        return findings

    with tx(conn):
        t = db_now(conn)
        for finding in findings:
            updated = conn.execute(
                "UPDATE work_items SET intent_state='blocked',"
                " blocked_reason_class='done_signal_self_declared_incomplete',"
                " updated_at=?,version=version+1 "
                "WHERE work_id=? AND intent_state='done' AND version=?",
                (t, finding["work_id"], finding["work_version"]),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    f"done-signal status sweep CAS drift for {finding['work_id']}"
                )
            payload = {
                "schema_version": 1,
                "writer_contract": "done_signal_status_reopen.v1",
                "authority_work_id": clean_authority,
                "from_intent_state": "done",
                "to_intent_state": "blocked",
                "state": finding["state"],
                "line_number": finding["line_number"],
                "quote": finding["quote"],
                "done_signal": finding["done_signal"],
            }
            cur = conn.execute(
                "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,"
                "trust,title,body,refs_json,payload_json,idempotency_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t,
                    "reopen",
                    clean_actor,
                    clean_session,
                    f"actor:{finding['assignee']}",
                    finding["work_id"],
                    "agent",
                    f"Done-signal self-declared {finding['state']}",
                    finding["quote"],
                    json.dumps([finding["done_signal"], clean_authority]),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    f"done-signal-status-reopen:v1:{finding['work_id']}",
                ),
            )
            finding["event_id"] = int(cur.lastrowid)
            finding["to_intent_state"] = "blocked"
    return findings

_OPERATOR_AUTHORITY_WORK_FIELDS = (
    "work_id",
    "title",
    "assignee",
    "tier",
    "kind",
    "module",
    "sublane",
    "parent_id",
    "acceptance_json",
    "done_signal",
    "context_pack_ref",
    "depends_on",
)


def operator_authority_contract_sha256(row: dict[str, Any] | sqlite3.Row) -> str:
    source = dict(row)
    contract = {
        key: source.get(key)
        for key in _OPERATOR_AUTHORITY_WORK_FIELDS
    }
    return hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _operator_ok_event_is_valid_unlocked(
    event: sqlite3.Row,
    work_id: str,
    work_row: dict[str, Any] | sqlite3.Row,
) -> bool:
    if (
        str(event["kind"] or "").strip().lower() != "operator_ok"
        or str(event["actor"] or "").strip().lower() != "operator"
        or str(event["work_id"] or "").strip() != work_id
        or str(event["trust"] or "").strip().lower() != "system"
    ):
        return False
    try:
        payload = json.loads(str(event["payload_json"] or "{}"))
    except json.JSONDecodeError:
        return False
    return (
        payload.get("schema_version") == 1
        and payload.get("writer_contract") == "operator_ok.v1"
        and payload.get("authority_channel") == "authenticated_resident_controller"
        and payload.get("work_id") == work_id
        and isinstance(payload.get("expected_work_version"), int)
        and payload.get("work_contract_sha256")
        == operator_authority_contract_sha256(work_row)
        and bool(
            re.fullmatch(r"[a-f0-9]{64}", str(payload.get("refs_sha256") or ""))
        )
    )


def _has_valid_operator_ok_unlocked(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    event_id: int | None = None,
    work_row: dict[str, Any] | sqlite3.Row | None = None,
) -> bool:
    # ``work_row`` lets a caller that already holds the row skip the by-primary-key
    # re-SELECT. Every field this reads -- the binding and the contract fields --
    # is a plain ``work_items`` column, and ``v_work_owner`` is ``w.*`` plus joins,
    # so a row from either source hashes to the same contract digest.
    row: dict[str, Any] | sqlite3.Row | None = work_row
    if row is None:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
    if row is None:
        return False
    try:
        raw_bound_id = row["operator_ok_event_id"]
    except (KeyError, IndexError):
        raw_bound_id = None
    row_bound_id = int(raw_bound_id or 0)
    if event_id is not None and int(event_id) != row_bound_id:
        return False
    bound_id = int(event_id or row_bound_id or 0)
    if bound_id <= 0:
        return False
    event = conn.execute(
        "SELECT kind,actor,work_id,trust,payload_json FROM events WHERE event_id=?",
        (bound_id,),
    ).fetchone()
    if event is None:
        return False
    return _operator_ok_event_is_valid_unlocked(event, work_id, row)


OPERATOR_AUTHORITY_CHANNEL = "authenticated_resident_controller"
OPERATOR_SIGN_OFF_WRITER_CONTRACT = "operator_ok.v1"
OPERATOR_REASSIGNMENT_WRITER_CONTRACT = "operator_reassignment.v1"
OPERATOR_REASSIGNMENT_RECEIPT_CONTRACT = "operator_reassignment_receipt.v1"
# Storage callers cannot confer operator authority by spelling a trusted
# string.  The authenticated resident-controller boundary must deliberately
# pass this process-local capability after it verifies its own credential.
_OPERATOR_REASSIGNMENT_CAPABILITY = object()


def operator_sign_off_refs_sha256(refs: list[str]) -> str:
    """Canonical digest of the evidence a sign-off was given against.

    The validator only checks that ``refs_sha256`` is 64 hex characters, so the
    digest exists to make the *list* tamper-evident rather than to be recomputed
    by the reader. Canonicalized the same way every other payload in this module
    is, so a replay of the identical request produces the identical digest.
    """
    return hashlib.sha256(
        json.dumps(refs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def record_operator_sign_off(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    reason: str,
    refs: list[str],
    operation_id: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Mint the one event that lets a human override a review gate.

    This is the writer ``post_event`` refuses to be. ``operator_ok`` needs
    ``trust='system'`` and a payload the public event writer cannot be trusted to
    assemble, so both are stamped here and nowhere else, and the receipt is bound
    to the row under a compare-and-set in the same transaction that mints it --
    an event with nothing pointing at it is not a sign-off,
    ``_has_valid_operator_ok_unlocked`` reads the *binding*.

    This function is the mechanism. It is not the human-only part: it cannot see
    who called it, and nothing in Python can. The channel that makes the
    authority real is ``coord sign-off``, which asks the controlling terminal
    before it gets here. Calling this from anywhere else records an operator
    sign-off that no operator gave.

    Refuses a row with an open review barrier. ``classify_verdict_status`` only
    honours ``operator_ok`` when the barrier is zero, so a sign-off minted while
    an ``audit_request`` or an acceptance repair is outstanding is a *valid event
    that does nothing* -- it would report success and leave the row exactly as
    stuck as it was. Answering with a refusal that names the barrier is the whole
    difference between an escape hatch and a placebo.
    """
    clean_work_id = str(work_id or "").strip()
    clean_reason = str(reason or "").strip()
    clean_operation = str(operation_id or "").strip()
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if not clean_work_id:
        raise ValueError("operator sign-off requires work_id")
    if not clean_reason:
        raise ValueError(
            "operator sign-off requires a reason naming what was accepted"
        )
    if not clean_refs:
        raise ValueError(
            "operator sign-off requires at least one evidence ref; a sign-off "
            "with nothing to point at cannot be reviewed later"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", clean_operation):
        raise ValueError("operator sign-off operation_id must be 8-200 safe characters")
    refs_sha = operator_sign_off_refs_sha256(clean_refs)
    idempotency_key = f"operator-ok:{clean_operation}"

    with tx(conn):
        prior = conn.execute(
            "SELECT event_id,work_id,payload_json,refs_json FROM events"
            " WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"operator sign-off work item not found: {clean_work_id}")
        work = dict(row)
        observed_version = int(work.get("version") or 0)

        if prior is not None:
            try:
                prior_payload = json.loads(str(prior["payload_json"] or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("operator sign-off replay receipt is malformed") from exc
            if (
                str(prior["work_id"] or "") != clean_work_id
                or prior_payload.get("reason") != clean_reason
                or prior_payload.get("refs_sha256") != refs_sha
            ):
                raise ValueError(
                    "operator sign-off operation_id reused for a different request"
                )
            event_id = int(prior["event_id"])
            bound_id = int(work.get("operator_ok_event_id") or 0)
            if bound_id not in {0, event_id}:
                raise ValueError(
                    "operator sign-off replay conflicts with an existing binding"
                )
            binding_backfilled = False
            if bound_id == 0:
                bound = conn.execute(
                    "UPDATE work_items SET operator_ok_event_id=?,updated_at=?,"
                    " version=version+1 WHERE work_id=? AND version=?"
                    " AND operator_ok_event_id IS NULL",
                    (event_id, db_now(conn), clean_work_id, observed_version),
                )
                if bound.rowcount != 1:
                    raise ValueError("operator sign-off replay binding CAS drift")
                observed_version += 1
                binding_backfilled = True
            return {
                "work_id": clean_work_id,
                "event_id": event_id,
                "version": observed_version,
                "binding_backfilled": binding_backfilled,
                "replayed": True,
            }

        if expected_version is not None and int(expected_version) != observed_version:
            raise ValueError(
                f"operator sign-off version drift for {clean_work_id}: expected "
                f"{int(expected_version)}, observed {observed_version}"
            )
        if work.get("archived_at") is not None or str(
            work.get("intent_state") or ""
        ).strip().lower() in TERMINAL_WORK_STATES:
            raise ValueError(
                "operator sign-off refuses terminal or archived work; there is no "
                "gate left to open"
            )
        existing_binding = int(work.get("operator_ok_event_id") or 0)
        if existing_binding > 0 and _has_valid_operator_ok_unlocked(
            conn, clean_work_id
        ):
            raise ValueError(
                f"{clean_work_id} already carries a valid operator sign-off at "
                f"event:{existing_binding}; signing twice would not add authority"
            )
        # A binding that no longer validates is not a sign-off, it is a receipt
        # the row has moved out from under -- the contract digest is over the
        # work's identity and acceptance, so editing either voids it. Refusing
        # here on the strength of a receipt that authorizes nothing would strand
        # the row for good, so a stale binding is superseded rather than
        # protected.

        from . import review_integrity

        status = review_integrity.classify_verdict_status(
            conn, clean_work_id, row=work
        )
        barrier = int(status.get("latest_review_barrier_event_id") or 0)
        if barrier > 0:
            raise ValueError(
                f"operator sign-off refused: {clean_work_id} has an open review "
                f"barrier at event:{barrier}. A sign-off does not answer a review "
                "that was actually requested -- it would be recorded and then "
                "ignored. Answer the request with an opposite-lane verdict, or "
                "sign off on a row whose review was never requested."
            )

        t = db_now(conn)
        payload = {
            "schema_version": 1,
            "writer_contract": OPERATOR_SIGN_OFF_WRITER_CONTRACT,
            "authority_channel": OPERATOR_AUTHORITY_CHANNEL,
            "work_id": clean_work_id,
            "expected_work_version": observed_version,
            "work_contract_sha256": operator_authority_contract_sha256(work),
            "refs_sha256": refs_sha,
            "reason": clean_reason,
            "operation_id": clean_operation,
            "effective_tier_at_sign_off": effective_review_tier_for_work(
                conn, clean_work_id, row=work
            ),
        }
        assignee = str(work.get("assignee") or "").strip().lower()
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "operator_ok",
                "operator",
                None,
                f"actor:{assignee}" if assignee in _lane_set() else None,
                clean_work_id,
                "system",
                f"Operator sign-off for {clean_work_id}",
                clean_reason[:_BODY_CAP],
                json.dumps(clean_refs, sort_keys=True, separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)
        bound = conn.execute(
            "UPDATE work_items SET operator_ok_event_id=?,updated_at=?,"
            " version=version+1 WHERE work_id=? AND version=?"
            " AND COALESCE(operator_ok_event_id,0)=?",
            (event_id, t, clean_work_id, observed_version, existing_binding),
        )
        if bound.rowcount != 1:
            raise ValueError("operator sign-off receipt binding CAS drift")
        return {
            "work_id": clean_work_id,
            "event_id": event_id,
            "version": observed_version + 1,
            "binding_backfilled": False,
            "superseded_event_id": existing_binding or None,
            "replayed": False,
        }


def _flag_repair_request_event_is_valid_unlocked(
    event: sqlite3.Row,
    work_id: str,
) -> bool:

    actor = str(event["actor"] or "").strip().lower()
    session_id = str(event["session_id"] or "").strip()
    target = str(event["to_selector"] or "").strip().lower()
    event_work_id = str(event["work_id"] or "").strip()
    if (
        str(event["kind"] or "").strip().lower() != "audit_request"
        or event_work_id != work_id
        or actor not in _lane_set()
        or expected_actor_for_session_id(session_id) != actor
        or target not in {
            f"actor:{lane}" for lane in _lane_set() if lane != actor
        }
        or str(event["trust"] or "").strip().lower() != "agent"
    ):
        return False
    payload = _strict_json_mapping(event["payload_json"])
    refs = _strict_json_list(event["refs_json"])
    if payload is None or refs is None:
        return False
    expected_payload_keys = {
        "schema_version",
        "writer_contract",
        "request_kind",
        "event_only",
        "source",
        "task",
        "why",
        "acceptance",
        "negative_verdict_event_id",
        "negative_verdict",
        "remediation_event_ids",
        "author_lane",
        "review_lane",
        "effective_tier",
    }
    negative_event_id = _strict_positive_event_id(
        payload.get("negative_verdict_event_id")
    )
    remediation_ids = payload.get("remediation_event_ids")
    parsed_ref_ids = [_flag_repair_event_ref_id(ref) for ref in refs]
    if (
        set(payload) != expected_payload_keys
        or payload.get("schema_version") != 1
        or payload.get("writer_contract") != "flag_repair_audit_request.v1"
        or payload.get("request_kind") != "flag_repair"
        or payload.get("event_only") is not True
        or not isinstance(payload.get("source"), str)
        or not str(payload.get("task") or "").strip()
        or not str(payload.get("why") or "").strip()
        or not (
            payload.get("acceptance") is None
            or isinstance(payload.get("acceptance"), str)
        )
        or negative_event_id is None
        or payload.get("negative_verdict") not in _FLAG_REPAIR_NEGATIVE_VERDICTS
        or not isinstance(remediation_ids, list)
        or not remediation_ids
        or any(_strict_positive_event_id(value) is None for value in remediation_ids)
        or remediation_ids != sorted(set(remediation_ids))
        or any(int(value) <= negative_event_id for value in remediation_ids)
        or payload.get("author_lane") != actor
        or payload.get("review_lane") != target.split(":", 1)[1]
        or payload.get("effective_tier") != "T1"
        or not refs
        or len(refs) > 32
        or any(ref_id is None for ref_id in parsed_ref_ids)
        or len(set(refs)) != len(refs)
        or set(int(ref_id) for ref_id in parsed_ref_ids if ref_id is not None)
        != {negative_event_id, *(int(value) for value in remediation_ids)}
        or max(
            int(ref_id) for ref_id in parsed_ref_ids if ref_id is not None
        )
        >= int(event["event_id"])
    ):
        return False
    canonical_request = {
        "work_id": event_work_id,
        "actor": actor,
        "session_id": session_id,
        "to_selector": target,
        "refs": refs,
        "payload": {key: value for key, value in payload.items() if key != "source"},
    }
    request_sha256 = hashlib.sha256(
        json.dumps(
            canonical_request, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return str(event["idempotency_key"] or "") == (
        f"flag-repair-audit-request:{actor}:{request_sha256}"
    )


def completion_review_state(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    row: dict[str, Any] | sqlite3.Row | None = None,
) -> dict[str, Any]:
    if row is None:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"completion review state work_id not found: {work_id}")
    work = dict(row)
    from .review_tier import t0_predicate_reasons

    tier = effective_review_tier_for_work(conn, work_id, row=work)
    stored_rubric = str(work.get("rubric_verdict") or "").strip().lower()
    operator_ok = _has_valid_operator_ok_unlocked(conn, work_id)
    from . import review_integrity

    ordered_status = review_integrity.classify_verdict_status(
        conn, work_id, row=work
    )
    latest_request_event_id = ordered_status.get("latest_request_event_id")
    latest_repair_event_id = ordered_status.get(
        "latest_acceptance_contract_repair_event_id"
    )
    latest_review_barrier_event_id = ordered_status.get(
        "latest_review_barrier_event_id"
    )
    ordered_verdict = (
        str(ordered_status.get("verdict") or "").strip().lower()
        if ordered_status.get("reason") == "independent_verdict"
        else ""
    )
    latest_request_row: sqlite3.Row | None = None
    if latest_request_event_id is not None:
        latest_request_row = conn.execute(
            "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
            " refs_json,payload_json,idempotency_key FROM events"
            " WHERE event_id=? AND work_id=?"
            " AND kind='audit_request'",
            (int(latest_request_event_id), work_id),
        ).fetchone()
    flag_repair_cycle = bool(
        latest_request_row is not None
        and _flag_repair_request_event_is_valid_unlocked(
            latest_request_row, work_id
        )
    )
    acceptance_repair_is_latest_barrier = bool(
        latest_repair_event_id is not None
        and latest_repair_event_id == latest_review_barrier_event_id
    )
    stored_negative = stored_rubric in {
        "flag",
        "blocked",
        "block",
        "fail",
        "failed",
        "red",
        "reject",
        "rejected",
    }
    if latest_review_barrier_event_id is not None:
        effective_rubric = ordered_verdict
        if not effective_rubric and (
            flag_repair_cycle
            or (acceptance_repair_is_latest_barrier and stored_negative)
        ):
            effective_rubric = stored_rubric
        operator_satisfies_review = False
    else:
        effective_rubric = stored_rubric
        operator_satisfies_review = operator_ok
    negative = effective_rubric in {
        "flag",
        "blocked",
        "block",
        "fail",
        "failed",
        "red",
        "reject",
        "rejected",
    }
    return {
        "work_id": work_id,
        "effective_tier": tier,
        "tier_reasons": t0_predicate_reasons(work),
        "rubric_verdict": stored_rubric or None,
        "effective_rubric_verdict": effective_rubric or None,
        "operator_ok": operator_ok,
        "operator_ok_satisfies_review": operator_satisfies_review,
        "latest_audit_request_event_id": latest_request_event_id,
        "latest_acceptance_contract_repair_event_id": latest_repair_event_id,
        "latest_review_barrier_event_id": latest_review_barrier_event_id,
        "review_resolution_event_id": ordered_status.get("verdict_event_id"),
        "review_reopened": bool(
            latest_review_barrier_event_id is not None and not ordered_verdict
        ),
        "flag_repair_cycle": flag_repair_cycle,
        "acceptance_repair_is_latest_barrier": acceptance_repair_is_latest_barrier,
        "negative_verdict": negative,
        "needs_review": (
            (tier == "T0" or flag_repair_cycle)
            and effective_rubric != "pass"
            and not operator_satisfies_review
        ),
        "satisfied": not negative and (
            (tier != "T0" and not flag_repair_cycle)
            or effective_rubric == "pass"
            or operator_satisfies_review
        ),
    }


def effective_review_tier_for_work(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    row: dict[str, Any] | sqlite3.Row | None = None,
) -> str:
    if row is None:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"effective review tier work_id not found: {work_id}")
    work = dict(row)
    from .review_tier import effective_review_tier

    return effective_review_tier(
        work,
        tier_down_authorized=_tier_down_authorized_unlocked(conn, work_id, work),
    )


def complete_claim(
    conn,
    claim_id: str,
    *,
    artifact_path: str | None = None,
    artifact_kind: str = "done_signal",
    enforce_terminal_status: bool = True,
    proof_root: str | Path | None = None,
    receipt_body: str | None = None,
    receipt_source: str = "coord_db.complete_claim",
    session_id: str | None,
    actor: str | None = None,
    system_caller: str | None = None,
) -> str:
    with tx(conn):
        t = db_now(conn)
        _assert_claim_holder_unlocked(
            conn,
            claim_id,
            action="complete",
            session_id=session_id,
            actor=actor,
            system_caller=system_caller,
        )
        row = conn.execute(
            "SELECT c.work_id, c.session_id, c.status, c.expires_at,"
            " w.done_signal, w.acceptance_json,"
            " w.rubric_verdict,w.title,w.tier,w.kind,w.module,w.sublane,"
            " w.context_pack_ref,w.depends_on,w.assignee,w.version,"
            " w.operator_ok_event_id,"
            " w.tier_correction_event_id"
            " FROM claims c"
            " JOIN work_items w ON w.work_id=c.work_id"
            " WHERE c.claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"cannot complete missing claim {claim_id!r}")
        if str(row["status"] or "").strip().lower() != "running":
            raise ValueError(
                f"cannot complete claim {claim_id!r} with status {row['status']!r}"
            )
        if row["expires_at"] is not None and float(row["expires_at"]) <= t:
            raise ValueError(f"cannot complete expired claim {claim_id!r}")
        if enforce_terminal_status:
            from coordharness.coord.agent_cli import done_terminal_block_reason

            terminal_reason = done_terminal_block_reason(dict(row))
            if terminal_reason:
                raise ValueError(terminal_reason)
            live_local = conn.execute(
                "SELECT run_id, runner_kind, sidecar_path FROM runs"
                " WHERE work_id=? AND state IN ('live','running','waiting')"
                " AND runner_kind IN ('local_cpu', 'local_gpu')"
                " AND COALESCE(sidecar_path, '') <> ''"
                " ORDER BY started_at DESC LIMIT 1",
                (row["work_id"],),
            ).fetchone()
            if live_local is not None:
                raise ValueError(
                    f"cannot complete claim {claim_id!r}: live local run "
                    f"{live_local['run_id']!r} ({live_local['runner_kind']}) still active"
                )
        review_state = completion_review_state(
            conn,
            str(row["work_id"]),
            row=row,
        )
        if review_state["negative_verdict"]:
            raise ValueError(
                f"cannot complete claim {claim_id!r}: negative audit verdict "
                f"{review_state['rubric_verdict']!r} is unresolved"
            )
        if review_state["needs_review"]:
            raise ValueError(
                f"cannot complete claim {claim_id!r}: T0 review has not passed "
                "and no valid operator-ok event is bound"
            )
        normalized_artifact_kind = (
            str(artifact_kind or "done_signal").strip() or "done_signal"
        )
        if normalized_artifact_kind in _NON_PROOF_ARTIFACT_KINDS:
            raise ValueError(
                f"complete_claim artifact_kind must be proof-capable; "
                f"got {normalized_artifact_kind!r}"
            )
        declared_proof = str(row["done_signal"] or "").strip()
        explicit_proof = str(artifact_path or "").strip()
        root = Path(proof_root or HARNESS_ROOT)
        if not declared_proof:
            raise ValueError(
                f"complete_claim requires controller-declared work_items.done_signal artifact proof "
                f"for claim {claim_id!r}"
            )
        from .review_tier import coord_event_done_signal_id

        event_proof_id = coord_event_done_signal_id(declared_proof)
        declared_path: Path | None = None
        declared_was_absolute = False
        if event_proof_id is not None:
            if explicit_proof and explicit_proof != declared_proof:
                raise ValueError(
                    f"complete_claim artifact_path must match the controller-declared "
                    f"done_signal for claim {claim_id!r}"
                )
        else:
            declared_path = Path(declared_proof).expanduser()
            declared_was_absolute = declared_path.is_absolute()
            if not declared_path.is_absolute():
                parts = declared_path.parts
                if parts and parts[0] == "coordharness" and root.name == "coordharness":
                    declared_path = root.joinpath(*parts[1:])
                else:
                    declared_path = root / declared_path
            if declared_proof and explicit_proof:
                explicit_path = Path(explicit_proof).expanduser()
                if not explicit_path.is_absolute():
                    parts = explicit_path.parts
                    if parts and parts[0] == "coordharness" and root.name == "coordharness":
                        explicit_path = root.joinpath(*parts[1:])
                    else:
                        explicit_path = root / explicit_path
                if os.path.realpath(declared_path) != os.path.realpath(explicit_path):
                    raise ValueError(
                        f"complete_claim artifact_path must match the controller-declared "
                        f"done_signal for claim {claim_id!r}"
                    )
        proof = declared_proof
        if not done_signal_satisfied(conn, proof, root):
            # A valid Markdown proof can fail only because it is not in the Git
            # index. Invalid or empty artifacts need content repair, not git add.
            if done_signal_exists(proof, root):
                raise ValueError(
                    f"complete_claim artifact proof exists but is not carried by git's "
                    f"index for claim {claim_id!r}: {proof}. The custody gate requires "
                    f"the proof to be staged -- run `git add {proof}` and retry. Staging "
                    f"is enough; it does not need to be committed."
                )
            path_part = proof.split("::", 1)[0]
            physical = Path(path_part).expanduser()
            if not physical.is_absolute():
                physical = root / physical
            if physical.exists():
                raise ValueError(
                    f"complete_claim artifact proof exists but is empty, incomplete, "
                    f"or not an admissible completion proof for claim {claim_id!r}: {proof}"
                )
            raise ValueError(
                f"complete_claim artifact proof does not exist for "
                f"claim {claim_id!r}: {proof}"
            )
        declaration = done_signal_blocking_declaration(proof, root)
        if declaration is not None:
            raise ValueError(
                f"cannot complete claim {claim_id!r}: done_signal self-declares "
                f"{declaration['state']} on line {declaration['line_number']}: "
                f"{declaration['quote']!r}; block or park the work instead"
            )
        # Completion proofs are recorded the way the board declares them:
        # repo-relative to the proof root. Storing the resolved absolute path
        # made the row unportable and unverifiable -- every reader that proves
        # containment refuses an absolute pointer, so the first successful
        # completion turned `coord doctor` permanently red.
        stored_proof = proof
        if event_proof_id is None and declared_path is not None and declared_was_absolute:
            relative = _proof_root_relative(declared_path, root)
            stored_proof = relative if relative is not None else str(declared_path)
        exact = conn.execute(
            "SELECT 1 FROM artifacts WHERE work_id=? AND path=? AND COALESCE(kind,'')=? LIMIT 1",
            (row["work_id"], stored_proof, normalized_artifact_kind),
        ).fetchone()
        if exact is None:
            conn.execute(
                "INSERT INTO artifacts(artifact_id, work_id, path, kind,"
                " validation_json, created_at) VALUES (?,?,?,?,?,?)",
                (
                    new_id("art"),
                    row["work_id"],
                    stored_proof,
                    normalized_artifact_kind,
                    "{}",
                    t,
                ),
            )
        conn.execute(
            "UPDATE claims SET status='completed', version=version+1 WHERE claim_id=?",
            (claim_id,),
        )
        conn.execute(
            "UPDATE work_items SET intent_state='done', updated_at=?, version=version+1"
            " WHERE work_id=?",
            (t, row["work_id"]),
        )
        _terminalize_child_agent_state_for_completed_claim_unlocked(
            conn,
            parent_session_id=row["session_id"],
            work_id=row["work_id"],
            at=t,
        )
        _insert_completion_receipt_unlocked(
            conn,
            claim_id=claim_id,
            work_id=str(row["work_id"]),
            session_id=str(row["session_id"]),
            artifact_path=stored_proof,
            artifact_kind=normalized_artifact_kind,
            at=t,
            body=receipt_body,
            source=receipt_source,
        )
    return stored_proof


def prune_terminal(
    conn: sqlite3.Connection,
    *,
    older_than_s: float = 7 * 86_400,
    dry_run: bool = False,
    at: float | None = None,
) -> dict[str, Any]:
    t = at if at is not None else db_now(conn)
    cutoff = float(t) - float(older_than_s)
    placeholders = ",".join("?" for _ in TERMINAL_WORK_STATES)
    rows = conn.execute(
        "SELECT w.work_id, w.intent_state, w.done_signal, w.rubric_verdict,"
        " EXISTS(SELECT 1 FROM artifacts a WHERE a.work_id=w.work_id) AS has_artifact"
        " FROM work_items w"
        f" WHERE w.intent_state IN ({placeholders})"
        " AND w.archived_at IS NULL"
        " AND COALESCE(w.updated_at, w.created_at, 0) < ?",
        (*TERMINAL_WORK_STATES, cutoff),
    ).fetchall()
    proof_rows = [
        r
        for r in rows
        if str(r["done_signal"] or "").strip()
        or str(r["rubric_verdict"] or "").strip()
        or bool(r["has_artifact"])
    ]
    proofless_rows = [r for r in rows if r not in proof_rows]
    if rows and not dry_run:
        with tx(conn):
            update_t = db_now(conn)
            conn.executemany(
                "UPDATE work_items SET archived_at=?, updated_at=?, version=version+1"
                " WHERE work_id=? AND archived_at IS NULL",
                [(update_t, update_t, r["work_id"]) for r in rows],
            )
    by_state: dict[str, int] = {}
    for r in rows:
        state = str(r["intent_state"] or "")
        by_state[state] = by_state.get(state, 0) + 1
    return {
        "dry_run": dry_run,
        "scanned_terminal": len(rows),
        "archived": 0 if dry_run else len(rows),
        "proof_retained": len(proof_rows),
        "proofless_archived": len(proofless_rows),
        "by_state": by_state,
    }


TERMINAL_RUN_STATES = {
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "exhausted",
    "success",
    "stale",
    "orphaned",
}


def _resolve_root_session(conn, session_id: str, _max_depth: int = 16) -> str:
    cur = session_id
    seen: set[str] = set()
    for _ in range(_max_depth):
        if cur in seen:
            return cur
        seen.add(cur)
        row = conn.execute(
            "SELECT parent_session_id FROM agent_sessions WHERE session_id=?", (cur,)
        ).fetchone()
        if row is None or not row["parent_session_id"]:
            return cur
        cur = row["parent_session_id"]
    return cur


def appear_run(
    conn,
    *,
    work_id: str | None = None,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    runner_kind: str,
    model: str | None = None,
    progress_mode: str = "indeterminate",
    sidecar_path: str | None = None,
    pid: int | None = None,
    pgid: int | None = None,
    resource_class: str | None = None,
    run_id: str | None = None,
    pid_started_at: float | None = None,
    heartbeat_at: float | None = None,
    initial_state: str = "live",
) -> str:
    explicit = run_id is not None
    run_id = run_id or new_id("run")
    verb = "INSERT OR IGNORE INTO" if explicit else "INSERT INTO"
    if parent_session_id is not None:
        parent_session_id = _resolve_root_session(conn, parent_session_id)
    with tx(conn):
        t = db_now(conn)
        observed_at = float(heartbeat_at) if heartbeat_at is not None else t
        if pid is not None and pid_started_at is None:
            pid_started_at = pid_start_time(pid)
        observed_state = str(initial_state or "live").strip().lower() or "live"
        conn.execute(
            f"{verb} runs(run_id, work_id, session_id, parent_session_id, runner_kind,"
            " model, progress_mode, sidecar_path, pid, pid_started_at, pgid, resource_class,"
            " host_id, started_at, heartbeat_at, state, version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?, 0)",
            (
                run_id,
                work_id,
                session_id,
                parent_session_id,
                runner_kind,
                model,
                progress_mode,
                sidecar_path,
                pid,
                pid_started_at,
                pgid,
                resource_class,
                stable_host_id(),
                t,
                observed_at,
                observed_state,
            ),
        )
        if explicit:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None and str(row["state"] or "") not in TERMINAL_RUN_STATES:
                desired = {
                    "runner_kind": runner_kind,
                    "progress_mode": progress_mode,
                    "heartbeat_at": observed_at,
                    "finished_at": None,
                    "state": observed_state,
                }
                for key, value in (
                    ("work_id", work_id),
                    ("session_id", session_id),
                    ("parent_session_id", parent_session_id),
                    ("model", model),
                    ("sidecar_path", sidecar_path),
                    ("pid", pid),
                    ("pid_started_at", pid_started_at),
                    ("pgid", pgid),
                    ("resource_class", resource_class),
                ):
                    if value is not None:
                        if key == "sidecar_path" and row[key] not in (None, ""):
                            continue
                        desired[key] = value
                changed = {
                    key: value
                    for key, value in desired.items()
                    if row[key] != value
                    and not (
                        key == "heartbeat_at"
                        and row[key] is not None
                        and float(row[key]) >= float(value)
                    )
                }
                if changed:
                    sets = ", ".join(f"{key}=?" for key in changed)
                    conn.execute(
                        f"UPDATE runs SET {sets}, version=version+1 WHERE run_id=?",
                        (*changed.values(), run_id),
                    )
    return run_id


def finalize_run(conn, run_id: str, state: str = "done") -> None:
    with tx(conn):
        t = db_now(conn)
        terminal_placeholders = ",".join("?" for _ in TERMINAL_RUN_STATES)
        conn.execute(
            "UPDATE runs SET"
            f" state=CASE WHEN COALESCE(state, '') IN ({terminal_placeholders}) THEN state ELSE ? END,"
            f" finished_at=CASE WHEN COALESCE(state, '') IN ({terminal_placeholders})"
            " AND finished_at IS NOT NULL THEN finished_at ELSE ? END,"
            f" version=CASE WHEN COALESCE(state, '') IN ({terminal_placeholders})"
            " AND finished_at IS NOT NULL THEN version ELSE version+1 END"
            " WHERE run_id=? AND ("
            f" COALESCE(state, '') NOT IN ({terminal_placeholders}) OR finished_at IS NULL)",
            (
                *TERMINAL_RUN_STATES,
                state,
                *TERMINAL_RUN_STATES,
                t,
                *TERMINAL_RUN_STATES,
                run_id,
                *TERMINAL_RUN_STATES,
            ),
        )


def runstore_v2_enabled() -> bool:
    raw = str(os.environ.get("COORD_RUNSTORE_V2", "1") or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def runs_read_model(
    conn,
    *,
    work_id: str | None = None,
    session_id: str | None = None,
    state: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    enabled = runstore_v2_enabled()
    try:
        bounded_limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        bounded_limit = 100
    if not enabled:
        return {
            "enabled": False,
            "env_flag": "COORD_RUNSTORE_V2",
            "rows": [],
            "count": 0,
            "truncated": False,
        }
    where: list[str] = []
    params: list[Any] = []
    if work_id:
        where.append("work_id=?")
        params.append(work_id)
    if session_id:
        where.append("session_id=?")
        params.append(session_id)
    if state:
        where.append("state=?")
        params.append(state)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"SELECT * FROM v_runs_read_model{clause} ORDER BY started_at DESC, run_id DESC LIMIT ?",
        [*params, bounded_limit + 1],
    ).fetchall()
    out = [dict(row) for row in rows[:bounded_limit]]
    return {
        "enabled": True,
        "env_flag": "COORD_RUNSTORE_V2",
        "rows": out,
        "count": len(out),
        "truncated": len(rows) > bounded_limit,
    }


def store_artifact(
    conn,
    *,
    path: str,
    work_id: str | None = None,
    run_id: str | None = None,
    kind: str | None = None,
    sha256: str | None = None,
    validation_json: str = "{}",
) -> str:
    artifact_id = new_id("art")
    with tx(conn):
        t = db_now(conn)
        conn.execute(
            "INSERT INTO artifacts(artifact_id, work_id, run_id, path, kind, sha256,"
            " validation_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, work_id, run_id, path, kind, sha256, validation_json, t),
        )
    return artifact_id


def _acceptance_from_work_row(work: dict[str, Any]) -> Any:
    raw = work.get("acceptance_json")
    try:
        parsed = json.loads(raw or "null")
    except (TypeError, json.JSONDecodeError):
        return raw
    if isinstance(parsed, dict) and set(parsed) == {"acceptance"}:
        return parsed["acceptance"]
    return parsed


def compact_event_payload_same_as_row(
    payload: dict[str, Any], work: dict[str, Any] | None
) -> dict[str, Any]:
    if not work:
        return dict(payload)
    out = dict(payload)
    row_values = {
        "task": work.get("title"),
        "why": work.get("note"),
        "acceptance": _acceptance_from_work_row(work),
    }
    same = dict(out.get("same_as_row") or {})
    for key, row_field in (
        ("task", "title"),
        ("why", "note"),
        ("acceptance", "acceptance_json"),
    ):
        if key in out and out[key] == row_values[key]:
            out.pop(key)
            same[key] = row_field
    if same:
        out["same_as_row"] = same
    return out


def expand_event_payload_same_as_row(
    payload: dict[str, Any], work: dict[str, Any] | None
) -> dict[str, Any]:
    out = dict(payload)
    same = out.get("same_as_row")
    if not isinstance(same, dict) or not work:
        return out
    row_values = {
        "title": work.get("title"),
        "note": work.get("note"),
        "acceptance_json": _acceptance_from_work_row(work),
    }
    for key, row_field in same.items():
        if key not in out and row_field in row_values:
            out[key] = row_values[row_field]
    return out


def _flag_repair_event_ref_id(value: Any) -> int | None:

    match = re.fullmatch(r"(?:coord:)?event:([1-9][0-9]*)", str(value or ""))
    return int(match.group(1)) if match else None


def post_flag_repair_audit_request(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    session_id: str,
    to_selector: str,
    task: str,
    why: str,
    refs: Iterable[str],
    acceptance: str | None = None,
    source: str = "coord_db.post_flag_repair_audit_request",
) -> dict[str, Any]:

    clean_work_id = str(work_id or "").strip()
    clean_actor = str(actor or "").strip().lower()
    clean_session_id = str(session_id or "").strip()
    clean_target = str(to_selector or "").strip().lower()
    clean_task = str(task or "").strip()
    clean_why = str(why or "").strip()
    clean_acceptance = str(acceptance or "").strip() or None
    clean_refs = [str(ref).strip() for ref in refs if str(ref).strip()]
    if not clean_work_id:
        raise ValueError("flag_repair requires work_id")
    if clean_actor not in _lane_set():
        raise ValueError(f"flag_repair requires actor {_lanes_display()}")
    if expected_actor_for_session_id(clean_session_id) != clean_actor:
        raise ValueError("flag_repair requires an actor-namespaced author session")
    if not clean_task or not clean_why:
        raise ValueError("flag_repair requires non-empty task and why")
    if not clean_refs or len(clean_refs) > 32:
        raise ValueError("flag_repair requires 1-32 exact coord event refs")
    if len(set(clean_refs)) != len(clean_refs):
        raise ValueError("flag_repair refs must be unique")
    ref_ids = [_flag_repair_event_ref_id(ref) for ref in clean_refs]
    if any(event_id is None for event_id in ref_ids):
        raise ValueError(
            "flag_repair refs must be exact event:<id> or coord:event:<id> pointers"
        )
    exact_ref_ids = [int(event_id) for event_id in ref_ids if event_id is not None]

    with tx(conn):
        work = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (clean_work_id,)
        ).fetchone()
        if work is None:
            raise ValueError(f"flag_repair work_id not found: {clean_work_id}")
        work_dict = dict(work)
        if work_dict.get("archived_at") is not None:
            raise ValueError("flag_repair refuses archived work")
        tier = effective_review_tier_for_work(
            conn, clean_work_id, row=work_dict
        )
        if tier != "T1":
            raise ValueError(
                f"flag_repair requires effective tier T1, got {tier}; ordinary "
                "request_audit remains T0-only"
            )
        intent_state = str(work_dict.get("intent_state") or "").strip().lower()
        if intent_state not in {"queued", "blocked"}:
            raise ValueError(
                "flag_repair requires current intent_state queued|blocked, got "
                f"{intent_state or '<empty>'}"
            )
        assignee = str(work_dict.get("assignee") or "").strip().lower()
        author_lane = _latest_claim_author_lane_unlocked(conn, clean_work_id)
        if assignee != clean_actor or author_lane != clean_actor:
            raise ValueError(
                "flag_repair actor must be the current row owner and author lane"
            )
        registered_session = conn.execute(
            "SELECT actor,state FROM agent_sessions WHERE session_id=?",
            (clean_session_id,),
        ).fetchone()
        if (
            registered_session is None
            or str(registered_session["actor"] or "").strip().lower()
            != clean_actor
            or str(registered_session["state"] or "").strip().lower()
            != "active"
        ):
            raise ValueError(
                "flag_repair requires an active actor-matched author session"
            )
        cross_lane_targets = {
            f"actor:{lane}" for lane in _lane_set() if lane != clean_actor
        }
        if clean_target not in cross_lane_targets:
            raise ValueError(
                "flag_repair target must be another lane, one of "
                f"{sorted(cross_lane_targets)}"
            )
        opposite_lane = clean_target.split(":", 1)[1]

        latest_verdict = conn.execute(
            "SELECT event_id,actor,session_id,to_selector,verdict FROM events"
            " WHERE work_id=? AND kind='audit_verdict'"
            " ORDER BY event_id DESC LIMIT 1",
            (clean_work_id,),
        ).fetchone()
        if latest_verdict is None:
            raise ValueError(
                "flag_repair requires a latest same-row negative audit_verdict"
            )
        negative_event_id = int(latest_verdict["event_id"])
        negative_verdict = str(latest_verdict["verdict"] or "").strip().upper()
        negative_actor = str(latest_verdict["actor"] or "").strip().lower()
        negative_session_lane = expected_actor_for_session_id(
            str(latest_verdict["session_id"] or "")
        )
        if (
            negative_verdict not in _FLAG_REPAIR_NEGATIVE_VERDICTS
            or negative_actor != opposite_lane
            or negative_session_lane != opposite_lane
        ):
            raise ValueError(
                "flag_repair requires the latest same-row verdict to be an "
                "opposite-lane FLAG or BLOCKED"
            )
        stored_rubric = str(work_dict.get("rubric_verdict") or "").strip().upper()
        review_state = completion_review_state(
            conn, clean_work_id, row=work_dict
        )
        if (
            stored_rubric != negative_verdict
            or not review_state["negative_verdict"]
        ):
            raise ValueError(
                "flag_repair requires the latest opposite-lane FLAG/BLOCKED to "
                "remain the current effective negative verdict"
            )
        if negative_event_id not in exact_ref_ids:
            raise ValueError(
                "flag_repair requires an exact ref to the latest same-row "
                f"negative audit_verdict event:{negative_event_id}"
            )

        placeholders = ",".join("?" for _ in exact_ref_ids)
        referenced = conn.execute(
            "SELECT event_id,kind,actor,session_id,work_id,trust,refs_json,"
            "payload_json,verdict"
            f" FROM events WHERE event_id IN ({placeholders})",
            exact_ref_ids,
        ).fetchall()
        by_id = {int(event["event_id"]): event for event in referenced}
        if len(by_id) != len(exact_ref_ids):
            raise ValueError("flag_repair refs include a missing coord event")
        remediation_ids: list[int] = []
        for event_id in exact_ref_ids:
            event = by_id[event_id]
            if event_id == negative_event_id:
                if str(event["work_id"] or "").strip() != clean_work_id:
                    raise ValueError(
                        "flag_repair negative verdict ref is from another work"
                    )
                continue
            if str(event["work_id"] or "").strip() != clean_work_id:
                raise ValueError("flag_repair remediation ref is from another work")
            if event_id <= negative_event_id:
                raise ValueError(
                    "flag_repair remediation refs must strictly postdate the "
                    "negative verdict"
                )
            event_actor = str(event["actor"] or "").strip().lower()
            event_session_lane = expected_actor_for_session_id(
                str(event["session_id"] or "")
            )
            event_kind = str(event["kind"] or "").strip().lower()
            try:
                evidence_refs = json.loads(str(event["refs_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                evidence_refs = []
            try:
                evidence_payload = json.loads(
                    str(event["payload_json"] or "{}")
                )
            except (TypeError, json.JSONDecodeError):
                evidence_payload = {}
            remediation_of_event_id = (
                evidence_payload.get("remediation_of_event_id")
                if isinstance(evidence_payload, dict)
                else None
            )
            if (
                event_actor != clean_actor
                or event_session_lane != clean_actor
                or str(event["trust"] or "").strip().lower() != "agent"
                or event_kind not in _FLAG_REPAIR_REMEDIATION_KINDS
                or not isinstance(evidence_refs, list)
                or not any(
                    isinstance(ref, str) and ref.strip() for ref in evidence_refs
                )
                or isinstance(remediation_of_event_id, bool)
                or not isinstance(remediation_of_event_id, int)
                or remediation_of_event_id != negative_event_id
            ):
                raise ValueError(
                    "flag_repair remediation refs must identify same-row, "
                    "author-lane evidence events after and explicitly bound to "
                    "the latest negative verdict"
                )
            remediation_ids.append(event_id)
        if not remediation_ids:
            raise ValueError(
                "flag_repair requires at least one post-verdict remediation "
                "evidence event"
            )

        payload = {
            "schema_version": 1,
            "writer_contract": "flag_repair_audit_request.v1",
            "request_kind": "flag_repair",
            "event_only": True,
            "source": str(source or "").strip(),
            "task": clean_task,
            "why": clean_why,
            "acceptance": clean_acceptance,
            "negative_verdict_event_id": negative_event_id,
            "negative_verdict": negative_verdict,
            "remediation_event_ids": sorted(remediation_ids),
            "author_lane": clean_actor,
            "review_lane": opposite_lane,
            "effective_tier": tier,
        }
        canonical_request = {
            "work_id": clean_work_id,
            "actor": clean_actor,
            "session_id": clean_session_id,
            "to_selector": clean_target,
            "refs": clean_refs,
            "payload": {
                key: value for key, value in payload.items() if key != "source"
            },
        }
        request_sha256 = hashlib.sha256(
            json.dumps(
                canonical_request, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        key = f"flag-repair-audit-request:{clean_actor}:{request_sha256}"
        later_request = conn.execute(
            "SELECT event_id,idempotency_key FROM events WHERE work_id=?"
            " AND kind='audit_request' AND event_id>?"
            " ORDER BY event_id DESC LIMIT 1",
            (clean_work_id, negative_event_id),
        ).fetchone()
        if (
            later_request is not None
            and str(later_request["idempotency_key"] or "") != key
        ):
            raise ValueError(
                "flag_repair review cycle is already reopened; only an exact "
                "replay is allowed"
            )
        prior = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?", (key,)
        ).fetchone()
        if prior is not None:
            prior_event_id = int(prior["event_id"])
            return {
                "event_id": prior_event_id,
                "work_id": clean_work_id,
                "request_kind": "flag_repair",
                "negative_verdict_event_id": negative_event_id,
                "remediation_event_ids": sorted(remediation_ids),
                "superseded_event_ids": [],
                "replayed": True,
            }

        t = db_now(conn)
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,'agent',?,?,?,?,?)",
            (
                t,
                "audit_request",
                clean_actor,
                clean_session_id,
                clean_target,
                clean_work_id,
                f"Flag repair review request for {clean_work_id}",
                clean_why[:_BODY_CAP],
                json.dumps(clean_refs),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                key,
            ),
        )
        event_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO request_consumption("
            "recipient_lane,work_id,request_event_id) VALUES (?,?,?)",
            (opposite_lane, clean_work_id, event_id),
        )
        superseded = _supersede_active_handoff_heads_unlocked(
            conn,
            work_id=clean_work_id,
            by_event_id=event_id,
            prior_kinds={"handoff", "audit_request"},
        )
        return {
            "event_id": event_id,
            "work_id": clean_work_id,
            "request_kind": "flag_repair",
            "negative_verdict_event_id": negative_event_id,
            "remediation_event_ids": sorted(remediation_ids),
            "superseded_event_ids": superseded,
            "replayed": False,
        }


def post_event(
    conn,
    *,
    kind: str,
    actor: str | None = None,
    session_id: str | None = None,
    to_selector: str | None = None,
    work_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    severity: str | None = None,
    verdict: str | None = None,
    trust: str = "agent",
    title: str | None = None,
    body: str | None = None,
    refs_json: str = "[]",
    payload_json: str = "{}",
    idempotency_key: str | None = None,
) -> int | None:
    normalized_kind = str(kind or "").strip().lower()
    if str(idempotency_key or "").startswith("flag-repair-audit-request:"):
        raise ValueError(
            "public post_event cannot mint the reserved flag-repair request "
            "namespace; use the typed request_audit writer"
        )
    if str(idempotency_key or "").startswith("typed-handoff-canary-rollback:"):
        raise ValueError(
            "public post_event cannot mint the reserved typed canary rollback "
            "namespace; use the dedicated controller writer"
        )
    if str(idempotency_key or "").startswith("operator-ok:"):
        # Reserved for the same reason as the two above. The kind refusal below
        # already stops a forged sign-off, but an unreserved key namespace lets
        # any writer squat an operation id and turn the operator's next real
        # sign-off into a replay collision -- a denial of the escape hatch
        # rather than a forgery of it, and equally not something a caller
        # should be able to do.
        raise ValueError(
            "public post_event cannot mint the reserved operator sign-off "
            "namespace; use the typed human-only writer"
        )
    if str(idempotency_key or "").startswith("operator-reassignment:"):
        raise ValueError(
            "public post_event cannot mint the reserved operator reassignment "
            "namespace; use the typed resident-controller writer"
        )
    if normalized_kind == "operator_ok":
        raise ValueError(
            "public post_event cannot mint operator_ok; use the typed human-only writer"
        )
    if actor == "operator" and trust == "operator":
        trust = "external"
    if trust == "system":
        raise ValueError(
            "public post_event cannot select system trust; use a typed controller writer"
        )
    if trust not in {"agent", "external"}:
        raise ValueError(f"unsupported public event trust {trust!r}")
    if body and len(body) > _BODY_CAP:
        body = body[
            :_BODY_CAP
        ]
    with tx(conn):
        event_payload: dict[str, Any] | None = None
        if normalized_kind in {"handoff", "audit_request"}:
            try:
                event_payload = json.loads(payload_json or "{}")
            except (TypeError, json.JSONDecodeError):
                event_payload = {}
            if isinstance(event_payload, dict) and "sla_s" not in event_payload:
                event_payload["sla_s"] = 1800 if normalized_kind == "audit_request" else 3600
        work = None
        if normalized_kind in {"handoff", "audit_request"} and work_id:
            work = conn.execute(
                "SELECT * FROM work_items WHERE work_id=?", (work_id,)
            ).fetchone()
        if isinstance(event_payload, dict):
            event_payload = compact_event_payload_same_as_row(
                event_payload, dict(work) if work is not None else None
            )
            payload_json = json.dumps(
                event_payload, sort_keys=True, separators=(",", ":")
            )
        if str(kind or "").strip().lower() == "audit_request":
            if not str(work_id or "").strip():
                raise ValueError(
                    "audit_request requires a work_id and is T0-only; see "
                    "docs/review-tiers.md"
                )
            if work is None:
                raise ValueError(
                    f"audit_request work_id not found: {work_id}; review requests are "
                    "events on an existing T0 author row"
                )
            from .review_tier import effective_review_tier

            work_dict = dict(work)
            tier = effective_review_tier(
                work_dict,
                tier_down_authorized=_tier_down_authorized_unlocked(
                    conn, str(work_id), work_dict
                ),
            )
            if tier != "T0":
                raise ValueError(
                    f"audit_request rejected for effective tier {tier}; T2/T1 work "
                    "self-verifies and may be included in one batched evidence review. "
                    "See docs/review-tiers.md"
                )
        t = db_now(conn)
        try:
            cur = conn.execute(
                "INSERT INTO events(ts, kind, actor, session_id, to_selector, work_id, run_id,"
                " thread_id, severity, verdict, trust, title, body, refs_json, payload_json,"
                " idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t,
                    kind,
                    actor,
                    session_id,
                    to_selector,
                    work_id,
                    run_id,
                    thread_id,
                    severity,
                    verdict,
                    trust,
                    title,
                    body,
                    refs_json,
                    payload_json,
                    idempotency_key,
                ),
            )
            event_id = int(cur.lastrowid)
            clean_actor = str(actor or "").strip().lower()
            clean_work_id = str(work_id or "").strip()
            if clean_actor in _lane_set() and clean_work_id:
                conn.execute(
                    "UPDATE request_consumption SET consumed_event_id=?,consumed_at=?"
                    " WHERE recipient_lane=? AND work_id=? AND consumed_at IS NULL"
                    " AND request_event_id<?",
                    (event_id, t, clean_actor, clean_work_id, event_id),
                )
            clean_selector = str(to_selector or "").strip().lower()
            clean_kind = str(kind or "").strip().lower()
            if (
                clean_kind in {"handoff", "audit_request"}
                and clean_selector in {f"actor:{lane}" for lane in _lane_set()}
                and clean_work_id
            ):
                recipient_lane = clean_selector.split(":", 1)[1]
                if clean_actor != recipient_lane:
                    conn.execute(
                        "INSERT OR IGNORE INTO request_consumption("
                        "recipient_lane,work_id,request_event_id)"
                        " VALUES (?,?,?)",
                        (recipient_lane, clean_work_id, event_id),
                    )
            if clean_kind == "audit_request" and clean_work_id:
                _supersede_active_handoff_heads_unlocked(
                    conn,
                    work_id=clean_work_id,
                    by_event_id=event_id,
                    prior_kinds={"handoff", "audit_request"},
                )
            return event_id
        except sqlite3.IntegrityError:
            return None


def post_recurring_launch_license_revoked(
    conn,
    *,
    work_id: str,
    job_id: str,
) -> dict[str, Any]:
    clean_work_id = str(work_id or "").strip()
    clean_job_id = str(job_id or "").strip()
    if not clean_work_id:
        raise ValueError("recurring launch revocation event requires work_id")
    if not clean_job_id:
        raise ValueError("recurring launch revocation event requires job_id")

    with tx(conn):
        row = conn.execute(
            "SELECT intent_state,rubric_verdict,archived_at,updated_at,version"
            " FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        if row is None:
            return {"emitted": False, "event_id": None, "reason": "work_not_found"}

        intent_state = str(row["intent_state"] or "").strip().lower()
        rubric_verdict = str(row["rubric_verdict"] or "").strip().lower()
        if row["archived_at"] is not None:
            return {"emitted": False, "event_id": None, "reason": "work_archived"}
        if intent_state not in {"done", "complete", "completed", "success"}:
            return {
                "emitted": False,
                "event_id": None,
                "reason": "not_successful_terminal_provenance",
            }
        if rubric_verdict == "pass":
            return {"emitted": False, "event_id": None, "reason": "license_active"}

        epoch = {
            "work_id": clean_work_id,
            "job_id": clean_job_id,
            "intent_state": intent_state,
            "rubric_verdict": rubric_verdict or "missing",
            "work_version": int(row["version"] or 0),
            "work_updated_at": float(row["updated_at"] or 0.0),
        }
        epoch_sha256 = hashlib.sha256(
            json.dumps(epoch, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        idempotency_key = f"recurring-launch-license-revoked:{epoch_sha256}"
        prior = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            return {
                "emitted": False,
                "event_id": int(prior["event_id"]),
                "reason": "already_emitted",
                "idempotency_key": idempotency_key,
            }

        observed_at = db_now(conn)
        payload = {
            "schema": "coordharness.recurring-launch-license-revoked.v1",
            "observed_at": observed_at,
            **epoch,
            "revocation_epoch_sha256": epoch_sha256,
            "launch_refused": True,
            "no_lifecycle_mutation": True,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,work_id,severity,trust,title,body,"
            "payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                observed_at,
                "recurring_launch_license_revoked",
                "local_job",
                clean_work_id,
                "error",
                "agent",
                f"Recurring launch license revoked: {clean_job_id}",
                "Launch refused because successful terminal provenance no longer retains PASS.",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        return {
            "emitted": True,
            "event_id": int(cur.lastrowid),
            "reason": "license_revoked",
            "idempotency_key": idempotency_key,
        }


def post_typed_controller_source_event(
    conn,
    *,
    work_id: str,
    controller_actor: str,
    controller_session_id: str,
    claim_id: str,
    claim_fence: str,
    event_type: str,
    to_selector: str,
    request: dict[str, Any],
    operation_id: str,
) -> int:
    r5_work_id = os.environ.get("COORD_TYPED_CONTROLLER_WORK_ID", "")
    if work_id != r5_work_id:
        raise ValueError("typed controller writer is bound to the exact R5 work_id")
    if event_type not in {"grant", "consume"}:
        raise ValueError("typed controller event_type must be grant|consume")
    if not isinstance(request, dict):
        raise ValueError("typed controller request must be an object")
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", operation_id
    ):
        raise ValueError("typed controller operation_id invalid")
    with tx(conn):
        now = db_now(conn)
        holder = conn.execute(
            "SELECT c.work_id,c.session_id,c.claim_id,c.lease_token,c.version,c.status,c.expires_at,s.actor "
            "FROM claims c JOIN agent_sessions s ON s.session_id=c.session_id "
            "WHERE c.claim_id=?",
            (claim_id,),
        ).fetchone()
        if holder is None:
            raise ValueError("typed controller claim absent")
        if (
            holder["work_id"] != work_id
            or holder["session_id"] != controller_session_id
            or holder["lease_token"] != claim_fence
            or holder["actor"] != controller_actor
            or holder["status"] != "running"
            or float(holder["expires_at"]) <= now
        ):
            raise ValueError(
                "typed controller requires exact live work/session/claim/fence"
            )
        if to_selector != f"actor:{controller_actor}":
            raise ValueError("typed controller target selector drift")
        identity = {
            "actor": controller_actor,
            "session_id": controller_session_id,
            "claim_id": claim_id,
            "claim_fence": claim_fence,
            "claim_version": int(holder["version"]),
            "work_id": work_id,
        }
        if event_type == "grant":
            required = {
                "schema_version",
                "status",
                "nonce",
                "tracked_identity",
                "source_evidence",
                "telemetry_evidence",
            }
            if (
                set(request) != required
                or request["schema_version"] != "d4-r12-controller-event.r5.grant"
                or request["status"] != "GRANTED_ONCE"
            ):
                raise ValueError("typed controller grant schema drift")
            if not isinstance(request["nonce"], str) or not re.fullmatch(
                r"[a-f0-9]{64}", request["nonce"]
            ):
                raise ValueError("typed controller grant nonce invalid")
            if not all(
                isinstance(request[key], dict) and request[key]
                for key in ("tracked_identity", "source_evidence", "telemetry_evidence")
            ):
                raise ValueError("typed controller grant evidence absent")
            payload = {
                "schema_version": "d4-r12-controller-event.r5",
                "status": "SYSTEM_CONTROLLER_GRANT",
                "writer": "coord_db.post_typed_controller_source_event.v1",
                "work_id": work_id,
                "actor": controller_actor,
                "session_id": controller_session_id,
                "to_selector": to_selector,
                "controller_identity": identity,
                "request": request,
                "request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            kind = "controller_source_grant"
        else:
            required = {"schema_version", "status", "grant_event_id", "nonce"}
            if (
                set(request) != required
                or request["schema_version"] != "d4-r12-controller-event.r5.consume"
                or request["status"] != "CONSUME_ONCE"
                or not isinstance(request["grant_event_id"], int)
            ):
                raise ValueError("typed controller consumption schema drift")
            grant = conn.execute(
                "SELECT kind,trust,work_id,actor,session_id,to_selector,payload_json FROM events WHERE event_id=?",
                (request["grant_event_id"],),
            ).fetchone()
            if (
                grant is None
                or grant["kind"] != "controller_source_grant"
                or grant["trust"] != "system"
                or grant["work_id"] != work_id
                or grant["actor"] != controller_actor
                or grant["session_id"] != controller_session_id
                or grant["to_selector"] != to_selector
            ):
                raise ValueError("typed controller grant provenance drift")
            grant_payload = json.loads(grant["payload_json"])
            if (
                grant_payload.get("controller_identity") != identity
                or grant_payload.get("request", {}).get("nonce") != request["nonce"]
            ):
                raise ValueError("typed controller grant identity or nonce drift")
            replay = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='controller_source_consumption' AND trust='system' AND json_extract(payload_json,'$.grant_event_id')=?",
                (request["grant_event_id"],),
            ).fetchone()[0]
            if replay:
                raise ValueError("typed controller grant already consumed")
            payload = {
                "schema_version": "d4-r12-controller-event.r5",
                "status": "SYSTEM_CONTROLLER_CONSUMPTION",
                "writer": "coord_db.post_typed_controller_source_event.v1",
                "work_id": work_id,
                "actor": controller_actor,
                "session_id": controller_session_id,
                "to_selector": to_selector,
                "controller_identity": identity,
                "grant_event_id": request["grant_event_id"],
                "nonce": request["nonce"],
                "tracked_identity": grant_payload["request"]["tracked_identity"],
                "source_evidence": grant_payload["request"]["source_evidence"],
                "telemetry_evidence": grant_payload["request"]["telemetry_evidence"],
            }
            kind = "controller_source_consumption"
        key = f"typed-controller:{event_type}:{work_id}:{operation_id}"
        event_id = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                now,
                kind,
                controller_actor,
                controller_session_id,
                to_selector,
                work_id,
                "system",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                key,
            ),
        ).lastrowid
        return int(event_id)


def _audit_verdict_evidence_refs(refs_json: str) -> list[str]:

    try:
        raw_refs = json.loads(refs_json or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("audit verdict requires refs_json to be a JSON list") from exc
    if not isinstance(raw_refs, list):
        raise ValueError("audit verdict requires refs_json to be a JSON list")
    refs = [
        str(ref).strip()
        for ref in raw_refs
        if isinstance(ref, str) and ref.strip()
    ]
    if not refs:
        raise ValueError("audit verdict requires at least one evidence ref")
    return refs


def _latest_claim_author_lane_unlocked(
    conn: sqlite3.Connection, work_id: str
) -> str | None:

    claim = conn.execute(
        "SELECT c.session_id,s.actor FROM claims c"
        " LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
        " WHERE c.work_id=? ORDER BY c.acquired_at DESC,c.claim_id DESC LIMIT 1",
        (work_id,),
    ).fetchone()
    if claim is not None:
        session_lane = expected_actor_for_session_id(str(claim["session_id"] or ""))
        registered_lane = str(claim["actor"] or "").strip().lower()
        if registered_lane not in _lane_set():
            registered_lane = ""
        if session_lane and registered_lane and session_lane != registered_lane:
            return None
        return session_lane or registered_lane or None

    claim_kinds = tuple(f"{lane}_claim" for lane in _configured_lanes())
    event = conn.execute(
        "SELECT kind,actor,session_id,payload_json FROM events"
        " WHERE work_id=? AND kind IN "
        f"({','.join('?' for _ in claim_kinds)})"
        " ORDER BY event_id DESC LIMIT 1",
        (work_id, *claim_kinds),
    ).fetchone()
    if event is not None:
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if str(payload.get("status") or "").strip().lower() != "applied":
            return None
        kind_lane = str(event["kind"] or "").split("_", 1)[0].strip().lower()
        event_lane = str(event["actor"] or "").strip().lower()
        session_lane = expected_actor_for_session_id(str(event["session_id"] or ""))
        lanes = {lane for lane in (kind_lane, event_lane, session_lane) if lane}
        if len(lanes) != 1:
            return None
        registered_lane = lanes.pop()
        if registered_lane not in _lane_set():
            return None
        return registered_lane

    legacy_events = conn.execute(
        "SELECT kind,actor,session_id FROM events"
        " WHERE work_id=? AND kind IN ('handoff','audit_request')"
        " ORDER BY event_id",
        (work_id,),
    ).fetchall()
    if {str(row["kind"] or "") for row in legacy_events} != {
        "handoff",
        "audit_request",
    }:
        return None
    work = conn.execute(
        "SELECT assignee FROM work_items WHERE work_id=?",
        (work_id,),
    ).fetchone()
    assignee_lane = str(work["assignee"] or "").strip().lower() if work else ""
    lanes = {assignee_lane} if assignee_lane else set()
    for row in legacy_events:
        event_lane = str(row["actor"] or "").strip().lower()
        session_lane = expected_actor_for_session_id(str(row["session_id"] or ""))
        if not event_lane or not session_lane:
            return None
        lanes.update((event_lane, session_lane))
    if len(lanes) != 1:
        return None
    registered_lane = lanes.pop()
    return registered_lane if registered_lane in _lane_set() else None


def request_completion_review_if_needed(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
) -> dict[str, Any] | None:

    claim_id = str(claim_id or "").strip()
    if not claim_id:
        raise ValueError("completion review request requires claim_id")
    with tx(conn):
        row = conn.execute(
            "SELECT c.work_id,c.session_id,c.status,s.actor,w.done_signal,"
            " w.acceptance_json,w.rubric_verdict,w.completion_requested_at,"
            " w.title,w.tier,w.kind,w.module,w.sublane,w.context_pack_ref,"
            " w.depends_on,w.assignee,w.version,w.operator_ok_event_id,"
            " w.tier_correction_event_id"
            " FROM claims c JOIN work_items w ON w.work_id=c.work_id"
            " LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
            " WHERE c.claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"completion review request claim not found: {claim_id}")
        acceptance_raw = str(row["acceptance_json"] or "").strip()
        review_state = completion_review_state(
            conn,
            str(row["work_id"]),
            row=row,
        )
        if review_state["negative_verdict"]:
            raise ValueError(
                f"completion review request blocked by negative verdict "
                f"{review_state['rubric_verdict']!r}"
            )
        if not review_state["needs_review"]:
            return None
        if str(row["status"] or "").strip().lower() != "running":
            raise ValueError(
                f"completion review request requires running claim, got {row['status']!r}"
            )
        author_lane = expected_actor_for_session_id(str(row["session_id"] or ""))
        registered_lane = str(row["actor"] or "").strip().lower()
        if registered_lane not in _lane_set():
            registered_lane = ""
        if author_lane and registered_lane and author_lane != registered_lane:
            raise ValueError("completion review request has contradictory author identity")
        author_lane = author_lane or registered_lane or None
        if author_lane not in _lane_set():
            raise ValueError("completion review request requires an unambiguous author lane")
        review_lane = _counterpart_lane(author_lane)
        if review_lane is None:
            raise ValueError(
                "completion review request requires a second configured lane; "
                f"COORD_LANES names only {_lanes_display()}"
            )
        key = f"completion-review:{claim_id}"
        prior = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if prior is not None:
            _supersede_active_handoff_heads_unlocked(
                conn,
                work_id=str(row["work_id"]),
                by_event_id=int(prior["event_id"]),
                prior_kinds={"handoff", "audit_request"},
            )
            return {
                "status": "awaiting_review",
                "event_id": int(prior["event_id"]),
                "work_id": str(row["work_id"]),
                "claim_id": claim_id,
                "author_lane": author_lane,
                "review_lane": review_lane,
                "replayed": True,
            }
        try:
            acceptance_echo = json.loads(acceptance_raw or "[]")
        except json.JSONDecodeError:
            acceptance_echo = acceptance_raw
        artifacts = [
            {"path": str(a["path"]), "kind": str(a["kind"] or "")}
            for a in conn.execute(
                "SELECT path,kind FROM artifacts WHERE work_id=?"
                " ORDER BY created_at DESC,artifact_id DESC LIMIT 20",
                (row["work_id"],),
            ).fetchall()
        ]
        event_refs: list[str] = []
        for event_row in conn.execute(
            "SELECT refs_json FROM events WHERE work_id=? AND refs_json IS NOT NULL"
            " ORDER BY event_id DESC LIMIT 20",
            (row["work_id"],),
        ).fetchall():
            try:
                refs = json.loads(event_row["refs_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for ref in refs if isinstance(refs, list) else []:
                clean = str(ref or "").strip()
                if clean and clean not in event_refs:
                    event_refs.append(clean)
                if len(event_refs) >= 32:
                    break
            if len(event_refs) >= 32:
                break
        done_signal = str(row["done_signal"] or "").strip()
        evidence_refs = ([done_signal] if done_signal else []) + event_refs
        evidence_manifest = {
            "done_signal": done_signal or None,
            "done_signal_exists": bool(
                done_signal and done_signal_satisfied(conn, done_signal, HARNESS_ROOT)
            ),
            "artifacts": artifacts,
            "event_refs": event_refs,
        }
        t = db_now(conn)
        payload = {
            "schema_version": 1,
            "sla_s": 1800,
            "request_kind": "completion_review",
            "task": (
                f"Independently review T0 completion for {row['work_id']}: "
                f"{str(row['title'] or row['work_id']).strip()}"
            ),
            "why": (
                "The T0 effect tier requires an opposite-lane verdict before "
                "the author claim may complete."
            ),
            "acceptance": acceptance_raw,
            "constraints": [
                "Post the verdict on the existing author work row; do not create a sibling review row.",
                "Same-lane PASS remains forbidden.",
            ],
            "claim_id": claim_id,
            "work_id": str(row["work_id"]),
            "author_lane": author_lane,
            "review_lane": review_lane,
            "effective_tier": review_state["effective_tier"],
            "tier_reasons": review_state["tier_reasons"],
            "acceptance_echo": acceptance_echo,
            "evidence_manifest": evidence_manifest,
        }
        conn.execute(
            "UPDATE work_items SET blocked_reason_class='awaiting_review',"
            " completion_requested_at=?,updated_at=?,version=version+1 WHERE work_id=?",
            (t, t, row["work_id"]),
        )
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,'agent',?,?,?,?,?)",
            (
                t,
                "audit_request",
                author_lane,
                row["session_id"],
                f"actor:{review_lane}",
                row["work_id"],
                f"Review completion request for {row['work_id']}",
                "T0 effect tier requires an opposite-lane verdict before completion.",
                json.dumps(evidence_refs[:32]),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                key,
            ),
        )
        event_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO request_consumption("
            "recipient_lane,work_id,request_event_id) VALUES (?,?,?)",
            (review_lane, str(row["work_id"]), event_id),
        )
        superseded_event_ids = _supersede_active_handoff_heads_unlocked(
            conn,
            work_id=str(row["work_id"]),
            by_event_id=event_id,
            prior_kinds={"handoff", "audit_request"},
        )
        return {
            "status": "awaiting_review",
            "event_id": event_id,
            "work_id": str(row["work_id"]),
            "claim_id": claim_id,
            "author_lane": author_lane,
            "review_lane": review_lane,
            "completion_requested_at": t,
            "evidence_manifest": evidence_manifest,
            "superseded_event_ids": superseded_event_ids,
            "replayed": False,
        }


def post_audit_verdict(
    conn,
    *,
    work_id: str,
    verdict: str,
    actor: str | None = None,
    session_id: str | None = None,
    to_selector: str | None = None,
    severity: str | None = None,
    trust: str = "agent",
    title: str | None = None,
    body: str | None = None,
    refs_json: str = "[]",
    payload_json: str = "{}",
    operation_id: str,
    request_sha256: str,
) -> dict[str, Any]:
    normalized = str(verdict or "").strip().upper()
    if normalized not in {"PASS", "FLAG", "BLOCKED"}:
        raise ValueError(f"audit verdict must be PASS|FLAG|BLOCKED, got {verdict!r}")
    work_id = str(work_id or "").strip()
    if not work_id:
        raise ValueError("audit verdict requires work_id")
    if body and len(body) > _BODY_CAP:
        body = body[:_BODY_CAP]
    operation_id = str(operation_id or "").strip()
    request_sha256 = str(request_sha256 or "").strip().lower()
    if not operation_id:
        raise ValueError("audit verdict requires operation_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", operation_id):
        raise ValueError(
            "audit verdict operation_id must be 8-200 safe identifier characters"
        )
    if not re.fullmatch(r"[a-f0-9]{64}", request_sha256):
        raise ValueError("audit verdict requires a canonical request_sha256")
    evidence_refs = _audit_verdict_evidence_refs(refs_json)
    idempotency_key = f"mcp-audit-verdict:{actor}:{operation_id}"

    with tx(conn):
        existing = conn.execute(
            "SELECT intent_state FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"audit verdict work_id not found: {work_id}")
        author_lane = _latest_claim_author_lane_unlocked(conn, work_id)
        effective_to_selector = (
            f"actor:{author_lane}"
            if author_lane in _lane_set()
            else to_selector
        )
        if normalized == "PASS":
            declared_actor = str(actor or "").strip().lower()
            session_lane = expected_actor_for_session_id(str(session_id or ""))
            if declared_actor not in _lane_set() or (
                session_lane is not None and declared_actor != session_lane
            ):
                raise ValueError(
                    "PASS audit verdict requires an unambiguous reviewer lane"
                )
            verdict_lane = declared_actor
            if author_lane is None:
                raise ValueError(
                    "PASS audit verdict requires unambiguous authoring claim history"
                )
            if verdict_lane == author_lane:
                raise ValueError(
                    f"same-lane PASS is forbidden: reviewer and author are {author_lane}"
                )
            review_state = completion_review_state(conn, work_id)
            if review_state.get("flag_repair_cycle") and review_state.get(
                "review_reopened"
            ):
                request_event_id = int(
                    review_state["latest_audit_request_event_id"]
                )
                request_row = conn.execute(
                    "SELECT payload_json FROM events WHERE event_id=? AND work_id=?"
                    " AND kind='audit_request'",
                    (request_event_id, work_id),
                ).fetchone()
                try:
                    request_payload = json.loads(
                        str(request_row["payload_json"] or "{}")
                        if request_row is not None
                        else "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    request_payload = {}
                negative_event_id = (
                    request_payload.get("negative_verdict_event_id")
                    if isinstance(request_payload, dict)
                    else None
                )
                if (
                    isinstance(negative_event_id, bool)
                    or not isinstance(negative_event_id, int)
                    or negative_event_id <= 0
                ):
                    raise ValueError(
                        "flag_repair PASS requires an exact negative verdict binding"
                    )
                negative_row = conn.execute(
                    "SELECT verdict,refs_json FROM events WHERE event_id=?"
                    " AND work_id=? AND kind='audit_verdict'",
                    (negative_event_id, work_id),
                ).fetchone()
                if (
                    negative_row is None
                    or str(negative_row["verdict"] or "").strip().upper()
                    not in _FLAG_REPAIR_NEGATIVE_VERDICTS
                ):
                    raise ValueError(
                        "flag_repair PASS negative verdict binding is invalid"
                    )
                negative_refs = _audit_verdict_evidence_refs(
                    str(negative_row["refs_json"] or "[]")
                )
                if not set(evidence_refs).difference(negative_refs):
                    raise ValueError(
                        "flag_repair PASS requires at least one new evidence ref "
                        "not carried by the negative verdict"
                    )
        prior = conn.execute(
            "SELECT event_id, work_id, verdict, payload_json FROM events"
            " WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            try:
                prior_payload = json.loads(prior["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                prior_payload = {}
            if (
                str(prior["work_id"] or "") != work_id
                or str(prior["verdict"] or "").upper() != normalized
                or str(prior_payload.get("operation_request_sha256") or "").lower()
                != request_sha256
            ):
                raise ValueError(
                    f"audit verdict operation_id {operation_id!r} was already used for a different request"
                )
            post = conn.execute(
                "SELECT intent_state, rubric_verdict, version, updated_at"
                " FROM work_items WHERE work_id=?",
                (work_id,),
            ).fetchone()
            return {
                "event_id": int(prior["event_id"]),
                "work_id": work_id,
                "verdict": normalized,
                "work": dict(post),
                "operation_id": operation_id,
                "request_sha256": request_sha256,
                "replayed": True,
            }
        t = db_now(conn)
        cur = conn.execute(
            "INSERT INTO events(ts, kind, actor, session_id, to_selector, work_id,"
            " severity, verdict, trust, title, body, refs_json, payload_json, idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "audit_verdict",
                actor,
                session_id,
                effective_to_selector,
                work_id,
                severity,
                normalized,
                trust,
                title,
                body,
                refs_json,
                payload_json,
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)
        superseded_event_ids = _supersede_active_handoff_heads_unlocked(
            conn,
            work_id=work_id,
            by_event_id=event_id,
            prior_kinds={"audit_request"},
        )
        if normalized == "PASS":
            conn.execute(
                "UPDATE work_items SET rubric_verdict='pass',"
                " blocked_reason_class=CASE WHEN blocked_reason_class='awaiting_review'"
                " THEN NULL ELSE blocked_reason_class END, updated_at=?,"
                " version=version+1 WHERE work_id=?",
                (t, work_id),
            )
        else:
            intent_state = "blocked" if normalized == "BLOCKED" else "queued"
            conn.execute(
                "UPDATE work_items SET rubric_verdict=?, intent_state=?, updated_at=?,"
                " version=version+1 WHERE work_id=?",
                (normalized.lower(), intent_state, t, work_id),
            )
        post = conn.execute(
            "SELECT intent_state, rubric_verdict, version, updated_at"
            " FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
    return {
        "event_id": event_id,
        "work_id": work_id,
        "verdict": normalized,
        "work": dict(post),
        "operation_id": operation_id,
        "request_sha256": request_sha256,
        "superseded_event_ids": superseded_event_ids,
        "replayed": False,
    }


def _strict_json_mapping(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _strict_json_list(raw: Any) -> list[Any] | None:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def _strict_positive_event_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _strict_event_reference(value: Any) -> int | None:
    exact = _strict_positive_event_id(value)
    if exact is not None:
        return exact
    if isinstance(value, str) and value == value.strip() and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


_LEGACY_G3_CANARY_SELF_TOMBSTONES = frozenset(
    {
        (
            64901,
            64902,
            "g3-live-canary-1783883088361304000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "a08f4f6e96edcc71bdbb9a0fb323705cd6da264fd3bbc1c776055219701e5b25",
        ),
        (
            64989,
            64990,
            "g3-live-canary-1783886466636480000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "baafc69855763f1cf3d55bc955aa081eeca7b8fd5f8c471c12b03a67dd67694e",
        ),
        (
            65295,
            65296,
            "g3-live-canary-1783889933278186000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "4c8a4940efb57f6010fb24c5e1d53b04608891da3ef865765b4f4affcfe2fe2d",
        ),
        (
            65297,
            65298,
            "g3-live-canary-1783889954173065000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "9b61ce81001f97405b32e2840b5af2bfa3b7d2d91211beab09622bdb16222343",
        ),
        (
            65857,
            65858,
            "g3-live-canary-1783910309154423000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "f6e57c1951ae1eddfb470124c4a38a34d5a5b7cba9dd061567effe0b71502fd9",
        ),
        (
            66011,
            66012,
            "g3-live-canary-1783914915930075000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "9ddf95489c8d1ffd39b0fe684597fd3777b0ab5c702e28a3436e4e7e649fc89e",
        ),
        (
            66087,
            66088,
            "g3-live-canary-1783915915683554000",
            "9f352175a04059d8e34ca00a344096d1ff0954f074aa51b7d7de2b6c74c1dd06",
            "221d49732ddcdba58575a7e5ba108c7ca9d39d3fc5151a92048869c3af073a9c",
        ),
    }
)


def _legacy_g3_canary_self_tombstone_is_valid_unlocked(
    *,
    row: sqlite3.Row,
    payload: dict[str, Any] | None,
    prior_id: int | None,
    by_event_id: int | None,
    successor: sqlite3.Row | None,
    successor_payload: dict[str, Any] | None,
) -> bool:

    if prior_id is None or prior_id != by_event_id or successor is None:
        return False
    tombstone_event_id = int(row["event_id"])
    allowlisted = next(
        (
            receipt
            for receipt in _LEGACY_G3_CANARY_SELF_TOMBSTONES
            if receipt[0] == prior_id and receipt[1] == tombstone_event_id
        ),
        None,
    )
    if allowlisted is None:
        return False
    _, _, expected_operation_id, expected_preimage_sha256, expected_request_sha256 = (
        allowlisted
    )
    operation_id = (
        successor_payload.get("operation_id")
        if successor_payload is not None
        else None
    )
    operation_receipt = (
        successor_payload.get("operation_receipt")
        if successor_payload is not None
        else None
    )
    preconditions = (
        successor_payload.get("preconditions")
        if successor_payload is not None
        else None
    )
    return bool(
        tombstone_event_id > prior_id
        and str(row["work_id"] or "") == "CANARY-CDX-G3-LIVE-HANDOFF"
        and str(row["trust"] or "") == "system"
        and str(row["actor"] or "") == "codex"
        and str(row["session_id"] or "") == "codex:thread:g3-live-canary"
        and str(row["to_selector"] or "") == "actor:codex"
        and str(row["idempotency_key"] or "")
        == f"typed-handoff-supersede:{prior_id}:{prior_id}"
        and payload is not None
        and set(payload)
        == {
            "schema_version",
            "supersedes",
            "by_event_id",
            "reason",
            "preimage_slice_sha256",
        }
        and payload.get("schema_version") == 1
        and payload.get("reason")
        == "G3 controlled live canary exact lifecycle rollback"
        and payload.get("preimage_slice_sha256") == expected_preimage_sha256
        and _strict_json_list(row["refs_json"]) == [f"event:{prior_id}"]
        and str(successor["kind"] or "") == "handoff"
        and str(successor["work_id"] or "") == str(row["work_id"] or "")
        and str(successor["trust"] or "") == "agent"
        and str(successor["actor"] or "") == "codex"
        and str(successor["session_id"] or "") == "codex:thread:g3-live-canary"
        and str(successor["to_selector"] or "") == "actor:claude"
        and operation_id == expected_operation_id
        and str(successor["idempotency_key"] or "")
        == f"typed-handoff:codex:{operation_id}"
        and _strict_json_list(successor["refs_json"])
        == ["artifact://g3-live-canary"]
        and successor_payload is not None
        and successor_payload.get("schema_version") == 2
        and successor_payload.get("writer_contract")
        == "existing_work_handoff.v2"
        and successor_payload.get("task")
        == "G3 controlled existing non-product live writer canary"
        and successor_payload.get("why")
        == "Prove current-build profile-attested MCP handoff and exact rollback."
        and successor_payload.get("acceptance")
        == "One immutable handoff event, exact replay, then lifecycle/head restoration."
        and successor_payload.get("constraints")
        == ["existing non-product row", "exact rollback required"]
        and successor_payload.get("refs") == ["artifact://g3-live-canary"]
        and successor_payload.get("done_signal_source")
        == "existing_coord_work_row"
        and preconditions
        == {
            "expected_version": 0,
            "expected_assignee": "codex",
            "expected_head_event_ids": [],
        }
        and isinstance(operation_receipt, dict)
        and successor_payload.get("operation_request_sha256")
        == expected_request_sha256
        and operation_receipt.get("request_sha256") == expected_request_sha256
        and operation_receipt.get("superseded_event_ids") == []
    )


def _typed_handoff_canary_rollback_request(
    *,
    work_id: str,
    canary_event_id: int,
    rollback_event_id: int,
    operation_id: str,
    operation_request_sha256: str,
    preimage_slice_sha256: str,
    canary_event_payload_sha256: str,
    canary_work_postimage_sha256: str,
) -> tuple[dict[str, Any], str]:

    clean_work_id = str(work_id or "").strip()
    clean_operation_id = str(operation_id or "").strip()
    exact_canary_event_id = _strict_positive_event_id(canary_event_id)
    exact_rollback_event_id = _strict_positive_event_id(rollback_event_id)
    hashes = {
        "operation_request_sha256": str(operation_request_sha256 or "").strip(),
        "preimage_slice_sha256": str(preimage_slice_sha256 or "").strip(),
        "canary_event_payload_sha256": str(
            canary_event_payload_sha256 or ""
        ).strip(),
        "canary_work_postimage_sha256": str(
            canary_work_postimage_sha256 or ""
        ).strip(),
    }
    if not clean_work_id:
        raise ValueError("typed canary rollback requires work_id")
    if (
        exact_canary_event_id is None
        or exact_rollback_event_id is None
        or exact_rollback_event_id <= exact_canary_event_id
    ):
        raise ValueError("typed canary rollback requires ordered exact event ids")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", clean_operation_id):
        raise ValueError("typed canary rollback requires a safe operation_id")
    if any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in hashes.values()):
        raise ValueError("typed canary rollback requires exact SHA-256 bindings")
    request = {
        "schema_version": 1,
        "writer_contract": "typed_handoff_canary_rollback.v1",
        "work_id": clean_work_id,
        "canary_event_id": exact_canary_event_id,
        "rollback_event_id": exact_rollback_event_id,
        "expected_active_head_event_ids": [exact_canary_event_id],
        "operation_id": clean_operation_id,
        **hashes,
        "actor": "codex",
        "session_id": "codex:thread:g3-live-canary",
        "to_selector": "actor:codex",
        "trust": "system",
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return request, request_sha256


def _typed_handoff_canary_rollback_is_valid_unlocked(
    *,
    row: sqlite3.Row,
    payload: dict[str, Any] | None,
    prior_id: int | None,
    by_event_id: int | None,
    canary: sqlite3.Row | None,
    canary_payload: dict[str, Any] | None,
) -> bool:

    if (
        payload is None
        or canary is None
        or prior_id is None
        or by_event_id != int(row["event_id"])
        or by_event_id <= prior_id
    ):
        return False
    operation_receipt = (
        canary_payload.get("operation_receipt")
        if canary_payload is not None
        else None
    )
    if not isinstance(operation_receipt, dict):
        return False
    try:
        request, request_sha256 = _typed_handoff_canary_rollback_request(
            work_id=str(row["work_id"] or ""),
            canary_event_id=prior_id,
            rollback_event_id=int(row["event_id"]),
            operation_id=str(payload.get("operation_id") or ""),
            operation_request_sha256=str(
                payload.get("operation_request_sha256") or ""
            ),
            preimage_slice_sha256=str(payload.get("preimage_slice_sha256") or ""),
            canary_event_payload_sha256=str(
                payload.get("canary_event_payload_sha256") or ""
            ),
            canary_work_postimage_sha256=str(
                payload.get("canary_work_postimage_sha256") or ""
            ),
        )
    except ValueError:
        return False
    expected_receipt = {
        "schema_version": 1,
        "writer_contract": "typed_handoff_canary_rollback_receipt.v1",
        "rollback_request_sha256": request_sha256,
        "work_id": request["work_id"],
        "canary_event_id": prior_id,
        "rollback_event_id": int(row["event_id"]),
        "expected_active_head_event_ids": [prior_id],
        "operation_id": request["operation_id"],
        "operation_request_sha256": request["operation_request_sha256"],
        "preimage_slice_sha256": request["preimage_slice_sha256"],
        "canary_event_payload_sha256": request["canary_event_payload_sha256"],
        "canary_work_postimage_sha256": request[
            "canary_work_postimage_sha256"
        ],
    }
    expected_payload_keys = {
        "schema_version",
        "writer_contract",
        "supersedes",
        "by_event_id",
        "expected_active_head_event_ids",
        "operation_id",
        "operation_request_sha256",
        "preimage_slice_sha256",
        "canary_event_payload_sha256",
        "canary_work_postimage_sha256",
        "rollback_request_sha256",
        "rollback_receipt",
    }
    canary_payload_sha256 = hashlib.sha256(
        str(canary["payload_json"] or "").encode("utf-8")
    ).hexdigest()
    return bool(
        set(payload) == expected_payload_keys
        and payload.get("schema_version") == 1
        and payload.get("writer_contract") == "typed_handoff_canary_rollback.v1"
        and payload.get("supersedes") == prior_id
        and payload.get("by_event_id") == int(row["event_id"])
        and payload.get("expected_active_head_event_ids") == [prior_id]
        and payload.get("rollback_request_sha256") == request_sha256
        and payload.get("rollback_receipt") == expected_receipt
        and str(row["kind"] or "") == "handoff_superseded"
        and str(row["actor"] or "") == request["actor"]
        and str(row["session_id"] or "") == request["session_id"]
        and str(row["to_selector"] or "") == request["to_selector"]
        and str(row["trust"] or "") == request["trust"]
        and str(row["idempotency_key"] or "")
        == f"typed-handoff-canary-rollback:{request['operation_id']}:{request_sha256}"
        and _strict_json_list(row["refs_json"]) == [f"event:{prior_id}"]
        and str(canary["kind"] or "") == "handoff"
        and str(canary["work_id"] or "") == request["work_id"]
        and str(canary["actor"] or "") == "codex"
        and str(canary["session_id"] or "")
        == "codex:thread:g3-live-canary"
        and str(canary["to_selector"] or "") == "actor:claude"
        and str(canary["trust"] or "") == "agent"
        and canary_payload is not None
        and canary_payload.get("schema_version") == 2
        and canary_payload.get("writer_contract")
        == "existing_work_handoff.v2"
        and canary_payload.get("operation_id") == request["operation_id"]
        and canary_payload.get("operation_request_sha256")
        == request["operation_request_sha256"]
        and operation_receipt.get("request_sha256")
        == request["operation_request_sha256"]
        and operation_receipt.get("work_postimage_sha256")
        == request["canary_work_postimage_sha256"]
        and canary_payload_sha256 == request["canary_event_payload_sha256"]
    )


def _typed_handoff_request(
    *,
    work_id: str,
    actor: str,
    session_id: str,
    owner_lane: str,
    target_intent: str,
    task: str,
    why: str,
    acceptance: str,
    refs: Iterable[str],
    constraints: Iterable[str],
    operation_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_head_event_ids: Iterable[int],
) -> tuple[dict[str, Any], str]:

    request = {
        "schema_version": 2,
        "work_id": str(work_id or "").strip(),
        "actor": str(actor or "").strip().lower(),
        "session_id": str(session_id or "").strip(),
        "owner_lane": str(owner_lane or "").strip().lower(),
        "target_intent": str(target_intent or "").strip().lower(),
        "task": str(task or "").strip(),
        "why": str(why or "").strip(),
        "acceptance": str(acceptance or "").strip(),
        "refs": [str(value).strip() for value in refs if str(value).strip()],
        "constraints": [
            str(value).strip() for value in constraints if str(value).strip()
        ],
        "operation_id": str(operation_id or "").strip(),
        "expected_version": expected_version,
        "expected_assignee": str(expected_assignee or "").strip().lower(),
        "expected_head_event_ids": list(expected_head_event_ids),
    }
    if not request["work_id"]:
        raise ValueError("typed handoff requires work_id")
    if len(request["work_id"].encode("utf-8")) > 256:
        raise ValueError("typed handoff work_id exceeds 256 UTF-8 bytes")
    if request["actor"] not in _lane_set() or request["owner_lane"] not in _lane_set():
        raise ValueError(f"typed handoff actor/owner_lane must be {_lanes_display()}")
    if request["actor"] == request["owner_lane"]:
        raise ValueError("typed handoff owner_lane must differ from actor")
    if not request["session_id"]:
        raise ValueError("typed handoff requires a process-bound session_id")
    _validate_session_actor(request["session_id"], request["actor"])
    if expected_actor_for_session_id(request["session_id"]) != request["actor"]:
        raise ValueError(
            "typed handoff requires an actor-namespaced process session_id"
        )
    if request["target_intent"] not in {"queued", "blocked"}:
        raise ValueError("typed handoff target_intent must be queued|blocked")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", request["operation_id"]):
        raise ValueError(
            "typed handoff operation_id must be 8-200 safe identifier characters"
        )
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError(
            "typed handoff expected_version must be a non-negative exact integer"
        )
    if request["expected_assignee"] != request["actor"]:
        raise ValueError("typed handoff expected_assignee must equal the caller actor")
    heads = request["expected_head_event_ids"]
    if any(_strict_positive_event_id(value) is None for value in heads):
        raise ValueError(
            "typed handoff expected_head_event_ids must contain exact positive integers"
        )
    if heads != sorted(set(heads)):
        raise ValueError(
            "typed handoff expected_head_event_ids must be sorted and unique"
        )
    if len(heads) > 64:
        raise ValueError("typed handoff expected_head_event_ids is bounded to 64 heads")
    limits = {
        "task": (request["task"], 1_000),
        "why": (request["why"], 2_048),
        "acceptance": (request["acceptance"], 4_096),
    }
    if any(not value for value, _cap in limits.values()):
        raise ValueError("typed handoff task, why, and acceptance must be non-empty")
    for name, (value, cap) in limits.items():
        if len(value.encode("utf-8")) > cap:
            raise ValueError(f"typed handoff {name} exceeds {cap} UTF-8 bytes")
    if len(request["refs"]) > 32 or any(
        len(value.encode("utf-8")) > 2_048 for value in request["refs"]
    ):
        raise ValueError("typed handoff refs are bounded to 32 pointers of 2048 bytes")
    if not request["refs"]:
        raise ValueError("typed handoff requires at least one pointer ref")
    if sum(len(value.encode("utf-8")) for value in request["refs"]) > 4_096:
        raise ValueError("typed handoff refs are bounded to 4096 aggregate UTF-8 bytes")
    if len(request["constraints"]) > 16 or any(
        len(value.encode("utf-8")) > 512 for value in request["constraints"]
    ):
        raise ValueError(
            "typed handoff constraints are bounded to 16 entries of 512 bytes"
        )
    if not request["constraints"]:
        raise ValueError("typed handoff requires at least one explicit constraint")
    if sum(len(value.encode("utf-8")) for value in request["constraints"]) > 2_048:
        raise ValueError(
            "typed handoff constraints are bounded to 2048 aggregate UTF-8 bytes"
        )
    canonical_request = json.dumps(
        request, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(canonical_request) > 12_000:
        raise ValueError("typed handoff canonical request exceeds 12000 UTF-8 bytes")
    request_sha256 = hashlib.sha256(canonical_request).hexdigest()
    return request, request_sha256


def _operator_reassignment_request(
    *,
    work_id: str,
    owner_lane: str,
    task: str,
    why: str,
    acceptance: str,
    refs: Iterable[str],
    constraints: Iterable[str],
    operation_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_head_event_ids: Iterable[int],
    release_held_claim: bool = False,
    target_intent: str = "queued",
) -> tuple[dict[str, Any], str]:
    """Validate and hash the operator-only assignment-transfer request.

    This deliberately does not route through ``_typed_handoff_request``.  The
    agent contract proves that the caller is the current assignee and therefore
    requires ``actor == expected_assignee``.  An operator transition has a
    different authority predicate, but shares the same bounded capsule and CAS
    shapes without relaxing that agent-only rule.
    """

    try:
        clean_refs = [str(value).strip() for value in refs if str(value).strip()]
        clean_constraints = [
            str(value).strip() for value in constraints if str(value).strip()
        ]
        clean_heads = list(expected_head_event_ids)
    except TypeError as exc:
        raise ValueError(
            "operator reassignment refs, constraints, and assignment heads must be lists"
        ) from exc
    request = {
        "schema_version": 1,
        "writer_contract": "operator_reassignment_request.v1",
        "authority_channel": OPERATOR_AUTHORITY_CHANNEL,
        "work_id": str(work_id or "").strip(),
        "owner_lane": str(owner_lane or "").strip().lower(),
        "target_intent": str(target_intent or "").strip().lower(),
        "task": str(task or "").strip(),
        "why": str(why or "").strip(),
        "acceptance": str(acceptance or "").strip(),
        "refs": clean_refs,
        "constraints": clean_constraints,
        "operation_id": str(operation_id or "").strip(),
        "expected_version": expected_version,
        "expected_assignee": str(expected_assignee or "").strip().lower(),
        "expected_head_event_ids": clean_heads,
        "release_held_claim": release_held_claim,
    }
    if not request["work_id"]:
        raise ValueError("operator reassignment requires work_id")
    if len(request["work_id"].encode("utf-8")) > 256:
        raise ValueError("operator reassignment work_id exceeds 256 UTF-8 bytes")
    if request["owner_lane"] not in _lane_set():
        raise ValueError(
            f"operator reassignment owner_lane must be {_lanes_display()}"
        )
    if request["expected_assignee"] not in _lane_set():
        raise ValueError(
            f"operator reassignment expected_assignee must be {_lanes_display()}"
        )
    if request["owner_lane"] == request["expected_assignee"]:
        raise ValueError(
            "operator reassignment owner_lane must differ from expected_assignee"
        )
    if request["target_intent"] not in {"queued", "blocked"}:
        raise ValueError("operator reassignment target_intent must be queued|blocked")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", request["operation_id"]
    ):
        raise ValueError(
            "operator reassignment operation_id must be 8-200 safe identifier characters"
        )
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError(
            "operator reassignment expected_version must be a non-negative exact integer"
        )
    if any(_strict_positive_event_id(value) is None for value in clean_heads):
        raise ValueError(
            "operator reassignment expected_head_event_ids must contain exact positive integers"
        )
    if clean_heads != sorted(set(clean_heads)):
        raise ValueError(
            "operator reassignment expected_head_event_ids must be sorted and unique"
        )
    if len(clean_heads) > 64:
        raise ValueError(
            "operator reassignment expected_head_event_ids is bounded to 64 heads"
        )
    if not isinstance(release_held_claim, bool):
        raise ValueError(
            "operator reassignment release_held_claim must be an explicit boolean"
        )
    if release_held_claim:
        raise ValueError(
            "operator reassignment cannot release a live claim; stop or release "
            "the current holder first"
        )
    limits = {
        "task": (request["task"], 1_000),
        "why": (request["why"], 2_048),
        "acceptance": (request["acceptance"], 4_096),
    }
    if any(not value for value, _cap in limits.values()):
        raise ValueError(
            "operator reassignment task, why, and acceptance must be non-empty"
        )
    for name, (value, cap) in limits.items():
        if len(value.encode("utf-8")) > cap:
            raise ValueError(
                f"operator reassignment {name} exceeds {cap} UTF-8 bytes"
            )
    if len(clean_refs) > 32 or any(
        len(value.encode("utf-8")) > 2_048 for value in clean_refs
    ):
        raise ValueError(
            "operator reassignment refs are bounded to 32 pointers of 2048 bytes"
        )
    if not clean_refs:
        raise ValueError("operator reassignment requires at least one pointer ref")
    if sum(len(value.encode("utf-8")) for value in clean_refs) > 4_096:
        raise ValueError(
            "operator reassignment refs are bounded to 4096 aggregate UTF-8 bytes"
        )
    if len(clean_constraints) > 16 or any(
        len(value.encode("utf-8")) > 512 for value in clean_constraints
    ):
        raise ValueError(
            "operator reassignment constraints are bounded to 16 entries of 512 bytes"
        )
    if not clean_constraints:
        raise ValueError(
            "operator reassignment requires at least one explicit constraint"
        )
    if sum(len(value.encode("utf-8")) for value in clean_constraints) > 2_048:
        raise ValueError(
            "operator reassignment constraints are bounded to 2048 aggregate UTF-8 bytes"
        )
    canonical_request = json.dumps(
        request, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(canonical_request) > 12_000:
        raise ValueError(
            "operator reassignment canonical request exceeds 12000 UTF-8 bytes"
        )
    return request, hashlib.sha256(canonical_request).hexdigest()


def _typed_handoff_capsule_warnings(request: dict[str, Any]) -> list[str]:
    from .creation_lint import handoff_capsule_warnings

    return handoff_capsule_warnings(
        {
            "task": request.get("task"),
            "why": request.get("why"),
            "acceptance": request.get("acceptance"),
            "constraints": request.get("constraints"),
            "refs": request.get("refs"),
        }
    )


def _policy_moot_request_retirement_is_valid_unlocked(
    *,
    row: sqlite3.Row,
    payload: dict[str, Any] | None,
    prior_id: int | None,
    by_event_id: int | None,
    prior: sqlite3.Row | None,
    successor: sqlite3.Row | None,
    successor_payload: dict[str, Any] | None,
) -> bool:

    if (
        payload is None
        or prior is None
        or successor is None
        or successor_payload is None
        or prior_id is None
        or by_event_id is None
        or prior_id >= by_event_id
    ):
        return False
    request_keys = {
        "schema_version",
        "writer_contract",
        "work_id",
        "expected_version",
        "expected_assignee",
        "expected_intent_state",
        "expected_last_event_id",
        "expected_last_event_at",
        "skip_if_events_after",
        "packet_sha256",
    }
    request = {key: successor_payload.get(key) for key in request_keys}
    request_sha256 = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    retired = successor_payload.get("retired_audit_request_event_ids")
    retired_ids = (
        [_strict_positive_event_id(event_id) for event_id in retired]
        if isinstance(retired, list)
        else []
    )
    valid_retired = bool(
        isinstance(retired, list)
        and all(event_id is not None for event_id in retired_ids)
        and retired == sorted(set(retired_ids))
    )
    return bool(
        payload.get("schema_version") == 1
        and payload.get("supersedes") == prior_id
        and payload.get("by_event_id") == by_event_id
        and valid_retired
        and prior_id in retired_ids
        and str(prior["kind"] or "") == "audit_request"
        and str(successor["kind"] or "") == "board_hygiene_policy_moot_closed"
        and str(successor["trust"] or "") == "system"
        and successor_payload.get("schema_version") == 1
        and successor_payload.get("writer_contract")
        == "board_hygiene_policy_moot_close.v1"
        and successor_payload.get("operation_request_sha256") == request_sha256
        and successor_payload.get("closed_to_intent_state") == "closed"
        and successor_payload.get("no_claim_mutation") is True
        and successor_payload.get("no_rubric_mutation") is True
        and successor_payload.get("no_artifact_mutation") is True
        and str(successor["idempotency_key"] or "")
        == (
            "board-hygiene-policy-moot:"
            f"{successor_payload.get('packet_sha256')}:{successor_payload.get('work_id')}"
        )
        and str(row["idempotency_key"] or "")
        == f"policy-moot-supersede:{by_event_id}:{prior_id}"
        and str(row["actor"] or "") == str(successor["actor"] or "")
        and str(row["session_id"] or "") == str(successor["session_id"] or "")
        and str(row["to_selector"] or "") == str(successor["to_selector"] or "")
        and str(row["trust"] or "") == "system"
        and _strict_json_list(row["refs_json"]) == [f"event:{prior_id}"]
    )


_AUDIT_REQUEST_RETIREMENT_REASONS = {
    "answered_by_legacy_verdict",
    "withdrawn_by_requester_note",
    "terminal_work_state",
}


def _audit_request_retirement_is_valid_unlocked(
    *,
    row: sqlite3.Row,
    payload: dict[str, Any] | None,
    prior_id: int | None,
    by_event_id: int | None,
    prior: sqlite3.Row | None,
    successor: sqlite3.Row | None,
    successor_payload: dict[str, Any] | None,
) -> bool:

    if (
        payload is None
        or prior is None
        or successor is None
        or successor_payload is None
        or prior_id is None
        or by_event_id is None
        or prior_id >= by_event_id
    ):
        return False
    operation_id = str(successor_payload.get("operation_id") or "")
    manifest_sha256 = str(successor_payload.get("manifest_sha256") or "")
    operation_request_sha256 = str(
        successor_payload.get("operation_request_sha256") or ""
    )
    reason_class = str(successor_payload.get("reason_class") or "")
    return bool(
        payload.get("schema_version") == 1
        and payload.get("writer_contract")
        == "audit_request_retirement_supersession.v1"
        and payload.get("supersedes") == prior_id
        and payload.get("by_event_id") == by_event_id
        and payload.get("operation_id") == operation_id
        and payload.get("manifest_sha256") == manifest_sha256
        and payload.get("operation_request_sha256") == operation_request_sha256
        and str(prior["kind"] or "") == "audit_request"
        and str(successor["kind"] or "") == "audit_request_retired"
        and str(successor["work_id"] or "") == str(prior["work_id"] or "")
        and str(successor["trust"] or "") == "system"
        and successor_payload.get("schema_version") == 1
        and successor_payload.get("writer_contract")
        == "audit_request_zombie_retirement.v1"
        and successor_payload.get("request_event_id") == prior_id
        and reason_class in _AUDIT_REQUEST_RETIREMENT_REASONS
        and re.fullmatch(r"[a-f0-9]{64}", manifest_sha256) is not None
        and re.fullmatch(r"[a-f0-9]{64}", operation_request_sha256) is not None
        and str(successor["idempotency_key"] or "")
        == f"audit-request-retired:{operation_id}:{prior_id}"
        and str(row["idempotency_key"] or "")
        == f"audit-request-retirement-supersede:{by_event_id}:{prior_id}"
        and str(row["actor"] or "") == str(successor["actor"] or "")
        and str(row["session_id"] or "") == str(successor["session_id"] or "")
        and str(row["to_selector"] or "") == str(successor["to_selector"] or "")
        and str(row["trust"] or "") == "system"
        and _strict_json_list(row["refs_json"])
        == [f"event:{prior_id}", f"event:{by_event_id}"]
    )


def _typed_handoff_head_state_unlocked(conn, work_id: str) -> dict[str, Any]:

    rows = conn.execute(
        "SELECT event_id,kind,actor,session_id,to_selector,work_id,verdict,"
        " trust,refs_json,payload_json,idempotency_key"
        " FROM events WHERE work_id=? AND kind IN"
        " ('handoff','audit_request','audit_verdict','handoff_superseded',"
        " 'board_hygiene_policy_moot_closed','audit_request_retired')"
        " ORDER BY event_id",
        (work_id,),
    ).fetchall()
    by_id = {int(row["event_id"]): row for row in rows}
    candidates: set[int] = set()
    malformed: list[int] = []
    for row in rows:
        if row["kind"] not in {"handoff", "audit_request"}:
            continue
        payload = _strict_json_mapping(row["payload_json"])
        schema = payload.get("schema_version") if payload else None
        if isinstance(schema, bool) or not isinstance(schema, int) or schema <= 0:
            malformed.append(int(row["event_id"]))
            continue
        candidates.add(int(row["event_id"]))

    superseded_by: dict[int, int] = {}
    for row in rows:
        if row["kind"] != "handoff_superseded":
            continue
        payload = _strict_json_mapping(row["payload_json"])
        prior_id = _strict_event_reference(
            payload.get("supersedes") if payload else None
        )
        by_event_id = _strict_event_reference(
            payload.get("by_event_id") if payload else None
        )
        schema = payload.get("schema_version") if payload else None
        successor = by_id.get(by_event_id or -1)
        expected_key = (
            f"typed-handoff-supersede:{by_event_id}:{prior_id}"
            if prior_id and by_event_id
            else ""
        )
        successor_payload = (
            _strict_json_mapping(successor["payload_json"]) if successor else None
        )
        canary = by_id.get(prior_id or -1)
        canary_payload = (
            _strict_json_mapping(canary["payload_json"]) if canary else None
        )
        legacy_canary_self_tombstone = (
            _legacy_g3_canary_self_tombstone_is_valid_unlocked(
                row=row,
                payload=payload,
                prior_id=prior_id,
                by_event_id=by_event_id,
                successor=successor,
                successor_payload=successor_payload,
            )
        )
        typed_canary_rollback = _typed_handoff_canary_rollback_is_valid_unlocked(
            row=row,
            payload=payload,
            prior_id=prior_id,
            by_event_id=by_event_id,
            canary=canary,
            canary_payload=canary_payload,
        )
        typed_trusted = bool(
            schema == 1
            and prior_id in candidates
            and by_event_id in candidates
            and (prior_id < by_event_id or legacy_canary_self_tombstone)
            and successor is not None
            and successor["kind"] == "handoff"
            and str(row["idempotency_key"] or "") == expected_key
            and str(successor["idempotency_key"] or "").startswith("typed-handoff:")
            and successor_payload
            and successor_payload.get("writer_contract") == "existing_work_handoff.v2"
            and str(row["actor"] or "") == str(successor["actor"] or "")
            and str(row["session_id"] or "") == str(successor["session_id"] or "")
        )
        operator_trusted = bool(
            schema == 1
            and prior_id in candidates
            and by_event_id in candidates
            and prior_id < by_event_id
            and successor is not None
            and successor["kind"] == "handoff"
            and successor_payload
            and successor_payload.get("schema_version") == 1
            and successor_payload.get("writer_contract")
            == OPERATOR_REASSIGNMENT_WRITER_CONTRACT
            and successor_payload.get("authority_channel")
            == OPERATOR_AUTHORITY_CHANNEL
            and str(successor["actor"] or "") == "operator"
            and not str(successor["session_id"] or "")
            and str(successor["trust"] or "") == "system"
            and str(successor["idempotency_key"] or "").startswith(
                "operator-reassignment:"
            )
            and str(row["idempotency_key"] or "")
            == f"operator-reassignment-supersede:{by_event_id}:{prior_id}"
            and payload.get("writer_contract")
            == "operator_reassignment_supersession.v1"
            and str(row["actor"] or "") == "operator"
            and not str(row["session_id"] or "")
            and str(row["trust"] or "") == "system"
            and str(row["to_selector"] or "")
            == str(successor["to_selector"] or "")
        )
        successor_schema = (
            successor_payload.get("schema_version") if successor_payload else None
        )
        legacy_trusted = bool(
            schema == 1
            and prior_id in candidates
            and by_event_id in candidates
            and prior_id < by_event_id
            and successor is not None
            and successor["kind"] in {"handoff", "audit_request"}
            and isinstance(successor_schema, int)
            and not isinstance(successor_schema, bool)
            and successor_schema > 0
            and (
                successor_payload is None
                or successor_payload.get("writer_contract")
                != OPERATOR_REASSIGNMENT_WRITER_CONTRACT
            )
            and str(row["actor"] or "") == str(successor["actor"] or "")
            and str(row["session_id"] or "") == str(successor["session_id"] or "")
            and str(row["to_selector"] or "") == str(successor["to_selector"] or "")
        )
        verdict_trusted = bool(
            schema == 1
            and prior_id in candidates
            and by_event_id is not None
            and prior_id < by_event_id
            and successor is not None
            and by_id[prior_id]["kind"] == "audit_request"
            and successor["kind"] == "audit_verdict"
            and str(successor["verdict"] or "").upper()
            in {"PASS", "FLAG", "BLOCKED"}
            and str(successor["idempotency_key"] or "").startswith(
                "mcp-audit-verdict:"
            )
            and str(row["idempotency_key"] or "")
            == f"audit-verdict-supersede:{by_event_id}:{prior_id}"
            and str(row["actor"] or "") == str(successor["actor"] or "")
            and str(row["session_id"] or "") == str(successor["session_id"] or "")
            and str(row["to_selector"] or "") == str(successor["to_selector"] or "")
        )
        policy_moot_trusted = _policy_moot_request_retirement_is_valid_unlocked(
            row=row,
            payload=payload,
            prior_id=prior_id,
            by_event_id=by_event_id,
            prior=by_id.get(prior_id or -1),
            successor=successor,
            successor_payload=successor_payload,
        )
        audit_request_retirement_trusted = (
            _audit_request_retirement_is_valid_unlocked(
                row=row,
                payload=payload,
                prior_id=prior_id,
                by_event_id=by_event_id,
                prior=by_id.get(prior_id or -1),
                successor=successor,
                successor_payload=successor_payload,
            )
        )
        if not (
            typed_trusted
            or operator_trusted
            or legacy_trusted
            or verdict_trusted
            or policy_moot_trusted
            or audit_request_retirement_trusted
            or typed_canary_rollback
        ):
            malformed.append(int(row["event_id"]))
            continue
        superseded_by[prior_id] = max(by_event_id, superseded_by.get(prior_id, 0))
    active = sorted(candidates - set(superseded_by))
    return {
        "active_event_ids": active,
        "superseded_by": superseded_by,
        "quarantined_event_ids": sorted(set(malformed)),
    }


def _supersede_active_handoff_heads_unlocked(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    by_event_id: int,
    prior_kinds: set[str],
) -> list[int]:

    successor = conn.execute(
        "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust"
        " FROM events WHERE event_id=?",
        (int(by_event_id),),
    ).fetchone()
    if successor is None or str(successor["work_id"] or "") != str(work_id):
        raise ValueError(
            f"handoff supersession successor {by_event_id} is not bound to {work_id}"
        )
    successor_kind = str(successor["kind"] or "")
    if successor_kind not in {
        "audit_request",
        "audit_verdict",
        "board_hygiene_policy_moot_closed",
    }:
        raise ValueError(
            "automatic handoff supersession requires audit_request, audit_verdict, "
            "or typed policy-moot successor"
        )

    head_state = _typed_handoff_head_state_unlocked(conn, work_id)
    active_ids = [
        int(event_id)
        for event_id in head_state["active_event_ids"]
        if int(event_id) < int(by_event_id)
    ]
    if not active_ids:
        return []
    placeholders = ",".join("?" for _ in active_ids)
    active_rows = conn.execute(
        f"SELECT event_id,kind FROM events WHERE event_id IN ({placeholders})",
        active_ids,
    ).fetchall()
    prefix = {
        "audit_verdict": "audit-verdict-supersede",
        "audit_request": "audit-request-supersede",
        "board_hygiene_policy_moot_closed": "policy-moot-supersede",
    }[successor_kind]
    t = db_now(conn)
    superseded: list[int] = []
    for prior in active_rows:
        prior_id = int(prior["event_id"])
        if str(prior["kind"] or "") not in prior_kinds:
            continue
        key = f"{prefix}:{int(by_event_id)}:{prior_id}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(ts,kind,actor,session_id,to_selector,"
            " work_id,trust,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "handoff_superseded",
                successor["actor"],
                successor["session_id"],
                successor["to_selector"],
                work_id,
                successor["trust"] or "agent",
                json.dumps([f"event:{prior_id}"]),
                json.dumps(
                    {
                        "schema_version": 1,
                        "supersedes": prior_id,
                        "by_event_id": int(by_event_id),
                        "reason": (
                            "audit verdict resolved active audit request"
                            if successor_kind == "audit_verdict"
                            else (
                                "typed policy-moot close retired active audit request"
                                if successor_kind
                                == "board_hygiene_policy_moot_closed"
                                else "completion review request replaced prior active head"
                            )
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                key,
            ),
        )
        if cur.rowcount:
            superseded.append(prior_id)
    return sorted(superseded)


def reconcile_audit_verdict_request_heads(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    verdict_event_id: int,
) -> dict[str, Any]:

    with tx(conn):
        superseded = _supersede_active_handoff_heads_unlocked(
            conn,
            work_id=str(work_id or "").strip(),
            by_event_id=int(verdict_event_id),
            prior_kinds={"audit_request"},
        )
        state = _typed_handoff_head_state_unlocked(conn, str(work_id or "").strip())
    return {
        "work_id": str(work_id or "").strip(),
        "verdict_event_id": int(verdict_event_id),
        "superseded_audit_request_event_ids": superseded,
        "active_event_ids": state["active_event_ids"],
        "quarantined_event_ids": state["quarantined_event_ids"],
    }


def _canonical_mapping_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _open_audit_request_rows_unlocked(
    conn: sqlite3.Connection, *, recipient_lane: str
) -> list[dict[str, Any]]:
    clean_lane = str(recipient_lane or "").strip().lower()
    rows = conn.execute(
        "SELECT rc.request_event_id AS event_id,e.work_id"
        " FROM request_consumption rc"
        " JOIN events e ON e.event_id=rc.request_event_id"
        " WHERE rc.recipient_lane=? AND rc.consumed_at IS NULL"
        " AND e.kind='audit_request' ORDER BY rc.request_event_id",
        (clean_lane,),
    ).fetchall()
    open_rows: list[dict[str, Any]] = []
    for row in rows:
        state = _typed_handoff_head_state_unlocked(conn, str(row["work_id"] or ""))
        if int(row["event_id"]) not in state["superseded_by"]:
            open_rows.append(
                {"event_id": int(row["event_id"]), "work_id": str(row["work_id"])}
            )
    return open_rows


def _audit_request_open_snapshot_unlocked(conn: sqlite3.Connection) -> dict[str, Any]:
    lanes: dict[str, Any] = {}
    for lane in _configured_lanes():
        rows = _open_audit_request_rows_unlocked(conn, recipient_lane=lane)
        lanes[lane] = {
            "open_audit_request_count": len(rows),
            "open_audit_request_set_sha256": _canonical_mapping_sha256(rows),
            "open_audit_requests": rows,
        }
    return lanes


def _retirement_protected_state_sha256(
    conn: sqlite3.Connection, work_ids: Iterable[str]
) -> str:
    ids = sorted({str(work_id) for work_id in work_ids if str(work_id)})
    if not ids:
        return _canonical_mapping_sha256({"work_items": [], "claims": [], "artifacts": []})
    placeholders = ",".join("?" for _ in ids)
    state: dict[str, Any] = {}
    for table, order in (
        ("work_items", "work_id"),
        ("claims", "claim_id"),
        ("artifacts", "artifact_id"),
    ):
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE work_id IN ({placeholders}) ORDER BY {order}",
            ids,
        ).fetchall()
        state[table] = [dict(row) for row in rows]
    return _canonical_mapping_sha256(state)


def build_audit_request_retirement_manifest(
    conn: sqlite3.Connection,
    *,
    requests: Iterable[dict[str, Any]],
) -> dict[str, Any]:

    specifications = list(requests)
    if not specifications:
        raise ValueError("audit-request retirement manifest requires entries")
    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    terminal_states = set(TERMINAL_WORK_STATES) | {"completed"}
    for specification in specifications:
        request_id = _strict_positive_event_id(specification.get("request_event_id"))
        reason_class = str(specification.get("reason_class") or "").strip()
        evidence_id = specification.get("evidence_event_id")
        if evidence_id is not None:
            evidence_id = _strict_positive_event_id(evidence_id)
        if request_id is None or request_id in seen:
            raise ValueError("audit-request retirement IDs must be positive and unique")
        if reason_class not in _AUDIT_REQUEST_RETIREMENT_REASONS:
            raise ValueError(f"unsupported audit-request retirement reason {reason_class!r}")
        seen.add(request_id)
        request_row = conn.execute(
            "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
            " refs_json,payload_json,idempotency_key FROM events WHERE event_id=?",
            (request_id,),
        ).fetchone()
        if request_row is None or str(request_row["kind"] or "") != "audit_request":
            raise ValueError(f"request event {request_id} is not an audit_request")
        work_id = str(request_row["work_id"] or "")
        payload = _strict_json_mapping(request_row["payload_json"])
        if not work_id or payload is None or _strict_positive_event_id(
            payload.get("schema_version")
        ) is None:
            raise ValueError(f"request event {request_id} is not structured")
        work_row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        if work_row is None:
            raise ValueError(f"request event {request_id} has no work row")
        consumption = conn.execute(
            "SELECT recipient_lane,work_id,request_event_id,consumed_event_id,consumed_at"
            " FROM request_consumption WHERE request_event_id=?",
            (request_id,),
        ).fetchone()
        if consumption is None or consumption["consumed_at"] is not None:
            raise ValueError(f"request event {request_id} is not unconsumed")
        state = _typed_handoff_head_state_unlocked(conn, work_id)
        if request_id not in state["active_event_ids"]:
            raise ValueError(f"request event {request_id} is not an active trusted head")

        evidence: dict[str, Any] | None = None
        if reason_class == "answered_by_legacy_verdict":
            if evidence_id is None:
                raise ValueError("legacy-verdict retirement requires evidence_event_id")
            evidence_row = conn.execute(
                "SELECT * FROM events WHERE event_id=? AND work_id=? AND event_id>?",
                (evidence_id, work_id, request_id),
            ).fetchone()
            if (
                evidence_row is None
                or str(evidence_row["kind"] or "") != "audit_verdict"
                or str(evidence_row["verdict"] or "").upper()
                not in {"PASS", "FLAG", "BLOCKED"}
            ):
                raise ValueError(f"request event {request_id} lacks its legacy verdict")
            evidence = dict(evidence_row)
        elif reason_class == "withdrawn_by_requester_note":
            if evidence_id is None:
                raise ValueError("note withdrawal requires evidence_event_id")
            evidence_row = conn.execute(
                "SELECT * FROM events WHERE event_id=? AND work_id=? AND event_id>?",
                (evidence_id, work_id, request_id),
            ).fetchone()
            note_text = " ".join(
                str(evidence_row[key] or "") if evidence_row is not None else ""
                for key in ("title", "body")
            ).lower()
            if (
                evidence_row is None
                or str(evidence_row["kind"] or "") != "note"
                or str(evidence_row["actor"] or "")
                != str(request_row["actor"] or "")
                or "class-superseded" not in note_text
            ):
                raise ValueError(f"request event {request_id} lacks its requester note")
            evidence = dict(evidence_row)
        else:
            work_state = str(work_row["intent_state"] or "").strip().lower()
            if work_state not in terminal_states and work_row["archived_at"] is None:
                raise ValueError(f"request event {request_id} work row is not terminal")
            if evidence_id is not None:
                raise ValueError("terminal-state retirement does not accept an evidence event")

        entry = {
            "request_event_id": request_id,
            "work_id": work_id,
            "recipient_lane": str(consumption["recipient_lane"]),
            "reason_class": reason_class,
            "evidence_event_id": evidence_id,
            "request_event_sha256": _canonical_mapping_sha256(dict(request_row)),
            "evidence_event_sha256": (
                _canonical_mapping_sha256(evidence) if evidence is not None else None
            ),
            "work_row_sha256": _canonical_mapping_sha256(dict(work_row)),
            "expected_work_version": int(work_row["version"] or 0),
            "expected_work_state": str(work_row["intent_state"] or ""),
            "expected_work_archived_at": work_row["archived_at"],
            "expected_active_head_event_ids": state["active_event_ids"],
            "request_consumption_sha256": _canonical_mapping_sha256(dict(consumption)),
        }
        entries.append(entry)
    entries.sort(key=lambda entry: int(entry["request_event_id"]))
    before = _audit_request_open_snapshot_unlocked(conn)
    open_ids = {
        int(row["event_id"])
        for lane in before.values()
        for row in lane["open_audit_requests"]
    }
    if seen - open_ids:
        raise ValueError(
            f"retirement manifest contains non-open request IDs {sorted(seen - open_ids)}"
        )
    protected = {
        lane: [
            row
            for row in lane_state["open_audit_requests"]
            if int(row["event_id"]) not in seen
        ]
        for lane, lane_state in before.items()
    }
    reason_counts = {
        reason: sum(entry["reason_class"] == reason for entry in entries)
        for reason in sorted(_AUDIT_REQUEST_RETIREMENT_REASONS)
    }
    manifest = {
        "schema_version": 1,
        "writer_contract": "audit_request_zombie_retirement_manifest.v1",
        "entries": entries,
        "reason_counts": reason_counts,
        "before_open_audit_request_summary": before,
        "protected_open_audit_requests": protected,
        "protected_open_audit_request_set_sha256": {
            lane: _canonical_mapping_sha256(rows) for lane, rows in protected.items()
        },
    }
    manifest["manifest_sha256"] = _canonical_mapping_sha256(manifest)
    return manifest


def retire_zombie_audit_requests(
    conn: sqlite3.Connection,
    *,
    manifest: dict[str, Any],
    actor: str,
    session_id: str,
    operation_id: str,
) -> dict[str, Any]:

    clean_actor = str(actor or "").strip().lower()
    clean_session = str(session_id or "").strip()
    clean_operation = str(operation_id or "").strip()
    if clean_actor not in _lane_set():
        raise ValueError(f"audit-request retirement actor must be {_lanes_display()}")
    _validate_session_actor(clean_session, clean_actor)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", clean_operation):
        raise ValueError("audit-request retirement requires a safe operation_id")
    supplied_manifest = dict(manifest)
    manifest_sha256 = str(supplied_manifest.pop("manifest_sha256", ""))
    if manifest_sha256 != _canonical_mapping_sha256(supplied_manifest):
        raise ValueError("audit-request retirement manifest SHA-256 mismatch")
    entries = supplied_manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("audit-request retirement manifest has no entries")
    operation_request = {
        "schema_version": 1,
        "writer_contract": "audit_request_zombie_retirement.v1",
        "actor": clean_actor,
        "session_id": clean_session,
        "operation_id": clean_operation,
        "manifest_sha256": manifest_sha256,
    }
    operation_request_sha256 = _canonical_mapping_sha256(operation_request)
    request_ids = [int(entry["request_event_id"]) for entry in entries]
    work_ids = [str(entry["work_id"]) for entry in entries]

    with tx(conn):
        prior_rows = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE kind='audit_request_retired'"
            " AND idempotency_key LIKE ? ORDER BY event_id",
            (f"audit-request-retired:{clean_operation}:%",),
        ).fetchall()
        if prior_rows:
            if len(prior_rows) != len(entries):
                raise ValueError("audit-request retirement replay is incomplete")
            for prior in prior_rows:
                prior_payload = _strict_json_mapping(prior["payload_json"])
                if (
                    prior_payload is None
                    or prior_payload.get("manifest_sha256") != manifest_sha256
                    or prior_payload.get("operation_request_sha256")
                    != operation_request_sha256
                ):
                    raise ValueError("audit-request retirement replay identity mismatch")
            after = _audit_request_open_snapshot_unlocked(conn)
            return {
                "schema_version": 1,
                "writer_contract": "audit_request_zombie_retirement_receipt.v1",
                "operation_id": clean_operation,
                "operation_request_sha256": operation_request_sha256,
                "manifest_sha256": manifest_sha256,
                "retired_audit_request_event_ids": request_ids,
                "retirement_event_ids": [int(row["event_id"]) for row in prior_rows],
                "before_open_audit_request_summary": supplied_manifest[
                    "before_open_audit_request_summary"
                ],
                "after_open_audit_request_summary": after,
                "reason_counts": supplied_manifest["reason_counts"],
                "replayed": True,
            }

        request_specs = [
            {
                "request_event_id": entry["request_event_id"],
                "reason_class": entry["reason_class"],
                "evidence_event_id": entry.get("evidence_event_id"),
            }
            for entry in entries
        ]
        current_manifest = build_audit_request_retirement_manifest(
            conn, requests=request_specs
        )
        if current_manifest != manifest:
            raise ValueError("audit-request retirement manifest drifted before apply")
        protected_before = _retirement_protected_state_sha256(conn, work_ids)
        t = db_now(conn)
        retirement_event_ids: list[int] = []
        supersession_event_ids: list[int] = []
        for entry in entries:
            request_id = int(entry["request_event_id"])
            request_row = conn.execute(
                "SELECT actor FROM events WHERE event_id=?", (request_id,)
            ).fetchone()
            to_selector = f"actor:{str(request_row['actor'] or clean_actor)}"
            refs = [f"event:{request_id}"]
            if entry.get("evidence_event_id") is not None:
                refs.append(f"event:{int(entry['evidence_event_id'])}")
            payload = {
                "schema_version": 1,
                "writer_contract": "audit_request_zombie_retirement.v1",
                "operation_id": clean_operation,
                "operation_request_sha256": operation_request_sha256,
                "manifest_sha256": manifest_sha256,
                "request_event_id": request_id,
                "reason_class": entry["reason_class"],
                "evidence_event_id": entry.get("evidence_event_id"),
                "recipient_lane": entry["recipient_lane"],
                "request_event_sha256": entry["request_event_sha256"],
                "evidence_event_sha256": entry["evidence_event_sha256"],
                "work_row_sha256": entry["work_row_sha256"],
                "request_consumption_sha256": entry[
                    "request_consumption_sha256"
                ],
                "no_work_claim_rubric_artifact_mutation": True,
            }
            cur = conn.execute(
                "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
                " refs_json,payload_json,idempotency_key)"
                " VALUES (?,?,?,?,?,?,'system',?,?,?)",
                (
                    t,
                    "audit_request_retired",
                    clean_actor,
                    clean_session,
                    to_selector,
                    entry["work_id"],
                    json.dumps(refs, separators=(",", ":")),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    f"audit-request-retired:{clean_operation}:{request_id}",
                ),
            )
            retirement_event_id = int(cur.lastrowid)
            retirement_event_ids.append(retirement_event_id)
            supersession_payload = {
                "schema_version": 1,
                "writer_contract": "audit_request_retirement_supersession.v1",
                "supersedes": request_id,
                "by_event_id": retirement_event_id,
                "operation_id": clean_operation,
                "operation_request_sha256": operation_request_sha256,
                "manifest_sha256": manifest_sha256,
            }
            cur = conn.execute(
                "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
                " refs_json,payload_json,idempotency_key)"
                " VALUES (?,?,?,?,?,?,'system',?,?,?)",
                (
                    t,
                    "handoff_superseded",
                    clean_actor,
                    clean_session,
                    to_selector,
                    entry["work_id"],
                    json.dumps(
                        [f"event:{request_id}", f"event:{retirement_event_id}"],
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        supersession_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    (
                        "audit-request-retirement-supersede:"
                        f"{retirement_event_id}:{request_id}"
                    ),
                ),
            )
            supersession_event_ids.append(int(cur.lastrowid))
            updated = conn.execute(
                "UPDATE request_consumption SET consumed_event_id=?,consumed_at=?"
                " WHERE request_event_id=? AND recipient_lane=? AND consumed_at IS NULL",
                (retirement_event_id, t, request_id, entry["recipient_lane"]),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    f"request consumption CAS failed for request event {request_id}"
                )

        if _retirement_protected_state_sha256(conn, work_ids) != protected_before:
            raise ValueError("audit-request retirement mutated protected work state")
        for entry, retirement_event_id in zip(entries, retirement_event_ids, strict=True):
            state = _typed_handoff_head_state_unlocked(conn, str(entry["work_id"]))
            request_id = int(entry["request_event_id"])
            if (
                state["superseded_by"].get(request_id) != retirement_event_id
                or request_id in state["active_event_ids"]
            ):
                raise ValueError(
                    f"audit-request retirement trust readback failed for {request_id}"
                )
        after = _audit_request_open_snapshot_unlocked(conn)
        for lane in _configured_lanes():
            expected = supplied_manifest["protected_open_audit_requests"][lane]
            if after[lane]["open_audit_requests"] != expected:
                raise ValueError(
                    f"audit-request retirement changed protected {lane} requests"
                )
    return {
        "schema_version": 1,
        "writer_contract": "audit_request_zombie_retirement_receipt.v1",
        "operation_id": clean_operation,
        "operation_request_sha256": operation_request_sha256,
        "manifest_sha256": manifest_sha256,
        "retired_audit_request_event_ids": request_ids,
        "retirement_event_ids": retirement_event_ids,
        "supersession_event_ids": supersession_event_ids,
        "before_open_audit_request_summary": supplied_manifest[
            "before_open_audit_request_summary"
        ],
        "after_open_audit_request_summary": after,
        "reason_counts": supplied_manifest["reason_counts"],
        "protected_state_sha256": protected_before,
        "replayed": False,
    }


def _typed_handoff_event_id_summary(event_ids: Iterable[int]) -> str:

    values = [int(event_id) for event_id in event_ids]
    preview = values[:_TYPED_HANDOFF_EVENT_ID_PREVIEW_LIMIT]
    suffix = (
        f" total={len(values)} truncated=true"
        if len(values) > _TYPED_HANDOFF_EVENT_ID_PREVIEW_LIMIT
        else f" total={len(values)} truncated=false"
    )
    return f"{preview}{suffix}"


def compact_existing_work_handoff_result(result: dict[str, Any]) -> dict[str, Any]:

    quarantine_ids = [
        int(event_id)
        for event_id in result.get("quarantined_event_ids", [])
        if isinstance(event_id, int) and not isinstance(event_id, bool) and event_id > 0
    ]
    return {
        **result,
        "quarantined_event_ids": quarantine_ids[:_TYPED_HANDOFF_EVENT_ID_PREVIEW_LIMIT],
        "quarantined_event_ids_total": len(quarantine_ids),
        "quarantined_event_ids_truncated": (
            len(quarantine_ids) > _TYPED_HANDOFF_EVENT_ID_PREVIEW_LIMIT
        ),
    }


def _typed_handoff_work_postimage(
    work: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:

    canonical = json.dumps(
        work,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    compact: dict[str, Any] = {}
    omitted: dict[str, dict[str, Any]] = {}
    for key in sorted(work):
        value = work[key]
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) <= _TYPED_HANDOFF_WORK_FIELD_INLINE_BYTES:
            compact[key] = value
        else:
            omitted[key] = {
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
    return compact, {
        "work_postimage_sha256": hashlib.sha256(canonical).hexdigest(),
        "work_postimage_bytes": len(canonical),
        "work_postimage_omitted_fields": omitted,
    }


def _typed_handoff_replay_unlocked(
    conn,
    *,
    request: dict[str, Any],
    request_sha256: str,
) -> dict[str, Any] | None:
    idempotency_key = f"typed-handoff:{request['actor']}:{request['operation_id']}"
    prior = conn.execute(
        "SELECT event_id,kind,work_id,to_selector,payload_json FROM events"
        " WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if prior is None:
        return None
    payload = _strict_json_mapping(prior["payload_json"])
    receipt = payload.get("operation_receipt") if payload else None
    if (
        str(prior["kind"] or "") != "handoff"
        or str(prior["work_id"] or "") != request["work_id"]
        or str(prior["to_selector"] or "") != f"actor:{request['owner_lane']}"
        or not isinstance(receipt, dict)
        or str(receipt.get("request_sha256") or "") != request_sha256
    ):
        raise ValueError(
            f"typed handoff operation_id {request['operation_id']!r} was already used for a different request"
        )
    postimage = receipt.get("work_postimage")
    superseded_ids = receipt.get("superseded_event_ids")
    if not isinstance(postimage, dict) or not isinstance(superseded_ids, list):
        raise ValueError("typed handoff immutable operation receipt is malformed")
    head_state = _typed_handoff_head_state_unlocked(conn, request["work_id"])
    event_id = int(prior["event_id"])
    receipt_meta = {
        "work_postimage_sha256": receipt.get("work_postimage_sha256"),
        "work_postimage_bytes": receipt.get("work_postimage_bytes"),
        "work_postimage_omitted_fields": receipt.get("work_postimage_omitted_fields"),
    }
    if (
        not isinstance(receipt_meta["work_postimage_sha256"], str)
        or not isinstance(receipt_meta["work_postimage_bytes"], int)
        or not isinstance(receipt_meta["work_postimage_omitted_fields"], dict)
    ):
        postimage, receipt_meta = _typed_handoff_work_postimage(dict(postimage))
    return {
        "event_id": event_id,
        "work_id": request["work_id"],
        "owner_lane": request["owner_lane"],
        "operation_id": request["operation_id"],
        "request_sha256": request_sha256,
        "superseded_event_ids": [int(value) for value in superseded_ids],
        "work": dict(postimage),
        **receipt_meta,
        "active": event_id in head_state["active_event_ids"],
        "superseded_by": head_state["superseded_by"].get(event_id),
        "quarantined_event_ids": head_state["quarantined_event_ids"],
        "replayed": True,
        "capsule_warnings": _typed_handoff_capsule_warnings(request),
    }


def lookup_existing_work_handoff_replay(conn, **fields: Any) -> dict[str, Any] | None:

    request, request_sha256 = _typed_handoff_request(**fields)
    return _typed_handoff_replay_unlocked(
        conn,
        request=request,
        request_sha256=request_sha256,
    )


def post_existing_work_handoff(conn, **fields: Any) -> dict[str, Any]:

    request, request_sha256 = _typed_handoff_request(**fields)
    work_id = request["work_id"]
    actor = request["actor"]
    session_id = request["session_id"]
    owner_lane = request["owner_lane"]
    idempotency_key = f"typed-handoff:{actor}:{request['operation_id']}"

    with tx(conn):
        replay = _typed_handoff_replay_unlocked(
            conn,
            request=request,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay

        existing = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(
                f"typed handoff work_id not found: {work_id}; create work through the CLI/admin path first"
            )
        declared_done = str(existing["done_signal"] or "").strip()
        if not declared_done:
            raise ValueError(
                f"typed handoff requires an existing done_signal: {work_id}"
            )
        current_intent = str(existing["intent_state"] or "").strip().lower()
        terminal_states = set(TERMINAL_WORK_STATES) | {"completed"}
        if current_intent in terminal_states or existing["archived_at"] is not None:
            raise ValueError(
                f"typed handoff refuses terminal/archived work {work_id}: {current_intent}"
            )
        proof_resolves = done_signal_satisfied(conn, declared_done, HARNESS_ROOT)
        awaits_t0_verdict = (
            str(existing["tier"] or "").strip().upper() == "T0"
            and not str(existing["rubric_verdict"] or "").strip()
        )
        if proof_resolves and not awaits_t0_verdict:
            raise ValueError(
                f"typed handoff refuses work whose completion proof already resolves: {work_id}"
            )
        proof_artifact = conn.execute(
            "SELECT artifact_id,kind,path FROM artifacts WHERE work_id=?"
            " AND COALESCE(kind,'') NOT IN ('context_pack') ORDER BY created_at,artifact_id LIMIT 1",
            (work_id,),
        ).fetchone()
        if proof_artifact is not None:
            raise ValueError(
                f"typed handoff refuses work already carrying derived completion artifact "
                f"{proof_artifact['artifact_id']} ({proof_artifact['kind']}): {work_id}"
            )
        live_run = conn.execute(
            "SELECT run_id,runner_kind,state FROM runs WHERE work_id=?"
            " AND state IN ('live','running','waiting')"
            " ORDER BY started_at,run_id LIMIT 1",
            (work_id,),
        ).fetchone()
        if live_run is not None:
            raise ValueError(
                f"typed handoff refuses active run {live_run['run_id']} "
                f"({live_run['runner_kind']}:{live_run['state']}) for {work_id}"
            )
        if int(existing["version"] or 0) != request["expected_version"]:
            raise ValueError(
                f"typed handoff version CAS failed for {work_id}: expected "
                f"{request['expected_version']}, observed {existing['version']}"
            )
        current_assignee = str(existing["assignee"] or "").strip().lower()
        if (
            current_assignee != actor
            or current_assignee != request["expected_assignee"]
        ):
            raise ValueError(
                f"typed handoff caller does not own assignment for {work_id}: "
                f"expected={request['expected_assignee']} observed={current_assignee or '<empty>'}"
            )
        head_state = _typed_handoff_head_state_unlocked(conn, work_id)
        if head_state["active_event_ids"] != request["expected_head_event_ids"]:
            raise ValueError(
                f"typed handoff assignment-head CAS failed for {work_id}: expected "
                f"{_typed_handoff_event_id_summary(request['expected_head_event_ids'])}, observed "
                f"{_typed_handoff_event_id_summary(head_state['active_event_ids'])}"
            )

        t = db_now(conn)
        _release_expired_claims_unlocked(conn, at=t, work_id=work_id)
        held = conn.execute(
            "SELECT claim_id,session_id,status FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked')"
            " AND (expires_at IS NULL OR expires_at > ?) ORDER BY acquired_at,claim_id",
            (work_id, t),
        ).fetchall()
        related_sessions = set(
            _related_session_ids_unlocked(conn, session_id, actor=actor) or [session_id]
        )
        foreign = [
            row for row in held if str(row["session_id"] or "") not in related_sessions
        ]
        if foreign:
            holder = foreign[0]
            raise ValueError(
                "typed handoff cannot steal a held claim: "
                f"claim_id={holder['claim_id']} session_id={holder['session_id']}"
            )
        own_held = [
            row for row in held if str(row["session_id"] or "") in related_sessions
        ]
        if own_held:
            claim_ids = [str(row["claim_id"]) for row in own_held]
            placeholders = ",".join("?" for _ in claim_ids)
            conn.execute(
                f"UPDATE claims SET status='released', release_reason=?,"
                f" version=version+1 WHERE claim_id IN ({placeholders})",
                ("typed cross-agent handoff closes caller ownership epoch", *claim_ids),
            )

        _upsert_work_unlocked(
            conn,
            work_id,
            {
                "assigned_by": actor,
                "assignee": owner_lane,
                "intent_state": request["target_intent"],
            },
        )
        work = conn.execute(
            "SELECT work_id,title,note,acceptance_json,assignee,assigned_by,"
            " intent_state,rubric_verdict,done_signal,version,updated_at"
            " FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
        work_postimage, work_postimage_meta = _typed_handoff_work_postimage(dict(work))
        superseded_ids = list(head_state["active_event_ids"])
        payload = {
            "schema_version": 2,
            "writer_contract": "existing_work_handoff.v2",
            "task": request["task"],
            "why": request["why"],
            "acceptance": request["acceptance"],
            "constraints": request["constraints"],
            "refs": request["refs"],
            "done_signal_source": "existing_coord_work_row",
            "operation_id": request["operation_id"],
            "operation_request_sha256": request_sha256,
            "preconditions": {
                "expected_version": request["expected_version"],
                "expected_assignee": request["expected_assignee"],
                "expected_head_event_ids": request["expected_head_event_ids"],
            },
            "operation_receipt": {
                "request_sha256": request_sha256,
                "work_postimage": work_postimage,
                **work_postimage_meta,
                "superseded_event_ids": superseded_ids,
            },
        }
        payload = compact_event_payload_same_as_row(payload, dict(work))
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " refs_json,payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "handoff",
                actor,
                session_id,
                f"actor:{owner_lane}",
                work_id,
                "agent",
                json.dumps(request["refs"], separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)
        for prior_id in superseded_ids:
            supersession_payload = json.dumps(
                {
                    "schema_version": 1,
                    "supersedes": prior_id,
                    "by_event_id": event_id,
                    "reason": "new typed global assignment head",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            conn.execute(
                "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
                " refs_json,payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t,
                    "handoff_superseded",
                    actor,
                    session_id,
                    f"actor:{owner_lane}",
                    work_id,
                    "agent",
                    json.dumps([f"event:{prior_id}"]),
                    supersession_payload,
                    f"typed-handoff-supersede:{event_id}:{prior_id}",
                ),
            )
    return {
        "event_id": event_id,
        "work_id": work_id,
        "owner_lane": owner_lane,
        "operation_id": request["operation_id"],
        "request_sha256": request_sha256,
        "superseded_event_ids": superseded_ids,
        "work": work_postimage,
        **work_postimage_meta,
        "active": True,
        "superseded_by": None,
        "quarantined_event_ids": head_state["quarantined_event_ids"],
        "replayed": False,
        "capsule_warnings": _typed_handoff_capsule_warnings(request),
    }


def _operator_reassignment_replay_unlocked(
    conn: sqlite3.Connection,
    *,
    request: dict[str, Any],
    request_sha256: str,
) -> dict[str, Any] | None:
    idempotency_key = f"operator-reassignment:{request['operation_id']}"
    prior = conn.execute(
        "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
        " refs_json,payload_json FROM events WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if prior is None:
        return None
    payload = _strict_json_mapping(prior["payload_json"])
    receipt = payload.get("operation_receipt") if payload else None
    expected_payload = {
        "task": request["task"],
        "why": request["why"],
        "acceptance": request["acceptance"],
        "constraints": request["constraints"],
        "refs": request["refs"],
        "operation_id": request["operation_id"],
        "operation_request_sha256": request_sha256,
    }
    if (
        str(prior["kind"] or "") != "handoff"
        or str(prior["actor"] or "") != "operator"
        or str(prior["session_id"] or "")
        or str(prior["work_id"] or "") != request["work_id"]
        or str(prior["to_selector"] or "")
        != f"actor:{request['owner_lane']}"
        or str(prior["trust"] or "") != "system"
        or _strict_json_list(prior["refs_json"]) != request["refs"]
        or payload is None
        or payload.get("schema_version") != 1
        or payload.get("writer_contract")
        != OPERATOR_REASSIGNMENT_WRITER_CONTRACT
        or payload.get("authority_channel") != OPERATOR_AUTHORITY_CHANNEL
        or any(payload.get(key) != value for key, value in expected_payload.items())
        or payload.get("preconditions")
        != {
            "expected_version": request["expected_version"],
            "expected_assignee": request["expected_assignee"],
            "expected_head_event_ids": request["expected_head_event_ids"],
            "release_held_claim": request["release_held_claim"],
        }
        or not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("writer_contract")
        != OPERATOR_REASSIGNMENT_RECEIPT_CONTRACT
        or receipt.get("authority_channel") != OPERATOR_AUTHORITY_CHANNEL
        or receipt.get("request_sha256") != request_sha256
    ):
        raise ValueError(
            "operator reassignment operation_id was already used for a different request"
        )
    postimage = receipt.get("work_postimage")
    superseded_ids = receipt.get("superseded_event_ids")
    released_claim_ids = receipt.get("released_claim_ids")
    if (
        not isinstance(postimage, dict)
        or not isinstance(superseded_ids, list)
        or not isinstance(released_claim_ids, list)
        or any(
            _strict_positive_event_id(value) is None for value in superseded_ids
        )
        or superseded_ids != sorted(set(superseded_ids))
        or len(superseded_ids) > 64
        or any(not isinstance(value, str) or not value for value in released_claim_ids)
        or len(released_claim_ids) > 1
        or not isinstance(receipt.get("work_postimage_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", receipt["work_postimage_sha256"])
        is None
        or not isinstance(receipt.get("work_postimage_bytes"), int)
        or not isinstance(receipt.get("work_postimage_omitted_fields"), dict)
    ):
        raise ValueError("operator reassignment immutable receipt is malformed")
    head_state = _typed_handoff_head_state_unlocked(conn, request["work_id"])
    event_id = int(prior["event_id"])
    result = {
        "schema_version": 1,
        "writer_contract": OPERATOR_REASSIGNMENT_RECEIPT_CONTRACT,
        "authority_channel": OPERATOR_AUTHORITY_CHANNEL,
        "event_id": event_id,
        "work_id": request["work_id"],
        "owner_lane": request["owner_lane"],
        "operation_id": request["operation_id"],
        "request_sha256": request_sha256,
        "superseded_event_ids": [int(value) for value in superseded_ids],
        "released_claim_ids": list(released_claim_ids),
        "work": dict(postimage),
        "work_postimage_sha256": receipt["work_postimage_sha256"],
        "work_postimage_bytes": receipt["work_postimage_bytes"],
        "work_postimage_omitted_fields": receipt[
            "work_postimage_omitted_fields"
        ],
        "active": event_id in head_state["active_event_ids"],
        "superseded_by": head_state["superseded_by"].get(event_id),
        "quarantined_event_ids": head_state["quarantined_event_ids"],
        "replayed": True,
        "capsule_warnings": _typed_handoff_capsule_warnings(request),
    }
    return compact_existing_work_handoff_result(result)


def post_operator_reassignment(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    owner_lane: str,
    task: str,
    why: str,
    acceptance: str,
    refs: Iterable[str],
    constraints: Iterable[str],
    operation_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_head_event_ids: Iterable[int],
    release_held_claim: bool = False,
    target_intent: str = "queued",
    _authority_capability: object,
) -> dict[str, Any]:
    """Reassign one canonical work row under resident-controller authority.

    Unlike the agent handoff writer, this operation has no lane session and
    never claims that the current assignee initiated the transfer.  It stamps
    the operator/system authority itself, requires all three assignment fences,
    and always refuses to disturb a live claim. The operator must stop or
    release the current holder before requesting a new ownership epoch.
    """

    if _authority_capability is not _OPERATOR_REASSIGNMENT_CAPABILITY:
        raise ValueError(
            "operator reassignment requires the authenticated resident-controller capability"
        )
    request, request_sha256 = _operator_reassignment_request(
        work_id=work_id,
        owner_lane=owner_lane,
        task=task,
        why=why,
        acceptance=acceptance,
        refs=refs,
        constraints=constraints,
        operation_id=operation_id,
        expected_version=expected_version,
        expected_assignee=expected_assignee,
        expected_head_event_ids=expected_head_event_ids,
        release_held_claim=release_held_claim,
        target_intent=target_intent,
    )
    clean_work_id = request["work_id"]
    idempotency_key = f"operator-reassignment:{request['operation_id']}"

    with tx(conn):
        replay = _operator_reassignment_replay_unlocked(
            conn,
            request=request,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay

        existing = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (clean_work_id,)
        ).fetchone()
        if existing is None:
            raise ValueError("operator reassignment work item not found")
        declared_done = str(existing["done_signal"] or "").strip()
        if not declared_done:
            raise ValueError("operator reassignment requires an existing done_signal")
        current_intent = str(existing["intent_state"] or "").strip().lower()
        if (
            current_intent in (set(TERMINAL_WORK_STATES) | {"completed"})
            or existing["archived_at"] is not None
        ):
            raise ValueError("operator reassignment refuses terminal or archived work")
        if done_signal_satisfied(conn, declared_done, HARNESS_ROOT):
            raise ValueError("operator reassignment refuses resolved completion proof")
        if conn.execute(
            "SELECT 1 FROM artifacts WHERE work_id=?"
            " AND COALESCE(kind,'') NOT IN ('context_pack') LIMIT 1",
            (clean_work_id,),
        ).fetchone() is not None:
            raise ValueError(
                "operator reassignment refuses a derived completion artifact"
            )
        terminal_run_states = tuple(sorted(TERMINAL_RUN_STATES))
        terminal_placeholders = ",".join("?" for _ in terminal_run_states)
        if conn.execute(
            "SELECT 1 FROM runs WHERE work_id=?"
            f" AND (state IS NULL OR state NOT IN ({terminal_placeholders})) LIMIT 1",
            (clean_work_id, *terminal_run_states),
        ).fetchone() is not None:
            raise ValueError("operator reassignment refuses a nonterminal run")
        observed_version = int(existing["version"] or 0)
        if observed_version != request["expected_version"]:
            raise ValueError(
                "operator reassignment version CAS failed: "
                f"expected {request['expected_version']}, observed {observed_version}"
            )
        current_assignee = str(existing["assignee"] or "").strip().lower()
        if current_assignee != request["expected_assignee"]:
            raise ValueError(
                "operator reassignment assignee CAS failed: expected and observed lanes differ"
            )
        head_state = _typed_handoff_head_state_unlocked(conn, clean_work_id)
        if head_state["active_event_ids"] != request["expected_head_event_ids"]:
            raise ValueError(
                "operator reassignment assignment-head CAS failed: expected "
                f"{_typed_handoff_event_id_summary(request['expected_head_event_ids'])}, observed "
                f"{_typed_handoff_event_id_summary(head_state['active_event_ids'])}"
            )

        t = db_now(conn)
        _release_expired_claims_unlocked(conn, at=t, work_id=clean_work_id)
        held = conn.execute(
            "SELECT claim_id FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked')"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY acquired_at,claim_id",
            (clean_work_id, t),
        ).fetchall()
        if held:
            raise ValueError(
                "operator reassignment refuses a live held claim; stop or release "
                "the current holder first"
            )
        released_claim_ids: list[str] = []

        changed = conn.execute(
            "UPDATE work_items SET assigned_by='operator',assignee=?,intent_state=?,"
            " updated_at=?,version=version+1 WHERE work_id=? AND version=?"
            " AND assignee=? AND archived_at IS NULL",
            (
                request["owner_lane"],
                request["target_intent"],
                t,
                clean_work_id,
                request["expected_version"],
                request["expected_assignee"],
            ),
        )
        if changed.rowcount != 1:
            raise ValueError("operator reassignment work-row CAS drift")
        work = conn.execute(
            "SELECT work_id,title,note,acceptance_json,assignee,assigned_by,"
            " intent_state,rubric_verdict,done_signal,version,updated_at"
            " FROM work_items WHERE work_id=?",
            (clean_work_id,),
        ).fetchone()
        work_postimage, work_postimage_meta = _typed_handoff_work_postimage(dict(work))
        superseded_ids = list(head_state["active_event_ids"])
        operation_receipt = {
            "schema_version": 1,
            "writer_contract": OPERATOR_REASSIGNMENT_RECEIPT_CONTRACT,
            "authority_channel": OPERATOR_AUTHORITY_CHANNEL,
            "request_sha256": request_sha256,
            "work_postimage": work_postimage,
            **work_postimage_meta,
            "superseded_event_ids": superseded_ids,
            "released_claim_ids": released_claim_ids,
        }
        payload = {
            "schema_version": 1,
            "writer_contract": OPERATOR_REASSIGNMENT_WRITER_CONTRACT,
            "authority_channel": OPERATOR_AUTHORITY_CHANNEL,
            "task": request["task"],
            "why": request["why"],
            "acceptance": request["acceptance"],
            "constraints": request["constraints"],
            "refs": request["refs"],
            "done_signal_source": "existing_coord_work_row",
            "operation_id": request["operation_id"],
            "operation_request_sha256": request_sha256,
            "preconditions": {
                "expected_version": request["expected_version"],
                "expected_assignee": request["expected_assignee"],
                "expected_head_event_ids": request["expected_head_event_ids"],
                "release_held_claim": request["release_held_claim"],
            },
            "operation_receipt": operation_receipt,
        }
        cur = conn.execute(
            "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
            " title,body,refs_json,payload_json,idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t,
                "handoff",
                "operator",
                None,
                f"actor:{request['owner_lane']}",
                clean_work_id,
                "system",
                "Operator reassignment",
                "Resident controller reassigned canonical work ownership.",
                json.dumps(request["refs"], separators=(",", ":")),
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                idempotency_key,
            ),
        )
        event_id = int(cur.lastrowid)
        for prior_id in superseded_ids:
            conn.execute(
                "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
                " refs_json,payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t,
                    "handoff_superseded",
                    "operator",
                    None,
                    f"actor:{request['owner_lane']}",
                    clean_work_id,
                    "system",
                    json.dumps([f"event:{prior_id}"], separators=(",", ":")),
                    json.dumps(
                        {
                            "schema_version": 1,
                            "writer_contract": "operator_reassignment_supersession.v1",
                            "supersedes": prior_id,
                            "by_event_id": event_id,
                            "reason": "operator reassignment replaced the active assignment head",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    f"operator-reassignment-supersede:{event_id}:{prior_id}",
                ),
            )
        final_head_state = _typed_handoff_head_state_unlocked(conn, clean_work_id)
        if (
            event_id not in final_head_state["active_event_ids"]
            or any(
                prior_id in final_head_state["active_event_ids"]
                for prior_id in superseded_ids
            )
        ):
            raise ValueError("operator reassignment assignment-head receipt failed")

    result = {
        "schema_version": 1,
        "writer_contract": OPERATOR_REASSIGNMENT_RECEIPT_CONTRACT,
        "authority_channel": OPERATOR_AUTHORITY_CHANNEL,
        "event_id": event_id,
        "work_id": clean_work_id,
        "owner_lane": request["owner_lane"],
        "operation_id": request["operation_id"],
        "request_sha256": request_sha256,
        "superseded_event_ids": superseded_ids,
        "released_claim_ids": released_claim_ids,
        "work": work_postimage,
        **work_postimage_meta,
        "active": True,
        "superseded_by": None,
        "quarantined_event_ids": final_head_state["quarantined_event_ids"],
        "replayed": False,
        "capsule_warnings": _typed_handoff_capsule_warnings(request),
    }
    return compact_existing_work_handoff_result(result)


def _post_typed_handoff_canary_rollback_unlocked(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    canary_event_id: int,
    operation_id: str,
    preimage_slice_sha256: str,
) -> dict[str, Any]:

    if not conn.in_transaction:
        raise RuntimeError(
            "typed canary rollback requires a caller-owned restore transaction"
        )

    clean_work_id = str(work_id or "").strip()
    clean_operation_id = str(operation_id or "").strip()
    clean_preimage_slice_sha256 = str(preimage_slice_sha256 or "").strip()
    exact_canary_event_id = _strict_positive_event_id(canary_event_id)
    if not clean_work_id or exact_canary_event_id is None:
        raise ValueError("typed canary rollback requires exact work/event identity")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", clean_operation_id
    ):
        raise ValueError("typed canary rollback requires a safe operation_id")
    if re.fullmatch(r"[a-f0-9]{64}", clean_preimage_slice_sha256) is None:
        raise ValueError("typed canary rollback requires exact preimage SHA-256")
    successor = conn.execute(
        "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
        " refs_json,payload_json,idempotency_key FROM events WHERE event_id=?",
        (exact_canary_event_id,),
    ).fetchone()
    successor_payload = (
        _strict_json_mapping(successor["payload_json"]) if successor else None
    )
    operation_receipt = (
        successor_payload.get("operation_receipt")
        if successor_payload is not None
        else None
    )
    if (
        successor is None
        or successor_payload is None
        or str(successor["kind"] or "") != "handoff"
        or str(successor["work_id"] or "") != clean_work_id
        or str(successor["actor"] or "") != "codex"
        or str(successor["session_id"] or "")
        != "codex:thread:g3-live-canary"
        or str(successor["to_selector"] or "") != "actor:claude"
        or str(successor["trust"] or "") != "agent"
        or successor_payload.get("schema_version") != 2
        or successor_payload.get("writer_contract")
        != "existing_work_handoff.v2"
        or successor_payload.get("operation_id") != clean_operation_id
        or not isinstance(operation_receipt, dict)
    ):
        raise ValueError("typed canary rollback predecessor contract mismatch")
    operation_request_sha256 = str(
        successor_payload.get("operation_request_sha256") or ""
    )
    canary_work_postimage_sha256 = str(
        operation_receipt.get("work_postimage_sha256") or ""
    )
    if operation_receipt.get("request_sha256") != operation_request_sha256:
        raise ValueError("typed canary rollback predecessor request receipt mismatch")
    canary_event_payload_sha256 = hashlib.sha256(
        str(successor["payload_json"] or "").encode("utf-8")
    ).hexdigest()
    rollback_key_prefix = f"typed-handoff-canary-rollback:{clean_operation_id}:"
    prior_rows = conn.execute(
        "SELECT event_id,kind,actor,session_id,to_selector,work_id,trust,"
        " refs_json,payload_json,idempotency_key FROM events"
        " WHERE instr(COALESCE(idempotency_key,''),?)=1 ORDER BY event_id",
        (rollback_key_prefix,),
    ).fetchall()
    valid_priors: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for prior in prior_rows:
        prior_payload = _strict_json_mapping(prior["payload_json"])
        if (
            not _typed_handoff_canary_rollback_is_valid_unlocked(
                row=prior,
                payload=prior_payload,
                prior_id=exact_canary_event_id,
                by_event_id=int(prior["event_id"]),
                canary=successor,
                canary_payload=successor_payload,
            )
            or prior_payload is None
            or prior_payload.get("preimage_slice_sha256")
            != clean_preimage_slice_sha256
        ):
            raise ValueError("typed canary rollback replay receipt mismatch")
        valid_priors.append((prior, prior_payload))
    if len(valid_priors) > 1:
        raise ValueError("typed canary rollback replay authority is duplicated")
    if valid_priors:
        prior, prior_payload = valid_priors[0]
        return {
            "event_id": int(prior["event_id"]),
            "work_id": clean_work_id,
            "canary_event_id": exact_canary_event_id,
            "operation_id": clean_operation_id,
            "rollback_request_sha256": prior_payload[
                "rollback_request_sha256"
            ],
            "replayed": True,
        }
    work = conn.execute(
        "SELECT work_id,title,note,acceptance_json,assignee,assigned_by,"
        " intent_state,rubric_verdict,done_signal,version,updated_at"
        " FROM work_items WHERE work_id=?",
        (clean_work_id,),
    ).fetchone()
    if work is None:
        raise ValueError("typed canary rollback work row is missing")
    _work_postimage, work_meta = _typed_handoff_work_postimage(dict(work))
    if work_meta["work_postimage_sha256"] != canary_work_postimage_sha256:
        raise ValueError("typed canary rollback work postimage drifted")
    head_state = _typed_handoff_head_state_unlocked(conn, clean_work_id)
    expected_active_head_event_ids = [exact_canary_event_id]
    if head_state["active_event_ids"] != expected_active_head_event_ids:
        raise ValueError(
            "typed canary rollback active-head CAS failed: expected "
            f"{expected_active_head_event_ids}, observed "
            f"{head_state['active_event_ids']}"
        )
    sequence_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='events'"
    ).fetchone()
    max_event_id = int(
        conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]
    )
    sequence_id = int(sequence_row[0]) if sequence_row is not None else 0
    rollback_event_id = max(sequence_id, max_event_id) + 1
    request, request_sha256 = _typed_handoff_canary_rollback_request(
        work_id=clean_work_id,
        canary_event_id=exact_canary_event_id,
        rollback_event_id=rollback_event_id,
        operation_id=clean_operation_id,
        operation_request_sha256=operation_request_sha256,
        preimage_slice_sha256=clean_preimage_slice_sha256,
        canary_event_payload_sha256=canary_event_payload_sha256,
        canary_work_postimage_sha256=canary_work_postimage_sha256,
    )
    key = (
        f"typed-handoff-canary-rollback:{request['operation_id']}:{request_sha256}"
    )
    receipt = {
        "schema_version": 1,
        "writer_contract": "typed_handoff_canary_rollback_receipt.v1",
        "rollback_request_sha256": request_sha256,
        "work_id": clean_work_id,
        "canary_event_id": exact_canary_event_id,
        "rollback_event_id": rollback_event_id,
        "expected_active_head_event_ids": [exact_canary_event_id],
        "operation_id": request["operation_id"],
        "operation_request_sha256": operation_request_sha256,
        "preimage_slice_sha256": request["preimage_slice_sha256"],
        "canary_event_payload_sha256": canary_event_payload_sha256,
        "canary_work_postimage_sha256": canary_work_postimage_sha256,
    }
    payload = {
        "schema_version": 1,
        "writer_contract": "typed_handoff_canary_rollback.v1",
        "supersedes": exact_canary_event_id,
        "by_event_id": rollback_event_id,
        "expected_active_head_event_ids": [exact_canary_event_id],
        "operation_id": request["operation_id"],
        "operation_request_sha256": operation_request_sha256,
        "preimage_slice_sha256": request["preimage_slice_sha256"],
        "canary_event_payload_sha256": canary_event_payload_sha256,
        "canary_work_postimage_sha256": canary_work_postimage_sha256,
        "rollback_request_sha256": request_sha256,
        "rollback_receipt": receipt,
    }
    conn.execute(
        "INSERT INTO events(event_id,ts,kind,actor,session_id,to_selector,work_id,"
        " trust,refs_json,payload_json,idempotency_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            rollback_event_id,
            db_now(conn),
            "handoff_superseded",
            request["actor"],
            request["session_id"],
            request["to_selector"],
            clean_work_id,
            request["trust"],
            json.dumps([f"event:{exact_canary_event_id}"]),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            key,
        ),
    )
    post_state = _typed_handoff_head_state_unlocked(conn, clean_work_id)
    if (
        post_state["active_event_ids"]
        or rollback_event_id in post_state["quarantined_event_ids"]
        or post_state["superseded_by"].get(exact_canary_event_id)
        != rollback_event_id
    ):
        raise ValueError("typed canary rollback receipt failed strict readback")
    return {
        "event_id": rollback_event_id,
        "work_id": clean_work_id,
        "canary_event_id": exact_canary_event_id,
        "operation_id": request["operation_id"],
        "rollback_request_sha256": request_sha256,
        "replayed": False,
    }


def _held_claim_for_conflict(
    conn, work_id: str, requester_session_id: str | None = None
) -> dict | None:
    release_expired_claims(conn, work_id=work_id)
    t = db_now(conn)
    args: list[Any] = [work_id]
    exclude = ""
    if requester_session_id:
        exclude = " AND c.session_id!=?"
        args.append(requester_session_id)
    row = conn.execute(
        "SELECT c.claim_id, c.work_id, c.session_id AS holder_session_id,"
        " c.status AS holder_status, c.step AS holder_step,"
        " s.actor AS holder_actor"
        " FROM claims c LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
        " WHERE c.work_id=?"
        f"{exclude}"
        " AND c.status IN ('running','paused','blocked')"
        " AND (c.expires_at IS NULL OR c.expires_at > ?)"
        " ORDER BY c.acquired_at DESC LIMIT 1",
        tuple([*args, t]),
    ).fetchone()
    return dict(row) if row else None


def post_claim_conflict(
    conn,
    *,
    work_id: str,
    requester_actor: str | None,
    requester_session_id: str,
    requester_step: str | None = None,
    verb: str = "claim",
    holder: dict | None = None,
) -> list[int | None]:
    holder = holder or _held_claim_for_conflict(conn, work_id, requester_session_id)
    requester_actor = requester_actor or "agent"
    payload = {
        "schema_version": 1,
        "reason": "held_by_other",
        "verb": verb,
        "work_id": work_id,
        "requester_actor": requester_actor,
        "requester_session_id": requester_session_id,
        "requester_step": requester_step,
        "holder_claim_id": holder.get("claim_id") if holder else None,
        "holder_actor": holder.get("holder_actor") if holder else None,
        "holder_session_id": holder.get("holder_session_id") if holder else None,
        "holder_status": holder.get("holder_status") if holder else None,
        "holder_step": holder.get("holder_step") if holder else None,
    }
    selectors = [f"actor:{requester_actor}"]
    holder_actor = payload.get("holder_actor")
    if holder_actor and holder_actor != requester_actor:
        selectors.append(f"actor:{holder_actor}")
    title = f"Claim conflict: {work_id}"
    body = (
        f"{requester_actor}:{requester_session_id} could not {verb} {work_id}; "
        f"held by {payload.get('holder_actor') or 'unknown'}:"
        f"{payload.get('holder_session_id') or 'unknown'}"
    )
    return [
        post_event(
            conn,
            kind="claim_conflict",
            actor=requester_actor,
            session_id=requester_session_id,
            to_selector=selector,
            work_id=work_id,
            severity="warning",
            title=title,
            body=body,
            payload_json=_json_dumps(payload),
        )
        for selector in selectors
    ]


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def resolve_note_session_target(conn, session_id: str, *, at: float | None = None) -> str:
    """Validate one session as a note recipient, or refuse loudly.

    ``inbox_cursors`` is keyed ``(recipient, session_id)`` and every inbox read
    path already resolves a ``session:<id>`` selector, so addressing a single
    session has always been readable -- there was simply no writer that produced
    one. Adding the writer means adding this check with it: a selector naming a
    session that never existed, or one whose lease has lapsed, is a message
    posted into a void. Nothing errors, nothing bounces, and the sender believes
    it was delivered. Refusing here is the difference between "the other agent
    has not answered yet" and "the other agent was never going to see it".

    Liveness is the same derivation the session rollup uses -- an active row
    whose lease has not expired -- so a session the board shows as live is
    exactly a session that can be addressed.
    """
    target = str(session_id or "").strip()
    if not target:
        raise ValueError("a session note target must name a session_id")
    row = conn.execute(
        "SELECT session_id, actor, state, lease_until, ended_at FROM agent_sessions"
        " WHERE session_id=?",
        (target,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"unknown session {target!r}: no such session has ever registered on "
            "this board, so a note addressed to it would be readable by nobody"
        )
    now = at if at is not None else db_now(conn)
    state = str(row["state"] or "")
    lease_until = float(row["lease_until"] or 0.0)
    if state != "active" or lease_until <= now:
        detail = (
            f"state={state!r}"
            if state != "active"
            else f"lease expired {int(now - lease_until)}s ago"
        )
        raise ValueError(
            f"session {target!r} is not live ({detail}); a note addressed to a "
            "dead session is never read by anyone -- address the lane with "
            "to_actor instead, or wait for that session to reappear"
        )
    return target


def post_note(
    conn,
    *,
    work_id: str,
    actor: str,
    session_id: str = "",
    to_actor: str = "",
    to_session_id: str = "",
    title: str | None = None,
    body: str,
    refs: list[str] | None = None,
) -> dict:
    """Append one directed, append-only message about an existing row.

    A note carries no lifecycle: it does not move ownership, status, version or
    verdict. That is the whole point of having it -- an agent mid-run needs a
    way to tell the other lane something without taking or altering its work.

    ``to_session_id`` narrows the address from a lane to one live session, which
    is what a fleet of same-lane sessions needs: ``actor:claude`` reaches all
    three of them and is answered by none. It is checked, not trusted -- see
    ``resolve_note_session_target``. Omitted, the behaviour is unchanged.

    Bounded like the MCP tool it mirrors, so the two surfaces cannot drift into
    different contracts for the same event kind.
    """
    clean_work = str(work_id or "").strip()
    if not clean_work:
        raise ValueError("note requires an existing work_id")
    clean_body = str(body or "").strip()
    if not clean_body:
        raise ValueError("note requires a non-empty body")
    if len(clean_body) > 2048:
        raise ValueError("note body exceeds 2048 characters; use pointer refs")
    clean_title = str(title or "").strip() or f"Note: {clean_work}"
    if len(clean_title) > 200:
        raise ValueError("note title exceeds 200 characters")
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if len(clean_refs) > 32:
        raise ValueError("note refs are bounded to 32 pointers")
    if not conn.execute(
        "SELECT 1 FROM work_items WHERE work_id = ?", (clean_work,)
    ).fetchone():
        raise ValueError(f"note target {clean_work!r} is not a row on this board")

    # A session address wins over the lane address when both are given: it is
    # the strictly narrower of the two, and silently widening it back to the
    # lane would deliver the message to peers the sender chose not to name.
    if str(to_session_id or "").strip():
        selector = "session:" + resolve_note_session_target(conn, to_session_id)
    elif str(to_actor or "").strip():
        selector = f"actor:{to_actor.strip()}"
    else:
        selector = None
    payload = json.dumps({
        "schema_version": 1,
        "event_only": True,
        "lifecycle_mutation": False,
        "neutral_note": True,
        "source": "coord.cli.note",
    }, sort_keys=True)
    cur = conn.execute(
        "INSERT INTO events(ts,kind,actor,session_id,to_selector,work_id,trust,"
        "title,body,refs_json,payload_json)"
        " VALUES (?,'note',?,?,?,?,'agent',?,?,?,?)",
        (
            time.time(), actor, session_id or None, selector, clean_work,
            clean_title, clean_body, json.dumps(clean_refs), payload,
        ),
    )
    conn.commit()
    return {
        "event_id": int(cur.lastrowid),
        "work_id": clean_work,
        "to_selector": selector,
        "kind": "note",
    }


def unread_inbox_counts(
    conn, *, recipient_actor: str, session_id: str = ""
) -> dict[str, int]:
    """Unread past the cursor, split by whether the message was addressed here.

    One summed number cannot answer the question an agent is actually asking.
    Broadcasts are legitimate -- they are how the board reports its own activity
    -- but they are written to nobody in particular, and on a working board they
    outnumber directed messages by an order of magnitude. Summed, one interrupt
    naming this actor is indistinguishable from forty events naming no one, so
    "44 unread" reads as noise at exactly the moment it should read as "someone
    is waiting on you". An interrupt is by definition directed; the split is
    what makes it findable.

    ``total`` remains the sum of the two legs, so a caller that only ever knew
    the single number keeps reading the same figure.
    """
    cursor = get_cursor(conn, recipient_actor, session_id)
    selectors = [f"actor:{recipient_actor}"]
    if session_id:
        selectors.append(f"session:{session_id}")
    placeholders = ",".join("?" * len(selectors))
    directed_row = conn.execute(
        f"SELECT COUNT(*) FROM events"
        f" WHERE to_selector IN ({placeholders}) AND event_id > ?",
        (*selectors, cursor),
    ).fetchone()
    broadcast_row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE to_selector IS NULL AND event_id > ?",
        (cursor,),
    ).fetchone()
    directed = int(directed_row[0] if directed_row else 0)
    broadcast = int(broadcast_row[0] if broadcast_row else 0)
    return {
        "directed": directed,
        "broadcast": broadcast,
        "total": directed + broadcast,
    }


def unread_inbox_count(conn, *, recipient_actor: str, session_id: str = "") -> int:
    """How many messages sit past the cursor, regardless of any read limit.

    A caller that asks for twenty and receives twenty cannot tell whether that
    was the whole queue or the front of it. Mid-flight, that difference decides
    whether "nothing new arrived" is a fact or an artefact of the limit.

    This is both legs summed. ``unread_inbox_counts`` splits them, which is the
    reading that distinguishes an interrupt from board chatter.
    """
    return unread_inbox_counts(
        conn, recipient_actor=recipient_actor, session_id=session_id
    )["total"]


def read_inbox(
    conn, *, recipient_actor: str, session_id: str = "", limit: int = 20,
    newest_first: bool = False, directed_only: bool = False,
) -> list[dict]:
    """Messages past this actor's cursor.

    Order is a real choice, not a detail. The queue reading -- oldest first --
    is right for draining a backlog in the order it was written. It is wrong
    for the question an agent actually asks mid-run, "did anything arrive while
    I was working", because at any limit smaller than the backlog the newest
    message is exactly the one outside the window: the caller sees twenty old
    messages and concludes nothing new came in.

    ``newest_first`` answers that question instead. Both orders return the same
    set; they differ only in which end the limit truncates, which is precisely
    what made the default dangerous.

    Every row carries ``directed``: true when the message named this actor or
    its session in ``to_selector``, false when it was written to the board at
    large. Without it the two arrive as one undifferentiated stream and the
    reading end cannot tell an interrupt from board chatter.

    ``directed_only`` drops the broadcast leg for a caller that wants just the
    messages aimed at it. It is opt-in on purpose: the default has to keep
    returning the same SET it always did, because a reader that quietly stopped
    seeing broadcasts would be the same defect wearing the other sign.
    """
    cursor = get_cursor(conn, recipient_actor, session_id)
    selectors = [f"actor:{recipient_actor}"]
    if session_id:
        selectors.append(f"session:{session_id}")
    placeholders = ",".join("?" * len(selectors))
    direction = "DESC" if newest_first else "ASC"
    legs = [
        f"  SELECT *, 1 AS _directed FROM events"
        f"   WHERE to_selector IN ({placeholders}) AND event_id > ?"
    ]
    params: list[Any] = [*selectors, cursor]
    if not directed_only:
        legs.append(
            "  SELECT *, 0 AS _directed FROM events"
            "   WHERE to_selector IS NULL AND event_id > ?"
        )
        params.append(cursor)
    rows = conn.execute(
        "SELECT * FROM ("
        + "  UNION ALL".join(legs)
        + f") ORDER BY event_id {direction} LIMIT ?",
        (*params, limit),
    ).fetchall()
    messages = []
    for row in rows:
        message = dict(row)
        # ``_directed`` is the column name the sibling reader already uses; the
        # caller gets it as a plain boolean rather than a private-looking int.
        message["directed"] = bool(message.pop("_directed", 0))
        messages.append(message)
    return messages


def open_audit_request_summary(
    conn, *, recipient_lane: str, limit: int = OPEN_AUDIT_REQUEST_PREVIEW_LIMIT
) -> dict[str, Any]:
    clean_lane = str(recipient_lane or "").strip().lower()
    if not clean_lane:
        raise ValueError("open audit request summary requires recipient_lane")
    try:
        bounded_limit = max(
            0, min(int(limit), OPEN_AUDIT_REQUEST_PREVIEW_LIMIT)
        )
    except (TypeError, ValueError):
        bounded_limit = OPEN_AUDIT_REQUEST_PREVIEW_LIMIT
    where = (
        " FROM request_consumption rc"
        " JOIN events e ON e.event_id=rc.request_event_id"
        " WHERE rc.recipient_lane=? AND rc.consumed_at IS NULL"
        " AND e.kind='audit_request'"
    )
    rows = conn.execute(
        "SELECT rc.request_event_id AS event_id,e.work_id"
        + where
        + " ORDER BY rc.request_event_id DESC",
        (clean_lane,),
    ).fetchall()
    superseded_by_work: dict[str, set[int]] = {}
    open_rows = []
    for row in rows:
        work_id = str(row["work_id"] or "")
        if work_id not in superseded_by_work:
            head_state = _typed_handoff_head_state_unlocked(conn, work_id)
            superseded_by_work[work_id] = {
                int(event_id) for event_id in head_state["superseded_by"]
            }
        if int(row["event_id"]) not in superseded_by_work[work_id]:
            open_rows.append(row)
    count = len(open_rows)
    preview = [
        {"event_id": int(row["event_id"]), "work_id": str(row["work_id"])}
        for row in open_rows[:bounded_limit]
    ]
    return {
        "open_audit_request_count": count,
        "open_audit_requests": preview,
        "open_audit_request_truncated": count > len(preview),
    }


INBOX_NOISE_KINDS = (
    "telemetry",
    "prompt",
    "activity",
    "beacon",
    "tool",
    "tool_use",
    "pretool",
    "posttool",
    "heartbeat",
    "claude_heartbeat",
    "codex_heartbeat",
    "claude_activity",
    "codex_activity",
    "subagent_start",
    "subagent_stop",
    "run_start",
    "run_end",
    "run",
    "session_start",
    "session_end",
)


def inbox_recent(
    conn,
    *,
    recipient_actor: str,
    session_id: str = "",
    limit: int = 20,
    exclude_kinds: tuple[str, ...] = (),
) -> list[dict]:
    cursor = get_cursor(conn, recipient_actor, session_id)
    selectors = [f"actor:{recipient_actor}"]
    if session_id:
        selectors.append(f"session:{session_id}")
    placeholders = ",".join("?" * len(selectors))
    kind_clause = ""
    kind_params: tuple = ()
    if exclude_kinds:
        kph = ",".join("?" * len(exclude_kinds))
        kind_clause = f" AND kind NOT IN ({kph})"
        kind_params = tuple(exclude_kinds)
    rows = conn.execute(
        f"SELECT * FROM ("
        f"  SELECT *, 1 AS _directed FROM events"
        f"   WHERE to_selector IN ({placeholders}) AND event_id > ?{kind_clause}"
        f"  UNION ALL"
        f"  SELECT *, 0 AS _directed FROM events"
        f"   WHERE to_selector IS NULL AND event_id > ?{kind_clause}"
        f") ORDER BY _directed DESC, event_id DESC LIMIT ?",
        (*selectors, cursor, *kind_params, cursor, *kind_params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cursor(conn, recipient_actor: str, session_id: str = "") -> int:
    row = conn.execute(
        "SELECT last_seen_event_id FROM inbox_cursors WHERE recipient=? AND session_id=?",
        (recipient_actor, session_id),
    ).fetchone()
    return row["last_seen_event_id"] if row else 0


def advance_cursor(
    conn, recipient_actor: str, last_seen_event_id: int, session_id: str = ""
) -> None:
    with tx(conn):
        t = db_now(conn)
        conn.execute(
            "INSERT INTO inbox_cursors(recipient, session_id, last_seen_event_id, updated_at)"
            " VALUES (?,?,?,?) ON CONFLICT(recipient, session_id) DO UPDATE SET"
            " last_seen_event_id=MAX(last_seen_event_id, excluded.last_seen_event_id), updated_at=excluded.updated_at",
            (recipient_actor, session_id, last_seen_event_id, t),
        )
        conn.execute(
            "UPDATE request_consumption SET consumed_at=?"
            " WHERE recipient_lane=? AND request_event_id<=?"
            " AND consumed_at IS NULL",
            (t, recipient_actor, int(last_seen_event_id)),
        )


def set_display_title(conn, key: str, display: str) -> None:
    with tx(conn):
        t = db_now(conn)
        conn.execute(
            "INSERT INTO display_titles(key, display, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET display=excluded.display, updated_at=excluded.updated_at",
            (key, display, t),
        )
        if str(display or "").strip():
            conn.execute(
                "UPDATE work_items SET display=?, updated_at=?, version=version+1 WHERE work_id=?",
                (str(display).strip(), t, key),
            )


def active_intent_debt(
    conn,
    *,
    at: float | None = None,
    grace_s: float = ACTIVE_INTENT_STALE_SECS,
    limit: int = 25,
) -> list[dict]:
    at = at if at is not None else db_now(conn)
    rows = conn.execute(
        """
        SELECT w.work_id, w.title, w.assignee, w.updated_at, w.created_at
          FROM work_items w
         WHERE w.intent_state='running'
           AND w.archived_at IS NULL
           AND COALESCE(w.updated_at, w.created_at, 0) < ?
           AND NOT EXISTS (
               SELECT 1 FROM claims c
                WHERE c.work_id=w.work_id
                  AND c.status IN ('running','paused','blocked')
           )
           AND NOT EXISTS (
               SELECT 1 FROM artifacts a
                WHERE a.work_id=w.work_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM runs r
                WHERE r.work_id=w.work_id
                  AND r.state='live'
           )
         ORDER BY COALESCE(w.updated_at, w.created_at, 0) ASC
         LIMIT ?
        """,
        (at - grace_s, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def normalize_stale_active_intents(
    conn,
    *,
    at: float | None = None,
    grace_s: float = ACTIVE_INTENT_STALE_SECS,
    limit: int = 1000,
) -> dict:
    at = at if at is not None else db_now(conn)
    debt = active_intent_debt(conn, at=at, grace_s=grace_s, limit=limit)
    ids = [str(row["work_id"]) for row in debt]
    if ids:
        with tx(conn):
            for wid in ids:
                conn.execute(
                    "UPDATE work_items SET intent_state='queued', updated_at=?, version=version+1"
                    " WHERE work_id=? AND intent_state='running'",
                    (at, wid),
                )
    return {"normalized": len(ids), "work_ids": ids[:25]}


def _is_observer_work_id(work_id: str) -> bool:
    wid = (work_id or "").strip()
    if not wid:
        return False
    lowered = wid.lower()
    if lowered.startswith(
        ("job:obs_", "raw:", *(f"{lane}:" for lane in _configured_lanes()))
    ):
        return True
    if re.fullmatch(r"job:[0-9a-f]{10,16}", lowered):
        return True
    if re.fullmatch(r"019e[0-9a-f-]{20,}", lowered):
        return True
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lowered
    ):
        return True
    if re.fullmatch(r"[0-9a-f]{12,40}", lowered):
        return True
    return False


_REVIEW_TIER_POLICY_ENFORCED_AT = 1784774176.0


def derive_work_status(row: dict, at: float) -> str:
    if row.get("archived_at"):
        return "archived"
    intent = row.get("intent_state") or "planned"
    if intent in {"archived", "superseded", "cancelled", "canceled", "closed"}:
        return intent
    has_artifact = bool(row.get("has_artifact"))
    rubric = row.get("rubric_verdict")
    from .review_tier import effective_review_tier, normalize_declared_tier

    effective_tier = normalize_declared_tier(row.get("effective_tier"))
    needs_rubric = bool(
        (effective_tier or effective_review_tier(row)) == "T0"
        and not row.get("legacy_pre_review_tier_done")
    )
    operator_ok = bool(row.get("operator_ok_validated"))
    work_id = str(row.get("work_id") or "")
    if "live_pid_count" in row:
        local_live = int(row.get("live_pid_count") or 0) > 0
    else:
        local_live = int(row.get("live_run_count") or 0) > 0
    if local_live and not _is_observer_work_id(work_id):
        return "running"
    owner = row.get("owner_session_id")
    cstatus = row.get("claim_status")
    expires = row.get("claim_expires_at")
    if intent == "failed":
        return "failed"
    if intent != "done" and owner and cstatus == "running":
        return "running" if (expires and expires > at) else "attention"
    if intent != "done" and owner and cstatus in ("paused", "blocked"):
        if expires and expires > at:
            return cstatus
        return "attention"
    if intent == "blocked":
        return "blocked"
    if intent == "done":
        rubric_ok = (not needs_rubric) or rubric == "pass" or operator_ok
        if has_artifact and rubric_ok:
            return "done"
        return "attention"
    if has_artifact:
        return "attention"
    if rubric in ("flag", "blocked"):
        return "attention"
    if intent == "running":
        return "queued"
    return intent


def derive_proof_state(row: dict, at: float) -> str:
    status = derive_work_status(row, at)
    if status in {"archived", "superseded", "cancelled", "canceled", "closed"}:
        return status
    if status == "running":
        return "running"
    if status == "failed":
        return "failed"
    if status == "blocked":
        return "blocked"
    intent = row.get("intent_state") or "planned"
    has_artifact = bool(row.get("has_artifact"))
    rubric = str(row.get("rubric_verdict") or "").strip().lower()
    from .review_tier import effective_review_tier, normalize_declared_tier

    effective_tier = normalize_declared_tier(row.get("effective_tier"))
    needs_rubric = bool(
        (effective_tier or effective_review_tier(row)) == "T0"
        and not row.get("legacy_pre_review_tier_done")
    )
    operator_ok = bool(row.get("operator_ok_validated"))
    if status == "done":
        return (
            "verified_done"
            if (not needs_rubric or rubric == "pass" or operator_ok)
            else "legacy_done"
        )
    if has_artifact and intent != "done":
        return "needs_adjudication"
    if rubric in {"flag", "fail", "failed", "red", "reject", "rejected"}:
        return "acceptance_failed"
    if rubric in {"blocked", "block"}:
        return "acceptance_blocked"
    if intent == "done" and not has_artifact:
        return "missing_done_signal"
    if has_artifact and needs_rubric and rubric != "pass" and not operator_ok:
        return "needs_verification"
    if has_artifact:
        return "artifact_present"
    if intent in {"queued", "planned"}:
        return "awaiting_artifact"
    return status


PRIORITY_UNRANKED_SORT = float("inf")


def priority_sort_value(value: Any) -> float:

    if isinstance(value, bool):
        return PRIORITY_UNRANKED_SORT
    try:
        rank = float(value)
    except (TypeError, ValueError):
        return PRIORITY_UNRANKED_SORT
    if rank != rank or rank <= 0:
        return PRIORITY_UNRANKED_SORT
    return rank


def board_current_rank(row: dict[str, Any]) -> tuple[int, float, float, str]:

    status = str(row.get("status") or row.get("intent_state") or "").strip().lower()
    status_rank = {
        "running": 0,
        "blocked": 1,
        "attention": 2,
        "queued": 3,
        "planned": 4,
        "done": 10,
        "failed": 11,
        "closed": 12,
        "superseded": 13,
        "archived": 14,
        "cancelled": 15,
        "canceled": 15,
    }.get(status, 9)
    priority = priority_sort_value(row.get("priority"))
    try:
        updated = float(row.get("updated_at") or row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        updated = 0.0
    return (status_rank, priority, -updated, str(row.get("work_id") or ""))


def board_recent_rank(row: dict[str, Any]) -> tuple[float, float, str]:

    try:
        updated = float(row.get("updated_at") or row.get("created_at") or 0.0)
    except (TypeError, ValueError):
        updated = 0.0
    priority = priority_sort_value(row.get("priority"))
    return (-updated, priority, str(row.get("work_id") or ""))


_BOARD_SCAN_CHUNK = 500


def _board_scan_chunks(
    conn,
    chunk_size: int,
) -> Iterator[list[dict]]:
    """Yield ``v_work_owner`` rows in board order, in bounded chunks.

    Keyset paging rather than OFFSET: ``(updated_at DESC, work_id ASC)`` is a
    total order because ``work_id`` is the primary key, so resuming after the
    last row of a chunk reproduces exactly the sequence one unbounded scan
    would have produced -- and unlike OFFSET it does not re-sort the whole
    table once per chunk.
    """
    cursor_updated: float | None = None
    cursor_work_id: str | None = None
    while True:
        if cursor_updated is None:
            batch = conn.execute(
                "SELECT * FROM v_work_owner"
                " ORDER BY updated_at DESC, work_id ASC LIMIT ?",
                (chunk_size,),
            ).fetchall()
        else:
            batch = conn.execute(
                "SELECT * FROM v_work_owner"
                " WHERE updated_at < ? OR (updated_at = ? AND work_id > ?)"
                " ORDER BY updated_at DESC, work_id ASC LIMIT ?",
                (cursor_updated, cursor_updated, cursor_work_id, chunk_size),
            ).fetchall()
        if not batch:
            return
        yield [dict(r) for r in batch]
        if len(batch) < chunk_size:
            return
        cursor_updated = batch[-1]["updated_at"]
        cursor_work_id = batch[-1]["work_id"]


def board_rows(
    conn,
    at: float | None = None,
    group_by: str = "module",
    limit: int | None = None,
    status_filter: str | Iterable[str] | None = None,
) -> list[dict]:
    from .review_tier import normalize_declared_tier

    at = at if at is not None else db_now(conn)
    if isinstance(status_filter, str):
        wanted_statuses = (
            {status_filter.strip().lower()} if status_filter.strip() else set()
        )
    elif status_filter is None:
        wanted_statuses = set()
    else:
        wanted_statuses = {
            str(value).strip().lower() for value in status_filter if str(value).strip()
        }
    want_limit = None if limit is None else max(0, int(limit))
    # Status is DERIVED, so a status filter cannot be pushed into SQL. Scanning
    # the whole table to find the first N matches is what made the filtered
    # board O(all work items); instead walk it in bounded chunks and stop at the
    # Nth match. The scan order is the same, so the rows returned are the same.
    if wanted_statuses or want_limit is None:
        scan_chunk = _BOARD_SCAN_CHUNK
    else:
        scan_chunk = want_limit
    pid_rows = conn.execute(
        "SELECT work_id, pid, pid_started_at, host_id FROM runs"
        " WHERE state='live' AND work_id IS NOT NULL AND pid IS NOT NULL"
    ).fetchall()
    live_pid_by_work: dict[str, int] = {}
    seen_pid_work: set[str] = set()
    foreign_host_work: set[str] = set()
    for pr in pid_rows:
        wid = str(pr["work_id"] or "")
        if not wid:
            continue
        # A run recorded on another host gets no pid verdict: its pid names a
        # process on a machine this probe cannot see, so answering would mean
        # answering "dead" for something healthy.
        if not pid_liveness_is_meaningful(pr["host_id"]):
            foreign_host_work.add(wid)
            continue
        seen_pid_work.add(wid)
        if pid_matches(pr["pid"], pr["pid_started_at"]):
            live_pid_by_work[wid] = live_pid_by_work.get(wid, 0) + 1
    # One unanswerable run makes the whole work item's pid count unanswerable:
    # a local dead run beside a live remote one would otherwise report 0 and
    # read as "not running". Dropping the work id from seen_pid_work is the
    # UNKNOWN answer -- derive_work_status then falls back to live_run_count,
    # i.e. the lease/heartbeat state, rather than a pid that means nothing here.
    seen_pid_work -= foreign_host_work
    for wid in foreign_host_work:
        live_pid_by_work.pop(wid, None)
    rows: list[dict] = []
    group_by_checked = False
    for chunk in _board_scan_chunks(conn, scan_chunk):
        for r in chunk:
            wid = str(r.get("work_id") or "")
            if wid in seen_pid_work:
                r["live_pid_count"] = live_pid_by_work.get(wid, 0)
            r["legacy_pre_review_tier_done"] = bool(
                not normalize_declared_tier(r.get("tier"))
                and str(r.get("intent_state") or "").strip().lower() == "done"
                and float(r.get("created_at") or 0.0) < _REVIEW_TIER_POLICY_ENFORCED_AT
            )
            r["has_artifact"] = bool(
                r.get("has_artifact")
                or (
                    r["legacy_pre_review_tier_done"]
                    and done_signal_satisfied(conn, r.get("done_signal"), HARNESS_ROOT)
                )
            )
            r["effective_tier"] = effective_review_tier_for_work(conn, wid, row=r)
            r["operator_ok_validated"] = _has_valid_operator_ok_unlocked(
                conn, wid, work_row=r
            )
            r["status"] = derive_work_status(r, at)
            r["proof_state"] = derive_proof_state(r, at)
            r["group"] = r.get(group_by) or "(ungrouped)"
        # A group_by naming a field no row carries used to sort every row into
        # "(ungrouped)" and return success, so a typo and a real grouping were
        # indistinguishable in the output. Refuse instead, and say what can be
        # grouped -- the caller cannot discover that from a silent all-ungrouped
        # board. Checked on the first row scanned, which is the same row the
        # unbounded scan checked.
        if not group_by_checked:
            group_by_checked = True
            if group_by not in chunk[0]:
                groupable = sorted(
                    k for k, v in chunk[0].items()
                    if isinstance(v, (str, type(None))) and not k.startswith("_")
                )
                raise ValueError(
                    f"cannot group the board by {group_by!r}: no such field on a "
                    f"work row; try one of: {', '.join(groupable)}"
                )
        if wanted_statuses:
            chunk = [
                r
                for r in chunk
                if str(r.get("status") or "").lower() in wanted_statuses
            ]
        rows.extend(chunk)
        if want_limit is not None and len(rows) >= want_limit:
            del rows[want_limit:]
            break
    return rows


def session_rollup(conn, at: float | None = None) -> list[dict]:
    at = at if at is not None else db_now(conn)
    out = []
    for r in conn.execute("SELECT * FROM v_session_rollup").fetchall():
        d = dict(r)
        d["live"] = bool(d.get("lease_until") and d["lease_until"] > at)
        out.append(d)
    return out


_CLAIM_CAPSULE_DECISION_KINDS: frozenset[str] = frozenset(
    {
        "decision",
        "handoff",
        "audit_request",
        "audit_verdict",
        "note",
        "claude_block",
        "codex_block",
        "claude_park",
        "codex_park",
        "done",
        "DONE",
        "claude_done",
        "codex_done",
        "claude_claim",
        "codex_claim",
    }
)
_CLAIM_CAPSULE_EVENT_LIMIT = 5
_CLAIM_CAPSULE_SIBLING_LIMIT = 5
_CLAIM_CAPSULE_ACCEPTANCE_ITEM_LIMIT = 5
_CLAIM_CAPSULE_SNIPPET_LIMIT = 100
_CLAIM_CAPSULE_FIELD_LIMIT = 200
_CLAIM_CAPSULE_MAX_BYTES = 2048
_CLAIM_CAPSULE_PAYLOAD_KEYS = (
    "task",
    "why",
    "summary",
    "reason",
    "message",
    "note",
    "verdict",
    "action",
    "next_step",
    "resume_when",
)
_CLAIM_CAPSULE_SIBLING_STATES = (
    "blocked",
    "paused",
    "held",
    "hold",
    "queued",
    "planned",
)


def _claim_capsule_one_line(value: Any, *, limit: int = _CLAIM_CAPSULE_SNIPPET_LIMIT) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _claim_capsule_payload_snippet(payload_json: Any) -> str:
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in _CLAIM_CAPSULE_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _claim_capsule_one_line(value)
    for key in sorted(payload.keys()):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _claim_capsule_one_line(value)
    return ""


def _claim_capsule_decision_fields(payload_json: Any) -> tuple[str, str]:
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    ruling = payload.get("ruling")
    scope = payload.get("scope")
    ruling = str(ruling).strip() if isinstance(ruling, str) else ""
    scope = str(scope).strip() if isinstance(scope, str) else ""
    return ruling, scope


def _claim_capsule_event_line(row: dict[str, Any]) -> str:
    try:
        date = time.strftime("%Y-%m-%d", time.gmtime(float(row.get("ts") or 0.0)))
    except (TypeError, ValueError, OSError):
        date = "0000-00-00"
    kind = str(row.get("kind") or "").strip() or "event"
    actor = str(row.get("actor") or "").strip() or "system"
    snippet = str(row.get("title") or "").strip() or str(row.get("body") or "").strip()
    snippet = _claim_capsule_one_line(snippet) if snippet else ""
    scope = ""
    if kind == "decision":
        ruling, scope = _claim_capsule_decision_fields(row.get("payload_json"))
        if not snippet and ruling:
            snippet = _claim_capsule_one_line(ruling)
    if not snippet:
        snippet = _claim_capsule_payload_snippet(row.get("payload_json"))
    line = f"{date} {kind} {actor}"
    if snippet:
        line += f": {snippet}"
    if kind == "decision" and scope and scope != "global":
        line += f" [{scope}]"
    return line


def _claim_capsule_shrink_to_budget(capsule: dict[str, Any]) -> dict[str, Any]:
    try:
        if len(json.dumps(capsule, sort_keys=True)) <= _CLAIM_CAPSULE_MAX_BYTES:
            return capsule
    except Exception:
        return capsule
    for key, keep_counts in (
        ("siblings", (3, 1)),
        ("recent_decisions", (3, 1)),
    ):
        values = capsule.get(key)
        if not isinstance(values, list):
            continue
        for keep in keep_counts:
            if len(values) <= keep:
                break
            capsule[key] = values[:keep]
            try:
                if len(json.dumps(capsule, sort_keys=True)) <= _CLAIM_CAPSULE_MAX_BYTES:
                    return capsule
            except Exception:
                return capsule
            values = capsule[key]
    acceptance = capsule.get("acceptance")
    if isinstance(acceptance, dict) and isinstance(acceptance.get("criteria"), list):
        acceptance["criteria"] = acceptance["criteria"][:2]
    try:
        if len(json.dumps(capsule, sort_keys=True)) <= _CLAIM_CAPSULE_MAX_BYTES:
            return capsule
    except Exception:
        return capsule
    capsule.pop("siblings", None)
    return capsule


def build_claim_context_capsule(conn, work_id: str) -> dict[str, Any] | None:
    try:
        work_id = str(work_id or "").strip()
        if not work_id:
            return None
        capsule: dict[str, Any] = {}

        try:
            row = conn.execute(
                "SELECT next_step, resume_when FROM work_items WHERE work_id=?",
                (work_id,),
            ).fetchone()
            if row is not None:
                next_step = str(row["next_step"] or "").strip()
                resume_when = str(row["resume_when"] or "").strip()
                if next_step or resume_when:
                    intent: dict[str, Any] = {}
                    if next_step:
                        intent["next_step"] = _claim_capsule_one_line(
                            next_step, limit=_CLAIM_CAPSULE_FIELD_LIMIT
                        )
                    if resume_when:
                        intent["resume_when"] = _claim_capsule_one_line(
                            resume_when, limit=_CLAIM_CAPSULE_FIELD_LIMIT
                        )
                    capsule["resume_intent"] = intent
        except Exception:
            pass

        try:
            row = conn.execute(
                "SELECT acceptance_json, done_signal, tier FROM work_items WHERE work_id=?",
                (work_id,),
            ).fetchone()
            if row is not None:
                done_signal = str(row["done_signal"] or "").strip()
                tier = str(row["tier"] or "").strip()
                raw_acceptance = row["acceptance_json"]
                parsed_acceptance: Any = None
                if raw_acceptance:
                    try:
                        parsed_acceptance = json.loads(raw_acceptance)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed_acceptance = str(raw_acceptance)
                block: dict[str, Any] = {}
                if isinstance(parsed_acceptance, list) and parsed_acceptance:
                    block["criteria"] = [
                        _claim_capsule_one_line(item, limit=140)
                        for item in parsed_acceptance[:_CLAIM_CAPSULE_ACCEPTANCE_ITEM_LIMIT]
                    ]
                elif isinstance(parsed_acceptance, str) and parsed_acceptance.strip():
                    block["criteria"] = _claim_capsule_one_line(
                        parsed_acceptance, limit=_CLAIM_CAPSULE_FIELD_LIMIT
                    )
                elif parsed_acceptance:
                    block["criteria"] = _claim_capsule_one_line(
                        json.dumps(parsed_acceptance), limit=_CLAIM_CAPSULE_FIELD_LIMIT
                    )
                if done_signal:
                    block["done_signal"] = _claim_capsule_one_line(
                        done_signal, limit=_CLAIM_CAPSULE_FIELD_LIMIT
                    )
                if tier:
                    block["tier"] = tier
                if block:
                    capsule["acceptance"] = block
        except Exception:
            pass

        try:
            kinds = sorted(_CLAIM_CAPSULE_DECISION_KINDS)
            placeholders = ",".join("?" for _ in kinds)
            fetch_limit = min(50, _CLAIM_CAPSULE_EVENT_LIMIT * 4)
            rows = conn.execute(
                "SELECT event_id, ts, kind, actor, title, body, payload_json FROM events"
                f" WHERE work_id=? AND kind IN ({placeholders})"
                " ORDER BY (kind = 'decision') DESC, event_id DESC LIMIT ?",
                (work_id, *kinds, fetch_limit),
            ).fetchall()
            live_decision_ids = _decision_head_event_ids(conn)
            filtered = [
                r for r in rows
                if str(r["kind"]) != "decision" or int(r["event_id"]) in live_decision_ids
            ]
            lines = [
                _claim_capsule_event_line(dict(r))
                for r in filtered[:_CLAIM_CAPSULE_EVENT_LIMIT]
            ]
            lines = [line for line in lines if line]
            if lines:
                capsule["recent_decisions"] = lines
        except Exception:
            pass

        try:
            parent_row = conn.execute(
                "SELECT parent_id FROM work_items WHERE work_id=?", (work_id,)
            ).fetchone()
            parent_id = str(parent_row["parent_id"] or "").strip() if parent_row else ""
            if parent_id:
                state_placeholders = ",".join("?" for _ in _CLAIM_CAPSULE_SIBLING_STATES)
                sib_rows = conn.execute(
                    "SELECT work_id, intent_state FROM work_items"
                    " WHERE parent_id=? AND work_id!=? AND archived_at IS NULL"
                    f" AND lower(COALESCE(intent_state,'')) IN ({state_placeholders})"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (
                        parent_id,
                        work_id,
                        *_CLAIM_CAPSULE_SIBLING_STATES,
                        _CLAIM_CAPSULE_SIBLING_LIMIT,
                    ),
                ).fetchall()
                siblings = [
                    {"work_id": r["work_id"], "intent_state": r["intent_state"]}
                    for r in sib_rows
                ]
                if siblings:
                    capsule["siblings"] = siblings
        except Exception:
            pass

        if not capsule:
            return None
        return _claim_capsule_shrink_to_budget(capsule)
    except Exception:
        return None


_DECISION_SCOPE_GLOBAL = "global"


def normalize_decision_scope(scope: str | None) -> str:
    s = str(scope or "").strip() or _DECISION_SCOPE_GLOBAL
    if s == _DECISION_SCOPE_GLOBAL:
        return s
    for prefix in ("initiative:", "module:"):
        if s.startswith(prefix) and s[len(prefix):].strip():
            return s
    raise ValueError(
        f"invalid decision scope {scope!r}; use 'global', 'initiative:<id>', "
        "or 'module:<key>'"
    )


_DECISION_STALE_IF_LIMIT = 16
_DECISION_STALE_IF_FIELD_LIMIT = 200


def post_decision_event(
    conn,
    *,
    ruling: str,
    actor: str | None,
    session_id: str | None,
    work_id: str | None = None,
    binds: Iterable[str] | None = None,
    scope: str = _DECISION_SCOPE_GLOBAL,
    refs: Iterable[str] | None = None,
    source: str = "coord_db.post_decision_event",
    supersedes_event_id: int | str | None = None,
    valid_from: float | None = None,
    stale_if: Iterable[str] | None = None,
    memory_candidate: bool = False,
) -> int | None:
    clean_ruling = str(ruling or "").strip()
    if not clean_ruling:
        raise ValueError("decision requires a non-empty ruling")
    if not isinstance(memory_candidate, bool):
        raise ValueError("decision memory_candidate must be boolean")
    scope = normalize_decision_scope(scope)
    clean_binds = [str(b).strip() for b in (binds or []) if str(b).strip()]
    clean_refs = [str(r).strip() for r in (refs or []) if str(r).strip()]
    clean_stale_if = [
        str(s).strip()[:_DECISION_STALE_IF_FIELD_LIMIT]
        for s in (stale_if or [])
        if str(s).strip()
    ][:_DECISION_STALE_IF_LIMIT]
    wid = str(work_id or "").strip() or None
    if wid is not None:
        row = conn.execute(
            "SELECT 1 FROM work_items WHERE work_id=?", (wid,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"decision work_id not found: {wid}; omit work_id for a global ruling"
            )
    clean_supersedes: int | None = None
    if supersedes_event_id is not None and str(supersedes_event_id).strip():
        try:
            clean_supersedes = int(supersedes_event_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"supersedes_event_id is not an integer: {supersedes_event_id!r}"
            ) from exc
        prior = conn.execute(
            "SELECT kind FROM events WHERE event_id=?", (clean_supersedes,)
        ).fetchone()
        if prior is None or str(prior["kind"]) != "decision":
            raise ValueError(
                f"supersedes_event_id does not reference an existing decision "
                f"event: {supersedes_event_id!r}"
            )
    clean_valid_from: float | None = None
    if valid_from is not None:
        try:
            clean_valid_from = float(valid_from)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"valid_from is not numeric: {valid_from!r}") from exc
    payload = {
        "ruling": clean_ruling,
        "binds": clean_binds,
        "scope": scope,
        "schema_version": 1,
        "source": source,
    }
    if memory_candidate:
        payload["memory_candidate"] = {
            "schema": "coordharness.memory-candidate.v1",
            "kind": "fact",
        }
    if clean_supersedes is not None:
        payload["supersedes_event_id"] = clean_supersedes
    if clean_valid_from is not None:
        payload["valid_from"] = clean_valid_from
    if clean_stale_if:
        payload["stale_if"] = clean_stale_if
    title = _claim_capsule_one_line(clean_ruling, limit=_CLAIM_CAPSULE_FIELD_LIMIT)
    return post_event(
        conn,
        kind="decision",
        actor=actor,
        session_id=session_id,
        to_selector=None,
        work_id=wid,
        title=title,
        refs_json=json.dumps(clean_refs),
        payload_json=json.dumps(payload, sort_keys=True),
    )


def _decision_event_payload(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _decision_is_valid_at(
    payload: dict[str, Any],
    *,
    as_of: float,
    stale_fences: frozenset[str],
) -> bool:
    valid_from = payload.get("valid_from")
    if valid_from is not None:
        try:
            if float(valid_from) > as_of:
                return False
        except (TypeError, ValueError):
            pass
    stale_if = payload.get("stale_if")
    if isinstance(stale_if, list) and stale_fences:
        if any(str(fence) in stale_fences for fence in stale_if):
            return False
    return True


_DECISION_CHAIN_WALK_MAX_DEPTH = 200


def _decision_supersession_cycle_members(
    versions: dict[int, dict[str, Any]]
) -> set[int]:
    parent_of: dict[int, int] = {}
    for eid, v in versions.items():
        parent = v.get("supersedes_event_id")
        if parent is not None and int(parent) in versions:
            parent_of[eid] = int(parent)

    status: dict[int, int] = {}
    cyclic: set[int] = set()
    for start in parent_of:
        if start in status:
            continue
        path: list[int] = []
        cursor = start
        while cursor in parent_of and cursor not in status:
            status[cursor] = 0
            path.append(cursor)
            cursor = parent_of[cursor]
        if cursor in status and status[cursor] == 0:
            idx = path.index(cursor)
            cyclic.update(path[idx:])
        for node in path:
            status[node] = 1
    return cyclic


def _resolve_decision_heads_and_conflicts(
    conn,
    *,
    work_id: str | None = None,
    scope: str | None = None,
    as_of: float | None = None,
    stale_fences: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved_as_of = time.time() if as_of is None else float(as_of)
    fences = frozenset(str(f) for f in (stale_fences or []))
    clean_wid = str(work_id or "").strip() or None
    clean_scope = str(scope or "").strip() or None
    if clean_scope is not None:
        clean_scope = normalize_decision_scope(clean_scope)

    rows = conn.execute(
        "SELECT event_id, ts, actor, work_id, refs_json, payload_json FROM events"
        " WHERE kind='decision' ORDER BY event_id ASC"
    ).fetchall()

    versions: dict[int, dict[str, Any]] = {}
    for r in rows:
        payload = _decision_event_payload(r)
        eid = int(r["event_id"])
        versions[eid] = {
            "event_id": eid,
            "ts": r["ts"],
            "actor": r["actor"],
            "work_id": r["work_id"],
            "scope": payload.get("scope"),
            "ruling": payload.get("ruling"),
            "binds": payload.get("binds") or [],
            "refs": json.loads(r["refs_json"]) if r["refs_json"] else [],
            "supersedes_event_id": payload.get("supersedes_event_id"),
            "valid_from": payload.get("valid_from"),
            "stale_if": payload.get("stale_if") or [],
            "_payload": payload,
        }

    cyclic_members = _decision_supersession_cycle_members(versions)
    if cyclic_members:
        _logger.warning(
            "coord_db: resolve_decision_heads found a closed decision-supersession"
            " cycle with no way to walk out of it (event_ids=%s) — every member is"
            " excluded from head resolution until the underlying data is corrected;"
            " this data shape cannot come from post_decision_event and indicates a"
            " raw-SQL write bypassed its supersedes_event_id existence check",
            sorted(cyclic_members),
        )

    superseded_ids: set[int] = set()
    for v in versions.values():
        parent = v.get("supersedes_event_id")
        if parent is not None and int(parent) in versions:
            superseded_ids.add(int(parent))
    all_terminals = [eid for eid in versions if eid not in superseded_ids]

    terminal_winner_of_parent: dict[int, int] = {}
    for eid in all_terminals:
        parent = versions[eid].get("supersedes_event_id")
        if parent is not None and int(parent) in versions:
            existing = terminal_winner_of_parent.get(int(parent))
            if existing is None or eid > existing:
                terminal_winner_of_parent[int(parent)] = eid

    conflicts: list[dict[str, Any]] = []
    resolvable_terminals: list[int] = []
    for eid in all_terminals:
        parent = versions[eid].get("supersedes_event_id")
        if parent is not None and int(parent) in versions:
            winner = terminal_winner_of_parent.get(int(parent))
            if winner is not None and winner != eid:
                conflicts.append({
                    "event_id": eid,
                    "supersedes_event_id": int(parent),
                    "lost_to_event_id": winner,
                })
                continue
        resolvable_terminals.append(eid)

    heads: list[dict[str, Any]] = []
    for terminal_id in resolvable_terminals:
        cursor: int | None = terminal_id
        chosen: dict[str, Any] | None = None
        seen: set[int] = set()
        depth = 0
        while cursor is not None:
            if cursor in seen:
                _logger.warning(
                    "coord_db: resolve_decision_heads cycle detected walking back"
                    " from terminal event_id=%s (revisited event_id=%s); treating"
                    " this chain as headless rather than hanging (mirrors"
                    " _walk_parent_chain_for_cycle_unlocked's seen-set guard)",
                    terminal_id, cursor,
                )
                chosen = None
                break
            seen.add(cursor)
            depth += 1
            if depth > _DECISION_CHAIN_WALK_MAX_DEPTH:
                _logger.warning(
                    "coord_db: resolve_decision_heads walk-back from terminal"
                    " event_id=%s exceeded max depth=%s; treating this chain as"
                    " headless rather than hanging on a pathological chain",
                    terminal_id, _DECISION_CHAIN_WALK_MAX_DEPTH,
                )
                chosen = None
                break
            candidate = versions.get(cursor)
            if candidate is None:
                break
            if _decision_is_valid_at(
                candidate["_payload"], as_of=resolved_as_of, stale_fences=fences
            ):
                chosen = candidate
                break
            parent = candidate.get("supersedes_event_id")
            cursor = int(parent) if parent is not None and int(parent) in versions else None
        if chosen is None:
            continue
        if clean_wid is not None and str(chosen.get("work_id") or "") != clean_wid:
            continue
        if clean_scope is not None and str(chosen.get("scope") or "") != clean_scope:
            continue
        out = {k: v for k, v in chosen.items() if k != "_payload"}
        heads.append(out)

    heads.sort(key=lambda h: h["event_id"], reverse=True)
    return heads, conflicts


def resolve_decision_heads(
    conn,
    *,
    work_id: str | None = None,
    scope: str | None = None,
    as_of: float | None = None,
    stale_fences: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    heads, _conflicts = _resolve_decision_heads_and_conflicts(
        conn, work_id=work_id, scope=scope, as_of=as_of, stale_fences=stale_fences,
    )
    return heads


def _decision_immediate_successor_event_id(conn, event_id: int) -> int | None:
    try:
        target = int(event_id)
    except (TypeError, ValueError):
        return None
    rows = conn.execute(
        "SELECT event_id, payload_json FROM events WHERE kind='decision'"
        " ORDER BY event_id ASC"
    ).fetchall()
    winner: int | None = None
    for r in rows:
        payload = _decision_event_payload(r)
        parent = payload.get("supersedes_event_id")
        if parent is not None and int(parent) == target:
            eid = int(r["event_id"])
            if winner is None or eid > winner:
                winner = eid
    return winner


def _decision_head_event_ids(
    conn,
    *,
    as_of: float | None = None,
    stale_fences: Iterable[str] | None = None,
) -> frozenset[int]:
    try:
        heads = resolve_decision_heads(conn, as_of=as_of, stale_fences=stale_fences)
    except Exception:
        return frozenset()
    return frozenset(int(h["event_id"]) for h in heads)


_CLOSEOUT_EVENT_KIND = "session_closeout"
_DIRECTED_CLOSEOUT_KINDS = ("handoff", "audit_request", "continuation_ready")
_CLOSEOUT_ROWS_TOUCHED_CAP = 40
_MEMORY_PROPOSAL_FAILURE_KIND = "memory_proposal_failure"
_MEMORY_PROPOSAL_REPLAY_KIND = "memory_proposal_replay"
_MEMORY_PROPOSAL_FAILURE_SCHEMA = "coordharness.memory-proposal-failure.v1"
_MEMORY_PROPOSAL_REPLAY_SCHEMA = "coordharness.memory-proposal-replay.v1"


def _knowledge_db_binding(db_path: str | Path) -> dict[str, str]:
    from coordharness import config as harness_config

    resolved = Path(db_path).resolve(strict=False)
    return {
        "database_ref": harness_config.public_path_ref(resolved),
        "path_binding_sha256": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
    }


def _record_memory_proposal_failures(
    conn,
    failures: Iterable[dict[str, Any]],
    *,
    actor: str,
    session_id: str,
    knowledge_db_path: str | Path,
) -> list[int]:
    receipt_ids: list[int] = []
    knowledge_binding = _knowledge_db_binding(knowledge_db_path)
    for failure in failures:
        candidate = failure.get("candidate")
        coordination_source = failure.get("coordination_source")
        if not isinstance(candidate, dict) or not isinstance(coordination_source, dict):
            continue
        payload = {
            "schema": _MEMORY_PROPOSAL_FAILURE_SCHEMA,
            "candidate": candidate,
            "coordination_source": coordination_source,
            "knowledge_db": knowledge_binding,
            "failure": {
                "reason": failure.get("reason"),
                "detail": failure.get("detail"),
            },
        }
        receipt_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        idempotency_key = f"memory-proposal-failure:{receipt_hash}"
        event_id = post_event(
            conn,
            kind=_MEMORY_PROPOSAL_FAILURE_KIND,
            actor=actor,
            session_id=session_id,
            work_id=candidate.get("work_id"),
            title=f"Memory proposal pending for decision #{candidate.get('event_id')}",
            payload_json=json.dumps(payload, sort_keys=True),
            idempotency_key=idempotency_key,
        )
        if event_id is None:
            prior = conn.execute(
                "SELECT event_id FROM events WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if prior is None:
                raise RuntimeError("memory proposal failure receipt replay is missing")
            event_id = int(prior["event_id"])
        failure["receipt_event_id"] = int(event_id)
        receipt_ids.append(int(event_id))
    return receipt_ids


def _record_memory_proposal_session_failure(
    conn,
    *,
    actor: str,
    session_id: str,
    session_ids: Iterable[str],
    current_decision_ids: Iterable[int],
    knowledge_db_path: str | Path,
    error: Exception,
) -> int:
    payload = {
        "schema": _MEMORY_PROPOSAL_FAILURE_SCHEMA,
        "mode": "session_scan",
        "source_session_id": session_id,
        "source_session_ids": sorted({str(value) for value in session_ids}),
        "current_decision_ids": sorted({int(value) for value in current_decision_ids}),
        "knowledge_db": _knowledge_db_binding(knowledge_db_path),
        "failure": {"reason": "session_producer_failed", "detail": str(error)},
    }
    receipt_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    idempotency_key = f"memory-proposal-failure:{receipt_hash}"
    event_id = post_event(
        conn,
        kind=_MEMORY_PROPOSAL_FAILURE_KIND,
        actor=actor,
        session_id=session_id,
        title=f"Memory proposal session scan pending for {session_id}",
        payload_json=json.dumps(payload, sort_keys=True),
        idempotency_key=idempotency_key,
    )
    if event_id is None:
        prior = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if prior is None:
            raise RuntimeError("memory proposal session failure receipt replay is missing")
        event_id = int(prior["event_id"])
    return int(event_id)


def replay_memory_proposal_failures(
    conn,
    *,
    knowledge_db_path: str | Path,
    actor: str,
    session_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Retry unresolved producer failures and durably terminate each success/refusal."""

    from coordharness.knowledge.proposal_producer import (
        produce_memory_candidate,
        produce_session_memory_proposals,
    )

    rows = conn.execute(
        "SELECT e.event_id,e.payload_json FROM events e"
        " WHERE e.kind=?"
        " AND NOT EXISTS (SELECT 1 FROM events r WHERE r.kind=?"
        " AND CAST(json_extract(r.payload_json,'$.failure_event_id') AS INTEGER)=e.event_id)"
        " ORDER BY e.event_id LIMIT ?",
        (_MEMORY_PROPOSAL_FAILURE_KIND, _MEMORY_PROPOSAL_REPLAY_KIND, max(1, int(limit))),
    ).fetchall()
    current_binding = _knowledge_db_binding(knowledge_db_path)
    current_decision_ids = _decision_head_event_ids(conn)
    resolved: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    for row in rows:
        failure_event_id = int(row["event_id"])
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            still_pending.append(
                {"failure_event_id": failure_event_id, "reason": "malformed_failure_receipt"}
            )
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != _MEMORY_PROPOSAL_FAILURE_SCHEMA
            or payload.get("knowledge_db") != current_binding
        ):
            still_pending.append(
                {"failure_event_id": failure_event_id, "reason": "binding_or_schema_mismatch"}
            )
            continue
        if payload.get("mode") == "session_scan":
            stored_decision_ids = {
                int(value) for value in (payload.get("current_decision_ids") or [])
            }
            replay_decision_ids = sorted(stored_decision_ids & current_decision_ids)
            try:
                production = produce_session_memory_proposals(
                    conn,
                    session_id=str(payload.get("source_session_id") or ""),
                    session_ids=payload.get("source_session_ids") or [],
                    db_path=knowledge_db_path,
                    current_decision_ids=replay_decision_ids,
                )
            except Exception as exc:
                still_pending.append(
                    {
                        "failure_event_id": failure_event_id,
                        "reason": "session_producer_failed",
                        "detail": str(exc),
                    }
                )
                continue
            if production.get("failed"):
                still_pending.append(
                    {
                        "failure_event_id": failure_event_id,
                        "reason": "candidate_production_failed",
                        "failed": production["failed"],
                    }
                )
                continue
            outcome = {
                "status": "batch_resolved",
                "production": production,
                "stale_decision_ids": sorted(stored_decision_ids - current_decision_ids),
            }
            replay_work_id = None
        else:
            if not isinstance(payload.get("candidate"), dict) or not isinstance(
                payload.get("coordination_source"), dict
            ):
                still_pending.append(
                    {"failure_event_id": failure_event_id, "reason": "malformed_candidate_receipt"}
                )
                continue
            candidate = payload["candidate"]
            candidate_event_id = int(candidate.get("event_id") or 0)
            replay_work_id = candidate.get("work_id")
            if candidate_event_id not in current_decision_ids:
                outcome = {
                    "status": "stale",
                    "event_id": candidate_event_id,
                    "reason": "decision_superseded_or_no_longer_current",
                }
            else:
                outcome = produce_memory_candidate(
                    candidate,
                    db_path=knowledge_db_path,
                    coordination_source=payload["coordination_source"],
                )
        if outcome.get("status") == "failed":
            still_pending.append(
                {
                    "failure_event_id": failure_event_id,
                    "reason": outcome.get("reason"),
                    "detail": outcome.get("detail"),
                }
            )
            continue
        replay_payload = {
            "schema": _MEMORY_PROPOSAL_REPLAY_SCHEMA,
            "failure_event_id": failure_event_id,
            "outcome": outcome,
            "knowledge_db": current_binding,
        }
        replay_event_id = post_event(
            conn,
            kind=_MEMORY_PROPOSAL_REPLAY_KIND,
            actor=actor,
            session_id=session_id,
            work_id=replay_work_id,
            title=f"Memory proposal replay resolved failure #{failure_event_id}",
            payload_json=json.dumps(replay_payload, sort_keys=True),
            idempotency_key=f"memory-proposal-replay:{failure_event_id}",
        )
        if replay_event_id is None:
            prior = conn.execute(
                "SELECT event_id FROM events WHERE idempotency_key=?",
                (f"memory-proposal-replay:{failure_event_id}",),
            ).fetchone()
            if prior is None:
                raise RuntimeError("memory proposal replay receipt is missing")
            replay_event_id = int(prior["event_id"])
        resolved.append(
            {
                "failure_event_id": failure_event_id,
                "replay_event_id": int(replay_event_id),
                "outcome": outcome,
            }
        )
    return {"attempted": len(rows), "resolved": resolved, "still_pending": still_pending}


def _closeout_family(conn, session_id: str | None, actor: str | None) -> tuple[str, list[str]]:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_closeout requires a non-empty session")
    family = related_session_ids(conn, sid, actor=actor)
    if sid not in family:
        family = [*family, sid]
    return sid, sorted({s for s in family if str(s or "").strip()})


def _normalize_waived_events(waived_events: Iterable[Any] | None) -> dict[int, str]:
    waivers: dict[int, str] = {}
    for entry in waived_events or []:
        event_id: Any = None
        reason: Any = ""
        if isinstance(entry, dict):
            event_id = entry.get("event_id")
            reason = entry.get("reason", "")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 1:
            event_id = entry[0]
            reason = entry[1] if len(entry) > 1 else ""
        else:
            event_id = entry
        try:
            eid = int(event_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"waived event id is not an integer: {event_id!r}") from exc
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError(f"waived event #{eid} requires a non-empty reason")
        waivers[eid] = clean_reason
    return waivers


def closeout_inventory(conn, *, session_id: str, actor: str | None) -> dict[str, Any]:
    resolved_actor = str(actor or "").strip().lower() or None
    sid, family = _closeout_family(conn, session_id, resolved_actor)
    fph = ",".join("?" for _ in family)

    held_rows = conn.execute(
        f"SELECT claim_id, work_id, status, step FROM claims"
        f" WHERE session_id IN ({fph}) AND status IN ('running','paused','blocked')"
        f" ORDER BY work_id, claim_id",
        family,
    ).fetchall()
    held = [
        {"claim_id": r["claim_id"], "work_id": r["work_id"],
         "status": r["status"], "step": r["step"]}
        for r in held_rows
    ]

    touched_rows = conn.execute(
        f"SELECT DISTINCT work_id FROM events"
        f" WHERE session_id IN ({fph}) AND work_id IS NOT NULL AND TRIM(work_id) <> ''"
        f" ORDER BY work_id",
        family,
    ).fetchall()
    touched = [str(r["work_id"]) for r in touched_rows]

    unanswered: list[dict[str, Any]] = []
    if touched and resolved_actor in _lane_set():
        tph = ",".join("?" for _ in touched)
        kph = ",".join("?" for _ in _DIRECTED_CLOSEOUT_KINDS)
        rows = conn.execute(
            f"SELECT e.event_id, e.work_id, e.kind, e.actor, e.ts, e.title FROM events e"
            f" WHERE e.to_selector = ? AND e.kind IN ({kph}) AND e.work_id IN ({tph})"
            f"   AND NOT EXISTS (SELECT 1 FROM events e2 WHERE e2.work_id = e.work_id"
            f"     AND e2.session_id IN ({fph}) AND e2.event_id > e.event_id)"
            f" ORDER BY e.event_id",
            (f"actor:{resolved_actor}", *_DIRECTED_CLOSEOUT_KINDS, *touched, *family),
        ).fetchall()
        unanswered = [
            {"event_id": int(r["event_id"]), "work_id": r["work_id"], "kind": r["kind"],
             "from_actor": r["actor"], "title": r["title"]}
            for r in rows
        ]

    decision_rows = conn.execute(
        f"SELECT event_id FROM events WHERE kind='decision' AND session_id IN ({fph})"
        f" ORDER BY event_id",
        family,
    ).fetchall()
    decisions = [int(r["event_id"]) for r in decision_rows]

    disposed_rows = conn.execute(
        f"SELECT work_id, status, heartbeat_at, claim_id FROM claims"
        f" WHERE session_id IN ({fph})"
        f" ORDER BY work_id, heartbeat_at DESC, claim_id DESC",
        family,
    ).fetchall()
    seen: set[str] = set()
    disposed: list[dict[str, Any]] = []
    for r in disposed_rows:
        wid = str(r["work_id"])
        if wid in seen:
            continue
        seen.add(wid)
        if str(r["status"]) in _HELD_CLAIM_STATUSES:
            continue
        disposed.append({"work_id": wid, "final_state": str(r["status"])})

    return {
        "session_id": sid,
        "actor": resolved_actor,
        "family": family,
        "held_claims": held,
        "unanswered_directed_events": unanswered,
        "rows_touched": touched[:_CLOSEOUT_ROWS_TOUCHED_CAP],
        "decisions_posted": decisions,
        "claims_disposed": disposed,
    }


def session_closeout(
    conn,
    *,
    session_id: str,
    actor: str,
    summary: str,
    successor_hints: Iterable[str] | None = None,
    dead_ends: Iterable[str] | None = None,
    waived_events: Iterable[Any] | None = None,
    ack_dirty: bool = False,
    dirty_files: Iterable[str] | None = None,
    live_job_sidecars: Iterable[Any] | None = None,
    source: str = "coord_db.session_closeout",
    knowledge_db_path: str | Path | None = None,
) -> dict[str, Any]:
    clean_summary = str(summary or "").strip()
    if not clean_summary:
        raise ValueError("session_closeout requires a non-empty summary")
    resolved_actor = str(actor or "").strip().lower()
    if resolved_actor not in _lane_set():
        raise ValueError(f"session_closeout requires actor {_lanes_display()}")
    hints = [str(h).strip() for h in (successor_hints or []) if str(h).strip()]
    dead = [str(d).strip() for d in (dead_ends or []) if str(d).strip()]
    dirty = [str(f).strip() for f in (dirty_files or []) if str(f).strip()]
    sidecars = [s for s in (live_job_sidecars or [])]
    waivers = _normalize_waived_events(waived_events)

    sid, family = _closeout_family(conn, session_id, resolved_actor)
    fph = ",".join("?" for _ in family)

    prior = conn.execute(
        f"SELECT event_id FROM events WHERE kind=? AND session_id IN ({fph})"
        f" ORDER BY event_id DESC LIMIT 1",
        (_CLOSEOUT_EVENT_KIND, *family),
    ).fetchone()
    if prior is not None:
        raise ValueError(
            f"session {sid} already closed out (event #{int(prior['event_id'])}); closeout"
            " is terminal — boot a fresh successor via `board_context.py successor --sessions`"
        )

    inv = closeout_inventory(conn, session_id=sid, actor=resolved_actor)

    blocked: list[str] = []
    if inv["held_claims"]:
        lines = "\n".join(
            f"    - {c['work_id']} [{c['status']}]" for c in inv["held_claims"]
        )
        blocked.append(
            "  held claims (run `done` with proof OR `park` with a resume predicate for"
            f" each first):\n{lines}"
        )
    unwaived = [
        e for e in inv["unanswered_directed_events"] if int(e["event_id"]) not in waivers
    ]
    if unwaived:
        lines = "\n".join(
            f"    - #{e['event_id']} {e['kind']} from {e['from_actor']} on {e['work_id']}"
            for e in unwaived
        )
        blocked.append(
            "  unanswered directed events (answer via note/verdict, or repeat"
            f' --waive-event <id> "reason"):\n{lines}'
        )
    if dirty and not ack_dirty:
        lines = "\n".join(f"    - {f}" for f in dirty[:50])
        blocked.append(
            "  dirty worktree (commit YOUR OWN files by explicit path first, then pass"
            f" --ack-dirty to acknowledge the remaining foreign files):\n{lines}"
        )
    if blocked:
        raise ValueError(
            f"session_closeout blocked for {sid}; resolve before closing:\n"
            + "\n".join(blocked)
        )

    matched_waivers = [
        {"event_id": int(e["event_id"]), "reason": waivers[int(e["event_id"])]}
        for e in inv["unanswered_directed_events"]
        if int(e["event_id"]) in waivers
    ]

    payload = {
        "schema_version": 1,
        "summary": clean_summary,
        "successor_hints": hints,
        "dead_ends": dead,
        "claims_disposed": inv["claims_disposed"],
        "rows_touched": inv["rows_touched"],
        "decisions_posted": inv["decisions_posted"],
        "dirty_files_present": dirty,
        "waived_events": matched_waivers,
        "live_job_sidecars": sidecars,
        "actor": resolved_actor,
        "source": source,
    }
    title = _claim_capsule_one_line(clean_summary, limit=_CLAIM_CAPSULE_FIELD_LIMIT)
    event_id = post_event(
        conn,
        kind=_CLOSEOUT_EVENT_KIND,
        actor=resolved_actor,
        session_id=sid,
        to_selector=None,
        title=title,
        refs_json=json.dumps([]),
        payload_json=json.dumps(payload, sort_keys=True),
    )
    end_session(conn, sid)
    from coordharness import config as harness_config

    resolved_knowledge_db = Path(
        knowledge_db_path or harness_config.knowledge_db_path()
    ).resolve(strict=False)
    current_decision_ids = _decision_head_event_ids(conn)
    try:
        replay_result = replay_memory_proposal_failures(
            conn,
            knowledge_db_path=resolved_knowledge_db,
            actor=resolved_actor,
            session_id=sid,
        )
    except Exception as exc:
        replay_result = {
            "attempted": 0,
            "resolved": [],
            "still_pending": [{"reason": "replay_scan_failed", "detail": str(exc)}],
        }
    try:
        from coordharness.knowledge.proposal_producer import (
            produce_session_memory_proposals,
        )

        proposal_production = produce_session_memory_proposals(
            conn,
            session_id=sid,
            session_ids=family,
            db_path=resolved_knowledge_db,
            current_decision_ids=current_decision_ids,
        )
        proposal_production["failure_receipts"] = _record_memory_proposal_failures(
            conn,
            proposal_production["failed"],
            actor=resolved_actor,
            session_id=sid,
            knowledge_db_path=resolved_knowledge_db,
        )
    except Exception as exc:
        # Proposal production is downstream of the closeout fence and must not
        # turn a successful terminal lifecycle action into a stranded session.
        failure_receipt = _record_memory_proposal_session_failure(
            conn,
            actor=resolved_actor,
            session_id=sid,
            session_ids=family,
            current_decision_ids=current_decision_ids,
            knowledge_db_path=resolved_knowledge_db,
            error=exc,
        )
        proposal_production = {
            "eligible": 0,
            "emitted": [],
            "rejected": [],
            "failed": [{"reason": "session_producer_failed", "detail": str(exc)}],
            "failure_receipts": [failure_receipt],
            "error": str(exc),
        }
    proposal_production["replay"] = replay_result
    return {
        "verb": "session_closeout",
        "event_id": event_id,
        "session_id": sid,
        "actor": resolved_actor,
        "claims_disposed": inv["claims_disposed"],
        "rows_touched": inv["rows_touched"],
        "decisions_posted": inv["decisions_posted"],
        "successor_hints": hints,
        "dead_ends": dead,
        "dirty_files_present": dirty,
        "waived_events": matched_waivers,
        "memory_proposals": proposal_production,
        "successor_pointer": (
            # Runnable from a clone: board_context lives inside the package and
            # has no console script, so name the module. The previous string
            # pointed at a coordharness/scripts/ directory that no checkout has.
            f"python -m coordharness.coord.board_context successor --sessions {sid}"
        ),
    }


def discover_dirty_worktree_files(cwd: str | None = None) -> list[str]:
    import subprocess

    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        workdir = root.stdout.strip() or cwd
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, cwd=workdir,
        )
        if out.returncode != 0:
            return []
        return [ln.rstrip("\n") for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def discover_closeout_job_sidecars(
    family: Iterable[str], *, job_progress_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    fam = {str(s or "").strip() for s in family if str(s or "").strip()}
    if not fam:
        return []
    base = Path(job_progress_dir) if job_progress_dir else (
        _repo_data_local_dir() / "job_progress"
    )
    out: list[dict[str, Any]] = []
    try:
        if not base.is_dir():
            return []
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            candidate = str(
                data.get("session_id") or data.get("session") or ""
            ).strip()
            if candidate and candidate in fam:
                out.append({
                    "sidecar": path.name,
                    "session_id": candidate,
                    "state": data.get("state") or data.get("status"),
                })
    except Exception:
        return out
    return out


def _repo_data_local_dir() -> Path:
    from coordharness import config as _harness_config

    return _harness_config.state_dir()


DEFAULT_CLAUDE_MEMORY_DIR = Path(
    os.environ.get("COORD_MEMORY_DIR", "")
) if os.environ.get("COORD_MEMORY_DIR") else None
DONE_PROPOSAL_MIN_CHARS = 200


def resolve_claude_memory_dir(memory_dir: str | Path | None = None) -> Path:
    if memory_dir:
        return Path(memory_dir)
    env = str(os.environ.get("COORD_CLAUDE_MEMORY_DIR") or "").strip()
    if env:
        return Path(env)
    return DEFAULT_CLAUDE_MEMORY_DIR


def _proposal_slug(work_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(work_id or "").strip())


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_done_memory_proposal(
    work_id: str,
    note: str | None,
    *,
    refs: Iterable[str] | None = None,
    actor: str | None = None,
    memory_dir: str | Path | None = None,
    date: str | None = None,
) -> str | None:
    try:
        text = str(note or "").strip()
        if len(text) < DONE_PROPOSAL_MIN_CHARS:
            return None
        wid = str(work_id or "").strip()
        if not wid:
            return None
        slug = _proposal_slug(wid)
        if not slug:
            return None
        proposals_dir = resolve_claude_memory_dir(memory_dir) / "_proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        if any(proposals_dir.glob(f"*-{slug}.md")):
            return None
        day = str(date or "").strip() or time.strftime("%Y-%m-%d", time.gmtime())
        path = proposals_dir / f"{day}-{slug}.md"
        if path.exists():
            return None
        clean_refs = [str(r).strip() for r in (refs or []) if str(r).strip()]
        description = _claim_capsule_one_line(text, limit=_CLAIM_CAPSULE_FIELD_LIMIT)
        lines = [
            "---",
            f"name: {slug.lower()}",
            f"description: {_yaml_quote(description)}",
            "type: project",
            "status: proposed",
            f"source_work_id: {wid}",
            f"source_actor: {str(actor or '').strip() or 'unknown'}",
            f"created: {day}",
        ]
        if clean_refs:
            lines.append("refs:")
            lines.extend(f"  - {ref}" for ref in clean_refs)
        lines.extend(
            [
                "---",
                "",
                "<!-- AUTO-DRAFT from a done/complete closing note; review before"
                " promoting into MEMORY.md. Never auto-accepted. -->",
                "",
                text,
                "",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def work_contract_schema(conn) -> None:
    from . import work_contracts

    work_contracts.ensure_schema(conn)


def completed_done_signal_lookup(conn, **kwargs):
    from . import work_contracts

    return work_contracts.lookup_completion(conn, **kwargs)


def declared_write_set_overlaps(conn, **kwargs):
    from . import work_contracts

    return work_contracts.write_set_overlaps(conn, **kwargs)


def fleet_child_attempts(conn, **kwargs):
    from . import work_contracts

    return work_contracts.child_attempts(conn, **kwargs)
