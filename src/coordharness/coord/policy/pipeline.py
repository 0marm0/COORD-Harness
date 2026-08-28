
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from coordharness.coord import deferred_tools, output_budget, run_events


PolicyMode = Literal["report", "warn", "enforce"]
PassStatus = Literal["ok", "warning", "block"]
PASS_ORDER = (
    "creation_lint",
    "loop_doctor",
    "token_budget",
    "structured_status",
    "output_budget",
    "run_event_emit",
    "deferred_tool_catalog",
)
DEFAULT_PASS_MODES: dict[str, PolicyMode] = {
    "creation_lint": "warn",
    "loop_doctor": "warn",
    "token_budget": "warn",
    "structured_status": "warn",
    "output_budget": "warn",
    "run_event_emit": "report",
    "deferred_tool_catalog": "warn",
}
INLINE_OUTPUT_LIMIT = output_budget.INLINE_OUTPUT_LIMIT


class Pass(Protocol):
    name: str
    mode: PolicyMode

    def apply(self, ctx: "PolicyContext") -> "PassResult":
        ...


@dataclass(frozen=True)
class PolicyContext:
    work_id: str
    boundary: str
    action: str
    run_id: str | None = None
    session_id: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PassResult:
    name: str
    status: PassStatus
    reason: str = ""
    severity: str = "info"
    detail: dict[str, Any] = field(default_factory=dict)
    write_blocking: bool = True
    lifecycle_mutation: bool = False

    @classmethod
    def ok(
        cls,
        name: str,
        reason: str = "",
        *,
        detail: dict[str, Any] | None = None,
        lifecycle_mutation: bool = False,
    ) -> "PassResult":
        return cls(
            name=name,
            status="ok",
            reason=reason,
            detail=dict(detail or {}),
            lifecycle_mutation=lifecycle_mutation,
        )

    @classmethod
    def report(
        cls,
        name: str,
        reason: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> "PassResult":
        return cls(name=name, status="ok", reason=reason, detail=dict(detail or {}))

    @classmethod
    def warn(
        cls,
        name: str,
        reason: str,
        *,
        detail: dict[str, Any] | None = None,
        write_blocking: bool = True,
    ) -> "PassResult":
        return cls(
            name=name,
            status="warning",
            reason=reason,
            severity="warning",
            detail=dict(detail or {}),
            write_blocking=write_blocking,
        )

    @classmethod
    def block(
        cls,
        name: str,
        reason: str,
        *,
        detail: dict[str, Any] | None = None,
        write_blocking: bool = True,
    ) -> "PassResult":
        return cls(
            name=name,
            status="block",
            reason=reason,
            severity="error",
            detail=dict(detail or {}),
            write_blocking=write_blocking,
        )

    def to_dict(self, *, order: int, mode: PolicyMode) -> dict[str, Any]:
        return {
            "order": order,
            "name": self.name,
            "mode": mode,
            "status": self.status,
            "severity": self.severity,
            "reason": self.reason,
            "detail": dict(self.detail),
            "write_blocking": self.write_blocking,
            "lifecycle_mutation": self.lifecycle_mutation,
        }


PolicyHandler = Callable[[PolicyContext], PassResult]


@dataclass(frozen=True)
class PolicyPass:
    name: str
    mode: PolicyMode
    handler: PolicyHandler


@dataclass(frozen=True)
class PolicyResult:
    ok: bool
    blocked: bool
    block_reason: str | None
    warning_count: int
    results: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "warning_count": self.warning_count,
            "results": list(self.results),
            "warnings": list(self.warnings),
        }


def _mode_adjusted(result: PassResult, *, mode: PolicyMode) -> PassResult:
    if result.lifecycle_mutation:
        return PassResult.block(result.name, "policy pass attempted lifecycle mutation")
    if result.status == "block" and (mode != "enforce" or not result.write_blocking):
        return PassResult.warn(
            result.name,
            result.reason,
            detail=result.detail,
            write_blocking=result.write_blocking,
        )
    if result.status == "warning" and mode == "report":
        return PassResult.report(result.name, result.reason, detail=result.detail)
    return result


