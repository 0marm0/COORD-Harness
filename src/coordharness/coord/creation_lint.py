from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .ingest import _resolve_grouping
from .review_tier import (
    RULING_POINTER,
    ReviewTierPolicyError,
    effective_review_tier,
    event_ref_ids,
    is_review_row,
    validate_done_signal_grammar,
    validate_tier_declaration,
)


class CreationLintError(ValueError):

    def __init__(
        self,
        message: str,
        *,
        code: str = "creation_lint_rejected",
        work_id: str | None = None,
        lane: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.work_id = work_id
        self.lane = lane


_DURABLE_ID_RE = re.compile(
    r"^N\d{4}-(CLA|CDX|OP|FABLE)-[A-Z0-9][A-Z0-9-]*$"
)
_CHAT_NUMBERED_OWNER_RE = re.compile(r"-(?:CLA|CDX|OP|FABLE)-\d+-", re.I)
_GENERIC_MODULES = {
    "data",
    "platform",
    "data-platform",
    "data_platform",
    "data platform",
}
_GENERIC_LABELS = {
    "job",
    "task",
    "work",
    "todo",
    "tbd",
    "untitled",
    "session",
    "live session",
    "claude",
    "claude live session",
    "claude - live session",
    "codex",
    "codex live session",
    "codex - live session",
}
_GENERATED_SESSION_LABEL_RE = re.compile(
    r"^(?:claude|codex)[-_\s]*live[-_\s]*(?:session[-_\s]*)?[0-9a-f]{6,16}$",
    re.I,
)
_GENERATED_AGENT_LABEL_RE = re.compile(r"^(?:claude|codex)\s+[0-9a-f]{6,16}$", re.I)
_VALID_SURFACES = {"epic", "job", "task"}
_TERMINAL_STATES = {
    "done",
    "complete",
    "completed",
    "cleared",
    "superseded",
    "archived",
    "cancelled",
    "canceled",
    "failed",
}
_FOLLOWUP_STATES = {"demoted", "followup", "follow-up"}
_HIDDEN_VISIBILITIES = {"hidden", "diagnostic", "internal", "session"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_descriptive_label(value: Any, *, work_id: Any = None) -> bool:
    label = _text(value)
    if not label:
        return False
    if work_id and label.casefold() == _text(work_id).casefold():
        return False
    normalized = re.sub(
        r"\s+",
        " ",
        label.replace("\u2013", "-").replace("\u2014", "-"),
    ).strip()
    folded = normalized.casefold()
    if folded in _GENERIC_LABELS:
        return False
    if _GENERATED_SESSION_LABEL_RE.match(normalized):
        return False
    if _GENERATED_AGENT_LABEL_RE.match(normalized):
        return False
    return True


def _json_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, (dict, list)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return False
        try:
            parsed = json.loads(raw)
        except ValueError:
            return False
        return isinstance(parsed, (dict, list)) and bool(parsed)
    return False


def _acceptance_waived(row: dict[str, Any]) -> bool:
    if _text(row.get("acceptance_waiver") or row.get("acceptance_waiver_reason")):
        return True
    waived = row.get("acceptance_waived")
    if isinstance(waived, bool) and waived:
        return True
    required = row.get("acceptance_required")
    if isinstance(required, bool) and required is False:
        return True
    return _text(required).lower() in {"0", "false", "no", "off"}


def _acceptance_present(row: dict[str, Any]) -> bool:
    return (
        _json_present(row.get("acceptance_json"))
        or _json_present(row.get("acceptance"))
        or _acceptance_waived(row)
    )


def _acceptance_text_fragments(value: Any) -> Iterable[str]:
    if value is None or value is False:
        return
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            yield raw
            return
        if parsed == raw:
            yield raw
        else:
            yield from _acceptance_text_fragments(parsed)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if item not in (None, False, "", [], {}):
                yield str(key).replace("_", " ").replace("-", " ")
            yield from _acceptance_text_fragments(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _acceptance_text_fragments(item)
        return
    yield str(value)


_PASS_TOKEN = r"(?:audit\s+verdict\s+)?pass"
_PASS_NOT_A_TEST = _PASS_TOKEN + r"\b(?!\s+(?:tests?|checks?|suite|lint|verification|gate)\b)"
_PASS_PRODUCER = r"(?:issue|post|provide|return|record|write|submit|render|give)"
_PASS_PRODUCER_NOUN = (
    r"(?:issu(?:e|ing)|post(?:ing)?|provid(?:e|ing)|return(?:ing)?|"
    r"record(?:ing)?|writ(?:e|ing)|submit(?:ting)?|render(?:ing)?|giv(?:e|ing))"
)
_REQUIREMENT = r"(?:must|shall|has\s+to|is\s+required\s+to|needs?\s+to|to)"
_OBLIGATION = r"(?:is\s+)?(?:responsible|accountable)\s+for|(?:is\s+)?(?:tasked|charged)\s+with"
_NEGATED_PASS_PREFIX_RE = re.compile(
    r"\b(?:forbids?|prohibits?|rejects?|disallows?)\b.{0,45}$",
    re.I,
)
_NEGATED_PASS_SUFFIX_RE = re.compile(
    r"^\s*(?:is|are|must\s+be)\s+"
    r"(?:forbidden|prohibited|rejected|disallowed|not\s+(?:allowed|accepted|required))\b",
    re.I,
)


def _matched_pass_rule_is_negated(
    clause: str,
    match: re.Match[str],
) -> bool:
    matched = match.group(0)
    if re.search(r"\b(?:must\s+not|not\s+allowed\s+to)\b", matched, re.I):
        return True
    prefix = clause[max(0, match.start() - 55) : match.start()]
    if _NEGATED_PASS_PREFIX_RE.search(prefix):
        return True
    suffix = clause[match.end() : min(len(clause), match.end() + 45)]
    return _NEGATED_PASS_SUFFIX_RE.search(suffix) is not None


def _required_pass_patterns(lane: str) -> tuple[re.Pattern[str], ...]:
    lane_token = re.escape(lane)
    role = r"(?:assignee|assigned|owner|author)(?:\s+lane)?"
    return (
        re.compile(
            rf"\b(?:self|same\s+lane)\s+{_PASS_TOKEN}\b.{{0,60}}"
            r"\b(?:required|needed|mandatory)\b",
            re.I,
        ),
        re.compile(
            rf"\b(?:requires?|needs?|must\s+have)\b.{{0,60}}"
            rf"\b(?:self|same\s+lane)\s+{_PASS_TOKEN}\b",
            re.I,
        ),
        re.compile(
            rf"\bsame\s+lane\b.{{0,40}}\b(?:must|shall|is\s+required\s+to)\s+"
            rf"{_PASS_NOT_A_TEST}",
            re.I,
        ),
        re.compile(
            rf"\b{_PASS_TOKEN}\b.{{0,40}}\b(?:by|from)\s+(?:the\s+)?same\s+lane\b",
            re.I,
        ),
        re.compile(
            rf"\b{role}\b.{{0,45}}\b{_REQUIREMENT}\s+"
            rf"(?:{_PASS_PRODUCER}\s+)?{_PASS_NOT_A_TEST}",
            re.I,
        ),
        re.compile(
            rf"\b{role}\b.{{0,35}}\b(?:{_OBLIGATION})\s+"
            rf"{_PASS_PRODUCER_NOUN}\s+{_PASS_NOT_A_TEST}",
            re.I,
        ),
        re.compile(
            rf"\b(?:requires?|needs?)\b.{{0,45}}\b{role}\b.{{0,20}}"
            rf"(?:to\s+)?(?:{_PASS_PRODUCER}\s+)?{_PASS_NOT_A_TEST}",
            re.I,
        ),
        re.compile(
            rf"\b{_PASS_TOKEN}\b.{{0,40}}\b(?:by|from)\s+(?:the\s+)?{role}\b",
            re.I,
        ),
        re.compile(
            rf"\b{lane_token}\b(?:\s+(?:lane|reviewer))?.{{0,35}}"
            rf"\b(?:must|shall|is\s+required\s+to)\s+"
            rf"(?:{_PASS_PRODUCER}\s+)?{_PASS_NOT_A_TEST}",
            re.I,
        ),
        re.compile(
            rf"\b{lane_token}\b(?:\s+(?:lane|reviewer))?.{{0,35}}"
            rf"\b(?:{_OBLIGATION})\s+{_PASS_PRODUCER_NOUN}\s+"
            rf"{_PASS_NOT_A_TEST}",
            re.I,
        ),
        re.compile(
            rf"\b{lane_token}(?:\s+lane)?\s+{_PASS_TOKEN}\b.{{0,45}}"
            r"\b(?:required|needed|mandatory)\b",
            re.I,
        ),
        re.compile(
            rf"\b(?:requires?|needs?)\b.{{0,45}}\b{lane_token}"
            rf"(?:\s+lane)?\s+{_PASS_TOKEN}\b",
            re.I,
        ),
        re.compile(
            rf"\b{_PASS_TOKEN}\b.{{0,40}}\b(?:by|from)\s+"
            rf"(?:the\s+)?{lane_token}(?:\s+lane)?\b",
            re.I,
        ),
        re.compile(
            rf"\b{lane_token}[- ]authored\s+(?:audit[_ -]?)?verdict\b"
            rf".{{0,160}}\b{_PASS_TOKEN}\b",
            re.I,
        ),
    )


def acceptance_requires_assignee_pass(acceptance: Any, assignee: Any) -> bool:
    lane = _text(assignee).casefold()
    if lane not in {"claude", "codex"}:
        return False
    patterns = _required_pass_patterns(lane)
    for fragment in _acceptance_text_fragments(acceptance):
        normalized = re.sub(r"[_-]+", " ", fragment)
        for clause in re.split(r"[\n.;]+", normalized):
            for pattern in patterns:
                match = pattern.search(clause)
                if match is None:
                    continue
                if not _matched_pass_rule_is_negated(clause, match):
                    return True
    return False


def expected_owner_prefix(assignee: Any) -> str | None:
    lane = _text(assignee).casefold()
    if "codex" in lane or lane in {"cdx"}:
        return "CDX"
    if "claude" in lane or lane in {"cla"}:
        return "CLA"
    if lane in {"operator", "human", "user"}:
        return "OP"
    return None


def durable_id_actor(work_id: str) -> str | None:
    match = _DURABLE_ID_RE.match(_text(work_id).upper())
    return match.group(1) if match else None


def durable_id_policy_issues(
    work_id: str,
    *,
    assignee: Any = None,
    require_policy_id: bool = True,
) -> list[str]:
    wid = _text(work_id).upper()
    issues: list[str] = []
    if not wid:
        return ["id/work_id"]
    if _CHAT_NUMBERED_OWNER_RE.search(wid):
        issues.append("no chat-number owner in durable ID")
    actor = durable_id_actor(wid)
    if require_policy_id and actor is None:
        issues.append("durable ID prefix NMMDD-CLA|CDX|OP|FABLE")
    return issues


def is_operator_visible_active(row: dict[str, Any]) -> bool:
    visibility = _text(row.get("visibility") or "operator").lower()
    if visibility in _HIDDEN_VISIBILITIES:
        return False
    surface = _text(row.get("surface")).lower()
    if surface in {"epic", "container"}:
        return False
    state = _text(row.get("status") or row.get("intent_state")).lower()
    operator_state = _text(row.get("operator_state")).lower()
    if state in _TERMINAL_STATES or operator_state in _TERMINAL_STATES:
        return False
    tier = _text(row.get("tier")).lower()
    if operator_state in _FOLLOWUP_STATES or tier in _FOLLOWUP_STATES:
        return False
    return True


def row_quality_missing_fields(
    row: dict[str, Any],
    *,
    require_policy_id: bool = False,
) -> list[str]:
    rid = _text(row.get("_row_context_id") or row.get("id") or row.get("work_id"))
    title = _text(row.get("title") or row.get("name"))
    display = _text(row.get("display") or row.get("display_title"))
    module = _text(row.get("module"))
    sublane = _text(row.get("sublane") or row.get("sub"))
    surface = _text(row.get("surface")).lower()
    note = _text(row.get("note") or row.get("why"))
    context_ref = _text(row.get("context_pack_ref"))
    depends_on = row.get("depends_on")
    parent = _text(row.get("parent_id") or row.get("parent"))
    status = _text(row.get("status") or row.get("intent_state")).lower()
    assignee = _text(row.get("assignee"))

    missing: list[str] = []
    missing.extend(
        durable_id_policy_issues(
            rid,
            assignee=assignee,
            require_policy_id=require_policy_id,
        )
    )
    if not is_descriptive_label(title, work_id=rid):
        missing.append("descriptive title")
    if not is_descriptive_label(display, work_id=rid):
        missing.append("descriptive display")
    if not assignee:
        missing.append("assignee")
    if not module or (module.casefold() in _GENERIC_MODULES and not sublane):
        missing.append("specific module")
    if surface not in _VALID_SURFACES:
        missing.append("surface=epic|job|task")
    if surface == "task" and not (parent or depends_on):
        missing.append("task parent_id/depends_on")
    done_signal = _text(row.get("done_signal"))
    if not done_signal:
        missing.append("done_signal")
    else:
        try:
            validate_done_signal_grammar(done_signal)
        except ReviewTierPolicyError:
            missing.append("valid done_signal grammar")
    tier = effective_review_tier(row)
    if tier in {"T0", "T1"} and not _acceptance_present(row):
        missing.append("acceptance_json/acceptance or waiver")
    if not (note or context_ref):
        missing.append("note/why or context_pack_ref")
    if status == "blocked" and not (row.get("blocked_reason_class") or note):
        missing.append("blocked_reason_class or blocker note")
    return missing


def _title_prefix_tokens(value: Any, *, count: int) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", str(value or "").upper())
    return tokens[:count]


def sibling_atomization_warning(
    new_title: Any,
    sibling_titles: Iterable[Any],
    *,
    prefix_tokens: int = 3,
    min_similar_siblings: int = 3,
) -> str | None:
    prefix = _title_prefix_tokens(new_title, count=prefix_tokens)
    if len(prefix) < prefix_tokens:
        return None
    similar = 0
    for title in sibling_titles or []:
        if _title_prefix_tokens(title, count=prefix_tokens) == prefix:
            similar += 1
    if similar < min_similar_siblings:
        return None
    total = similar + 1
    prefix_text = " ".join(prefix)
    return (
        f"anti-atomization: {total} near-identical sibling rows in one hour "
        f"(shared title prefix {prefix_text!r}); prefer ONE JOB with "
        "surface='task' children that roll up, not many top-level rows"
    )


def claim_rubric_missing_fields(row: dict[str, Any]) -> list[str]:
    rid = _text(row.get("_row_context_id") or row.get("id") or row.get("work_id"))
    missing: list[str] = []
    if not is_descriptive_label(row.get("title") or row.get("name"), work_id=rid):
        missing.append("descriptive title")
    if not _text(row.get("done_signal")):
        missing.append("done_signal")
    tier = effective_review_tier(row)
    if tier in {"T0", "T1"} and not _acceptance_present(row):
        missing.append("acceptance_json/acceptance")
    return missing


def validate_creation_policy(
    work_id: str,
    row: dict[str, Any],
    *,
    refs: Iterable[str] | None = None,
    tier_down_authorized: bool = False,
) -> str:
    refs_list = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    try:
        validate_done_signal_grammar(row.get("done_signal"))
        tier = validate_tier_declaration(
            row,
            refs=refs_list,
            tier_down_authorized=tier_down_authorized,
        )
    except ReviewTierPolicyError as exc:
        raise CreationLintError(str(exc)) from exc
    assignee = _text(row.get("assignee")).casefold()
    acceptance = row.get("acceptance_json")
    if acceptance_requires_assignee_pass(acceptance, assignee):
        raise CreationLintError(
            f"acceptance_json requires assignee lane {assignee} to PASS its own row; "
            f"same-lane PASS is unconditionally forbidden. Use opposite-lane T0 review "
            f"or an operator-ok event; see {RULING_POINTER} sec 2",
            code="acceptance_requires_same_lane_pass",
            work_id=work_id,
            lane=assignee,
        )
    if is_review_row(work_id, row):
        if tier != "T0":
            raise CreationLintError(
                f"review-row creation is T0-only; effective tier is {tier}. "
                f"Use a verdict event on the author row; see {RULING_POINTER} sec 2"
            )
        ref_values: list[Any] = [*refs_list, row.get("context_pack_ref"), row.get("depends_on")]
        if not event_ref_ids(ref_values):
            raise CreationLintError(
                f"T0 review-row creation requires a referenced cross-lane audit_request event; "
                f"see {RULING_POINTER} sec 2"
            )
    return tier


def normalize_creation_fields(
    work_id: str,
    fields: dict[str, Any],
    *,
    source: str,
    refs: Iterable[str] | None = None,
    require_done_signal: bool = True,
    require_note_or_context: bool = True,
    require_acceptance: bool = True,
    allow_other_module: bool = False,
    tier_down_authorized: bool = False,
) -> dict[str, Any]:
    wid = _text(work_id)
    if not wid:
        raise CreationLintError(f"{source}: work_id is required")

    out = dict(fields)
    id_issues = durable_id_policy_issues(
        wid,
        assignee=out.get("assignee"),
        require_policy_id=True,
    )
    if id_issues:
        raise CreationLintError(f"{source}:{wid}: missing {', '.join(id_issues)}")

    title = _text(out.get("title") or out.get("display"))
    if not title:
        raise CreationLintError(f"{source}:{wid}: title/display is required")
    if not is_descriptive_label(title, work_id=wid):
        raise CreationLintError(f"{source}:{wid}: descriptive title is required")
    out["title"] = title
    display = _text(out.get("display") or title[:60])
    if not is_descriptive_label(display, work_id=wid):
        raise CreationLintError(f"{source}:{wid}: descriptive display is required")
    out["display"] = display

    if not _text(out.get("assignee")):
        raise CreationLintError(f"{source}:{wid}: assignee/owner lane is required")

    if require_done_signal and not _text(out.get("done_signal")):
        raise CreationLintError(f"{source}:{wid}: done_signal is required for new work rows")

    if require_note_or_context and not (
        _text(out.get("note")) or _text(out.get("context_pack_ref"))
    ):
        raise CreationLintError(f"{source}:{wid}: note or context_pack_ref is required")

    effective_tier = validate_creation_policy(
        wid,
        out,
        refs=refs,
        tier_down_authorized=tier_down_authorized,
    )
    out["tier"] = effective_tier
    if (
        require_acceptance
        and effective_tier in {"T0", "T1"}
        and not _json_present(out.get("acceptance_json"))
    ):
        raise CreationLintError(f"{source}:{wid}: acceptance_json is required")

    explicit_module = _text(out.get("module"))
    explicit_sublane = _text(out.get("sublane") or out.get("sub"))
    if explicit_module.casefold() in _GENERIC_MODULES and not explicit_sublane:
        raise CreationLintError(f"{source}:{wid}: specific module is required")

    out.setdefault("surface", "job")

    probe = {
        "id": wid,
        "title": title,
        "name": title,
        "note": out.get("note") or out.get("context_pack_ref") or "",
        "module": out.get("module"),
        "sublane": out.get("sublane"),
    }
    resolved_module, domain, resolved_sublane = _resolve_grouping(wid, probe)
    if explicit_module:
        module = explicit_module
        sublane = explicit_sublane or (
            resolved_sublane if resolved_module == explicit_module else None
        )
        if resolved_module != explicit_module:
            domain = None
    else:
        module = resolved_module
        sublane = resolved_sublane
    if not module:
        raise CreationLintError(f"{source}:{wid}: module could not be resolved")
    if module == "other" and not allow_other_module:
        raise CreationLintError(
            f"{source}:{wid}: module resolved to other; pass a specific module/sublane"
        )

    out["module"] = module
    if domain:
        out["domain"] = domain
    if sublane:
        out["sublane"] = sublane

    missing = row_quality_missing_fields(
        {"id": wid, **out},
        require_policy_id=True,
    )
    if missing:
        raise CreationLintError(f"{source}:{wid}: missing {', '.join(missing)}")
    return out


_HANDOFF_CAPSULE_WARN_BYTES = 2048
_HANDOFF_REF_BODY_CHAR_THRESHOLD = 300


def handoff_capsule_warnings(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return warnings

    title = payload.get("title")
    task = payload.get("task")
    why = payload.get("why")
    acceptance = payload.get("acceptance")
    constraints = payload.get("constraints") or []
    refs = payload.get("refs") or []
    if not isinstance(constraints, (list, tuple)):
        constraints = [constraints]
    if not isinstance(refs, (list, tuple)):
        refs = [refs]
    constraints = list(constraints)
    refs = list(refs)

    capsule = {
        "task": task,
        "why": why,
        "acceptance": acceptance,
        "constraints": constraints,
        "refs": refs,
    }
    try:
        serialized = json.dumps(capsule, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(capsule)
    total_bytes = len(serialized.encode("utf-8"))
    if total_bytes > _HANDOFF_CAPSULE_WARN_BYTES:
        warnings.append(
            f"handoff capsule oversized ({total_bytes / 1024:.1f}KB): "
            "move detail to a report and pass pointers"
        )

    for ref in refs:
        ref_text = _text(ref)
        if not ref_text:
            continue
        if len(ref_text) > _HANDOFF_REF_BODY_CHAR_THRESHOLD or "\n" in ref_text:
            warnings.append(
                "handoff ref looks like an inlined body, not a pointer "
                f"({len(ref_text)} chars): {ref_text[:60]!r}..."
            )

    if not _text(acceptance):
        warnings.append(
            "handoff missing acceptance: a capsule needs acceptance + refs"
        )
    if not any(_text(ref) for ref in refs):
        warnings.append(
            "handoff missing refs: a capsule needs acceptance + refs"
        )

    body_one_liner = task if _text(task) else (payload.get("body") or payload.get("note"))
    if not _text(title) and not _text(body_one_liner):
        warnings.append(
            "decision-blind event: give it a one-line title -- the board's "
            "decision history is built from titles"
        )

    return warnings
