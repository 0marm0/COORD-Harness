
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import base64
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from coordharness.coord.r4_plane_authority import R4PlaneAuthority


SCHEMA_VERSION = "work-query-v2.dark.r1"
RANKING_VERSION = "protected-workstream-rank.r1"
CURSOR_SCHEMA = "work-query-v2-keyset.r1"
R4_SUBJECT_PLANES = frozenset({"product", "harness", "infrastructure", "shared"})
QUARANTINE_PLANES = frozenset({"unknown", "mixed"})
WORK_ACTIVE_STATUSES = frozenset({"running", "claimed"})
CLAIM_HELD_STATUSES = frozenset({"running", "claimed", "paused", "blocked"})
PRIORITY_RANK = {"p0": 0, "p1": 1, "p2": 2, "p3": 3, "p4": 4}

__all__ = [
    "CLAIM_HELD_STATUSES",
    "QUARANTINE_PLANES",
    "R4_SUBJECT_PLANES",
    "RANKING_VERSION",
    "ResponseBudget",
    "SCHEMA_VERSION",
    "WORK_ACTIVE_STATUSES",
    "WorkFilters",
    "canonical_response_bytes",
    "canonical_wire_bytes",
    "expand_workstream_v2",
    "normalize_span",
    "search_history_v2",
    "session_capsule_v2",
    "work_query_v2",
]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _priority(value: Any) -> int:
    if isinstance(value, int):
        return max(0, min(value, 99))
    text = _text(value).strip().lower()
    if text in PRIORITY_RANK:
        return PRIORITY_RANK[text]
    try:
        return max(0, min(int(text), 99))
    except ValueError:
        return 50


def _provided_values(row: Mapping[str, Any], field: str) -> tuple[Any, ...]:

    values: list[Any] = []
    if field in row and row.get(field) not in (None, ""):
        values.append(row.get(field))
    metadata = _metadata(row)
    if field in metadata and metadata.get(field) not in (None, ""):
        values.append(metadata.get(field))
    return tuple(values)


def _subject_plane(
    row: Mapping[str, Any],
    plane_authority: "R4PlaneAuthority | None",
) -> tuple[str, bool, str | None]:

    if plane_authority is None:
        return "unknown", False, "missing_bound_r4_plane_authority"
    from coordharness.coord.r4_plane_authority import R4PlaneAuthority

    if not isinstance(plane_authority, R4PlaneAuthority):
        raise TypeError("plane_authority must be a hash-verified R4PlaneAuthority")

    values = _provided_values(row, "subject_plane")
    authority = plane_authority.resolve(_text(row.get("work_id") or row.get("id")))
    authority_plane = authority.get("subject_plane")
    if any(isinstance(value, (list, tuple, set, frozenset, dict)) for value in values):
        return "mixed", False, "multi_value_r4_subject_plane"
    text_values = tuple(_text(value) for value in values)
    if any(value == "mixed" for value in text_values):
        return "mixed", False, "explicit_mixed_r4_subject_plane"
    if len(set(text_values)) > 1:
        return "mixed", False, "conflicting_exact_r4_subject_plane_sources"
    if authority_plane not in R4_SUBJECT_PLANES:
        return "unknown", False, str(
            authority.get("quarantine_reason") or "missing_adjudicated_r4_authority"
        )
    if text_values and text_values[0] != authority_plane:
        return "mixed", False, "declared_plane_conflicts_with_adjudicated_authority"
    return str(authority_plane), True, None


def _lineage_field(
    row: Mapping[str, Any],
    field: str,
    *,
    fallback: str,
) -> tuple[str, bool, str | None]:
    values = tuple(_text(value) for value in _provided_values(row, field))
    if not values:
        return fallback, False, f"missing_{field}"
    if len(set(values)) > 1:
        return fallback, False, f"conflicting_{field}"
    return values[0], True, None


