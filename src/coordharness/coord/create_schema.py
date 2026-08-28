from __future__ import annotations

import argparse
import hashlib
import sqlite3
import time
from pathlib import Path

from .config import DEFAULT_DB_PATH, _validate_existing_db_file, connect

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
_MIGRATION_VERSION = 1
_MIGRATION_NAME = "coord_v1_initial"

_EXPECTED_TABLES = {
    "schema_migrations", "agent_sessions", "work_items", "claims", "runs",
    "events", "inbox_cursors", "artifacts", "display_titles",
    "request_consumption",
}
_EXPECTED_VIEWS = {"v_session_claimcount", "v_work_owner", "v_session_rollup", "v_runs_read_model"}
_ADD_COLUMNS = {
    "agent_sessions": {
        "pid_started_at": "REAL",
        "human_label": "TEXT",
        "external_thread_id": "TEXT",
        "conversation_title": "TEXT",
        "worktree_id": "TEXT",
        "label_source": "TEXT",
        "label_updated_at": "REAL",
    },
    "runs": {
        "pid_started_at": "REAL",
    },
    "work_items": {
        "note": "TEXT",
        "depends_on": "TEXT",
        "kind": "TEXT",
        "tier": "TEXT",
        "operator_state": "TEXT",
        "completion_requested_at": "REAL",
        "next_step": "TEXT",
        "resume_when": "TEXT",
        "resume_predicate_json": "TEXT",
        "continuation_ready_at": "REAL",
        "operator_ok_event_id": "INTEGER",
        "tier_correction_event_id": "INTEGER",
    },
}
_BUSY_RETRY_LIMIT = 5
_BUSY_RETRY_BASE_S = 0.05


def _guard_existing_file(db_path: Path | str | None) -> None:
    _validate_existing_db_file(Path(db_path or DEFAULT_DB_PATH))


def _apply_additive_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _ADD_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _backfill_request_consumption(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO request_consumption("
        "recipient_lane,work_id,request_event_id)"
        " SELECT CASE e.to_selector"
        " WHEN 'actor:claude' THEN 'claude' WHEN 'actor:codex' THEN 'codex' END,"
        " e.work_id,e.event_id FROM events e"
        " WHERE e.kind IN ('handoff','audit_request')"
        " AND e.to_selector IN ('actor:claude','actor:codex')"
        " AND COALESCE(e.work_id,'')<>''"
    )
    pending = conn.execute(
        "SELECT recipient_lane,work_id,request_event_id"
        " FROM request_consumption WHERE consumed_at IS NULL"
    ).fetchall()
    for recipient_lane, work_id, request_event_id in pending:
        acknowledgement = conn.execute(
            "SELECT event_id,ts FROM events WHERE work_id=? AND actor=? AND event_id>?"
            " ORDER BY event_id LIMIT 1",
            (work_id, recipient_lane, request_event_id),
        ).fetchone()
        if acknowledgement is not None:
            conn.execute(
                "UPDATE request_consumption SET consumed_event_id=?,consumed_at=?"
                " WHERE recipient_lane=? AND work_id=? AND request_event_id=?"
                " AND consumed_at IS NULL",
                (
                    int(acknowledgement[0]),
                    float(acknowledgement[1]),
                    recipient_lane,
                    work_id,
                    request_event_id,
                ),
            )


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "lock" in msg or "busy" in msg


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def apply_schema(db_path: Path | str | None = None) -> dict:
    _guard_existing_file(db_path)
    sql = _SCHEMA_FILE.read_text()
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    conn = connect(db_path)
    try:
        already = False
        last_busy: sqlite3.OperationalError | None = None
        for attempt in range(_BUSY_RETRY_LIMIT):
            try:
                conn.executescript(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                    "applied_at REAL NOT NULL, checksum TEXT NOT NULL)"
                )
                row = conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (_MIGRATION_VERSION,),
                ).fetchone()
                already = row is not None
                conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                _apply_additive_columns(conn)
                _backfill_request_consumption(conn)
                if not already:
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at, checksum) "
                        "VALUES (?,?,?,?)",
                        (_MIGRATION_VERSION, _MIGRATION_NAME, time.time(), checksum),
                    )
                last_busy = None
                break
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last_busy = exc
                _rollback_quietly(conn)
                if attempt < _BUSY_RETRY_LIMIT - 1:
                    time.sleep(_BUSY_RETRY_BASE_S * (2 ** attempt))
        if last_busy is not None:
            raise last_busy
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        views = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }
        missing_t = _EXPECTED_TABLES - tables
        missing_v = _EXPECTED_VIEWS - views
        if missing_t or missing_v:
            raise RuntimeError(f"schema incomplete: tables {missing_t}, views {missing_v}")
        return {
            "db": str(Path(db_path or DEFAULT_DB_PATH).resolve()),
            "migration_applied": not already,
            "tables": sorted(tables),
            "views": sorted(views),
            "migration_rows": conn.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    from coordharness.bootstrap import bootstrap_database

    bootstrap = bootstrap_database(args.db)
    report = bootstrap["base"]
    print(f"coord.db @ {report['db']}")
    print(f"  migration_applied={report['migration_applied']} "
          f"migration_rows={report['migration_rows']}")
    print(f"  tables ({len(report['tables'])}): {', '.join(report['tables'])}")
    print(f"  views  ({len(report['views'])}): {', '.join(report['views'])}")


if __name__ == "__main__":
    main()