def run_policy_pipeline(
    context: PolicyContext,
    passes: list[PolicyPass] | tuple[PolicyPass, ...],
) -> PolicyResult:
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    blocked = False
    block_reason: str | None = None

    for policy_pass in passes:
        raw = policy_pass.handler(context)
        adjusted = _mode_adjusted(raw, mode=policy_pass.mode)
        row = adjusted.to_dict(order=len(rows) + 1, mode=policy_pass.mode)
        rows.append(row)
        if adjusted.status == "warning":
            warnings.append(row)
        if adjusted.status == "block":
            blocked = True
            block_reason = adjusted.reason
            break

    return PolicyResult(
        ok=not blocked,
        blocked=blocked,
        block_reason=block_reason,
        warning_count=len(warnings),
        results=rows,
        warnings=warnings,
    )


def _configured_modes() -> dict[str, PolicyMode]:
    try:
        from coordharness.config import HARNESS_ROOT

        path = Path(HARNESS_ROOT) / "data_local" / "resource_modes.json"
        parsed = json.loads(path.read_text())
        raw = parsed.get("harness_policy_modes", {}) if isinstance(parsed, dict) else {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, PolicyMode] = {}
        for name, mode in raw.items():
            if name in PASS_ORDER and mode in {"report", "warn", "enforce"}:
                out[str(name)] = mode
        return out
    except Exception:
        return {}


def _mode(name: str, modes: dict[str, PolicyMode] | None = None) -> PolicyMode:
    merged = dict(DEFAULT_PASS_MODES)
    merged.update(_configured_modes())
    merged.update(modes or {})
    return merged[name]


def _row_from_payload(ctx: PolicyContext) -> dict[str, Any]:
    row = ctx.payload.get("row") or ctx.payload.get("work_fields") or {}
    return dict(row) if isinstance(row, dict) else {}


def _creation_lint(ctx: PolicyContext) -> PassResult:
    detail = {
        "boundary": ctx.boundary,
        "action": ctx.action,
        "has_work_id": bool(str(ctx.work_id or "").strip()),
    }
    if ctx.action in {"handoff", "claim", "launch"} and not detail["has_work_id"]:
        return PassResult.warn("creation_lint", "write is missing work_id", detail=detail)
    return PassResult.report("creation_lint", "creation metadata observed", detail=detail)


def _loop_doctor(ctx: PolicyContext) -> PassResult:
    recent_tool_events = ctx.payload.get("recent_tool_events")
    if ctx.action == "heartbeat" and not recent_tool_events:
        return PassResult.report(
            "loop_doctor",
            "loop doctor skipped for heartbeat",
            detail={"skipped": True, "reason": "heartbeat"},
        )
    if not recent_tool_events and ctx.metadata.get("conn") is not None and str(ctx.work_id or "").strip():
        try:
            recent_tool_events = run_events.list_run_events(
                ctx.metadata["conn"],
                work_id=ctx.work_id,
                category="tool",
                limit=int(ctx.metadata.get("loop_doctor_event_limit") or 25),
            )
        except Exception as exc:
            return PassResult.warn("loop_doctor", f"loop event scan unavailable: {exc}")
    if isinstance(recent_tool_events, list) and recent_tool_events:
        try:
            from coordharness.coord.loop_contracts import loop_doctor_suggestions_from_run_events

            report = loop_doctor_suggestions_from_run_events(recent_tool_events)
        except Exception as exc:
            return PassResult.warn("loop_doctor", f"loop doctor unavailable: {exc}")
        risky = [
            suggestion
            for suggestion in report.get("suggestions", [])
            if suggestion.get("risk") in {"expensive", "dangerous", "expensive_or_dangerous"}
        ]
        if risky:
            return PassResult.warn(
                "loop_doctor",
                "repeated expensive/dangerous tool pattern detected",
                detail={
                    "mode": report.get("mode"),
                    "suggestion_count": len(risky),
                    "suggestions": risky[:5],
                },
            )

    row = _row_from_payload(ctx)
    if not row:
        return PassResult.report("loop_doctor", "no row payload supplied")
    try:
        from coordharness.coord.loop_contracts import claim_warning_for_row

        warning = claim_warning_for_row(row)
    except Exception as exc:
        return PassResult.warn("loop_doctor", f"loop doctor unavailable: {exc}")
    if warning:
        return PassResult.warn("loop_doctor", warning)
    return PassResult.report("loop_doctor", "loop contract check passed")


