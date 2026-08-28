
from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from coordharness.coord import run_events


ALLOWED_LOOP_TYPES = {
    "coord",
    "ops",
    "docs",
    "context",
    "runtime",
    "audit",
    "model",
    "product",
}
ALLOWED_TERMINAL_STATES = {
    "success",
    "clean_noop",
    "blocked",
    "approval_required",
    "exhausted",
    "stagnated",
    "failed",
}
DONE_ALLOWED_TERMINAL_STATES = {"success", "clean_noop"}
RECEIPT_STATES = ALLOWED_TERMINAL_STATES | {"continue"}
TERMINAL_STATE_FIELDS = (
    "loop_terminal_state",
    "terminal_state",
    "stop_state",
)
REQUIRED_STEP_FIELDS = ("observe", "choose", "act", "verify", "record")
MAX_STEP_ITEMS = 12
MAX_TEXT_CHARS = 1200
LOOP_SIGNAL_TERMS = {
    "autonomous",
    "daemon",
    "digest",
    "every",
    "loop",
    "monitor",
    "queue",
    "recurring",
    "retry",
    "scheduled",
    "supervisor",
    "sync",
    "triage",
    "watcher",
}
HIGH_SIGNAL_TERMS = {"autonomous", "daemon", "loop", "recurring", "scheduled", "supervisor", "watcher"}
DANGEROUS_AUTHORITY_TERMS = {
    "budget",
    "credential",
    "delete",
    "deploy",
    "destructive",
    "external send",
    "rm -rf",
    "send email",
    "token override",
}
APPROVAL_GATE_TERMS = {
    "budget",
    "budget_halt_override",
    "credential_access",
    "destructive_command",
    "external_send",
    "production_deploy",
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]$")
LOOP_SUGGESTIONS_MODE = "advisory_loop_contract_suggestions"
LOOP_SUGGESTIONS_COMPAT_DECISION = "schema_version_1_additive_requires_ranking_version_v2"
REQUIRED_SUPERVISOR_RANK_FIELDS = ("rank", "rank_score", "rank_reasons", "action_policy")
DEFAULT_OMITTED_LIMIT = 25
COMPACT_VERIFY_COMMAND = "coordharness/.venv/bin/python coordharness/scripts/verify_jobs.py --summary --no-oks"
REQUIRED_RECEIPT_FIELDS = ("work_id", "loop_id", "iteration", "terminal_state", "observed", "action", "verification", "artifact_refs")
MAX_RECEIPT_ARTIFACT_REFS = 20
EXPENSIVE_TOOL_TERMS = {
    "exec_command",
    "pytest",
    "run_tests",
    "verify_jobs",
    "gpu_job",
    "run_tracked_job",
    "web.run",
    "imagegen",
}
DANGEROUS_TOOL_TERMS = {
    "apply_patch",
    "git push",
    "deploy",
    "request_plugin_install",
    "rm -rf",
    "delete",
}
DEFAULT_LOOP_STAGNATION_LIMIT = 2
TERMINAL_SCAN_STATUSES = {
    "archived",
    "canceled",
    "cancelled",
    "cleared",
    "closed",
    "done",
    "failed",
    "superseded",
}


@dataclass(frozen=True)
class LoopContractIssue:
    code: str
    severity: str
    message: str
    repair_hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def issue_codes(issues: Iterable[LoopContractIssue]) -> list[str]:
    return [issue.code for issue in issues]


def issues_to_dicts(issues: Iterable[LoopContractIssue]) -> list[dict[str, str]]:
    return [issue.to_dict() for issue in issues]


