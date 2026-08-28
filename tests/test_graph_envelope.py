from __future__ import annotations

from hashlib import sha256
import json

import pytest

from coordharness.board.graph_envelope import GRAPH_ENVELOPE_SCHEMA, build_graph_envelope


def _node(node_id: str, **overrides: object) -> dict[str, object]:
    node: dict[str, object] = {
        "id": node_id,
        "kind": "work",
        "label": node_id,
        "status": "planned",
        "missing": False,
    }
    node.update(overrides)
    return node


def _edge(edge_id: str, source: str, target: str, **overrides: object) -> dict[str, object]:
    edge: dict[str, object] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "kind": "depends_on",
        "source_field": "work_items.depends_on",
        "relationship_state": "source_bound",
    }
    edge.update(overrides)
    return edge


def _reason_counts(rows: list[dict[str, object]]) -> dict[str, object]:
    return {row["reason"]: row["count"] for row in rows}


def test_graph_envelope_is_ordered_deterministically_but_fingerprints_raw_input() -> None:
    graph = {
        "schema_version": "GraphV1",
        "generated_at": "2026-08-25T12:00:00Z",
        "source": "coord.db",
        "nodes": [_node("work:B"), _node("work:A")],
        "edges": [_edge("edge:B", "work:B", "work:A"), _edge("edge:A", "work:A", "work:B")],
    }
    reordered = {**graph, "nodes": list(reversed(graph["nodes"])), "edges": list(reversed(graph["edges"]))}

    first = build_graph_envelope(graph)
    second = build_graph_envelope(reordered)

    assert [node["id"] for node in first["nodes"]] == ["work:A", "work:B"]
    assert [edge["id"] for edge in first["edges"]] == ["edge:A", "edge:B"]
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]
    assert {key: value for key, value in first.items() if key != "source"} == {
        key: value for key, value in second.items() if key != "source"
    }
    assert first["source"]["content_sha256"] != second["source"]["content_sha256"]


def test_graph_envelope_quarantines_duplicate_identities_and_missing_identities() -> None:
    graph = {
        "nodes": [_node("work:duplicate"), _node("work:duplicate"), _node("work:good"), _node("")],
        "edges": [
            _edge("edge:duplicate", "work:good", "work:good"),
            _edge("edge:duplicate", "work:good", "work:good"),
            _edge("edge:quarantined", "work:duplicate", "work:good"),
            _edge("", "work:good", "work:good"),
            _edge("edge:no-target", "work:good", ""),
        ],
    }

    envelope = build_graph_envelope(graph)

    assert envelope["population"] == {"nodes": 4, "edges": 5}
    assert envelope["eligible"] == {"nodes": 1, "edges": 0}
    assert envelope["nodes"] == [_node("work:good")]
    assert envelope["edges"] == []
    assert envelope["collisions"] == {
        "nodes": {"identity_count": 1, "entry_count": 2, "ids": ["work:duplicate"], "truncated": False},
        "edges": {"identity_count": 1, "entry_count": 2, "ids": ["edge:duplicate"], "truncated": False},
    }
    assert _reason_counts(envelope["omitted"]["node_reasons"]) == {
        "duplicate_identity": 2,
        "missing_identity": 1,
    }
    assert _reason_counts(envelope["omitted"]["edge_reasons"]) == {
        "duplicate_identity": 2,
        "missing_identity": 1,
        "missing_endpoint_identity": 1,
        "quarantined_source": 1,
    }
    assert envelope["complete"] is False


def test_graph_envelope_retains_and_reports_unknown_kinds_and_states() -> None:
    graph = {
        "nodes": [_node("work:A", kind="future_node")],
        "edges": [
            _edge(
                "edge:future",
                "work:A",
                "work:A",
                kind="future_edge",
                relationship_state="future_state",
            )
        ],
    }

    envelope = build_graph_envelope(graph)

    assert envelope["nodes"][0]["kind"] == "future_node"
    assert envelope["edges"][0]["kind"] == "future_edge"
    assert envelope["edges"][0]["relationship_state"] == "future_state"
    assert envelope["unknowns"] == {
        "node_kinds": [{"reason": "future_node", "count": 1}],
        "edge_kinds": [{"reason": "future_edge", "count": 1}],
        "relationship_states": [{"reason": "future_state", "count": 1}],
    }
    assert envelope["complete"] is True


