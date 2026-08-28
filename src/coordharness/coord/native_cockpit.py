from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import time
from typing import Any

from coordharness import config as _harness_config
from . import coord_db
from coordharness.jobs import sidecar_snapshot, status as jobstatus

CONTRACT = "native_cockpit.v1"

PROJECTION_MAINTENANCE_EXCLUSION = (
    _harness_config.state_dir()
    / "coord"
    / "mcp_v15_projection_maintenance.lock"
)
SCHEMA_VERSION = 1
STALE_AFTER_S = 300.0
JOB_PROGRESS_STALE_S = 15 * 60.0
JOB_PROGRESS_RUNNING_STATES = {"running", "in-progress", "in_progress", "active", "live"}
JOB_PROGRESS_PAUSED_STATES = {"paused", "waiting"}
JOB_PROGRESS_BLOCKED_STATES = {"blocked", "failed", "stalled"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


ROW_COLUMNS = (
    "writer_seq", "row_version", "dedup_key", "row_id", "id", "job_id",
    "roadmap_id", "coord_work_id", "bucket", "bucket_order", "display_order",
    "display", "name", "owner", "owners", "owner_group", "assignee",
    "owner_session_id", "owner_session_actor", "owner_session_label",
    "owner_external_thread_id", "owner_conversation_title", "owner_worktree_id",
    "handoff_from", "handoff_to", "module", "module_label", "domain_label",
    "domain_short_label", "surface", "parent", "status", "operator_state",
    "intent_state", "visibility", "blocked_reason_class", "pct", "pct_display",
    "eta_s", "eta_text", "eta_derived", "rate", "done", "total",
    "progress_kind", "has_progress", "determinate", "why_text", "note_text",
    "detail", "current_step", "done_signal", "done_signal_exists",
    "acceptance_summary", "context_pack_ref", "priority", "next_rank",
    "next_rank_reason", "queue_position", "queue_status", "queue_launchable",
    "pid", "pgid", "live", "paused", "kind", "resource_class",
    "sidecar_age_s", "stale", "available_actions", "unsafe_actions",
    "requires_confirmation",
    "effective_epic", "parent_id", "sublane", "tier", "group_key", "group_label",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS native_projection_meta (
          contract TEXT PRIMARY KEY,
          schema_version INTEGER NOT NULL,
          writer_seq INTEGER NOT NULL,
          built_at REAL NOT NULL,
          source_version TEXT,
          stale INTEGER NOT NULL DEFAULT 0,
          refreshing INTEGER NOT NULL DEFAULT 0,
          error_code TEXT,
          error_text TEXT,
          mode TEXT,
          live_mode TEXT,
          row_count INTEGER NOT NULL DEFAULT 0,
          action_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS native_projection_refresh_queue (
          contract TEXT PRIMARY KEY,
          generation INTEGER NOT NULL,
          first_requested_at REAL NOT NULL,
          latest_requested_at REAL NOT NULL,
          request_count INTEGER NOT NULL,
          reason TEXT
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_rows (
          writer_seq INTEGER NOT NULL,
          row_version INTEGER NOT NULL,
          dedup_key TEXT NOT NULL,
          row_id TEXT,
          id TEXT,
          job_id TEXT,
          roadmap_id TEXT,
          coord_work_id TEXT,
          bucket TEXT NOT NULL,
          bucket_order INTEGER NOT NULL,
          display_order INTEGER NOT NULL,
          display TEXT,
          name TEXT,
          owner TEXT,
          owners TEXT,
          owner_group TEXT,
          assignee TEXT,
          owner_session_id TEXT,
          owner_session_actor TEXT,
          owner_session_label TEXT,
          owner_external_thread_id TEXT,
          owner_conversation_title TEXT,
          owner_worktree_id TEXT,
          handoff_from TEXT,
          handoff_to TEXT,
          module TEXT,
          module_label TEXT,
          domain_label TEXT,
          domain_short_label TEXT,
          surface TEXT,
          parent TEXT,
          status TEXT,
          operator_state TEXT,
          intent_state TEXT,
          visibility TEXT,
          blocked_reason_class TEXT,
          pct REAL,
          pct_display TEXT,
          eta_s REAL,
          eta_text TEXT,
          eta_derived INTEGER NOT NULL DEFAULT 0,
          rate REAL,
          done REAL,
          total REAL,
          progress_kind TEXT,
          has_progress INTEGER NOT NULL DEFAULT 0,
          determinate INTEGER NOT NULL DEFAULT 0,
          why_text TEXT,
          note_text TEXT,
          detail TEXT,
          current_step TEXT,
          done_signal TEXT,
          done_signal_exists INTEGER NOT NULL DEFAULT 0,
          acceptance_summary TEXT,
          context_pack_ref TEXT,
          priority INTEGER,
          next_rank INTEGER,
          next_rank_reason TEXT,
          queue_position INTEGER,
          queue_status TEXT,
          queue_launchable INTEGER NOT NULL DEFAULT 0,
          pid INTEGER,
          pgid INTEGER,
          live INTEGER NOT NULL DEFAULT 0,
          paused INTEGER NOT NULL DEFAULT 0,
          kind TEXT,
          resource_class TEXT,
          sidecar_age_s REAL,
          stale INTEGER NOT NULL DEFAULT 0,
          available_actions TEXT,
          unsafe_actions TEXT,
          requires_confirmation INTEGER NOT NULL DEFAULT 0,
          effective_epic TEXT,
          parent_id TEXT,
          sublane TEXT,
          tier TEXT,
          group_key TEXT,
          group_label TEXT,
          PRIMARY KEY (writer_seq, dedup_key)
        );
        CREATE INDEX IF NOT EXISTS ix_native_rows_bucket
          ON native_cockpit_rows(writer_seq, bucket_order, display_order);
        CREATE INDEX IF NOT EXISTS ix_native_rows_work
          ON native_cockpit_rows(coord_work_id, roadmap_id, job_id);
        CREATE TABLE IF NOT EXISTS native_cockpit_summary (
          writer_seq INTEGER NOT NULL,
          summary_key TEXT NOT NULL,
          value_num REAL,
          value_text TEXT,
          label TEXT,
          PRIMARY KEY (writer_seq, summary_key)
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_row_actions (
          writer_seq INTEGER NOT NULL,
          row_dedup_key TEXT NOT NULL,
          row_id TEXT,
          work_id TEXT,
          job_id TEXT,
          action TEXT NOT NULL,
          label TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          requires_confirmation INTEGER NOT NULL DEFAULT 0,
          disabled_reason TEXT,
          endpoint TEXT,
          method TEXT NOT NULL DEFAULT 'POST',
          sort_order INTEGER NOT NULL,
          PRIMARY KEY (writer_seq, row_dedup_key, action)
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_filter_options (
          writer_seq INTEGER NOT NULL,
          filter_key TEXT NOT NULL,
          value TEXT NOT NULL,
          label TEXT NOT NULL,
          count INTEGER NOT NULL,
          sort_order INTEGER NOT NULL,
          PRIMARY KEY (writer_seq, filter_key, value)
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_column_model (
          writer_seq INTEGER NOT NULL,
          column_key TEXT NOT NULL,
          label TEXT NOT NULL,
          min_width INTEGER NOT NULL,
          default_visible INTEGER NOT NULL,
          sort_order INTEGER NOT NULL,
          PRIMARY KEY (writer_seq, column_key)
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_group_model (
          writer_seq INTEGER NOT NULL,
          group_key TEXT NOT NULL,
          label TEXT NOT NULL,
          count INTEGER NOT NULL,
          sort_order INTEGER NOT NULL,
          PRIMARY KEY (writer_seq, group_key)
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_sessions (
          writer_seq INTEGER NOT NULL,
          session_id TEXT NOT NULL,
          actor TEXT,
          display TEXT,
          status TEXT,
          last_heartbeat REAL,
          heartbeat_fresh INTEGER NOT NULL DEFAULT 0,
          claim_count INTEGER NOT NULL DEFAULT 0,
          sort_order INTEGER NOT NULL,
          PRIMARY KEY (writer_seq, session_id)
        );
        CREATE TABLE IF NOT EXISTS native_cockpit_diagnostics (
          writer_seq INTEGER NOT NULL,
          diagnostic_key TEXT NOT NULL,
          severity TEXT NOT NULL DEFAULT 'info',
          label TEXT,
          value_text TEXT,
          detail TEXT,
          sort_order INTEGER NOT NULL,
          PRIMARY KEY (writer_seq, diagnostic_key)
        );
        """
    )
    existing_cols = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(native_cockpit_rows)").fetchall()
    }
    for col in (
        "owner_session_id",
        "owner_session_actor",
        "owner_session_label",
        "owner_external_thread_id",
        "owner_conversation_title",
        "owner_worktree_id",
        "effective_epic",
        "parent_id",
        "sublane",
        "tier",
        "group_key",
        "group_label",
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE native_cockpit_rows ADD COLUMN {col} TEXT")


def _bucket(status: str) -> tuple[str, int]:
    status_lc = str(status or "").strip().lower()
    if status_lc in {"running", "paused"}:
        return "running", 0
    if status_lc in {"attention", "blocked", "failed"}:
        return "attention", 1
    if status_lc in {"queued", "planned"}:
        return "next", 2
    if status_lc in {"done", "archived", "superseded", "cancelled", "canceled", "closed"}:
        return "done", 3
    return "next", 2


def _owner(row: dict[str, Any]) -> str:
    return str(
        row.get("owner_session_actor")
        or row.get("assignee")
        or row.get("actor")
        or ""
    ).strip().lower()


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    num = _num(value)
    return int(num) if num is not None else None


def _norm_key(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _SLUG_RE.sub("-", raw).strip("-")


def _identity_keys(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        key = _norm_key(value)
        if key and key not in out:
            out.append(key)
    return out


def _progress_age_s(sidecar: dict[str, Any], *, now: float) -> float | None:
    updated = jobstatus.parse_updated_at(sidecar.get("updated_at") or sidecar.get("updated"))
    if updated is None:
        updated = jobstatus.parse_updated_at(sidecar.get("last_progress_at"))
    if updated is None:
        for path in sidecar.get("merged_from") or ():
            try:
                updated = os.path.getmtime(path)
                break
            except OSError:
                continue
    if updated is None:
        return None
    return max(0.0, now - float(updated))


def _sidecar_index(*, now: float) -> dict[str, dict[str, Any]]:
    snap = sidecar_snapshot.load_snapshot(sidecar_snapshot.default_job_progress_dir())
    out: dict[str, dict[str, Any]] = {}
    for sidecar in snap.items:
        state = str(sidecar.get("state") or sidecar.get("status") or "").strip().lower()
        if state not in JOB_PROGRESS_RUNNING_STATES:
            continue
        age = _progress_age_s(sidecar, now=now)
        pid_alive = False
        pid = _int_or_none(sidecar.get("pid"))
        if pid is not None:
            try:
                os.kill(pid, 0)
                pid_alive = True
            except OSError:
                pid_alive = False
        if not pid_alive and (age is None or age > JOB_PROGRESS_STALE_S):
            continue
        canonical = jobstatus.canonical_id(sidecar)
        if not canonical:
            continue
        previous = out.get(canonical)
        if previous is None:
            out[canonical] = sidecar
            continue
        prev_age = _progress_age_s(previous, now=now)
        if prev_age is None or (age is not None and age <= prev_age):
            out[canonical] = sidecar
    return out


def _sidecar_lookup(
    progress_by_work: dict[str, dict[str, Any]], *, now: float
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for canonical, sidecar in progress_by_work.items():
        keys = _identity_keys(
            canonical,
            sidecar.get("roadmap_id"),
            sidecar.get("work_id"),
            sidecar.get("id"),
            sidecar.get("coord_work_id"),
            sidecar.get("job_id"),
            sidecar.get("display"),
            sidecar.get("title"),
            sidecar.get("name"),
        )
        for key in keys:
            existing = lookup.get(key)
            if existing is None:
                lookup[key] = sidecar
                continue
            existing_age = _progress_age_s(existing, now=now)
            age = _progress_age_s(sidecar, now=now)
            if existing_age is None or (age is not None and age <= existing_age):
                lookup[key] = sidecar
    return lookup


def _row_lookup_keys(row: dict[str, Any]) -> list[str]:
    return _identity_keys(
        row.get("work_id"),
        row.get("coord_work_id"),
        row.get("roadmap_id"),
        row.get("id"),
        row.get("job_id"),
        row.get("display"),
        row.get("title"),
        row.get("name"),
    )


def _find_progress_sidecar(
    row: dict[str, Any],
    lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for key in _row_lookup_keys(row):
        sidecar = lookup.get(key)
        if sidecar is not None:
            return sidecar
    return None


def _effective_epic(row: dict[str, Any]) -> str:
    raw = str(row.get("epic") or row.get("domain") or "").strip()
    return raw or "_unassigned"


def _humanize_epic_label(key: str) -> str:
    if not key or key == "_unassigned":
        return "Unassigned"
    text = re.sub(r"[_\-]+", " ", key).strip()
    return text.title() if text else "Unassigned"


def _priority_rank(value: Any) -> int:
    if value in (None, ""):
        return 9
    text = str(value).strip().upper()
    aliases = {"HIGH": 1, "MEDIUM": 5, "LOW": 8}
    if text in aliases:
        return aliases[text]
    if text.startswith("P") and text[1:].isdigit():
        return int(text[1:])
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 9


def _priority_sort_value(value: Any) -> float:

    return coord_db.priority_sort_value(_priority_rank(value))


def _eta_rank(row: dict[str, Any]) -> float:
    eta = _num(row.get("eta_s"))
    return eta if eta is not None else 1e12


def _resource_key(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("resource_class", "kind", "module", "title", "display", "name", "job_id", "coord_work_id")
    ).lower()
    if any(token in text for token in ("gpu", "mlx", "cuda")):
        return "gpu"
    if any(token in text for token in ("api", "network", "download", "fetch", "retrieval")):
        return "api"
    if any(token in text for token in ("cpu", "heavy", "ram")):
        return "cpu"
    return ""


def _active_loads(rows: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    lane_loads: dict[str, int] = {}
    resource_loads: dict[str, int] = {}
    for row in rows:
        if row.get("bucket") != "running":
            continue
        lane = _norm_key(row.get("module") or row.get("domain_label") or row.get("bucket"))
        if lane:
            lane_loads[lane] = lane_loads.get(lane, 0) + 1
        resource = _resource_key(row)
        if resource:
            resource_loads[resource] = resource_loads.get(resource, 0) + 1
    return lane_loads, resource_loads


def _readiness(row: dict[str, Any], resource_loads: dict[str, int]) -> tuple[int, str]:
    if row.get("queue_launchable") is False:
        return 1, "held"
    resource = _resource_key(row)
    if resource == "gpu" and resource_loads.get("gpu", 0) > 0:
        return 1, "lane busy"
    return 0, "ready"


def _rank_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lane_loads, resource_loads = _active_loads(payloads)
    ranked: list[dict[str, Any]] = []
    next_ord = 1
    for original_index, row in enumerate(payloads):
        out = dict(row)
        if out.get("bucket") == "next":
            priority = _priority_rank(out.get("priority"))
            readiness, readiness_label = _readiness(out, resource_loads)
            eta = _eta_rank(out)
            lane = _norm_key(out.get("module") or out.get("domain_label") or out.get("bucket"))
            lane_load = lane_loads.get(lane, 0)
            out["_sort_key"] = (
                out["bucket_order"],
                _priority_sort_value(out.get("priority")),
                readiness,
                eta,
                lane_load,
                original_index,
            )
            out["next_rank"] = next_ord
            eta_label = "eta unknown" if eta >= 1e12 else f"eta {int(eta)}s"
            out["next_rank_reason"] = f"P{priority} · {readiness_label} · {eta_label} · lane load {lane_load}"
            next_ord += 1
        else:
            progress_rank = -float(out.get("pct") or 0.0)
            out["_sort_key"] = (
                out["bucket_order"],
                _priority_sort_value(out.get("priority")),
                progress_rank,
                _norm_key(out.get("display") or out.get("name") or out.get("id")),
                original_index,
            )
        ranked.append(out)
    ranked.sort(key=lambda row: row.get("_sort_key") or ())
    for display_order, row in enumerate(ranked):
        row["display_order"] = display_order
    next_ord = 1
    for row in ranked:
        if row.get("bucket") == "next":
            row["next_rank"] = next_ord
            next_ord += 1
        row.pop("_sort_key", None)
    return ranked


def _overlay_progress(payload: dict[str, Any], sidecar: dict[str, Any]) -> None:
    done = _num(sidecar.get("rows_done", sidecar.get("done")))
    total = _num(sidecar.get("total"))
    pct = (done / total * 100.0) if done is not None and total and total > 0 else _num(sidecar.get("pct"))
    rate = _num(sidecar.get("rate") or sidecar.get("docs_per_s"))
    eta_s = _num(sidecar.get("eta_s"))
    if eta_s is None:
        eta_min = _num(sidecar.get("eta_min"))
        eta_s = eta_min * 60.0 if eta_min is not None else None
    state = str(sidecar.get("state") or sidecar.get("status") or "").strip().lower()
    if state in JOB_PROGRESS_RUNNING_STATES:
        payload["bucket"], payload["bucket_order"] = _bucket("running")
        payload["status"] = "RUNNING"
    elif state in JOB_PROGRESS_PAUSED_STATES:
        payload["bucket"], payload["bucket_order"] = _bucket("paused")
        payload["status"] = "PAUSED"
    elif state in JOB_PROGRESS_BLOCKED_STATES:
        payload["bucket"], payload["bucket_order"] = _bucket(state)
        payload["status"] = state.upper()
    payload["job_id"] = str(sidecar.get("job_id") or payload.get("job_id") or "")
    payload["pid"] = _int_or_none(sidecar.get("pid"))
    payload["pgid"] = _int_or_none(sidecar.get("pgid"))
    payload["kind"] = sidecar.get("kind") or payload.get("kind") or ""
    payload["resource_class"] = sidecar.get("resource_class") or payload.get("resource_class") or ""
    payload["sidecar_age_s"] = _progress_age_s(sidecar, now=time.time())
    payload["live"] = 1 if state in JOB_PROGRESS_RUNNING_STATES else payload.get("live", 0)
    payload["paused"] = 1 if state in JOB_PROGRESS_PAUSED_STATES else 0
    payload["current_step"] = sidecar.get("step") or sidecar.get("phase") or payload.get("current_step") or ""
    if payload["current_step"]:
        payload["detail"] = payload["current_step"]

    has_progress = pct is not None or (done is not None and total is not None)
    if not has_progress:
        payload["progress_kind"] = "indeterminate" if state in JOB_PROGRESS_RUNNING_STATES else payload["progress_kind"]
        return

    payload["pct"] = pct
    payload["pct_display"] = jobstatus.pct_display(done, total, pct) or "—"
    payload["eta_s"] = eta_s
    payload["eta_text"] = jobstatus.format_eta(eta_s)
    payload["eta_derived"] = 0
    payload["rate"] = rate
    payload["done"] = done
    payload["total"] = total
    payload["progress_kind"] = "count" if done is not None and total is not None else "pct"
    payload["has_progress"] = 1
    payload["determinate"] = 1


def _row_payload(row: dict[str, Any], *, writer_seq: int, display_order: int) -> dict[str, Any]:
    work_id = str(row.get("work_id") or "").strip()
    status_lc = str(row.get("status") or row.get("intent_state") or "planned").strip().lower()
    bucket, bucket_order = _bucket(status_lc)
    owner = _owner(row)
    note = str(row.get("note") or row.get("claim_step") or row.get("blocked_reason_class") or "")
    detail = str(row.get("claim_step") or row.get("blocked_reason_class") or note)
    has_artifact = 1 if row.get("has_artifact") else 0
    effective_epic = _effective_epic(row)
    group_label = _humanize_epic_label(effective_epic)
    payload = {
        "writer_seq": writer_seq,
        "row_version": writer_seq,
        "dedup_key": work_id,
        "row_id": work_id,
        "id": work_id,
        "job_id": work_id,
        "roadmap_id": work_id,
        "coord_work_id": work_id,
        "bucket": bucket,
        "bucket_order": bucket_order,
        "display_order": display_order,
        "display": row.get("display") or row.get("title") or work_id,
        "name": row.get("title") or row.get("display") or work_id,
        "owner": owner,
        "owners": json.dumps([owner] if owner else []),
        "owner_group": owner,
        "assignee": owner,
        "owner_session_id": row.get("owner_session_id") or "",
        "owner_session_actor": row.get("owner_session_actor") or owner,
        "owner_session_label": row.get("owner_session_label") or "",
        "owner_external_thread_id": row.get("owner_external_thread_id") or "",
        "owner_conversation_title": row.get("owner_conversation_title") or "",
        "owner_worktree_id": row.get("owner_worktree_id") or "",
        "handoff_from": "",
        "handoff_to": "",
        "module": row.get("module") or "",
        "module_label": row.get("module") or "",
        "domain_label": row.get("domain") or "",
        "domain_short_label": row.get("domain") or "",
        "surface": row.get("surface") or "job",
        "parent": row.get("parent_id") or "",
        "status": status_lc.upper(),
        "operator_state": row.get("operator_state") or "",
        "intent_state": row.get("intent_state") or "",
        "visibility": row.get("visibility") or "operator",
        "blocked_reason_class": row.get("blocked_reason_class") or "",
        "pct": None,
        "pct_display": "—",
        "eta_s": None,
        "eta_text": "—",
        "eta_derived": 0,
        "rate": None,
        "done": None,
        "total": None,
        "progress_kind": "none",
        "has_progress": 0,
        "determinate": 0,
        "why_text": note,
        "note_text": note,
        "detail": detail,
        "current_step": row.get("claim_step") or "",
        "done_signal": row.get("done_signal") or "",
        "done_signal_exists": has_artifact,
        "acceptance_summary": "",
        "context_pack_ref": row.get("context_pack_ref") or "",
        "priority": row.get("priority") if row.get("priority") not in ("", None) else None,
        "next_rank": None,
        "next_rank_reason": "",
        "queue_position": None,
        "queue_status": "",
        "queue_launchable": 0,
        "pid": None,
        "pgid": None,
        "live": 1 if status_lc == "running" else 0,
        "paused": 1 if status_lc == "paused" else 0,
        "kind": row.get("kind") or row.get("resource_class") or "",
        "resource_class": row.get("resource_class") or "",
        "sidecar_age_s": None,
        "stale": 1 if status_lc == "attention" else 0,
        "available_actions": "[]",
        "unsafe_actions": "[]",
        "requires_confirmation": 0,
        "effective_epic": effective_epic,
        "parent_id": row.get("parent_id") or "",
        "sublane": row.get("sublane") or "",
        "tier": row.get("effective_tier") or "",
        "group_key": effective_epic,
        "group_label": group_label,
    }
    sidecar = row.get("_job_progress_sidecar")
    if isinstance(sidecar, dict):
        _overlay_progress(payload, sidecar)
    return payload


def _unbound_sidecar_row(canonical_id: str, sidecar: dict[str, Any]) -> dict[str, Any]:
    title = str(
        sidecar.get("display")
        or sidecar.get("title")
        or sidecar.get("name")
        or sidecar.get("job_id")
        or canonical_id
    )
    step = str(sidecar.get("step") or sidecar.get("phase") or "unbound local job_progress sidecar")
    return {
        "work_id": canonical_id,
        "title": title,
        "display": title,
        "assignee": "local",
        "owner_session_label": sidecar.get("driver") or "",
        "status": "running",
        "module": "ops",
        "domain": "Local Jobs",
        "surface": "local",
        "visibility": "operator",
        "resource_class": sidecar.get("resource_class") or sidecar.get("kind") or "local",
        "kind": sidecar.get("kind") or sidecar.get("resource_class") or "local",
        "note": step,
        "claim_step": step,
        "blocked_reason_class": "unbound_job_progress",
        "_job_progress_sidecar": sidecar,
    }


def _insert_row(conn: sqlite3.Connection, table: str, payload: dict[str, Any], columns: tuple[str, ...]) -> None:
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        tuple(payload.get(col) for col in columns),
    )


def refresh(
    conn: sqlite3.Connection,
    *,
    mode: str | None = None,
    live_mode: str | None = None,
    source_version: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    built_at = coord_db.db_now(conn)
    rows = coord_db.board_rows(conn, group_by="module")
    progress_by_work = _sidecar_index(now=built_at)
    progress_lookup = _sidecar_lookup(progress_by_work, now=built_at)
    matched_progress: set[int] = set()
    for row in rows:
        sidecar = _find_progress_sidecar(row, progress_lookup)
        if sidecar is not None:
            row["_job_progress_sidecar"] = sidecar
            matched_progress.add(id(sidecar))
    unbound_rows = [
        _unbound_sidecar_row(work_id, sidecar)
        for work_id, sidecar in sorted(progress_by_work.items())
        if id(sidecar) not in matched_progress
    ]
    rows = list(rows) + unbound_rows
    sessions = coord_db.session_rollup(conn, at=built_at)
    previous = conn.execute(
        "SELECT COALESCE(MAX(writer_seq), 0) FROM native_projection_meta"
    ).fetchone()[0]
    writer_seq = int(previous or 0) + 1
    row_payloads = _rank_payloads([
        _row_payload(row, writer_seq=writer_seq, display_order=i)
        for i, row in enumerate(rows)
        if row.get("work_id")
    ])
    summary_counts = {
        "total": len(row_payloads),
        "running": sum(1 for r in row_payloads if r["bucket"] == "running"),
        "attention": sum(1 for r in row_payloads if r["bucket"] == "attention"),
        "next": sum(1 for r in row_payloads if r["bucket"] == "next"),
        "done": sum(1 for r in row_payloads if r["bucket"] == "done"),
        "stale": sum(1 for r in row_payloads if r["stale"]),
        "blocked": sum(1 for r in row_payloads if r["status"] == "BLOCKED"),
    }

    with coord_db.tx(conn):
        for table in (
            "native_cockpit_rows",
            "native_cockpit_summary",
            "native_cockpit_row_actions",
            "native_cockpit_filter_options",
            "native_cockpit_column_model",
            "native_cockpit_group_model",
            "native_cockpit_sessions",
            "native_cockpit_diagnostics",
        ):
            conn.execute(f"DELETE FROM {table}")
        for payload in row_payloads:
            _insert_row(conn, "native_cockpit_rows", payload, ROW_COLUMNS)
        for order, (key, value) in enumerate(summary_counts.items()):
            conn.execute(
                "INSERT INTO native_cockpit_summary(writer_seq, summary_key, value_num, value_text, label)"
                " VALUES(?,?,?,?,?)",
                (writer_seq, key, float(value), str(value), key.replace("_", " ").title()),
            )
        for order, key in enumerate(("display", "status", "owner", "module", "current_step")):
            conn.execute(
                "INSERT INTO native_cockpit_column_model(writer_seq, column_key, label, min_width,"
                " default_visible, sort_order) VALUES(?,?,?,?,?,?)",
                (writer_seq, key, key.replace("_", " ").title(), 90, 1, order),
            )
        for order, (bucket_key, _bucket_order) in enumerate((("running", 0), ("attention", 1), ("next", 2), ("done", 3))):
            conn.execute(
                "INSERT INTO native_cockpit_group_model(writer_seq, group_key, label, count, sort_order)"
                " VALUES(?,?,?,?,?)",
                (
                    writer_seq,
                    bucket_key,
                    bucket_key.replace("_", " ").title(),
                    summary_counts.get(bucket_key, 0),
                    order,
                ),
            )
        bucket_group_keys = {"running", "attention", "next", "done"}
        epic_counts: dict[str, dict[str, Any]] = {}
        for payload in row_payloads:
            key = str(payload.get("group_key") or "_unassigned")
            entry = epic_counts.setdefault(
                key, {"label": payload.get("group_label") or key, "count": 0}
            )
            entry["count"] += 1
        ranked_epics = sorted(
            epic_counts.items(), key=lambda kv: (-kv[1]["count"], kv[0])
        )
        for order, (key, info) in enumerate(ranked_epics, start=len(bucket_group_keys)):
            if key in bucket_group_keys:
                continue
            conn.execute(
                "INSERT INTO native_cockpit_group_model(writer_seq, group_key, label, count, sort_order)"
                " VALUES(?,?,?,?,?)",
                (writer_seq, key, info["label"], info["count"], order),
            )
        for order, session in enumerate(sessions):
            sid = str(session.get("session_id") or "")
            claim_count = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE session_id=? AND status IN ('running','paused','blocked')",
                (sid,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO native_cockpit_sessions(writer_seq, session_id, actor, display, status,"
                " last_heartbeat, heartbeat_fresh, claim_count, sort_order)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    writer_seq,
                    sid,
                    session.get("actor") or "",
                    session.get("human_label") or session.get("conversation_title") or sid,
                    "live" if session.get("live") else "stale",
                    session.get("lease_until"),
                    1 if session.get("live") else 0,
                    int(claim_count or 0),
                    order,
                ),
            )
        conn.execute(
            "INSERT INTO native_cockpit_diagnostics(writer_seq, diagnostic_key, severity, label,"
            " value_text, detail, sort_order) VALUES(?,?,?,?,?,?,?)",
            (writer_seq, "materializer", "info", "Native cockpit materializer", "fresh", "", 0),
        )
        conn.execute(
            "INSERT INTO native_projection_meta(contract, schema_version, writer_seq, built_at,"
            " source_version, stale, refreshing, error_code, error_text, mode, live_mode,"
            " row_count, action_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(contract) DO UPDATE SET"
            " schema_version=excluded.schema_version,"
            " writer_seq=excluded.writer_seq,"
            " built_at=excluded.built_at,"
            " source_version=excluded.source_version,"
            " stale=excluded.stale,"
            " refreshing=excluded.refreshing,"
            " error_code=excluded.error_code,"
            " error_text=excluded.error_text,"
            " mode=excluded.mode,"
            " live_mode=excluded.live_mode,"
            " row_count=excluded.row_count,"
            " action_count=excluded.action_count",
            (
                CONTRACT,
                SCHEMA_VERSION,
                writer_seq,
                built_at,
                source_version or "coord.db",
                0,
                0,
                None,
                None,
                mode,
                live_mode,
                len(row_payloads),
                0,
            ),
        )
    return {"writer_seq": writer_seq, "row_count": len(row_payloads), "built_at": built_at}


def request_refresh(
    conn: sqlite3.Connection,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    requested_at = coord_db.db_now(conn)
    with coord_db.tx(conn):
        conn.execute(
            "INSERT INTO native_projection_refresh_queue("
            "contract,generation,first_requested_at,latest_requested_at,request_count,reason)"
            " VALUES(?,1,?,?,1,?)"
            " ON CONFLICT(contract) DO UPDATE SET"
            " generation=native_projection_refresh_queue.generation+1,"
            " latest_requested_at=excluded.latest_requested_at,"
            " request_count=native_projection_refresh_queue.request_count+1,"
            " reason=excluded.reason",
            (CONTRACT, requested_at, requested_at, str(reason or "")[:200]),
        )
        row = conn.execute(
            "SELECT generation,first_requested_at,latest_requested_at,request_count,reason"
            " FROM native_projection_refresh_queue WHERE contract=?",
            (CONTRACT,),
        ).fetchone()
    return dict(row) if row is not None else {}


def flush_requested_refresh(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    min_interval_s: float = 5.0,
) -> dict[str, Any]:
    PROJECTION_MAINTENANCE_EXCLUSION.parent.mkdir(parents=True, exist_ok=True)
    exclusion = PROJECTION_MAINTENANCE_EXCLUSION.open("a+b")
    try:
        fcntl.flock(exclusion.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        exclusion.close()
        return {
            "flushed": False,
            "pending": True,
            "reason": "mcp_v15_projection_maintenance_exclusion",
        }
    try:
        return _flush_requested_refresh_unlocked(
            conn,
            force=force,
            min_interval_s=min_interval_s,
        )
    finally:
        fcntl.flock(exclusion.fileno(), fcntl.LOCK_UN)
        exclusion.close()


def _flush_requested_refresh_unlocked(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    min_interval_s: float = 5.0,
) -> dict[str, Any]:
    ensure_schema(conn)
    pending = conn.execute(
        "SELECT generation,first_requested_at,latest_requested_at,request_count,reason"
        " FROM native_projection_refresh_queue WHERE contract=?",
        (CONTRACT,),
    ).fetchone()
    if pending is None:
        return {"flushed": False, "pending": False, "reason": "no_request"}

    now = coord_db.db_now(conn)
    meta = conn.execute(
        "SELECT built_at FROM native_projection_meta WHERE contract=?",
        (CONTRACT,),
    ).fetchone()
    if (
        not force
        and meta is not None
        and now - float(meta["built_at"] or 0.0) < float(min_interval_s)
    ):
        return {
            "flushed": False,
            "pending": True,
            "reason": "minimum_interval",
            "generation": int(pending["generation"]),
            "request_count": int(pending["request_count"]),
        }

    generation = int(pending["generation"])
    request_count = int(pending["request_count"])
    result = refresh(
        conn,
        source_version=f"coord.db:batched-generation:{generation}",
    )
    with coord_db.tx(conn):
        cleared = conn.execute(
            "DELETE FROM native_projection_refresh_queue"
            " WHERE contract=? AND generation=?",
            (CONTRACT, generation),
        ).rowcount == 1
    return {
        **result,
        "flushed": True,
        "pending": not cleared,
        "generation": generation,
        "request_count": request_count,
        "queue_cleared": cleared,
    }


def mark_stale_if_old(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    max_age_s: float = STALE_AFTER_S,
) -> bool:
    ensure_schema(conn)
    at = coord_db.db_now(conn) if now is None else float(now)
    row = conn.execute(
        "SELECT built_at FROM native_projection_meta WHERE contract=?",
        (CONTRACT,),
    ).fetchone()
    if not row:
        return False
    stale = float(row["built_at"]) + float(max_age_s) < at
    if stale:
        with coord_db.tx(conn):
            conn.execute(
                "UPDATE native_projection_meta SET stale=1 WHERE contract=?",
                (CONTRACT,),
            )
    return stale
