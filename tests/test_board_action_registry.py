from __future__ import annotations

import json

from coordharness.board import action_registry as action_registry_module
from coordharness.board.action_registry import (
    ACTION_DECLARATIONS,
    build_action_registry_response,
    resolve_actions,
)
from coordharness.coord import coord_db


MUTATIONS = {"claim", "heartbeat", "release", "park", "block", "complete", "handoff"}
POLICIES = {f"work.{action}" for action in MUTATIONS}
EXECUTORS = {
    "mcp.claim_work",
    "mcp.heartbeat",
    "mcp.release",
    "mcp.park",
    "mcp.block",
    "mcp.complete",
    "mcp.handoff_existing",
}


def _by_id(actions):
    return {action["id"]: action for action in actions}


def _target(**updates):
    target = {
        "work_id": "WORK-1",
        "target_kind": "work",
        "intent_state": "queued",
        "assignee": "codex",
    }
    target.update(updates)
    return target


def _active_target(**updates):
    target = _target(
        intent_state="running",
        claim_id="claim-1",
        claim_state="active",
        claim_actor="codex",
        claim_session_id="codex:s1",
    )
    target.update(updates)
    return target


def _context(**updates):
    context = {
        "actor": "codex",
        "session_id": "codex:s1",
        "structural_context": {
            "dependencies_satisfied": True,
            "next_step": "resume implementation",
            "resume_when": "dependency lands",
            "handoff_to": "claude",
        },
        "policy_capabilities": POLICIES,
        "executor_capabilities": EXECUTORS,
        "source_face": "mcp_client",
    }
    context.update(updates)
    return context


def test_declarations_are_complete_typed_and_deterministic():
    required = {
        "id",
        "label",
        "target_kind",
        "executor",
        "required_actor",
        "required_policy",
        "preconditions",
        "mutation",
        "requires_confirmation",
        "placement",
    }
    assert [item.id for item in ACTION_DECLARATIONS] == [
        "inspect", "copy_id", "claim", "heartbeat", "release", "park", "block", "complete", "handoff"
    ]
    assert all(set(item.as_dict()) == required for item in ACTION_DECLARATIONS)
    assert {item.id for item in ACTION_DECLARATIONS if item.mutation} == MUTATIONS


def test_terminal_states_are_exactly_the_canonical_coord_lifecycle_set():
    assert action_registry_module._TERMINAL_STATES == coord_db.TERMINAL_WORK_STATES
    for state in sorted(coord_db.TERMINAL_WORK_STATES):
        actions = _by_id(resolve_actions(_target(intent_state=state), **_context()))
        assert all(not actions[action_id]["available"] for action_id in MUTATIONS)
        assert all("terminal" in actions[action_id]["reason"] for action_id in MUTATIONS)


def test_missing_actor_and_session_leave_safe_affordances_but_explain_mutations():
    actions = _by_id(
        resolve_actions(
            _target(),
            structural_context={"dependencies_satisfied": True},
            policy_capabilities=POLICIES,
            executor_capabilities=EXECUTORS,
            source_face="mcp_client",
        )
    )
    assert actions["inspect"]["available"]
    assert actions["copy_id"]["available"]
    for action_id in MUTATIONS:
        assert not actions[action_id]["available"]
        assert actions[action_id]["reason"]
        failed = [check for check in actions[action_id]["checks"] if not check["passed"]]
        assert failed
        assert all(check["reason"] for check in failed)
    assert "authenticated actor identity" in actions["claim"]["reason"]
    assert "authenticated session identity" in actions["claim"]["reason"]


def test_assignment_mismatch_and_terminal_or_claim_states_fail_closed():
    mismatch = _by_id(resolve_actions(_target(assignee="claude"), **_context()))
    assert not mismatch["claim"]["available"]
    assert "assigned to 'claude'" in mismatch["claim"]["reason"]

    terminal = _by_id(resolve_actions(_target(intent_state="done"), **_context()))
    assert all(not terminal[action_id]["available"] for action_id in MUTATIONS)
    assert all("terminal" in terminal[action_id]["reason"] for action_id in MUTATIONS)

    already_claimed = _by_id(resolve_actions(_active_target(), **_context()))
    assert not already_claimed["claim"]["available"]
    assert "already owns" in already_claimed["claim"]["reason"]
    assert already_claimed["heartbeat"]["available"]