def _token_budget(ctx: PolicyContext) -> PassResult:
    if ctx.action == "heartbeat":
        return PassResult.report(
            "token_budget",
            "token budget skipped for heartbeat",
            detail={"skipped": True, "reason": "heartbeat"},
        )
    conn = ctx.metadata.get("conn")
    if conn is None:
        return PassResult.report("token_budget", "no coord connection supplied")
    try:
        from coordharness.coord.agent_cli import token_budget_warning

        warning = token_budget_warning(conn, ctx.work_id)
    except Exception as exc:
        return PassResult.warn("token_budget", f"token budget check unavailable: {exc}")
    if warning:
        return PassResult.warn("token_budget", "token budget threshold reached", detail=warning)
    return PassResult.report("token_budget", "token budget within warn threshold")


def _structured_status(ctx: PolicyContext) -> PassResult:
    row = _row_from_payload(ctx)
    try:
        from coordharness.coord.agent_cli import done_terminal_block_reason

        reason = done_terminal_block_reason(row) if row else None
    except Exception as exc:
        return PassResult.warn("structured_status", f"structured status unavailable: {exc}")
    if ctx.action == "done" and reason:
        return PassResult.block("structured_status", reason)
    return PassResult.report("structured_status", "structured status observed")


def _output_budget(ctx: PolicyContext) -> PassResult:
    enabled = output_budget.output_budget_enabled()
    inline_limit = int(ctx.metadata.get("inline_output_limit") or INLINE_OUTPUT_LIMIT)
    output_bytes = ctx.payload.get("output_bytes")
    if output_bytes is None and "output_text" in ctx.payload:
        output_bytes = len(str(ctx.payload.get("output_text") or "").encode("utf-8"))
    if output_bytes is None:
        detail = {
            "inline_limit": inline_limit,
            "output_bytes": None,
            "enabled": enabled,
            "env_flag": output_budget.ENV_FLAG,
            "skipped": True,
            "reason": "no_inline_output_supplied",
        }
        return PassResult.report("output_budget", "no inline output supplied", detail=detail)
    try:
        size = int(output_bytes)
    except (TypeError, ValueError):
        size = 0
    detail = {
        "inline_limit": inline_limit,
        "output_bytes": size,
        "enabled": enabled,
        "env_flag": output_budget.ENV_FLAG,
    }
    if not enabled:
        return PassResult.report("output_budget", "output budget disabled by kill switch", detail=detail)
    if size > inline_limit:
        return PassResult.warn("output_budget", "inline output exceeds budget", detail=detail)
    return PassResult.report("output_budget", "inline output within budget", detail=detail)


def _compact_emit_content(ctx: PolicyContext) -> dict[str, Any]:
    payload = ctx.payload
    content: dict[str, Any] = {
        "boundary": ctx.boundary,
        "action": ctx.action,
        "work_id": ctx.work_id,
        "actor": ctx.actor,
    }
    if ctx.run_id:
        content["run_id"] = ctx.run_id
    if ctx.session_id:
        content["session_id"] = ctx.session_id
    for key in (
        "artifact_kind",
        "artifact_path",
        "job_id",
        "kind",
        "output_bytes",
        "reason",
        "status",
        "task_mode",
    ):
        if key in payload and payload.get(key) is not None:
            content[key] = payload.get(key)
    if payload.get("step") is not None:
        content["step"] = str(payload.get("step") or "")[:300]
    return content


