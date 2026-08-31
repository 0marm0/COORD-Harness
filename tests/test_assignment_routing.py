from __future__ import annotations

import copy
import dataclasses
import hashlib

import pytest

from coordharness.coord.assignment_routing import (
    ActiveAssignmentHeadV1,
    LaneSnapshotV1,
    LiveOwnershipV1,
    RoutePlanningError,
    RoutingPolicyV1,
    UsageSnapshotV1,
    WorkSnapshotV1,
    canonical_sha256,
    plan_assignment_route,
)


NOW = "2026-08-31T12:00:00Z"
OBSERVED = "2026-08-31T11:58:00Z"
EXPIRES = "2026-08-31T12:05:00Z"
POLICY_HASH = hashlib.sha256(b"public assignment routing policy v1").hexdigest()


def _policy(**overrides):
    values = {
        "version": "2026-08-31.v1",
        "sha256": POLICY_HASH,
        "max_usage_age_seconds": 900,
    }
    values.update(overrides)
    return RoutingPolicyV1(**values)


def _work(**overrides):
    values = {
        "work_id": "WORK-101",
        "version": 7,
        "assignee": "lane-a",
        "review_tier": "T1",
        "required_capabilities": ("python",),
    }
    values.update(overrides)
    return WorkSnapshotV1(**values)


def _lane(name, **overrides):
    values = {
        "lane": name,
        "available": True,
        "auth_available": True,
        "capabilities": ("python", "review"),
        "active_load": 1,
        "load_capacity": 10,
    }
    values.update(overrides)
    return LaneSnapshotV1(**values)


def _usage(name, **overrides):
    values = {
        "lane": name,
        "headroom": 80,
        "limit": 100,
        "coverage_complete": True,
        "observed_at": OBSERVED,
    }
    values.update(overrides)
    return UsageSnapshotV1(**values)


def _plan(**overrides):
    values = {
        "work": _work(),
        "assignment_heads": (ActiveAssignmentHeadV1(12, "lane-a"),),
        "lanes": (_lane("lane-a"), _lane("lane-b", active_load=5)),
        "usage": (_usage("lane-a"), _usage("lane-b", headroom=40)),
        "policy": _policy(),
        "evidence_observed_at": OBSERVED,
        "now": NOW,
        "expires_at": EXPIRES,
    }
    values.update(overrides)
    return plan_assignment_route(**values)


def test_plan_is_deterministic_under_input_order_and_duplicate_heads():
    first = _plan(
        assignment_heads=(
            ActiveAssignmentHeadV1(19, "lane-b"),
            ActiveAssignmentHeadV1(12, "lane-a"),
            ActiveAssignmentHeadV1(19, "lane-b"),
        )
    )
    second = _plan(
        assignment_heads=(
            ActiveAssignmentHeadV1(12, "lane-a"),
            ActiveAssignmentHeadV1(19, "lane-b"),
        ),
        lanes=tuple(reversed((_lane("lane-a"), _lane("lane-b", active_load=5)))),
        usage=tuple(reversed((_usage("lane-a"), _usage("lane-b", headroom=40)))),
    )

    assert first.as_dict() == second.as_dict()
    assert first.plan_sha256 == second.plan_sha256
    assert first.as_dict()["work_fence"] == {
        "version": 7,
        "assignee": "lane-a",
        "head_event_ids": [12, 19],
    }
    document = first.as_dict()
    digest = document.pop("plan_sha256")
    assert canonical_sha256(document) == digest


def test_every_hard_exclusion_is_explained_and_excluded_candidates_are_not_scored():
    work = _work(
        review_tier="T0",
        author_lane="lane-a",
        unresolved_dependencies=("DEP-2", "DEP-1"),
        allowed_assignees=("lane-b",),
        live_claim=LiveOwnershipV1("lane-a", "claim-7"),
        live_run=LiveOwnershipV1("lane-a", "run-9"),
    )
    plan = _plan(
        work=work,
        lanes=(
            _lane("lane-a", auth_available=False, capabilities=()),
            _lane("lane-b", available=False),
        ),
        usage=(
            _usage("lane-a", coverage_complete=False),
            _usage("lane-b", observed_at="2026-08-31T11:00:00Z"),
        ),
        explicit_user_route="lane-b",
    )

    assert plan.status == "refused"
    assert plan.selected_lane is None
    assert plan.confidence == 0.0
    assert plan.mutation_allowed is False
    by_lane = {candidate.lane: candidate for candidate in plan.candidates}
    assert by_lane["lane-a"].exclusions == tuple(
        sorted(
            {
                "assignee_restricted",
                "auth_unavailable",
                "explicit_user_route_binding",
                "required_capability_unavailable",
                "t0_reviewer_not_independent",
                "unresolved_dependencies",
                "usage_coverage_incomplete",
            }
        )
    )
    assert by_lane["lane-b"].exclusions == tuple(
        sorted(
            {
                "foreign_live_claim",
                "foreign_live_run",
                "lane_unavailable",
                "unresolved_dependencies",
                "usage_evidence_stale",
            }
        )
    )
    assert all(candidate.score_components == {} for candidate in plan.candidates)
    assert all(candidate.total_score is None for candidate in plan.candidates)
    assert plan.reason_codes == tuple(sorted(set(plan.reason_codes)))


