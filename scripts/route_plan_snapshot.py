#!/usr/bin/env python3
"""Build a non-mutating RoutePlanV1 from an explicit JSON snapshot.

This surface deliberately has no live telemetry adapter.  It imports only the
pure assignment planner, never opens coord.db, and never imports a lifecycle
writer.  All decision timestamps and evidence must be supplied by the caller.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


# Running this file directly does not normally add the repository's ``src``
# directory to sys.path.  The same relative layout is used in both repositories.
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from coordharness.coord.assignment_routing import (  # noqa: E402
    ActiveAssignmentHeadV1,
    LaneSnapshotV1,
    LiveOwnershipV1,
    RoutePlanningError,
    RoutingPolicyV1,
    UsageSnapshotV1,
    WorkSnapshotV1,
    plan_assignment_route,
)


INPUT_SCHEMA_VERSION = "RoutePlanSnapshotV1"
_EVIDENCE_REFUSALS = frozenset(
    {
        "evidence_expired",
        "evidence_observed_in_future",
        "usage_coverage_incomplete",
        "usage_evidence_missing",
        "usage_evidence_stale",
        "usage_observed_in_future",
    }
)


class SnapshotSurfaceError(ValueError):
    """Raised when a snapshot cannot safely produce a planning receipt."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotSurfaceError(f"{label} must be a JSON object")
    return value


def _array(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise SnapshotSurfaceError(f"{label} must be a JSON array")
    return value


def _strict_fields(
    value: Mapping[str, Any],
    label: str,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise SnapshotSurfaceError(f"{label} is missing fields: {', '.join(missing)}")
    if unknown:
        raise SnapshotSurfaceError(f"{label} has unknown fields: {', '.join(unknown)}")


def _ownership(value: Any, label: str) -> LiveOwnershipV1 | None:
    if value is None:
        return None
    data = _mapping(value, label)
    _strict_fields(data, label, required=frozenset({"lane", "identity"}))
    return LiveOwnershipV1(lane=data["lane"], identity=data["identity"])


def _work(value: Any) -> WorkSnapshotV1:
    data = _mapping(value, "work")
    required = frozenset({"work_id", "version"})
    optional = frozenset(
        {
            "assignee",
            "review_tier",
            "author_lane",
            "required_capabilities",
            "unresolved_dependencies",
            "allowed_assignees",
            "live_claim",
            "live_run",
        }
    )
    _strict_fields(data, "work", required=required, optional=optional)
    kwargs = dict(data)
    for name in ("required_capabilities", "unresolved_dependencies", "allowed_assignees"):
        if name in kwargs:
            kwargs[name] = tuple(_array(kwargs[name], f"work.{name}"))
    for name in ("live_claim", "live_run"):
        if name in kwargs:
            kwargs[name] = _ownership(kwargs[name], f"work.{name}")
    return WorkSnapshotV1(**kwargs)


def _assignment_head(value: Any, index: int) -> ActiveAssignmentHeadV1:
    label = f"assignment_heads[{index}]"
    data = _mapping(value, label)
    _strict_fields(data, label, required=frozenset({"event_id", "lane"}))
    return ActiveAssignmentHeadV1(event_id=data["event_id"], lane=data["lane"])


def _lane(value: Any, index: int) -> LaneSnapshotV1:
    label = f"lanes[{index}]"
    data = _mapping(value, label)
    _strict_fields(
        data,
        label,
        required=frozenset({"lane", "available", "auth_available"}),
        optional=frozenset({"capabilities", "active_load", "load_capacity"}),
    )
    kwargs = dict(data)
    if "capabilities" in kwargs:
        kwargs["capabilities"] = tuple(_array(kwargs["capabilities"], f"{label}.capabilities"))
    return LaneSnapshotV1(**kwargs)


def _usage(value: Any, index: int) -> UsageSnapshotV1:
    label = f"usage[{index}]"
    data = _mapping(value, label)
    _strict_fields(
        data,
        label,
        required=frozenset(
            {"lane", "headroom", "limit", "coverage_complete", "observed_at"}
        ),
    )
    return UsageSnapshotV1(**data)


def _policy(value: Any) -> RoutingPolicyV1:
    data = _mapping(value, "policy")
    _strict_fields(
        data,
        "policy",
        required=frozenset({"version", "sha256", "max_usage_age_seconds"}),
        optional=frozenset({"weights"}),
    )
    kwargs = dict(data)
    if "weights" in kwargs:
        weights = _mapping(kwargs["weights"], "policy.weights")
        kwargs["weights"] = tuple(weights.items())
    return RoutingPolicyV1(**kwargs)


def build_route_plan(snapshot: Any) -> dict[str, Any]:
    """Validate one explicit snapshot and return its RoutePlanV1 document."""

    data = _mapping(snapshot, "snapshot")
    _strict_fields(
        data,
        "snapshot",
        required=frozenset(
            {
                "schema_version",
                "work",
                "assignment_heads",
                "lanes",
                "usage",
                "policy",
                "timestamps",
            }
        ),
        optional=frozenset({"explicit_user_route"}),
    )
    if data["schema_version"] != INPUT_SCHEMA_VERSION:
        raise SnapshotSurfaceError(
            f"schema_version must be {INPUT_SCHEMA_VERSION!r}"
        )

    timestamps = _mapping(data["timestamps"], "timestamps")
    _strict_fields(
        timestamps,
        "timestamps",
        required=frozenset({"evidence_observed_at", "now", "expires_at"}),
    )
    explicit_route = data.get("explicit_user_route")
    if explicit_route is not None and not isinstance(explicit_route, str):
        raise SnapshotSurfaceError("explicit_user_route must be a string or null")

    assignment_heads = tuple(
        _assignment_head(item, index)
        for index, item in enumerate(_array(data["assignment_heads"], "assignment_heads"))
    )
    lanes = tuple(
        _lane(item, index)
        for index, item in enumerate(_array(data["lanes"], "lanes"))
    )
    usage = tuple(
        _usage(item, index)
        for index, item in enumerate(_array(data["usage"], "usage"))
    )
    if {item.lane for item in usage} != {item.lane for item in lanes}:
        raise SnapshotSurfaceError(
            "usage lanes must exactly match the declared lane snapshots"
        )

    plan = plan_assignment_route(
        work=_work(data["work"]),
        assignment_heads=assignment_heads,
        lanes=lanes,
        usage=usage,
        policy=_policy(data["policy"]),
        evidence_observed_at=timestamps["evidence_observed_at"],
        now=timestamps["now"],
        expires_at=timestamps["expires_at"],
        explicit_user_route=explicit_route,
    )
    evidence_refusals = sorted(
        _EVIDENCE_REFUSALS.intersection(
            exclusion
            for candidate in plan.candidates
            for exclusion in candidate.exclusions
        )
    )
    if evidence_refusals:
        raise SnapshotSurfaceError(
            "snapshot evidence is incomplete, future, expired, or stale: "
            + ", ".join(evidence_refusals)
        )
    document = plan.as_dict()
    if document.get("mutation_allowed") is not False:
        raise SnapshotSurfaceError("planner violated the non-mutation boundary")
    return document


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotSurfaceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_snapshot(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a plan-only RoutePlanV1 from RoutePlanSnapshotV1 JSON."
    )
    parser.add_argument(
        "snapshot",
        nargs="?",
        default="-",
        help="JSON snapshot file, or - for stdin (default)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = build_route_plan(_read_snapshot(args.snapshot))
    except (OSError, TypeError, ValueError, RoutePlanningError) as exc:
        print(f"route-plan-snapshot: error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
