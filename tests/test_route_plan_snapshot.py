from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _surface() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "route_plan_snapshot.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("route_plan_snapshot.py was not found")


def _snapshot() -> dict:
    return {
        "schema_version": "RoutePlanSnapshotV1",
        "work": {
            "work_id": "WORK-101",
            "version": 7,
            "assignee": "lane-a",
            "review_tier": "T1",
            "required_capabilities": ["python"],
        },
        "assignment_heads": [{"event_id": 12, "lane": "lane-a"}],
        "lanes": [
            {
                "lane": "lane-a",
                "available": True,
                "auth_available": True,
                "capabilities": ["python"],
                "active_load": 1,
                "load_capacity": 10,
            },
            {
                "lane": "lane-b",
                "available": True,
                "auth_available": True,
                "capabilities": ["python"],
                "active_load": 5,
                "load_capacity": 10,
            },
        ],
        "usage": [
            {
                "lane": "lane-a",
                "headroom": 80,
                "limit": 100,
                "coverage_complete": True,
                "observed_at": "2026-08-31T11:58:00Z",
            },
            {
                "lane": "lane-b",
                "headroom": 40,
                "limit": 100,
                "coverage_complete": True,
                "observed_at": "2026-08-31T11:58:00Z",
            },
        ],
        "policy": {
            "version": "2026-08-31.v1",
            "sha256": hashlib.sha256(b"assignment routing policy v1").hexdigest(),
            "max_usage_age_seconds": 900,
        },
        "timestamps": {
            "evidence_observed_at": "2026-08-31T11:58:00Z",
            "now": "2026-08-31T12:00:00Z",
            "expires_at": "2026-08-31T12:05:00Z",
        },
    }


def _run(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_surface())],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )


def test_valid_snapshot_emits_canonical_non_mutating_route_plan() -> None:
    snapshot = _snapshot()
    snapshot["explicit_user_route"] = "lane-b"

    result = _run(json.dumps(snapshot))

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    document = json.loads(result.stdout)
    assert document["schema_version"] == "RoutePlanV1"
    assert document["work_id"] == "WORK-101"
    assert document["selected_lane"] == "lane-b"
    assert document["reason_codes"] == ["explicit_user_route_honored"]
    assert document["mutation_allowed"] is False
    assert document["work_fence"] == {
        "assignee": "lane-a",
        "head_event_ids": [12],
        "version": 7,
    }
    assert len(document["plan_sha256"]) == 64
    assert result.stdout == json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def test_any_stale_lane_evidence_fails_closed_without_a_plan() -> None:
    snapshot = copy.deepcopy(_snapshot())
    snapshot["usage"][0]["observed_at"] = "2026-08-31T11:00:00Z"

    result = _run(json.dumps(snapshot))

    assert result.returncode != 0
    assert result.stdout == ""
    assert "usage_evidence_stale" in result.stderr


def test_invalid_or_ambiguous_json_fails_closed_nonzero() -> None:
    invalid = _snapshot()
    invalid["live_database_path"] = "coord.db"
    unknown = _run(json.dumps(invalid))
    duplicate = _run('{"schema_version":"RoutePlanSnapshotV1","schema_version":"wrong"}')
    missing_usage = _snapshot()
    missing_usage["usage"].pop()
    incomplete = _run(json.dumps(missing_usage))

    assert unknown.returncode != 0
    assert unknown.stdout == ""
    assert "unknown fields: live_database_path" in unknown.stderr
    assert duplicate.returncode != 0
    assert duplicate.stdout == ""
    assert "duplicate JSON key: schema_version" in duplicate.stderr
    assert incomplete.returncode != 0
    assert incomplete.stdout == ""
    assert "usage lanes must exactly match" in incomplete.stderr


def test_surface_has_no_database_or_lifecycle_writer_imports() -> None:
    source = _surface().read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "sqlite3" not in imported
    assert "coord_db" not in source
    assert not any(
        name.endswith((".cli", ".coord_db", ".native_cockpit", ".mcp_coord_server"))
        for name in imported
    )