@dataclass(frozen=True)
class _Span:
    work_id: str
    title: str
    status: str
    priority: int
    assignee: str
    updated_seq: int
    program_id: str
    workstream_id: str
    episode_id: str
    span_id: str
    subject_plane: str
    subject_plane_exact: bool
    lineage_exact: bool
    lineage_reason: str | None
    quarantined: bool
    quarantine_reason: str | None
    pinned: bool
    high_value: bool
    value_score: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee,
            "updated_seq": self.updated_seq,
            "program_id": self.program_id,
            "workstream_id": self.workstream_id,
            "episode_id": self.episode_id,
            "span_id": self.span_id,
            "subject_plane": self.subject_plane,
            "subject_plane_exact": self.subject_plane_exact,
            "lineage_exact": self.lineage_exact,
            "lineage_reason": self.lineage_reason,
            "quarantined": self.quarantined,
            "quarantine_reason": self.quarantine_reason,
            "pinned": self.pinned,
            "high_value": self.high_value,
            "value_score": self.value_score,
        }


def _normalize_span(
    row: Mapping[str, Any],
    plane_authority: "R4PlaneAuthority | None" = None,
) -> _Span:
    work_id = _text(row.get("work_id") or row.get("id"))
    identity = work_id or "anonymous"
    plane, plane_exact, quarantine_reason = _subject_plane(row, plane_authority)

    program_id, program_exact, program_reason = _lineage_field(
        row,
        "program_id",
        fallback=f"__unclassified_program__:{identity}",
    )
    workstream_id, workstream_exact, workstream_reason = _lineage_field(
        row,
        "workstream_id",
        fallback=f"__unclassified_workstream__:{identity}",
    )
    episode_id, episode_exact, episode_reason = _lineage_field(
        row,
        "episode_id",
        fallback=f"__unclassified_episode__:{identity}",
    )
    span_id, _span_exact, _span_reason = _lineage_field(
        row,
        "span_id",
        fallback=work_id or f"__anonymous_span__:{identity}",
    )
    lineage_reasons = [
        reason
        for reason in (program_reason, workstream_reason, episode_reason)
        if reason is not None
    ]
    metadata = _metadata(row)
    value_score = max(
        0,
        min(
            _int(row.get("value_score", metadata.get("value_score")), 0),
            100,
        ),
    )
    pinned = _bool(row.get("pinned", metadata.get("pinned")))
    high_value = (
        _bool(row.get("high_value", metadata.get("high_value")))
        or value_score >= 80
    )
    forced_quarantine = _bool(row.get("authority_quarantined"))
    forced_reason = _text(row.get("authority_quarantine_reason")).strip() or None
    lineage_reason = ";".join(lineage_reasons) or None
    return _Span(
        work_id=work_id,
        title=_text(row.get("title")),
        status=_text(row.get("status") or row.get("intent_state") or "planned").lower(),
        priority=_priority(row.get("priority")),
        assignee=_text(row.get("assignee") or row.get("owner_lane")),
        updated_seq=_int(row.get("updated_seq", row.get("change_seq")), 0),
        program_id=program_id,
        workstream_id=workstream_id,
        episode_id=episode_id,
        span_id=span_id,
        subject_plane=plane,
        subject_plane_exact=plane_exact,
        lineage_exact=program_exact and workstream_exact and episode_exact,
        lineage_reason=lineage_reason,
        quarantined=(
            not plane_exact
            or forced_quarantine
            or (
                _bool(row.get("require_exact_lineage"))
                and not (program_exact and workstream_exact and episode_exact)
            )
        ),
        quarantine_reason=(
            forced_reason
            or quarantine_reason
            or (f"missing_exact_lineage:{lineage_reason}" if lineage_reason else None)
        ),
        pinned=pinned,
        high_value=high_value,
        value_score=value_score,
    )


def normalize_span(
    row: Mapping[str, Any],
    *,
    plane_authority: "R4PlaneAuthority | None" = None,
) -> dict[str, Any]:

    return _normalize_span(row, plane_authority).as_dict()


@dataclass(frozen=True)
class WorkFilters:

    subject_planes: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    assignee: str | None = None
    program_id: str | None = None
    workstream_id: str | None = None
    text_query: str | None = None
    include_quarantined: bool = False

    def __post_init__(self) -> None:
        invalid = set(self.subject_planes) - R4_SUBJECT_PLANES
        if invalid:
            raise ValueError(
                "subject_planes accepts exact R4 planes only; use "
                f"include_quarantined for unknown/mixed: {sorted(invalid)}"
            )


