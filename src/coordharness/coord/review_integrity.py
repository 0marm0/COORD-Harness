from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable

from . import coord_db
from .config import (
    configured_lanes as _configured_lanes,
    counterpart_lane as _counterpart_lane,
    lane_set as _independent_lanes,
    lanes_display as _lanes_display,
)

REASON_SELF_VERDICT = "self_verdict"
REASON_CROSS_ROW_VERDICT = "cross_row_verdict"
REASON_VERDICT_ABSENT = "verdict_absent"
REASON_MOOT_CLOSED = "moot_closed_without_verdict"
UNREVIEWED_REASONS = (
    REASON_SELF_VERDICT,
    REASON_CROSS_ROW_VERDICT,
    REASON_VERDICT_ABSENT,
    REASON_MOOT_CLOSED,
)

# The lanes whose verdicts can clear another lane's work are exactly the
# configured lanes (``COORD_LANES``). Independence is lane INEQUALITY with
# the author -- never membership in one hardcoded pair -- so a lane added by
# configuration reviews on the same terms, and still cannot clear itself.
_REVIEW_VERDICTS = frozenset({"PASS", "FLAG", "BLOCKED"})
_EXPLICIT_REVIEW_READY_REASONS = frozenset(
    {
        "awaiting_review",
        "awaiting_t0_review",
        "awaiting_independent_reviewer",
        "awaiting_operator_or_t0_review",
    }
)
_REQUEST_REVIEW_READY_REASONS = _EXPLICIT_REVIEW_READY_REASONS | {"", "blocked"}
_ACTIVE_REVIEW_STATES = frozenset({"running", "blocked", "attention"})
_MOOT_CLOSE_EVENT_KINDS = frozenset(
    {"board_hygiene_policy_moot_closed", "moot_close", "moot_closed"}
)
REASON_NEVER_CLAIMED_SUPERSEDED = "never_claimed_superseded_no_artifact"


def _own_row_audit_verdict_events(conn: sqlite3.Connection, work_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT event_id, actor, verdict, ts FROM events"
        " WHERE work_id=? AND kind='audit_verdict' ORDER BY event_id ASC",
        (work_id,),
    ).fetchall()


def _latest_audit_request_event(
    conn: sqlite3.Connection, work_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT event_id,actor,to_selector,ts,payload_json,refs_json"
        " FROM events WHERE work_id=? AND kind='audit_request'"
        " ORDER BY event_id DESC LIMIT 1",
        (work_id,),
    ).fetchone()


def _latest_acceptance_contract_repair_event(
    conn: sqlite3.Connection, work_id: str
) -> sqlite3.Row | None:

    return conn.execute(
        "SELECT event_id,actor,to_selector,ts,payload_json,refs_json"
        " FROM events WHERE work_id=? AND kind='acceptance_contract_repaired'"
        " ORDER BY event_id DESC LIMIT 1",
        (work_id,),
    ).fetchone()


def _find_cross_row_verdict_reference(
    conn: sqlite3.Connection, work_id: str, *, after_event_id: int = 0
) -> dict[str, Any] | None:
    like = f"%{work_id}%"
    row = conn.execute(
        "SELECT event_id, work_id, verdict FROM events"
        " WHERE kind='audit_verdict'"
        " AND upper(COALESCE(verdict,'')) IN ('PASS','FLAG','BLOCKED')"
        " AND event_id > ? AND work_id != ? AND ("
        "   refs_json LIKE ? OR payload_json LIKE ? OR"
        "   COALESCE(title,'') LIKE ? OR COALESCE(body,'') LIKE ?"
        " ) ORDER BY event_id ASC LIMIT 1",
        (int(after_event_id), work_id, like, like, like, like),
    ).fetchone()
    if row is None:
        return None
    return {
        "event_id": int(row["event_id"]),
        "work_id": str(row["work_id"]),
        "verdict": str(row["verdict"] or "").strip().upper(),
    }


def _own_row_moot_closed(conn: sqlite3.Connection, work_id: str) -> bool:
    placeholders = ",".join("?" for _ in _MOOT_CLOSE_EVENT_KINDS)
    return (
        conn.execute(
            f"SELECT 1 FROM events WHERE work_id=? AND kind IN ({placeholders}) LIMIT 1",
            (work_id, *_MOOT_CLOSE_EVENT_KINDS),
        ).fetchone()
        is not None
    )


