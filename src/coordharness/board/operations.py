"""Bounded operational analytics over the board's four public documents.

The operations atlas is a projection, not another authority.  It receives the
already-materialised snapshot, graph, structural context, and occurrence-only
timeline documents and derives metrics without opening SQLite or reading event
prose.  Keeping the builder pure gives the server one important guarantee: the
atlas can never observe a fifth instant or quietly widen the public surface.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from coordharness.board.graph_envelope import build_graph_envelope


OPERATIONS_SCHEMA = "OpsAtlasV1"
_DONE = {"done", "complete", "completed", "closed", "archived"}
_RUNNING = {"running", "active"}
_ATTENTION = {"blocked", "attention", "failed", "stuck"}
_DEPENDENCY_ANALYSIS_CAP = 400


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    rendered = str(value).strip()
    return rendered or fallback


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _lane(owner: Any) -> str:
    rendered = _text(owner)
    return rendered.split(":", 1)[0].lower() if rendered else "unowned"


def _bounded_counts(values: Iterable[str], *, limit: int = 40) -> list[dict[str, Any]]:
    counts = Counter(_text(value, "unspecified") for value in values)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shown = ordered[:limit]
    other = sum(count for _, count in ordered[limit:])
    result = [{"key": key, "count": count} for key, count in shown]
    if other:
        result.append({"key": "other", "count": other})
    return result


def _graph_integrity(
    graph: Mapping[str, Any], envelope: Mapping[str, Any]
) -> dict[str, Any]:
    """Summarize graph-envelope trust failures with bounded reason codes.

    The receipt deliberately exposes categories and counts, never unknown
    vocabulary, exception prose, or filesystem paths.
    """
    reasons: Counter[str] = Counter()
    omitted = _mapping(envelope.get("omitted"))
    omitted_count = int(omitted.get("nodes") or 0) + int(omitted.get("edges") or 0)
    if not bool(envelope.get("complete")):
        reasons["incomplete_projection"] = max(omitted_count, 1)

    collisions = _mapping(envelope.get("collisions"))
    for kind in ("nodes", "edges"):
        count = int(_mapping(collisions.get(kind)).get("identity_count") or 0)
        if count:
            reasons[f"{kind}_identity_collisions"] = count

    unknowns = _mapping(envelope.get("unknowns"))
    for key in ("node_kinds", "edge_kinds", "relationship_states"):
        count = sum(
            int(_mapping(row).get("count") or 0)
            for row in _list(unknowns.get(key))
        )
        if count:
            reasons[f"unknown_{key}"] = count

    canonical_graph = json.dumps(
        graph, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    source = _mapping(envelope.get("source"))
    if _text(source.get("content_sha256")) != sha256(canonical_graph).hexdigest():
        reasons["source_fingerprint_mismatch"] = 1
    if _text(source.get("declared"), "unknown") != _text(
        graph.get("source"), "unknown"
    ):
        reasons["source_declaration_mismatch"] = 1
    if _text(source.get("graph_schema_version"), "unknown") != _text(
        graph.get("schema_version"), "unknown"
    ):
        reasons["source_schema_mismatch"] = 1
    if _text(envelope.get("generated_at")) != _text(graph.get("generated_at")):
        reasons["source_generation_mismatch"] = 1
    if _text(source.get("freshness_state")) == "stale":
        reasons["source_declared_stale"] = 1

    source_mismatch_count = sum(
        count for reason, count in reasons.items() if reason.startswith("source_")
    )
    reason_rows = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reasons.items())
    ]
    return {
        "ok": not reason_rows,
        "source_match": source_mismatch_count == 0,
        "reasons": reason_rows[:12],
    }


def _dependency_analysis(
    work_ids: set[str],
    context_items: list[Mapping[str, Any]],
    *,
    node_limit: int,
    impact_limit: int,
) -> dict[str, Any]:
    all_work_ids = set(work_ids)
    analysis_limit = min(node_limit, _DEPENDENCY_ANALYSIS_CAP)
    work_ids = set(sorted(all_work_ids)[:analysis_limit])
    dependencies: dict[str, list[str]] = {work_id: [] for work_id in work_ids}
    missing: list[dict[str, str]] = []
    boundary_dependencies: list[dict[str, str]] = []
    for item in context_items:
        work_id = _text(item.get("id"))
        if work_id not in dependencies:
            continue
        for dependency in sorted({_text(value) for value in _list(item.get("depends_on")) if _text(value)}):
            if dependency in work_ids:
                dependencies[work_id].append(dependency)
            elif dependency in all_work_ids:
                boundary_dependencies.append(
                    {"source": work_id, "target": dependency}
                )
            else:
                missing.append({"source": work_id, "target": dependency})

    # Collapse cycles before computing release leverage.  A DFS memoized per
    # node makes A<->B traversal-order dependent (and can even count A as its
    # own downstream row).  Tarjan SCCs turn the dependency graph into a DAG
    # whose unresolved cycle components can be reported once and whose members
    # and downstream rows can be withheld from individual scheduling metrics.
    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def strongconnect(work_id: str) -> None:
        nonlocal next_index
        indices[work_id] = next_index
        lowlinks[work_id] = next_index
        next_index += 1
        stack.append(work_id)
        on_stack.add(work_id)
        for dependency in dependencies[work_id]:
            if dependency not in indices:
                strongconnect(dependency)
                lowlinks[work_id] = min(lowlinks[work_id], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[work_id] = min(lowlinks[work_id], indices[dependency])
        if lowlinks[work_id] == indices[work_id]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == work_id:
                    break
            components.append(tuple(sorted(component)))

    for work_id in sorted(work_ids):
        if work_id not in indices:
            strongconnect(work_id)

    components.sort()
    component_by_node = {
        work_id: component_id
        for component_id, component in enumerate(components)
        for work_id in component
    }
    cyclic_component_ids = {
        component_id
        for component_id, component in enumerate(components)
        if len(component) > 1 or component[0] in dependencies[component[0]]
    }
    cyclic_components = [
        list(components[component_id]) for component_id in sorted(cyclic_component_ids)
    ]
    dependents: dict[str, list[str]] = {work_id: [] for work_id in work_ids}
    for work_id, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            dependents[prerequisite].append(work_id)

    component_dependents: dict[int, set[int]] = {
        component_id: set() for component_id in range(len(components))
    }
    for prerequisite, direct_dependents in dependents.items():
        prerequisite_component = component_by_node[prerequisite]
        for dependent in direct_dependents:
            dependent_component = component_by_node[dependent]
            if dependent_component != prerequisite_component:
                component_dependents[prerequisite_component].add(dependent_component)

    # A row downstream of an unresolved dependency cycle cannot participate in
    # a schedulable critical path or layer yet.  Propagate that taint across the
    # condensation DAG and withhold those derived metrics instead of silently
    # treating the reciprocal prerequisite as resolved.
    def propagate_component_taint(seed_ids: set[int]) -> set[int]:
        tainted_ids = set(seed_ids)
        frontier = list(sorted(seed_ids))
        while frontier:
            component_id = frontier.pop(0)
            for dependent_component in sorted(component_dependents[component_id]):
                if dependent_component in tainted_ids:
                    continue
                tainted_ids.add(dependent_component)
                frontier.append(dependent_component)
        return tainted_ids

    cycle_tainted_component_ids = propagate_component_taint(cyclic_component_ids)
    boundary_seed_component_ids = {
        component_by_node[row["source"]] for row in boundary_dependencies
    }
    boundary_tainted_component_ids = propagate_component_taint(
        boundary_seed_component_ids
    )
    missing_seed_component_ids = {
        component_by_node[row["source"]] for row in missing
    }
    missing_tainted_component_ids = propagate_component_taint(
        missing_seed_component_ids
    )
    withheld_component_ids = (
        cycle_tainted_component_ids
        | boundary_tainted_component_ids
        | missing_tainted_component_ids
    )
    cycle_tainted = {
        work_id
        for component_id in cycle_tainted_component_ids
        for work_id in components[component_id]
    }
    boundary_tainted = {
        work_id
        for component_id in boundary_tainted_component_ids
        for work_id in components[component_id]
    }
    missing_tainted = {
        work_id
        for component_id in missing_tainted_component_ids
        for work_id in components[component_id]
    }
    schedulable_nodes = (
        work_ids - cycle_tainted - boundary_tainted - missing_tainted
    )
    memo_path: dict[str, list[str]] = {}

    def longest_prerequisite_path(work_id: str, visiting: set[str]) -> list[str]:
        if work_id in memo_path:
            return memo_path[work_id]
        if work_id in visiting or work_id not in schedulable_nodes:
            return []
        visiting.add(work_id)
        best = [work_id]
        for dependency in dependencies[work_id]:
            if dependency not in schedulable_nodes:
                continue
            path = longest_prerequisite_path(dependency, visiting)
            candidate = [work_id, *path]
            if len(candidate) > len(best) or (len(candidate) == len(best) and candidate < best):
                best = candidate
        visiting.remove(work_id)
        memo_path[work_id] = best
        return best

    path = max(
        (
            longest_prerequisite_path(work_id, set())
            for work_id in sorted(schedulable_nodes)
        ),
        key=lambda value: (len(value), tuple(reversed(value))),
        default=[],
    )
    # Dependencies point from a row to what must happen first.  Reverse that
    # storage order so the public path reads in execution order.
    critical_path = list(reversed(path))[:node_limit]

    memo_depth: dict[str, int | None] = {}

    def depth(work_id: str, visiting: set[str]) -> int | None:
        if work_id in memo_depth:
            return memo_depth[work_id]
        if work_id in visiting or work_id not in schedulable_nodes:
            memo_depth[work_id] = None
            return None
        visiting.add(work_id)
        prerequisite_depths = [
            value
            for dependency in dependencies[work_id]
            if dependency in schedulable_nodes
            if (value := depth(dependency, visiting)) is not None
        ]
        visiting.remove(work_id)
        result = 0 if not prerequisite_depths else max(prerequisite_depths) + 1
        memo_depth[work_id] = result
        return result

    layers: dict[int, list[str]] = {}
    for work_id in sorted(schedulable_nodes):
        value = depth(work_id, set())
        if value is not None:
            layers.setdefault(value, []).append(work_id)
    layer_rows = [
        {"depth": value, "count": len(ids), "ids": ids[:node_limit]}
        for value, ids in sorted(layers.items())
    ]

    impact_memo: dict[int, set[int]] = {}

    def downstream_components(component_id: int) -> set[int]:
        if component_id in impact_memo:
            return impact_memo[component_id]
        reached: set[int] = set()
        for dependent_component in sorted(component_dependents[component_id]):
            reached.add(dependent_component)
            reached.update(downstream_components(dependent_component))
        impact_memo[component_id] = reached
        return reached

    impact = []
    for work_id in sorted(work_ids):
        component_id = component_by_node[work_id]
        if component_id in withheld_component_ids:
            continue
        downstream_count = sum(
            len(components[downstream_id])
            for downstream_id in downstream_components(component_id)
        )
        impact.append(
            {
                "id": work_id,
                "downstream": downstream_count,
                "cyclic": component_id in cyclic_component_ids,
                "component_size": len(components[component_id]),
                "cycle_tainted": False,
            }
        )
    impact.sort(key=lambda item: (-item["downstream"], item["id"]))
    cycle_impact = []
    for component_id in sorted(cyclic_component_ids):
        members = list(components[component_id])
        downstream_count = sum(
            len(components[downstream_id])
            for downstream_id in downstream_components(component_id)
        )
        cycle_impact.append(
            {
                "members": members[:node_limit],
                "members_total": len(members),
                "members_truncated": len(members) > node_limit,
                "downstream_after_component": downstream_count,
            }
        )
    bounded_cycle_impact = cycle_impact[:impact_limit]
    cycle_tainted_rows = sorted(cycle_tainted)
    boundary_tainted_rows = sorted(boundary_tainted)
    missing_tainted_rows = sorted(missing_tainted)
    unresolved_tainted_rows = sorted(boundary_tainted | missing_tainted)
    cycle_member_ids_total = sum(row["members_total"] for row in cycle_impact)
    cycle_member_ids_emitted = sum(len(row["members"]) for row in bounded_cycle_impact)
    if not cyclic_component_ids:
        if len(all_work_ids) > len(work_ids):
            topology_metrics_status = (
                "partial_population" if schedulable_nodes else "withheld_population"
            )
        elif boundary_tainted_rows or missing_tainted_rows:
            topology_metrics_status = (
                "partial_unresolved" if schedulable_nodes else "withheld_unresolved"
            )
        else:
            topology_metrics_status = "available"
    elif schedulable_nodes:
        topology_metrics_status = "partial_cycle"
    else:
        topology_metrics_status = "withheld_cycle"

    return {
        "dependencies": dependencies,
        "_missing_sources": {row["source"] for row in missing},
        "_analysis_boundary_sources": {
            row["source"] for row in boundary_dependencies
        },
        "_schedulable_nodes": schedulable_nodes,
        "analysis_population_total": len(all_work_ids),
        "analysis_population_emitted": len(work_ids),
        "analysis_population_omitted": len(all_work_ids) - len(work_ids),
        "analysis_population_truncated": len(all_work_ids) > len(work_ids),
        "analysis_boundary_dependencies": boundary_dependencies[:node_limit],
        "analysis_boundary_dependencies_total": len(boundary_dependencies),
        "analysis_boundary_dependencies_emitted": min(
            len(boundary_dependencies), node_limit
        ),
        "analysis_boundary_dependencies_truncated": len(boundary_dependencies)
        > node_limit,
        "analysis_boundary_tainted": boundary_tainted_rows[:node_limit],
        "analysis_boundary_tainted_total": len(boundary_tainted_rows),
        "analysis_boundary_tainted_emitted": min(
            len(boundary_tainted_rows), node_limit
        ),
        "analysis_boundary_tainted_truncated": len(boundary_tainted_rows)
        > node_limit,
        "missing_dependencies": missing[:node_limit],
        "missing_dependencies_total": len(missing),
        "missing_dependencies_emitted": min(len(missing), node_limit),
        "missing_dependencies_truncated": len(missing) > node_limit,
        "missing_dependency_tainted": missing_tainted_rows[:node_limit],
        "missing_dependency_tainted_total": len(missing_tainted_rows),
        "missing_dependency_tainted_emitted": min(
            len(missing_tainted_rows), node_limit
        ),
        "missing_dependency_tainted_truncated": len(missing_tainted_rows)
        > node_limit,
        "unresolved_tainted": unresolved_tainted_rows[:node_limit],
        "unresolved_tainted_total": len(unresolved_tainted_rows),
        "unresolved_tainted_emitted": min(
            len(unresolved_tainted_rows), node_limit
        ),
        "unresolved_tainted_truncated": len(unresolved_tainted_rows)
        > node_limit,
        "cycles": [
            list(component[:node_limit]) for component in cyclic_components[:node_limit]
        ],
        "cycle_tainted": cycle_tainted_rows[:node_limit],
        "cycle_tainted_total": len(cycle_tainted_rows),
        "cycle_tainted_emitted": min(len(cycle_tainted_rows), node_limit),
        "cycle_tainted_truncated": len(cycle_tainted_rows) > node_limit,
        "cycle_impact": bounded_cycle_impact,
        "cycle_components_total": len(cycle_impact),
        "cycle_components_emitted": len(bounded_cycle_impact),
        "cycle_impact_truncated": len(cycle_impact) > impact_limit,
        "cycle_member_ids_total": cycle_member_ids_total,
        "cycle_member_ids_emitted": cycle_member_ids_emitted,
        "cycle_member_ids_truncated": cycle_member_ids_emitted
        < cycle_member_ids_total,
        "topology_metrics_status": topology_metrics_status,
        "critical_path": critical_path,
        "layers": layer_rows[:node_limit],
        "impact": impact[:impact_limit],
        "max_parallel_width": max((row["count"] for row in layer_rows), default=0),
    }


def build_operations(
    snapshot: Mapping[str, Any],
    graph: Mapping[str, Any],
    context: Mapping[str, Any],
    timeline: Mapping[str, Any] | None,
    *,
    node_limit: int = 400,
    activity_limit: int = 120,
    impact_limit: int = 12,
) -> dict[str, Any]:
    """Derive a bounded operational document from one coherent board set."""
    if node_limit < 1 or activity_limit < 1 or impact_limit < 1:
        raise ValueError("operation limits must be positive")

    rows = [_mapping(row) for row in _list(snapshot.get("rows"))]
    # `bucket` is the hierarchy surface for work rows (initiative/job/task),
    # while tracked runtime projections also use the literal bucket `job`.
    # Their explicit `job:` identity namespace is the non-ambiguous boundary.
    work_rows = [row for row in rows if not _text(row.get("id")).startswith("job:")]
    job_rows = [row for row in rows if _text(row.get("id")).startswith("job:")]
    row_by_id = {_text(row.get("id")): row for row in work_rows if _text(row.get("id"))}
    work_ids = set(row_by_id)

    sessions = [_mapping(row) for row in _list(snapshot.get("sessions"))]
    graph_nodes = [_mapping(node) for node in _list(graph.get("nodes"))]
    graph_edges = [_mapping(edge) for edge in _list(graph.get("edges"))]
    graph_envelope = build_graph_envelope(
        graph,
        node_cap=node_limit,
        edge_cap=max(node_limit * 4, 1),
        source_stale=bool(snapshot.get("stale")),
    )
    graph_integrity = _graph_integrity(graph, graph_envelope)
    context_items = [_mapping(item) for item in _list(context.get("items"))]
    context_by_id = {
        _text(item.get("id")): item for item in context_items if _text(item.get("id"))
    }

    timeline_items = [_mapping(item) for item in _list(_mapping(timeline).get("items"))]
    activity: list[dict[str, str]] = []
    for item in timeline_items:
        work_id = _text(item.get("id"))
        if work_id not in work_ids:
            continue
        for event in _list(item.get("events")):
            public_event = _mapping(event)
            at = _text(public_event.get("at"))
            if _timestamp(at) is None:
                continue
            # Copy exactly the occurrence-only fields.  Even if a future input
            # accidentally contains prose, it has no route into this document.
            activity.append(
                {
                    "id": work_id,
                    "at": at,
                    "kind": _text(public_event.get("kind"), "event"),
                    "actor": _text(public_event.get("actor"), "unknown"),
                }
            )
    activity.sort(key=lambda event: (event["at"], event["id"], event["kind"]))
    recorded_events = len(activity)
    recent_activity = activity[-activity_limit:]

    generated_at = _text(snapshot.get("generated_at"))
    generated_ts = _timestamp(generated_at)
    recent_24h = 0
    future_events: list[dict[str, str]] = []
    if generated_ts is not None:
        future_events = [
            event
            for event in activity
            if (event_ts := _timestamp(event["at"])) is not None
            and event_ts > generated_ts + 1.0
        ]
        recent_24h = sum(
            1
            for event in activity
            if (event_ts := _timestamp(event["at"])) is not None
            and 0 <= generated_ts - event_ts <= 86_400
        )

    dependency = _dependency_analysis(
        work_ids,
        context_items,
        node_limit=node_limit,
        impact_limit=impact_limit,
    )

    statuses = {_text(row.get("id")): _text(row.get("status")).lower() for row in work_rows}
    dependency_clear = []
    for work_id, prerequisites in dependency["dependencies"].items():
        status = statuses.get(work_id, "")
        if status in _DONE | _RUNNING | _ATTENTION:
            continue
        if work_id not in dependency["_schedulable_nodes"]:
            continue
        if work_id in dependency["_missing_sources"]:
            continue
        if work_id in dependency["_analysis_boundary_sources"]:
            continue
        if all(statuses.get(prerequisite, "") in _DONE for prerequisite in prerequisites):
            dependency_clear.append(work_id)

    expired_claims: list[str] = []
    expiring_claims: list[str] = []
    blocked_without_resume: list[str] = []
    done_without_artifact: list[str] = []
    for work_id, row in row_by_id.items():
        item = context_by_id.get(work_id, {})
        remaining = item.get("lease_remaining_s")
        if item.get("claim_present") and isinstance(remaining, int):
            if remaining < 0:
                expired_claims.append(work_id)
            elif remaining < 300:
                expiring_claims.append(work_id)
        status = _text(row.get("status")).lower()
        if status in _ATTENTION and not (
            _text(item.get("resume_when")) or _text(item.get("next_step"))
        ):
            blocked_without_resume.append(work_id)
        if status in _DONE and item and not bool(item.get("artifact_recorded")):
            done_without_artifact.append(work_id)

    missing_graph_targets = sorted(
        {
            _text(edge.get("target"))
            for edge in graph_edges
            if _text(edge.get("relationship_state")) != "source_bound"
            and _text(edge.get("target"))
        }
    )
    stale_rows = sorted(_text(row.get("id")) for row in rows if bool(row.get("stale")))

    document_times = {
        name: _timestamp(_text(document.get("generated_at")))
        for name, document in (
            ("snapshot", snapshot),
            ("graph", graph),
            ("context", context),
            ("timeline", _mapping(timeline)),
        )
    }
    present_times = [value for value in document_times.values() if value is not None]
    skew = max(present_times) - min(present_times) if present_times else 0.0
    documents = [
        {
            "name": name,
            "generated_at": _text(document.get("generated_at")),
            "schema_version": _text(document.get("schema_version")),
            "present": document_times[name] is not None,
        }
        for name, document in (
            ("snapshot", snapshot),
            ("graph", graph),
            ("context", context),
            ("timeline", _mapping(timeline)),
        )
    ]

    signals = [
        {
            "key": "future_events",
            "severity": "error",
            "count": len(future_events),
            "label": "Recorded events occur after this document's read clock",
        },
        {
            "key": "dependency_analysis_scope",
            "severity": "warning",
            "count": dependency["analysis_population_omitted"],
            "label": "Work rows are outside the bounded dependency analysis",
        },
        {
            "key": "dependency_analysis_boundary",
            "severity": "warning",
            "count": dependency["analysis_boundary_dependencies_total"],
            "label": "Published rows depend on work outside the analysis boundary",
        },
        {
            "key": "missing_dependencies",
            "severity": "error",
            "count": dependency["missing_dependencies_total"],
            "label": "Published rows name prerequisites absent from the board",
        },
        {
            "key": "graph_integrity",
            "severity": "error",
            "count": sum(row["count"] for row in graph_integrity["reasons"]),
            "label": "Graph envelope is incomplete or not source-faithful",
        },
        {
            "key": "missing_targets",
            "severity": "error",
            "count": len(missing_graph_targets),
            "label": "Referenced nodes are absent",
        },
        {
            "key": "cycles",
            "severity": "error",
            "count": dependency["cycle_components_total"],
            "label": "Dependency cycles need resolution",
        },
        {
            "key": "expired_claims",
            "severity": "warning",
            "count": len(expired_claims),
            "label": "Claims have passed their lease",
        },
        {
            "key": "expiring_claims",
            "severity": "warning",
            "count": len(expiring_claims),
            "label": "Claims expire within five minutes",
        },
        {
            "key": "blocked_without_resume",
            "severity": "warning",
            "count": len(blocked_without_resume),
            "label": "Attention rows lack a recorded next condition",
        },
        {
            "key": "done_without_artifact",
            "severity": "error",
            "count": len(done_without_artifact),
            "label": "Done rows lack a recorded artifact",
        },
        {
            "key": "stale_rows",
            "severity": "warning",
            "count": len(stale_rows),
            "label": "Job projections are stale",
        },
    ]

    healthy = not any(signal["count"] for signal in signals if signal["severity"] == "error")
    edge_kinds = _bounded_counts(_text(edge.get("kind"), "unknown") for edge in graph_edges)
    event_kinds = _bounded_counts(event["kind"] for event in activity)
    graph_admission = _mapping(graph_envelope.get("admission"))
    topology_availability = {
        "schema_version": "TopologyAvailabilityV1",
        "state": _text(graph_admission.get("state"), "low_information"),
        "reason_code": _text(
            graph_admission.get("reason_code"),
            "topology_admission_receipt_absent",
        ),
        "reason": _text(
            graph_admission.get("reason"),
            "No topology admission receipt is available.",
        ),
        "missing_prerequisite": (
            str(graph_admission.get("missing_prerequisite") or "").strip()
            if graph_admission
            else "graph_topology_admission_receipt"
        ),
        "population": {
            "work_items": len(work_rows),
            "nodes": graph_envelope["population"]["nodes"],
            "edges": graph_envelope["population"]["edges"],
            "events": recorded_events,
        },
        "admitted": {
            "work_items": dependency["analysis_population_emitted"],
            "nodes": graph_envelope["emitted"]["nodes"],
            "edges": graph_envelope["emitted"]["edges"],
            "events": len(recent_activity),
        },
        "omitted": {
            "work_items": dependency["analysis_population_omitted"],
            "nodes": graph_envelope["omitted"]["nodes"],
            "edges": graph_envelope["omitted"]["edges"],
            "events": max(0, recorded_events - len(recent_activity)),
            "node_reasons": graph_envelope["omitted"]["node_reasons"],
            "edge_reasons": graph_envelope["omitted"]["edge_reasons"],
            "event_reasons": (
                [
                    {
                        "reason": "activity_limit",
                        "count": max(0, recorded_events - len(recent_activity)),
                    }
                ]
                if recorded_events > len(recent_activity)
                else []
            ),
        },
        "source": {
            "operations": "derived:snapshot+graph+context+timeline",
            "graph_declared": _text(graph.get("source"), "unknown"),
            "graph_schema_version": _text(graph.get("schema_version"), "unknown"),
            "graph_generated_at": _text(graph.get("generated_at")),
            "graph_content_sha256": _text(
                _mapping(graph_envelope.get("source")).get("content_sha256")
            ),
        },
        "freshness": {
            "state": (
                "stale"
                if bool(snapshot.get("stale"))
                else "current"
                if generated_ts is not None
                else "unknown"
            ),
            "stale": bool(snapshot.get("stale")),
            "generated_at": generated_at,
            "document_skew_seconds": round(skew, 6),
        },
    }

    return {
        "schema_version": OPERATIONS_SCHEMA,
        "generated_at": generated_at,
        "source": "derived:snapshot+graph+context+timeline",
        "stale": bool(snapshot.get("stale")),
        "scope": {
            "total_work": len(work_rows),
            "included_work": dependency["analysis_population_emitted"],
            "node_limit": node_limit,
            "dependency_analysis_cap": _DEPENDENCY_ANALYSIS_CAP,
            "activity_limit": activity_limit,
            "truncated": dependency["analysis_population_truncated"],
        },
        "metrics": {
            "rows": len(rows),
            "work_items": len(work_rows),
            "job_projections": len(job_rows),
            "sessions": len(sessions),
            "live_sessions": sum(1 for session in sessions if bool(session.get("live"))),
            "graph_nodes": len(graph_nodes),
            "graph_edges": len(graph_edges),
            "graph_nodes_emitted": graph_envelope["emitted"]["nodes"],
            "graph_edges_emitted": graph_envelope["emitted"]["edges"],
            "recorded_events": recorded_events,
            "events_24h": recent_24h,
            "dependency_clear_planned": len(dependency_clear),
            "critical_path_steps": len(dependency["critical_path"]),
            "max_parallel_width": dependency["max_parallel_width"],
        },
        "health": {
            "ok": healthy,
            "document_skew_seconds": round(skew, 6),
            "graph_integrity": graph_integrity,
            "missing_targets": missing_graph_targets[:node_limit],
            "cycles": dependency["cycles"],
            "expired_claims": expired_claims[:node_limit],
            "expiring_claims": expiring_claims[:node_limit],
            "blocked_without_resume": blocked_without_resume[:node_limit],
            "done_without_artifact": done_without_artifact[:node_limit],
            "future_events": sorted({event["id"] for event in future_events})[
                :node_limit
            ],
            "stale_rows": stale_rows[:node_limit],
            "signals": signals,
        },
        "documents": documents,
        "distribution": {
            "statuses": _bounded_counts(_text(row.get("status"), "planned").lower() for row in rows),
            "lanes": _bounded_counts(_lane(row.get("owner")) for row in work_rows),
            "modules": _bounded_counts(_text(row.get("module"), "unassigned") for row in rows),
            "node_kinds": _bounded_counts(_text(node.get("kind"), "unknown") for node in graph_nodes),
            "edge_kinds": edge_kinds,
            "event_kinds": event_kinds,
        },
        "execution": {
            "topology_metrics_status": dependency["topology_metrics_status"],
            "analysis_population_total": dependency["analysis_population_total"],
            "analysis_population_emitted": dependency[
                "analysis_population_emitted"
            ],
            "analysis_population_omitted": dependency[
                "analysis_population_omitted"
            ],
            "analysis_population_truncated": dependency[
                "analysis_population_truncated"
            ],
            "analysis_boundary_dependencies": dependency[
                "analysis_boundary_dependencies"
            ],
            "analysis_boundary_dependencies_total": dependency[
                "analysis_boundary_dependencies_total"
            ],
            "analysis_boundary_dependencies_emitted": dependency[
                "analysis_boundary_dependencies_emitted"
            ],
            "analysis_boundary_dependencies_truncated": dependency[
                "analysis_boundary_dependencies_truncated"
            ],
            "analysis_boundary_tainted": dependency[
                "analysis_boundary_tainted"
            ],
            "analysis_boundary_tainted_total": dependency[
                "analysis_boundary_tainted_total"
            ],
            "analysis_boundary_tainted_emitted": dependency[
                "analysis_boundary_tainted_emitted"
            ],
            "analysis_boundary_tainted_truncated": dependency[
                "analysis_boundary_tainted_truncated"
            ],
            "missing_dependencies_total": dependency[
                "missing_dependencies_total"
            ],
            "missing_dependencies_emitted": dependency[
                "missing_dependencies_emitted"
            ],
            "missing_dependencies_truncated": dependency[
                "missing_dependencies_truncated"
            ],
            "missing_dependency_tainted": dependency[
                "missing_dependency_tainted"
            ],
            "missing_dependency_tainted_total": dependency[
                "missing_dependency_tainted_total"
            ],
            "missing_dependency_tainted_emitted": dependency[
                "missing_dependency_tainted_emitted"
            ],
            "missing_dependency_tainted_truncated": dependency[
                "missing_dependency_tainted_truncated"
            ],
            "unresolved_tainted": dependency["unresolved_tainted"],
            "unresolved_tainted_total": dependency[
                "unresolved_tainted_total"
            ],
            "unresolved_tainted_emitted": dependency[
                "unresolved_tainted_emitted"
            ],
            "unresolved_tainted_truncated": dependency[
                "unresolved_tainted_truncated"
            ],
            "cycle_tainted": dependency["cycle_tainted"],
            "cycle_tainted_total": dependency["cycle_tainted_total"],
            "cycle_tainted_emitted": dependency["cycle_tainted_emitted"],
            "cycle_tainted_truncated": dependency["cycle_tainted_truncated"],
            "cycle_impact": dependency["cycle_impact"],
            "cycle_components_total": dependency["cycle_components_total"],
            "cycle_components_emitted": dependency["cycle_components_emitted"],
            "cycle_impact_truncated": dependency["cycle_impact_truncated"],
            "cycle_member_ids_total": dependency["cycle_member_ids_total"],
            "cycle_member_ids_emitted": dependency["cycle_member_ids_emitted"],
            "cycle_member_ids_truncated": dependency[
                "cycle_member_ids_truncated"
            ],
            "critical_path": dependency["critical_path"],
            "layers": dependency["layers"],
            "impact": dependency["impact"],
            "dependency_clear_planned": dependency_clear[:node_limit],
            "missing_dependencies": dependency["missing_dependencies"],
        },
        "topology_availability": topology_availability,
        "graph_envelope": graph_envelope,
        "activity": recent_activity,
    }


__all__ = ["OPERATIONS_SCHEMA", "build_operations"]
