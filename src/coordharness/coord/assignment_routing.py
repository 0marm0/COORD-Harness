"""Pure, evidence-bound planning for assignment routing.

The planner in this module is deliberately separated from every lifecycle
writer.  It accepts immutable snapshots, evaluates hard exclusions, and
returns an expiring recommendation receipt.  It never opens a database,
claims work, starts a run, or applies the recommendation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ActiveAssignmentHeadV1",
    "CandidateRouteV1",
    "LaneSnapshotV1",
    "LiveOwnershipV1",
    "RoutePlanV1",
    "RoutePlanningError",
    "RoutingPolicyV1",
    "UsageSnapshotV1",
    "WorkSnapshotV1",
    "canonical_sha256",
    "plan_assignment_route",
]


SCHEMA_VERSION = "RoutePlanV1"
_SCORE_COMPONENTS = ("available_capacity", "continuity", "usage_headroom")
_DEFAULT_WEIGHTS = (
    ("available_capacity", 0.35),
    ("continuity", 0.10),
    ("usage_headroom", 0.55),
)


class RoutePlanningError(ValueError):
    """Raised when an input is malformed rather than merely ineligible."""


def _clean(value: Any, label: str, *, allow_empty: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if not text and not allow_empty:
        raise RoutePlanningError(f"{label} must be a non-empty string")
    return text


def _clean_lane(value: Any, label: str = "lane") -> str:
    return _clean(value, label).lower()


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({_clean(value, "list item") for value in values}))


def _utc(value: str, label: str) -> datetime:
    raw = _clean(value, label)
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise RoutePlanningError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RoutePlanningError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _finite_nonnegative(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RoutePlanningError(f"{label} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise RoutePlanningError(f"{label} must be a finite non-negative number")
    return number


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of the public canonical-JSON representation."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    """Recursively freeze one JSON-shaped receipt value."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return a detached JSON-shaped copy of one frozen receipt value."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class LiveOwnershipV1:
    """A live claim or run owner carried in the caller's work snapshot."""

    lane: str
    identity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane", _clean_lane(self.lane))
        object.__setattr__(self, "identity", _clean(self.identity, "ownership identity"))

    def as_dict(self) -> dict[str, str]:
        return {"lane": self.lane, "identity": self.identity}


@dataclass(frozen=True)
class WorkSnapshotV1:
    """The exact work facts needed by routing; it is not a live row handle."""

    work_id: str
    version: int
    assignee: str | None = None
    review_tier: str = "T1"
    author_lane: str | None = None
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    unresolved_dependencies: tuple[str, ...] = field(default_factory=tuple)
    allowed_assignees: tuple[str, ...] = field(default_factory=tuple)
    live_claim: LiveOwnershipV1 | None = None
    live_run: LiveOwnershipV1 | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_id", _clean(self.work_id, "work_id"))
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise RoutePlanningError("work version must be a non-negative integer")
        assignee = _clean_lane(self.assignee, "assignee") if self.assignee else None
        author = _clean_lane(self.author_lane, "author_lane") if self.author_lane else None
        tier = _clean(self.review_tier, "review_tier").upper()
        if tier not in {"T0", "T1", "T2"}:
            raise RoutePlanningError("review_tier must be T0, T1, or T2")
        object.__setattr__(self, "assignee", assignee)
        object.__setattr__(self, "author_lane", author)
        object.__setattr__(self, "review_tier", tier)
        object.__setattr__(
            self, "required_capabilities", _unique_strings(self.required_capabilities)
        )
        object.__setattr__(
            self, "unresolved_dependencies", _unique_strings(self.unresolved_dependencies)
        )
        object.__setattr__(
            self,
            "allowed_assignees",
            tuple(
                sorted({_clean_lane(value, "allowed assignee") for value in self.allowed_assignees})
            ),
        )
        if self.live_claim is not None and not isinstance(self.live_claim, LiveOwnershipV1):
            raise RoutePlanningError("live_claim must be LiveOwnershipV1 or None")
        if self.live_run is not None and not isinstance(self.live_run, LiveOwnershipV1):
            raise RoutePlanningError("live_run must be LiveOwnershipV1 or None")

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "version": self.version,
            "assignee": self.assignee,
            "review_tier": self.review_tier,
            "author_lane": self.author_lane,
            "required_capabilities": list(self.required_capabilities),
            "unresolved_dependencies": list(self.unresolved_dependencies),
            "allowed_assignees": list(self.allowed_assignees),
            "live_claim": self.live_claim.as_dict() if self.live_claim else None,
            "live_run": self.live_run.as_dict() if self.live_run else None,
        }


