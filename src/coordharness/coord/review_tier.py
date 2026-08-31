from __future__ import annotations

import json
from pathlib import PurePosixPath
import re

from .config import lane_set as _lane_set
from typing import Any, Iterable


RULING_POINTER = "docs/review-tiers.md"
VALID_REVIEW_TIERS = frozenset({"T0", "T1", "T2"})

_COORD_EVENT_DONE_SIGNAL_RE = re.compile(r"^coord:event:([1-9][0-9]*)$")
_EVENT_REF_RE = re.compile(r"(?:^|[^A-Za-z0-9_])(?:coord:)?event:([1-9][0-9]*)")
_REVIEW_ID_RE = re.compile(r"-REVIEW(?:-R[0-9]+)?$", re.IGNORECASE)

_T0_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("verified_output", re.compile(r"(?:^|[/_.-])verified[_-]?output(?:[/_.-]|$)", re.I)),
    ("served_number", re.compile(r"\bserved (?:number|metric|score|probability|value)s?\b", re.I)),
    ("served_config", re.compile(r"\bserved[-_ ]config(?:uration)?\b", re.I)),
    ("serving_query", re.compile(r"\bserving[-_ ]quer(?:y|ies)\b", re.I)),
    ("activation", re.compile(r"\b(?:activate|activation|cutover|go[-_ ]live)\b", re.I)),
    ("model_promotion", re.compile(r"\b(?:model[-_ ]promotion|promote model|promotion receipt)\b", re.I)),
    ("render_authority", re.compile(r"\b(?:render[-_ ]authority|external[-_ ]render)\b", re.I)),
    ("ground_truth", re.compile(r"\bground[-_ ]truth\b", re.I)),
    ("label_authority", re.compile(r"\b(?:label[-_ ]registry|label[-_ ]authority|canonical labels?)\b", re.I)),
    ("external_publication", re.compile(r"\b(?:external publication|publish externally|public release)\b", re.I)),
    ("irreversible", re.compile(r"\birreversible\b", re.I)),
    ("cross_lane_config", re.compile(r"\bcross[-_ ]lane (?:config|configuration)\b", re.I)),
)

_T0_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("verified_output_path", re.compile(r"(?:^|/)verified_output(?:/|$)", re.I)),
    ("served_config_path", re.compile(r"(?:^|/)(?:served_config|serving_queries)(?:/|$)", re.I)),
    ("promotion_path", re.compile(r"(?:^|/)(?:model_promotion|promotion_receipts?)(?:/|$)", re.I)),
    ("activation_path", re.compile(r"(?:^|/)[^/]*(?:activation|cutover)[^/]*(?:/|$)", re.I)),
    ("ground_truth_path", re.compile(r"(?:^|/)(?:ground_truth|label_registry)(?:/|$)", re.I)),
    ("external_publish_path", re.compile(r"(?:^|/)(?:external_publish|publication)(?:/|$)", re.I)),
)


