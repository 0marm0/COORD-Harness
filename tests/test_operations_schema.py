from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from coordharness.board.operations import build_operations
from test_operations_atlas import _documents


SCHEMA_DIR = Path(__file__).parents[1] / "src" / "coordharness" / "board"


def _validator() -> Draft202012Validator:
    ops_schema = json.loads((SCHEMA_DIR / "ops_atlas_v1.schema.json").read_text())
    graph_schema = json.loads((SCHEMA_DIR / "graph_envelope_v1.schema.json").read_text())
    Draft202012Validator.check_schema(ops_schema)
    Draft202012Validator.check_schema(graph_schema)
    graph_resource = Resource.from_contents(graph_schema)
    registry = (
        Registry()
        .with_resource(graph_schema["$id"], graph_resource)
        .with_resource(
            "https://coordharness.dev/schema/graph_envelope_v1.schema.json",
            graph_resource,
        )
    )
    return Draft202012Validator(ops_schema, registry=registry)


def _bundle_validator() -> Draft202012Validator:
    bundle_schema = json.loads(
        (SCHEMA_DIR / "ops_atlas_bundle_v1.schema.json").read_text()
    )
    ops_schema = json.loads((SCHEMA_DIR / "ops_atlas_v1.schema.json").read_text())
    graph_schema = json.loads((SCHEMA_DIR / "graph_envelope_v1.schema.json").read_text())
    status_schema = json.loads((SCHEMA_DIR / "read_status_v1.schema.json").read_text())
    for schema in (bundle_schema, ops_schema, graph_schema, status_schema):
        Draft202012Validator.check_schema(schema)
    registry = Registry()
    for schema in (ops_schema, graph_schema, status_schema):
        registry = registry.with_resource(
            schema["$id"], Resource.from_contents(schema)
        )
    registry = registry.with_resource(
        "https://coordharness.dev/schema/graph_envelope_v1.schema.json",
        Resource.from_contents(graph_schema),
    )
    return Draft202012Validator(bundle_schema, registry=registry)


def test_ops_atlas_schema_validates_public_payload_and_rejects_extra_fields() -> None:
    validator = _validator()
    atlas = build_operations(*_documents())

    validator.validate(atlas)

    with pytest.raises(ValidationError, match="Additional properties are not allowed"):
        validator.validate({**atlas, "private_note": "prose must not enter the public surface"})


def test_ops_atlas_bundle_schema_validates_one_generation_receipt() -> None:
    snapshot, graph, context, timeline = _documents()
    operations = build_operations(snapshot, graph, context, timeline)
    generated_at = snapshot["generated_at"]
    bundle = {
        "schema_version": "OpsAtlasBundleV1",
        "generated_at": generated_at,
        "cache_generation": 7,
        "snapshot": snapshot,
        "graph": graph,
        "context": context,
        "timeline": timeline,
        "operations": operations,
        "read_status": {
            "schema_version": "ReadStatusV1",
            "generated_at": generated_at,
            "source": "board-cache",
            "read_only": True,
            "degraded": False,
            "cache_generation": 7,
            "source_generated_at": generated_at,
            "last_refresh_attempt": generated_at,
            "last_successful_refresh": generated_at,
            "consecutive_refresh_failures": 0,
            "last_failure_class": "",
            "refresh_interval_seconds": 2,
        },
    }

    _bundle_validator().validate(bundle)
    assert bundle["cache_generation"] == bundle["read_status"]["cache_generation"]

    with pytest.raises(ValidationError, match="Additional properties are not allowed"):
        _bundle_validator().validate({**bundle, "exception": "must stay private"})