def test_claim_owner_completion_proof_and_resume_structure_are_independent_checks():
    no_proof = _by_id(resolve_actions(_active_target(), **_context()))
    assert not no_proof["complete"]["available"]
    assert "Completion proof" in no_proof["complete"]["reason"]
    assert no_proof["heartbeat"]["available"]

    proof = _by_id(resolve_actions(_active_target(done_signal_exists=True), **_context()))
    assert proof["complete"]["available"]

    wrong_owner = _by_id(
        resolve_actions(_active_target(claim_session_id="codex:other"), **_context())
    )
    assert not wrong_owner["heartbeat"]["available"]
    assert "do not own" in wrong_owner["heartbeat"]["reason"]

    no_resume = _by_id(
        resolve_actions(_active_target(), **_context(structural_context={}))
    )
    assert not no_resume["park"]["available"]
    assert not no_resume["block"]["available"]
    assert "next step and resume condition" in no_resume["park"]["reason"]


def test_policy_and_executor_capabilities_are_both_required():
    missing_policy = _by_id(
        resolve_actions(_target(), **_context(policy_capabilities=set()))
    )
    assert not missing_policy["claim"]["available"]
    assert "Policy capability 'work.claim' is required" in missing_policy["claim"]["reason"]

    missing_executor = _by_id(
        resolve_actions(_target(), **_context(executor_capabilities=set()))
    )
    assert not missing_executor["claim"]["available"]
    assert "Executor capability 'mcp.claim_work' is required" in missing_executor["claim"]["reason"]


def test_malformed_mapping_capabilities_and_boolean_fields_fail_closed():
    malformed_values = ("false", 1, object())
    for malformed in malformed_values:
        policy = _by_id(
            resolve_actions(
                _target(),
                **_context(policy_capabilities={"work.claim": malformed}),
            )
        )
        assert not policy["claim"]["available"]
        assert "Policy capability 'work.claim' is required" in policy["claim"]["reason"]

        executor = _by_id(
            resolve_actions(
                _target(),
                **_context(executor_capabilities={"mcp.claim_work": malformed}),
            )
        )
        assert not executor["claim"]["available"]
        assert "Executor capability 'mcp.claim_work' is required" in executor["claim"]["reason"]

        dependencies = _by_id(
            resolve_actions(
                _target(),
                **_context(structural_context={"dependencies_satisfied": malformed}),
            )
        )
        assert not dependencies["claim"]["available"]
        assert "must confirm" in dependencies["claim"]["reason"]

        for proof_field in ("proof_present", "done_signal_exists", "artifact_present"):
            proof = _by_id(
                resolve_actions(
                    _active_target(**{proof_field: malformed}),
                    **_context(),
                )
            )
            assert not proof["complete"]["available"]
            assert "Completion proof must be present" in proof["complete"]["reason"]


def test_loopback_and_native_read_model_hard_veto_and_ablation():
    for source_face in ("loopback_board", "native_read_model"):
        response = build_action_registry_response(
            _active_target(done_signal_exists=True),
            **_context(source_face=source_face),
        )
        actions = _by_id(response["actions"])
        assert {action_id for action_id, action in actions.items() if action["mutation"]} == MUTATIONS
        assert response["counts"]["available_mutations"] == 0
        assert response["counts"]["reachable_mutations"] == 0
        assert all(not actions[action_id]["available"] for action_id in MUTATIONS)
        assert all("hard-vetoes every mutation" in actions[action_id]["reason"] for action_id in MUTATIONS)
        assert actions["inspect"]["available"] and actions["copy_id"]["available"]

    ablated = build_action_registry_response(
        _active_target(done_signal_exists=True),
        **_context(source_face="mcp_client"),
    )
    assert ablated["counts"]["available_mutations"] > 0


def test_authenticated_mcp_preview_is_available_but_never_authority():
    response = build_action_registry_response(
        _target(),
        cache_generation=42,
        generated_at="2026-08-26T12:00:00Z",
        **_context(),
    )
    claim = _by_id(response["actions"])["claim"]
    assert claim["available"] and claim["reachable"]
    assert claim["preview_only"] is True
    assert claim["authorized"] is False
    assert claim["execution_authority"] == "not_granted"
    assert response["preview_only"] is True
    assert response["authorization"] == "not_granted"


def test_response_is_deterministic_and_every_unavailable_action_has_human_reason():
    first = build_action_registry_response({}, source_face="loopback_board")
    second = build_action_registry_response({}, source_face="loopback_board")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["counts"] == {
        "declared": 9,
        "mutations": 7,
        "non_mutations": 2,
        "available": 0,
        "unavailable": 9,
        "available_mutations": 0,
        "reachable_mutations": 0,
    }
    unavailable = [action for action in first["actions"] if not action["available"]]
    assert unavailable
    assert all(isinstance(action["reason"], str) and action["reason"] for action in unavailable)
    assert all(
        check["reason"]
        for action in first["actions"]
        for check in action["checks"]
        if not check["passed"]
    )