def _never_claimed_superseded_without_artifact(
    conn: sqlite3.Connection,
    work_id: str,
    row: dict[str, Any] | sqlite3.Row,
) -> bool:

    row_d = dict(row)
    if str(row_d.get("intent_state") or "").strip().lower() != "superseded":
        return False
    if conn.execute(
        "SELECT 1 FROM claims WHERE work_id=? LIMIT 1", (work_id,)
    ).fetchone() is not None:
        return False
    claim_kinds = tuple(f"{lane}_claim" for lane in _configured_lanes())
    if conn.execute(
        "SELECT 1 FROM events WHERE work_id=?"
        f" AND kind IN ({','.join('?' for _ in claim_kinds)}) LIMIT 1",
        (work_id, *claim_kinds),
    ).fetchone() is not None:
        return False
    if coord_db.done_signal_satisfied(conn, row_d.get("done_signal")):
        return False
    supersession = conn.execute(
        "SELECT 1 FROM events WHERE work_id=? AND ("
        " lower(COALESCE(body,'')) LIKE '%superseded by%' OR"
        " lower(COALESCE(title,'')) LIKE '%superseded by%' OR"
        " lower(COALESCE(payload_json,'')) LIKE '%superseded_by%'"
        " ) LIMIT 1",
        (work_id,),
    ).fetchone()
    return supersession is not None


def classify_verdict_status(
    conn: sqlite3.Connection, work_id: str, row: dict[str, Any] | sqlite3.Row | None = None
) -> dict[str, Any]:
    if row is None:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
    if row is None:
        return {"work_id": work_id, "reviewed": True, "reason": "row_missing"}

    if _never_claimed_superseded_without_artifact(conn, work_id, row):
        return {
            "work_id": work_id,
            "reviewed": True,
            "reason": REASON_NEVER_CLAIMED_SUPERSEDED,
        }

    author_lane = coord_db._latest_claim_author_lane_unlocked(conn, work_id)
    latest_request = _latest_audit_request_event(conn, work_id)
    latest_request_event_id = (
        int(latest_request["event_id"]) if latest_request is not None else 0
    )
    latest_repair = _latest_acceptance_contract_repair_event(conn, work_id)
    latest_repair_event_id = (
        int(latest_repair["event_id"]) if latest_repair is not None else 0
    )
    latest_review_barrier_event_id = max(
        latest_request_event_id,
        latest_repair_event_id,
    )
    if (
        latest_review_barrier_event_id == 0
        and coord_db._has_valid_operator_ok_unlocked(conn, work_id)
    ):
        return {"work_id": work_id, "reviewed": True, "reason": "operator_ok"}
    own_events = _own_row_audit_verdict_events(conn, work_id)
    own_verdicts = [
        event
        for event in own_events
        if int(event["event_id"]) > latest_review_barrier_event_id
        and str(event["verdict"] or "").strip().upper() in _REVIEW_VERDICTS
    ]

    for event in own_verdicts:
        actor = str(event["actor"] or "").strip().lower()
        if actor in _independent_lanes() and actor != author_lane:
            return {
                "work_id": work_id,
                "reviewed": True,
                "reason": "independent_verdict",
                "verdict_event_id": int(event["event_id"]),
                "verdict": str(event["verdict"] or "").strip().upper(),
                "verdict_actor": actor,
                "latest_request_event_id": latest_request_event_id or None,
                "latest_acceptance_contract_repair_event_id": (
                    latest_repair_event_id or None
                ),
                "latest_review_barrier_event_id": (
                    latest_review_barrier_event_id or None
                ),
            }

    if own_verdicts:
        event = own_verdicts[-1]
        return {
            "work_id": work_id,
            "reviewed": False,
            "reason": REASON_SELF_VERDICT,
            "verdict_event_id": int(event["event_id"]),
            "verdict": str(event["verdict"] or "").strip().upper(),
            "verdict_actor": str(event["actor"] or "").strip().lower(),
            "author_lane": author_lane,
            "latest_request_event_id": latest_request_event_id or None,
            "latest_acceptance_contract_repair_event_id": (
                latest_repair_event_id or None
            ),
            "latest_review_barrier_event_id": latest_review_barrier_event_id or None,
        }

    laundered = _find_cross_row_verdict_reference(
        conn, work_id, after_event_id=latest_review_barrier_event_id
    )
    if laundered is not None:
        return {
            "work_id": work_id,
            "reviewed": False,
            "reason": REASON_CROSS_ROW_VERDICT,
            "laundered_from_work_id": laundered["work_id"],
            "laundered_from_event_id": laundered["event_id"],
            "laundered_verdict": laundered["verdict"],
            "author_lane": author_lane,
            "latest_request_event_id": latest_request_event_id or None,
            "latest_acceptance_contract_repair_event_id": (
                latest_repair_event_id or None
            ),
            "latest_review_barrier_event_id": latest_review_barrier_event_id or None,
        }

    if _own_row_moot_closed(conn, work_id):
        return {
            "work_id": work_id,
            "reviewed": False,
            "reason": REASON_MOOT_CLOSED,
            "author_lane": author_lane,
            "latest_request_event_id": latest_request_event_id or None,
            "latest_acceptance_contract_repair_event_id": (
                latest_repair_event_id or None
            ),
            "latest_review_barrier_event_id": latest_review_barrier_event_id or None,
        }

    return {
        "work_id": work_id,
        "reviewed": False,
        "reason": REASON_VERDICT_ABSENT,
        "author_lane": author_lane,
        "latest_request_event_id": latest_request_event_id or None,
        "latest_acceptance_contract_repair_event_id": latest_repair_event_id or None,
        "latest_review_barrier_event_id": latest_review_barrier_event_id or None,
    }


