
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from coordharness.coord import board_context
from coordharness.coord.config import harness_autonomy_config
from coordharness.coord.modeld_lite import ModeldLiteRequest, plan_request
from coordharness.coord.staleness import SUPERVISOR_STALE_RUNNING_SECS


OPEN_STATUSES = {"queued", "planned", "running", "blocked", "needs_verification", "artifact_present", "awaiting_artifact"}
TERMINAL_STATUSES = {"done", "failed", "archived", "superseded", "cancelled", "canceled", "closed", "skipped"}
RANKING_VERSION = "supervisor_readonly.v2"
SEVERITY_RANK_WEIGHT = {"urgent": 3000.0, "warn": 2000.0, "info": 1000.0}
KIND_RANK_WEIGHT = {
    "expired_running_claim": 500.0,
    "blocked_needs_triage": 400.0,
    "stale_running_heartbeat": 300.0,
    "verification_queue": 250.0,
    "launch_contract_gap": 200.0,
    "ready_next_move": 100.0,
}
ACTIONABILITY_RANK_WEIGHT = {
    "ready_to_claim": 1200.0,
    "broad_metadata_gap": -1300.0,
}


@dataclass(frozen=True)
class SupervisorSuggestion:
    work_id: str
    kind: str
    severity: str
    reason: str
    suggested_next_step: str
    refs: list[str]
    status: str | None = None
    assignee: str | None = None
    priority: float | None = None
    rank: int | None = None
    rank_score: float | None = None
    rank_reasons: list[str] = field(default_factory=list)
    actionability: str | None = None
    action_policy: str = "observe_only"
    modeld_lite_plan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload.get("modeld_lite_plan") is None:
            payload.pop("modeld_lite_plan", None)
        return payload


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _status(row: dict[str, Any]) -> str:
    return _text(row.get("status") or row.get("intent_state")).lower()


def _work_id(row: dict[str, Any]) -> str:
    return _text(row.get("work_id") or row.get("id") or row.get("roadmap_id"))


def _priority(row: dict[str, Any]) -> float:
    try:
        return float(row.get("priority"))
    except (TypeError, ValueError):
        return 99.0


def _updated_at(row: dict[str, Any]) -> float:
    for key in ("updated_at", "created_at"):
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            continue
    return 0.0