@dataclass(frozen=True)
class ActiveAssignmentHeadV1:
    event_id: int
    lane: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.event_id, bool)
            or not isinstance(self.event_id, int)
            or self.event_id < 1
        ):
            raise RoutePlanningError("assignment head event_id must be a positive integer")
        object.__setattr__(self, "lane", _clean_lane(self.lane))

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "lane": self.lane}


@dataclass(frozen=True)
class LaneSnapshotV1:
    lane: str
    available: bool
    auth_available: bool
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    active_load: float = 0.0
    load_capacity: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane", _clean_lane(self.lane))
        if not isinstance(self.available, bool) or not isinstance(self.auth_available, bool):
            raise RoutePlanningError("lane availability and auth_available must be booleans")
        object.__setattr__(self, "capabilities", _unique_strings(self.capabilities))
        load = _finite_nonnegative(self.active_load, "active_load")
        capacity = _finite_nonnegative(self.load_capacity, "load_capacity")
        if capacity <= 0:
            raise RoutePlanningError("load_capacity must be greater than zero")
        object.__setattr__(self, "active_load", load)
        object.__setattr__(self, "load_capacity", capacity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "available": self.available,
            "auth_available": self.auth_available,
            "capabilities": list(self.capabilities),
            "active_load": self.active_load,
            "load_capacity": self.load_capacity,
        }


@dataclass(frozen=True)
class UsageSnapshotV1:
    lane: str
    headroom: float
    limit: float
    coverage_complete: bool
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane", _clean_lane(self.lane))
        object.__setattr__(self, "headroom", _finite_nonnegative(self.headroom, "headroom"))
        limit = _finite_nonnegative(self.limit, "limit")
        if limit <= 0:
            raise RoutePlanningError("usage limit must be greater than zero")
        object.__setattr__(self, "limit", limit)
        if not isinstance(self.coverage_complete, bool):
            raise RoutePlanningError("coverage_complete must be a boolean")
        object.__setattr__(self, "observed_at", _iso(_utc(self.observed_at, "usage observed_at")))

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "headroom": self.headroom,
            "limit": self.limit,
            "coverage_complete": self.coverage_complete,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class RoutingPolicyV1:
    version: str
    sha256: str
    max_usage_age_seconds: float
    weights: tuple[tuple[str, float], ...] = _DEFAULT_WEIGHTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _clean(self.version, "policy version"))
        digest = _clean(self.sha256, "policy sha256").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RoutePlanningError("policy sha256 must be 64 lowercase hexadecimal characters")
        object.__setattr__(self, "sha256", digest)
        age = _finite_nonnegative(self.max_usage_age_seconds, "max_usage_age_seconds")
        object.__setattr__(self, "max_usage_age_seconds", age)
        normalized: dict[str, float] = {}
        for name, value in self.weights:
            clean_name = _clean(name, "score component name")
            if clean_name in normalized:
                raise RoutePlanningError(f"duplicate score component weight: {clean_name}")
            normalized[clean_name] = _finite_nonnegative(value, f"weight {clean_name}")
        if set(normalized) != set(_SCORE_COMPONENTS):
            raise RoutePlanningError(
                "policy weights must define available_capacity, continuity, and usage_headroom"
            )
        total = sum(normalized.values())
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise RoutePlanningError("policy weights must sum to 1.0")
        object.__setattr__(self, "weights", tuple(sorted(normalized.items())))

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "max_usage_age_seconds": self.max_usage_age_seconds,
            "weights": dict(self.weights),
        }


@dataclass(frozen=True)
class CandidateRouteV1:
    lane: str
    eligible: bool
    exclusions: tuple[str, ...]
    score_components: Mapping[str, Mapping[str, float]]
    total_score: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane", _clean_lane(self.lane))
        if not isinstance(self.eligible, bool):
            raise RoutePlanningError("candidate eligible must be a boolean")
        object.__setattr__(self, "exclusions", _unique_strings(self.exclusions))
        object.__setattr__(self, "score_components", _freeze(self.score_components))
        if self.total_score is not None:
            score = _finite_nonnegative(self.total_score, "candidate total_score")
            object.__setattr__(self, "total_score", score)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "eligible": self.eligible,
            "exclusions": list(self.exclusions),
            "score_components": _thaw(self.score_components),
            "total_score": self.total_score,
        }