@dataclass(frozen=True)
class ResponseBudget:

    max_wire_bytes: int = 24_576
    max_items: int = 100

    def __post_init__(self) -> None:
        if self.max_wire_bytes < 1_024:
            raise ValueError("max_wire_bytes must leave room for a complete receipt")
        if self.max_items < 0:
            raise ValueError("max_items must be non-negative")


def _apply_cross_span_plane_quarantine(spans: Sequence[_Span]) -> list[_Span]:

    planes_by_lineage: dict[tuple[str, str], set[str]] = defaultdict(set)
    for span in spans:
        if span.subject_plane_exact and span.lineage_exact:
            planes_by_lineage[(span.program_id, span.workstream_id)].add(span.subject_plane)
    mixed_lineages = {
        key for key, planes in planes_by_lineage.items() if len(planes) > 1
    }
    if not mixed_lineages:
        return list(spans)
    return [
        replace(
            span,
            subject_plane="mixed",
            subject_plane_exact=False,
            quarantined=True,
            quarantine_reason="cross_span_workstream_plane_conflict",
        )
        if span.lineage_exact and (span.program_id, span.workstream_id) in mixed_lineages
        else span
        for span in spans
    ]


def _matches(span: _Span, filters: WorkFilters) -> bool:
    if span.quarantined and not filters.include_quarantined:
        return False
    if filters.subject_planes and span.subject_plane not in filters.subject_planes:
        return False
    if filters.statuses and span.status not in filters.statuses:
        return False
    if filters.assignee is not None and span.assignee != filters.assignee:
        return False
    if filters.program_id is not None and span.program_id != filters.program_id:
        return False
    if filters.workstream_id is not None and span.workstream_id != filters.workstream_id:
        return False
    if filters.text_query:
        needle = filters.text_query.casefold()
        haystack = "\n".join(
            (
                span.work_id,
                span.title,
                span.program_id,
                span.workstream_id,
                span.episode_id,
                span.span_id,
            )
        ).casefold()
        if needle not in haystack:
            return False
    return True


@dataclass(frozen=True)
class _Prepared:
    all_spans: tuple[_Span, ...]
    eligible: tuple[_Span, ...]
    quarantine: tuple[_Span, ...]
    plane_authority_sha256: str | None


def _prepare(
    rows: Iterable[Mapping[str, Any]],
    filters: WorkFilters,
    *,
    plane_authority: "R4PlaneAuthority | None" = None,
) -> _Prepared:
    normalized = _apply_cross_span_plane_quarantine(
        tuple(_normalize_span(row, plane_authority) for row in rows)
    )
    quarantine = tuple(span for span in normalized if span.quarantined)
    eligible = tuple(span for span in normalized if _matches(span, filters))
    return _Prepared(
        tuple(normalized),
        eligible,
        quarantine,
        getattr(plane_authority, "source_sha256", None),
    )


def _workstream_rank_tuple(card: Mapping[str, Any]) -> tuple[Any, ...]:

    protected = bool(card["pinned"] or card["active"] or card["blocked"] or card["high_value"])
    return (
        -int(protected),
        -int(bool(card["pinned"])),
        -int(bool(card["active"])),
        -int(bool(card["blocked"])),
        -int(bool(card["high_value"])),
        int(card["priority"]),
        -int(card["value_score"]),
        -int(card["newest_update_seq"]),
        _text(card["subject_plane"]),
        _text(card["program_id"]),
        _text(card["workstream_id"]),
    )