def _refs_for(row: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    work_id = _work_id(row)
    if work_id:
        refs.append(f"coord://work/{work_id}")
    for key in ("done_signal", "context_pack_ref"):
        value = _text(row.get(key)).strip()
        if value:
            refs.append(value)
    return refs


def _is_low_context_legacy_import(row: dict[str, Any]) -> bool:
    note = _text(row.get("note")).lower()
    return (
        "legacy jobs.db import" in note
        and not row.get("assignee")
        and not row.get("done_signal")
        and not row.get("context_pack_ref")
    )


def _is_broad_metadata_gap(row: dict[str, Any]) -> bool:
    work_id = _work_id(row).upper()
    title = _text(row.get("title") or row.get("display")).lower()
    level = _text(row.get("level") or row.get("work_model") or row.get("kind")).lower()
    return work_id.startswith("EPIC-") or level == "epic" or " epic" in f" {title}"


def _base_suggestion(
    row: dict[str, Any],
    *,
    kind: str,
    severity: str,
    reason: str,
    suggested_next_step: str,
    actionability: str | None = None,
) -> SupervisorSuggestion:
    return SupervisorSuggestion(
        work_id=_work_id(row),
        kind=kind,
        severity=severity,
        reason=reason,
        suggested_next_step=suggested_next_step,
        refs=_refs_for(row),
        status=_status(row) or None,
        assignee=_text(row.get("assignee")) or None,
        priority=_priority(row),
        actionability=actionability,
    )


def _rank_score(item: SupervisorSuggestion) -> float:
    priority = item.priority if item.priority is not None else 99.0
    priority_weight = max(0.0, 100.0 - min(priority, 100.0))
    score = (
        SEVERITY_RANK_WEIGHT.get(item.severity, 0.0)
        + KIND_RANK_WEIGHT.get(item.kind, 0.0)
        + ACTIONABILITY_RANK_WEIGHT.get(item.actionability or "", 0.0)
        + priority_weight
    )
    return round(score, 3)


def _rank_reasons(item: SupervisorSuggestion) -> list[str]:
    reasons = [f"severity:{item.severity}", f"kind:{item.kind}"]
    if item.priority is not None:
        reasons.append(f"priority:{item.priority:g}")
    if item.status:
        reasons.append(f"status:{item.status}")
    if item.actionability:
        reasons.append(f"actionability:{item.actionability}")
    return reasons


def _ranked_suggestions(suggestions: list[SupervisorSuggestion], limit: int) -> list[SupervisorSuggestion]:
    scored = [
        replace(item, rank_score=_rank_score(item), rank_reasons=_rank_reasons(item))
        for item in suggestions
    ]
    scored.sort(key=lambda item: (-(item.rank_score or 0.0), item.work_id))
    bounded = scored[: max(0, int(limit))]
    return [replace(item, rank=index) for index, item in enumerate(bounded, start=1)]


def _modeld_mode_for(item: SupervisorSuggestion) -> str:
    if item.kind == "verification_queue":
        return "audit"
    return "triage"


def _modeld_prompt_for(item: SupervisorSuggestion) -> str:
    return (
        f"Readonly supervisor suggestion for {item.work_id}.\n"
        f"Kind: {item.kind}\n"
        f"Severity: {item.severity}\n"
        f"Reason: {item.reason}\n"
        f"Suggested next step: {item.suggested_next_step}\n"
        "Return only advisory triage notes; do not claim, release, complete, or mutate coordination state."
    )


def _with_modeld_plan(item: SupervisorSuggestion, *, autonomy: dict[str, Any]) -> SupervisorSuggestion:
    request = ModeldLiteRequest(
        work_id=item.work_id,
        mode=_modeld_mode_for(item),
        prompt=_modeld_prompt_for(item),
        max_output_tokens=256,
    )
    return replace(item, modeld_lite_plan=plan_request(request, autonomy=autonomy))


def _autonomy_enabled(autonomy: dict[str, Any], tier: str) -> bool:
    tiers = autonomy.get("tiers") if isinstance(autonomy.get("tiers"), dict) else {}
    return bool(autonomy.get("enabled") and tiers.get(tier))


def _disabled_payload(rows: list[dict[str, Any]], *, now: float, autonomy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "readonly",
        "actions_enabled": False,
        "ranking_version": RANKING_VERSION,
        "autonomy": autonomy,
        "output_contract": {
            "authority": "advisory_only",
            "mutates_coord_state": False,
            "writes_events": False,
            "starts_processes": False,
            "respects_live_leases": True,
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "row_count": len(rows),
        "suggestion_count": 0,
        "counts_by_kind": {},
        "suggestions": [],
        "disabled_reason": str(autonomy.get("reason") or "autonomy_disabled"),
    }


def suggest_from_rows(
    rows: list[dict[str, Any]],
    *,
    now: float | None = None,
    limit: int = 25,
    stale_running_s: float = SUPERVISOR_STALE_RUNNING_SECS,
    blocked_stale_s: float = 86_400.0,
    include_modeld_plan: bool = False,
    include_loop_proposals: bool = False,
    loop_proposal_limit: int = 10,
    autonomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    autonomy = autonomy or harness_autonomy_config(default_enabled=False)
    if not _autonomy_enabled(autonomy, "supervisor_readonly_digest"):
        payload = _disabled_payload(rows, now=now, autonomy=autonomy)
        if include_modeld_plan:
            payload["modeld_lite_planning"] = {
                "included": False,
                "actions_enabled": False,
                "auto_execute": False,
                "allow_complete": False,
                "disabled_reason": payload["disabled_reason"],
            }
        if include_loop_proposals:
            payload["loop_contract_proposals"] = {
                "schema_version": 1,
                "mode": "manual_loop_contract_proposals",
                "actions_enabled": False,
                "proposal_count": 0,
                "disabled_reason": payload["disabled_reason"],
            }
        return payload
    suggestions: list[SupervisorSuggestion] = []

    for row in rows:
        work_id = _work_id(row)
        if not work_id:
            continue
        status = _status(row)
        if status not in OPEN_STATUSES or status in TERMINAL_STATUSES:
            continue
        priority = _priority(row)
        age_s = max(0.0, now - _updated_at(row)) if _updated_at(row) else 0.0

        if status == "running":
            claim_expires = row.get("claim_expires_at")
            try:
                expired = float(claim_expires) < now
            except (TypeError, ValueError):
                expired = False
            if expired:
                suggestions.append(
                    _base_suggestion(
                        row,
                        kind="expired_running_claim",
                        severity="urgent",
                        reason="running row has an expired claim lease",
                        suggested_next_step=f"Inspect with board_context focus {work_id}; only a human/agent should release or reclaim through coord helpers.",
                    )
                )
            elif age_s >= stale_running_s:
                suggestions.append(
                    _base_suggestion(
                        row,
                        kind="stale_running_heartbeat",
                        severity="warn",
                        reason=f"running row has not updated for {int(age_s)}s",
                        suggested_next_step=f"Inspect with board_context focus {work_id} and ask the owner for a heartbeat before takeover.",
                    )
                )
            continue

        if status == "blocked":
            has_block_context = bool(row.get("blocked_reason_class") or row.get("claim_step") or row.get("note"))
            if not has_block_context or age_s >= blocked_stale_s:
                suggestions.append(
                    _base_suggestion(
                        row,
                        kind="blocked_needs_triage",
                        severity="warn" if has_block_context else "urgent",
                        reason="blocked row needs clearer unblock context" if not has_block_context else f"blocked row unchanged for {int(age_s)}s",
                        suggested_next_step=f"Open board_context focus {work_id}; add a specific blocker, next decision, or park/replan note through coord helpers.",
                    )
                )
            continue

        if status in {"needs_verification", "artifact_present", "awaiting_artifact"}:
            suggestions.append(
                _base_suggestion(
                    row,
                    kind="verification_queue",
                    severity="warn",
                    reason=f"{status} row needs proof/rubric follow-through",
                    suggested_next_step=f"Inspect artifacts for {work_id}; do not mark done without complete_claim/rubric proof.",
                )
            )
            continue

        if status in {"queued", "planned"} and priority <= 1:
            if _is_low_context_legacy_import(row):
                continue
            if row.get("done_signal") and row.get("context_pack_ref"):
                suggestions.append(
                    _base_suggestion(
                        row,
                        kind="ready_next_move",
                        severity="info",
                        reason="high-priority open row has both done_signal and context_pack_ref",
                        suggested_next_step=f"Candidate for manual claim after checking board_context focus {work_id}.",
                        actionability="ready_to_claim",
                    )
                )
            else:
                missing = [
                    name for name, present in (
                        ("done_signal", row.get("done_signal")),
                        ("context_pack_ref", row.get("context_pack_ref")),
                    )
                    if not present
                ]
                suggestions.append(
                    _base_suggestion(
                        row,
                        kind="launch_contract_gap",
                        severity="warn",
                        reason=f"high-priority open row missing {', '.join(missing)}",
                        suggested_next_step=f"Backfill launch context for {work_id} before starting execution.",
                        actionability="broad_metadata_gap" if _is_broad_metadata_gap(row) else None,
                    )
                )

    suggestions = _ranked_suggestions(suggestions, limit)
    include_modeld = include_modeld_plan and _autonomy_enabled(autonomy, "modeld_advisory")
    if include_modeld:
        suggestions = [_with_modeld_plan(item, autonomy=autonomy) for item in suggestions]
    counts = Counter(item.kind for item in suggestions)
    payload = {
        "schema_version": 1,
        "mode": "readonly",
        "actions_enabled": False,
        "ranking_version": RANKING_VERSION,
        "autonomy": autonomy,
        "output_contract": {
            "authority": "advisory_only",
            "mutates_coord_state": False,
            "writes_events": False,
            "starts_processes": False,
            "respects_live_leases": True,
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "row_count": len(rows),
        "suggestion_count": len(suggestions),
        "counts_by_kind": dict(sorted(counts.items())),
        "suggestions": [item.to_dict() for item in suggestions],
    }
    if include_modeld_plan:
        payload["modeld_lite_planning"] = {
            "included": include_modeld,
            "actions_enabled": False,
            "auto_execute": False,
            "allow_complete": False,
            "mode_source": "supervisor_suggestion_kind",
            "disabled_reason": None if include_modeld else str(autonomy.get("reason") or "modeld_advisory_disabled"),
        }
    if include_loop_proposals:
        try:
            from coordharness.coord.loop_contracts import propose_loop_contracts_from_supervisor

            payload["loop_contract_proposals"] = propose_loop_contracts_from_supervisor(
                payload,
                rows=rows,
                limit=loop_proposal_limit,
            )
        except Exception as exc:
            payload["loop_contract_proposals"] = {
                "schema_version": 1,
                "mode": "manual_loop_contract_proposals",
                "actions_enabled": False,
                "proposal_count": 0,
                "error": str(exc),
            }
    return payload


def suggest_from_board(
    *,
    db_path: str | Path | None = None,
    limit: int = 25,
    now: float | None = None,
    include_modeld_plan: bool = False,
    include_loop_proposals: bool = False,
    loop_proposal_limit: int = 10,
    resource_modes_path: str | Path | None = None,
) -> dict[str, Any]:
    rows = board_context.load_rows(db_path=db_path)
    autonomy = harness_autonomy_config(resource_modes_path, default_enabled=False)
    return suggest_from_rows(
        rows,
        now=now,
        limit=limit,
        include_modeld_plan=include_modeld_plan,
        include_loop_proposals=include_loop_proposals,
        loop_proposal_limit=loop_proposal_limit,
        autonomy=autonomy,
    )


def load_rows_json(path: str | Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("rows"), list):
        raw = raw["rows"]
    if not isinstance(raw, list):
        raise ValueError("rows JSON must be a list or object with rows[]")
    return [row for row in raw if isinstance(row, dict)]
