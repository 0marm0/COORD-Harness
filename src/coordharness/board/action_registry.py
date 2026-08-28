"""Pure action declaration and availability preview for board integrations.

This module intentionally has no control-plane imports.  It declares actions
and explains whether a caller has supplied enough *context* to offer each one;
it never grants authority or performs an action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


ACTION_REGISTRY_SCHEMA = "ActionRegistryV1"
_BOARD_MUTATION_VETO_FACES = frozenset({"loopback_board", "native_read_model"})
_TERMINAL_STATES = frozenset(
    {
        "archived",
        "canceled",
        "cancelled",
        "closed",
        "complete",
        "completed",
        "done",
        "failed",
        "finished",
        "skipped",
        "succeeded",
        "success",
        "superseded",
    }
)
_ACTIVE_CLAIM_STATES = frozenset({"active", "claimed", "running"})
_OPEN_ASSIGNMENTS = frozenset({"", "any", "shared", "unassigned"})


@dataclass(frozen=True, slots=True)
class ActionDeclaration:
    """One stable, serialisable action declaration."""

    id: str
    label: str
    target_kind: str
    executor: str
    required_actor: str
    required_policy: str
    preconditions: tuple[str, ...]
    mutation: bool
    requires_confirmation: bool
    placement: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "target_kind": self.target_kind,
            "executor": self.executor,
            "required_actor": self.required_actor,
            "required_policy": self.required_policy,
            "preconditions": list(self.preconditions),
            "mutation": self.mutation,
            "requires_confirmation": self.requires_confirmation,
            "placement": self.placement,
        }


@dataclass(frozen=True, slots=True)
class ActionResolutionContext:
    """Normalised, immutable inputs used only to preview action availability."""

    target: Mapping[str, Any]
    actor: str = ""
    session_id: str = ""
    structural_context: Mapping[str, Any] | None = None
    policy_capabilities: frozenset[str] = frozenset()
    executor_capabilities: frozenset[str] = frozenset()
    source_face: str = "loopback_board"


ACTION_DECLARATIONS: tuple[ActionDeclaration, ...] = (
    ActionDeclaration(
        "inspect",
        "Inspect",
        "any",
        "client.inspect",
        "none",
        "none",
        ("target_identity",),
        False,
        False,
        "primary",
    ),
    ActionDeclaration(
        "copy_id",
        "Copy ID",
        "any",
        "client.copy",
        "none",
        "none",
        ("target_identity",),
        False,
        False,
        "utility",
    ),
    ActionDeclaration(
        "claim",
        "Claim",
        "work",
        "mcp.claim_work",
        "assigned_or_shared",
        "work.claim",
        ("target_identity", "nonterminal", "unclaimed", "assignment", "dependencies_satisfied"),
        True,
        False,
        "lifecycle",
    ),
    ActionDeclaration(
        "heartbeat",
        "Heartbeat",
        "work",
        "mcp.heartbeat",
        "claim_owner",
        "work.heartbeat",
        ("target_identity", "nonterminal", "active_claim", "claim_owner"),
        True,
        False,
        "lifecycle",
    ),
    ActionDeclaration(
        "release",
        "Release",
        "work",
        "mcp.release",
        "claim_owner",
        "work.release",
        ("target_identity", "nonterminal", "active_claim", "claim_owner"),
        True,
        True,
        "lifecycle",
    ),
    ActionDeclaration(
        "park",
        "Park",
        "work",
        "mcp.park",
        "claim_owner",
        "work.park",
        ("target_identity", "nonterminal", "active_claim", "claim_owner", "resume_intent"),
        True,
        True,
        "lifecycle",
    ),
    ActionDeclaration(
        "block",
        "Block",
        "work",
        "mcp.block",
        "claim_owner",
        "work.block",
        ("target_identity", "nonterminal", "active_claim", "claim_owner", "resume_intent"),
        True,
        True,
        "lifecycle",
    ),
    ActionDeclaration(
        "complete",
        "Complete",
        "work",
        "mcp.complete",
        "claim_owner",
        "work.complete",
        ("target_identity", "nonterminal", "active_claim", "claim_owner", "proof_present"),
        True,
        True,
        "terminal",
    ),
    ActionDeclaration(
        "handoff",
        "Handoff",
        "work",
        "mcp.handoff_existing",
        "claim_owner",
        "work.handoff",
        ("target_identity", "nonterminal", "active_claim", "claim_owner", "handoff_destination"),
        True,
        True,
        "lifecycle",
    ),
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _capabilities(value: Mapping[str, Any] | Iterable[str] | None) -> frozenset[str]:
    if isinstance(value, Mapping):
        return frozenset(
            _text(key) for key, enabled in value.items() if enabled is True and _text(key)
        )
    if value is None or isinstance(value, (str, bytes)):
        return frozenset({_text(value)}) if _text(value) else frozenset()
    return frozenset(_text(item) for item in value if _text(item))


def _actor_id(actor: Any) -> str:
    if isinstance(actor, Mapping):
        return _lower(actor.get("actor") or actor.get("id") or actor.get("name"))
    return _lower(actor)


def _session_id(session_id: Any) -> str:
    if isinstance(session_id, Mapping):
        return _text(session_id.get("session_id") or session_id.get("id"))
    return _text(session_id)


def _target_view(target: Mapping[str, Any]) -> dict[str, Any]:
    claim = target.get("claim") if isinstance(target.get("claim"), Mapping) else {}
    identity = _text(target.get("work_id") or target.get("id") or target.get("target_id"))
    kind = _lower(target.get("target_kind") or target.get("kind"))
    if not kind and target.get("work_id") is not None:
        kind = "work"
    return {
        "id": identity,
        "kind": kind or "unknown",
        "state": _lower(target.get("intent_state") or target.get("state") or target.get("status")) or "unknown",
        "assignee": _lower(target.get("assignee") or target.get("assigned_actor")),
        "claim_id": _text(target.get("claim_id") or claim.get("claim_id") or claim.get("id")),
        "claim_state": _lower(
            target.get("claim_state") or target.get("claim_status") or claim.get("state") or claim.get("status")
        ),
        "claim_actor": _lower(
            target.get("claim_actor") or target.get("owner_session_actor") or claim.get("actor")
        ),
        "claim_session_id": _text(
            target.get("claim_session_id") or target.get("owner_session_id") or claim.get("session_id")
        ),
        "proof_present": any(
            target.get(field) is True
            for field in ("proof_present", "done_signal_exists", "artifact_present")
        ),
    }


def _check(check_id: str, passed: bool, reason: str) -> dict[str, Any]:
    return {"id": check_id, "passed": bool(passed), "reason": reason}


def _has_capability(capabilities: frozenset[str], required: str) -> bool:
    return required == "none" or required in capabilities or "*" in capabilities


def _resolve_one(declaration: ActionDeclaration, context: ActionResolutionContext) -> dict[str, Any]:
    target = _target_view(context.target)
    structural = context.structural_context or {}
    terminal = target["state"] in _TERMINAL_STATES
    active_claim = bool(target["claim_id"]) and target["claim_state"] in _ACTIVE_CLAIM_STATES
    actor_present = bool(context.actor)
    session_present = bool(context.session_id)
    kind_ok = declaration.target_kind == "any" or target["kind"] == declaration.target_kind

    if declaration.required_actor == "none":
        actor_ok = True
        actor_reason = "This local affordance does not require an actor identity."
        session_ok = True
        session_reason = "This local affordance does not require a session identity."
    else:
        actor_ok = actor_present
        actor_reason = (
            f"Actor {context.actor!r} is present."
            if actor_present
            else "An authenticated actor identity is required."
        )
        session_ok = session_present
        session_reason = (
            f"Session {context.session_id!r} is present."
            if session_present
            else "An authenticated session identity is required."
        )

    assignment_ok = True
    assignment_reason = "This action does not depend on lane assignment."
    if declaration.required_actor == "assigned_or_shared":
        assignment_ok = bool(context.actor) and (
            target["assignee"] in _OPEN_ASSIGNMENTS or target["assignee"] == context.actor
        )
        assignment_reason = (
            "The work is shared or assigned to the requesting actor."
            if assignment_ok
            else f"Work assigned to {target['assignee'] or '<unknown>'!r} cannot be claimed by {context.actor or '<missing>'!r}."
        )

    claim_ok = True
    claim_reason = "This action does not require claim ownership."
    if "unclaimed" in declaration.preconditions:
        claim_ok = not active_claim
        claim_reason = (
            "No active claim is present."
            if claim_ok
            else f"Active claim {target['claim_id']!r} already owns this work."
        )
    elif "active_claim" in declaration.preconditions:
        claim_ok = active_claim
        claim_reason = (
            f"Active claim {target['claim_id']!r} is present."
            if claim_ok
            else "An active claim is required for this lifecycle action."
        )

    owner_ok = True
    owner_reason = "This action does not require claim-owner identity."
    if "claim_owner" in declaration.preconditions:
        owner_ok = (
            active_claim
            and bool(context.actor)
            and bool(context.session_id)
            and target["claim_actor"] == context.actor
            and target["claim_session_id"] == context.session_id
        )
        owner_reason = (
            "The requesting actor and session own the active claim."
            if owner_ok
            else "The requesting actor and session do not own the active claim."
        )

    structural_ok = True
    structural_reason = "No additional structural context is required."
    if "dependencies_satisfied" in declaration.preconditions:
        structural_ok = structural.get("dependencies_satisfied") is True
        structural_reason = (
            "Structural context reports all claim dependencies satisfied."
            if structural_ok
            else "Structural context must confirm that all claim dependencies are satisfied."
        )
    elif "resume_intent" in declaration.preconditions:
        structural_ok = bool(
            _text(structural.get("next_step"))
            and (_text(structural.get("resume_when")) or _text(structural.get("resume_predicate")))
        )
        structural_reason = (
            "Structural context contains a next step and resume condition."
            if structural_ok
            else "A next step and resume condition are required to park or block work."
        )
    elif "handoff_destination" in declaration.preconditions:
        destination = _lower(structural.get("handoff_to") or structural.get("target_actor"))
        structural_ok = bool(destination) and destination != context.actor
        structural_reason = (
            f"Handoff destination {destination!r} is structurally valid."
            if structural_ok
            else "A handoff destination different from the current actor is required."
        )

    proof_ok = True
    proof_reason = "This action does not require completion proof."
    if "proof_present" in declaration.preconditions:
        proof_ok = target["proof_present"]
        proof_reason = (
            "The target reports completion proof present."
            if proof_ok
            else "Completion proof must be present before complete can be offered."
        )

    policy_ok = _has_capability(context.policy_capabilities, declaration.required_policy)
    policy_reason = (
        "No policy capability is required."
        if declaration.required_policy == "none"
        else (
            f"Policy capability {declaration.required_policy!r} is present."
            if policy_ok
            else f"Policy capability {declaration.required_policy!r} is required."
        )
    )
    builtin_executor = declaration.executor.startswith("client.")
    executor_ok = builtin_executor or _has_capability(context.executor_capabilities, declaration.executor)
    executor_reason = (
        "The affordance uses a built-in client executor."
        if builtin_executor
        else (
            f"Executor capability {declaration.executor!r} is present."
            if executor_ok
            else f"Executor capability {declaration.executor!r} is required."
        )
    )
    source_veto = declaration.mutation and context.source_face in _BOARD_MUTATION_VETO_FACES
    source_ok = not source_veto
    source_reason = (
        f"Source face {context.source_face!r} is read-only and hard-vetoes every mutation."
        if source_veto
        else (
            f"Source face {context.source_face!r} does not impose the board mutation veto."
            if declaration.mutation
            else "Source-face mutation vetoes do not apply to a non-mutating affordance."
        )
    )

    checks = [
        _check(
            "target_identity",
            bool(target["id"]),
            f"Target identity {target['id']!r} is present." if target["id"] else "A target identity is required.",
        ),
        _check(
            "target_kind",
            kind_ok,
            (
                f"Target kind {target['kind']!r} is supported."
                if kind_ok
                else f"Action requires target kind {declaration.target_kind!r}, not {target['kind']!r}."
            ),
        ),
        _check(
            "target_state",
            not (declaration.mutation and terminal),
            (
                f"Target state {target['state']!r} is terminal."
                if declaration.mutation and terminal
                else f"Target state {target['state']!r} permits preview evaluation."
            ),
        ),
        _check("actor_identity", actor_ok, actor_reason),
        _check("session_identity", session_ok, session_reason),
        _check("assignment", assignment_ok, assignment_reason),
        _check("claim_state", claim_ok, claim_reason),
        _check("claim_owner", owner_ok, owner_reason),
        _check("structural_context", structural_ok, structural_reason),
        _check("proof_state", proof_ok, proof_reason),
        _check("policy_capability", policy_ok, policy_reason),
        _check("executor_capability", executor_ok, executor_reason),
        _check("source_face", source_ok, source_reason),
    ]
    failed_reasons = [check["reason"] for check in checks if not check["passed"]]
    available = not failed_reasons
    row = declaration.as_dict()
    row.update(
        {
            "available": available,
            "reachable": bool(declaration.mutation and available),
            "reason": None if available else " ".join(failed_reasons),
            "checks": checks,
            "preview_only": True,
            "authorized": False,
            "execution_authority": "not_granted",
        }
    )
    return row


def resolve_actions(
    target: Mapping[str, Any],
    *,
    actor: str | Mapping[str, Any] | None = None,
    session_id: str | Mapping[str, Any] | None = None,
    structural_context: Mapping[str, Any] | None = None,
    policy_capabilities: Mapping[str, Any] | Iterable[str] | None = None,
    executor_capabilities: Mapping[str, Any] | Iterable[str] | None = None,
    source_face: str = "loopback_board",
) -> list[dict[str, Any]]:
    """Resolve declarations into deterministic previews; never execute or authorise."""
    if not isinstance(target, Mapping):
        raise TypeError("target must be a mapping")
    context = ActionResolutionContext(
        target=target,
        actor=_actor_id(actor),
        session_id=_session_id(session_id),
        structural_context=structural_context or {},
        policy_capabilities=_capabilities(policy_capabilities),
        executor_capabilities=_capabilities(executor_capabilities),
        source_face=_lower(source_face) or "unknown",
    )
    return [_resolve_one(declaration, context) for declaration in ACTION_DECLARATIONS]


def build_action_registry_response(
    target: Mapping[str, Any],
    *,
    cache_generation: int = 0,
    generated_at: str = "",
    **context: Any,
) -> dict[str, Any]:
    """Build a server-ready deterministic response with explicit preview accounting."""
    if isinstance(cache_generation, bool) or not isinstance(cache_generation, int):
        raise TypeError("cache_generation must be an integer")
    if cache_generation < 0:
        raise ValueError("cache_generation must be non-negative")
    actions = resolve_actions(target, **context)
    target_view = _target_view(target)
    source_face = _lower(context.get("source_face")) or "loopback_board"
    mutations = [action for action in actions if action["mutation"]]
    return {
        "schema_version": ACTION_REGISTRY_SCHEMA,
        "preview_only": True,
        "authorization": "not_granted",
        "source": {
            "cache_generation": cache_generation,
            "generated_at": _text(generated_at),
            "read_only": True,
            "source_face": source_face,
        },
        "target": {
            "id": target_view["id"],
            "kind": target_view["kind"],
            "state": target_view["state"],
        },
        "counts": {
            "declared": len(actions),
            "mutations": len(mutations),
            "non_mutations": len(actions) - len(mutations),
            "available": sum(action["available"] for action in actions),
            "unavailable": sum(not action["available"] for action in actions),
            "available_mutations": sum(action["available"] for action in mutations),
            "reachable_mutations": sum(action["reachable"] for action in mutations),
        },
        "actions": actions,
    }


__all__ = [
    "ACTION_DECLARATIONS",
    "ACTION_REGISTRY_SCHEMA",
    "ActionDeclaration",
    "ActionResolutionContext",
    "build_action_registry_response",
    "resolve_actions",
]
