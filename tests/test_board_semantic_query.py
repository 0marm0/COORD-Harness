from __future__ import annotations

from base64 import urlsafe_b64encode
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import random

import pytest

from coordharness.board import semantic_query as semantic_query_module
from coordharness.board.semantic_query import (
    QueryContractError,
    build_semantic_query_response,
    decode_display_state,
    decode_query,
    encode_display_state,
    encode_query,
    query_error_document,
)


STAMP = "2026-08-26T12:00:00Z"


def _row(identity: str, **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": identity,
        "title": identity,
        "status": "planned",
        "bucket": "task",
        "owner": "",
        "module": "",
        "group": "",
        "priority": 0,
        "progress_fraction": None,
        "eta_seconds": None,
        "stale": False,
        "current_step": "",
    }
    row.update(changes)
    return row


def _context(identity: str, **changes: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": identity,
        "parent": "",
        "children": [],
        "depends_on": [],
        "dependents": [],
        "siblings": [],
        "done_signal": "",
        "artifact_recorded": False,
        "blocked_reason_class": "",
        "resume_when": "",
        "next_step": "",
        "claim_present": False,
        "lease_remaining_s": None,
    }
    item.update(changes)
    return item


def _sources() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    snapshot = {
        "schema_version": "1",
        "generated_at": STAMP,
        "source": "coord.db+job_progress",
        "stale": False,
        "rows": [
            _row("A", status="running", owner="codex:one", module="runtime", group="control", priority=5),
            _row("B", status="blocked", owner="claude:one", module="research", group="data", stale=True),
            _row("C", status="done", owner="codex:two", module="runtime", group="control"),
            _row(
                "job:J1",
                status="running",
                bucket="job",
                owner="local",
                module="ml",
                progress_fraction=0.5,
                eta_seconds=60,
            ),
            _row("D", status="planned", owner="", module="ui", group="product"),
        ],
    }
    graph = {
        "schema_version": "1",
        "generated_at": STAMP,
        "source": "coord.db+job_progress",
        "nodes": [],
        "edges": [
            {
                "id": "depends_on:work:A:work:B",
                "source": "work:A",
                "target": "work:B",
                "kind": "depends_on",
                "source_field": "work_items.depends_on",
                "relationship_state": "source_bound",
            },
            {
                "id": "evidence:work:C:artifact:receipt",
                "source": "work:C",
                "target": "artifact:receipt",
                "kind": "evidence",
                "source_field": "artifacts.work_id",
                "relationship_state": "source_bound",
            },
            {
                "id": "runtime_evidence:work:A:job:J1",
                "source": "work:A",
                "target": "job:J1",
                "kind": "runtime_evidence",
                "source_field": "job_progress.roadmap_id",
                "relationship_state": "source_bound",
            },
        ],
    }
    context = {
        "schema_version": "ContextV1",
        "generated_at": STAMP,
        "source": "coord.db",
        "items": [
            _context("A", depends_on=["B"], children=["D"], claim_present=True, lease_remaining_s=90),
            _context("B", dependents=["A"], claim_present=True, lease_remaining_s=-30),
            _context("C", artifact_recorded=True),
            _context("D", parent="A"),
        ],
    }
    return snapshot, graph, context


def _ids(expr: dict[str, object]) -> list[str]:
    snapshot, graph, context = _sources()
    return build_semantic_query_response(
        snapshot,
        graph,
        context,
        query={"expr": expr},
    )["matched_ids"]


def test_randomized_query_and_display_round_trips_are_canonical() -> None:
    generator = random.Random(20260826)
    leaves = [
        {"status": {"in": ["running", "blocked"]}},
        {"module": {"in": ["runtime", "research"]}},
        {"actor": {"in": ["codex:one", "claude:one"]}},
        {"freshness": {"in": ["fresh", "lease_expired"]}},
        {"context": {"field": "claim_present", "operator": "eq", "value": True}},
    ]
    for _ in range(100):
        chosen = generator.sample(leaves, generator.randint(1, len(leaves)))
        generator.shuffle(chosen)
        query = {"expr": {generator.choice(["all", "any"]): deepcopy(chosen)}}
        token = encode_query(query)
        assert "=" not in token
        assert encode_query(decode_query(token)) == token

        state = {
            "view": generator.choice(["list", "mesh", "compact"]),
            "sort": generator.choice(["id", "priority"]),
            "selected_id": generator.choice(["", "A", "B"]),
            "expanded_ids": generator.sample(["A", "B", "C", "D"], generator.randint(0, 4)),
        }
        display_token = encode_display_state(state)
        assert "=" not in display_token
        assert encode_display_state(decode_display_state(display_token)) == display_token


