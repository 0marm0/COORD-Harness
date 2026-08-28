from __future__ import annotations

import json
from typing import Optional

from . import coord_db
from . import projection_schema as ps
from . import review_integrity
from .process_liveness import pid_matches
from coordharness.jobs.diagnostic_marker import read_sidecar_with_authority


def _pid_alive(pid, expected_start_time: float | None = None) -> bool:
    return pid_matches(pid, expected_start_time)


def _live_runs_by_work(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT run_id, work_id, runner_kind, pid, pgid, resource_class, sidecar_path, model,"
        " pid_started_at, started_at FROM runs WHERE state='live' AND pid IS NOT NULL"
        " ORDER BY started_at").fetchall()
    for r in rows:
        d = dict(r)
        wid = d.get("work_id")
        if wid and _pid_alive(d.get("pid"), d.get("pid_started_at")):
            out[wid] = d
    return out


def _orphan_live_runs(conn, claimed_work_ids: set[str]) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, work_id, runner_kind, pid, pid_started_at, resource_class, sidecar_path, model"
        " FROM runs WHERE state='live' AND pid IS NOT NULL").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not _pid_alive(d.get("pid"), d.get("pid_started_at")):
            continue
        if not d.get("work_id") or d["work_id"] not in claimed_work_ids:
            out.append(d)
    return out


