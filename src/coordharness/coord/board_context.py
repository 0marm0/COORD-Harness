from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
import shlex
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from coordharness import config as _harness_config
from coordharness.coord import coord_db
from coordharness.coord import projection
from coordharness.coord.config import connect_ro
from coordharness.coord.row_classification import (
    has_active_claim as _shared_has_active_claim,
    has_non_generated_label as _shared_has_non_generated_label,
    is_telemetry_orphan,
)
from coordharness.jobs import status as job_status
from coordharness.jobs import diagnostic_marker as job_authority

MAX_SEARCH_ROWS = 100
MAX_SKELETON_ROWS = 100
MAX_CHANGE_ROWS = 100
TERMINAL_STATUSES = {"done", "failed", "archived", "superseded", "cancelled", "canceled", "closed", "skipped"}
OPEN_STATUSES = {"queued", "planned", "running", "blocked", "needs_verification", "artifact_present", "awaiting_artifact"}
ATTENTION_STATUSES = {"blocked", "failed", "needs_verification", "artifact_present"}
ATTENTION_PROOF_STATES = {"blocked", "failed", "needs_verification", "artifact_present", "missing_done_signal"}
CURATION_PROOF_STATES = {"missing_done_signal", "needs_verification", "artifact_present", "stale_path"}
TEXT_FIELDS = (
    "work_id",
    "title",
    "display",
    "module",
    "domain",
    "lane",
    "sublane",
    "assignee",
    "assigned_by",
    "done_signal",
    "context_pack_ref",
    "acceptance_json",
    "blocked_reason_class",
    "claim_step",
    "note",
    "depends_on",
    "kind",
    "tier",
)
COMPACT_FIELDS = (
    "status",
    "proof_state",
    "assignee",
    "module",
    "sublane",
    "lane",
    "priority",
    "parent_id",
    "owner_session_actor",
    "owner_session_label",
)

_REPO_ROOT = _harness_config.project_root()
_HARNESS_ROOT = _REPO_ROOT
_STATE_ROOT = _harness_config.state_dir()
_BACKLOG_PATH = _STATE_ROOT / "roadmap_backlog.json"
_JOB_PROGRESS_DIR = _harness_config.job_progress_dir()
_BOARD_CONTEXT_COMMAND = "python -m coordharness.coord.board_context"
MAX_TOKENIZE_CHARS = 4_000
MAX_SEARCH_TERMS = 32
MAX_ROW_FIELD_CHARS = 1_200

_POLICY_EPOCH_DOC_PATHS = (
    "docs/agent-protocol.md",
    "docs/review-tiers.md",
)
_RESOURCE_MODE_PATH = _STATE_ROOT / "resource_mode.txt"
MAX_CAPSULE_RUNNING = 10
MAX_CAPSULE_RESUME_INTENTS = 10
MAX_CAPSULE_DECISIONS = 10
MAX_CAPSULE_BYTES = 6_144


def _connect(db_path: str | Path | None = None):
    return connect_ro(Path(db_path) if db_path is not None else None)


def canonical_board_open_total(db_path: str | Path | None = None) -> int:

    conn = _connect(db_path)
    try:
        return int(projection.health_summary(conn)["open"])
    finally:
        conn.close()


def load_rows(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    if not _harness_config.is_strict_deployment():
        conn = _connect(db_path)
        try:
            rows = coord_db.board_rows(conn)
            for row in rows:
                row["query_core_mode"] = "generic_coord_db"
            return filter_visible_rows(rows)
        finally:
            conn.close()
    if db_path is not None:
        conn = _connect(db_path)
        try:
            exact_schema = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='coord_authority_policy'"
            ).fetchone()
            if exact_schema is None:
                rows = coord_db.board_rows(conn)
                for row in rows:
                    row["query_core_mode"] = "legacy_noncanonical_fixture"
                return filter_visible_rows(rows)
        finally:
            conn.close()
    from coordharness.coord.exact_query_core import load_query_snapshot

    snapshot = load_query_snapshot(db_path)
    return filter_visible_rows(snapshot.flat_ranked_rows(include_quarantine=True))


def load_recent_events(
    work_id: str,
    *,
    db_path: str | Path | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event_id, ts, kind, actor, session_id, to_selector, work_id,"
            " severity, verdict, trust, title, body, refs_json, payload_json"
            " FROM events WHERE work_id=? ORDER BY event_id DESC LIMIT ?",
            (work_id, max(0, min(int(limit), 50))),
        ).fetchall()
    finally:
        conn.close()
    return [_parse_event(dict(r)) for r in rows]


