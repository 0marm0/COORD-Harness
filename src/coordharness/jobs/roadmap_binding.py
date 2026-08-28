"""Read-only authority validation for standalone tracked launches.

This module never bootstraps a database or creates lifecycle state. A launcher
must bind an existing work row to an exact live claim and active session before
it may create a process, run row, or sidecar.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import quote

from coordharness import config

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
    }
)
_SHARED_ASSIGNEES = frozenset({"", "shared", "any", "unassigned"})
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REQUIRED_TABLES = frozenset({"work_items", "claims", "agent_sessions", "runs"})


@dataclass(frozen=True)
class RuntimeBinding:
    work_id: str
    job_id: str
    claim_id: str
    claim_fence: str
    session_id: str
    actor: str
    assignee: str
    intent_state: str
    coord_db: Path


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""
    binding: RuntimeBinding | None = None


def resolve_coord_db_path(
    explicit: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit > COORD_DB > COORD_COORD_DB > config default."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    values = os.environ if env is None else env
    override = values.get("COORD_DB") or values.get("COORD_COORD_DB")
    if override:
        return Path(override).expanduser()
    return config.coord_db_path()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise ValueError("coord database must be an existing regular non-symlink file")
    absolute = path.resolve(strict=True)
    uri = f"file:{quote(str(absolute), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not _REQUIRED_TABLES <= tables:
        conn.close()
        raise ValueError("coord database lacks standalone lifecycle tables")
    return conn


def _query_binding(
    conn: sqlite3.Connection,
    *,
    clean_work: str,
    clean_job: str,
    clean_claim: str,
    clean_fence: str,
    clean_session: str,
    clean_actor: str,
    path: Path,
    now: float | None,
) -> ValidationResult:
    try:
        row = conn.execute(
            "SELECT w.work_id,w.intent_state,w.archived_at,w.assignee,"
            " c.claim_id,c.work_id AS claim_work_id,c.session_id AS claim_session_id,"
            " c.status AS claim_status,c.lease_token,c.expires_at,"
            " s.actor AS session_actor,s.state AS session_state,s.lease_until"
            " FROM work_items w"
            " LEFT JOIN claims c ON c.claim_id=? AND c.work_id=w.work_id"
            " LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
            " WHERE w.work_id=?",
            (clean_claim, clean_work),
        ).fetchone()
    except sqlite3.Error as exc:
        return ValidationResult(False, f"coord binding query failed: {type(exc).__name__}")
    if row is None:
        return ValidationResult(False, f"work_id {clean_work!r} does not exist")
    state = str(row["intent_state"] or "").strip().lower()
    if row["archived_at"] is not None:
        return ValidationResult(False, f"work_id {clean_work!r} is archived")
    if state in TERMINAL_WORK_STATES:
        return ValidationResult(False, f"work_id {clean_work!r} is terminal ({state})")
    if row["claim_id"] is None:
        return ValidationResult(False, "explicit claim does not bind the requested work")
    if str(row["claim_session_id"] or "").strip() != clean_session:
        return ValidationResult(False, "explicit session does not own the requested claim")
    if str(row["lease_token"] or "").strip() != clean_fence:
        return ValidationResult(False, "explicit claim fence does not match current custody")
    if str(row["claim_status"] or "").strip().lower() != "running":
        return ValidationResult(False, "explicit claim is not running")
    timestamp = time.time() if now is None else float(now)
    expires_at = row["expires_at"]
    if expires_at is not None and float(expires_at) <= timestamp:
        return ValidationResult(False, "explicit claim lease has expired")
    if str(row["session_state"] or "").strip().lower() != "active":
        return ValidationResult(False, "claim session is not active")
    lease_until = row["lease_until"]
    if lease_until is not None and float(lease_until) <= timestamp:
        return ValidationResult(False, "claim session lease has expired")
    controller_actor = str(row["session_actor"] or "").strip().lower()
    if not controller_actor or controller_actor != clean_actor:
        return ValidationResult(False, "owner does not match the claim session controller")
    assignee = str(row["assignee"] or "").strip().lower()
    if assignee not in _SHARED_ASSIGNEES and assignee != clean_actor:
        return ValidationResult(False, "owner is incompatible with the work assignee")
    return ValidationResult(
        True,
        binding=RuntimeBinding(
            work_id=clean_work,
            job_id=clean_job,
            claim_id=clean_claim,
            claim_fence=clean_fence,
            session_id=clean_session,
            actor=clean_actor,
            assignee=assignee,
            intent_state=state,
            coord_db=path.resolve(strict=True),
        ),
    )


def validate(
    *,
    work_id: str,
    job_id: str,
    claim_id: str,
    claim_fence: str,
    session_id: str,
    actor: str,
    coord_db: str | Path | None = None,
    now: float | None = None,
) -> ValidationResult:
    """Validate exact launch custody without mutating the database or state tree."""
    clean_work = str(work_id or "").strip()
    clean_job = str(job_id or "").strip()
    clean_claim = str(claim_id or "").strip()
    clean_fence = str(claim_fence or "").strip()
    clean_session = str(session_id or "").strip()
    try:
        clean_actor = config.actor_name(actor)
    except ValueError as exc:
        return ValidationResult(False, str(exc))
    if not clean_work:
        return ValidationResult(False, "work_id is required")
    if not _SAFE_JOB_ID.fullmatch(clean_job):
        return ValidationResult(False, "job_id must be a safe literal identifier")
    if not clean_claim or not clean_fence or not clean_session:
        return ValidationResult(
            False, "claim_id, claim_fence, and session_id are required for runtime binding"
        )
    path = resolve_coord_db_path(coord_db)
    try:
        conn = _connect_read_only(path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return ValidationResult(False, f"coord database unavailable: {type(exc).__name__}")
    try:
        return _query_binding(
            conn,
            clean_work=clean_work,
            clean_job=clean_job,
            clean_claim=clean_claim,
            clean_fence=clean_fence,
            clean_session=clean_session,
            clean_actor=clean_actor,
            path=path,
            now=now,
        )
    finally:
        conn.close()


def reserve_run(
    *,
    work_id: str,
    job_id: str,
    claim_id: str,
    claim_fence: str,
    session_id: str,
    actor: str,
    run_id: str,
    sidecar_path: str,
    resource_class: str,
    coord_db: str | Path | None = None,
    now: float | None = None,
) -> ValidationResult:
    """Atomically revalidate exact custody and reserve a run before process release."""
    clean_work = str(work_id or "").strip()
    clean_job = str(job_id or "").strip()
    clean_claim = str(claim_id or "").strip()
    clean_fence = str(claim_fence or "").strip()
    clean_session = str(session_id or "").strip()
    try:
        clean_actor = config.actor_name(actor)
    except ValueError as exc:
        return ValidationResult(False, str(exc))
    if not clean_work:
        return ValidationResult(False, "work_id is required")
    if not _SAFE_JOB_ID.fullmatch(clean_job):
        return ValidationResult(False, "job_id must be a safe literal identifier")
    if not clean_claim or not clean_fence or not clean_session:
        return ValidationResult(
            False, "claim_id, claim_fence, and session_id are required for runtime binding"
        )
    path = resolve_coord_db_path(coord_db)
    conn: sqlite3.Connection | None = None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("coord database must be an existing regular non-symlink file")
        absolute = path.resolve(strict=True)
        conn = sqlite3.connect(absolute, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not _REQUIRED_TABLES <= tables:
            raise ValueError("coord database lacks standalone lifecycle tables")
        result = _query_binding(
            conn,
            clean_work=clean_work,
            clean_job=clean_job,
            clean_claim=clean_claim,
            clean_fence=clean_fence,
            clean_session=clean_session,
            clean_actor=clean_actor,
            path=absolute,
            now=now,
        )
        if not result.ok:
            conn.rollback()
            return result
        timestamp = time.time() if now is None else float(now)
        conn.execute(
            "INSERT INTO runs("
            "run_id,work_id,session_id,runner_kind,progress_mode,sidecar_path,"
            "resource_class,started_at,heartbeat_at,state,version"
            ") VALUES (?,?,?,?,?,?,?,?,?,'reserved',0)",
            (
                run_id,
                clean_work,
                clean_session,
                "local_job",
                "sidecar",
                sidecar_path,
                resource_class,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        return result
    except (OSError, sqlite3.Error, ValueError) as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return ValidationResult(False, f"coord run reservation failed: {type(exc).__name__}")
    finally:
        if conn is not None:
            conn.close()


class ReservationGuardError(RuntimeError):
    """Raised when exact launch custody cannot be held through child release."""


@contextmanager
def reserved_launch_guard(
    *,
    work_id: str,
    job_id: str,
    claim_id: str,
    claim_fence: str,
    session_id: str,
    actor: str,
    run_id: str,
    coord_db: str | Path | None = None,
    now: float | None = None,
) -> Iterator[RuntimeBinding]:
    """Hold a write transaction from final custody validation through Popen."""
    clean_work = str(work_id or "").strip()
    clean_job = str(job_id or "").strip()
    clean_claim = str(claim_id or "").strip()
    clean_fence = str(claim_fence or "").strip()
    clean_session = str(session_id or "").strip()
    try:
        clean_actor = config.actor_name(actor)
    except ValueError as exc:
        raise ReservationGuardError(str(exc)) from exc
    path = resolve_coord_db_path(coord_db)
    conn: sqlite3.Connection | None = None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("coord database must be an existing regular non-symlink file")
        absolute = path.resolve(strict=True)
        conn = sqlite3.connect(absolute, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        result = _query_binding(
            conn,
            clean_work=clean_work,
            clean_job=clean_job,
            clean_claim=clean_claim,
            clean_fence=clean_fence,
            clean_session=clean_session,
            clean_actor=clean_actor,
            path=absolute,
            now=now,
        )
        if not result.ok or result.binding is None:
            raise ReservationGuardError(result.reason)
        reserved = conn.execute(
            "SELECT work_id,session_id,state FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if reserved is None:
            raise ReservationGuardError("reserved run is missing")
        if (
            str(reserved["work_id"] or "") != clean_work
            or str(reserved["session_id"] or "") != clean_session
            or str(reserved["state"] or "") != "reserved"
        ):
            raise ReservationGuardError("reserved run custody binding changed")
    except ReservationGuardError:
        if conn is not None:
            conn.rollback()
            conn.close()
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()
        raise ReservationGuardError(
            f"coord reservation guard failed: {type(exc).__name__}"
        ) from exc

    try:
        yield result.binding
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--claim-id", required=True)
    parser.add_argument("--claim-fence", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--coord-db")
    args = parser.parse_args(argv)
    result = validate(
        work_id=args.work_id,
        job_id=args.job_id,
        claim_id=args.claim_id,
        claim_fence=args.claim_fence,
        session_id=args.session_id,
        actor=args.actor,
        coord_db=args.coord_db,
    )
    if result.ok:
        print(f"ok: {args.work_id} claim={args.claim_id} session={args.session_id}")
        return 0
    print(f"tracked launch binding invalid: {result.reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