def _aggregate_workstreams(spans: Sequence[_Span]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[_Span]] = defaultdict(list)
    for span in spans:
        grouped[(span.subject_plane, span.program_id, span.workstream_id)].append(span)

    cards: list[dict[str, Any]] = []
    for (plane, program_id, workstream_id), members in grouped.items():
        episodes_by_id: dict[str, list[_Span]] = defaultdict(list)
        for member in members:
            episodes_by_id[member.episode_id].append(member)

        episodes: list[dict[str, Any]] = []
        for episode_id, episode_spans in episodes_by_id.items():
            ordered = sorted(
                episode_spans,
                key=lambda span: (-span.updated_seq, span.span_id, span.work_id),
            )
            episodes.append(
                {
                    "episode_id": episode_id,
                    "collapsed_span": {
                        "span_count": len(ordered),
                        "status_counts": dict(
                            sorted(Counter(span.status for span in ordered).items())
                        ),
                        "oldest_update_seq": min(span.updated_seq for span in ordered),
                        "newest_update_seq": max(span.updated_seq for span in ordered),
                        "sample_span_ids": [span.span_id for span in ordered[:3]],
                    },
                }
            )
        episodes.sort(
            key=lambda episode: (
                -int(episode["collapsed_span"]["newest_update_seq"]),
                _text(episode["episode_id"]),
            )
        )

        pinned = any(span.pinned for span in members)
        active = any(span.status in WORK_ACTIVE_STATUSES for span in members)
        blocked = any(span.status == "blocked" for span in members)
        high_value = any(span.high_value for span in members)
        card: dict[str, Any] = {
            "subject_plane": plane,
            "program_id": program_id,
            "workstream_id": workstream_id,
            "episode_count": len(episodes),
            "span_count": len(members),
            "episodes": episodes,
            "status_counts": dict(
                sorted(Counter(span.status for span in members).items())
            ),
            "pinned": pinned,
            "active": active,
            "blocked": blocked,
            "high_value": high_value,
            "protected": pinned or active or blocked or high_value,
            "priority": min(span.priority for span in members),
            "value_score": max(span.value_score for span in members),
            "newest_update_seq": max(span.updated_seq for span in members),
            "oldest_update_seq": min(span.updated_seq for span in members),
            "lineage_exact": all(span.lineage_exact for span in members),
        }
        card["rank_signature"] = list(_workstream_rank_tuple(card))
        cards.append(card)

    cards.sort(key=_workstream_rank_tuple)
    for rank, card in enumerate(cards, start=1):
        card["rank"] = rank
        card["ranking_version"] = RANKING_VERSION
    return cards


def _program_hierarchy(cards: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for card in cards:
        groups[(_text(card["subject_plane"]), _text(card["program_id"]))].append(card)
    ordered = sorted(
        groups.items(),
        key=lambda item: (
            min(int(card["rank"]) for card in item[1]),
            item[0][0],
            item[0][1],
        ),
    )
    return [
        {
            "subject_plane": key[0],
            "program_id": key[1],
            "workstreams": sorted(
                (dict(card) for card in members),
                key=lambda card: int(card["rank"]),
            ),
        }
        for key, members in ordered
    ]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("continuation cursor base64 is noncanonical")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("continuation cursor base64 is invalid") from exc
    if _b64(raw) != value:
        raise ValueError("continuation cursor base64 is noncanonical")
    return raw


def _encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = _canonical_bytes(dict(payload))
    return f"{_b64(raw)}.{hashlib.sha256(raw).hexdigest()}"


def _decode_cursor(value: str) -> dict[str, Any]:
    try:
        encoded, supplied = value.split(".")
    except ValueError as exc:
        raise ValueError("continuation cursor framing is invalid") from exc
    raw = _unb64(encoded)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), supplied):
        raise ValueError("continuation cursor digest mismatch")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("continuation cursor JSON is invalid") from exc
    if not isinstance(payload, dict) or _canonical_bytes(payload) != raw:
        raise ValueError("continuation cursor JSON is noncanonical")
    return payload


def _filters_digest(filters: WorkFilters) -> str:
    return _digest(
        {
            "subject_planes": list(filters.subject_planes),
            "statuses": list(filters.statuses),
            "assignee": filters.assignee,
            "program_id": filters.program_id,
            "workstream_id": filters.workstream_id,
            "text_query": filters.text_query,
            "include_quarantined": filters.include_quarantined,
        }
    )