def _emit_token_usage(ctx: PolicyContext, *, conn: Any, run_id: str) -> dict[str, Any] | None:
    usage = ctx.payload.get("token_usage")
    if not isinstance(usage, dict):
        return None
    logical_usage_id = str(usage.get("logical_usage_id") or "").strip()
    if not logical_usage_id:
        return {"error": "token_usage.logical_usage_id missing"}
    try:
        tokens = int(usage.get("tokens") or 0)
    except (TypeError, ValueError):
        return {"error": "token_usage.tokens invalid"}
    if tokens <= 0:
        return {"skipped": True, "reason": "non_positive_tokens", "tokens": tokens}
    try:
        return run_events.record_measured_token_usage(
            conn,
            work_id=ctx.work_id,
            run_id=run_id,
            thread_id=ctx.session_id,
            session_id=ctx.session_id,
            caller=str(usage.get("caller") or ctx.actor or ctx.boundary),
            model=str(usage.get("model") or ctx.boundary),
            tokens=tokens,
            source=str(usage.get("source") or ctx.boundary),
            logical_usage_id=logical_usage_id,
            metadata={
                "adapter": "policy.run_event_emit.token_usage.v1",
                "boundary": ctx.boundary,
                "action": ctx.action,
                **(usage.get("metadata") if isinstance(usage.get("metadata"), dict) else {}),
            },
            source_reliability=str(usage.get("source_reliability") or "exact"),
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run_event_emit(ctx: PolicyContext) -> PassResult:
    conn = ctx.metadata.get("conn")
    requested_category = str(ctx.payload.get("run_event_category") or "trace").strip().lower()
    if requested_category not in run_events.ALLOWED_CATEGORIES:
        requested_category = "trace"
    category = requested_category
    detail: dict[str, Any] = {
        "category": category,
        "requested_category": requested_category,
        "boundary": ctx.boundary,
        "writer_flag": "COORD_RUNEVENTSTORE_V2",
    }
    if ctx.metadata.get("read_only") is True:
        detail.update(
            {
                "emitted": False,
                "skipped": True,
                "reason": "read_only_policy_evaluation",
            }
        )
        return PassResult.report(
            "run_event_emit",
            "policy run event skipped for read-only evaluation",
            detail=detail,
        )
    if ctx.boundary == "mcp" and ctx.action == "heartbeat":
        detail["emitted"] = False
        detail["skipped"] = True
        detail["reason"] = "mcp_heartbeat_lifecycle_event_recorded_after_renewal"
        return PassResult.report("run_event_emit", "policy run event skipped for MCP heartbeat", detail=detail)
    if conn is None:
        detail["emitted"] = False
        return PassResult.report("run_event_emit", "no coord connection supplied", detail=detail)
    if not str(ctx.work_id or "").strip():
        detail["emitted"] = False
        return PassResult.report("run_event_emit", "missing work_id; skipped run event emission", detail=detail)
    run_id = ctx.run_id or f"{ctx.boundary}:{ctx.session_id or ctx.actor or 'unknown'}:{ctx.work_id}"
    content = _compact_emit_content(ctx)
    metadata = {
        "adapter": "policy.run_event_emit.v1",
        "append_only_evidence": True,
        "authoritative_lifecycle": False,
        "lifecycle_phase": "precheck",
        "mutates_work_items": False,
        "pass": "run_event_emit",
    }
    try:
        event_id = run_events.record_run_event(
            conn,
            work_id=ctx.work_id,
            run_id=run_id,
            thread_id=ctx.session_id,
            session_id=ctx.session_id,
            category=category,
            event_type=f"policy.{ctx.boundary}.{ctx.action}",
            content=content,
            metadata=metadata,
        )
        detail.update(
            {
                "emitted": event_id is not None,
                "event_id": event_id,
                "run_id": run_id,
                "token_usage": _emit_token_usage(ctx, conn=conn, run_id=run_id),
            }
        )
    except Exception as exc:
        return PassResult.warn(
            "run_event_emit",
            f"run event store unavailable: {type(exc).__name__}: {exc}",
            detail=detail,
            write_blocking=False,
        )
    return PassResult.report(
        "run_event_emit",
        "run event emission is best effort",
        detail=detail,
    )


def filter_deferred_tools(
    tool_names: list[str] | tuple[str, ...] | set[str],
    *,
    promoted: set[str] | None = None,
) -> dict[str, Any]:
    return deferred_tools.filter_deferred_tools(tool_names, promoted=promoted)


def _deferred_tool_catalog(ctx: PolicyContext) -> PassResult:
    tools = ctx.payload.get("tool_names") or []
    if not isinstance(tools, (list, tuple, set)):
        return PassResult.report("deferred_tool_catalog", "no tool catalog payload supplied")
    catalog = filter_deferred_tools(list(tools), promoted=set(ctx.metadata.get("promoted_tools") or set()))
    if catalog["deferred"]:
        return PassResult.warn("deferred_tool_catalog", "heavy tools deferred until promoted", detail=catalog)
    return PassResult.report("deferred_tool_catalog", "tool catalog visible set allowed", detail=catalog)


def default_policy_passes(modes: dict[str, PolicyMode] | None = None) -> list[PolicyPass]:
    handlers: dict[str, PolicyHandler] = {
        "creation_lint": _creation_lint,
        "loop_doctor": _loop_doctor,
        "token_budget": _token_budget,
        "structured_status": _structured_status,
        "output_budget": _output_budget,
        "run_event_emit": _run_event_emit,
        "deferred_tool_catalog": _deferred_tool_catalog,
    }
    return [PolicyPass(name, _mode(name, modes), handlers[name]) for name in PASS_ORDER]


def _has_structured_terminal_status(payload: dict[str, Any]) -> bool:
    row = payload.get("row") or payload.get("work_fields") or {}
    if not isinstance(row, dict):
        return False
    try:
        from coordharness.coord.loop_contracts import done_terminal_state

        return done_terminal_state(row) is not None
    except Exception:
        return any(str(row.get(field) or "").strip() for field in ("loop_terminal_state", "terminal_state", "stop_state"))


def _boundary_modes(
    *,
    action: str,
    payload: dict[str, Any],
    modes: dict[str, PolicyMode] | None,
) -> dict[str, PolicyMode] | None:
    effective = dict(modes or {})
    if action == "done" and _has_structured_terminal_status(payload):
        effective["structured_status"] = "enforce"
    return effective or modes


def run_boundary_policy(
    *,
    boundary: str,
    action: str,
    work_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
    actor: str | None = None,
    payload: dict[str, Any] | None = None,
    conn: Any | None = None,
    modes: dict[str, PolicyMode] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    if conn is not None:
        meta["conn"] = conn
    full_payload = dict(payload or {})
    result = run_policy_pipeline(
        PolicyContext(
            work_id=work_id,
            boundary=boundary,
            action=action,
            run_id=run_id,
            session_id=session_id,
            actor=actor,
            payload=full_payload,
            metadata=meta,
        ),
        default_policy_passes(_boundary_modes(action=action, payload=full_payload, modes=modes)),
    )
    out = result.to_dict()
    out.update({"boundary": boundary, "action": action, "work_id": work_id, "pass_order": list(PASS_ORDER)})
    return out


def apply_output_budget(
    text: str,
    *,
    inline_limit: int = INLINE_OUTPUT_LIMIT,
    artifact_dir: str | Path | None = None,
    artifact_prefix: str = "policy-output",
) -> dict[str, Any]:
    return output_budget.apply_output_budget(
        text,
        inline_limit=inline_limit,
        artifact_dir=artifact_dir,
        artifact_prefix=artifact_prefix,
    )