@dataclass(frozen=True)
class RoutePlanV1:
    work_id: str
    selected_lane: str | None
    status: str
    reason_codes: tuple[str, ...]
    candidates: tuple[CandidateRouteV1, ...]
    confidence: float
    confidence_reason_codes: tuple[str, ...]
    mutation_allowed: bool
    work_fence: Mapping[str, Any]
    policy: Mapping[str, Any]
    evidence: Mapping[str, Any]
    planned_at: str
    plan_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_fence", _freeze(self.work_fence))
        object.__setattr__(self, "policy", _freeze(self.policy))
        object.__setattr__(self, "evidence", _freeze(self.evidence))
        if self.mutation_allowed is not False:
            raise RoutePlanningError("RoutePlanV1 mutation_allowed must be false")
        digest = _clean(self.plan_sha256, "plan_sha256").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RoutePlanningError("plan_sha256 must be 64 lowercase hexadecimal characters")
        object.__setattr__(self, "plan_sha256", digest)
        document = self.as_dict()
        document.pop("plan_sha256")
        if canonical_sha256(document) != digest:
            raise RoutePlanningError("RoutePlanV1 receipt hash does not match its contents")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "work_id": self.work_id,
            "selected_lane": self.selected_lane,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "confidence": self.confidence,
            "confidence_reason_codes": list(self.confidence_reason_codes),
            "mutation_allowed": self.mutation_allowed,
            "work_fence": _thaw(self.work_fence),
            "policy": _thaw(self.policy),
            "evidence": _thaw(self.evidence),
            "planned_at": self.planned_at,
            "plan_sha256": self.plan_sha256,
        }


