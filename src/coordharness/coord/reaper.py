from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import hashlib
import sqlite3
import tempfile

from . import coord_db
from .config import connect
from .process_liveness import START_TIME_TOLERANCE_S, pid_start_time
from .staleness import ACTIVE_INTENT_STALE_SECS, PIDLESS_RUN_STALE_SECS


def _process_start_time(pid: int | None) -> float | None:
    return pid_start_time(pid)


def _pid_alive(pid: int | None, expected_start_time: float | None = None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    if expected_start_time is None:
        return True
    actual = _process_start_time(pid)
    return actual is not None and abs(float(actual) - float(expected_start_time)) <= START_TIME_TOLERANCE_S


RUN_STALE_S = PIDLESS_RUN_STALE_SECS
WORKFLOW_RUN_STALE_S = 30 * 60.0
ACTIVE_INTENT_STALE_S = float(os.environ.get("COORD_COORD_ACTIVE_INTENT_STALE_S", str(ACTIVE_INTENT_STALE_SECS)))
PIDLESS_WORKLESS_SESSION_GRACE_S = float(
    os.environ.get("COORD_COORD_PIDLESS_WORKLESS_SESSION_GRACE_S", "900")
)

AUTO_REQUEUE_BLOCK_REASON_CLASSES = frozenset(
    {
        "dependency_missing_predicate",
        "stale_resolved_upstream",
        "upstream_artifact_not_ready",
    }
)
_NON_MECHANICAL_EVENT_KINDS = frozenset(
    {
        "audit_verdict",
        "decision",
        "operator_ok",
        "operator-ok",
    }
)


def _predicate_can_auto_requeue(predicate: object) -> bool:
    if not isinstance(predicate, dict):
        return False
    kind = str(predicate.get("type") or "").strip()
    if kind in {"artifact_exists", "artifact_readable", "sha_matches"}:
        path = str(predicate.get("path") or predicate.get("ref") or "").strip()
        if not path:
            return False
        if kind == "sha_matches":
            return len(str(predicate.get("sha256") or "").strip()) == 64
        return True
    if kind == "event_exists":
        if not all(
            str(predicate.get(field) or "").strip()
            for field in ("work_id", "kind", "actor")
        ):
            return False
        event_kind = str(predicate.get("kind") or "").strip().lower()
        return event_kind not in _NON_MECHANICAL_EVENT_KINDS
    if kind in {"all_of", "any_of"}:
        children = predicate.get("predicates")
        return bool(
            isinstance(children, list)
            and children
            and all(_predicate_can_auto_requeue(child) for child in children)
        )
    return False


def _pidless_workless_stale_sessions(conn, *, at: float, grace_s: float) -> set[str]:
    rows = conn.execute(
        "SELECT s.session_id FROM agent_sessions s"
        " WHERE s.state='active' AND s.pause_at IS NULL AND s.pid IS NULL"
        " AND s.lease_until<?"
        " AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.session_id=s.session_id"
        "   AND c.status IN ('running','paused','blocked'))"
        " AND NOT EXISTS (SELECT 1 FROM runs r"
        "   WHERE (r.session_id=s.session_id OR r.parent_session_id=s.session_id)"
        "   AND r.state='live')",
        (at - grace_s,),
    ).fetchall()
    return {str(row["session_id"]) for row in rows}


def _predicate_true(
    conn,
    predicate: object,
    *,
    root: Path,
    current_work_id: str = "",
    dark_hold_exits: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    if not isinstance(predicate, dict):
        return False
    kind = str(predicate.get("type") or "").strip()
    if kind in {"all_of", "any_of"}:
        values = predicate.get("predicates")
        if not isinstance(values, list) or not values:
            return False
        results = [
            _predicate_true(
                conn,
                value,
                root=root,
                current_work_id=current_work_id,
                dark_hold_exits=dark_hold_exits,
            )
            for value in values
        ]
        return all(results) if kind == "all_of" else any(results)
    if kind == "event_exists":
        work_id = str(predicate.get("work_id") or "").strip()
        event_kind = str(predicate.get("kind") or "").strip()
        actor = str(predicate.get("actor") or "").strip()
        after = int(predicate.get("after_event_id") or 0)
        clauses = ["event_id>?", "work_id=?"]
        params: list[object] = [after, work_id]
        if event_kind:
            clauses.append("kind=?")
            params.append(event_kind)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        return conn.execute(
            f"SELECT 1 FROM events WHERE {' AND '.join(clauses)} LIMIT 1", params
        ).fetchone() is not None
    if kind in {"artifact_exists", "artifact_readable"}:
        path = Path(str(predicate.get("path") or ""))
        if not path.is_absolute():
            path = root / path
        return path.is_file() and (kind != "artifact_readable" or os.access(path, os.R_OK))
    if kind == "sha_matches":
        path = Path(str(predicate.get("path") or predicate.get("ref") or ""))
        if not path.is_absolute():
            path = root / path
        expected = str(predicate.get("sha256") or "").strip().lower()
        if not path.is_file() or len(expected) != 64:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == expected
    if kind == "verdict_posted":
        return conn.execute(
            "SELECT 1 FROM events WHERE work_id=? AND kind='audit_verdict'"
            " AND UPPER(COALESCE(verdict,''))=? AND LOWER(COALESCE(actor,''))=? LIMIT 1",
            (
                str(predicate.get("work_id") or ""),
                str(predicate.get("verdict") or "").upper(),
                str(predicate.get("from_lane") or "").lower(),
            ),
        ).fetchone() is not None
    if kind == "dark_hold_exit":
        hold_id = str(predicate.get("hold_id") or "").strip()
        predicate_work_id = str(predicate.get("work_id") or "").strip()
        return bool(
            hold_id
            and predicate_work_id
            and predicate_work_id == current_work_id
            and (hold_id, predicate_work_id) in dark_hold_exits
        )
    return False


def _dark_hold_pairs(predicate: object, *, current_work_id: str) -> set[tuple[str, str]]:

    if not isinstance(predicate, dict):
        return set()
    kind = str(predicate.get("type") or "").strip()
    if kind in {"all_of", "any_of"}:
        children = predicate.get("predicates")
        if not isinstance(children, list):
            return set()
        pairs: set[tuple[str, str]] = set()
        for child in children:
            pairs.update(_dark_hold_pairs(child, current_work_id=current_work_id))
        return pairs
    if kind != "dark_hold_exit":
        return set()
    hold_id = str(predicate.get("hold_id") or "").strip()
    predicate_work_id = str(predicate.get("work_id") or "").strip()
    if not hold_id or predicate_work_id != current_work_id:
        return set()
    return {(hold_id, predicate_work_id)}


def _evaluate_dark_hold_snapshot(rows: list[object]) -> frozenset[tuple[str, str]]:

    requested: set[tuple[str, str]] = set()
    for row in rows:
        try:
            predicate = json.loads(row["resume_predicate_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        requested.update(
            _dark_hold_pairs(predicate, current_work_id=str(row["work_id"]))
        )
    if not requested:
        return frozenset()

    try:
        from coordharness import dark_triggers
    except Exception:
        return frozenset()

    satisfied: set[tuple[str, str]] = set()
    for hold_id, work_id in sorted(requested):
        try:
            result = dark_triggers.evaluate_hold_exit(
                hold_id=hold_id,
                work_id=work_id,
            )
        except Exception:
            continue
        if (
            result.get("hold_id") == hold_id
            and result.get("work_id") == work_id
            and result.get("status") in dark_triggers.HOLD_EXIT_OUTCOMES
        ):
            satisfied.add((hold_id, work_id))
    return frozenset(satisfied)


def evaluate_continuations(conn, *, root: Path | None = None) -> dict:
    from coordharness import config as _harness_config

    root = root or _harness_config.project_root()
    ready: list[dict] = []
    rows = conn.execute(
        "SELECT w.work_id,w.assignee,w.intent_state,w.blocked_reason_class,"
        " w.resume_predicate_json,w.version,c.claim_id,c.version AS claim_version,"
        " (SELECT COUNT(*) FROM claims bc WHERE bc.work_id=w.work_id"
        " AND bc.status='blocked') AS blocked_claim_count"
        " FROM work_items w LEFT JOIN claims c ON c.work_id=w.work_id"
        " AND c.status='blocked'"
        " WHERE intent_state IN ('blocked','paused','queued','planned')"
        " AND continuation_ready_at IS NULL"
        " AND COALESCE(resume_predicate_json,'')<>''"
    ).fetchall()
    dark_hold_exits = _evaluate_dark_hold_snapshot(rows)
    for row in rows:
        try:
            predicate = json.loads(row["resume_predicate_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not _predicate_true(
            conn,
            predicate,
            root=root,
            current_work_id=str(row["work_id"]),
            dark_hold_exits=dark_hold_exits,
        ):
            continue
        intent = str(row["intent_state"] or "").strip().lower()
        reason_class = str(row["blocked_reason_class"] or "").strip().lower()
        scanned_claim_id = str(row["claim_id"] or "").strip() or None
        scanned_claim_version = row["claim_version"]
        safe_candidate = bool(
            intent == "blocked"
            and reason_class in AUTO_REQUEUE_BLOCK_REASON_CLASSES
            and _predicate_can_auto_requeue(predicate)
        )
        if safe_candidate and (
            scanned_claim_id is None or int(row["blocked_claim_count"] or 0) != 1
        ):
            continue
        requeue = safe_candidate
        lane = str(row["assignee"] or "").strip().lower()
        transition = coord_db.apply_continuation_ready_transition(
            conn,
            work_id=str(row["work_id"]),
            lane=lane,
            predicate=predicate,
            expected_work_version=int(row["version"]),
            expected_intent=intent,
            expected_claim_id=scanned_claim_id,
            expected_claim_version=scanned_claim_version,
            requeue=requeue,
        )
        if transition is None:
            continue
        ready.append(transition)
    return {"ready_count": len(ready), "ready": ready}


def reclassify_self_directed_requests(conn) -> dict:
    with coord_db.tx(conn):
        now = coord_db.db_now(conn)
        rows = conn.execute(
            "SELECT rc.request_event_id,rc.recipient_lane,rc.work_id"
            " FROM request_consumption rc JOIN events e"
            " ON e.event_id=rc.request_event_id"
            " WHERE rc.consumed_at IS NULL"
            " AND lower(COALESCE(e.actor,''))=lower(COALESCE(rc.recipient_lane,''))"
            " ORDER BY rc.request_event_id"
        ).fetchall()
        ids = [int(row["request_event_id"]) for row in rows]
        digest = hashlib.sha256(
            json.dumps(ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for row in rows:
            conn.execute(
                "UPDATE request_consumption"
                " SET consumed_event_id=request_event_id,consumed_at=?"
                " WHERE request_event_id=? AND recipient_lane=? AND consumed_at IS NULL",
                (now, row["request_event_id"], row["recipient_lane"]),
            )
        event_id = None
        if rows:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events(ts,kind,actor,trust,title,body,payload_json,"
                "idempotency_key) VALUES (?,'self_directed_sla_reclassification','system',"
                "'system','Self-directed SLA timers reclassified',?,?,?)",
                (
                    now,
                    f"Reclassified {len(rows)} own-lane request timer(s); cross-lane requests unchanged.",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "reclassified_count": len(rows),
                            "request_event_ids_sha256": digest,
                            "first_event_id": ids[0],
                            "last_event_id": ids[-1],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    f"self-directed-sla-reclassification:{digest}",
                ),
            )
            if cursor.rowcount == 1:
                event_id = int(cursor.lastrowid)
        return {
            "reclassified_count": len(rows),
            "request_event_ids_sha256": digest,
            "event_id": event_id,
        }


def evaluate_request_slas(conn) -> dict:
    reclassification = reclassify_self_directed_requests(conn)
    escalations: list[dict] = []
    with coord_db.tx(conn):
        now = coord_db.db_now(conn)
        rows = conn.execute(
            "SELECT e.event_id,e.ts,e.kind,e.work_id,e.payload_json,rc.recipient_lane"
            " FROM request_consumption rc JOIN events e ON e.event_id=rc.request_event_id"
            " WHERE rc.consumed_at IS NULL"
            " AND (e.kind!='audit_request' OR NOT EXISTS ("
            "   SELECT 1 FROM events verdict"
            "   WHERE verdict.work_id=e.work_id"
            "     AND verdict.kind='audit_verdict'"
            "     AND verdict.event_id>e.event_id"
            "     AND lower(COALESCE(verdict.actor,''))="
            "         lower(COALESCE(rc.recipient_lane,''))"
            "     AND upper(COALESCE(verdict.verdict,''))"
            "         IN ('PASS','FLAG','BLOCKED')"
            " ))"
            " ORDER BY e.ts"
        ).fetchall()
        for row in rows:
            if len(escalations) >= 50:
                break
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            default_sla = 1800 if row["kind"] == "audit_request" else 3600
            try:
                sla_s = max(1, int(payload.get("sla_s") or default_sla))
            except (TypeError, ValueError):
                sla_s = default_sla
            age_s = max(0.0, now - float(row["ts"]))
            multiple = 2 if age_s >= 2 * sla_s else (1 if age_s >= sla_s else 0)
            if not multiple:
                continue
            key = f"request-sla:{row['event_id']}:{multiple}x"
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(ts,kind,actor,to_selector,work_id,severity,"
                "trust,title,body,refs_json,payload_json,idempotency_key)"
                " VALUES (?,'sla_escalation','system',?,?,?,'system',?,?,?, ?,?)",
                (
                    now,
                    f"actor:{row['recipient_lane']}",
                    row["work_id"],
                    "warning" if multiple == 1 else "high",
                    f"Cross-lane request past {multiple}x SLA",
                    f"Request event {row['event_id']} remains unconsumed after {int(age_s)}s.",
                    json.dumps([f"event:{row['event_id']}"]),
                    json.dumps(
                        {
                            "schema_version": 1,
                            "request_event_id": int(row["event_id"]),
                            "sla_s": sla_s,
                            "age_s": int(age_s),
                            "multiple": multiple,
                            "spawn_action": (
                                f"claim:{row['work_id']}"
                                if row["recipient_lane"] == "claude"
                                else None
                            ),
                        },
                        sort_keys=True,
                    ),
                    key,
                ),
            )
            if cur.rowcount == 1:
                escalations.append(
                    {
                        "work_id": row["work_id"],
                        "lane": row["recipient_lane"],
                        "multiple": multiple,
                        "event_id": int(cur.lastrowid),
                    }
                )
    return {
        "escalation_count": len(escalations),
        "escalations": escalations,
        "self_directed_reclassified": int(reclassification["reclassified_count"]),
        "self_directed_reclassification_event_id": reclassification["event_id"],
    }


def reap_dead_runs(conn, *, stale_s: float = RUN_STALE_S,
                   workflow_stale_s: float = WORKFLOW_RUN_STALE_S) -> int:
    now = coord_db.db_now(conn)
    live = conn.execute(
        "SELECT run_id, pid, pid_started_at, started_at, runner_kind, heartbeat_at"
        " FROM runs WHERE state='live'"
    ).fetchall()
    to_orphan: list[str] = []
    for r in live:
        pid = r["pid"]
        if pid is not None:
            if not _pid_alive(pid, r["pid_started_at"]):
                to_orphan.append(r["run_id"])
        else:
            kind = str(r["runner_kind"] or "")
            cutoff = workflow_stale_s if (kind == "workflow" and not r["heartbeat_at"]) else stale_s
            if (r["started_at"] or 0) < now - cutoff:
                to_orphan.append(r["run_id"])
    if to_orphan:
        with coord_db.tx(conn):
            for rid in to_orphan:
                conn.execute(
                    "UPDATE runs SET state='orphaned', finished_at=?, version=version+1"
                    " WHERE run_id=? AND state='live'", (now, rid))
    return len(to_orphan)


def run_reaper(
    db_path=None,
    grace_s: float = coord_db.REAP_GRACE_S,
    *,
    flush_projection: bool = True,
) -> dict:
    conn = connect(db_path)
    try:
        now = coord_db.db_now(conn)
        active = conn.execute(
            "SELECT session_id, pid, pid_started_at FROM agent_sessions WHERE state='active'"
        ).fetchall()
        dead_sessions = {
            r["session_id"]
            for r in active
            if r["pid"] and not _pid_alive(r["pid"], r["pid_started_at"])
        }
        pidless_workless = _pidless_workless_stale_sessions(
            conn, at=now, grace_s=PIDLESS_WORKLESS_SESSION_GRACE_S
        )
        dead_sessions.update(pidless_workless)
        rep = coord_db.reap_zombie_sessions(conn, grace_s=grace_s, dead_sessions=dead_sessions)
        rep["pidless_workless_reaped"] = sorted(pidless_workless)
        rep["continuations"] = evaluate_continuations(conn)
        rep["request_slas"] = evaluate_request_slas(conn)
        rep["runs_finalized"] = reap_dead_runs(conn)
        fleet_renewals = coord_db.renew_claims_from_live_fleets(conn)
        rep["fleet_claim_renewals"] = fleet_renewals
        normalized = coord_db.normalize_stale_active_intents(
            conn,
            grace_s=ACTIVE_INTENT_STALE_S,
        )
        rep["active_intents_normalized"] = normalized["normalized"]
        rep["active_intent_samples"] = normalized["work_ids"]
        expired = coord_db.release_expired_claims_batch(conn)
        rep["expired_claims"] = expired

        from . import native_cockpit

        mutation_count = (
            int(rep.get("claims_released") or 0)
            + int(rep.get("runs_finalized") or 0)
            + int(fleet_renewals.get("renewed_count") or 0)
            + int(rep.get("active_intents_normalized") or 0)
            + int(expired.get("released_count") or 0)
            + len(rep.get("reaped") or ())
        )
        if mutation_count:
            native_cockpit.request_refresh(
                conn,
                reason=f"coord_reaper:{mutation_count}",
            )
        rep["projection"] = (
            native_cockpit.flush_requested_refresh(conn, force=True)
            if flush_projection
            else {"flushed": False, "pending": True, "reason": "caller_deferred"}
        )
        return rep
    finally:
        conn.close()


def dry_run_reaper(
    db_path=None,
    grace_s: float = coord_db.REAP_GRACE_S,
) -> dict:
    """Report what `run_reaper()` would do, without writing to the real database.

    A hand-written preview that re-implements each predicate ("is this claim
    expired", "is this session's pid dead") alongside the real one is a second
    copy of exactly the logic most likely to drift -- and a preview that drifts
    from reality is worse than no preview at all, because it reports a lie with
    a reassuring "would" in front of it. So nothing here is re-implemented: this
    takes a consistent point-in-time copy of the database with SQLite's own
    online backup API -- which, unlike a raw file copy, is safe to run against a
    live WAL-mode database with concurrent writers -- and then runs the real,
    completely unmodified `run_reaper()` against that disposable copy. The
    report it returns is exactly the report a real run would have produced.

    The source database is opened read-only and is never written to; the
    snapshot lives in a temporary directory that is removed before this
    function returns, so nothing about a dry run outlives the call.
    """
    from . import config as _config

    source_path = Path(db_path) if db_path is not None else _config.DEFAULT_DB_PATH
    if not source_path.exists():
        raise FileNotFoundError(f"coord.db does not exist: {source_path}")
    with tempfile.TemporaryDirectory(prefix="coord-reaper-dry-run-") as scratch:
        snapshot_path = Path(scratch) / "snapshot.db"
        source_conn = sqlite3.connect(
            f"file:{source_path}?mode=ro", uri=True, timeout=5.0
        )
        snapshot_conn = sqlite3.connect(str(snapshot_path))
        try:
            source_conn.backup(snapshot_conn)
        finally:
            snapshot_conn.close()
            source_conn.close()
        # flush_projection=False: the snapshot has no reader waiting on it, and
        # flushing takes native_cockpit's projection-maintenance file lock --
        # real machinery a throwaway preview has no business touching.
        report = run_reaper(snapshot_path, grace_s, flush_projection=False)
    report["dry_run"] = True
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="coord-reaper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Release expired claims, reap zombie sessions, finalize dead runs, "
            "renew claims for live fleets, and drain the native-cockpit "
            "projection-refresh queue.\n"
            "\n"
            "THIS COMMAND WRITES TO THE DATABASE. `coord doctor` is the "
            "read-only diagnostic surface; a normal run of coord-reaper changes "
            "lifecycle state. It is the background half of \"status is derived "
            "at read time, never stored\": a lapsed lease already reads as "
            "stale on the very next board read, but nobody puts the claim back "
            "in circulation or notices a dead process until something actually "
            "walks the table and checks -- this is that something. Run with "
            "--dry-run first if you want to see what it would change before it "
            "changes anything."
        ),
    )
    ap.add_argument(
        "--db", default=None,
        help="path to coord.db (default: the configured project database)",
    )
    ap.add_argument(
        "--grace", type=float, default=coord_db.REAP_GRACE_S,
        help="seconds of grace before a dead-pid session is reaped (default: %(default)s)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help=(
            "report what would be released/reaped/finalized WITHOUT mutating "
            "the real database. Runs the real reaper logic against a "
            "disposable snapshot of the database, so the preview cannot drift "
            "from what a real run would do."
        ),
    )
    ap.add_argument(
        "--receipt", default=None,
        help="write the JSON report to this path (a dry run writes a preview, marked \"dry_run\": true)",
    )
    ap.add_argument(
        "--defer-projection", action="store_true",
        help=(
            "skip flushing the native-cockpit projection queue after reaping "
            "(ignored with --dry-run, which never flushes)"
        ),
    )
    args = ap.parse_args()
    if args.dry_run:
        report = dry_run_reaper(args.db, args.grace)
    else:
        report = run_reaper(
            args.db,
            args.grace,
            flush_projection=not args.defer_projection,
        )
    if args.receipt:
        path = Path(args.receipt)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
    if args.dry_run:
        print(
            f"[DRY RUN] would reap {len(report['reaped'])} session(s); "
            f"would release {report['claims_released']} zombie claim(s) + "
            f"{report['expired_claims']['released_count']} expired claim(s); "
            f"would finalize {report['runs_finalized']} dead run(s); "
            f"would normalize {report['active_intents_normalized']} stale "
            "active intent(s). Nothing was written -- rerun without --dry-run "
            "to apply."
        )
    else:
        print(f"reaped {len(report['reaped'])} session(s); released {report['claims_released']} "
              f"zombie claim(s) + {report['expired_claims']['released_count']} expired claim(s); "
              f"finalized {report['runs_finalized']} dead run(s); "
              f"normalized {report['active_intents_normalized']} stale active intent(s); "
              f"projection_flushed={report['projection'].get('flushed', False)}")


if __name__ == "__main__":
    main()
