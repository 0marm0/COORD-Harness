
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


from coordharness import config as _harness_config
from .config import (
    configured_lanes as _configured_lanes,
    lanes_display as _lanes_display,
)

_REPO_ROOT = _harness_config.project_root()
DEFAULT_AUDIT_VERDICT_CONTRACT_PATH = _REPO_ROOT / "coordharness" / "data_local" / "contracts" / "audit_verdict.json"
AUDIT_VERDICT_STATUSES = {
    "completed",
    "failed",
    "blocked",
    "approval_required",
    "exhausted",
    "stagnated",
}
AUDIT_VERDICT_STATUS_BY_VERDICT = {
    "PASS": "completed",
    "FLAG": "failed",
    "BLOCKED": "blocked",
}
AUDIT_VERDICT_TEMPLATE = {
    "version": 1,
    "work_id": "...",
    "actor": "claude|codex",
    "status": "completed|failed|blocked|approval_required|exhausted|stagnated",
    "visibility": "...",
    "source": "...",
    "receiver_lane": "...",
    "supersedes": None,
    "acceptance": {},
    "action_metadata": {},
    "refs": [],
    "created_at": "ISO8601",
}
REQUIRED_AUDIT_VERDICT_FIELDS = tuple(AUDIT_VERDICT_TEMPLATE.keys())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _non_empty_text(value: Any) -> str:
    return str(value or "").strip()


def _allowed_statuses_from_contract(contract: dict[str, Any] | None) -> set[str]:
    raw = str((contract or {}).get("status") or "").strip()
    if "|" in raw:
        return {part.strip() for part in raw.split("|") if part.strip()}
    return set(AUDIT_VERDICT_STATUSES)


def load_audit_verdict_contract(path: str | Path | None = None) -> dict[str, Any]:
    contract_path = Path(path) if path is not None else DEFAULT_AUDIT_VERDICT_CONTRACT_PATH
    try:
        parsed = json.loads(contract_path.read_text())
    except FileNotFoundError:
        return dict(AUDIT_VERDICT_TEMPLATE)
    if not isinstance(parsed, dict):
        return dict(AUDIT_VERDICT_TEMPLATE)
    return parsed