def test_commutative_and_set_equivalence_have_one_token() -> None:
    left = {
        "expr": {
            "all": [
                {"status": {"in": ["running", "blocked", "running"]}},
                {"module": {"in": ["runtime", "research"]}},
            ]
        }
    }
    right = {
        "schema_version": "SemanticQueryV1",
        "expr": {
            "all": [
                {"module": {"in": ["research", "runtime"]}},
                {"status": {"in": ["blocked", "running"]}},
            ]
        },
    }
    assert encode_query(left) == encode_query(right)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ({"lifecycle": {"in": ["active"]}}, ["A", "job:J1"]),
        ({"actor": {"in": ["codex:one"]}}, ["A"]),
        ({"module": {"in": ["control"]}}, ["A", "C"]),
        ({"status": {"in": ["blocked"]}}, ["B"]),
        ({"evidence": {"recorded": True}}, ["A", "C"]),
        ({"freshness": {"in": ["stale"]}}, ["B"]),
        (
            {
                "graph_relation": {
                    "direction": "outgoing",
                    "kind_in": ["depends_on"],
                    "related_ids": ["B"],
                }
            },
            ["A"],
        ),
        ({"job": {"is_job": True, "progress": "known", "eta": "known"}}, ["job:J1"]),
        ({"context": {"field": "depends_on", "operator": "contains", "value": "B"}}, ["A"]),
    ],
)
def test_each_predicate_family(expr: dict[str, object], expected: list[str]) -> None:
    assert _ids(expr) == expected


def test_nested_boolean_operators() -> None:
    expression = {
        "all": [
            {"any": [{"module": {"in": ["runtime"]}}, {"status": {"in": ["blocked"]}}]},
            {"not": {"lifecycle": {"in": ["terminal"]}}},
        ]
    }
    assert _ids(expression) == ["A", "B"]


def test_default_query_is_match_all_and_display_state_never_changes_population() -> None:
    snapshot, graph, context = _sources()
    first = build_semantic_query_response(
        snapshot,
        graph,
        context,
        display_state={"view": "mesh", "sort": "priority", "selected_id": "A"},
    )
    second = build_semantic_query_response(
        snapshot,
        graph,
        context,
        display_state={"view": "list", "sort": "id", "selected_id": "C"},
    )
    expected = ["A", "B", "C", "D", "job:J1"]
    assert first["matched_ids"] == second["matched_ids"] == expected
    assert first["results"] == second["results"]
    assert first["population"] == second["population"]


def test_full_id_population_survives_detail_cap_with_explicit_receipt() -> None:
    snapshot, graph, context = _sources()
    operations = {
        "graph_envelope": {
            "population": {"nodes": 99, "edges": 77},
            "emitted": {"nodes": 10, "edges": 20, "bytes": 5000},
            "omitted": {"nodes": 89, "edges": 57},
            "caps": {"nodes": 10, "edges": 20, "bytes": 5000},
            "complete": False,
        }
    }
    document = build_semantic_query_response(
        snapshot, graph, context, operations, cache_generation=12, result_cap=2
    )
    assert document["cache_generation"] == 12
    assert document["matched_ids"] == ["A", "B", "C", "D", "job:J1"]
    assert [result["id"] for result in document["results"]] == ["A", "B"]
    assert document["population"] == {"rows": 5, "matched": 5, "detailed": 2, "omitted_detail": 3}
    receipt = document["omission_receipt"]
    assert receipt["matched_ids_complete"] is True
    assert receipt["omitted_detail_count"] == 3
    assert receipt["reason"] == "result_cap"
    assert receipt["operations_graph_envelope"]["population"] == {"nodes": 99, "edges": 77}


def test_tokens_reject_invalid_tampered_noncanonical_and_oversized_input() -> None:
    token = encode_query({"expr": {"status": {"in": ["running"]}}})
    prefix, payload, digest = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"
    with pytest.raises(QueryContractError, match="digest") as tampered:
        decode_query(f"{prefix}.{payload[:-1]}{replacement}.{digest}")
    assert tampered.value.code == "tampered_token"

    noncanonical_payload = json.dumps(
        {"schema_version": "SemanticQueryV1", "expr": {"status": {"in": ["running"]}}},
        indent=2,
    ).encode()
    noncanonical = (
        "sq1."
        + urlsafe_b64encode(noncanonical_payload).decode().rstrip("=")
        + "."
        + urlsafe_b64encode(sha256(noncanonical_payload).digest()).decode().rstrip("=")
    )
    with pytest.raises(QueryContractError) as rejected:
        decode_query(noncanonical)
    assert rejected.value.code == "noncanonical_token"

    with pytest.raises(QueryContractError) as invalid:
        decode_query("sq1.not/base64.digest")
    assert invalid.value.code == "invalid_token"
    with pytest.raises(QueryContractError) as oversized:
        decode_query("sq1." + "A" * 24_001 + ".AA")
    assert oversized.value.code == "oversize_token"