def _continuation(
    omitted_count: int,
    source_change_seq: int | None,
    *,
    source_id: str | None = None,
    source_change_seq_canonical: bool = False,
    last_card: Mapping[str, Any] | None = None,
    filters_digest: str | None = None,
) -> dict[str, Any]:
    available = bool(
        omitted_count > 0
        and source_change_seq_canonical
        and source_id
        and source_change_seq is not None
        and last_card is not None
        and filters_digest
    )
    cursor = None
    if available:
        cursor = _encode_cursor(
            {
                "schema": CURSOR_SCHEMA,
                "ranking_version": RANKING_VERSION,
                "source_id": source_id,
                "source_change_seq": source_change_seq,
                "filters_sha256": filters_digest,
                "last_rank_signature": list(last_card["rank_signature"]),
            }
        )
    return {
        "required": omitted_count > 0,
        "available": available,
        "cursor": cursor,
        "omitted_count": omitted_count,
        "reason": (
            None
            if available or omitted_count == 0
            else "canonical_monotonic_source_change_seq_adapter_unavailable"
        ),
        "source_change_seq_observed": source_change_seq,
        "source_change_seq_canonical": source_change_seq_canonical,
        "promotion_gate": {
            "schema": "add monotonic source_change_seq to canonical read snapshot",
            "adapter": "bind one read-only transaction snapshot and expose its fence",
            "keyset": ["rank_signature", "workstream_id"],
            "resume_rule": "reject stale fence and restart; never offset-page",
        },
    }