def _parse_event(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key, default in (("refs_json", []), ("payload_json", {})):
        raw = out.pop(key, None)
        try:
            out[key[:-5]] = json.loads(raw or json.dumps(default))
        except (TypeError, ValueError):
            out[key[:-5]] = default
    if out.get("body"):
        out["body"] = _short_text(out["body"], 320)
    return out


def _policy_epoch() -> dict[str, Any]:
    docs: list[str] = []
    subject: list[tuple[str, int, int]] = []
    for relative_path in _POLICY_EPOCH_DOC_PATHS:
        path = _HARNESS_ROOT / relative_path
        docs.append(str(path))
        try:
            stat = path.stat()
            subject.append((relative_path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            subject.append((relative_path, -1, -1))
    epoch = hashlib.sha256(json.dumps(subject, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return {"docs": docs, "epoch": epoch}


def _resource_mode() -> str | None:
    try:
        text = _RESOURCE_MODE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


_FALLBACK_DECISION_KINDS = frozenset(
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
    }
)


def _decision_event_kinds() -> frozenset[str]:
    kinds = getattr(coord_db, "_CLAIM_CAPSULE_DECISION_KINDS", None)
    if isinstance(kinds, (frozenset, set)) and kinds:
        return frozenset(kinds)
    return _FALLBACK_DECISION_KINDS


def _fallback_decision_event_line(row: dict[str, Any]) -> str:
    try:
        date = time.strftime("%Y-%m-%d", time.gmtime(float(row.get("ts") or 0.0)))
    except (TypeError, ValueError, OSError):
        date = "0000-00-00"
    kind = _text(row.get("kind")).strip() or "event"
    actor = _text(row.get("actor")).strip() or "system"
    snippet = _text(row.get("title")).strip() or _text(row.get("body")).strip()
    if not snippet:
        payload_raw = row.get("payload_json")
        try:
            payload = json.loads(payload_raw or "{}")
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            for key in ("task", "why", "summary", "reason", "message", "note", "verdict", "action"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    snippet = value
                    break
            if not snippet:
                for key in sorted(payload.keys()):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        snippet = value
                        break
    line = f"{date} {kind} {actor}"
    if snippet:
        line += f": {_short_text(snippet, 100)}"
    return line


def _render_decision_event_line(row: dict[str, Any]) -> str:
    renderer = getattr(coord_db, "_claim_capsule_event_line", None)
    if callable(renderer):
        try:
            line = renderer(row)
            if line:
                return line
        except Exception:
            pass
    return _fallback_decision_event_line(row)


def load_recent_decisions(db_path: str | Path | None = None, *, limit: int = MAX_CAPSULE_DECISIONS) -> list[str]:
    kinds = sorted(_decision_event_kinds())
    if not kinds:
        return []
    bounded_limit = max(0, min(int(limit), 50))
    if bounded_limit == 0:
        return []
    conn = _connect(db_path)
    try:
        placeholders = ",".join("?" for _ in kinds)
        fetch_limit = min(150, bounded_limit * 4)
        rows = conn.execute(
            "SELECT event_id, ts, kind, actor, title, body, payload_json FROM events"
            f" WHERE kind IN ({placeholders})"
            " ORDER BY (kind = 'decision') DESC, event_id DESC LIMIT ?",
            (*kinds, fetch_limit),
        ).fetchall()
        head_ids_fn = getattr(coord_db, "_decision_head_event_ids", None)
        try:
            live_decision_ids = head_ids_fn(conn) if callable(head_ids_fn) else None
        except Exception:
            live_decision_ids = None
    finally:
        conn.close()
    lines: list[str] = []
    for row in rows:
        d = dict(row)
        if (
            live_decision_ids is not None
            and str(d.get("kind")) == "decision"
            and int(d.get("event_id") or 0) not in live_decision_ids
        ):
            continue
        line = _render_decision_event_line(d)
        if line:
            lines.append(line)
        if len(lines) >= bounded_limit:
            break
    return lines


def _capsule_running_rows(rows: list[dict[str, Any]], *, limit: int = MAX_CAPSULE_RUNNING) -> list[dict[str, Any]]:
    running = sorted([r for r in rows if _status(r) == "running"], key=_sort_open)
    out: list[dict[str, Any]] = []
    for row in running[: max(0, int(limit))]:
        entry: dict[str, Any] = {
            "work_id": _row_id(row),
            "display": _short_text(row.get("display") or row.get("title") or _row_id(row), 90),
            "assignee": row.get("assignee") or None,
        }
        if row.get("claim_step"):
            entry["claim_step"] = _short_text(row.get("claim_step"), 100)
        out.append(entry)
    return out


def _capsule_resume_intents(rows: list[dict[str, Any]], *, limit: int = MAX_CAPSULE_RESUME_INTENTS) -> list[str]:
    parked = [
        r
        for r in rows
        if _status(r) in OPEN_STATUSES
        and (_text(r.get("next_step")).strip() or _text(r.get("resume_when")).strip())
    ]
    parked.sort(key=_sort_recent)
    lines: list[str] = []
    for row in parked[: max(0, int(limit))]:
        work_id = _row_id(row)
        next_step = _short_text(row.get("next_step"), 70) if _text(row.get("next_step")).strip() else ""
        resume_when = _short_text(row.get("resume_when"), 40) if _text(row.get("resume_when")).strip() else ""
        piece = f"{work_id}: {next_step}" if next_step else f"{work_id}: (resume_when only)"
        if resume_when:
            piece += f" [resume_when: {resume_when}]"
        lines.append(piece)
    return lines


def _capsule_json_bytes(payload: dict[str, Any]) -> int:
    try:
        compact = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return len(compact.encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _trim_capsule_to_budget(payload: dict[str, Any], *, max_bytes: int = MAX_CAPSULE_BYTES) -> dict[str, Any]:
    if _capsule_json_bytes(payload) <= max_bytes:
        return payload
    trimmed: dict[str, int] = {}
    for key in ("recent_decisions", "resume_intents", "running"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        while len(values) > 1 and _capsule_json_bytes(payload) > max_bytes:
            values.pop()
            trimmed[key] = trimmed.get(key, 0) + 1
    if trimmed:
        note = "trimmed for byte budget: " + ", ".join(f"{k}(-{v})" for k, v in trimmed.items())
        payload.setdefault("omitted", []).append(note)
    return payload


def build_capsule(
    rows: list[dict[str, Any]],
    *,
    db_path: str | Path | None = None,
    decision_limit: int = MAX_CAPSULE_DECISIONS,
    resume_limit: int = MAX_CAPSULE_RESUME_INTENTS,
    running_limit: int = MAX_CAPSULE_RUNNING,
) -> dict[str, Any]:
    visible = filter_visible_rows(rows)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "lens": "capsule",
        "generated_at": time.time(),
    }
    omitted: list[str] = []

    try:
        conn = _connect(db_path)
        try:
            payload["health_summary"] = projection.health_summary(conn)
        finally:
            conn.close()
    except Exception as exc:
        omitted.append(f"health_summary: {exc}")

    try:
        payload["running"] = _capsule_running_rows(visible, limit=running_limit)
    except Exception as exc:
        omitted.append(f"running: {exc}")

    try:
        payload["resume_intents"] = _capsule_resume_intents(visible, limit=resume_limit)
    except Exception as exc:
        omitted.append(f"resume_intents: {exc}")

    try:
        payload["recent_decisions"] = load_recent_decisions(db_path, limit=decision_limit)
    except Exception as exc:
        omitted.append(f"recent_decisions: {exc}")

    try:
        payload["policy_epoch"] = _policy_epoch()
    except Exception as exc:
        omitted.append(f"policy_epoch: {exc}")

    try:
        payload["resource_mode"] = _resource_mode()
    except Exception as exc:
        omitted.append(f"resource_mode: {exc}")

    payload["pointers"] = {
        "roadmap": "docs/roadmap.md",
        "review_tiers": "docs/review-tiers.md",
        "context_architecture": "docs/context-architecture.md",
        "focus_recipe": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
    }
    if omitted:
        payload["omitted"] = omitted
    return _trim_capsule_to_budget(payload)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=True)
        except TypeError:
            return str(value)
    return str(value)


def _fmt_ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return _text(value)


def _short_text(value: Any, max_chars: int = 180) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "..."


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{1,}", re.I)
_STOPWORDS = {
    "and", "are", "but", "can", "for", "from", "has", "have", "into", "not", "that", "the", "this", "with",
    "work", "item", "items", "task", "tasks", "job", "jobs", "row", "rows", "current", "default",
    "coordharness", "docs", "data", "local", "json", "markdown", "report", "reports", "spec", "md",
    "claude", "codex", "agent", "agents", "session", "sessions",
}


def tokenize(value: Any) -> set[str]:
    raw = _text(value).lower()
    if len(raw) > MAX_TOKENIZE_CHARS:
        raw = raw[:MAX_TOKENIZE_CHARS]
    expanded = re.sub(r"[_./:#-]+", " ", raw)
    tokens = {m.group(0).lower() for m in _TOKEN_RE.finditer(raw)}
    tokens.update(m.group(0).lower() for m in _TOKEN_RE.finditer(expanded))
    return {
        tok
        for tok in tokens
        if len(tok) > 2
        and tok not in _STOPWORDS
        and not tok.isdigit()
        and not re.fullmatch(r"n?\d{4,}", tok)
    }


def _row_blob(row: dict[str, Any]) -> str:
    return " ".join(_short_text(row.get(k), MAX_ROW_FIELD_CHARS) for k in TEXT_FIELDS if row.get(k) not in (None, ""))


def _row_terms(row: dict[str, Any]) -> set[str]:
    return tokenize(_row_blob(row))


def _status(row: dict[str, Any]) -> str:
    return _text(row.get("status") or row.get("intent_state")).lower()


def _needs_attention(row: dict[str, Any]) -> bool:
    return _status(row) in ATTENTION_STATUSES or _text(row.get("proof_state")).lower() in ATTENTION_PROOF_STATES


def _terminalish(row: dict[str, Any]) -> bool:
    return _status(row) in TERMINAL_STATUSES or _text(row.get("intent_state")).lower() in TERMINAL_STATUSES


def _row_id(row: dict[str, Any]) -> str:
    return _text(row.get("work_id") or row.get("id") or row.get("roadmap_id"))


_UUID_ONLY_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_THREAD_ID_RE = re.compile(r"019e[0-9a-f-]{20,}", re.I)
_HEX_ID_RE = re.compile(r"[0-9a-f]{12,40}", re.I)
_JOB_OBSERVER_RE = re.compile(r"job:[0-9a-f]{10,16}", re.I)
_TECHNICAL_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)+", re.I)


def _has_active_claim(row: dict[str, Any]) -> bool:
    return _shared_has_active_claim(row)


def _has_non_generated_label(row: dict[str, Any], work_id: str) -> bool:
    return _shared_has_non_generated_label(row, work_id)


def _has_actionable_metadata(row: dict[str, Any]) -> bool:
    for key in (
        "assignee",
        "assigned_by",
        "done_signal",
        "context_pack_ref",
        "blocked_reason_class",
        "parent_id",
        "note",
    ):
        if _text(row.get(key)).strip():
            return True
    acceptance = _text(row.get("acceptance_json")).strip()
    if acceptance and acceptance not in {"[]", "{}", "null", "None"}:
        return True
    return False


def is_operator_visible_row(row: dict[str, Any], canonical_ids: set[str] | None = None) -> bool:
    visibility = _text(row.get("visibility") or "operator").strip().lower()
    if visibility in {"hidden", "diagnostic", "internal", "session"}:
        return False

    work_id = _row_id(row).strip()
    if not work_id:
        return True
    if _has_active_claim(row):
        return True

    lower_id = work_id.lower()
    note = _text(row.get("note")).lower()
    if is_telemetry_orphan(row):
        return False
    if lower_id.startswith("job:obs_"):
        return False

    if "legacy jobs.db import" in note and (
        lower_id.startswith(("claude:", "codex:", "raw:"))
        or _JOB_OBSERVER_RE.fullmatch(lower_id)
        or _THREAD_ID_RE.fullmatch(lower_id)
        or _UUID_ONLY_RE.fullmatch(lower_id)
        or _HEX_ID_RE.fullmatch(lower_id)
    ):
        return False

    if "quarantined" in note and (
        _JOB_OBSERVER_RE.fullmatch(lower_id)
        or _THREAD_ID_RE.fullmatch(lower_id)
        or _UUID_ONLY_RE.fullmatch(lower_id)
        or _HEX_ID_RE.fullmatch(lower_id)
    ):
        return False

    if lower_id.startswith(("claude:", "codex:")):
        canonical = work_id.split(":", 1)[1] if ":" in work_id else ""
        if canonical_ids and canonical in canonical_ids and not _text(row.get("assignee")).strip():
            return False
        return _has_actionable_metadata(row) or _has_non_generated_label(row, work_id)

    technical_id = (
        _JOB_OBSERVER_RE.fullmatch(lower_id)
        or _THREAD_ID_RE.fullmatch(lower_id)
        or _UUID_ONLY_RE.fullmatch(lower_id)
        or _HEX_ID_RE.fullmatch(lower_id)
        or _TECHNICAL_TOKEN_RE.fullmatch(lower_id)
    )
    if technical_id and not _has_actionable_metadata(row) and not _has_non_generated_label(row, work_id):
        return False
    return True


def filter_visible_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    materialized = list(rows)
    canonical_ids = {
        _row_id(r)
        for r in materialized
        if _row_id(r) and not _row_id(r).lower().startswith(("claude:", "codex:"))
    }
    return [r for r in materialized if is_operator_visible_row(r, canonical_ids)]


def _updated(row: dict[str, Any]) -> float:
    for key in ("updated_at", "created_at", "claim_expires_at"):
        value = row.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _priority(row: dict[str, Any]) -> float:
    try:
        return float(row.get("priority"))
    except (TypeError, ValueError):
        return 99.0


def _split_pointer_candidates(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        values: list[str] = []
        for item in value:
            values.extend(_split_pointer_candidates(item))
        return values[:8]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return _split_pointer_candidates(parsed)
        if isinstance(parsed, dict):
            values = []
            for key in ("pointer", "path", "href", "url", "done_signal", "context_pack_ref"):
                if parsed.get(key):
                    values.extend(_split_pointer_candidates(parsed[key]))
            if values:
                return values[:8]
    chunks = re.split(r"[\n,]+", text)
    return [chunk.strip().strip("`'\"") for chunk in chunks if chunk.strip()][:8]


def _classify_pointer_candidate(raw: Any, *, field: str | None = None) -> dict[str, str] | None:
    value = str(raw or "").strip().strip("`'\"")
    if not value or any(ch.isspace() for ch in value):
        return None
    lower = value.lower()
    kind: str | None = None
    if lower.startswith("memory://"):
        kind = "memory"
    elif lower.startswith("kfts://"):
        kind = "kfts"
    elif lower.startswith(("http://", "https://")):
        kind = "url"
    elif lower.startswith("file://"):
        kind = "file"
    elif lower.startswith(("/", "./", "../")) or "/" in value:
        suffix = Path(value.split("#", 1)[0].split("?", 1)[0]).suffix.lower()
        if suffix or lower.startswith(("coordharness/", "docs/", "data/", "reports/")):
            kind = "path"
    if kind is None:
        return None
    out = {"kind": kind, "value": _short_text(value, 240)}
    if field:
        out["field"] = field
    return out


def _board_pointer_candidates(row: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field in ("context_pack_ref", "done_signal"):
        for raw in _split_pointer_candidates(row.get(field)):
            classified = _classify_pointer_candidate(raw, field=field)
            if not classified:
                continue
            key = (classified["kind"], classified["value"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(classified)
            if len(candidates) >= 4:
                return candidates
    return candidates


def _primary_board_pointer(row: dict[str, Any]) -> dict[str, str] | None:
    candidates = _board_pointer_candidates(row)
    if not candidates:
        return None
    field_rank = {"context_pack_ref": 0, "done_signal": 1}
    kind_rank = {"memory": 0, "kfts": 1, "path": 2, "file": 3, "url": 4}
    return sorted(
        candidates,
        key=lambda item: (field_rank.get(item.get("field") or "", 9), kind_rank.get(item.get("kind") or "", 9)),
    )[0]


def _done_signal_pointer(row: dict[str, Any]) -> dict[str, str] | None:
    for candidate in _board_pointer_candidates(row):
        if candidate.get("field") == "done_signal":
            return candidate
    return None


def _local_pointer_exists(pointer: dict[str, str] | None) -> bool | None:
    if not pointer:
        return None
    kind = pointer.get("kind")
    value = str(pointer.get("value") or "")
    if kind not in {"path", "file"} or not value:
        return None
    raw_path = value.removeprefix("file://").split("#", 1)[0].split("?", 1)[0]
    if pointer.get("field") == "done_signal":
        return job_status.done_signal_exists(raw_path, _HARNESS_ROOT)
    path = Path(raw_path)
    if not path.is_absolute():
        path = _REPO_ROOT / raw_path
    return path.exists()


def _pointer_health(pointer: dict[str, str] | None) -> str:
    if pointer is None:
        return "missing_pointer"
    exists = _local_pointer_exists(pointer)
    if exists is False:
        return "stale_path"
    return "ok"


def compact_row(
    row: dict[str, Any],
    *,
    reason: str | None = None,
    score: float | None = None,
    include_pointers: bool = False,
    include_timing: bool = False,
) -> dict[str, Any]:
    out = {k: row.get(k) for k in COMPACT_FIELDS if row.get(k) not in (None, "")}
    wid = _row_id(row)
    if wid:
        out["id"] = wid
    label = row.get("display") or row.get("title") or wid
    if label:
        out["label"] = _short_text(label, 140)
    subject_plane = str(row.get("subject_plane") or "").strip().lower()
    exact_plane = subject_plane in {"product", "harness", "infrastructure", "shared"}
    out["subject_plane"] = subject_plane if exact_plane else "unknown"
    out["subject_plane_exact"] = exact_plane
    out["semantic_system"] = subject_plane if exact_plane else "unknown"
    out["plane"] = subject_plane if exact_plane else "unknown"
    if row.get("quarantined"):
        out["quarantined"] = True
        out["quarantine_reasons"] = list(row.get("quarantine_reasons") or [])
    if row.get("query_rank") is not None:
        out["query_rank"] = row.get("query_rank")
        out["query_core_build_sha256"] = row.get("query_core_build_sha256")
    if row.get("done_signal"):
        out["has_done_signal"] = True
    if row.get("context_pack_ref"):
        out["has_context_pack_ref"] = True
    if include_pointers:
        for key in ("title", "display", "note"):
            if row.get(key):
                out[key] = _short_text(row.get(key), 240 if key == "note" else 180)
        for key in ("done_signal", "context_pack_ref"):
            if row.get(key):
                out[key] = _short_text(row.get(key), 240)
        candidates = _board_pointer_candidates(row)
        if candidates:
            out["pointer_candidates"] = candidates
            out["primary_pointer"] = _primary_board_pointer(row)
    if include_timing:
        for key in ("updated_at", "created_at"):
            if row.get(key) not in (None, ""):
                out[key] = row.get(key)
    if row.get("claim_step") and include_pointers:
        out["claim_step"] = _short_text(row.get("claim_step"), 160)
    if reason:
        out["why_included"] = reason
    if score is not None:
        out["score"] = round(float(score), 3)
    return out


def done_card_from_row(row: dict[str, Any], *, reason: str | None = None, score: float | None = None) -> dict[str, Any]:
    card = compact_row(
        row,
        reason=reason,
        score=score,
        include_pointers=True,
        include_timing=True,
    )
    primary = card.get("primary_pointer") if isinstance(card.get("primary_pointer"), dict) else None
    card["card_kind"] = "done_card"
    card["terminal_status"] = _status(row)
    card["pointer_health"] = _pointer_health(primary)
    pointer_exists = _local_pointer_exists(primary)
    if pointer_exists is not None:
        card["primary_pointer_exists"] = pointer_exists
    return card


def _assigned_to(row: dict[str, Any], actor: str | None) -> bool:
    if not actor:
        return True
    return str(actor).lower() in _text(row.get("assignee")).lower()


def _sort_open(row: dict[str, Any]) -> tuple[int, float, float, str]:
    exact_rank = row.get("query_rank_key")
    if isinstance(exact_rank, list):
        return tuple(exact_rank)
    return coord_db.board_current_rank(row)


def _sort_recent(row: dict[str, Any]) -> tuple[float, float, str]:
    return coord_db.board_recent_rank(row)


def _take_diverse(rows: Iterable[dict[str, Any]], limit: int, *, per_module: int = 6) -> list[dict[str, Any]]:
    limit = max(0, int(limit))
    if limit == 0:
        return []
    counts: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for row in rows:
        module = _text(row.get("module") or "(none)")
        if counts[module] < per_module:
            selected.append(row)
            counts[module] += 1
        else:
            overflow.append(row)
        if len(selected) >= limit:
            return selected
    for row in overflow:
        if len(selected) >= limit:
            break
        selected.append(row)
    return selected


def build_digest(
    rows: list[dict[str, Any]],
    *,
    actor: str | None = None,
    next_limit: int = 12,
    attention_limit: int = 12,
    recent_done_limit: int = 12,
    changed_limit: int = 12,
    canonical_open_total: int | None = None,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    counts = Counter(_status(r) or "(blank)" for r in rows)
    open_rows = [r for r in rows if _status(r) in OPEN_STATUSES]
    done_rows = [r for r in rows if _status(r) == "done"]
    running = sorted([r for r in rows if _status(r) == "running"], key=_sort_open)
    blocked = sorted([r for r in rows if _status(r) == "blocked"], key=_sort_open)
    actor_next = sorted(
        [r for r in rows if _status(r) in {"queued", "planned"} and _assigned_to(r, actor)],
        key=_sort_open,
    )
    attention = sorted([r for r in rows if _needs_attention(r)], key=_sort_open)
    recent_done = sorted(done_rows, key=_sort_recent)
    changed = sorted(rows, key=_sort_recent)
    done_by_module = Counter(_text(r.get("module") or "(none)") for r in done_rows)
    open_by_module = Counter(_text(r.get("module") or "(none)") for r in open_rows)
    foreground_open_total = len(open_rows)
    open_total = (
        max(0, int(canonical_open_total))
        if canonical_open_total is not None
        else foreground_open_total
    )

    return {
        "schema_version": 1,
        "lens": "digest",
        "generated_at": time.time(),
        "actor": actor,
        "counts": dict(sorted(counts.items())),
        "open_total": open_total,
        "open_total_scope": (
            "canonical_board" if canonical_open_total is not None else "foreground_lens"
        ),
        "foreground_open_total": foreground_open_total,
        "done_total": len(done_rows),
        "open_by_module": dict(open_by_module.most_common(20)),
        "done_by_module": dict(done_by_module.most_common(20)),
        "running": [compact_row(r, reason="currently running") for r in running[:20]],
        "blocked": [compact_row(r, reason="blocked") for r in blocked[:20]],
        "actor_next": [compact_row(r, reason=f"assigned to {actor or 'any actor'}") for r in actor_next[: max(0, next_limit)]],
        "attention": [compact_row(r, reason="blocked/failed/proof needs attention") for r in attention[: max(0, attention_limit)]],
        "recent_done": [compact_row(r, reason="recently completed") for r in _take_diverse(recent_done, recent_done_limit)],
        "recently_changed": [compact_row(r, reason="recently changed") for r in _take_diverse(changed, changed_limit)],
        "expansion": {
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
            "search": f"{_BOARD_CONTEXT_COMMAND} search '<query>' --limit 30",
            "skeleton": f"{_BOARD_CONTEXT_COMMAND} skeleton --status open --limit {MAX_SKELETON_ROWS}",
            "full_export": f"{_BOARD_CONTEXT_COMMAND} export --status all --out .coordharness/board_full.json",
        },
    }


def _score_row(
    row: dict[str, Any],
    query_terms: set[str],
    *,
    seed: dict[str, Any] | None = None,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    terms = _row_terms(row)
    matches = sorted(query_terms & terms)
    if matches:
        score += len(matches)
        reasons.append("matched terms: " + ", ".join(matches[:8]))
    title_terms = tokenize(row.get("title")) | tokenize(row.get("display"))
    title_matches = sorted(query_terms & title_terms)
    if title_matches:
        score += 2.5 * len(title_matches)
        reasons.append("title/display match")
    path_terms = tokenize(row.get("done_signal")) | tokenize(row.get("context_pack_ref"))
    path_matches = sorted(query_terms & path_terms)
    if path_matches:
        score += 2.0 * len(path_matches)
        reasons.append("artifact/context path match")
    if score > 0 and _status(row) not in TERMINAL_STATUSES:
        score += 0.2

    if seed:
        if _row_id(row) == _row_id(seed):
            return (-1.0, [])
        if row.get("parent_id") and row.get("parent_id") == seed.get("parent_id"):
            score += 5.0
            reasons.append("same parent")
        if row.get("parent_id") == _row_id(seed) or seed.get("parent_id") == _row_id(row):
            score += 6.0
            reasons.append("parent/child")
        if row.get("module") and row.get("module") == seed.get("module"):
            score += 2.0
            reasons.append("same module")
        if row.get("sublane") and row.get("sublane") == seed.get("sublane"):
            score += 1.5
            reasons.append("same sublane")
        shared_paths = (
            (tokenize(row.get("done_signal")) | tokenize(row.get("context_pack_ref")))
            & (tokenize(seed.get("done_signal")) | tokenize(seed.get("context_pack_ref")))
        )
        if shared_paths:
            score += min(6.0, 1.5 * len(shared_paths))
            reasons.append("shared artifact/context tokens")
    return (score, reasons)


def _limit_terms(terms: set[str]) -> set[str]:
    return set(sorted(terms)[:MAX_SEARCH_TERMS])


def search_rows(
    rows: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 30,
    include_done: bool = True,
    include_open: bool = True,
    diversify: bool = True,
    seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    q_raw = _text(query).strip()
    q = _short_text(q_raw, 500)
    requested_limit = max(0, int(limit))
    effective_limit = min(MAX_SEARCH_ROWS, requested_limit)
    query_terms = _limit_terms(tokenize(q_raw))
    if seed:
        query_terms = _limit_terms(
            query_terms
            | tokenize(seed.get("title"))
            | tokenize(seed.get("display"))
            | tokenize(seed.get("module"))
            | tokenize(seed.get("sublane"))
            | tokenize(seed.get("done_signal"))
            | tokenize(seed.get("context_pack_ref"))
            | tokenize(seed.get("note"))
        )
    scored: list[tuple[float, dict[str, Any], list[str]]] = []
    for row in rows:
        status = _status(row)
        if status == "done" and not include_done:
            continue
        if status != "done" and status not in TERMINAL_STATUSES and not include_open:
            continue
        score, reasons = _score_row(row, query_terms, seed=seed)
        if score <= 0:
            continue
        scored.append((score, row, reasons))
    scored.sort(key=lambda item: (-item[0], _status(item[1]) == "done", _sort_recent(item[1])))
    selected_rows = [(s, r, why) for s, r, why in scored]
    if diversify:
        ordered_rows = _take_diverse(
            (r for _s, r, _why in selected_rows),
            effective_limit,
            per_module=max(4, effective_limit // 6 or 1),
        )
        selected: list[tuple[float, dict[str, Any], list[str]]] = []
        ids = {_row_id(r) for r in ordered_rows}
        for score, row, why in selected_rows:
            if _row_id(row) in ids:
                selected.append((score, row, why))
                ids.remove(_row_id(row))
            if not ids:
                break
    else:
        selected = selected_rows[:effective_limit]
    return {
        "schema_version": 1,
        "lens": "search",
        "query": q,
        "limit_cap": MAX_SEARCH_ROWS,
        "requested_limit": requested_limit,
        "candidate_count": len(scored),
        "returned": len(selected),
        "truncated": len(scored) > len(selected),
        "results": [
            compact_row(row, score=score, reason="; ".join(why[:4]) or "structured/text match")
            for score, row, why in selected
        ],
        "expansion": {
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
            "fuller_search": f"{_BOARD_CONTEXT_COMMAND} search {json.dumps(q)} --limit {min(MAX_SEARCH_ROWS, max(effective_limit * 2, 50))}",
        },
    }


def build_history(
    rows: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 30,
    diversify: bool = True,
) -> dict[str, Any]:
    search = search_rows(
        rows,
        query,
        limit=limit,
        include_done=True,
        include_open=False,
        diversify=diversify,
    )
    return {
        "schema_version": 1,
        "lens": "history",
        "query": search["query"],
        "candidate_count": search["candidate_count"],
        "returned": search["returned"],
        "truncated": search["truncated"],
        "results": search["results"],
        "expansion": {
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
            "search_with_open": f"{_BOARD_CONTEXT_COMMAND} search {json.dumps(search['query'])} --limit {min(MAX_SEARCH_ROWS, max(limit * 2, 50))}",
            "deeper_history": f"{_BOARD_CONTEXT_COMMAND} history {json.dumps(search['query'])} --limit {min(MAX_SEARCH_ROWS, max(limit * 2, 50))}",
        },
    }


def build_done_cards(
    rows: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 30,
    diversify: bool = True,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    q = _text(query).strip()
    query_terms = tokenize(q)
    requested_limit = max(0, int(limit))
    max_rows = min(MAX_SEARCH_ROWS, requested_limit)
    scored: list[tuple[float, dict[str, Any], list[str]]] = []
    for row in rows:
        if not _terminalish(row):
            continue
        score, reasons = _score_row(row, query_terms)
        if score <= 0:
            continue
        scored.append((score, row, reasons))
    scored.sort(key=lambda item: (-item[0], _sort_recent(item[1])))
    selected = scored[:max_rows]
    if diversify:
        diverse = _take_diverse(
            (row for _score, row, _why in scored),
            max_rows,
            per_module=max(4, max_rows // 6 or 1),
        )
        ids = {_row_id(row) for row in diverse}
        selected = []
        for score, row, why in scored:
            if _row_id(row) in ids:
                selected.append((score, row, why))
                ids.remove(_row_id(row))
            if not ids:
                break
    cards: list[dict[str, Any]] = []
    for score, row, why in selected:
        cards.append(
            done_card_from_row(
                row,
                reason="; ".join(why[:4]) or "terminal work match",
                score=score,
            )
        )
    return {
        "schema_version": 1,
        "lens": "done_cards",
        "query": q,
        "limit_cap": MAX_SEARCH_ROWS,
        "requested_limit": requested_limit,
        "candidate_count": len(scored),
        "returned": len(cards),
        "truncated": len(scored) > len(cards),
        "cards": cards,
        "expansion": {
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
            "deeper_done_cards": f"{_BOARD_CONTEXT_COMMAND} history {json.dumps(q)} --limit {min(MAX_SEARCH_ROWS, max(limit * 2, 50))}",
        },
    }


def build_focus(
    rows: list[dict[str, Any]],
    work_id: str,
    *,
    query: str | None = None,
    related_limit: int = 30,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    by_id = {_row_id(r): r for r in rows if _row_id(r)}
    target = by_id.get(work_id)
    if target is None:
        raise ValueError(f"work_id {work_id!r} not found in board rows")
    parent_id = _text(target.get("parent_id"))
    parent = by_id.get(parent_id) if parent_id else None
    children = [r for r in rows if _text(r.get("parent_id")) == work_id]
    siblings = [r for r in rows if parent_id and _text(r.get("parent_id")) == parent_id and _row_id(r) != work_id]
    search_query = query or _short_text(" ".join(
        _text(target.get(k))
        for k in ("title", "display", "module", "sublane", "done_signal", "context_pack_ref", "note")
        if target.get(k)
    ), 500)
    related = search_rows(
        rows,
        search_query,
        limit=related_limit,
        include_done=True,
        include_open=True,
        diversify=True,
        seed=target,
    )
    open_related = [r for r in related["results"] if _text(r.get("status")).lower() not in TERMINAL_STATUSES]
    done_related = [r for r in related["results"] if _text(r.get("status")).lower() in TERMINAL_STATUSES]
    return {
        "schema_version": 1,
        "lens": "focus",
        "work_id": work_id,
        "row": compact_row(target, reason="target work item", include_pointers=True, include_timing=True),
        "parent": compact_row(parent, reason="parent", include_pointers=True) if parent else None,
        "children": [compact_row(r, reason="child task") for r in sorted(children, key=_sort_open)[:20]],
        "siblings": [compact_row(r, reason="same parent") for r in sorted(siblings, key=_sort_open)[:20]],
        "events": events or [],
        "related_open": open_related,
        "related_done": done_related,
        "related_candidate_count": related["candidate_count"],
        "truncated": related["truncated"],
        "expansion": {
            "search_more": f"{_BOARD_CONTEXT_COMMAND} search {json.dumps(search_query)} --limit {min(MAX_SEARCH_ROWS, max(related_limit * 2, 60))}",
            "skeleton_open": f"{_BOARD_CONTEXT_COMMAND} skeleton --status open --limit {MAX_SKELETON_ROWS}",
            "full_board_export": "Use explicit full-board/admin export only when needed; do not load by default.",
        },
    }


def _filter_by_status(rows: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    status = _text(status or "open").lower()
    if status == "open":
        return [r for r in rows if _status(r) not in TERMINAL_STATUSES]
    if status == "done":
        return [r for r in rows if _status(r) == "done"]
    if status == "attention":
        return [r for r in rows if _needs_attention(r)]
    if status == "all":
        return list(rows)
    return [r for r in rows if _status(r) == status]


def build_skeleton(
    rows: list[dict[str, Any]],
    *,
    status: str = "open",
    limit: int = MAX_SKELETON_ROWS,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    status = _text(status or "open").lower()
    filtered = _filter_by_status(rows, status)
    filtered.sort(key=_sort_open if status != "done" else _sort_recent)
    requested_rows = max(0, int(limit))
    max_rows = min(MAX_SKELETON_ROWS, requested_rows)
    selected = filtered[:max_rows]
    return {
        "schema_version": 1,
        "lens": "skeleton",
        "status_filter": status,
        "limit_cap": MAX_SKELETON_ROWS,
        "requested_limit": requested_rows,
        "total_matching": len(filtered),
        "returned": len(selected),
        "truncated": len(filtered) > len(selected),
        "rows": [compact_row(r) for r in selected],
        "expansion": {
            "increase_limit": f"{_BOARD_CONTEXT_COMMAND} skeleton --status {status} --limit {MAX_SKELETON_ROWS}",
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
        },
    }


def _parse_since(value: str) -> float:
    raw = _text(value).strip()
    if not raw:
        raise ValueError("--since is required")
    try:
        return float(raw)
    except ValueError:
        pass
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("--since must be a unix timestamp or ISO-8601 datetime") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def build_changes(
    rows: list[dict[str, Any]],
    *,
    since: str,
    status: str = "all",
    limit: int = 100,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    since_ts = _parse_since(since)
    filtered = [r for r in _filter_by_status(rows, status) if _updated(r) >= since_ts]
    filtered.sort(key=_sort_recent)
    requested_rows = max(0, int(limit))
    max_rows = min(MAX_CHANGE_ROWS, requested_rows)
    selected = filtered[:max_rows]
    return {
        "schema_version": 1,
        "lens": "changes",
        "since": since,
        "since_ts": since_ts,
        "status_filter": status,
        "limit_cap": MAX_CHANGE_ROWS,
        "requested_limit": requested_rows,
        "total_matching": len(filtered),
        "returned": len(selected),
        "truncated": len(filtered) > len(selected),
        "rows": [compact_row(r, include_timing=True) for r in selected],
        "expansion": {
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
            "full_export": f"{_BOARD_CONTEXT_COMMAND} export --status all --out .coordharness/board_full.json",
        },
    }


_CURATION_PREFIX_RE = re.compile(r"^(.+[-_])(?:\d{2,}|[a-z]\d{1,3})$", re.I)


def _curation_prefix(work_id: str) -> str | None:
    match = _CURATION_PREFIX_RE.match(str(work_id or "").strip())
    return match.group(1) if match else None


def _common_value(rows: list[dict[str, Any]], field: str) -> str:
    values = sorted({_text(row.get(field)).strip() for row in rows if _text(row.get(field)).strip()})
    return values[0] if len(values) == 1 else ""


_ATTACH_PROOF_STATUS_ELIGIBLE = {"done", "failed", "superseded", "cancelled", "canceled", "closed"}


def _backlog_sections_by_id(path: Path | None = None) -> dict[str, str]:
    path = path or _BACKLOG_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for section in ("today_live_jobs", "items", "done_archive", "epics"):
        for row in payload.get(section, []) or []:
            if isinstance(row, dict):
                work_id = _text(row.get("id")).strip()
                if work_id and work_id not in out:
                    out[work_id] = section
    return out


def _attach_proof_eligibility(
    row: dict[str, Any],
    pointer: dict[str, str] | None,
    pointer_health: str,
    backlog_section: str = "",
    job_progress_blocker: str = "",
) -> tuple[bool, str]:
    if not backlog_section:
        return False, "attach-proof requires a compatibility backlog row"
    if backlog_section == "done_archive":
        return False, "attach-proof refuses archived history rows"
    status = _status(row)
    if status not in _ATTACH_PROOF_STATUS_ELIGIBLE:
        return False, f"attach-proof refuses non-terminal status {status or '<empty>'}"
    if job_progress_blocker:
        return False, job_progress_blocker
    if pointer is None:
        return False, "no done_signal/context pointer to validate"
    if pointer.get("field") != "done_signal":
        return False, "no done_signal path candidate; pointer is context-only"
    if pointer_health == "ok":
        return False, "done_signal path already resolves; owner/rubric review may still be needed"
    return True, "dry-run required; codex_coord.py may still refuse archived history rows"


def _numeric_progress(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _job_progress_blocker(row: dict[str, Any], path: Path | None = None) -> str:
    work_id = _row_id(row)
    if not work_id:
        return ""
    path = path or _JOB_PROGRESS_DIR
    try:
        sidecars = sorted(path.glob("*.json"))
    except Exception:
        return ""

    fallback_failed = ""
    for sidecar in sidecars:
        payload, authority = job_authority.read_sidecar_with_authority(sidecar)
        if payload is None or authority is None or authority.diagnostic_only:
            continue
        if not isinstance(payload, dict) or _text(payload.get("roadmap_id")) != work_id:
            continue
        state = _text(payload.get("state")).lower()
        done = _numeric_progress(payload.get("done"))
        total = _numeric_progress(payload.get("total"))
        if state == "failed":
            if done is not None and total is not None and done < total:
                return (
                    f"authoritative job_progress {sidecar.name} failed incomplete "
                    f"({int(done) if done.is_integer() else done}/"
                    f"{int(total) if total.is_integer() else total})"
                )
            fallback_failed = f"authoritative job_progress {sidecar.name} failed"
        if state in {"running", "queued"}:
            return f"authoritative job_progress {sidecar.name} is still {state}"
    return fallback_failed


def build_curation(
    rows: list[dict[str, Any]],
    *,
    min_group: int = 3,
    open_group_threshold: int = 12,
    limit: int = 20,
) -> dict[str, Any]:
    rows = filter_visible_rows(rows)
    limit = max(0, min(MAX_SEARCH_ROWS, int(limit)))
    min_group = max(2, int(min_group))
    open_group_threshold = max(2, int(open_group_threshold))

    terminal_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not _terminalish(row) or _text(row.get("parent_id")).strip():
            continue
        prefix = _curation_prefix(_row_id(row))
        if prefix:
            terminal_groups[prefix].append(row)

    terminal_prefix_groups: list[dict[str, Any]] = []
    for prefix, group_rows in terminal_groups.items():
        if len(group_rows) < min_group:
            continue
        group_rows.sort(key=lambda r: _row_id(r))
        parent_id = prefix.rstrip("-_") + "-GROUP"
        module = _common_value(group_rows, "module")
        sublane = _common_value(group_rows, "sublane")
        parent_title = _short_text(
            "Historical group for " + prefix.rstrip("-_").replace("_", " ").replace("-", " "),
            90,
        )
        terminal_prefix_groups.append({
            "child_prefix": prefix,
            "parent_id": parent_id,
            "parent_title": parent_title,
            "matched_count": len(group_rows),
            "child_ids": [_row_id(row) for row in group_rows[:50]],
            "module": module,
            "sublane": sublane,
            "inspection_command": (
                f"{_BOARD_CONTEXT_COMMAND} search {json.dumps(prefix)} --limit 50"
            ),
            "apply_note": (
                "No public bulk grouping command is provided; use reviewed typed lifecycle "
                "operations if a downstream deployment adds one."
            ),
            "risk_control": "advisory grouping only; the context lens never mutates rows.",
        })
    terminal_prefix_groups.sort(key=lambda g: (-int(g["matched_count"]), str(g["child_prefix"])))

    proof_repair: list[dict[str, Any]] = []
    backlog_sections = _backlog_sections_by_id()
    for row in rows:
        pointer = _done_signal_pointer(row) or _primary_board_pointer(row)
        proof_state = _text(row.get("proof_state")).lower()
        pointer_health = _pointer_health(pointer)
        if not _terminalish(row) and _status(row) not in ATTENTION_STATUSES and proof_state not in ATTENTION_PROOF_STATES:
            continue
        if proof_state not in CURATION_PROOF_STATES and pointer_health != "stale_path":
            continue
        backlog_section = backlog_sections.get(_row_id(row), "")
        job_blocker = _job_progress_blocker(row)
        attach_eligible, ineligible_reason = _attach_proof_eligibility(
            row,
            pointer,
            pointer_health,
            backlog_section,
            job_blocker,
        )
        proof_repair.append({
            **compact_row(row, reason="proof pointer needs validation or replacement", include_pointers=True),
            "proof_state": proof_state,
            "pointer_health": pointer_health,
            "proof_pointer": pointer,
            "backlog_section": backlog_section,
            "job_progress_blocker": job_blocker,
            "attach_proof_eligible": attach_eligible,
            "ineligible_reason": "" if attach_eligible else ineligible_reason,
            "next_command": f"{_BOARD_CONTEXT_COMMAND} focus {shlex.quote(_row_id(row))}",
            "repair_command_template": (
                "Use a typed lifecycle client to attach or complete against the exact claim; "
                "no public bulk proof-repair command is provided."
                if attach_eligible
                else ""
            ),
        })
    proof_repair.sort(key=lambda r: (str(r.get("proof_state")), str(r.get("id"))))

    open_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if _status(row) not in {"queued", "planned"}:
            continue
        key = (
            _text(row.get("module") or "(none)"),
            _text(row.get("sublane") or "(none)"),
            _text(row.get("assignee") or "(unassigned)"),
        )
        open_groups[key].append(row)
    open_micro_batches: list[dict[str, Any]] = []
    for (module, sublane, assignee), group_rows in open_groups.items():
        if len(group_rows) < open_group_threshold:
            continue
        group_rows.sort(key=_sort_open)
        open_micro_batches.append({
            "module": module,
            "sublane": sublane,
            "assignee": assignee,
            "count": len(group_rows),
            "sample_ids": [_row_id(row) for row in group_rows[:12]],
            "recommendation": "Needs owner review: combine into fewer JOB rows with subtasks only after confirming these are not independent active work.",
            "risk_control": "read-only candidate; no bulk grouping mutation is provided.",
        })
    open_micro_batches.sort(key=lambda g: (-int(g["count"]), str(g["module"]), str(g["sublane"])))

    return {
        "schema_version": 1,
        "lens": "curation",
        "generated_at": time.time(),
        "limits": {
            "limit_cap": MAX_SEARCH_ROWS,
            "requested_limit": limit,
            "min_group": min_group,
            "open_group_threshold": open_group_threshold,
        },
        "operator_contract": [
            "This lens is read-only and emits inspection commands plus advisory candidates only.",
            "Any downstream grouping mutation requires an independently reviewed typed operation.",
            "Run coord doctor after any approved lifecycle or job-state mutation.",
        ],
        "terminal_prefix_groups": terminal_prefix_groups[:limit],
        "proof_repair": proof_repair[:limit],
        "open_micro_batches": open_micro_batches[:limit],
        "expansion": {
            "group_inspection": "No public bulk group mutation command; inspect the group and use typed lifecycle operations.",
            "focus": f"{_BOARD_CONTEXT_COMMAND} focus <WORK_ID>",
            "skeleton_open": f"{_BOARD_CONTEXT_COMMAND} skeleton --status open --limit {MAX_SKELETON_ROWS}",
        },
    }


def build_export(
    rows: list[dict[str, Any]],
    *,
    status: str = "all",
    include_shadow: bool = False,
) -> dict[str, Any]:
    visible_rows = list(rows) if include_shadow else filter_visible_rows(rows)
    filtered = _filter_by_status(visible_rows, status)
    filtered.sort(key=_sort_recent if status in {"all", "done"} else _sort_open)
    return {
        "schema_version": 1,
        "lens": "board_export",
        "generated_at": time.time(),
        "status_filter": status,
        "include_shadow": include_shadow,
        "total_matching": len(filtered),
        "rows": filtered,
    }


def write_export(payload: dict[str, Any], out: str | Path) -> Path:
    path = Path(out)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def _split_sessions_arg(sessions: Any) -> list[str]:
    if isinstance(sessions, str):
        raw = [sessions]
    else:
        raw = list(sessions or [])
    out: list[str] = []
    for chunk in raw:
        for piece in str(chunk or "").split(","):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


def _load_closeout_event(conn, session_id: str, actor: str | None) -> dict[str, Any] | None:
    family = coord_db.related_session_ids(conn, session_id, actor=actor) or [session_id]
    placeholders = ",".join("?" for _ in family)
    row = conn.execute(
        f"SELECT event_id, ts, actor, session_id, title, payload_json FROM events"
        f" WHERE kind='session_closeout' AND session_id IN ({placeholders})"
        f" ORDER BY event_id DESC LIMIT 1",
        family,
    ).fetchone()
    return dict(row) if row else None


def _decision_line_by_event_id(
    conn, event_id: Any, *, live_decision_ids: frozenset[int] | None = None
) -> str | None:
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        "SELECT ts, kind, actor, title, body, payload_json FROM events WHERE event_id=?",
        (eid,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    line = _render_decision_event_line(d)
    if not line:
        return None
    if (
        live_decision_ids is not None
        and str(d.get("kind")) == "decision"
        and eid not in live_decision_ids
    ):
        successor_fn = getattr(coord_db, "_decision_immediate_successor_event_id", None)
        successor_eid: int | None = None
        try:
            successor_eid = successor_fn(conn, eid) if callable(successor_fn) else None
        except Exception:
            successor_eid = None
        tag = (
            f"[SUPERSEDED by event {successor_eid}]"
            if successor_eid is not None
            else "[SUPERSEDED]"
        )
        line = f"{line} {tag}"
    return line


def build_successor_boot(
    rows: list[dict[str, Any]],
    *,
    sessions: Any,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    session_list = _split_sessions_arg(sessions)
    by_id = {_row_id(r): r for r in rows}
    per_session: list[dict[str, Any]] = []
    combined_hints: list[str] = []
    combined_open: dict[str, dict[str, Any]] = {}

    conn = _connect(db_path)
    try:
        head_ids_fn = getattr(coord_db, "_decision_head_event_ids", None)
        try:
            live_decision_ids = head_ids_fn(conn) if callable(head_ids_fn) else None
        except Exception:
            live_decision_ids = None
        for sid in session_list:
            actor = coord_db.expected_actor_for_session_id(sid)
            ev = _load_closeout_event(conn, sid, actor)
            payload: dict[str, Any] = {}
            if ev:
                try:
                    payload = json.loads(ev.get("payload_json") or "{}")
                except (TypeError, ValueError):
                    payload = {}
            hints = [str(h) for h in (payload.get("successor_hints") or []) if str(h).strip()]
            dead = [str(d) for d in (payload.get("dead_ends") or []) if str(d).strip()]
            summary = str(payload.get("summary") or "")
            touched: set[str] = {str(w) for w in (payload.get("rows_touched") or [])}
            for disp in payload.get("claims_disposed") or []:
                if isinstance(disp, dict) and disp.get("work_id"):
                    touched.add(str(disp["work_id"]))

            open_rows = [
                by_id[w] for w in touched
                if w in by_id and _status(by_id[w]) in OPEN_STATUSES
            ]
            open_rows.sort(key=_sort_open)
            resume: list[dict[str, Any]] = []
            for r in open_rows:
                wid = _row_id(r)
                combined_open[wid] = r
                resume.append({
                    "work_id": wid,
                    "status": _status(r),
                    "priority": _priority(r),
                    "display": _short_text(r.get("display") or r.get("title") or wid, 90),
                    "next_step": (
                        _short_text(r.get("next_step"), 90)
                        if _text(r.get("next_step")).strip() else ""
                    ),
                    "resume_when": (
                        _short_text(r.get("resume_when"), 60)
                        if _text(r.get("resume_when")).strip() else ""
                    ),
                })

            decisions: list[str] = []
            for eid in payload.get("decisions_posted") or []:
                line = _decision_line_by_event_id(conn, eid, live_decision_ids=live_decision_ids)
                if line:
                    decisions.append(line)

            combined_hints.extend(hints)
            per_session.append({
                "session_id": sid,
                "actor": actor,
                "closeout_event_id": int(ev["event_id"]) if ev else None,
                "missing": ev is None,
                "summary": summary,
                "successor_hints": hints,
                "dead_ends": dead,
                "resume_rows": resume,
                "decisions": decisions,
                "claims_disposed": payload.get("claims_disposed") or [],
            })
    finally:
        conn.close()

    seen_hints: set[str] = set()
    combined_actions: list[dict[str, Any]] = []
    for hint in combined_hints:
        if hint not in seen_hints:
            seen_hints.add(hint)
            combined_actions.append({"kind": "hint", "text": hint})
    for r in sorted(combined_open.values(), key=_sort_open):
        wid = _row_id(r)
        combined_actions.append({
            "kind": "open_row",
            "work_id": wid,
            "priority": _priority(r),
            "status": _status(r),
            "display": _short_text(r.get("display") or r.get("title") or wid, 90),
            "next_step": (
                _short_text(r.get("next_step"), 90)
                if _text(r.get("next_step")).strip() else ""
            ),
        })

    payload = {
        "schema_version": 1,
        "lens": "successor_boot",
        "sessions": session_list,
        "per_session": per_session,
        "combined_next_actions": combined_actions,
    }
    payload["markdown"] = _render_successor_markdown(payload)
    return payload


def _render_successor_markdown(payload: dict[str, Any]) -> str:
    sessions = payload.get("sessions") or []
    per_session = payload.get("per_session") or []
    actions = payload.get("combined_next_actions") or []
    lines: list[str] = [
        f"# Successor Boot — merged from {len(sessions)} session(s)",
        "",
        "> Read this FIRST. It merges the closeouts of: " + ", ".join(sessions) + ".",
        "> Boot from the board (preflight + capsule), not a pasted transcript.",
        "",
        f"## Combined next actions ({len(actions)})",
    ]
    if not actions:
        lines.append("- (none surfaced)")
    for i, act in enumerate(actions, 1):
        if act.get("kind") == "hint":
            lines.append(f"{i}. [hint] {act.get('text')}")
        else:
            pieces = [
                f"{i}. [row pri={act.get('priority')} {act.get('status')}]",
                f"{act.get('work_id')} — {act.get('display')}",
            ]
            if act.get("next_step"):
                pieces.append(f"— next: {act.get('next_step')}")
            lines.append(" ".join(pieces))
    for sess in per_session:
        lines.append("")
        header = f"## Session {sess.get('session_id')}"
        if sess.get("actor"):
            header += f" ({sess.get('actor')})"
        if sess.get("closeout_event_id") is not None:
            header += f" — closeout #{sess.get('closeout_event_id')}"
        lines.append(header)
        if sess.get("missing"):
            lines.append("_No session_closeout event found — session may still be open._")
            continue
        if sess.get("summary"):
            lines.append(sess["summary"])
        if sess.get("successor_hints"):
            lines.append("")
            lines.append("Successor hints:")
            lines.extend(f"- {h}" for h in sess["successor_hints"])
        if sess.get("dead_ends"):
            lines.append("")
            lines.append("Dead ends (do NOT retry):")
            lines.extend(f"- {d}" for d in sess["dead_ends"])
        if sess.get("resume_rows"):
            lines.append("")
            lines.append("Open / parked rows:")
            for r in sess["resume_rows"]:
                piece = f"- {r['work_id']} [{r['status']}]"
                if r.get("next_step"):
                    piece += f" — next: {r['next_step']}"
                if r.get("resume_when"):
                    piece += f" [resume_when: {r['resume_when']}]"
                lines.append(piece)
        if sess.get("decisions"):
            lines.append("")
            lines.append("Decisions posted:")
            lines.extend(f"- {d}" for d in sess["decisions"])
    return "\n".join(lines) + "\n"


def render_markdown(payload: dict[str, Any]) -> str:
    lens = payload.get("lens")
    if lens == "successor_boot":
        return payload.get("markdown") or ""
    lines: list[str] = [f"# Board {str(lens or 'Context').title()}"]
    if lens == "digest":
        lines.append(
            f"Open: {payload.get('open_total')} | "
            f"Foreground open: {payload.get('foreground_open_total')} | "
            f"Done: {payload.get('done_total')}"
        )
        for key, title in (
            ("running", "Running"),
            ("blocked", "Blocked"),
            ("actor_next", "Assigned Next"),
            ("attention", "Needs Attention"),
            ("recent_done", "Recent Done"),
            ("recently_changed", "Recently Changed"),
        ):
            rows = payload.get(key) or []
            lines.append(f"\n## {title} ({len(rows)})")
            lines.extend(_row_lines(rows))
    elif lens == "capsule":
        lines.append(f"Generated: {_fmt_ts(payload.get('generated_at'))}")
        health = payload.get("health_summary")
        lines.append("\n## Board Health")
        if health:
            lines.append(
                f"open {health.get('open')} | running {health.get('running')} | "
                f"blocked {health.get('blocked')} | attention {health.get('attention')} | "
                f"stale_14d {health.get('stale_14d')} | review_open {health.get('review_open')} | "
                f"phantom_no_claim {health.get('phantom_no_claim')}"
            )
            wip = health.get("wip_by_lane") or {}
            if wip:
                lines.append("wip_by_lane: " + ", ".join(f"{k}={v}" for k, v in wip.items()))
            review_queue = health.get("t0_review_queue") or {}
            if review_queue.get("count") is not None:
                reviewer_counts = review_queue.get("by_required_reviewer_lane") or {}
                reviewer_text = ", ".join(
                    f"{lane}={count}" for lane, count in reviewer_counts.items() if count
                ) or "none"
                lines.append(
                    "t0_review_ready: "
                    f"{review_queue.get('count')} | required_reviewer {reviewer_text} | "
                    f"oldest_s {int(review_queue.get('oldest_request_age_s') or 0)}"
                )
            unreviewed = health.get("t0_unreviewed") or {}
            if unreviewed.get("count") is not None:
                lines.append(
                    f"recent_terminal_t0_unreviewed: {unreviewed.get('count')}"
                )
        else:
            lines.append("- omitted")
        running = payload.get("running") or []
        lines.append(f"\n## Running Now ({len(running)})")
        if not running:
            lines.append("- none")
        for row in running:
            step = f" -- {row.get('claim_step')}" if row.get("claim_step") else ""
            lines.append(f"- `{row.get('work_id')}` {row.get('display') or ''} [{row.get('assignee') or 'unassigned'}]{step}")
        resume = payload.get("resume_intents") or []
        lines.append(f"\n## Resume Intents ({len(resume)})")
        if resume:
            lines.extend(f"- {line}" for line in resume)
        else:
            lines.append("- none")
        decisions = payload.get("recent_decisions") or []
        lines.append(f"\n## Recent Decisions ({len(decisions)})")
        if decisions:
            lines.extend(f"- {line}" for line in decisions)
        else:
            lines.append("- none")
        lines.append("\n## Environment")
        lines.append(f"- resource_mode: {payload.get('resource_mode')}")
        epoch = payload.get("policy_epoch") or {}
        if epoch:
            lines.append(f"- policy_epoch: {epoch.get('epoch')}")
        pointers = payload.get("pointers") or {}
        if pointers:
            lines.append("\n## Pointers")
            for key, value in pointers.items():
                lines.append(f"- {key}: `{value}`")
        if payload.get("omitted"):
            lines.append("\n## Omitted (fail-soft)")
            lines.extend(f"- {note}" for note in payload["omitted"])
    elif lens == "focus":
        lines.append(f"Work: {payload.get('work_id')}")
        lines.append("\n## Target")
        lines.extend(_row_lines([payload.get("row") or {}]))
        for key, title in (("parent", "Parent"), ("children", "Children"), ("siblings", "Siblings"),
                           ("related_open", "Related Open"), ("related_done", "Related Done")):
            rows = payload.get(key)
            if not rows:
                continue
            if isinstance(rows, dict):
                rows = [rows]
            lines.append(f"\n## {title} ({len(rows)})")
            lines.extend(_row_lines(rows))
        if payload.get("events"):
            lines.append(f"\n## Recent Events ({len(payload['events'])})")
            for ev in payload["events"]:
                lines.append(f"- {ev.get('event_id')} {ev.get('kind')} {ev.get('title') or ''} {ev.get('body') or ''}".rstrip())
    elif lens in {"search", "skeleton", "history", "changes"}:
        lines.append(f"Returned: {payload.get('returned')} | Truncated: {payload.get('truncated')}")
        lines.extend(_row_lines(payload.get("results") or payload.get("rows") or []))
    elif lens == "curation":
        limits = payload.get("limits") or {}
        lines.append(
            "Read-only operator curation candidates "
            f"(limit {limits.get('requested_limit')}, min_group {limits.get('min_group')})"
        )
        contract = payload.get("operator_contract") or []
        if contract:
            lines.append("\n## Operator Contract")
            lines.extend(f"- {item}" for item in contract)
        for key, title in (
            ("terminal_prefix_groups", "Terminal Prefix Groups"),
            ("proof_repair", "Proof Repair"),
            ("open_micro_batches", "Open Micro-Batches"),
        ):
            entries = payload.get(key) or []
            lines.append(f"\n## {title} ({len(entries)})")
            if not entries:
                lines.append("- None")
                continue
            if key == "terminal_prefix_groups":
                for entry in entries:
                    lines.append(
                        f"- `{entry.get('child_prefix')}` -> `{entry.get('parent_id')}` "
                        f"({entry.get('matched_count')} rows); inspect: "
                        f"`{entry.get('inspection_command')}`"
                    )
            elif key == "proof_repair":
                for entry in entries:
                    line = _row_lines([entry])[0]
                    if entry.get("attach_proof_eligible"):
                        line += " (typed proof repair may be eligible)"
                    elif entry.get("ineligible_reason"):
                        line += f" ({entry.get('ineligible_reason')})"
                    lines.append(line)
            else:
                for entry in entries:
                    lines.append(
                        f"- `{entry.get('module')}/{entry.get('sublane')}` "
                        f"({entry.get('assignee')}, {entry.get('count')} rows): "
                        f"{entry.get('recommendation')}"
                    )
    elif lens == "board_export":
        lines.append(f"Rows exported: {payload.get('total_matching')}")
        if payload.get("path"):
            lines.append(f"Path: `{payload.get('path')}`")
    else:
        lines.append(json.dumps(payload, indent=2, sort_keys=True))
    expansion = payload.get("expansion") or {}
    if expansion:
        lines.append("\n## Expansion")
        for key, value in expansion.items():
            lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _row_lines(rows: Iterable[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        if not row:
            continue
        label = row.get("label") or row.get("display") or row.get("title") or row.get("id") or row.get("work_id")
        meta = " / ".join(str(v) for v in (row.get("status"), row.get("assignee"), row.get("module"), row.get("sublane")) if v)
        reason = row.get("why_included")
        line = f"- `{row.get('id') or row.get('work_id')}` {label}"
        if meta:
            line += f" [{meta}]"
        if reason:
            line += f" - {reason}"
        out.append(line)
    return out


def _print(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))
    else:
        print(render_markdown(payload), end="")


_SUBJECT_PLANES = frozenset({"product", "harness", "infrastructure", "shared"})
_PLANE_FILTER_VALUES = _SUBJECT_PLANES | {"unknown", "operations", "all"}
_OPERATIONS_PLANES = frozenset({"harness", "infrastructure"})


def _exact_subject_plane(row: dict[str, Any]) -> str:
    subject_plane = str(row.get("subject_plane") or "").strip().lower()
    return subject_plane if subject_plane in _SUBJECT_PLANES else "unknown"


def _filter_rows_by_plane(rows: list[dict[str, Any]], plane: str) -> list[dict[str, Any]]:
    normalized = str(plane or "").strip().lower()
    if normalized not in _PLANE_FILTER_VALUES:
        raise ValueError(f"unsupported subject plane: {plane!r}")
    if normalized == "all":
        return rows
    keep = _OPERATIONS_PLANES if normalized == "operations" else frozenset({normalized})
    return [row for row in rows if _exact_subject_plane(row) in keep]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded board-context lenses over coord.db")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    parser.add_argument(
        "--plane",
        choices=["product", "harness", "infrastructure", "shared", "unknown", "operations", "all"],
        default="all",
        help="Filter rows by exact subject_plane before any lens runs (default: all = no filter).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_digest = sub.add_parser("digest")
    p_digest.add_argument("--actor", default=None)
    p_digest.add_argument("--next-limit", type=int, default=12)
    p_digest.add_argument("--attention-limit", type=int, default=12)
    p_digest.add_argument("--recent-done-limit", type=int, default=12)
    p_digest.add_argument("--changed-limit", type=int, default=12)
    p_digest.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_focus = sub.add_parser("focus")
    p_focus.add_argument("work_id")
    p_focus.add_argument("--query", default=None)
    p_focus.add_argument("--related-limit", type=int, default=30)
    p_focus.add_argument("--event-limit", type=int, default=8)
    p_focus.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=30)
    p_search.add_argument("--no-done", action="store_true")
    p_search.add_argument("--no-open", action="store_true")
    p_search.add_argument("--no-diversify", action="store_true")
    p_search.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_history = sub.add_parser("history")
    p_history.add_argument("query")
    p_history.add_argument("--limit", type=int, default=30)
    p_history.add_argument("--no-diversify", action="store_true")
    p_history.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_skeleton = sub.add_parser("skeleton")
    p_skeleton.add_argument("--status", default="open", choices=["open", "done", "attention", "all", "queued", "planned", "running", "blocked", "failed"])
    p_skeleton.add_argument("--limit", type=int, default=MAX_SKELETON_ROWS)
    p_skeleton.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_changes = sub.add_parser("changes")
    p_changes.add_argument("--since", required=True, help="Unix timestamp or ISO-8601 datetime")
    p_changes.add_argument("--status", default="all", choices=["open", "done", "attention", "all", "queued", "planned", "running", "blocked", "failed"])
    p_changes.add_argument("--limit", type=int, default=MAX_CHANGE_ROWS)
    p_changes.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_curation = sub.add_parser("curate", aliases=["curation"])
    p_curation.add_argument("--min-group", type=int, default=3)
    p_curation.add_argument("--open-group-threshold", type=int, default=12)
    p_curation.add_argument("--limit", type=int, default=20)
    p_curation.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_capsule = sub.add_parser("capsule", help="ONE-READ live-state view: health + running + resume intents + recent decisions + policy epoch")
    p_capsule.add_argument("--decision-limit", type=int, default=MAX_CAPSULE_DECISIONS)
    p_capsule.add_argument("--resume-limit", type=int, default=MAX_CAPSULE_RESUME_INTENTS)
    p_capsule.add_argument("--running-limit", type=int, default=MAX_CAPSULE_RUNNING)
    p_capsule.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_export = sub.add_parser("export")
    p_export.add_argument("--status", default="all", choices=["open", "done", "attention", "all", "queued", "planned", "running", "blocked", "failed"])
    p_export.add_argument("--out", required=True, help="Path to write full board JSON")
    p_export.add_argument("--include-shadow", action="store_true", help="Include hidden/diagnostic shadow rows")
    p_export.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_successor = sub.add_parser(
        "successor",
        help="merge N closed sessions' closeouts into ONE successor boot doc",
    )
    p_successor.add_argument("--sessions", action="append", default=[], required=True,
                             help="session id(s); comma-separated and/or repeatable")
    p_successor.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.cmd == "export" and args.include_shadow:
        conn = _connect(args.db_path)
        try:
            rows = coord_db.board_rows(conn)
        finally:
            conn.close()
    else:
        rows = load_rows(args.db_path)
    plane = getattr(args, "plane", "all")
    rows = _filter_rows_by_plane(rows, plane)
    if args.cmd == "digest":
        canonical_open_total = None
        if plane == "all":
            canonical_open_total = canonical_board_open_total(args.db_path)
        payload = build_digest(
            rows,
            actor=args.actor,
            next_limit=args.next_limit,
            attention_limit=args.attention_limit,
            recent_done_limit=args.recent_done_limit,
            changed_limit=args.changed_limit,
            canonical_open_total=canonical_open_total,
        )
    elif args.cmd == "focus":
        try:
            payload = build_focus(
                rows,
                args.work_id,
                query=args.query,
                related_limit=args.related_limit,
                events=load_recent_events(args.work_id, db_path=args.db_path, limit=args.event_limit),
            )
        except ValueError as exc:
            print(f"board_context: {exc}", file=sys.stderr)
            print("Recovery:", file=sys.stderr)
            print(
                f"  {_BOARD_CONTEXT_COMMAND} search {json.dumps(args.work_id)} --limit 30",
                file=sys.stderr,
            )
            print(
                f"  {_BOARD_CONTEXT_COMMAND} history {json.dumps(args.work_id)} --limit 20",
                file=sys.stderr,
            )
            print("  Use MCP preflight or `coord board` to confirm the work ID.", file=sys.stderr)
            return 2
    elif args.cmd == "search":
        payload = search_rows(
            rows,
            args.query,
            limit=args.limit,
            include_done=not args.no_done,
            include_open=not args.no_open,
            diversify=not args.no_diversify,
        )
    elif args.cmd == "history":
        payload = build_history(
            rows,
            args.query,
            limit=args.limit,
            diversify=not args.no_diversify,
        )
    elif args.cmd == "skeleton":
        payload = build_skeleton(rows, status=args.status, limit=args.limit)
    elif args.cmd == "changes":
        try:
            payload = build_changes(rows, since=args.since, status=args.status, limit=args.limit)
        except ValueError as exc:
            print(f"board_context: {exc}", file=sys.stderr)
            return 2
    elif args.cmd in {"curate", "curation"}:
        payload = build_curation(
            rows,
            min_group=args.min_group,
            open_group_threshold=args.open_group_threshold,
            limit=args.limit,
        )
    elif args.cmd == "capsule":
        payload = build_capsule(
            rows,
            db_path=args.db_path,
            decision_limit=args.decision_limit,
            resume_limit=args.resume_limit,
            running_limit=args.running_limit,
        )
    elif args.cmd == "export":
        export_payload = build_export(rows, status=args.status, include_shadow=args.include_shadow)
        path = write_export(export_payload, args.out)
        payload = {
            "schema_version": 1,
            "lens": "board_export",
            "path": str(path),
            "status_filter": export_payload["status_filter"],
            "include_shadow": export_payload["include_shadow"],
            "total_matching": export_payload["total_matching"],
        }
    elif args.cmd == "successor":
        payload = build_successor_boot(rows, sessions=args.sessions, db_path=args.db_path)
    else:
        raise AssertionError(args.cmd)
    _print(payload, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