def test_strict_ast_timestamp_skew_receipt_and_raw_graph_requirement() -> None:
    with pytest.raises(QueryContractError) as unknown:
        encode_query({"expr": {"status": {"in": ["running"], "extra": True}}})
    assert unknown.value.code == "invalid_document"

    snapshot, graph, context = _sources()
    graph["generated_at"] = "2026-08-26T12:00:00.125Z"
    context["generated_at"] = "2026-08-26T12:00:00.250Z"
    response = build_semantic_query_response(
        snapshot, graph, context, cache_generation=42
    )
    assert response["cache_generation"] == 42
    assert response["generated_at"] == STAMP
    assert response["source"]["snapshot_generated_at"] == STAMP
    assert response["source"]["graph_generated_at"] == "2026-08-26T12:00:00.125Z"
    assert response["source"]["context_generated_at"] == "2026-08-26T12:00:00.250Z"
    assert response["source"]["max_generation_skew_ms"] == 250.0

    malformed = deepcopy(context)
    malformed["generated_at"] = "not-a-timestamp"
    with pytest.raises(QueryContractError) as invalid_source:
        build_semantic_query_response(snapshot, graph, malformed)
    assert invalid_source.value.code == "invalid_source"

    envelope = {**graph, "schema_version": "GraphEnvelopeV1", "population": {}}
    with pytest.raises(QueryContractError) as partial:
        build_semantic_query_response(snapshot, envelope, context)
    assert partial.value.code == "partial_graph"


def test_no_input_mutation_and_public_structural_detail_only() -> None:
    snapshot, graph, context = _sources()
    snapshot["rows"][0]["private_payload"] = "must not escape"
    context["items"][0]["next_step"] = "not a semantic field"
    inputs = deepcopy((snapshot, graph, context))
    response = build_semantic_query_response(
        snapshot,
        graph,
        context,
        query={"expr": {"status": {"in": ["running"]}}},
    )
    assert (snapshot, graph, context) == inputs
    rendered = json.dumps(response)
    assert "private_payload" not in rendered
    assert "not a semantic field" not in rendered
    assert set(response["results"][0]["context"]) == {
        "parent", "children", "depends_on", "dependents", "siblings",
        "artifact_recorded", "blocked_reason_class", "claim_present", "lease_remaining_s",
    }


def test_error_document_is_explicit_and_does_not_echo_input() -> None:
    error = QueryContractError("tampered_token", "token digest does not match payload", path="$.digest")
    assert query_error_document(error) == {
        "schema_version": "SemanticQueryErrorV1",
        "error": {
            "code": "tampered_token",
            "message": "token digest does not match payload",
            "path": "$.digest",
        },
    }


def test_sparse_graph_evaluation_inspects_only_incident_edges(monkeypatch) -> None:
    row_count = 600
    edge_count = row_count - 1
    snapshot = {
        "schema_version": "1",
        "generated_at": STAMP,
        "stale": False,
        "rows": [_row(f"W-{index}") for index in range(row_count)],
    }
    graph = {
        "schema_version": "1",
        "generated_at": STAMP,
        "nodes": [],
        "edges": [
            {
                "id": f"edge-{index}",
                "source": f"work:W-{index}",
                "target": f"work:W-{index + 1}",
                "kind": "depends_on",
                "relationship_state": "source_bound",
            }
            for index in range(edge_count)
        ],
    }
    context = {
        "schema_version": "ContextV1",
        "generated_at": STAMP,
        "items": [_context(f"W-{index}") for index in range(row_count)],
    }
    real_matches = semantic_query_module._matches
    inspected = 0

    def counted_matches(expression, row, item, edges, *, snapshot_stale):
        nonlocal inspected
        inspected += len(edges)
        return real_matches(
            expression,
            row,
            item,
            edges,
            snapshot_stale=snapshot_stale,
        )

    monkeypatch.setattr(semantic_query_module, "_matches", counted_matches)
    response = build_semantic_query_response(
        snapshot,
        graph,
        context,
        query={"expr": {"graph_relation": {"kind_in": ["depends_on"]}}},
        result_cap=1,
    )

    assert len(response["matched_ids"]) == row_count
    assert inspected == edge_count * 2
    assert inspected < row_count * edge_count / 100


def test_contract_schemas_are_packaged_json() -> None:
    board = Path(__file__).resolve().parents[1] / "src" / "coordharness" / "board"
    query_schema = json.loads((board / "semantic_query_v1.schema.json").read_text())
    response_schema = json.loads((board / "semantic_query_response_v1.schema.json").read_text())
    assert query_schema["properties"]["schema_version"]["const"] == "SemanticQueryV1"
    assert response_schema["properties"]["schema_version"]["const"] == "SemanticQueryResponseV1"