def _quarantine_summary(quarantine: Sequence[_Span]) -> dict[str, Any]:
    reasons = Counter(span.quarantine_reason or "unspecified" for span in quarantine)
    planes = Counter(span.subject_plane for span in quarantine)
    return {
        "policy": "unknown/mixed are quarantined and never coerced to shared",
        "count": len(quarantine),
        "plane_counts": dict(sorted(planes.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "sample_work_ids": sorted(span.work_id for span in quarantine)[:10],
    }


def _stabilize_envelope(
    payload: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    envelope = {"payload": payload, "receipt": receipt}
    for _ in range(16):
        size = len(_canonical_bytes(envelope))
        if receipt.get("response_bytes") == size and receipt.get("wire_bytes") == size:
            return envelope, size
        receipt["response_bytes"] = size
        receipt["wire_bytes"] = size
    size = len(_canonical_bytes(envelope))
    receipt["response_bytes"] = size
    receipt["wire_bytes"] = size
    return envelope, len(_canonical_bytes(envelope))


def _base_receipt(
    *,
    budget: ResponseBudget,
    source_rows: int,
    candidate_items: int,
    selected_items: int,
    item_cap_omissions: int,
    byte_cap_omissions: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "receipt_version": "complete-wire-receipt.r1",
        "encoding": "canonical-json-utf8",
        "framing": "none",
        "transport_overhead_bytes": 0,
        "max_wire_bytes": budget.max_wire_bytes,
        "max_items": budget.max_items,
        "source_rows_scanned": source_rows,
        "candidate_items_before_cap": candidate_items,
        "selected_items": selected_items,
        "item_cap_omissions": item_cap_omissions,
        "byte_cap_omissions": byte_cap_omissions,
        "payload_bytes": len(_canonical_bytes(payload)),
        "response_bytes": 0,
        "wire_bytes": 0,
        "budget_enforced": True,
    }


def _pack_workstreams(
    cards: Sequence[Mapping[str, Any]],
    *,
    budget: ResponseBudget,
    source_change_seq: int | None,
    source_id: str | None,
    source_change_seq_canonical: bool,
    filters_digest: str,
    prepared: _Prepared,
) -> dict[str, Any]:
    initial_count = min(len(cards), budget.max_items)
    selected = [dict(card) for card in cards[:initial_count]]
    item_omissions = len(cards) - initial_count

    def build() -> tuple[dict[str, Any], int]:
        omitted = len(cards) - len(selected)
        omitted_cards = cards[len(selected) :]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ranking_version": RANKING_VERSION,
            "mode": "work_query_v2",
            "hierarchy": "Program→Workstream→Episode→collapsed Span",
            "r4_plane_authority": {
                "bound": prepared.plane_authority_sha256 is not None,
                "source_sha256": prepared.plane_authority_sha256,
                "raw_row_plane_fields_authoritative": False,
            },
            "programs": _program_hierarchy(selected),
            "quarantine": _quarantine_summary(prepared.quarantine),
            "continuation": _continuation(
                omitted,
                source_change_seq,
                source_id=source_id,
                source_change_seq_canonical=source_change_seq_canonical,
                last_card=selected[-1] if selected else None,
                filters_digest=filters_digest,
            ),
            "protected_overflow": {
                "count": sum(bool(card["protected"]) for card in omitted_cards),
                "sample_workstream_ids": [
                    _text(card["workstream_id"])
                    for card in omitted_cards
                    if card["protected"]
                ][:10],
                "recovery": "exact search_history_v2 or expand_workstream_v2",
            },
            "complete_candidate_digest_sha256": _digest(
                [
                    [card["subject_plane"], card["program_id"], card["workstream_id"]]
                    for card in cards
                ]
            ),
        }
        receipt = _base_receipt(
            budget=budget,
            source_rows=len(prepared.all_spans),
            candidate_items=len(cards),
            selected_items=len(selected),
            item_cap_omissions=item_omissions,
            byte_cap_omissions=initial_count - len(selected),
            payload=payload,
        )
        receipt.update(
            {
                "eligible_spans_before_rank_and_cap": len(prepared.eligible),
                "candidate_workstreams_before_cap": len(cards),
                "selected_workstreams": len(selected),
            }
        )
        return _stabilize_envelope(payload, receipt)

    envelope, size = build()
    while size > budget.max_wire_bytes and selected:
        selected.pop()
        envelope, size = build()
    if size > budget.max_wire_bytes:
        raise ValueError("max_wire_bytes is too small for the complete empty receipt")
    return envelope


def work_query_v2(
    rows: Iterable[Mapping[str, Any]],
    *,
    filters: WorkFilters = WorkFilters(),
    budget: ResponseBudget = ResponseBudget(),
    source_change_seq: int | None = None,
    source_id: str | None = None,
    source_change_seq_canonical: bool = False,
    cursor: str | None = None,
    plane_authority: "R4PlaneAuthority | None" = None,
) -> dict[str, Any]:

    prepared = _prepare(rows, filters, plane_authority=plane_authority)
    cards = _aggregate_workstreams(prepared.eligible)
    digest = _filters_digest(filters)
    if cursor is not None:
        if not source_change_seq_canonical or not source_id or source_change_seq is None:
            raise ValueError("continuation requires a canonical source fence")
        payload = _decode_cursor(cursor)
        if (
            payload.get("schema") != CURSOR_SCHEMA
            or payload.get("ranking_version") != RANKING_VERSION
            or payload.get("source_id") != source_id
            or payload.get("source_change_seq") != source_change_seq
            or payload.get("filters_sha256") != digest
        ):
            raise ValueError("continuation cursor source/filter fence mismatch; restart query")
        last_signature = payload.get("last_rank_signature")
        if not isinstance(last_signature, list):
            raise ValueError("continuation cursor rank signature is invalid")
        try:
            last_key = tuple(last_signature)
            cards = [card for card in cards if tuple(card["rank_signature"]) > last_key]
        except TypeError as exc:
            raise ValueError("continuation cursor rank signature types are invalid") from exc
    return _pack_workstreams(
        cards,
        budget=budget,
        source_change_seq=source_change_seq,
        source_id=source_id,
        source_change_seq_canonical=source_change_seq_canonical,
        filters_digest=digest,
        prepared=prepared,
    )


def _pack_span_results(
    spans: Sequence[_Span],
    *,
    mode: str,
    budget: ResponseBudget,
    source_change_seq: int | None,
    prepared: _Prepared,
    extra_payload: Mapping[str, Any],
) -> dict[str, Any]:
    initial_count = min(len(spans), budget.max_items)
    selected = list(spans[:initial_count])
    item_omissions = len(spans) - initial_count

    def build() -> tuple[dict[str, Any], int]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "r4_plane_authority": {
                "bound": prepared.plane_authority_sha256 is not None,
                "source_sha256": prepared.plane_authority_sha256,
                "raw_row_plane_fields_authoritative": False,
            },
            **dict(extra_payload),
            "source_rows_scanned": len(prepared.all_spans),
            "match_count_before_cap": len(spans),
            "matches": [span.as_dict() for span in selected],
            "complete_match_digest_sha256": _digest(
                [[span.work_id, span.span_id, span.updated_seq] for span in spans]
            ),
            "quarantine": _quarantine_summary(prepared.quarantine),
            "continuation": _continuation(len(spans) - len(selected), source_change_seq),
        }
        receipt = _base_receipt(
            budget=budget,
            source_rows=len(prepared.all_spans),
            candidate_items=len(spans),
            selected_items=len(selected),
            item_cap_omissions=item_omissions,
            byte_cap_omissions=initial_count - len(selected),
            payload=payload,
        )
        receipt["full_source_traversal"] = True
        return _stabilize_envelope(payload, receipt)

    envelope, size = build()
    while size > budget.max_wire_bytes and selected:
        selected.pop()
        envelope, size = build()
    if size > budget.max_wire_bytes:
        raise ValueError("max_wire_bytes is too small for the complete empty receipt")
    return envelope


def search_history_v2(
    rows: Iterable[Mapping[str, Any]],
    *,
    query: str | None = None,
    exact_work_id: str | None = None,
    filters: WorkFilters = WorkFilters(),
    budget: ResponseBudget = ResponseBudget(max_wire_bytes=32_768, max_items=100),
    source_change_seq: int | None = None,
    plane_authority: "R4PlaneAuthority | None" = None,
) -> dict[str, Any]:

    prepared = _prepare(rows, filters, plane_authority=plane_authority)
    needle = query.casefold() if query else None
    matches: list[_Span] = []
    for span in prepared.eligible:
        if exact_work_id is not None and span.work_id != exact_work_id:
            continue
        if needle is not None:
            haystack = "\n".join(
                (
                    span.work_id,
                    span.title,
                    span.program_id,
                    span.workstream_id,
                    span.episode_id,
                    span.span_id,
                )
            ).casefold()
            if needle not in haystack:
                continue
        matches.append(span)
    matches.sort(key=lambda span: (-span.updated_seq, span.work_id, span.span_id))
    return _pack_span_results(
        matches,
        mode="exact_full_history_search",
        budget=budget,
        source_change_seq=source_change_seq,
        prepared=prepared,
        extra_payload={"query": query, "exact_work_id": exact_work_id},
    )


def expand_workstream_v2(
    rows: Iterable[Mapping[str, Any]],
    workstream_id: str,
    *,
    filters: WorkFilters = WorkFilters(),
    budget: ResponseBudget = ResponseBudget(max_wire_bytes=65_536, max_items=500),
    source_change_seq: int | None = None,
    plane_authority: "R4PlaneAuthority | None" = None,
) -> dict[str, Any]:

    prepared = _prepare(rows, filters, plane_authority=plane_authority)
    matches = [span for span in prepared.eligible if span.workstream_id == workstream_id]
    matches.sort(key=lambda span: (span.updated_seq, span.span_id, span.work_id))
    return _pack_span_results(
        matches,
        mode="exact_full_history_workstream_expand",
        budget=budget,
        source_change_seq=source_change_seq,
        prepared=prepared,
        extra_payload={"workstream_id": workstream_id},
    )


def _compact_claim(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": _text(row.get("claim_id") or row.get("work_id")),
        "work_id": _text(row.get("work_id")),
        "step": _text(row.get("step"))[:240],
        "updated_seq": _int(row.get("updated_seq", row.get("change_seq")), 0),
    }


def _compact_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": _int(row.get("event_id"), 0),
        "work_id": _text(row.get("work_id")),
        "kind": _text(row.get("kind")),
        "summary": _text(row.get("summary"))[:240],
    }