@pytest.mark.parametrize(
    ("usage_override", "time_override", "reason"),
    [
        ({"coverage_complete": False}, {}, "usage_coverage_incomplete"),
        ({"observed_at": "2026-08-31T11:00:00Z"}, {}, "usage_evidence_stale"),
        ({}, {"now": "2026-08-31T12:05:00Z"}, "evidence_expired"),
    ],
)
def test_stale_or_incomplete_evidence_refuses_instead_of_falling_back(
    usage_override, time_override, reason
):
    plan = _plan(
        lanes=(_lane("lane-a"),),
        usage=(_usage("lane-a", **usage_override),),
        **time_override,
    )

    assert plan.status == "refused"
    assert plan.selected_lane is None
    assert reason in plan.reason_codes
    assert plan.candidates[0].eligible is False


def test_explicit_user_route_is_binding_but_cannot_override_safety_gates():
    selected = _plan(explicit_user_route="lane-b")
    assert selected.selected_lane == "lane-b"
    assert selected.reason_codes == ("explicit_user_route_honored",)
    assert selected.candidates[0].exclusions == ("explicit_user_route_binding",)

    refused = _plan(
        explicit_user_route="lane-b",
        lanes=(_lane("lane-a"), _lane("lane-b", auth_available=False)),
    )
    assert refused.status == "refused"
    assert "auth_unavailable" in refused.reason_codes


def test_score_components_are_decomposed_and_sum_to_total():
    plan = _plan(
        lanes=(_lane("lane-a", active_load=2, load_capacity=10),),
        usage=(_usage("lane-a", headroom=50, limit=100),),
    )
    candidate = plan.candidates[0]

    assert candidate.eligible is True
    assert candidate.score_components == {
        "available_capacity": {"value": 0.8, "weight": 0.35, "contribution": 0.28},
        "continuity": {"value": 1.0, "weight": 0.1, "contribution": 0.1},
        "usage_headroom": {"value": 0.5, "weight": 0.55, "contribution": 0.275},
    }
    assert candidate.total_score == 0.655
    assert candidate.total_score == sum(
        component["contribution"] for component in candidate.score_components.values()
    )


def test_planning_does_not_mutate_inputs_and_result_cannot_authorize_mutation():
    lanes = [_lane("lane-a")]
    usage = [_usage("lane-a")]
    heads = [ActiveAssignmentHeadV1(12, "lane-a")]
    before = copy.deepcopy((lanes, usage, heads))

    plan = _plan(lanes=lanes, usage=usage, assignment_heads=heads)

    assert (lanes, usage, heads) == before
    assert plan.mutation_allowed is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.selected_lane = "lane-z"
    with pytest.raises(TypeError):
        plan.work_fence["assignee"] = "lane-z"
    with pytest.raises(TypeError):
        plan.candidates[0].score_components["continuity"]["value"] = 0.0


def test_any_decision_bearing_input_change_changes_evidence_and_plan_hashes():
    baseline = _plan()
    changed_load = _plan(lanes=(_lane("lane-a", active_load=2), _lane("lane-b", active_load=5)))
    changed_head = _plan(assignment_heads=(ActiveAssignmentHeadV1(13, "lane-a"),))

    assert baseline.evidence["sha256"] != changed_load.evidence["sha256"]
    assert baseline.plan_sha256 != changed_load.plan_sha256
    assert baseline.evidence["sha256"] != changed_head.evidence["sha256"]
    assert baseline.plan_sha256 != changed_head.plan_sha256

    changed_policy = _plan(policy=_policy(max_usage_age_seconds=1_000))
    assert baseline.evidence["sha256"] != changed_policy.evidence["sha256"]
    assert baseline.plan_sha256 != changed_policy.plan_sha256


def test_selected_plan_cannot_outlive_decision_evidence():
    with pytest.raises(RoutePlanningError, match="freshness deadline"):
        _plan(expires_at="2026-09-01T12:00:00Z")


def test_output_is_public_schema_and_contains_the_required_receipts():
    document = _plan().as_dict()

    assert document["schema_version"] == "RoutePlanV1"
    assert document["status"] == "selected"
    assert document["mutation_allowed"] is False
    assert document["policy"] == {
        "version": "2026-08-31.v1",
        "artifact_sha256": POLICY_HASH,
        "effective_sha256": document["policy"]["effective_sha256"],
        "max_usage_age_seconds": 900.0,
        "weights": {
            "available_capacity": 0.35,
            "continuity": 0.1,
            "usage_headroom": 0.55,
        },
    }
    assert len(document["policy"]["effective_sha256"]) == 64
    assert document["evidence"] == {
        "observed_at": "2026-08-31T11:58:00.000000Z",
        "sha256": document["evidence"]["sha256"],
        "expires_at": "2026-08-31T12:05:00.000000Z",
    }
    assert len(document["plan_sha256"]) == 64
    assert len(document["evidence"]["sha256"]) == 64


def test_non_finite_numbers_are_rejected_before_canonical_json_hashing():
    with pytest.raises(RoutePlanningError, match="finite non-negative"):
        _lane("lane-a", active_load=float("nan"))
