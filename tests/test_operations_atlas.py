from __future__ import annotations

import pytest

from coordharness.board import operations as operations_module
from coordharness.board.operations import OPERATIONS_SCHEMA, build_operations


STAMP = "2026-08-25T12:00:00Z"


def _row(
    row_id: str,
    *,
    status: str = "planned",
    bucket: str = "job",
    owner: str = "codex:demo",
    module: str = "platform",
    stale: bool = False,
) -> dict:
    return {
        "id": row_id,
        "title": f"Work {row_id}",
        "status": status,
        "bucket": bucket,
        "owner": owner,
        "module": module,
        "group": "",
        "priority": 1,
        "progress_fraction": None,
        "eta_seconds": None,
        "stale": stale,
        "current_step": "",
    }


def _documents() -> tuple[dict, dict, dict, dict]:
    snapshot = {
        "schema_version": "NativeSnapshotV1",
        "generated_at": STAMP,
        "source": "coord.db+job_progress",
        "stale": False,
        "summary": {"running": 1, "attention": 0, "next": 3, "done": 1, "total": 5},
        "rows": [
            _row("A"),
            _row("B"),
            _row("C", status="done"),
            _row("D", owner=""),
            _row("job:RUN-1", status="running", bucket="job", owner="local:runner"),
        ],
        "sessions": [
            {"id": "codex:demo", "actor": "codex", "label": "demo", "live": True},
            {"id": "claude:review", "actor": "claude", "label": "review", "live": False},
        ],
    }
    graph = {
        "schema_version": "1",
        "generated_at": STAMP,
        "source": "coord.db+job_progress",
        "nodes": [
            {"id": f"work:{value}", "kind": "work", "label": value, "status": "", "missing": False}
            for value in ("A", "B", "C", "D")
        ]
        + [
            {"id": "job:RUN-1", "kind": "job", "label": "RUN-1", "status": "running", "missing": False},
            {"id": "work:LOST", "kind": "missing_work", "label": "Missing", "status": "", "missing": True},
        ],
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
                "id": "depends_on:work:B:work:C",
                "source": "work:B",
                "target": "work:C",
                "kind": "depends_on",
                "source_field": "work_items.depends_on",
                "relationship_state": "source_bound",
            },
            {
                "id": "runtime_evidence:work:A:job:RUN-1",
                "source": "work:A",
                "target": "job:RUN-1",
                "kind": "runtime_evidence",
                "source_field": "job_progress.roadmap_id",
                "relationship_state": "source_bound",
            },
            {
                "id": "depends_on:work:D:work:LOST",
                "source": "work:D",
                "target": "work:LOST",
                "kind": "depends_on",
                "source_field": "work_items.depends_on",
                "relationship_state": "missing_target",
            },
        ],
    }
    context = {
        "schema_version": "ContextV1",
        "generated_at": STAMP,
        "source": "coord.db",
        "items": [
            {
                "id": "A",
                "parent": "",
                "children": [],
                "depends_on": ["B"],
                "dependents": [],
                "siblings": [],
                "done_signal": "reports/a.md",
                "artifact_recorded": False,
                "blocked_reason_class": "",
                "resume_when": "",
                "next_step": "",
                "claim_present": False,
                "lease_remaining_s": None,
            },
            {
                "id": "B",
                "parent": "",
                "children": [],
                "depends_on": ["C"],
                "dependents": ["A"],
                "siblings": [],
                "done_signal": "reports/b.md",
                "artifact_recorded": False,
                "blocked_reason_class": "",
                "resume_when": "",
                "next_step": "",
                "claim_present": True,
                "lease_remaining_s": 120,
            },
            {
                "id": "C",
                "parent": "",
                "children": [],
                "depends_on": [],
                "dependents": ["B"],
                "siblings": [],
                "done_signal": "reports/c.md",
                "artifact_recorded": True,
                "blocked_reason_class": "",
                "resume_when": "",
                "next_step": "",
                "claim_present": False,
                "lease_remaining_s": None,
            },
            {
                "id": "D",
                "parent": "",
                "children": [],
                "depends_on": ["LOST"],
                "dependents": [],
                "siblings": [],
                "done_signal": "",
                "artifact_recorded": False,
                "blocked_reason_class": "",
                "resume_when": "",
                "next_step": "",
                "claim_present": False,
                "lease_remaining_s": None,
            },
        ],
    }
    timeline = {
        "schema_version": "TimelineV1",
        "generated_at": STAMP,
        "source": "coord.db",
        "items": [
            {
                "id": "A",
                "events": [
                    {
                        "at": "2026-08-25T11:00:00Z",
                        "kind": "claim",
                        "actor": "codex",
                        "body": "PRIVATE_SENTINEL_MUST_NOT_SURVIVE",
                    },
                    {
                        "at": "2026-08-25T11:30:00Z",
                        "kind": "note",
                        "actor": "claude",
                        "payload": {"secret": "PRIVATE_SENTINEL_MUST_NOT_SURVIVE"},
                    },
                ],
            }
        ],
    }
    return snapshot, graph, context, timeline