def test_graph_envelope_reports_low_information_without_inventing_relationships() -> None:
    envelope = build_graph_envelope(
        {
            "schema_version": "GraphV1",
            "generated_at": "2026-08-25T12:00:00Z",
            "source": "coord.db",
            "nodes": [_node("work:A")],
            "edges": [],
        }
    )

    assert envelope["edges"] == []
    assert envelope["admission"] == {
        "state": "low_information",
        "reason_code": "authoritative_relationships_absent",
        "reason": (
            "The authoritative graph publishes no relationships; topology motion "
            "and connectivity must not be inferred."
        ),
        "missing_prerequisite": "authoritative_graph_relationships",
        "population": {"nodes": 1, "edges": 0},
        "eligible": {"nodes": 1, "edges": 0},
        "admitted": {"nodes": 1, "edges": 0},
        "omitted": {
            "nodes": 0,
            "edges": 0,
            "node_reasons": [],
            "edge_reasons": [],
        },
        "source": {
            "declared": "coord.db",
            "graph_schema_version": "GraphV1",
            "generated_at": "2026-08-25T12:00:00Z",
            "content_fingerprint_ref": "source.content_sha256",
        },
        "freshness": {"state": "not_declared_stale", "stale": False},
    }


def test_graph_envelope_reports_nonzero_authoritative_admission() -> None:
    envelope = build_graph_envelope(
        {
            "nodes": [_node("work:A"), _node("work:B")],
            "edges": [_edge("edge:A", "work:A", "work:B")],
        }
    )

    assert envelope["admission"]["state"] == "available"
    assert envelope["admission"]["reason_code"] == "authoritative_relationships_admitted"
    assert envelope["admission"]["missing_prerequisite"] == ""
    assert envelope["admission"]["population"] == {"nodes": 2, "edges": 1}
    assert envelope["admission"]["admitted"] == {"nodes": 2, "edges": 1}


def test_graph_envelope_accounts_for_node_edge_and_byte_caps() -> None:
    node_limited = build_graph_envelope(
        {"nodes": [_node("work:A"), _node("work:B"), _node("work:C")], "edges": []},
        node_cap=2,
    )
    assert node_limited["emitted"]["nodes"] == 2
    assert _reason_counts(node_limited["omitted"]["node_reasons"]) == {"node_cap": 1}
    assert node_limited["complete"] is False

    edge_limited = build_graph_envelope(
        {
            "nodes": [_node("work:A"), _node("work:B")],
            "edges": [
                _edge("edge:A", "work:A", "work:B"),
                _edge("edge:B", "work:B", "work:A"),
                _edge("edge:C", "work:A", "work:A"),
            ],
        },
        edge_cap=1,
    )
    assert edge_limited["emitted"]["edges"] == 1
    assert _reason_counts(edge_limited["omitted"]["edge_reasons"]) == {"edge_cap": 2}
    assert edge_limited["complete"] is False

    byte_graph = {
        "nodes": [_node("work:A"), _node("work:B")],
        "edges": [_edge("edge:A", "work:A", "work:B")],
    }
    uncapped = build_graph_envelope(byte_graph)
    byte_limited = build_graph_envelope(byte_graph, byte_cap=uncapped["emitted"]["bytes"] - 1)
    assert byte_limited["emitted"]["nodes"] == 2
    assert byte_limited["emitted"]["edges"] == 0
    assert _reason_counts(byte_limited["omitted"]["edge_reasons"]) == {"byte_cap": 1}
    assert byte_limited["complete"] is False