class ReviewTierPolicyError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _flatten_text(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return
        yield raw
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return
        if parsed != raw:
            yield from _flatten_text(parsed)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _flatten_text(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_text(item)
        return
    yield str(value)


def normalize_declared_tier(value: Any, *, strict: bool = False) -> str | None:
    raw = _text(value).upper()
    if not raw:
        return None
    if raw in VALID_REVIEW_TIERS:
        return raw
    if strict:
        raise ReviewTierPolicyError(
            f"review tier must be T0|T1|T2, got {value!r}; see {RULING_POINTER} sec 2 Tiering"
        )
    return None


def t0_predicate_reasons(
    row: dict[str, Any],
    *,
    refs: Iterable[str] | None = None,
) -> list[str]:
    prose_parts: list[str] = []
    for key in ("title", "acceptance_json", "acceptance", "kind", "module", "sublane"):
        prose_parts.extend(_flatten_text(row.get(key)))
    prose_parts.extend(str(ref) for ref in (refs or []) if str(ref).strip())
    prose_parts.extend(_flatten_text(row.get("context_pack_ref")))
    prose_parts.extend(_flatten_text(row.get("depends_on")))
    prose_parts.extend(_flatten_text(row.get("done_signal")))
    subject = "\n".join(prose_parts)

    reasons: list[str] = []
    for code, pattern in _T0_PATTERNS:
        if pattern.search(subject):
            reasons.append(code)

    path_parts: list[str] = []
    for key in ("done_signal", "context_pack_ref", "depends_on"):
        path_parts.extend(_flatten_text(row.get(key)))
    path_parts.extend(str(ref) for ref in (refs or []) if str(ref).strip())
    for raw_path in path_parts:
        normalized = raw_path.replace("\\", "/").casefold()
        for code, pattern in _T0_PATH_PATTERNS:
            if pattern.search(normalized) and code not in reasons:
                reasons.append(code)
    return reasons


def effective_review_tier(
    row: dict[str, Any],
    *,
    refs: Iterable[str] | None = None,
    tier_down_authorized: bool = False,
) -> str:
    declared = normalize_declared_tier(row.get("tier"))
    reasons = t0_predicate_reasons(row, refs=refs)
    if reasons:
        if declared in {"T1", "T2"} and tier_down_authorized:
            return declared
        return "T0"
    return declared or "T1"


def validate_tier_declaration(
    row: dict[str, Any],
    *,
    refs: Iterable[str] | None = None,
    tier_down_authorized: bool = False,
) -> str:
    declared = normalize_declared_tier(row.get("tier"), strict=bool(_text(row.get("tier"))))
    reasons = t0_predicate_reasons(row, refs=refs)
    if reasons and declared in {"T1", "T2"} and not tier_down_authorized:
        raise ReviewTierPolicyError(
            f"{declared} is below the T0 effect floor ({', '.join(reasons)}); "
            f"tier-down requires a referenced opposite-lane ack or operator event; "
            f"see {RULING_POINTER} sec 2 Tiering"
        )
    return effective_review_tier(
        row,
        refs=refs,
        tier_down_authorized=tier_down_authorized,
    )


def is_review_row(work_id: Any, row: dict[str, Any] | None = None) -> bool:
    wid = _text(work_id)
    kind = _text((row or {}).get("kind")).casefold()
    return bool(_REVIEW_ID_RE.search(wid)) or kind in {
        "review",
        "review_row",
        "audit_review",
    }


def event_ref_ids(values: Iterable[Any]) -> list[int]:
    found: list[int] = []
    for value in values:
        for text in _flatten_text(value):
            for match in _EVENT_REF_RE.finditer(text):
                event_id = int(match.group(1))
                if event_id not in found:
                    found.append(event_id)
    return found


def coord_event_done_signal_id(value: Any) -> int | None:
    match = _COORD_EVENT_DONE_SIGNAL_RE.fullmatch(_text(value))
    return int(match.group(1)) if match else None


def validate_done_signal_grammar(value: Any) -> str:
    signal = _text(value)
    if not signal:
        raise ReviewTierPolicyError(
            f"done_signal is required; use a repo-relative file path or coord:event:<numeric-id>; "
            f"see {RULING_POINTER} sec 2"
        )
    if _COORD_EVENT_DONE_SIGNAL_RE.fullmatch(signal):
        return signal
    if signal.startswith(("coord:", "file:", "http:", "https:", "memory:", "kfts:", "git:", "sha256:")):
        raise ReviewTierPolicyError(
            f"invalid done_signal {signal!r}; URI/pointer schemes are not completion proofs. "
            f"Use a repo-relative file path or coord:event:<numeric-id>; see {RULING_POINTER} sec 2"
        )
    path = PurePosixPath(signal.replace("\\", "/"))
    if path.is_absolute() or signal.startswith(("~", "\\")) or ".." in path.parts:
        raise ReviewTierPolicyError(
            f"invalid done_signal {signal!r}; paths must be repo-relative and traversal-free; "
            f"see {RULING_POINTER} sec 2"
        )
    if any(part in {"", "."} for part in path.parts):
        raise ReviewTierPolicyError(
            f"invalid done_signal {signal!r}; use a normalized repo-relative file path; "
            f"see {RULING_POINTER} sec 2"
        )
    return signal


def acceptance_requires_self_pass(acceptance: Any, assignee: Any) -> bool:
    lane = _text(assignee).casefold()
    if lane not in _lane_set():
        return False
    text = " ".join(_flatten_text(acceptance)).casefold()
    if not text or "pass" not in text or lane not in text:
        return False
    lane_token = re.escape(lane)
    requirement = r"(?:must|required?|requires?|post|provide|issue|return|exactly one)"
    pass_token = r"(?:audit[_ -]?verdict\s+)?pass"
    patterns = (
        re.compile(rf"{requirement}.{{0,180}}\b{lane_token}\b.{{0,180}}\b{pass_token}\b", re.I),
        re.compile(rf"\b{lane_token}(?:[- ]authored|[- ]lane| reviewer)\b.{{0,180}}\b{pass_token}\b", re.I),
        re.compile(rf"\b{pass_token}\b.{{0,180}}\b(?:by|from)\s+{lane_token}\b", re.I),
        re.compile(
            rf"\b{lane_token}\b\s+(?:must|shall|posts?|provides?|issues?|returns?)"
            rf"\b.{{0,120}}\b{pass_token}\b",
            re.I,
        ),
    )
    return any(pattern.search(text) for pattern in patterns)
