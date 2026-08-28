
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from coordharness.coord.config import DEFAULT_DB_PATH
from coordharness.coord.run_events import canonical_run_id, run_events_table_exists, run_id_aliases


SCHEMA_VERSION = 1
MODE = "token_ledger_weekly_rollup"
EXACT_RUNNER_SOURCES = {"anthropic_usage", "codex_milestone", "mlx_lm"}
ESTIMATE_RUNNER_SOURCES = {"wallclock_fallback"}
SOURCE_PREFERENCE = {"run_events": 0, "coord_events": 1}
TRUNCATION_WARNING = (
    "event_limit truncated at least one source; totals and aggregates are partial/non-authoritative"
)
DEDUPE_RULE = (
    "Rows for the same work_id with the same explicit logical_usage_id/usage_id/call_id/"
    "idempotency_key are one logical usage; run_events is preferred over coord.events. Rows "
    "without an explicit key remain source-scoped except exact two-row run_events alias mirrors "
    "under job:<id>/run:sidecar:<id>, which are deduped by canonical_run_id and token shape."
)
RUN_ID_ALIAS_POLICY = (
    "job:<id> and run:sidecar:<id> are read-model aliases for the same physical tracked job; "
    "persisted run_ids are reported as stored and are not rewritten."
)


def _bounded_int(value: int, *, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def _work_id(value: Any, *, event_ref: str) -> tuple[str, bool]:
    text = str(value or "").strip()
    if text:
        return text, True
    return f"unattributed:{event_ref}", False


def _tokens(value: Any) -> int | None:
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens >= 0 else None


def _explicit_id(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _sort_id(value: Any) -> tuple[int, str]:
    try:
        return (0, f"{int(value):020d}")
    except (TypeError, ValueError):
        return (1, str(value or ""))


def _week_start(ts: Any) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        dt = datetime.fromtimestamp(0, tz=timezone.utc)
    monday = dt.date() - timedelta(days=dt.weekday())
    return monday.isoformat()


def _reliability(payload: dict[str, Any], *, source: str) -> str:
    explicit = str(payload.get("reliability") or payload.get("source_reliability") or "").strip().lower()
    if explicit in {"exact", "estimate"}:
        return explicit
    if payload.get("estimated") is True or payload.get("tokens_estimated") is True:
        return "estimate"
    if source == "coord_events":
        runner_source = str(payload.get("runner_source") or "").strip()
        if runner_source in EXACT_RUNNER_SOURCES:
            return "exact"
        if runner_source in ESTIMATE_RUNNER_SOURCES:
            return "estimate"
        return "estimate"
    return "exact"


def open_readonly_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"coord.db not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _coord_token_rows(conn: sqlite3.Connection, *, event_limit: int) -> tuple[list[dict[str, Any]], int]:
    total = int(conn.execute("SELECT COUNT(*) FROM events WHERE kind='token_usage'").fetchone()[0])
    rows = conn.execute(
        "SELECT event_id, ts, work_id, actor, session_id, payload_json, idempotency_key"
        " FROM events WHERE kind='token_usage'"
        " ORDER BY ts DESC, event_id DESC LIMIT ?",
        (event_limit,),
    ).fetchall()
    return [dict(row) for row in rows], total


def _run_event_token_rows(conn: sqlite3.Connection, *, event_limit: int) -> tuple[list[dict[str, Any]], int, bool]:
    if not run_events_table_exists(conn):
        return [], 0, False
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE category='token' AND event_type='token.usage'"
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT id, created_at, work_id, run_id, thread_id, session_id, content_json, metadata_json,"
        " idempotency_key"
        " FROM run_events WHERE category='token' AND event_type='token.usage'"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (event_limit,),
    ).fetchall()
    return [dict(row) for row in rows], total, True


def _normalize_coord_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = _safe_json(row.get("payload_json"))
    tokens = _tokens(payload.get("tokens"))
    event_ref = f"coord.events:{row.get('event_id')}"
    if tokens is None:
        return None, {"source": "coord_events", "event_ref": event_ref, "reason": "invalid_tokens"}
    payload_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if tokens == 0 and payload_metadata.get("attribution_kind") == "coord_cli_zero_attribution":
        return None, {"source": "coord_events", "event_ref": event_ref, "reason": "coord_cli_zero_attribution"}
    work_id, attributed = _work_id(row.get("work_id"), event_ref=event_ref)
    logical_usage_id = _explicit_id(
        payload.get("logical_usage_id"),
        payload.get("usage_id"),
        payload.get("call_id"),
        payload.get("idempotency_key"),
        row.get("idempotency_key"),
    )
    normalized = {
        "source": "coord_events",
        "source_event_id": row.get("event_id"),
        "timestamp": row.get("ts"),
        "week_start": _week_start(row.get("ts")),
        "work_id": work_id,
        "work_id_attributed": attributed,
        "runner": _text(payload.get("runner")),
        "model": _text(payload.get("model")),
        "reliability": _reliability(payload, source="coord_events"),
        "tokens": tokens,
        "runner_source": _text(payload.get("runner_source")),
        "session_id": row.get("session_id"),
        "logical_usage_id": logical_usage_id or f"coord_events:{row.get('event_id')}",
        "logical_usage_id_explicit": logical_usage_id is not None,
    }
    return normalized, None


def _normalize_run_event_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    content = _safe_json(row.get("content_json"))
    metadata = _safe_json(row.get("metadata_json"))
    tokens = _tokens(content.get("tokens"))
    event_ref = f"run_events:{row.get('id')}"
    if tokens is None:
        return None, {"source": "run_events", "event_ref": event_ref, "reason": "invalid_tokens"}
    if tokens == 0 and metadata.get("attribution_kind") == "coord_cli_zero_attribution":
        return None, {"source": "run_events", "event_ref": event_ref, "reason": "coord_cli_zero_attribution"}
    work_id, attributed = _work_id(row.get("work_id"), event_ref=event_ref)
    logical_usage_id = _explicit_id(
        content.get("logical_usage_id"),
        content.get("usage_id"),
        content.get("call_id"),
        content.get("idempotency_key"),
        metadata.get("logical_usage_id"),
        metadata.get("usage_id"),
        metadata.get("call_id"),
        metadata.get("idempotency_key"),
        row.get("idempotency_key"),
    )
    stored_run_id = row.get("run_id")
    aliases = run_id_aliases(str(stored_run_id or ""))
    normalized = {
        "source": "run_events",
        "source_event_id": row.get("id"),
        "timestamp": row.get("created_at"),
        "week_start": _week_start(row.get("created_at")),
        "work_id": work_id,
        "work_id_attributed": attributed,
        "runner": _text(content.get("caller")),
        "model": _text(content.get("model")),
        "reliability": _reliability({**metadata, **content}, source="run_events"),
        "tokens": tokens,
        "runner_source": _text(content.get("caller")),
        "run_id": stored_run_id,
        "canonical_run_id": canonical_run_id(str(stored_run_id or "")),
        "run_id_aliases": list(aliases),
        "thread_id": row.get("thread_id"),
        "session_id": row.get("session_id"),
        "logical_usage_id": logical_usage_id or f"run_events:{row.get('id')}",
        "logical_usage_id_explicit": logical_usage_id is not None,
    }
    return normalized, None


def _aggregate(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        key = (
            event["week_start"],
            event["work_id"],
            event["runner"],
            event["model"],
            event["source"],
            event["reliability"],
        )
        row = grouped.setdefault(
            key,
            {
                "week_start": event["week_start"],
                "work_id": event["work_id"],
                "runner": event["runner"],
                "model": event["model"],
                "source": event["source"],
                "reliability": event["reliability"],
                "tokens": 0,
                "event_count": 0,
            },
        )
        row["tokens"] += int(event["tokens"])
        row["event_count"] += 1
    return [grouped[key] for key in sorted(grouped)]


def _event_sort_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event["week_start"],
        event["work_id"],
        event["runner"],
        event["model"],
        event["source"],
        _sort_id(event.get("source_event_id")),
    )


def _dedupe_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped_explicit: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    grouped_implicit_alias: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    selected: list[dict[str, Any]] = []
    for event in events:
        if event.get("logical_usage_id_explicit"):
            grouped_explicit[(str(event["work_id"]), str(event["logical_usage_id"]))].append(event)
        elif (
            event.get("source") == "run_events"
            and event.get("canonical_run_id")
            and len(event.get("run_id_aliases") or []) > 1
        ):
            grouped_implicit_alias[
                (
                    event.get("work_id"),
                    event.get("canonical_run_id"),
                    event.get("week_start"),
                    event.get("runner"),
                    event.get("model"),
                    event.get("reliability"),
                    event.get("tokens"),
                )
            ].append(event)
        else:
            selected.append(event)

    duplicate_groups: list[dict[str, Any]] = []
    for (work_id, logical_usage_id), group in grouped_explicit.items():
        ordered = sorted(
            group,
            key=lambda event: (
                SOURCE_PREFERENCE.get(str(event.get("source")), 99),
                _sort_id(event.get("source_event_id")),
            ),
        )
        chosen = ordered[0]
        selected.append(chosen)
        if len(ordered) <= 1:
            continue
        tokens_by_source: dict[str, int] = defaultdict(int)
        for event in ordered:
            tokens_by_source[str(event["source"])] += int(event["tokens"])
        duplicate_groups.append(
            {
                "work_id": work_id,
                "logical_usage_id": logical_usage_id,
                "selected_source": chosen["source"],
                "selected_source_event_id": chosen.get("source_event_id"),
                "dropped_event_count": len(ordered) - 1,
                "sources": sorted({str(event["source"]) for event in ordered}),
                "tokens_by_source": dict(sorted(tokens_by_source.items())),
            }
        )

    for key, group in grouped_implicit_alias.items():
        alias_set = set(group[0].get("run_id_aliases") or [])
        stored_set = {str(event.get("run_id") or "") for event in group}
        if len(group) == 2 and stored_set == alias_set:
            ordered = sorted(group, key=lambda event: _sort_id(event.get("source_event_id")))
            chosen = ordered[0]
            selected.append(chosen)
            duplicate_groups.append(
                {
                    "work_id": str(chosen["work_id"]),
                    "logical_usage_id": str(chosen["logical_usage_id"]),
                    "dedupe_kind": "implicit_run_id_alias",
                    "canonical_run_id": chosen.get("canonical_run_id"),
                    "selected_source": chosen["source"],
                    "selected_source_event_id": chosen.get("source_event_id"),
                    "dropped_event_count": 1,
                    "sources": ["run_events"],
                    "run_ids": sorted(stored_set),
                    "tokens_by_source": {"run_events": sum(int(event["tokens"]) for event in ordered)},
                }
            )
        else:
            selected.extend(group)

    selected.sort(key=_event_sort_key)
    duplicate_groups.sort(key=lambda row: (str(row["work_id"]), str(row.get("canonical_run_id") or ""), str(row["logical_usage_id"])))
    return selected, duplicate_groups


def build_token_ledger_rollup(
    conn: sqlite3.Connection,
    *,
    event_limit: int = 10_000,
    aggregate_limit: int = 200,
    example_limit: int = 25,
    skipped_limit: int = 25,
) -> dict[str, Any]:
    event_limit = _bounded_int(event_limit, default=10_000)
    aggregate_limit = _bounded_int(aggregate_limit, default=200)
    example_limit = _bounded_int(example_limit, default=25)
    skipped_limit = _bounded_int(skipped_limit, default=25)

    coord_rows, coord_total = _coord_token_rows(conn, event_limit=event_limit)
    run_rows, run_total, run_table_exists = _run_event_token_rows(conn, event_limit=event_limit)

    normalized: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in coord_rows:
        event, skip = _normalize_coord_row(row)
        if event is not None:
            normalized.append(event)
        elif skip is not None:
            skipped.append(skip)
    for row in run_rows:
        event, skip = _normalize_run_event_row(row)
        if event is not None:
            normalized.append(event)
        elif skip is not None:
            skipped.append(skip)

    normalized.sort(key=_event_sort_key)
    ledger_events, duplicate_groups_all = _dedupe_events(normalized)
    aggregates_all = _aggregate(normalized)
    ledger_aggregates_all = _aggregate(ledger_events)
    sources_by_reliability = Counter(event["reliability"] for event in normalized)
    zero_token_count = sum(1 for event in normalized if int(event.get("tokens") or 0) == 0)
    unattributed_count = sum(1 for event in normalized if not event.get("work_id_attributed", True))
    unattributed_zero_count = sum(
        1
        for event in normalized
        if int(event.get("tokens") or 0) == 0 and not event.get("work_id_attributed", True)
    )
    run_id_alias_event_count = sum(
        1
        for event in normalized
        if event.get("source") == "run_events" and len(event.get("run_id_aliases") or []) > 1
    )
    tokens_by_source = defaultdict(int)
    for event in normalized:
        tokens_by_source[event["source"]] += int(event["tokens"])
    source_truncated = coord_total > len(coord_rows) or run_total > len(run_rows)
    source_observed_total = sum(int(event["tokens"]) for event in normalized)
    ledger_total = sum(int(event["tokens"]) for event in ledger_events)
    warnings = [TRUNCATION_WARNING] if source_truncated else []
    duplicate_limit = skipped_limit

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "actions_enabled": False,
        "read_only": True,
        "budget_enforcement_enabled": False,
        "sources": {
            "coord_token_usage": {
                "source": "coord.events",
                "event_count": len(coord_rows),
                "total_available": coord_total,
                "truncated": coord_total > len(coord_rows),
            },
            "run_events_token_usage": {
                "source": "run_events",
                "table_exists": run_table_exists,
                "event_count": len(run_rows),
                "total_available": run_total,
                "truncated": run_total > len(run_rows),
            },
        },
        "bounds": {
            "event_limit": event_limit,
            "aggregate_limit": aggregate_limit,
            "example_limit": example_limit,
            "skipped_limit": skipped_limit,
        },
        "dedupe": {
            "rule": DEDUPE_RULE,
            "preferred_source_order": ["run_events", "coord_events"],
            "duplicate_group_count": len(duplicate_groups_all),
            "duplicate_groups_truncated": len(duplicate_groups_all) > duplicate_limit,
        },
        "run_id_aliases": {
            "policy": RUN_ID_ALIAS_POLICY,
            "aliased_event_count": run_id_alias_event_count,
        },
        "summary": {
            "normalized_event_count": len(normalized),
            "ledger_event_count": len(ledger_events),
            "skipped_event_count": len(skipped),
            "total_tokens": ledger_total,
            "ledger_total_tokens": ledger_total,
            "source_observed_total_tokens": source_observed_total,
            "deduped_event_count": len(normalized) - len(ledger_events),
            "zero_token_event_count": zero_token_count,
            "unattributed_event_count": unattributed_count,
            "unattributed_zero_event_count": unattributed_zero_count,
            "total_tokens_authoritative": not source_truncated,
            "source_observed_total_authoritative": not source_truncated,
            "total_tokens_semantics": (
                "deduped_ledger_total"
                if not source_truncated
                else "partial_deduped_ledger_total_due_to_event_limit"
            ),
            "tokens_by_source": dict(sorted(tokens_by_source.items())),
            "events_by_reliability": dict(sorted(sources_by_reliability.items())),
            "aggregate_count": len(aggregates_all),
            "ledger_aggregate_count": len(ledger_aggregates_all),
            "aggregates_truncated": len(aggregates_all) > aggregate_limit,
            "ledger_aggregates_truncated": len(ledger_aggregates_all) > aggregate_limit,
            "examples_truncated": len(normalized) > example_limit,
            "skipped_truncated": len(skipped) > skipped_limit,
        },
        "warnings": warnings,
        "aggregates": aggregates_all[:aggregate_limit],
        "ledger_aggregates": ledger_aggregates_all[:aggregate_limit],
        "examples": normalized[:example_limit],
        "duplicate_groups": duplicate_groups_all[:duplicate_limit],
        "skipped": skipped[:skipped_limit],
    }


def build_token_ledger_rollup_from_db(
    db_path: str | Path | None = None,
    *,
    event_limit: int = 10_000,
    aggregate_limit: int = 200,
    example_limit: int = 25,
    skipped_limit: int = 25,
) -> dict[str, Any]:
    conn = open_readonly_connection(db_path)
    try:
        return build_token_ledger_rollup(
            conn,
            event_limit=event_limit,
            aggregate_limit=aggregate_limit,
            example_limit=example_limit,
            skipped_limit=skipped_limit,
        )
    finally:
        conn.close()