def _issue(code: str, severity: str, message: str, repair_hint: str = "") -> LoopContractIssue:
    return LoopContractIssue(code=code, severity=severity, message=message, repair_hint=repair_hint)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def parse_jsonish(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return json.loads(text)
    return value


def contract_from_value(value: Any) -> tuple[dict[str, Any] | None, list[LoopContractIssue]]:
    try:
        parsed = parse_jsonish(value)
    except Exception as exc:
        return None, [_issue("invalid_loop_contract_json", "error", f"loop contract JSON is invalid: {exc}")]
    if parsed is None:
        return None, []
    if not isinstance(parsed, dict):
        return None, [_issue("invalid_loop_contract_schema", "error", "loop contract must be a JSON object")]
    return parsed, []


def acceptance_from_row(row: dict[str, Any], *, warn_invalid: bool = True) -> tuple[dict[str, Any], list[LoopContractIssue]]:
    value = row.get("acceptance_json")
    if value in (None, ""):
        return {}, []
    try:
        parsed = parse_jsonish(value)
    except Exception as exc:
        issues = [_issue("invalid_acceptance_json", "warn", f"acceptance_json is invalid JSON: {exc}")] if warn_invalid else []
        return {}, issues
    if parsed is None:
        return {}, []
    if not isinstance(parsed, dict):
        issues = [_issue("invalid_acceptance_json", "warn", "acceptance_json is not an object")] if warn_invalid else []
        return {}, issues
    return parsed, []


def extract_contract(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, list[LoopContractIssue]]:
    if "loop_contract" in row:
        contract, issues = contract_from_value(row.get("loop_contract"))
        return contract, None, issues
    if str(row.get("loop_contract_ref") or "").strip():
        return None, str(row.get("loop_contract_ref")).strip(), []

    acceptance, issues = acceptance_from_row(row, warn_invalid=row_has_loop_signal(row))
    if "loop_contract" in acceptance:
        contract, contract_issues = contract_from_value(acceptance.get("loop_contract"))
        return contract, None, [*issues, *contract_issues]
    ref = str(acceptance.get("loop_contract_ref") or "").strip()
    if ref:
        return None, ref, issues
    return None, None, issues


def _normalized_terminal_state(value: Any) -> str | None:
    state = str(value or "").strip().lower()
    return state or None


def done_terminal_state(row: dict[str, Any]) -> str | None:

    for field in TERMINAL_STATE_FIELDS:
        state = _normalized_terminal_state(row.get(field))
        if state:
            return state
    acceptance, _issues = acceptance_from_row(row, warn_invalid=False)
    for field in TERMINAL_STATE_FIELDS:
        state = _normalized_terminal_state(acceptance.get(field))
        if state:
            return state
    return None


def done_terminal_block_reason(row: dict[str, Any]) -> str | None:

    state = done_terminal_state(row)
    if not state:
        return None
    if state not in ALLOWED_TERMINAL_STATES:
        return f"invalid loop terminal state {state!r}; expected one of {sorted(ALLOWED_TERMINAL_STATES)}"
    if state not in DONE_ALLOWED_TERMINAL_STATES:
        return f"loop terminal state {state!r} cannot mark work done"
    return None


def validate_loop_receipt(receipt: Any) -> list[LoopContractIssue]:
    if not isinstance(receipt, dict):
        return [_issue("invalid_loop_receipt_schema", "error", "loop receipt must be a JSON object")]
    issues: list[LoopContractIssue] = []
    for field in REQUIRED_RECEIPT_FIELDS:
        if field not in receipt:
            issues.append(_issue(f"missing_receipt_{field}", "error", f"loop receipt missing {field}"))
    issues.extend(_validate_slug(receipt.get("loop_id"), "loop_id"))
    work_id = str(receipt.get("work_id") or "").strip()
    if not work_id:
        issues.append(_issue("missing_receipt_work_id", "error", "loop receipt work_id is required"))
    state = _normalized_terminal_state(receipt.get("terminal_state"))
    if state not in RECEIPT_STATES:
        issues.append(_issue("invalid_receipt_terminal_state", "error", f"terminal_state must be one of {sorted(RECEIPT_STATES)}"))
    try:
        iteration = int(receipt.get("iteration"))
    except (TypeError, ValueError):
        issues.append(_issue("invalid_receipt_iteration", "error", "iteration must be an integer"))
    else:
        if iteration < 0 or iteration > 1000:
            issues.append(_issue("invalid_receipt_iteration", "error", "iteration must be between 0 and 1000"))
    for field in ("observed", "action", "verification"):
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(_issue(f"missing_receipt_{field}", "error", f"{field} must be non-empty text"))
        elif len(value) > MAX_TEXT_CHARS:
            issues.append(_issue(f"receipt_{field}_too_long", "error", f"{field} exceeds {MAX_TEXT_CHARS} chars"))
    refs = receipt.get("artifact_refs")
    if not isinstance(refs, list):
        issues.append(_issue("invalid_receipt_artifact_refs", "error", "artifact_refs must be a list"))
    elif len(refs) > MAX_RECEIPT_ARTIFACT_REFS:
        issues.append(_issue("receipt_artifact_refs_too_many", "error", f"artifact_refs exceeds {MAX_RECEIPT_ARTIFACT_REFS} entries"))
    elif any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        issues.append(_issue("invalid_receipt_artifact_ref", "error", "artifact_refs must contain non-empty strings"))
    return issues


def _default_done_signal_exists(path: str | None) -> bool:
    proof = str(path or "").strip()
    if not proof:
        return False
    try:
        from coordharness.config import HARNESS_ROOT
        from coordharness.jobs.status import done_signal_exists

        return done_signal_exists(proof, Path(HARNESS_ROOT))
    except Exception:
        return False


def record_loop_receipt_event(
    conn: Any,
    receipt: dict[str, Any],
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    enabled: bool | None = None,
) -> int | None:
    issues = validate_loop_receipt(receipt)
    if any(issue.severity == "error" for issue in issues):
        raise ValueError("invalid loop receipt: " + ", ".join(issue.code for issue in issues[:5]))
    work_id = str(receipt["work_id"])
    loop_id = str(receipt["loop_id"])
    event_run_id = run_id or f"loop:{loop_id}:{work_id}"
    return run_events.record_run_event(
        conn,
        work_id=work_id,
        run_id=event_run_id,
        session_id=session_id,
        category="trace",
        event_type="loop.receipt",
        content={
            "work_id": work_id,
            "loop_id": loop_id,
            "iteration": int(receipt["iteration"]),
            "terminal_state": _normalized_terminal_state(receipt.get("terminal_state")),
            "observed": str(receipt.get("observed") or "")[:MAX_TEXT_CHARS],
            "action": str(receipt.get("action") or "")[:MAX_TEXT_CHARS],
            "verification": str(receipt.get("verification") or "")[:MAX_TEXT_CHARS],
            "artifact_refs": [str(ref) for ref in receipt.get("artifact_refs") or []][:MAX_RECEIPT_ARTIFACT_REFS],
        },
        metadata={
            "adapter": "loop_contracts.receipt.v1",
            "authority": "append_only_evidence",
            "auto_done_enabled": False,
            "mutates_coord_state": False,
        },
        idempotency_key=f"loop-receipt:{work_id}:{loop_id}:{event_run_id}:{int(receipt['iteration'])}",
        enabled=enabled,
    )


LoopIterationFn = Callable[[int, dict[str, Any]], dict[str, Any]]


def _positive_int(value: Any, *, field: str, maximum: int = 1000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number <= 0 or number > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return number


def _wall_seconds_from_limits(limits: dict[str, Any]) -> float:
    for key in ("max_wall_seconds", "wall_clock_seconds"):
        value = limits.get(key)
        if value not in (None, ""):
            seconds = float(value)
            if seconds <= 0:
                raise ValueError(f"limits.{key} must be > 0")
            return seconds
    value = limits.get("max_wall_minutes")
    if value not in (None, ""):
        minutes = float(value)
        if minutes <= 0:
            raise ValueError("limits.max_wall_minutes must be > 0")
        return minutes * 60.0
    raise ValueError("bounded loop runner requires limits.max_wall_seconds or limits.max_wall_minutes")


def _receipt_signature(receipt: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(receipt.get("observed") or "").strip(),
        str(receipt.get("action") or "").strip(),
        str(receipt.get("verification") or "").strip(),
    )


def run_bounded_loop_contract(
    conn: Any,
    *,
    row: dict[str, Any],
    contract: dict[str, Any],
    iterate: LoopIterationFn,
    done_signal_exists_fn: Callable[[str | None], bool] | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    enabled: bool | None = True,
    now_fn: Callable[[], float] | None = None,
    stagnation_limit: int = DEFAULT_LOOP_STAGNATION_LIMIT,
) -> dict[str, Any]:

    issues = validate_loop_contract(contract)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ValueError("invalid loop contract: " + ", ".join(issue.code for issue in errors[:5]))
    limits = contract.get("limits") if isinstance(contract.get("limits"), dict) else {}
    max_iterations = _positive_int(limits.get("max_iterations"), field="limits.max_iterations")
    max_wall_seconds = _wall_seconds_from_limits(limits)
    stagnation_limit = _positive_int(stagnation_limit, field="stagnation_limit", maximum=20)
    work_id = row_id(row)
    loop_id = str(contract.get("loop_id") or "").strip()
    proof = str(row.get("done_signal") or "").strip()
    exists = done_signal_exists_fn or _default_done_signal_exists
    clock = now_fn or time.time
    start = float(clock())
    runner_run_id = run_id or f"loop:{loop_id}:{work_id}:{uuid.uuid4().hex[:12]}"
    receipts: list[dict[str, Any]] = []
    event_ids: list[int | None] = []
    last_signature: tuple[str, str, str] | None = None
    repeated = 0

    if exists(proof):
        return {
            "schema_version": 1,
            "mode": "bounded_loop_continuation_runner",
            "actions_enabled": True,
            "mutates_coord_state": False,
            "work_id": work_id,
            "loop_id": loop_id,
            "run_id": runner_run_id,
            "terminal_state": "success",
            "stop_reason": "done_signal_already_present",
            "iterations": 0,
            "max_iterations": max_iterations,
            "max_wall_seconds": max_wall_seconds,
            "receipts": [],
            "event_ids": [],
        }

    for iteration in range(1, max_iterations + 1):
        elapsed = float(clock()) - start
        if elapsed > max_wall_seconds:
            return {
                "schema_version": 1,
                "mode": "bounded_loop_continuation_runner",
                "actions_enabled": True,
                "mutates_coord_state": False,
                "work_id": work_id,
                "loop_id": loop_id,
                "run_id": runner_run_id,
                "terminal_state": "exhausted",
                "stop_reason": "wall_clock_budget_exhausted",
                "iterations": len(receipts),
                "max_iterations": max_iterations,
                "max_wall_seconds": max_wall_seconds,
                "receipts": receipts,
                "event_ids": event_ids,
            }

        receipt = dict(iterate(iteration, {"work_id": work_id, "loop_id": loop_id, "receipts": list(receipts)}))
        receipt.update({"work_id": work_id, "loop_id": loop_id, "iteration": iteration})
        if "artifact_refs" not in receipt:
            receipt["artifact_refs"] = []
        if float(clock()) - start > max_wall_seconds and str(receipt.get("terminal_state") or "").strip() == "continue":
            receipt["terminal_state"] = "exhausted"
            receipt["verification"] = (
                str(receipt.get("verification") or "")
                + "; stopped because wall-clock budget was exhausted during iteration"
            ).strip("; ")
        signature = _receipt_signature(receipt)
        repeated = repeated + 1 if signature == last_signature else 1
        last_signature = signature
        if repeated >= stagnation_limit and str(receipt.get("terminal_state") or "").strip() == "continue":
            receipt["terminal_state"] = "stagnated"
            receipt["verification"] = (
                str(receipt.get("verification") or "")
                + f"; stopped after {repeated} repeated loop signatures"
            ).strip("; ")
        receipt_issues = validate_loop_receipt(receipt)
        if any(issue.severity == "error" for issue in receipt_issues):
            raise ValueError("invalid loop receipt: " + ", ".join(issue.code for issue in receipt_issues[:5]))
        event_ids.append(
            record_loop_receipt_event(
                conn,
                receipt,
                run_id=runner_run_id,
                session_id=session_id,
                enabled=enabled,
            )
        )
        receipts.append(receipt)
        if exists(proof):
            return {
                "schema_version": 1,
                "mode": "bounded_loop_continuation_runner",
                "actions_enabled": True,
                "mutates_coord_state": False,
                "work_id": work_id,
                "loop_id": loop_id,
                "run_id": runner_run_id,
                "terminal_state": "success",
                "stop_reason": "done_signal_present",
                "iterations": len(receipts),
                "max_iterations": max_iterations,
                "max_wall_seconds": max_wall_seconds,
                "receipts": receipts,
                "event_ids": event_ids,
            }
        state = _normalized_terminal_state(receipt.get("terminal_state"))
        if state != "continue":
            return {
                "schema_version": 1,
                "mode": "bounded_loop_continuation_runner",
                "actions_enabled": True,
                "mutates_coord_state": False,
                "work_id": work_id,
                "loop_id": loop_id,
                "run_id": runner_run_id,
                "terminal_state": state,
                "stop_reason": f"receipt_terminal_state:{state}",
                "iterations": len(receipts),
                "max_iterations": max_iterations,
                "max_wall_seconds": max_wall_seconds,
                "receipts": receipts,
                "event_ids": event_ids,
            }

    return {
        "schema_version": 1,
        "mode": "bounded_loop_continuation_runner",
        "actions_enabled": True,
        "mutates_coord_state": False,
        "work_id": work_id,
        "loop_id": loop_id,
        "run_id": runner_run_id,
        "terminal_state": "exhausted",
        "stop_reason": "max_iterations_exhausted",
        "iterations": len(receipts),
        "max_iterations": max_iterations,
        "max_wall_seconds": max_wall_seconds,
        "receipts": receipts,
        "event_ids": event_ids,
    }


def _validate_slug(value: Any, field: str, *, required: bool = True) -> list[LoopContractIssue]:
    if not isinstance(value, str) or not value.strip():
        return [] if not required else [_issue(f"missing_{field}", "error", f"{field} is required")]
    slug = value.strip()
    if len(slug) > 80 or not SLUG_RE.fullmatch(slug):
        return [_issue(f"invalid_{field}", "error", f"{field} must be a lowercase slug <= 80 chars")]
    return []


def _text_blob(contract: dict[str, Any]) -> str:
    chunks: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif value is not None:
            chunks.append(str(value))

    walk(contract)
    return " ".join(chunks).lower()


def _has_dangerous_authority(contract: dict[str, Any]) -> bool:
    blob = _text_blob(contract)
    return any(term in blob for term in DANGEROUS_AUTHORITY_TERMS)


def _approval_gates_present(contract: dict[str, Any]) -> bool:
    gates = contract.get("approval_gates")
    if not isinstance(gates, list):
        return False
    normalized = {str(gate).strip().lower() for gate in gates if str(gate).strip()}
    return bool(normalized & APPROVAL_GATE_TERMS)


def validate_loop_contract(contract: Any) -> list[LoopContractIssue]:
    if not isinstance(contract, dict):
        return [_issue("invalid_loop_contract_schema", "error", "loop contract must be a JSON object")]

    issues: list[LoopContractIssue] = []
    if contract.get("schema_version") != 1:
        issues.append(_issue("invalid_schema_version", "error", "schema_version must equal 1"))
    issues.extend(_validate_slug(contract.get("loop_id"), "loop_id"))

    loop_type = contract.get("loop_type")
    if loop_type not in ALLOWED_LOOP_TYPES:
        issues.append(_issue("invalid_loop_type", "error", f"loop_type must be one of {sorted(ALLOWED_LOOP_TYPES)}"))

    for field in ("purpose", "use_when"):
        value = contract.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(_issue(f"missing_{field}", "error", f"{field} must be non-empty text"))
        elif len(value) > MAX_TEXT_CHARS:
            issues.append(_issue(f"{field}_too_long", "error", f"{field} exceeds {MAX_TEXT_CHARS} chars"))

    for field in REQUIRED_STEP_FIELDS:
        values = contract.get(field)
        if not isinstance(values, list) or not values:
            issues.append(_issue(f"missing_{field}", "error", f"{field} must contain 1-{MAX_STEP_ITEMS} strings"))
            continue
        if len(values) > MAX_STEP_ITEMS:
            issues.append(_issue(f"{field}_too_many", "error", f"{field} exceeds {MAX_STEP_ITEMS} entries"))
        for idx, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                issues.append(_issue(f"invalid_{field}_step", "error", f"{field}[{idx}] must be non-empty text"))
            elif len(value) > MAX_TEXT_CHARS:
                issues.append(_issue(f"{field}_step_too_long", "error", f"{field}[{idx}] exceeds {MAX_TEXT_CHARS} chars"))

    stop_states = contract.get("stop_states")
    if not isinstance(stop_states, dict) or not stop_states:
        issues.append(_issue("missing_stop_states", "error", "stop_states must include success and at least one non-success state"))
    else:
        keys = {str(key) for key in stop_states}
        if "success" not in keys:
            issues.append(_issue("missing_success_stop", "error", "stop_states must include success"))
        if not (keys - {"success"}):
            issues.append(_issue("missing_non_success_stop", "error", "stop_states must include a non-success terminal state"))
        invalid = sorted(keys - ALLOWED_TERMINAL_STATES)
        if invalid:
            issues.append(_issue("invalid_stop_state", "error", f"invalid stop state(s): {', '.join(invalid)}"))

    limits = contract.get("limits")
    max_iterations = limits.get("max_iterations") if isinstance(limits, dict) else None
    has_stagnated_stop = isinstance(stop_states, dict) and "stagnated" in stop_states
    if max_iterations in (None, "") and not has_stagnated_stop:
        issues.append(_issue("unbounded_loop", "warn", "loop lacks limits.max_iterations and a stagnated stop state"))

    if _has_dangerous_authority(contract) and not _approval_gates_present(contract):
        issues.append(_issue("unsafe_authority", "warn", "potentially unsafe authority lacks an explicit approval gate"))

    evidence = contract.get("evidence")
    if not isinstance(evidence, dict):
        issues.append(_issue("missing_evidence", "warn", "evidence should name required artifacts and commands"))
    else:
        artifacts = evidence.get("required_artifacts")
        commands = evidence.get("required_commands")
        if isinstance(artifacts, list) and artifacts and "done_signal" not in {str(a) for a in artifacts}:
            issues.append(_issue("weak_success_gate", "warn", "required_artifacts should include done_signal when work can close"))
        if not isinstance(commands, list) or not commands:
            issues.append(_issue("missing_verify_command", "warn", "evidence.required_commands should include a focused gate or verify_jobs.py"))

    return issues


def _row_text(row: dict[str, Any]) -> str:
    keys = (
        "id",
        "roadmap_id",
        "work_id",
        "title",
        "display",
        "note",
        "why",
        "context",
        "context_pack_ref",
        "done_signal",
        "assignee",
        "acceptance_json",
    )
    return " ".join(str(row.get(key) or "") for key in keys).lower()


def row_has_loop_signal(row: dict[str, Any]) -> bool:
    text = _row_text(row)
    term_hits = {term for term in LOOP_SIGNAL_TERMS if re.search(rf"\b{re.escape(term)}\b", text)}
    if term_hits & HIGH_SIGNAL_TERMS:
        return True
    return len(term_hits) >= 2


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("roadmap_id") or row.get("work_id") or "<unknown>").strip()


def doctor_row(row: dict[str, Any]) -> list[LoopContractIssue]:
    contract, ref, issues = extract_contract(row)
    out = list(issues)
    if ref:
        out.extend(_validate_slug(ref, "loop_contract_ref"))
        return out
    if contract is not None:
        out.extend(validate_loop_contract(contract))
        return out
    if row_has_loop_signal(row):
        out.append(_issue(
            "missing_loop_contract",
            "warn",
            "strong recurring/autonomy signal but no acceptance_json.loop_contract or loop_contract_ref",
            "add a compact loop_contract or reference a validated built-in contract",
        ))
    return out


def claim_warning_for_row(row: dict[str, Any]) -> str | None:

    errors = [issue for issue in doctor_row(row) if issue.severity == "error"]
    if not errors:
        return None
    shown = ", ".join(issue.code for issue in errors[:3])
    if len(errors) > 3:
        shown += f", +{len(errors) - 3} more"
    rid = row_id(row)
    return f"loop contract warning: {rid} schema issue(s): {shown}; claim is not blocked"


def iter_backlog_rows(backlog: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for section in ("today_live_jobs", "items"):
        rows = backlog.get(section)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                yield row


def doctor_backlog(backlog: dict[str, Any], *, max_messages: int = 80) -> tuple[list[str], int]:
    messages: list[str] = []
    total = 0
    for row in iter_backlog_rows(backlog):
        rid = row_id(row)
        for issue in doctor_row(row):
            total += 1
            if len(messages) < max_messages:
                messages.append(f"loop contract: {rid} {issue.code}: {issue.message}")
    omitted = max(total - len(messages), 0)
    if omitted:
        messages.append(f"loop contract: {omitted} additional warning(s) omitted; run loop_contract.py doctor/list for details")
    return messages, total


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("intent_state") or "").strip().lower()


def _title(row: dict[str, Any]) -> str:
    return str(row.get("title") or row.get("display") or row_id(row)).strip()


def scan_board_rows(backlog: dict[str, Any], *, limit: int = 25) -> dict[str, Any]:
    max_candidates = max(int(limit), 0)
    rows_scanned = 0
    terminal_rows_skipped = 0
    loop_signal_rows = 0
    rows_with_issues = 0
    issue_total = 0
    severity_counts: Counter[str] = Counter()
    issue_code_counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []

    for row in iter_backlog_rows(backlog):
        rows_scanned += 1
        if _status(row) in TERMINAL_SCAN_STATUSES:
            terminal_rows_skipped += 1
            continue
        has_loop_signal = row_has_loop_signal(row)
        if has_loop_signal:
            loop_signal_rows += 1
        issues = doctor_row(row)
        if not issues:
            continue

        rows_with_issues += 1
        issue_total += len(issues)
        severity_counts.update(issue.severity for issue in issues)
        issue_code_counts.update(issue.code for issue in issues)

        if len(candidates) < max_candidates:
            candidates.append({
                "work_id": row_id(row),
                "status": _status(row),
                "title": _title(row),
                "loop_signal": has_loop_signal,
                "issue_count": len(issues),
                "issue_codes": issue_codes(issues),
                "issues": issues_to_dicts(issues),
            })

    omitted_count = max(rows_with_issues - len(candidates), 0)
    return {
        "schema_version": 1,
        "mode": "loop_doctor_board_report",
        "read_only": True,
        "actions_enabled": False,
        "mutates_coord_state": False,
        "writes_events": False,
        "starts_processes": False,
        "creates_loop_runner": False,
        "bounded": True,
        "limit": max_candidates,
        "counts": {
            "rows_scanned": rows_scanned,
            "terminal_rows_skipped": terminal_rows_skipped,
            "loop_signal_rows": loop_signal_rows,
            "rows_with_issues": rows_with_issues,
            "issue_total": issue_total,
            "issue_codes": dict(sorted(issue_code_counts.items())),
            "severities": dict(sorted(severity_counts.items())),
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "omitted": {
            "candidate_count": omitted_count,
            "reason": "candidate_limit" if omitted_count else None,
        },
        "truncated": omitted_count > 0,
    }


def _tool_risk(tool_name: str) -> str:
    name = str(tool_name or "").strip().lower()
    expensive = any(term in name for term in EXPENSIVE_TOOL_TERMS)
    dangerous = any(term in name for term in DANGEROUS_TOOL_TERMS)
    if expensive and dangerous:
        return "expensive_or_dangerous"
    if dangerous:
        return "dangerous"
    if expensive:
        return "expensive"
    return "ordinary"


def loop_doctor_suggestions_from_run_events(events: Iterable[dict[str, Any]], *, limit: int = 10) -> dict[str, Any]:
    counts: Counter[tuple[str, str]] = Counter()
    sample: dict[tuple[str, str], dict[str, Any]] = {}
    for row in events:
        if str(row.get("category") or "") != "tool":
            continue
        try:
            content = json.loads(str(row.get("content_json") or "{}"))
        except Exception:
            content = {}
        tool_name = str(content.get("tool_name") or content.get("name") or "").strip()
        work_id = str(row.get("work_id") or "").strip()
        if not tool_name or not work_id:
            continue
        key = (work_id, tool_name)
        counts[key] += 1
        sample.setdefault(key, row)
    suggestions = []
    for (work_id, tool_name), count in counts.most_common():
        if count < 3:
            continue
        risk = _tool_risk(tool_name)
        suggestions.append({
            "work_id": work_id,
            "kind": "repeated_tool_pattern",
            "tool_name": tool_name,
            "risk": risk,
            "event_count": count,
            "recommendation": "Consider a manual loop_contract with explicit stop_states and max_iterations.",
            "policy_relevant": risk in {"expensive", "dangerous", "expensive_or_dangerous"},
            "actions_enabled": False,
            "creates_loop_runner": False,
            "mutates_coord_state": False,
            "sample_event_id": sample[(work_id, tool_name)].get("id"),
        })
        if len(suggestions) >= max(0, int(limit)):
            break
    return {
        "schema_version": 1,
        "mode": "loop_doctor_run_event_suggestions",
        "read_only": True,
        "actions_enabled": False,
        "creates_loop_runner": False,
        "mutates_coord_state": False,
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
        "truncated": any(count >= 3 for (_key, count) in counts.most_common()[len(suggestions):]),
    }


def find_backlog_row(backlog: dict[str, Any], roadmap_id: str) -> dict[str, Any] | None:
    for row in iter_backlog_rows(backlog):
        if row_id(row) == roadmap_id:
            return row
    return None


def scaffold_contract(loop_type: str, *, title: str = "Loop contract") -> dict[str, Any]:
    normalized_type = loop_type if loop_type in ALLOWED_LOOP_TYPES else "coord"
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or normalized_type
    loop_id = f"{base[:64].strip('-')}-v1"
    return {
        "schema_version": 1,
        "loop_id": loop_id,
        "loop_type": normalized_type,
        "purpose": f"Run {title} as one bounded feedback cycle.",
        "use_when": "Use when this work is recurring or requires repeated observe/act/verify cycles.",
        "observe": ["observe fresh coord/project state before acting"],
        "choose": ["choose one bounded reversible action"],
        "act": ["perform the chosen action without changing unrelated state"],
        "verify": [f"run {COMPACT_VERIFY_COMMAND}"],
        "record": ["write the done_signal before marking work done"],
        "stop_states": {
            "success": "done_signal exists and verifier gates pass",
            "blocked": "external input or unavailable dependency prevents progress",
            "stagnated": "no measurable improvement after allowed attempts",
        },
        "limits": {"max_iterations": 3, "max_wall_minutes": None, "max_cost_usd": None},
        "approval_gates": [
            "destructive_command",
            "external_send",
            "credential_access",
            "production_deploy",
            "budget_halt_override",
        ],
        "evidence": {
            "required_artifacts": ["done_signal"],
            "required_commands": [COMPACT_VERIFY_COMMAND],
            "independent_review_required": False,
        },
    }


def _priority(row: dict[str, Any]) -> float:

    from coordharness.coord import coord_db

    return coord_db.priority_sort_value(row.get("priority"))


def _rows_by_id(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        rid = row_id(row)
        if rid and rid != "<unknown>":
            out[rid] = row
    return out


def _loop_proposal_output_contract() -> dict[str, bool | str]:
    return {
        "authority": "advisory_only",
        "mutates_coord_state": False,
        "writes_events": False,
        "starts_processes": False,
        "creates_loop_runner": False,
        "auto_applies_contracts": False,
    }


def _supervisor_compatibility(payload: dict[str, Any]) -> dict[str, Any]:
    from coordharness.coord.supervisor_readonly import RANKING_VERSION

    output_contract = payload.get("output_contract") if isinstance(payload.get("output_contract"), dict) else {}
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    missing_rank_fields = sorted({
        field
        for suggestion in suggestions
        if isinstance(suggestion, dict)
        for field in REQUIRED_SUPERVISOR_RANK_FIELDS
        if field not in suggestion
    })

    compatible = True
    reason = "compatible"
    if payload.get("schema_version") != 1:
        compatible = False
        reason = "unsupported_supervisor_schema_version"
    elif payload.get("ranking_version") != RANKING_VERSION:
        compatible = False
        reason = "missing_or_unsupported_ranking_version"
    elif payload.get("actions_enabled") is not False:
        compatible = False
        reason = "supervisor_actions_not_readonly"
    elif output_contract.get("authority") != "advisory_only":
        compatible = False
        reason = "supervisor_authority_not_advisory"
    elif output_contract.get("mutates_coord_state") is not False:
        compatible = False
        reason = "supervisor_can_mutate_coord_state"
    elif missing_rank_fields:
        compatible = False
        reason = "missing_required_rank_fields"

    return {
        "compatible": compatible,
        "decision": LOOP_SUGGESTIONS_COMPAT_DECISION,
        "reason": reason,
        "supervisor_schema_version": payload.get("schema_version"),
        "supervisor_ranking_version": payload.get("ranking_version"),
        "required_ranking_version": RANKING_VERSION,
        "rank_fields_required": True,
        "missing_rank_fields": missing_rank_fields,
    }


def _is_open_for_loop_proposal(row: dict[str, Any]) -> bool:
    from coordharness.coord.supervisor_readonly import OPEN_STATUSES, TERMINAL_STATUSES

    status = _status(row)
    return status in OPEN_STATUSES and status not in TERMINAL_STATUSES


def _is_sparse_legacy_import(row: dict[str, Any]) -> bool:
    return (
        "legacy jobs.db import" in _text(row.get("note")).lower()
        and not _text(row.get("assignee")).strip()
        and not _text(row.get("done_signal")).strip()
        and not _text(row.get("context_pack_ref")).strip()
    )


def _has_missing_loop_contract(row: dict[str, Any]) -> bool:
    return any(issue.code == "missing_loop_contract" for issue in doctor_row(row))


def _proposal_for(
    row: dict[str, Any],
    suggestion: dict[str, Any],
    *,
    source_candidate: str = "supervisor",
) -> dict[str, Any]:
    rid = row_id(row)
    title = _title(row)
    kind = _text(suggestion.get("kind") or "supervisor_suggestion")
    contract = scaffold_contract("coord", title=title)
    contract["purpose"] = f"Run {title} as a bounded advisory coordination loop."
    contract["use_when"] = (
        f"Use when readonly supervisor reports {kind} for {rid} and a human wants a manual loop contract."
    )
    contract["observe"] = [
        "Run coordharness/scripts/supervisor_readonly.py or coordharness/scripts/loop_contract.py suggest for fresh advisory output.",
        f"Run coordharness/scripts/board_context.py focus {rid} before choosing any action.",
    ]
    contract["choose"] = [
        "Choose one bounded manual triage, documentation, proof, or launch-contract repair step.",
        "Stop instead of acting if the proposed step would claim work, launch a service, or change lifecycle truth automatically.",
    ]
    contract["act"] = [
        "Propose or manually merge this loop_contract after review; do not mutate lifecycle state automatically.",
        "Use coord helpers for any later lifecycle mutation outside this proposal report.",
    ]
    contract["verify"] = [
        "Run coordharness/scripts/loop_contract.py validate against the reviewed contract JSON.",
        f"Run {COMPACT_VERIFY_COMMAND} before closing any boarded work.",
    ]
    contract["record"] = [
        "Record the reviewed proposal artifact or done_signal before marking work done.",
        "Keep follow-up coordination updates on the board through codex_coord.py or claude_coord.py helpers.",
    ]
    contract["evidence"] = {
        "required_artifacts": ["done_signal", "loop_contract_review"],
        "required_commands": [
            "coordharness/scripts/loop_contract.py validate <contract-json>",
            COMPACT_VERIFY_COMMAND,
        ],
        "independent_review_required": False,
    }

    issues = validate_loop_contract(contract)
    errors = [issue for issue in issues if issue.severity == "error"]
    return {
        "work_id": rid,
        "title": title,
        "action_policy": "advisory_only",
        "apply_mode": "manual_only",
        "creates_loop_runner": False,
        "source_candidate": source_candidate,
        "source_suggestion_kind": kind,
        "source_supervisor_rank": suggestion.get("rank"),
        "source_supervisor_rank_score": suggestion.get("rank_score"),
        "source_supervisor_rank_reasons": suggestion.get("rank_reasons") or [],
        "source_supervisor_severity": suggestion.get("severity"),
        "source_refs": suggestion.get("refs") or [],
        "candidate_patch": {
            "field": "acceptance_json.loop_contract",
            "operation": "manual_merge_only",
            "value": contract,
        },
        "proposed_loop_contract": contract,
        "contract_validation": {
            "valid": not errors,
            "error_count": len(errors),
            "warning_count": len([issue for issue in issues if issue.severity == "warn"]),
            "issue_codes": issue_codes(issues),
        },
    }


def _loop_doctor_suggestions(
    rows: list[dict[str, Any]] | None,
    existing_work_ids: set[str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for row in rows or []:
        rid = row_id(row)
        if not rid or rid == "<unknown>" or rid in existing_work_ids:
            continue
        if not _is_open_for_loop_proposal(row):
            continue
        if _is_sparse_legacy_import(row):
            continue
        if not row_has_loop_signal(row):
            continue
        if not _has_missing_loop_contract(row):
            continue
        candidates.append(row)

    suggestions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in sorted(candidates, key=lambda item: (_priority(item), row_id(item))):
        rid = row_id(row)
        suggestions.append((
            row,
            {
                "work_id": rid,
                "kind": "loop_doctor_missing_contract",
                "severity": "warn",
                "refs": [f"coord://work/{rid}"],
                "rank": None,
                "rank_score": None,
                "rank_reasons": ["source:loop_doctor", "issue:missing_loop_contract"],
            },
        ))
    return suggestions


def _bounded_omitted(omitted: list[dict[str, Any]], omitted_limit: int) -> tuple[list[dict[str, Any]], int, bool]:
    limit = max(0, int(omitted_limit))
    return omitted[:limit], len(omitted), len(omitted) > limit


def propose_loop_contracts_from_supervisor(
    payload: dict[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
    limit: int = 25,
    omitted_limit: int = DEFAULT_OMITTED_LIMIT,
) -> dict[str, Any]:

    compatibility = _supervisor_compatibility(payload)
    output: dict[str, Any] = {
        "schema_version": 1,
        "mode": LOOP_SUGGESTIONS_MODE,
        "actions_enabled": False,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "type": "supervisor_readonly_payload",
            "supervisor_schema_version": payload.get("schema_version"),
            "supervisor_ranking_version": payload.get("ranking_version"),
            "supervisor_suggestion_count": len(payload.get("suggestions") or []),
            "loop_doctor_candidate_count": 0,
        },
        "compatibility": compatibility,
        "output_contract": _loop_proposal_output_contract(),
        "bounded": True,
        "truncated": False,
        "proposal_limit": max(0, int(limit)),
        "omitted_limit": max(0, int(omitted_limit)),
        "omitted_count": 0,
        "omitted_truncated": False,
        "proposal_count": 0,
        "counts_by_kind": {},
        "omitted": [],
        "proposals": [],
    }
    if not compatibility["compatible"]:
        return output

    row_lookup = _rows_by_id(rows)
    proposals: list[dict[str, Any]] = []
    proposed_work_ids: set[str] = set()
    omitted: list[dict[str, Any]] = []
    for suggestion in payload.get("suggestions") or []:
        if not isinstance(suggestion, dict):
            omitted.append({"reason": "invalid_supervisor_suggestion"})
            continue
        rid = _text(suggestion.get("work_id")).strip()
        row = row_lookup.get(rid)
        if row is None:
            omitted.append({"work_id": rid, "reason": "missing_row_context"})
            continue
        if not row_has_loop_signal(row):
            omitted.append({"work_id": rid, "reason": "row_has_no_loop_signal"})
            continue
        if not _has_missing_loop_contract(row):
            omitted.append({"work_id": rid, "reason": "loop_contract_already_declared"})
            continue
        proposals.append(_proposal_for(row, suggestion))
        proposed_work_ids.add(rid)

    loop_doctor_candidates = _loop_doctor_suggestions(rows, proposed_work_ids)
    output["source"]["loop_doctor_candidate_count"] = len(loop_doctor_candidates)
    for row, suggestion in loop_doctor_candidates:
        proposals.append(_proposal_for(row, suggestion, source_candidate="loop_doctor"))
        proposed_work_ids.add(row_id(row))

    max_items = max(0, int(limit))
    truncated = len(proposals) > max_items
    visible = proposals[:max_items]
    if truncated:
        for proposal in proposals[max_items:]:
            omitted.append({"work_id": proposal.get("work_id"), "reason": "limit_exceeded"})

    counts = Counter(_text(proposal.get("source_suggestion_kind")) for proposal in visible)
    visible_omitted, omitted_count, omitted_truncated = _bounded_omitted(omitted, omitted_limit)
    output["bounded"] = True
    output["truncated"] = truncated
    output["proposal_limit"] = max_items
    output["proposal_count"] = len(visible)
    output["counts_by_kind"] = dict(sorted(counts.items()))
    output["omitted_count"] = omitted_count
    output["omitted_truncated"] = omitted_truncated
    output["omitted"] = visible_omitted
    output["proposals"] = visible
    return output


def propose_loop_contracts_from_rows(
    rows: list[dict[str, Any]],
    *,
    now: float | None = None,
    limit: int = 25,
    supervisor_limit: int | None = None,
    omitted_limit: int = DEFAULT_OMITTED_LIMIT,
    autonomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from coordharness.coord.supervisor_readonly import suggest_from_rows

    source_limit = supervisor_limit if supervisor_limit is not None else max(25, int(limit) * 4)
    supervisor_payload = suggest_from_rows(rows, now=now, limit=source_limit, autonomy=autonomy)
    report = propose_loop_contracts_from_supervisor(
        supervisor_payload,
        rows=rows,
        limit=limit,
        omitted_limit=omitted_limit,
    )
    report["source"] = {
        **report["source"],
        "type": "board_rows_via_supervisor_readonly",
        "row_count": len(rows),
        "supervisor_limit": source_limit,
        "supervisor_ranking_version": supervisor_payload.get("ranking_version"),
    }
    return report


def propose_loop_contracts_from_board(
    *,
    db_path: str | None = None,
    now: float | None = None,
    limit: int = 25,
    supervisor_limit: int | None = None,
    omitted_limit: int = DEFAULT_OMITTED_LIMIT,
    autonomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from coordharness.coord import board_context

    rows = board_context.load_rows(db_path=db_path)
    return propose_loop_contracts_from_rows(
        rows,
        now=now,
        limit=limit,
        supervisor_limit=supervisor_limit,
        omitted_limit=omitted_limit,
        autonomy=autonomy,
    )