def _dependency_documents(
    dependencies: dict[str, list[str]],
    *,
    statuses: dict[str, str] | None = None,
) -> tuple[dict, dict, dict, dict]:
    statuses = statuses or {}
    ids = sorted(dependencies)
    snapshot = {
        "schema_version": "NativeSnapshotV1",
        "generated_at": STAMP,
        "source": "coord.db+job_progress",
        "stale": False,
        "summary": {},
        "rows": [
            _row(work_id, status=statuses.get(work_id, "planned"))
            for work_id in ids
        ],
        "sessions": [],
    }
    graph = {
        "schema_version": "1",
        "generated_at": STAMP,
        "source": "coord.db+job_progress",
        "nodes": [
            {
                "id": f"work:{work_id}",
                "kind": "work",
                "label": work_id,
                "status": statuses.get(work_id, "planned"),
                "missing": False,
            }
            for work_id in ids
        ],
        "edges": [],
    }
    context = {
        "schema_version": "ContextV1",
        "generated_at": STAMP,
        "source": "coord.db",
        "items": [
            {
                "id": work_id,
                "parent": "",
                "children": [],
                "depends_on": prerequisites,
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
            for work_id, prerequisites in sorted(dependencies.items())
        ],
    }
    timeline = {
        "schema_version": "TimelineV1",
        "generated_at": STAMP,
        "source": "coord.db",
        "items": [],
    }
    return snapshot, graph, context, timeline


def test_operations_atlas_derives_execution_shape_and_health() -> None:
    atlas = build_operations(*_documents())

    assert atlas["schema_version"] == OPERATIONS_SCHEMA
    assert atlas["metrics"]["work_items"] == 4
    assert atlas["metrics"]["job_projections"] == 1
    assert atlas["metrics"]["live_sessions"] == 1
    assert atlas["metrics"]["critical_path_steps"] == 3
    assert atlas["execution"]["critical_path"] == ["C", "B", "A"]
    assert atlas["execution"]["dependency_clear_planned"] == ["B"]
    assert atlas["execution"]["impact"][0] == {
        "id": "C",
        "downstream": 2,
        "cyclic": False,
        "component_size": 1,
        "cycle_tainted": False,
    }
    assert atlas["execution"]["topology_metrics_status"] == "partial_unresolved"
    assert atlas["execution"]["analysis_population_total"] == 4
    assert atlas["execution"]["analysis_population_emitted"] == 4
    assert atlas["execution"]["analysis_population_omitted"] == 0
    assert atlas["execution"]["analysis_population_truncated"] is False
    assert atlas["execution"]["cycle_tainted"] == []
    assert atlas["execution"]["cycle_tainted_total"] == 0
    assert atlas["execution"]["cycle_tainted_emitted"] == 0
    assert atlas["execution"]["cycle_tainted_truncated"] is False
    assert atlas["execution"]["cycle_impact"] == []
    assert atlas["execution"]["cycle_components_total"] == 0
    assert atlas["execution"]["cycle_components_emitted"] == 0
    assert atlas["execution"]["cycle_impact_truncated"] is False
    assert atlas["execution"]["missing_dependencies"] == [
        {"source": "D", "target": "LOST"}
    ]
    assert atlas["execution"]["missing_dependency_tainted"] == ["D"]
    assert atlas["execution"]["unresolved_tainted"] == ["D"]
    assert atlas["execution"]["unresolved_tainted_total"] == 1
    assert atlas["health"]["missing_targets"] == ["work:LOST"]
    assert atlas["health"]["expiring_claims"] == ["B"]
    assert atlas["health"]["ok"] is False
    assert atlas["health"]["document_skew_seconds"] == 0


def test_hierarchy_job_bucket_is_work_but_prefixed_job_is_runtime() -> None:
    atlas = build_operations(*_documents())

    assert atlas["scope"]["total_work"] == 4
    assert atlas["distribution"]["lanes"] == [
        {"key": "codex", "count": 3},
        {"key": "unowned", "count": 1},
    ]
    assert all(not item["id"].startswith("job:") for item in atlas["activity"])


def test_operations_atlas_reselects_occurrence_fields_and_bounds_activity() -> None:
    atlas = build_operations(*_documents(), activity_limit=1)

    assert atlas["metrics"]["recorded_events"] == 2
    assert atlas["metrics"]["events_24h"] == 2
    assert atlas["activity"] == [
        {
            "id": "A",
            "at": "2026-08-25T11:30:00Z",
            "kind": "note",
            "actor": "claude",
        }
    ]
    assert "PRIVATE_SENTINEL" not in repr(atlas)


def test_operations_atlas_refuses_clear_health_for_future_events() -> None:
    snapshot, graph, context, timeline = _documents()
    timeline["items"][0]["events"].append(
        {
            "at": "2026-08-25T12:01:00Z",
            "kind": "note",
            "actor": "codex",
        }
    )

    atlas = build_operations(snapshot, graph, context, timeline)

    assert atlas["metrics"]["recorded_events"] == 3
    assert atlas["metrics"]["events_24h"] == 2
    assert atlas["health"]["future_events"] == ["A"]
    assert atlas["health"]["ok"] is False
    signal = next(
        row for row in atlas["health"]["signals"] if row["key"] == "future_events"
    )
    assert signal == {
        "key": "future_events",
        "severity": "error",
        "count": 1,
        "label": "Recorded events occur after this document's read clock",
    }


def test_operations_atlas_detects_dependency_cycles_without_recursing_forever() -> None:
    snapshot, graph, context, timeline = _documents()
    context["items"][1]["depends_on"] = ["A"]
    graph["edges"][1]["target"] = "work:A"

    atlas = build_operations(snapshot, graph, context, timeline)

    assert atlas["health"]["cycles"] == [["A", "B"]]
    assert atlas["health"]["ok"] is False
    assert atlas["execution"]["topology_metrics_status"] == "partial_cycle"
    assert atlas["execution"]["cycle_tainted"] == ["A", "B"]
    assert {row["id"] for row in atlas["execution"]["impact"]}.isdisjoint({"A", "B"})
    assert atlas["execution"]["cycle_impact"] == [
        {
            "members": ["A", "B"],
            "members_total": 2,
            "members_truncated": False,
            "downstream_after_component": 0,
        }
    ]
    assert atlas["metrics"]["max_parallel_width"] >= 1


def test_cycle_taint_withholds_downstream_paths_layers_and_individual_impact() -> None:
    snapshot, graph, context, timeline = _documents()
    snapshot["rows"] = [_row(value) for value in ("A", "B", "C", "D", "E")]
    snapshot["sessions"] = []
    graph["nodes"] = [
        {
            "id": f"work:{value}",
            "kind": "work",
            "label": value,
            "status": "",
            "missing": False,
        }
        for value in ("A", "B", "C", "D", "E")
    ]
    graph["edges"] = []
    template = context["items"][0]
    dependencies = {
        "A": ["B"],
        "B": ["A"],
        "C": ["A"],
        "D": ["B"],
        "E": ["C"],
    }
    context["items"] = [
        {
            **template,
            "id": work_id,
            "depends_on": prerequisites,
            "dependents": [],
            "claim_present": False,
            "lease_remaining_s": None,
        }
        for work_id, prerequisites in dependencies.items()
    ]
    timeline["items"] = []

    atlas = build_operations(snapshot, graph, context, timeline)
    execution = atlas["execution"]

    assert atlas["health"]["cycles"] == [["A", "B"]]
    assert execution["topology_metrics_status"] == "withheld_cycle"
    assert execution["cycle_tainted"] == ["A", "B", "C", "D", "E"]
    assert execution["critical_path"] == []
    assert execution["layers"] == []
    assert execution["impact"] == []
    assert execution["cycle_impact"] == [
        {
            "members": ["A", "B"],
            "members_total": 2,
            "members_truncated": False,
            "downstream_after_component": 3,
        }
    ]


@pytest.mark.parametrize("node_limit", [400, 5_000])
def test_dependency_analysis_hard_caps_large_chains_without_recursion_failure(
    node_limit: int,
) -> None:
    dependencies = {
        f"W{index:04d}": ([] if index == 0 else [f"W{index - 1:04d}"])
        for index in range(1_200)
    }

    atlas = build_operations(*_dependency_documents(dependencies), node_limit=node_limit)
    execution = atlas["execution"]

    assert atlas["scope"] == {
        "total_work": 1_200,
        "included_work": 400,
        "node_limit": node_limit,
        "dependency_analysis_cap": 400,
        "activity_limit": 120,
        "truncated": True,
    }
    assert execution["topology_metrics_status"] == "partial_population"
    assert execution["analysis_population_total"] == 1_200
    assert execution["analysis_population_emitted"] == 400
    assert execution["analysis_population_omitted"] == 800
    assert len(execution["critical_path"]) == 400
    assert execution["critical_path"][0] == "W0000"
    assert execution["critical_path"][-1] == "W0399"


def test_boundary_and_missing_prerequisites_are_bounded_and_never_ready() -> None:
    dependencies = {
        "A": ["Z1", "Z2", "Z3", "Z4"],
        "B": ["LOST1", "LOST2", "LOST3", "LOST4"],
        "C": [],
        "Z1": [],
        "Z2": [],
        "Z3": [],
        "Z4": [],
    }

    atlas = build_operations(*_dependency_documents(dependencies), node_limit=3)
    execution = atlas["execution"]

    assert execution["topology_metrics_status"] == "partial_population"
    assert execution["analysis_population_total"] == 7
    assert execution["analysis_population_emitted"] == 3
    assert execution["analysis_boundary_dependencies_total"] == 4
    assert execution["analysis_boundary_dependencies_emitted"] == 3
    assert execution["analysis_boundary_dependencies_truncated"] is True
    assert execution["analysis_boundary_tainted"] == ["A"]
    assert execution["missing_dependencies_total"] == 4
    assert execution["missing_dependencies_emitted"] == 3
    assert execution["missing_dependencies_truncated"] is True
    assert execution["missing_dependency_tainted"] == ["B"]
    assert execution["unresolved_tainted"] == ["A", "B"]
    assert execution["unresolved_tainted_total"] == 2
    assert execution["dependency_clear_planned"] == ["C"]
    assert execution["critical_path"] == ["C"]
    assert [row["id"] for row in execution["impact"]] == ["C"]
    signal_counts = {row["key"]: row["count"] for row in atlas["health"]["signals"]}
    assert signal_counts["dependency_analysis_scope"] == 4
    assert signal_counts["dependency_analysis_boundary"] == 4
    assert signal_counts["missing_dependencies"] == 4


def test_unresolved_union_deduplicates_rows_tainted_by_both_reasons() -> None:
    dependencies = {
        "A": ["LOST", "Z"],
        "B": [],
        "Z": [],
    }

    atlas = build_operations(*_dependency_documents(dependencies), node_limit=2)
    execution = atlas["execution"]

    assert execution["analysis_boundary_tainted"] == ["A"]
    assert execution["missing_dependency_tainted"] == ["A"]
    assert execution["unresolved_tainted"] == ["A"]
    assert execution["unresolved_tainted_total"] == 1
    assert execution["unresolved_tainted_emitted"] == 1
    assert execution["unresolved_tainted_truncated"] is False
    assert execution["dependency_clear_planned"] == ["B"]


def test_cycle_tainted_planned_row_is_never_dependency_clear() -> None:
    dependencies = {"A": ["B"], "B": ["A"], "C": ["A"]}
    statuses = {"A": "done", "B": "done", "C": "planned"}

    atlas = build_operations(
        *_dependency_documents(dependencies, statuses=statuses)
    )

    assert atlas["execution"]["topology_metrics_status"] == "withheld_cycle"
    assert atlas["execution"]["cycle_tainted"] == ["A", "B", "C"]
    assert atlas["execution"]["dependency_clear_planned"] == []
    assert atlas["metrics"]["dependency_clear_planned"] == 0


def test_cycle_component_and_member_receipts_report_bounded_totals() -> None:
    dependencies = {"A": ["B"], "B": ["A"], "C": ["D"], "D": ["C"]}

    atlas = build_operations(
        *_dependency_documents(dependencies), impact_limit=1
    )
    execution = atlas["execution"]

    assert execution["cycle_components_total"] == 2
    assert execution["cycle_components_emitted"] == 1
    assert execution["cycle_impact_truncated"] is True
    assert execution["cycle_member_ids_total"] == 4
    assert execution["cycle_member_ids_emitted"] == 2
    assert execution["cycle_member_ids_truncated"] is True
    assert execution["cycle_impact"] == [
        {
            "members": ["A", "B"],
            "members_total": 2,
            "members_truncated": False,
            "downstream_after_component": 0,
        }
    ]
    cycles_signal = next(
        row for row in atlas["health"]["signals"] if row["key"] == "cycles"
    )
    assert cycles_signal["count"] == 2


def _minimal_documents() -> tuple[dict, dict, dict, dict]:
    snapshot, graph, context, timeline = _documents()
    snapshot["rows"] = [snapshot["rows"][0]]
    snapshot["sessions"] = []
    graph["nodes"] = [graph["nodes"][0]]
    graph["edges"] = []
    context["items"] = [context["items"][0]]
    context["items"][0]["depends_on"] = []
    timeline["items"] = []
    return snapshot, graph, context, timeline


def test_topology_availability_reconciles_zero_relationship_population() -> None:
    snapshot, graph, context, timeline = _minimal_documents()

    atlas = build_operations(snapshot, graph, context, timeline)

    receipt = atlas["topology_availability"]
    assert receipt["state"] == "low_information"
    assert receipt["reason_code"] == "authoritative_relationships_absent"
    assert receipt["missing_prerequisite"] == "authoritative_graph_relationships"
    assert receipt["population"] == {
        "work_items": 1,
        "nodes": 1,
        "edges": 0,
        "events": 0,
    }
    assert receipt["admitted"] == {
        "work_items": 1,
        "nodes": 1,
        "edges": 0,
        "events": 0,
    }
    assert receipt["omitted"]["work_items"] == 0
    assert receipt["omitted"]["nodes"] == 0
    assert receipt["omitted"]["edges"] == 0
    assert receipt["omitted"]["events"] == 0
    assert receipt["source"]["graph_declared"] == "coord.db+job_progress"
    assert receipt["freshness"] == {
        "state": "current",
        "stale": False,
        "generated_at": STAMP,
        "document_skew_seconds": 0.0,
    }
    assert atlas["graph_envelope"]["edges"] == []
    assert atlas["activity"] == []


def test_topology_availability_preserves_nonzero_relationship_population() -> None:
    atlas = build_operations(*_documents())

    receipt = atlas["topology_availability"]
    assert receipt["state"] == "available"
    assert receipt["reason_code"] == "authoritative_relationships_admitted"
    assert receipt["missing_prerequisite"] == ""
    assert receipt["population"] == {
        "work_items": 4,
        "nodes": 6,
        "edges": 4,
        "events": 2,
    }
    assert receipt["admitted"]["edges"] == 4
    assert receipt["admitted"]["events"] == 2


def test_unknown_graph_vocabulary_prevents_clear_health() -> None:
    snapshot, graph, context, timeline = _minimal_documents()
    graph["nodes"][0]["kind"] = "future_kind"

    atlas = build_operations(snapshot, graph, context, timeline)

    assert atlas["graph_envelope"]["complete"] is True
    assert atlas["health"]["ok"] is False
    assert atlas["health"]["graph_integrity"] == {
        "ok": False,
        "source_match": True,
        "reasons": [{"reason": "unknown_node_kinds", "count": 1}],
    }


def test_graph_collisions_and_incomplete_envelope_prevent_clear_health() -> None:
    snapshot, graph, context, timeline = _minimal_documents()
    graph["nodes"].append(dict(graph["nodes"][0]))

    atlas = build_operations(snapshot, graph, context, timeline)

    assert atlas["graph_envelope"]["complete"] is False
    assert atlas["health"]["ok"] is False
    assert atlas["health"]["graph_integrity"]["reasons"] == [
        {"reason": "incomplete_projection", "count": 2},
        {"reason": "nodes_identity_collisions", "count": 1},
    ]


def test_graph_source_mismatch_prevents_clear_health(monkeypatch) -> None:
    snapshot, graph, context, timeline = _minimal_documents()
    real_builder = operations_module.build_graph_envelope

    def _mismatched_envelope(*args, **kwargs):
        envelope = real_builder(*args, **kwargs)
        envelope["source"]["content_sha256"] = "0" * 64
        return envelope

    monkeypatch.setattr(
        operations_module, "build_graph_envelope", _mismatched_envelope
    )

    atlas = build_operations(snapshot, graph, context, timeline)

    assert atlas["health"]["ok"] is False
    assert atlas["health"]["graph_integrity"] == {
        "ok": False,
        "source_match": False,
        "reasons": [{"reason": "source_fingerprint_mismatch", "count": 1}],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"node_limit": 0},
        {"activity_limit": 0},
        {"impact_limit": 0},
    ],
)
def test_operations_atlas_rejects_nonpositive_limits(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="positive"):
        build_operations(*_documents(), **kwargs)