def test_graph_envelope_fingerprints_source_and_strips_prose_fields() -> None:
    graph = {
        "schema_version": "GraphV1",
        "generated_at": "2026-08-25T12:00:00Z",
        "source": "coord.db",
        "private_note": "PRIVATE_SENTINEL_MUST_NOT_SURVIVE",
        "nodes": [
            _node("work:A", title="PRIVATE_SENTINEL_MUST_NOT_SURVIVE", body="do not expose"),
        ],
        "edges": [
            _edge("edge:A", "work:A", "work:A", explanation="PRIVATE_SENTINEL_MUST_NOT_SURVIVE"),
        ],
    }

    envelope = build_graph_envelope(graph, source_stale=True)
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()

    assert envelope["schema_version"] == GRAPH_ENVELOPE_SCHEMA
    assert envelope["source"] == {
        "declared": "coord.db",
        "graph_schema_version": "GraphV1",
        "content_sha256": sha256(canonical).hexdigest(),
        "freshness_state": "stale",
    }
    assert set(envelope["nodes"][0]) == {"id", "kind", "label", "status", "missing"}
    assert set(envelope["edges"][0]) == {
        "id",
        "source",
        "target",
        "kind",
        "source_field",
        "relationship_state",
    }
    assert "PRIVATE_SENTINEL_MUST_NOT_SURVIVE" not in repr(envelope)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"node_cap": 0},
        {"edge_cap": 0},
        {"byte_cap": 0},
        {"collision_receipt_limit": 0},
    ],
)
def test_graph_envelope_rejects_nonpositive_limits(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        build_graph_envelope({"nodes": [], "edges": []}, **kwargs)


def test_graph_envelope_admits_running_work_a_degree_ranking_would_bury() -> None:
    """A capped operations envelope must not drop what is happening now.

    Ranking admission by degree alone favours old, well-connected, finished
    work: on a real 8,635-row board it returned 400 nodes of which 358 were
    done, archived or closed and none were running, because a job that started
    an hour ago has not had time to accumulate edges. This builds that shape
    deliberately -- many high-degree finished rows, a handful of freshly
    running ones with no edges at all -- and asserts the running rows survive.
    """
    finished = [
        {"id": f"work:done-{index:03d}", "kind": "work", "label": f"done {index}", "status": "done"}
        for index in range(300)
    ]
    running = [
        {"id": f"work:live-{index}", "kind": "work", "label": f"live {index}", "status": "running"}
        for index in range(5)
    ]
    # Every finished row is connected; the running rows are connected to
    # nothing, which is exactly why degree ranking loses them.
    edges = [
        {
            "id": f"edge-{index}",
            "source": f"work:done-{index:03d}",
            "target": f"work:done-{(index + 1) % 300:03d}",
            "kind": "depends_on",
            "source_field": "depends_on",
            "relationship_state": "source_bound",
        }
        for index in range(300)
    ]
    envelope = build_graph_envelope(
        {"nodes": finished + running, "edges": edges, "generated_at": "2026-08-26T00:00:00Z"},
        node_cap=100,
    )

    emitted = {node["id"] for node in envelope["nodes"]}
    assert emitted >= {node["id"] for node in running}, "a running row was capped out"
    assert envelope["emitted"]["nodes"] == 100
    frontier = envelope["frontier"]
    assert frontier["open_eligible"] == 5
    assert frontier["open_emitted"] == 5
    assert frontier["open_omitted"] == 0
    # The reserve is a ceiling, not a quota: five open rows do not hold fifty
    # slots away from the connected core.
    assert frontier["open_reserve"] == 5
    assert len(emitted) == 100


def test_graph_envelope_reserve_is_capped_and_the_rest_still_reaches_the_core() -> None:
    """More open work than the reserve holds: the reserve bounds it."""
    open_rows = [
        {"id": f"work:open-{index:03d}", "kind": "work", "label": f"open {index}", "status": "blocked"}
        for index in range(200)
    ]
    finished = [
        {"id": f"work:done-{index:03d}", "kind": "work", "label": f"done {index}", "status": "done"}
        for index in range(200)
    ]
    envelope = build_graph_envelope(
        {"nodes": open_rows + finished, "edges": [], "generated_at": "2026-08-26T00:00:00Z"},
        node_cap=100,
        open_reserve_ratio=0.5,
    )
    frontier = envelope["frontier"]
    assert frontier["open_reserve"] == 50
    assert frontier["reserved_admitted"] == 50
    assert frontier["open_eligible"] == 200
    emitted_open = sum(1 for node in envelope["nodes"] if node["status"] == "blocked")
    # 50 reserved, and the remaining 50 slots are filled from the rest, which
    # at equal degree is decided by id -- so open rows can win those too.
    assert emitted_open >= 50
    assert envelope["emitted"]["nodes"] == 100