def _priority_class(row: dict[str, Any]) -> str:
    module = str(row.get("module") or "").strip().lower()
    sublane = str(row.get("sublane") or "").strip().lower()
    title = str(row.get("title") or "").strip().lower()
    text = " ".join((module, sublane, title))
    if "leak" in text or "serving_quality" in text or "fail-open" in text:
        return "served_value_or_fail_open"
    if any(
        token in text
        for token in ("served", "dashboard", "surface", "render_authority", "promotion")
    ):
        return "served_surface"
    return "t0_artifact_or_binding"


def review_ready_t0_queue(
    conn: sqlite3.Connection,
    *,
    reviewer_lane: str | None = None,
    limit: int | None = None,
    at: float | None = None,
) -> list[dict[str, Any]]:

    clean_reviewer = str(reviewer_lane or "").strip().lower() or None
    if clean_reviewer is not None and clean_reviewer not in _independent_lanes():
        raise ValueError(f"reviewer_lane must be {_lanes_display()}")
    now = float(at if at is not None else time.time())
    state_placeholders = ",".join("?" for _ in _ACTIVE_REVIEW_STATES)
    rows = conn.execute(
        "SELECT * FROM work_items"
        f" WHERE lower(intent_state) IN ({state_placeholders})"
        " ORDER BY updated_at ASC, work_id ASC",
        tuple(_ACTIVE_REVIEW_STATES),
    ).fetchall()

    queue: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        work_id = str(row.get("work_id") or "").strip()
        if not work_id:
            continue
        if coord_db.effective_review_tier_for_work(conn, work_id, row=row) != "T0":
            continue
        author_lane = coord_db._latest_claim_author_lane_unlocked(conn, work_id)
        if author_lane not in _independent_lanes():
            continue
        # Any configured lane that is not the author's can clear this row. When
        # a reviewer asks for its own queue, the only rows it must not see are
        # the ones it wrote itself -- the same guarantee the two-lane version
        # gave, stated as the inequality it always actually was.
        if clean_reviewer is not None and clean_reviewer == author_lane:
            continue
        required_reviewer = clean_reviewer or _counterpart_lane(author_lane)
        if required_reviewer is None:
            continue
        latest_request = _latest_audit_request_event(conn, work_id)
        reason_class = str(row.get("blocked_reason_class") or "").strip().lower()
        explicitly_ready = reason_class in _EXPLICIT_REVIEW_READY_REASONS
        if latest_request is None and not explicitly_ready:
            continue
        if latest_request is not None and reason_class not in _REQUEST_REVIEW_READY_REASONS:
            continue
        if not coord_db.done_signal_satisfied(conn, row.get("done_signal")):
            continue
        status = classify_verdict_status(conn, work_id, row=row)
        if status["reviewed"]:
            continue

        request_event_id = (
            int(latest_request["event_id"]) if latest_request is not None else None
        )
        request_ts = (
            float(latest_request["ts"])
            if latest_request is not None
            else float(row.get("updated_at") or now)
        )
        queue.append(
            {
                "work_id": work_id,
                "author_lane": author_lane,
                "required_reviewer_lane": required_reviewer,
                "intent_state": str(row.get("intent_state") or ""),
                "blocked_reason_class": reason_class or None,
                "done_signal": str(row.get("done_signal") or ""),
                "request_event_id": request_event_id,
                "request_source": (
                    "audit_request" if latest_request is not None else "explicit_review_ready"
                ),
                "request_ts": request_ts,
                "request_age_s": max(0.0, now - request_ts),
                "priority_class": _priority_class(row),
                "integrity_status": status["reason"],
            }
        )

    priority_order = {
        "served_value_or_fail_open": 0,
        "served_surface": 1,
        "t0_artifact_or_binding": 2,
    }
    queue.sort(
        key=lambda item: (
            priority_order.get(str(item["priority_class"]), 99),
            float(item["request_ts"]),
            str(item["work_id"]),
        )
    )
    return queue[: int(limit)] if limit is not None else queue


