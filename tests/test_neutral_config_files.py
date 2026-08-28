from __future__ import annotations

import json
from pathlib import Path

from coordharness.coord import ingest
from coordharness.jobs import diagnostic_marker, status
from coordharness.knowledge import context_federator, kfts
from coordharness.testing import pytest_gate


def test_process_patterns_load_from_contained_file(tmp_path: Path) -> None:
    config = tmp_path / "patterns.json"
    config.write_text(json.dumps({"WORK-2": r"task\.py"}), encoding="utf-8")
    assert status.load_compute_proc_patterns(
        env={"COORD_JOB_PROCESS_PATTERNS_FILE": "patterns.json"}, root=tmp_path
    ) == {"WORK-2": r"task\.py"}


def test_snapshot_names_load_from_contained_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(diagnostic_marker.harness_config, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        diagnostic_marker.harness_config, "state_dir", lambda: tmp_path / ".coordharness"
    )
    config = tmp_path / "snapshots.json"
    config.write_text('["historical.json"]', encoding="utf-8")
    assert diagnostic_marker.configured_nonauthoritative_snapshot_names(
        env={"COORD_NONAUTHORITATIVE_SNAPSHOT_NAMES_FILE": "snapshots.json"}
    ) == frozenset({"historical.json"})


def test_knowledge_sources_load_from_contained_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(kfts, "_REPO_ROOT", tmp_path)
    config = tmp_path / "sources.json"
    config.write_text('["docs/**/*.md",".agents/**/*.md"]', encoding="utf-8")
    assert kfts.configured_vault_globs(
        env={"COORD_KNOWLEDGE_SOURCE_GLOBS_FILE": "sources.json"}
    ) == ("docs/**/*.md", ".agents/**/*.md")


def test_runtime_pointer_is_opt_in_and_does_not_create(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(context_federator, "REPO", tmp_path)
    destination = tmp_path / ".coordharness" / "knowledge" / "CURRENT"
    pointer = context_federator.configured_kfts_runtime_pointer(
        env={"COORD_KFTS_RUNTIME_POINTER": str(destination)}
    )
    assert pointer == destination.resolve()
    assert not destination.exists()


def test_grouping_rules_load_from_contained_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ingest.harness_config, "project_root", lambda: tmp_path)
    monkeypatch.setattr(ingest.harness_config, "state_dir", lambda: tmp_path / ".coordharness")
    config = tmp_path / "grouping.json"
    config.write_text(
        json.dumps(
            {
                "domains": ["engineering"],
                "modules": {"runtime": {"domain": "engineering"}},
            }
        ),
        encoding="utf-8",
    )
    parsed = ingest._grouping_config(env={"COORD_GROUPING_RULES_FILE": "grouping.json"})
    assert parsed["domains"] == ("engineering",)
    assert parsed["modules"]["runtime"]["domain"] == "engineering"


def test_pytest_roots_load_from_contained_file(tmp_path: Path) -> None:
    config = tmp_path / "pytest-roots.json"
    config.write_text('["tests","extensions/checks"]', encoding="utf-8")
    assert pytest_gate.configured_test_roots(
        tmp_path, env={"COORD_PYTEST_ROOTS_FILE": "pytest-roots.json"}
    ) == (Path("tests"), Path("extensions/checks"))