def _by_lane(values: Sequence[Any], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        lane = value.lane
        if lane in result:
            raise RoutePlanningError(f"duplicate {label} for lane {lane!r}")
        result[lane] = value
    return result


def _normalized_heads(
    heads: Sequence[ActiveAssignmentHeadV1],
) -> tuple[ActiveAssignmentHeadV1, ...]:
    by_event: dict[int, ActiveAssignmentHeadV1] = {}
    for head in heads:
        if not isinstance(head, ActiveAssignmentHeadV1):
            raise RoutePlanningError("assignment heads must be ActiveAssignmentHeadV1 values")
        existing = by_event.get(head.event_id)
        if existing is not None and existing != head:
            raise RoutePlanningError("one assignment head event_id cannot identify two lanes")
        by_event[head.event_id] = head
    return tuple(by_event[event_id] for event_id in sorted(by_event))


def _score(
    lane: LaneSnapshotV1,
    usage: UsageSnapshotV1,
    *,
    has_continuity: bool,
    policy: RoutingPolicyV1,
) -> tuple[dict[str, dict[str, float]], float]:
    raw = {
        "available_capacity": max(0.0, min(1.0, 1.0 - lane.active_load / lane.load_capacity)),
        "continuity": 1.0 if has_continuity else 0.0,
        "usage_headroom": max(0.0, min(1.0, usage.headroom / usage.limit)),
    }
    weights = dict(policy.weights)
    components = {
        name: {
            "value": round(raw[name], 12),
            "weight": round(weights[name], 12),
            "contribution": round(raw[name] * weights[name], 12),
        }
        for name in sorted(raw)
    }
    return components, round(sum(value["contribution"] for value in components.values()), 12)


def plan_assignment_route(
    *,
    work: WorkSnapshotV1,
    assignment_heads: Sequence[ActiveAssignmentHeadV1],
    lanes: Sequence[LaneSnapshotV1],
    usage: Sequence[UsageSnapshotV1],
    policy: RoutingPolicyV1,
    evidence_observed_at: str,
    now: str,
    expires_at: str,
    explicit_user_route: str | None = None,
) -> RoutePlanV1:
    """Build one deterministic, expiring, non-mutating assignment plan.

    All timestamps are caller-supplied.  This keeps replay deterministic and
    prevents a read-only planner from hiding an ambient clock dependency.
    Candidates that fail any hard gate receive no score.
    """

    if not isinstance(work, WorkSnapshotV1):
        raise RoutePlanningError("work must be WorkSnapshotV1")
    if not isinstance(policy, RoutingPolicyV1):
        raise RoutePlanningError("policy must be RoutingPolicyV1")
    normalized_heads = _normalized_heads(tuple(assignment_heads))
    lane_by_name = _by_lane(tuple(lanes), "lane snapshot")
    usage_by_lane = _by_lane(tuple(usage), "usage snapshot")
    if not lane_by_name:
        raise RoutePlanningError("at least one lane snapshot is required")
    if any(not isinstance(value, LaneSnapshotV1) for value in lane_by_name.values()):
        raise RoutePlanningError("lanes must be LaneSnapshotV1 values")
    if any(not isinstance(value, UsageSnapshotV1) for value in usage_by_lane.values()):
        raise RoutePlanningError("usage must be UsageSnapshotV1 values")

    observed_dt = _utc(evidence_observed_at, "evidence_observed_at")
    now_dt = _utc(now, "now")
    expires_dt = _utc(expires_at, "expires_at")
    if expires_dt <= observed_dt:
        raise RoutePlanningError("expires_at must be later than evidence_observed_at")
    usage_observation_times = [
        _utc(item.observed_at, "usage observed_at") for item in usage_by_lane.values()
    ]
    if any(item > observed_dt for item in usage_observation_times):
        raise RoutePlanningError(
            "evidence_observed_at must be at or after every usage observation"
        )
    freshness_deadlines = [
        observed_dt + timedelta(seconds=policy.max_usage_age_seconds),
        *(
            item + timedelta(seconds=policy.max_usage_age_seconds)
            for item in usage_observation_times
            if item + timedelta(seconds=policy.max_usage_age_seconds) > now_dt
        ),
    ]
    if expires_dt > min(freshness_deadlines):
        raise RoutePlanningError(
            "expires_at exceeds the decision evidence freshness deadline"
        )
    observed = _iso(observed_dt)
    planned = _iso(now_dt)
    expires = _iso(expires_dt)
    requested = (
        _clean_lane(explicit_user_route, "explicit_user_route") if explicit_user_route else None
    )

    policy_material = {
        "version": policy.version,
        "max_usage_age_seconds": policy.max_usage_age_seconds,
        "weights": dict(policy.weights),
    }
    effective_policy_sha256 = canonical_sha256(policy_material)
    evidence_material = {
        "work": work.as_dict(),
        "assignment_heads": [head.as_dict() for head in normalized_heads],
        "lanes": [lane_by_name[name].as_dict() for name in sorted(lane_by_name)],
        "usage": [usage_by_lane[name].as_dict() for name in sorted(usage_by_lane)],
        "policy": policy_material,
        "observed_at": observed,
    }
    evidence_sha256 = canonical_sha256(evidence_material)
    head_ids = [head.event_id for head in normalized_heads]
    head_lanes = {head.lane for head in normalized_heads}
    evidence_expired = now_dt >= expires_dt
    evidence_future = observed_dt > now_dt

    candidates: list[CandidateRouteV1] = []
    for lane_name in sorted(lane_by_name):
        lane = lane_by_name[lane_name]
        lane_usage = usage_by_lane.get(lane_name)
        exclusions: set[str] = set()

        # Hard exclusions are all evaluated before any score is produced.
        if requested is not None and lane_name != requested:
            exclusions.add("explicit_user_route_binding")
        if not lane.available:
            exclusions.add("lane_unavailable")
        if not lane.auth_available:
            exclusions.add("auth_unavailable")
        if not set(work.required_capabilities).issubset(lane.capabilities):
            exclusions.add("required_capability_unavailable")
        if work.review_tier == "T0":
            if work.author_lane is None:
                exclusions.add("t0_author_identity_missing")
            elif lane_name == work.author_lane:
                exclusions.add("t0_reviewer_not_independent")
        if work.unresolved_dependencies:
            exclusions.add("unresolved_dependencies")
        if work.live_claim is not None and work.live_claim.lane != lane_name:
            exclusions.add("foreign_live_claim")
        if work.live_run is not None and work.live_run.lane != lane_name:
            exclusions.add("foreign_live_run")
        if evidence_expired:
            exclusions.add("evidence_expired")
        if evidence_future:
            exclusions.add("evidence_observed_in_future")
        if work.allowed_assignees and lane_name not in work.allowed_assignees:
            exclusions.add("assignee_restricted")
        if lane_usage is None:
            exclusions.add("usage_evidence_missing")
        else:
            usage_dt = _utc(lane_usage.observed_at, "usage observed_at")
            if not lane_usage.coverage_complete:
                exclusions.add("usage_coverage_incomplete")
            if usage_dt > now_dt:
                exclusions.add("usage_observed_in_future")
            elif (now_dt - usage_dt).total_seconds() > policy.max_usage_age_seconds:
                exclusions.add("usage_evidence_stale")
            if lane_usage.headroom <= 0:
                exclusions.add("usage_headroom_exhausted")

        sorted_exclusions = tuple(sorted(exclusions))
        if sorted_exclusions:
            candidates.append(
                CandidateRouteV1(
                    lane=lane_name,
                    eligible=False,
                    exclusions=sorted_exclusions,
                    score_components={},
                    total_score=None,
                )
            )
            continue
        assert lane_usage is not None
        components, total = _score(
            lane,
            lane_usage,
            has_continuity=lane_name in head_lanes,
            policy=policy,
        )
        candidates.append(
            CandidateRouteV1(
                lane=lane_name,
                eligible=True,
                exclusions=(),
                score_components=components,
                total_score=total,
            )
        )

    eligible = [candidate for candidate in candidates if candidate.eligible]
    eligible.sort(key=lambda candidate: (-(candidate.total_score or 0.0), candidate.lane))
    selected = eligible[0] if eligible else None
    if requested is not None and requested not in lane_by_name:
        selected = None
        outcome_reasons = {"explicit_user_route_unknown", "no_eligible_candidate"}
    elif selected is None:
        outcome_reasons = {"no_eligible_candidate"}
    elif requested is not None:
        outcome_reasons = {"explicit_user_route_honored"}
    else:
        outcome_reasons = {"selected_highest_score"}
    if selected is None:
        outcome_reasons.update(
            reason for candidate in candidates for reason in candidate.exclusions
        )

    if selected is None:
        confidence = 0.0
        confidence_reasons = ("refused",)
    elif requested is not None:
        confidence = 1.0
        confidence_reasons = ("explicit_user_route_binding",)
    elif len(eligible) == 1:
        confidence = 0.9
        confidence_reasons = ("single_eligible_candidate",)
    else:
        first = selected.total_score or 0.0
        second = eligible[1].total_score or 0.0
        margin = max(0.0, first - second)
        confidence = round(min(0.99, 0.6 + margin), 12)
        confidence_reasons = ("deterministic_tie_break" if margin == 0 else "score_margin",)

    base = {
        "schema_version": SCHEMA_VERSION,
        "work_id": work.work_id,
        "selected_lane": selected.lane if selected else None,
        "status": "selected" if selected else "refused",
        "reason_codes": sorted(outcome_reasons),
        "candidates": [candidate.as_dict() for candidate in candidates],
        "confidence": confidence,
        "confidence_reason_codes": list(confidence_reasons),
        "mutation_allowed": False,
        "work_fence": {
            "version": work.version,
            "assignee": work.assignee,
            "head_event_ids": head_ids,
        },
        "policy": {
            **policy_material,
            "artifact_sha256": policy.sha256,
            "effective_sha256": effective_policy_sha256,
        },
        "evidence": {
            "observed_at": observed,
            "sha256": evidence_sha256,
            "expires_at": expires,
        },
        "planned_at": planned,
    }
    plan_sha256 = canonical_sha256(base)
    return RoutePlanV1(
        work_id=work.work_id,
        selected_lane=selected.lane if selected else None,
        status="selected" if selected else "refused",
        reason_codes=tuple(sorted(outcome_reasons)),
        candidates=tuple(candidates),
        confidence=confidence,
        confidence_reason_codes=confidence_reasons,
        mutation_allowed=False,
        work_fence=base["work_fence"],
        policy=base["policy"],
        evidence=base["evidence"],
        planned_at=planned,
        plan_sha256=plan_sha256,
    )