def _covered_child_pids(conn) -> set[int]:
    covered: set[int] = set()
    rows = conn.execute(
        "SELECT work_id, sidecar_path, pid, pid_started_at"
        " FROM runs WHERE state='live' AND work_id IS NOT NULL AND sidecar_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        if not _pid_alive(row["pid"], row["pid_started_at"]):
            continue
        sidecar, authority = read_sidecar_with_authority(row["sidecar_path"])
        if sidecar is None or authority is None or authority.diagnostic_only:
            continue
        if not isinstance(sidecar, dict):
            continue
        for key in ("child_pid", "worker_pid"):
            value = sidecar.get(key)
            try:
                pid = int(value)
            except (TypeError, ValueError):
                continue
            if pid and pid != int(row["pid"]):
                covered.add(pid)
    return covered


def _held_claim_stalled(row: dict, at: float) -> bool:
    owner = row.get("owner_session_id")
    cstatus = row.get("claim_status")
    if not owner or cstatus not in ("running", "paused", "blocked"):
        return False
    expires = row.get("claim_expires_at")
    try:
        expires_at = float(expires) if expires is not None else None
    except (TypeError, ValueError):
        expires_at = None
    return expires_at is not None and expires_at <= at


def _to_board_row(row: dict, group_by: str, run: Optional[dict], at: float) -> ps.BoardRow:
    raw = str(row.get("status") or "queued")
    br: ps.BoardRow = {
        "work_id": row.get("work_id"),
        "title": row.get("title"),
        "display": row.get("display"),
        "domain": row.get("domain"),
        "module": row.get("module"),
        "lane": row.get("lane"),
        "sublane": row.get("sublane"),
        "group": row.get("group") or (row.get(group_by) or "(ungrouped)"),
        "raw_status": raw,
        "status": ps.bucket(raw),
        "proof_state": row.get("proof_state"),
        "assignee": row.get("assignee"),
        "owner_session_id": row.get("owner_session_id"),
        "owner_session_label": row.get("owner_session_label"),
        "owner_session_actor": row.get("owner_session_actor"),
        "owner_conversation_title": row.get("owner_conversation_title"),
        "owner_external_thread_id": row.get("owner_external_thread_id"),
        "claim_status": row.get("claim_status"),
        "claim_step": row.get("claim_step"),
        "has_artifact": bool(row.get("has_artifact")),
        "acceptance_json": row.get("acceptance_json"),
        "context_pack_ref": row.get("context_pack_ref"),
        "done_signal": row.get("done_signal"),
        "rubric_state": row.get("rubric_verdict"),
        "rubric_verdict": row.get("rubric_verdict"),
        "token_budget": row.get("token_budget"),
        "due_date": row.get("due_date"),
        "visibility": row.get("visibility"),
        "priority": int(row.get("priority") or 0),
        "note": row.get("note"),
        "depends_on": row.get("depends_on"),
        "kind": row.get("kind"),
        "tier": row.get("tier"),
        "operator_state": row.get("operator_state"),
    }
    if run:
        br["pid"] = run.get("pid")
        br["resource_class"] = run.get("resource_class") or row.get("resource_class")
        br["runner_kind"] = run.get("runner_kind")
        if raw not in ("done", "failed", "archived", "superseded", "cancelled", "canceled", "closed"):
            br["raw_status"] = "running"
            br["status"] = ps.bucket("running")
    else:
        br["resource_class"] = row.get("resource_class")
    stalled = (not run) and _held_claim_stalled(row, at)
    br["stale"] = stalled
    if stalled:
        br["display_status"] = "STALLED"
        br["status"] = "blocked"
    return br


def build_state_from_coord(conn, *, group_by: str = "module", at: float | None = None,
                           mode: str = "full") -> ps.BoardState:
    at = at if at is not None else coord_db.db_now(conn)
    rows = coord_db.board_rows(conn, at=at, group_by=group_by)
    runs_by_work = _live_runs_by_work(conn)

    work_model: ps.WorkModel = {
        "running_rows": [], "attention_rows": [], "next_rows": [], "done_rows": [],
    }
    raw_tally: dict[str, int] = {}
    board_work_ids: set[str] = set()
    for row in rows:
        wid = row.get("work_id")
        board_work_ids.add(wid)
        br = _to_board_row(row, group_by, runs_by_work.get(wid), at)
        raw_tally[br["raw_status"]] = raw_tally.get(br["raw_status"], 0) + 1
        work_model[ps.list_for(br["raw_status"])].append(br)

    covered_child_pids = _covered_child_pids(conn)
    for run in _orphan_live_runs(conn, board_work_ids):
        try:
            run_pid = int(run.get("pid"))
        except (TypeError, ValueError):
            run_pid = None
        if run_pid in covered_child_pids:
            continue
        work_model["running_rows"].append({
            "work_id": run.get("work_id") or run.get("run_id"),
            "title": run.get("model") or run.get("runner_kind") or run.get("run_id"),
            "group": run.get("resource_class") or run.get("runner_kind") or "ops",
            "raw_status": "running", "status": "running",
            "pid": run.get("pid"), "resource_class": run.get("resource_class"),
            "runner_kind": run.get("runner_kind"), "has_artifact": False, "priority": 0,
        })
        raw_tally["running"] = raw_tally.get("running", 0) + 1

    running = len(work_model["running_rows"])
    attention = len(work_model["attention_rows"])
    nxt = len(work_model["next_rows"])
    done = len(work_model["done_rows"])
    work_model["counts"] = {
        "running": running, "attention": attention, "next": nxt, "done": done,
    }
    rt = raw_tally.get
    queue_open = rt("queued", 0) + rt("planned", 0)
    queue_blocked = rt("blocked", 0) + rt("paused", 0)
    queue_terminal = rt("failed", 0)
    work_model["followup_rows"] = []
    work_model["summary"] = {
        "running": running, "attention": attention, "next": nxt, "done": done,
        "queue": queue_open + queue_blocked + queue_terminal,
        "queue_open": queue_open, "queue_blocked": queue_blocked,
        "queue_terminal": queue_terminal, "followup": 0,
        "open": running + attention + nxt,
        "total": running + attention + nxt + done,
    }

    sessions: list[ps.SessionRow] = []
    for s in coord_db.session_rollup(conn, at=at):
        sessions.append({
            "session_id": s.get("parent_session_id"),
            "actor": s.get("actor"),
            "runner_type": s.get("runner_type"),
            "human_label": s.get("human_label"),
            "external_thread_id": s.get("external_thread_id"),
            "conversation_title": s.get("conversation_title"),
            "worktree_id": s.get("worktree_id"),
            "label_source": s.get("label_source"),
            "live": bool(s.get("live")),
            "child_sessions": int(s.get("child_sessions") or 0),
            "child_runs": int(s.get("child_runs") or 0),
            "lease_until": s.get("lease_until"),
            "pid": s.get("pid"),
        })

    return {
        "work_model": work_model,
        "sessions": sessions,
        "mode": mode,
        "generated_at": at,
        "source": "coord",
        "group_by": group_by,
    }


_HELD_CLAIM_STATUSES = ("running", "paused", "blocked")
_TERMINAL_INTENT_STATES = ("done", "archived", "superseded", "cancelled", "canceled", "closed")


def health_summary(conn, at: float | None = None) -> dict:
    at = at if at is not None else coord_db.db_now(conn)
    rows = coord_db.board_rows(conn, at=at)

    open_rows: list[dict] = []
    running = blocked = attention = 0
    for row in rows:
        status = str(row.get("status") or "")
        intent = str(row.get("intent_state") or "planned")
        canonical_open = row.get("archived_at") is None and intent in (
            "running", "blocked", "queued", "planned",
        )
        derived_attention = status == "attention"
        if canonical_open or derived_attention:
            open_rows.append(row)
        if status == "running":
            running += 1
        elif status == "blocked":
            blocked += 1
        elif status == "attention":
            attention += 1

    open_ids = [str(r.get("work_id")) for r in open_rows if r.get("work_id")]
    last_event_by_work: dict[str, float] = {}
    if open_ids:
        placeholders = ",".join("?" for _ in open_ids)
        for wid, ts in conn.execute(
            f"SELECT work_id, MAX(ts) FROM events WHERE work_id IN ({placeholders})"
            " GROUP BY work_id",
            tuple(open_ids),
        ).fetchall():
            last_event_by_work[str(wid)] = float(ts)

    stale_cutoff = at - 14 * 86400
    stale_14d = 0
    for row in open_rows:
        wid = str(row.get("work_id"))
        reference_ts = last_event_by_work.get(wid)
        if reference_ts is None:
            created = row.get("created_at")
            reference_ts = float(created) if created is not None else None
        if reference_ts is None or reference_ts < stale_cutoff:
            stale_14d += 1

    review_open = sum(1 for wid in open_ids if "-REVIEW" in wid)

    week_cutoff = at - 7 * 86400
    created_row = conn.execute(
        "SELECT COUNT(*) FROM work_items WHERE work_id LIKE '%-REVIEW%' AND created_at >= ?",
        (week_cutoff,),
    ).fetchone()
    review_created_7d = int(created_row[0]) if created_row else 0
    closed_placeholders = ",".join("?" for _ in _TERMINAL_INTENT_STATES)
    closed_row = conn.execute(
        "SELECT COUNT(*) FROM work_items WHERE work_id LIKE '%-REVIEW%'"
        f" AND intent_state IN ({closed_placeholders}) AND updated_at >= ?",
        (*_TERMINAL_INTENT_STATES, week_cutoff),
    ).fetchone()
    review_closed_7d = int(closed_row[0]) if closed_row else 0

    held_placeholders = ",".join("?" for _ in _HELD_CLAIM_STATUSES)
    phantom_row = conn.execute(
        "SELECT COUNT(*) FROM work_items w WHERE w.intent_state = 'running' AND NOT EXISTS ("
        f" SELECT 1 FROM claims c WHERE c.work_id = w.work_id AND c.status IN ({held_placeholders}))",
        tuple(_HELD_CLAIM_STATUSES),
    ).fetchone()
    phantom_no_claim = int(phantom_row[0]) if phantom_row else 0

    wip_by_lane: dict[str, int] = {}
    for actor, count in conn.execute(
        "SELECT COALESCE(s.actor, 'unknown'), COUNT(*) FROM claims c"
        " JOIN agent_sessions s ON s.session_id = c.session_id"
        " WHERE c.status = 'running' GROUP BY COALESCE(s.actor, 'unknown')"
    ).fetchall():
        wip_by_lane[str(actor)] = int(count)

    active_request_states = ("running", "blocked", "attention")
    request_rows = conn.execute(
        "WITH latest AS ("
        " SELECT rc.recipient_lane,e.work_id,e.kind,MAX(e.event_id) AS request_event_id"
        " FROM request_consumption rc JOIN events e ON e.event_id=rc.request_event_id"
        " GROUP BY rc.recipient_lane,e.work_id,e.kind"
        ")"
        " SELECT e.event_id,e.ts,e.kind,rc.recipient_lane,e.payload_json,"
        " rc.consumed_at,w.intent_state,"
        " EXISTS("
        "   SELECT 1 FROM events verdict"
        "   WHERE e.kind='audit_request'"
        "     AND verdict.work_id=e.work_id"
        "     AND verdict.kind='audit_verdict'"
        "     AND verdict.event_id>e.event_id"
        "     AND lower(COALESCE(verdict.actor,''))="
        "         lower(COALESCE(rc.recipient_lane,''))"
        "     AND upper(COALESCE(verdict.verdict,''))"
        "         IN ('PASS','FLAG','BLOCKED')"
        " ) AS exact_verdict_after_request"
        " FROM latest l"
        " JOIN request_consumption rc"
        "   ON rc.recipient_lane=l.recipient_lane"
        "  AND rc.work_id=l.work_id"
        "  AND rc.request_event_id=l.request_event_id"
        " JOIN events e ON e.event_id=rc.request_event_id"
        " LEFT JOIN work_items w ON w.work_id=e.work_id"
    ).fetchall()
    past_sla = 0
    active_unacknowledged_by_lane: dict[str, int] = {}
    excluded_terminal = 0
    excluded_queued = 0
    for (
        _event_id,
        ts,
        kind,
        lane,
        payload_json,
        consumed_at,
        intent_state,
        exact_verdict_after_request,
    ) in request_rows:
        if consumed_at is not None or bool(exact_verdict_after_request):
            continue
        intent = str(intent_state or "")
        if intent not in active_request_states:
            if intent in coord_db.TERMINAL_WORK_STATES or not intent:
                excluded_terminal += 1
            else:
                excluded_queued += 1
            continue
        active_unacknowledged_by_lane[str(lane)] = (
            active_unacknowledged_by_lane.get(str(lane), 0) + 1
        )
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        default_sla = 1800 if str(kind) == "audit_request" else 3600
        try:
            sla_s = max(1, int(payload.get("sla_s") or default_sla))
        except (TypeError, ValueError):
            sla_s = default_sla
        if at - float(ts) > sla_s:
            past_sla += 1

    try:
        t0_unreviewed = review_integrity.owed_t0_verdict_summary(conn)
    except Exception as exc:
        t0_unreviewed = {"count": None, "error": str(exc)}
    try:
        t0_review_queue = review_integrity.review_ready_t0_summary(conn, at=at)
    except Exception as exc:
        t0_review_queue = {"count": None, "error": str(exc)}

    return {
        "open": len(open_rows),
        "running": running,
        "blocked": blocked,
        "attention": attention,
        "stale_14d": stale_14d,
        "review_open": review_open,
        "review_created_7d": review_created_7d,
        "review_closed_7d": review_closed_7d,
        "phantom_no_claim": phantom_no_claim,
        "wip_by_lane": wip_by_lane,
        "cross_lane_past_sla": past_sla,
        "unread_requests_by_lane": active_unacknowledged_by_lane,
        "request_sla_scope": "latest_unacknowledged_on_running_blocked_attention",
        "request_sla_excluded_terminal": excluded_terminal,
        "request_sla_excluded_queued_or_planned": excluded_queued,
        "t0_unreviewed": t0_unreviewed,
        "t0_review_queue": t0_review_queue,
        "generated_at": at,
    }
