"""Neutral, bounded graph envelope for public explorer surfaces.

The raw graph is already safe and source-bound, but a renderer also needs to
know what it did *not* receive.  GraphEnvelopeV1 preserves the population,
eligibility, omission, collision, and cap accounting around a deterministic
subset.  Duplicate identities are quarantined rather than silently merged.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from typing import Any, Mapping


GRAPH_ENVELOPE_SCHEMA = "GraphEnvelopeV1"
_NODE_FIELDS = ("id", "kind", "label", "status", "missing")
_EDGE_FIELDS = (
    "id",
    "source",
    "target",
    "kind",
    "source_field",
    "relationship_state",
)
_KNOWN_NODE_KINDS = {"work", "job", "artifact", "missing_work"}
_KNOWN_EDGE_KINDS = {"parent", "depends_on", "evidence", "runtime_evidence"}
_KNOWN_RELATIONSHIP_STATES = {"source_bound", "missing_target"}

# A status that means the work is finished. Everything else -- including a
# status this file has never seen -- counts as open, because an unrecognised
# status is exactly the case where guessing "finished" loses the row.
_TERMINAL_STATUSES = {"done", "archived", "closed", "superseded", "cancelled"}
# Within the open reserve, the rows an operator is most likely to be looking
# for. Anything open but unlisted sorts after these, ahead of finished work.
_FRONTIER_RANK = {
    "running": 0,
    "failed": 1,
    "blocked": 2,
    "attention": 3,
    "review": 4,
    "queued": 5,
    "paused": 6,
    "planned": 7,
}
_FRONTIER_RANK_DEFAULT = 8


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    rendered = str(value).strip()
    return rendered or fallback


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _public_node(value: Mapping[str, Any]) -> dict[str, Any]:
    node = {key: value.get(key) for key in _NODE_FIELDS}
    return {
        "id": _text(node["id"]),
        "kind": _text(node["kind"], "unknown"),
        "label": _text(node["label"], _text(node["id"])),
        "status": _text(node["status"]),
        "missing": bool(node["missing"]),
    }


def _public_edge(value: Mapping[str, Any]) -> dict[str, str]:
    edge = {key: value.get(key) for key in _EDGE_FIELDS}
    return {
        "id": _text(edge["id"]),
        "source": _text(edge["source"]),
        "target": _text(edge["target"]),
        "kind": _text(edge["kind"], "unknown"),
        "source_field": _text(edge["source_field"]),
        "relationship_state": _text(edge["relationship_state"], "unknown"),
    }


def _reason_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counter.items())
        if count
    ]


def _collision_ids(values: list[Mapping[str, Any]], *, limit: int) -> tuple[set[str], dict[str, Any]]:
    counts = Counter(_text(value.get("id")) for value in values if _text(value.get("id")))
    duplicated = {identity for identity, count in counts.items() if count > 1}
    ordered = sorted(duplicated)
    return duplicated, {
        "identity_count": len(ordered),
        "entry_count": sum(counts[identity] for identity in ordered),
        "ids": ordered[:limit],
        "truncated": len(ordered) > limit,
    }


def build_graph_envelope(
    graph: Mapping[str, Any],
    *,
    node_cap: int = 400,
    edge_cap: int = 1_200,
    byte_cap: int = 1_000_000,
    collision_receipt_limit: int = 50,
    source_stale: bool = False,
    open_reserve_ratio: float = 0.5,
) -> dict[str, Any]:
    """Build one deterministic subset with complete accounting around it."""
    if min(node_cap, edge_cap, byte_cap, collision_receipt_limit) < 1:
        raise ValueError("graph envelope limits must be positive")

    raw_nodes = [_mapping(value) for value in _list(graph.get("nodes"))]
    raw_edges = [_mapping(value) for value in _list(graph.get("edges"))]
    source_fingerprint = sha256(_canonical_bytes(graph)).hexdigest()

    duplicate_node_ids, node_collisions = _collision_ids(
        raw_nodes, limit=collision_receipt_limit
    )
    duplicate_edge_ids, edge_collisions = _collision_ids(
        raw_edges, limit=collision_receipt_limit
    )

    node_omissions: Counter[str] = Counter()
    edge_omissions: Counter[str] = Counter()
    unknown_node_kinds: Counter[str] = Counter()
    unknown_edge_kinds: Counter[str] = Counter()
    unknown_relationship_states: Counter[str] = Counter()

    eligible_nodes: list[dict[str, Any]] = []
    for raw in raw_nodes:
        identity = _text(raw.get("id"))
        if not identity:
            node_omissions["missing_identity"] += 1
            continue
        if identity in duplicate_node_ids:
            node_omissions["duplicate_identity"] += 1
            continue
        node = _public_node(raw)
        if node["kind"] not in _KNOWN_NODE_KINDS:
            unknown_node_kinds[node["kind"]] += 1
        eligible_nodes.append(node)
    # Admission order decides what a capped envelope IS, so it cannot be the
    # id. Ids carry a type prefix ("artifact:", "job:", "session:", "work:"),
    # and sorting by them alphabetically fills the whole cap with whichever
    # type sorts first: on a real board of 8,635 rows the 400-node envelope
    # came back 400 artifacts, no work, and every one of 9,789 edges dropped
    # as endpoint_node_capped, because an edge needs both ends admitted. A
    # 47-row demo never shows this — it has fewer nodes than the cap.
    #
    # Degree first keeps the connected core: the nodes that hold the graph
    # together survive the cap, and the edges between them survive with them.
    # Ties break on id so the envelope stays deterministic.
    degree: Counter[str] = Counter()
    for raw in raw_edges:
        edge = _public_edge(raw)
        if edge["source"]:
            degree[edge["source"]] += 1
        if edge["target"]:
            degree[edge["target"]] += 1
    #
    # Degree alone is still the wrong answer for an operations surface. On the
    # same 8,635-row board it returned 400 nodes of which 358 were done,
    # archived or closed -- 89.5% finished work -- and admitted none of the
    # running rows, because a job that started an hour ago has not had time to
    # accumulate edges. The one thing an operations mesh exists to show was the
    # one thing the cap removed.
    #
    # So the cap is split. Open work holds a reserved share, ordered by how
    # much it is likely to be looked for (running before blocked before
    # queued) and then by degree; the remainder goes to the connected core as
    # before. An unused reserve is not wasted -- it falls through to the core.
    def _open(node: Mapping[str, Any]) -> bool:
        # Only work carries a lifecycle. An artifact has no status, and
        # counting one as open would have inflated "open_eligible" from the
        # 8,532 that are actually work items to include every piece of
        # evidence on the board. Artifacts still reach the envelope -- they
        # arrive as neighbours of what the reserve admitted, which is the
        # relationship that makes them worth drawing.
        if _text(node.get("kind")) not in {"work", "job", "missing_work"}:
            return False
        return _text(node.get("status")).lower() not in _TERMINAL_STATUSES

    def _core_key(node: Mapping[str, Any]) -> tuple[int, str]:
        return (-degree[node["id"]], node["id"])

    def _frontier_key(node: Mapping[str, Any]) -> tuple[int, int, str]:
        status = _text(node.get("status")).lower()
        return (
            _FRONTIER_RANK.get(status, _FRONTIER_RANK_DEFAULT),
            -degree[node["id"]],
            node["id"],
        )

    open_nodes = sorted((node for node in eligible_nodes if _open(node)), key=_frontier_key)
    closed_nodes = sorted((node for node in eligible_nodes if not _open(node)), key=_core_key)
    open_reserve = min(len(open_nodes), int(node_cap * open_reserve_ratio))
    reserved = open_nodes[:open_reserve]
    reserved_ids = {node["id"] for node in reserved}
    # Fill the rest by how much a node connects to what was just reserved,
    # before falling back to global degree. Ranking the remainder by global
    # degree alone scattered the admitted set across the graph and left it
    # with 192 edges for 400 nodes; the neighbours of live work are both
    # better connected to it and the thing an operator is actually asking
    # about -- what this running job waits on, and what waits on it.
    adjacency: Counter[str] = Counter()
    for raw in raw_edges:
        edge = _public_edge(raw)
        if edge["source"] in reserved_ids and edge["target"]:
            adjacency[edge["target"]] += 1
        if edge["target"] in reserved_ids and edge["source"]:
            adjacency[edge["source"]] += 1

    def _fill_key(node: Mapping[str, Any]) -> tuple[int, int, str]:
        return (-adjacency[node["id"]], -degree[node["id"]], node["id"])

    remainder = sorted(
        [node for node in open_nodes[open_reserve:]] + closed_nodes, key=_fill_key
    )
    eligible_nodes = reserved + remainder
    eligible_node_ids = {node["id"] for node in eligible_nodes}

    eligible_edges: list[dict[str, str]] = []
    for raw in raw_edges:
        identity = _text(raw.get("id"))
        if not identity:
            edge_omissions["missing_identity"] += 1
            continue
        if identity in duplicate_edge_ids:
            edge_omissions["duplicate_identity"] += 1
            continue
        edge = _public_edge(raw)
        if not edge["source"] or not edge["target"]:
            edge_omissions["missing_endpoint_identity"] += 1
            continue
        if edge["source"] not in eligible_node_ids:
            reason = (
                "quarantined_source"
                if edge["source"] in duplicate_node_ids
                else "absent_source"
            )
            edge_omissions[reason] += 1
            continue
        if edge["target"] not in eligible_node_ids:
            reason = (
                "quarantined_target"
                if edge["target"] in duplicate_node_ids
                else "absent_target"
            )
            edge_omissions[reason] += 1
            continue
        if edge["kind"] not in _KNOWN_EDGE_KINDS:
            unknown_edge_kinds[edge["kind"]] += 1
        if edge["relationship_state"] not in _KNOWN_RELATIONSHIP_STATES:
            unknown_relationship_states[edge["relationship_state"]] += 1
        eligible_edges.append(edge)
    eligible_edges.sort(key=lambda edge: edge["id"])

    emitted_nodes = eligible_nodes[:node_cap]
    if len(eligible_nodes) > node_cap:
        node_omissions["node_cap"] += len(eligible_nodes) - node_cap
    emitted_node_ids = {node["id"] for node in emitted_nodes}

    endpoint_eligible_edges = []
    for edge in eligible_edges:
        if edge["source"] not in emitted_node_ids or edge["target"] not in emitted_node_ids:
            edge_omissions["endpoint_node_capped"] += 1
            continue
        endpoint_eligible_edges.append(edge)
    emitted_edges = endpoint_eligible_edges[:edge_cap]
    if len(endpoint_eligible_edges) > edge_cap:
        edge_omissions["edge_cap"] += len(endpoint_eligible_edges) - edge_cap

    def payload_size() -> int:
        return len(_canonical_bytes({"nodes": emitted_nodes, "edges": emitted_edges}))

    while emitted_edges and payload_size() > byte_cap:
        emitted_edges.pop()
        edge_omissions["byte_cap"] += 1
    while emitted_nodes and payload_size() > byte_cap:
        emitted_nodes.pop()
        node_omissions["byte_cap"] += 1
        surviving_ids = {node["id"] for node in emitted_nodes}
        retained_edges = []
        for edge in emitted_edges:
            if edge["source"] in surviving_ids and edge["target"] in surviving_ids:
                retained_edges.append(edge)
            else:
                edge_omissions["endpoint_node_byte_capped"] += 1
        emitted_edges = retained_edges

    emitted_bytes = payload_size()
    omitted_nodes = sum(node_omissions.values())
    omitted_edges = sum(edge_omissions.values())

    if emitted_edges:
        admission_state = "available"
        admission_reason_code = "authoritative_relationships_admitted"
        admission_reason = (
            "Authoritative graph relationships are admitted for topology rendering."
        )
        missing_prerequisite = ""
    elif not raw_edges:
        admission_state = "low_information"
        admission_reason_code = "authoritative_relationships_absent"
        admission_reason = (
            "The authoritative graph publishes no relationships; topology motion "
            "and connectivity must not be inferred."
        )
        missing_prerequisite = "authoritative_graph_relationships"
    elif not eligible_edges:
        admission_state = "low_information"
        admission_reason_code = "authoritative_relationships_ineligible"
        admission_reason = (
            "Authoritative graph relationships exist, but none pass identity and "
            "endpoint admission."
        )
        missing_prerequisite = "eligible_authoritative_graph_relationships"
    else:
        admission_state = "low_information"
        admission_reason_code = "authoritative_relationships_not_admitted"
        admission_reason = (
            "Eligible authoritative graph relationships exist, but none fit the "
            "bounded envelope."
        )
        missing_prerequisite = "admitted_authoritative_graph_relationships"

    return {
        "schema_version": GRAPH_ENVELOPE_SCHEMA,
        "generated_at": _text(graph.get("generated_at")),
        "source": {
            "declared": _text(graph.get("source"), "unknown"),
            "graph_schema_version": _text(graph.get("schema_version"), "unknown"),
            "content_sha256": source_fingerprint,
            "freshness_state": "stale" if source_stale else "not_declared_stale",
        },
        "population": {
            "nodes": len(raw_nodes),
            "edges": len(raw_edges),
        },
        "eligible": {
            "nodes": len(eligible_nodes),
            "edges": len(eligible_edges),
        },
        "emitted": {
            "nodes": len(emitted_nodes),
            "edges": len(emitted_edges),
            "bytes": emitted_bytes,
        },
        "omitted": {
            "nodes": omitted_nodes,
            "edges": omitted_edges,
            "node_reasons": _reason_rows(node_omissions),
            "edge_reasons": _reason_rows(edge_omissions),
        },
        "collisions": {
            "nodes": node_collisions,
            "edges": edge_collisions,
        },
        "unknowns": {
            "node_kinds": _reason_rows(unknown_node_kinds),
            "edge_kinds": _reason_rows(unknown_edge_kinds),
            "relationship_states": _reason_rows(unknown_relationship_states),
        },
        "caps": {
            "nodes": node_cap,
            "edges": edge_cap,
            "bytes": byte_cap,
        },
        "frontier": {
            "rule": (
                "open work holds a reserved share of the node cap, ordered by "
                "status urgency then degree; the remainder is filled by degree"
            ),
            "open_reserve_ratio": open_reserve_ratio,
            "open_reserve": open_reserve,
            "open_eligible": len(open_nodes),
            "open_emitted": sum(
                1 for node in emitted_nodes if _text(node.get("status")).lower() not in _TERMINAL_STATUSES
            ),
            "open_omitted": len(open_nodes) - sum(
                1 for node in emitted_nodes if _text(node.get("status")).lower() not in _TERMINAL_STATUSES
            ),
            "reserved_admitted": sum(1 for node in emitted_nodes if node["id"] in reserved_ids),
        },
        "admission": {
            "state": admission_state,
            "reason_code": admission_reason_code,
            "reason": admission_reason,
            "missing_prerequisite": missing_prerequisite,
            "population": {
                "nodes": len(raw_nodes),
                "edges": len(raw_edges),
            },
            "eligible": {
                "nodes": len(eligible_nodes),
                "edges": len(eligible_edges),
            },
            "admitted": {
                "nodes": len(emitted_nodes),
                "edges": len(emitted_edges),
            },
            "omitted": {
                "nodes": omitted_nodes,
                "edges": omitted_edges,
                "node_reasons": _reason_rows(node_omissions),
                "edge_reasons": _reason_rows(edge_omissions),
            },
            "source": {
                "declared": _text(graph.get("source"), "unknown"),
                "graph_schema_version": _text(
                    graph.get("schema_version"), "unknown"
                ),
                "generated_at": _text(graph.get("generated_at")),
                "content_fingerprint_ref": "source.content_sha256",
            },
            "freshness": {
                "state": "stale" if source_stale else "not_declared_stale",
                "stale": source_stale,
            },
        },
        "complete": omitted_nodes == 0 and omitted_edges == 0,
        "nodes": emitted_nodes,
        "edges": emitted_edges,
    }


__all__ = ["GRAPH_ENVELOPE_SCHEMA", "build_graph_envelope"]
