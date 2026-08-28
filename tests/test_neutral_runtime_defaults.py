from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.coord import ingest
from coordharness.jobs import diagnostic_marker, status
from coordharness.knowledge import context_federator, kfts
from coordharness.testing import pytest_gate


def test_status_defaults_are_neutral_and_extensions_are_validated(tmp_path: Path) -> None:
    assert status.COMPUTE_PROC_PATTERNS == {}
    assert status.load_compute_proc_patterns(env={}, root=tmp_path) == {}
    configured = status.load_compute_proc_patterns(
        env={"COORD_JOB_PROCESS_PATTERNS_JSON": json.dumps({"WORK-1": r"worker\.py"})},
        root=tmp_path,
    )
    assert configured == {"WORK-1": r"worker\.py"}
    with pytest.raises(ValueError, match="invalid regex"):
        status.load_compute_proc_patterns(
            env={"COORD_JOB_PROCESS_PATTERNS_JSON": '{"WORK-1":"["}'}, root=tmp_path
        )


def test_status_done_signal_reads_are_contained(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    artifact = docs / "result.json"
    artifact.write_text("{}", encoding="utf-8")
    assert status.resolve_done_signal("docs/result.json", tmp_path) == artifact.resolve()
    assert status.resolve_done_signal(str(tmp_path.parent / "outside.json"), tmp_path) is None
    assert tuple(status._candidate_paths("../outside.json", tmp_path)) == ()


def test_diagnostic_snapshot_names_are_opt_in_and_basename_only() -> None:
    assert diagnostic_marker.LEGACY_NONAUTHORITATIVE_SNAPSHOT_NAMES == frozenset()
    assert diagnostic_marker.configured_nonauthoritative_snapshot_names(env={}) == frozenset()
    names = diagnostic_marker.configured_nonauthoritative_snapshot_names(
        env={"COORD_NONAUTHORITATIVE_SNAPSHOT_NAMES_JSON": '["historical.json"]'}
    )
    assert names == frozenset({"historical.json"})
    with pytest.raises(ValueError, match="basename"):
        diagnostic_marker.configured_nonauthoritative_snapshot_names(
            env={"COORD_NONAUTHORITATIVE_SNAPSHOT_NAMES_JSON": '["../escape.json"]'}
        )


def test_optional_diagnostic_configuration_does_not_create_paths(
    monkeypatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr(diagnostic_marker.harness_config, "project_root", lambda: tmp_path)
    monkeypatch.setattr(diagnostic_marker.harness_config, "state_dir", lambda: state)
    destination = state / "control"
    resolved = diagnostic_marker.configured_control_dir(
        env={"COORD_JOB_CONTROL_DIR": str(destination)}
    )
    assert resolved == destination.resolve()
    assert not destination.exists()


def test_kfts_defaults_only_use_standalone_owned_roots() -> None:
    assert kfts.REBUILD_INDEX_API == "coordharness.knowledge.kfts.rebuild_index"
    assert kfts.configured_vault_globs(env={}) == kfts.DEFAULT_VAULT_GLOBS
    allowed = set(kfts._ALLOWED_SOURCE_ROOTS) | set(kfts._ALLOWED_ROOT_FILES)
    for pattern in kfts.DEFAULT_VAULT_GLOBS:
        assert Path(pattern).parts[0] in allowed
    assert kfts.MEMORY_MIRROR_REL.startswith(".agents/")


def test_kfts_configured_extensions_and_containment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(kfts, "_REPO_ROOT", tmp_path)
    configured = kfts.configured_vault_globs(
        env={"COORD_KNOWLEDGE_SOURCE_GLOBS_JSON": '["docs/**/*.md","tests/notes/*.md"]'}
    )
    assert configured == ("docs/**/*.md", "tests/notes/*.md")
    with pytest.raises(ValueError, match="allowed roots"):
        kfts.configured_vault_globs(
            env={"COORD_KNOWLEDGE_SOURCE_GLOBS_JSON": '["../outside/**/*.md"]'}
        )
    outside = tmp_path.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link_root = tmp_path / "docs"
    link_root.mkdir()
    (link_root / "escape.md").symlink_to(outside)
    assert list(kfts._iter_vault_files(("docs/**/*.md",), ())) == []


def test_context_defaults_and_pointer_reads_are_contained(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(context_federator, "REPO", tmp_path)
    monkeypatch.setattr(context_federator, "COORDHARNESS", tmp_path)
    assert context_federator.KFTS_V2_RUNTIME_POINTER is None
    assert context_federator.configured_kfts_runtime_pointer(env={}) is None
    docs = tmp_path / "docs"
    docs.mkdir()
    note = docs / "note.md"
    note.write_text("# Note", encoding="utf-8")
    assert context_federator._path_from_local_pointer("docs/note.md") == note.resolve()
    assert context_federator._path_from_local_pointer(str(tmp_path.parent / "outside.md")) is None
    assert context_federator._path_from_local_pointer("unknown/note.md") is None


def test_grouping_is_explicit_and_configurable(monkeypatch) -> None:
    for key in ("COORD_GROUPING_RULES_JSON", "COORD_GROUPING_RULES_FILE"):
        monkeypatch.delenv(key, raising=False)
    assert ingest._resolve_grouping("W-1", {"title": "No taxonomy inference"}) == (
        None,
        None,
        None,
    )
    monkeypatch.setenv(
        "COORD_GROUPING_RULES_JSON",
        json.dumps(
            {
                "domains": ["engineering"],
                "modules": {"runtime": {"domain": "engineering", "sublane": "workers"}},
            }
        ),
    )
    assert ingest._resolve_grouping("W-1", {"module": "runtime"}) == (
        "runtime",
        "engineering",
        "workers",
    )
    assert ingest._valid_domain("engineering") is True
    assert ingest._valid_domain("undeclared") is False


def test_pytest_gate_defaults_and_configured_roots_are_contained(tmp_path: Path) -> None:
    assert pytest_gate.configured_test_roots(tmp_path, env={}) == (Path("tests"),)
    roots = pytest_gate.configured_test_roots(
        tmp_path, env={"COORD_PYTEST_ROOTS_JSON": '["tests","extensions/checks"]'}
    )
    assert roots == (Path("tests"), Path("extensions/checks"))
    with pytest.raises(ValueError, match="repository-relative"):
        pytest_gate.configured_test_roots(
            tmp_path, env={"COORD_PYTEST_ROOTS_JSON": '["../outside"]'}
        )
    receipt, running = pytest_gate.pytest_gate_paths(tmp_path, env={})
    assert receipt == (tmp_path / ".coordharness/pytest_gate/receipt.json").resolve()
    assert running == (tmp_path / ".coordharness/pytest_gate/running.json").resolve()
    assert not receipt.parent.exists()


def test_pytest_discovery_uses_only_configured_roots(tmp_path: Path, monkeypatch) -> None:
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "test_default.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    extension = tmp_path / "extensions" / "checks"
    extension.mkdir(parents=True)
    (extension / "test_extension.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    monkeypatch.setenv("COORD_PYTEST_ROOTS_JSON", '["tests","extensions/checks"]')
    assert pytest_gate.discover_test_files(tmp_path) == [
        "extensions/checks/test_extension.py",
        "tests/test_default.py",
    ]