def validate_audit_verdict_payload(
    payload: dict[str, Any],
    *,
    contract: dict[str, Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["audit verdict payload must be a JSON object"]
    for field in REQUIRED_AUDIT_VERDICT_FIELDS:
        if field not in payload:
            issues.append(f"missing field: {field}")
    if payload.get("version") != 1:
        issues.append("version must equal 1")
    for field in ("work_id", "actor", "status", "visibility", "source", "receiver_lane", "created_at"):
        if not _non_empty_text(payload.get(field)):
            issues.append(f"{field} must be non-empty")
    status = _non_empty_text(payload.get("status"))
    allowed_statuses = _allowed_statuses_from_contract(contract)
    if status and status not in allowed_statuses:
        issues.append(f"status must be one of {sorted(allowed_statuses)}")
    lanes = _configured_lanes()
    if payload.get("actor") not in lanes:
        issues.append(f"actor must be {_lanes_display()}")
    if payload.get("receiver_lane") not in lanes:
        issues.append(f"receiver_lane must be {_lanes_display()}")
    if payload.get("supersedes") is not None and not isinstance(payload.get("supersedes"), str):
        issues.append("supersedes must be string|null")
    if not isinstance(payload.get("acceptance"), dict):
        issues.append("acceptance must be an object")
    if not isinstance(payload.get("action_metadata"), dict):
        issues.append("action_metadata must be an object")
    refs = payload.get("refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        issues.append("refs must be a string list")
    return issues


def audit_verdict_status(verdict_value: str) -> str:
    verdict_key = str(verdict_value or "").strip().upper()
    try:
        return AUDIT_VERDICT_STATUS_BY_VERDICT[verdict_key]
    except KeyError as exc:
        raise ValueError(f"verdict must be one of {sorted(AUDIT_VERDICT_STATUS_BY_VERDICT)}") from exc


def audit_verdict_payload(
    *,
    work_id: str,
    actor: str,
    verdict_value: str,
    severity: str | None,
    refs: list[str],
    receiver_lane: str,
    session_id: str | None = None,
    visibility: str = "internal",
    source: str | None = None,
    supersedes: str | None = None,
    acceptance: dict[str, Any] | None = None,
    action_metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor_key = _non_empty_text(actor).split(":", 1)[0]
    receiver_key = _non_empty_text(receiver_lane)
    verdict_key = str(verdict_value or "").strip().upper()
    payload = {
        "version": 1,
        "schema_version": 1,
        "work_id": _non_empty_text(work_id),
        "actor": actor_key,
        "status": audit_verdict_status(verdict_key),
        "visibility": _non_empty_text(visibility) or "internal",
        "source": _non_empty_text(source) or f"{actor_key}_coord.verdict",
        "receiver_lane": receiver_key,
        "supersedes": supersedes,
        "acceptance": dict(acceptance or {"verdict": verdict_key, "severity": severity}),
        "action_metadata": {
            "verdict": verdict_key,
            "severity": severity,
            "session_id": session_id,
            **dict(action_metadata or {}),
        },
        "refs": [str(ref).strip() for ref in (refs or []) if str(ref).strip()],
        "created_at": created_at or _now_iso(),
        "verdict": verdict_key,
        "severity": severity,
        "to_lane": receiver_key,
    }
    issues = validate_audit_verdict_payload(payload, contract=contract or load_audit_verdict_contract())
    if issues:
        raise ValueError("; ".join(issues))
    return payload


def done_terminal_block_reason(row: dict[str, Any]) -> str | None:
    from coordharness.coord.loop_contracts import done_terminal_block_reason as _reason

    return _reason(row)


def token_budget_warning(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    warn_ratio: float = 0.8,
) -> dict[str, Any] | None:
    row = conn.execute("SELECT token_budget FROM work_items WHERE work_id=?", (work_id,)).fetchone()
    if row is None:
        return None
    raw_budget = row["token_budget"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        budget = int(raw_budget)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    from coordharness.coord.token_ledger_rollup import build_token_ledger_rollup

    report = build_token_ledger_rollup(conn, event_limit=50_000, aggregate_limit=50_000, example_limit=0)
    used = sum(
        int(aggregate.get("tokens") or 0)
        for aggregate in report.get("ledger_aggregates", [])
        if str(aggregate.get("work_id") or "") == str(work_id)
    )
    ratio = used / budget if budget else 0.0
    if ratio < warn_ratio:
        return None
    return {
        "work_id": work_id,
        "mode": "warn",
        "budget": budget,
        "used": used,
        "remaining": budget - used,
        "ratio": ratio,
        "threshold": warn_ratio,
        "over_budget": used > budget,
    }


def warn_token_budget_on_claim(conn: sqlite3.Connection, work_id: str, *, stream: Any = None) -> None:
    warning = token_budget_warning(conn, work_id)
    if warning is None:
        return
    out = stream if stream is not None else sys.stderr
    print(
        "token budget warning: "
        f"{work_id} used={warning['used']} budget={warning['budget']} "
        f"remaining={warning['remaining']} mode=warn",
        file=out,
    )


def format_coord_rejection(
    cmd: str | None,
    work_id: str | None,
    result: Any,
    *,
    agent: str,
) -> str:

    if not isinstance(result, dict):
        return f"invalid coord result: {result!r}"
    reason = result.get("reason") or result.get("error") or result.get("status") or "unknown"
    header = str(result.get("message") or reason)
    if not result.get("message") and (cmd or work_id):
        header = f"{agent}_coord {cmd or '<unknown>'} {work_id or '<none>'}: {header}"
    lines = [header]
    commands = result.get("recovery_commands")
    if isinstance(commands, list) and commands:
        lines.append("Recovery:")
        lines.extend(f"  {cmd}" for cmd in commands if str(cmd or "").strip())
    return "\n".join(lines)