def review_ready_t0_summary(
    conn: sqlite3.Connection, *, at: float | None = None
) -> dict[str, Any]:

    now = float(at if at is not None else time.time())
    queue = review_ready_t0_queue(conn, at=now)
    by_author = {lane: 0 for lane in sorted(_independent_lanes())}
    by_reviewer = {lane: 0 for lane in sorted(_independent_lanes())}
    by_priority: dict[str, int] = {}
    for item in queue:
        by_author[str(item["author_lane"])] += 1
        by_reviewer[str(item["required_reviewer_lane"])] += 1
        priority = str(item["priority_class"])
        by_priority[priority] = by_priority.get(priority, 0) + 1
    cutoff = now - 86400
    arrivals_24h = int(
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='audit_request' AND ts>=?",
            (cutoff,),
        ).fetchone()[0]
    )
    verdicts_24h = int(
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='audit_verdict' AND ts>=?",
            (cutoff,),
        ).fetchone()[0]
    )
    return {
        "count": len(queue),
        "oldest_request_age_s": (
            max(float(item["request_age_s"]) for item in queue) if queue else 0.0
        ),
        "by_author_lane": by_author,
        "by_required_reviewer_lane": by_reviewer,
        "by_priority_class": by_priority,
        "arrivals_24h": arrivals_24h,
        "verdicts_24h": verdicts_24h,
        "net_arrivals_minus_verdicts_24h": arrivals_24h - verdicts_24h,
        "sample_work_ids": [item["work_id"] for item in queue[:8]],
        "service_policy": "two_passes_daily_four_sequential_slots_no_same_lane_pass",
    }


def owed_t0_verdicts(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    recent_only_rows: int | None = None,
) -> list[dict[str, Any]]:
    terminal_placeholders = ",".join("?" for _ in coord_db.TERMINAL_WORK_STATES)
    query = (
        "SELECT * FROM work_items WHERE lower(intent_state) IN "
        f"({terminal_placeholders}) ORDER BY updated_at DESC"
    )
    params: tuple[Any, ...] = tuple(coord_db.TERMINAL_WORK_STATES)
    if recent_only_rows is not None:
        query += " LIMIT ?"
        params = params + (int(recent_only_rows),)
    rows = conn.execute(query, params).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        row_d = dict(row)
        work_id = str(row_d.get("work_id") or "")
        if not work_id:
            continue
        tier = coord_db.effective_review_tier_for_work(conn, work_id, row=row_d)
        if tier != "T0":
            continue
        status = classify_verdict_status(conn, work_id, row=row_d)
        if status["reviewed"]:
            continue
        results.append(status)
        if limit is not None and len(results) >= limit:
            break
    return results


def owed_t0_verdict_summary(
    conn: sqlite3.Connection, *, recent_only_rows: int | None = 300
) -> dict[str, Any]:
    owed = owed_t0_verdicts(conn, recent_only_rows=recent_only_rows)
    by_reason: dict[str, int] = {reason: 0 for reason in UNREVIEWED_REASONS}
    for item in owed:
        reason = str(item.get("reason") or "")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "count": len(owed),
        "by_reason": by_reason,
        "sample_work_ids": [item["work_id"] for item in owed[:5]],
        "scanned_recent_rows": recent_only_rows,
    }


def owed_t0_verdicts_for_ids(
    conn: sqlite3.Connection, work_ids: Iterable[str]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for work_id in work_ids:
        work_id = str(work_id or "").strip()
        if not work_id:
            continue
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        if row is None:
            results.append({"work_id": work_id, "reviewed": True, "reason": "row_missing"})
            continue
        row_d = dict(row)
        tier = coord_db.effective_review_tier_for_work(conn, work_id, row=row_d)
        if tier != "T0":
            results.append({"work_id": work_id, "reviewed": True, "reason": "not_t0", "tier": tier})
            continue
        status = classify_verdict_status(conn, work_id, row=row_d)
        if not status["reviewed"]:
            results.append(status)
    return results