def _pack_capsule(
    units: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    actor: str,
    budget: ResponseBudget,
    source_change_seq: int | None,
    input_counts: Mapping[str, int],
    quarantine: Sequence[_Span],
    plane_authority_sha256: str | None,
) -> dict[str, Any]:
    initial_count = min(len(units), budget.max_items)
    selected = list(units[:initial_count])
    item_omissions = len(units) - initial_count

    def build() -> tuple[dict[str, Any], int]:
        sections: dict[str, list[dict[str, Any]]] = {
            "own_claims": [],
            "directed_inbox": [],
            "ranked_work": [],
        }
        for section, item in selected:
            sections[section].append(dict(item))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": "pure_session_capsule",
            "actor": actor,
            "r4_plane_authority": {
                "bound": plane_authority_sha256 is not None,
                "source_sha256": plane_authority_sha256,
                "raw_row_plane_fields_authoritative": False,
            },
            "section_order": ["own_claims", "directed_inbox", "ranked_work"],
            **sections,
            "quarantine": _quarantine_summary(quarantine),
            "continuation": _continuation(len(units) - len(selected), source_change_seq),
            "purity_contract": {
                "function_of_supplied_values_only": True,
                "database_reads": 0,
                "database_writes": 0,
                "filesystem_reads": 0,
                "filesystem_writes": 0,
                "presence_mutations": 0,
                "cursor_mutations": 0,
                "clock_reads": 0,
                "environment_reads": 0,
                "network_calls": 0,
            },
        }
        receipt = _base_receipt(
            budget=budget,
            source_rows=sum(input_counts.values()),
            candidate_items=len(units),
            selected_items=len(selected),
            item_cap_omissions=item_omissions,
            byte_cap_omissions=initial_count - len(selected),
            payload=payload,
        )
        receipt["input_counts"] = dict(input_counts)
        receipt["selected_by_section"] = {
            section: len(values) for section, values in sections.items()
        }
        return _stabilize_envelope(payload, receipt)

    envelope, size = build()
    while size > budget.max_wire_bytes and selected:
        selected.pop()
        envelope, size = build()
    if size > budget.max_wire_bytes:
        raise ValueError("max_wire_bytes is too small for the complete empty receipt")
    return envelope


def session_capsule_v2(
    *,
    actor: str,
    claims: Iterable[Mapping[str, Any]],
    inbox_events: Iterable[Mapping[str, Any]],
    work_rows: Iterable[Mapping[str, Any]],
    budget: ResponseBudget = ResponseBudget(max_wire_bytes=15_360, max_items=30),
    source_change_seq: int | None = None,
    plane_authority: "R4PlaneAuthority | None" = None,
) -> dict[str, Any]:

    if not actor:
        raise ValueError("actor is required")
    claim_rows = tuple(claims)
    event_rows = tuple(inbox_events)
    supplied_work_rows = tuple(work_rows)

    own_claims = [
        _compact_claim(row)
        for row in claim_rows
        if _text(row.get("actor") or row.get("assignee")) == actor
        and _text(row.get("status") or "running").lower() in CLAIM_HELD_STATUSES
    ]
    own_claims.sort(key=lambda row: (-int(row["updated_seq"]), row["claim_id"]))

    directed_inbox = [
        _compact_event(row)
        for row in event_rows
        if _text(row.get("recipient")) == actor and not _bool(row.get("acked"))
    ]
    directed_inbox.sort(key=lambda row: (-int(row["event_id"]), row["work_id"]))

    prepared = _prepare(
        supplied_work_rows,
        WorkFilters(assignee=actor),
        plane_authority=plane_authority,
    )
    ranked_work = _aggregate_workstreams(prepared.eligible)
    units: list[tuple[str, Mapping[str, Any]]] = []
    units.extend(("own_claims", row) for row in own_claims)
    units.extend(("directed_inbox", row) for row in directed_inbox)
    units.extend(("ranked_work", row) for row in ranked_work)
    return _pack_capsule(
        units,
        actor=actor,
        budget=budget,
        source_change_seq=source_change_seq,
        input_counts={
            "claims": len(claim_rows),
            "inbox_events": len(event_rows),
            "work_rows": len(supplied_work_rows),
        },
        quarantine=prepared.quarantine,
        plane_authority_sha256=prepared.plane_authority_sha256,
    )


def canonical_wire_bytes(response: Mapping[str, Any]) -> bytes:

    return _canonical_bytes(response)


def canonical_response_bytes(response: Mapping[str, Any]) -> int:

    return len(canonical_wire_bytes(response))
