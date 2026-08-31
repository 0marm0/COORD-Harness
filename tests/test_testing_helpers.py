from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordharness.testing import apfs_cow_cleanroom, pytest_gate, verified_artifact_skip


# =====================================================================
# pytest_gate
# =====================================================================


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_shard_exit_is_zero_selection_true_for_clean_empty_shard() -> None:
    shard = {
        "exit_code": 5,
        "timed_out": False,
        "rss_exceeded": False,
        "junit_sha256": "abc",
        "counts": {"tests": 0, "failures": 0, "errors": 0, "skipped": 0},
    }
    assert pytest_gate.shard_exit_is_zero_selection(shard) is True


def test_shard_exit_is_zero_selection_false_when_tests_ran() -> None:
    shard = {
        "exit_code": 5,
        "timed_out": False,
        "rss_exceeded": False,
        "junit_sha256": "abc",
        "counts": {"tests": 1, "failures": 0, "errors": 0, "skipped": 0},
    }
    assert pytest_gate.shard_exit_is_zero_selection(shard) is False


def test_shard_exit_is_admissible_true_for_exit_0_and_1() -> None:
    assert pytest_gate.shard_exit_is_admissible({"exit_code": 0}) is True
    assert pytest_gate.shard_exit_is_admissible({"exit_code": 1}) is True


def test_shard_exit_is_admissible_false_for_exit_2() -> None:
    assert pytest_gate.shard_exit_is_admissible({"exit_code": 2}) is False


def test_shard_exit_is_admissible_on_empty_shard_dict() -> None:
    assert pytest_gate.shard_exit_is_admissible({}) is False


def test_runner_is_live_false_for_nonpositive_pid() -> None:
    assert pytest_gate.runner_is_live({"runner_pid": 0}) is False
    assert pytest_gate.runner_is_live({"runner_pid": -5}) is False


def test_runner_is_live_false_for_missing_or_non_numeric_pid() -> None:
    assert pytest_gate.runner_is_live({}) is False
    assert pytest_gate.runner_is_live({"runner_pid": "not-a-pid"}) is False


def test_runner_is_live_false_for_pid_that_does_not_exist() -> None:
    # A PID this large is essentially guaranteed not to exist on any real system.
    assert pytest_gate.runner_is_live({"runner_pid": 2**30}) is False


def test_canonical_gate_pair_findings_empty_when_fields_agree() -> None:
    running = {"schema": "s", "run_id": "r1", "totals": {"tests": 3}}
    receipt = {"schema": "s", "run_id": "r1", "totals": {"tests": 3}}

    assert pytest_gate.canonical_gate_pair_findings(running, receipt) == []


def test_canonical_gate_pair_findings_flags_disagreement() -> None:
    running = {"schema": "s", "run_id": "r1"}
    receipt = {"schema": "s", "run_id": "r2"}

    findings = pytest_gate.canonical_gate_pair_findings(running, receipt)

    assert any("run_id" in f for f in findings)


def test_canonical_gate_pair_findings_on_two_empty_dicts_is_empty() -> None:
    assert pytest_gate.canonical_gate_pair_findings({}, {}) == []


def test_configured_test_roots_defaults_to_tests(tmp_path: Path) -> None:
    assert pytest_gate.configured_test_roots(tmp_path, env={}) == (Path("tests"),)


def test_configured_test_roots_rejects_absolute_and_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pytest_gate.configured_test_roots(
            tmp_path, env={"COORD_PYTEST_ROOTS_JSON": '["../escape"]'}
        )


def test_configured_test_roots_rejects_both_inline_and_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pytest_gate.configured_test_roots(
            tmp_path,
            env={
                "COORD_PYTEST_ROOTS_JSON": "[]",
                "COORD_PYTEST_ROOTS_FILE": "roots.json",
            },
        )


def test_discover_test_files_finds_only_test_prefixed_python_files(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_x(): pass\n")
    (tests_dir / "helpers.py").write_text("def not_a_test(): pass\n")
    (tests_dir / "b_test.py").write_text("def test_y(): pass\n")

    found = pytest_gate.discover_test_files(tmp_path)

    assert found == ["tests/b_test.py", "tests/test_a.py"]


def test_discover_test_files_on_repo_with_no_tests_dir_is_empty(tmp_path: Path) -> None:
    assert pytest_gate.discover_test_files(tmp_path) == []


def test_memory_singleton_test_files_detects_marker(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    marked = tests_dir / "test_marked.py"
    marked.write_text(f"{pytest_gate.MEMORY_SINGLETON_MARKER}\ndef test_x(): pass\n")
    plain = tests_dir / "test_plain.py"
    plain.write_text("def test_y(): pass\n")

    result = pytest_gate.memory_singleton_test_files(
        tmp_path, ["tests/test_marked.py", "tests/test_plain.py"]
    )

    assert result == ["tests/test_marked.py"]


def test_memory_singleton_test_files_on_empty_list_is_empty(tmp_path: Path) -> None:
    assert pytest_gate.memory_singleton_test_files(tmp_path, []) == []


def test_shard_test_files_isolates_memory_singletons_into_their_own_bin(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    singleton = tests_dir / "test_singleton.py"
    singleton.write_text(f"{pytest_gate.MEMORY_SINGLETON_MARKER}\ndef test_x(): pass\n")
    ordinary = tests_dir / "test_ordinary.py"
    ordinary.write_text("def test_y(): pass\n")

    groups = pytest_gate.shard_test_files(
        tmp_path, ["tests/test_singleton.py", "tests/test_ordinary.py"], shard_count=2
    )

    assert ["tests/test_singleton.py"] in groups
    assert ["tests/test_ordinary.py"] in groups


def test_shard_test_files_rejects_empty_file_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pytest_gate.shard_test_files(tmp_path, [], shard_count=4)


def test_shard_test_files_rejects_nonpositive_shard_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pytest_gate.shard_test_files(tmp_path, ["tests/test_a.py"], shard_count=0)


def test_shard_plan_sha256_is_stable_and_order_sensitive() -> None:
    a = pytest_gate.shard_plan_sha256([["x.py"], ["y.py"]])
    b = pytest_gate.shard_plan_sha256([["x.py"], ["y.py"]])
    c = pytest_gate.shard_plan_sha256([["y.py"], ["x.py"]])

    assert a == b
    assert a != c


def test_shard_plan_sha256_on_empty_groups() -> None:
    assert pytest_gate.shard_plan_sha256([]) == pytest_gate.shard_plan_sha256([])


def test_recommended_shard_count_respects_minimum() -> None:
    assert pytest_gate.recommended_shard_count(3, minimum=10, max_files_per_shard=8) == 3


def test_recommended_shard_count_scales_with_capacity_need() -> None:
    # 100 files, 8 per shard -> needs 13 shards, above the default minimum of 1.
    assert (
        pytest_gate.recommended_shard_count(100, minimum=1, max_files_per_shard=8) == 13
    )


def test_recommended_shard_count_rejects_nonpositive_file_count() -> None:
    with pytest.raises(ValueError):
        pytest_gate.recommended_shard_count(0)


def test_git_source_commit_returns_full_sha_for_a_real_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    commit = pytest_gate.git_source_commit(repo)

    assert len(commit) == 40
    assert all(c in "0123456789abcdef" for c in commit)


def test_git_source_commit_raises_when_no_commits_exist(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)  # no commits made

    with pytest.raises(ValueError):
        pytest_gate.git_source_commit(repo)


def test_git_source_commit_raises_on_nonexistent_repo(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pytest_gate.git_source_commit(tmp_path / "does-not-exist")


# =====================================================================
# verified_artifact_skip
# =====================================================================


def test_skip_unmeasured_skips_by_default() -> None:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        verified_artifact_skip.skip_unmeasured(guard_id="g1", reason="no artifact yet")

    assert verified_artifact_skip._MARKER in str(excinfo.value)


def test_skip_unmeasured_fails_loudly_when_expected_true() -> None:
    with pytest.raises(pytest.fail.Exception) as excinfo:
        verified_artifact_skip.skip_unmeasured(
            guard_id="g1", reason="missing but was expected", expected=True
        )

    assert verified_artifact_skip._MARKER in str(excinfo.value)


def test_require_verified_artifact_returns_path_when_present(tmp_path: Path) -> None:
    artifact = tmp_path / "proof.json"
    artifact.write_text("{}")

    result = verified_artifact_skip.require_verified_artifact(artifact, guard_id="g1")

    assert result == artifact


def test_require_verified_artifact_skips_when_missing(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        verified_artifact_skip.require_verified_artifact(
            tmp_path / "missing.json", guard_id="g1"
        )


def test_parse_unmeasured_skip_round_trips_through_skip_unmeasured() -> None:
    try:
        verified_artifact_skip.skip_unmeasured(
            guard_id="guard-x", reason="unmeasured", artifact="a.json"
        )
    except pytest.skip.Exception as exc:
        parsed = verified_artifact_skip.parse_unmeasured_skip(str(exc))

    assert parsed is not None
    assert parsed.guard_id == "guard-x"
    assert parsed.artifact == "a.json"
    assert parsed.expected is False


def test_parse_unmeasured_skip_returns_none_for_untyped_reason() -> None:
    assert verified_artifact_skip.parse_unmeasured_skip("some ordinary skip reason") is None


def test_parse_unmeasured_skip_returns_none_for_empty_string() -> None:
    assert verified_artifact_skip.parse_unmeasured_skip("") is None


# =====================================================================
# apfs_cow_cleanroom
# =====================================================================


def test_sha256_file_matches_hashlib_for_known_content(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "f.bin"
    path.write_bytes(b"the quick brown fox")

    assert apfs_cow_cleanroom.sha256_file(path) == hashlib.sha256(b"the quick brown fox").hexdigest()


def test_sha256_file_on_empty_file(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "empty.bin"
    path.write_bytes(b"")

    assert apfs_cow_cleanroom.sha256_file(path) == hashlib.sha256(b"").hexdigest()


def test_sha256_file_on_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        apfs_cow_cleanroom.sha256_file(tmp_path / "missing.bin")


def test_stable_manifest_is_sorted_and_hash_is_content_derived(tmp_path: Path) -> None:
    entries = [
        apfs_cow_cleanroom.TopologyEntry(store_id="z", path="b.txt", kind="file", mode=0o644, size=1),
        apfs_cow_cleanroom.TopologyEntry(store_id="a", path="a.txt", kind="file", mode=0o644, size=1),
    ]

    manifest = apfs_cow_cleanroom.stable_manifest(entries)

    assert [row["store_id"] for row in manifest["entries"]] == ["a", "z"]
    assert manifest["sha256"]
    # Reordering the input must not change the canonical output.
    reordered = apfs_cow_cleanroom.stable_manifest(list(reversed(entries)))
    assert reordered["sha256"] == manifest["sha256"]


def test_stable_manifest_on_empty_entries() -> None:
    manifest = apfs_cow_cleanroom.stable_manifest([])

    assert manifest["entries"] == []
    assert manifest["sha256"]


def test_longest_prefix_mapping_picks_the_deepest_matching_source() -> None:
    mappings = {
        "/data": "/dest-data",
        "/data/models": "/dest-models",
    }

    result = apfs_cow_cleanroom.longest_prefix_mapping("/data/models/foo.bin", mappings)

    assert result is not None
    source, destination, relative = result
    assert str(source) == "/data/models"
    assert str(destination) == "/dest-models"
    assert str(relative) == "foo.bin"


def test_longest_prefix_mapping_returns_none_when_no_prefix_matches() -> None:
    result = apfs_cow_cleanroom.longest_prefix_mapping("/elsewhere/foo.bin", {"/data": "/dest"})

    assert result is None


def test_longest_prefix_mapping_on_empty_mappings() -> None:
    assert apfs_cow_cleanroom.longest_prefix_mapping("/data/foo.bin", {}) is None


def test_rewritten_symlink_target_maps_into_the_destination_store() -> None:
    target, classification, rewritten = apfs_cow_cleanroom.rewritten_symlink_target(
        source_link=Path("/data/models/link"),
        destination_link=Path("/clone/models/link"),
        raw_target="/data/models/real-file.bin",
        mappings={"/data": "/clone"},
    )

    assert classification == "mapped_store"
    assert rewritten == "real-file.bin"


def test_rewritten_symlink_target_allows_external_allowlisted_target() -> None:
    target, classification, rewritten = apfs_cow_cleanroom.rewritten_symlink_target(
        source_link=Path("/data/link"),
        destination_link=Path("/clone/link"),
        raw_target="/System/Library/thing",
        mappings={"/data": "/clone"},
        external_allowlist=["/System"],
    )

    assert classification == "external_readonly"
    assert rewritten == "/System/Library/thing"


def test_rewritten_symlink_target_refuses_unmapped_unallowlisted_target() -> None:
    with pytest.raises(apfs_cow_cleanroom.CleanroomRefusal) as excinfo:
        apfs_cow_cleanroom.rewritten_symlink_target(
            source_link=Path("/data/link"),
            destination_link=Path("/clone/link"),
            raw_target="/outside/somewhere",
            mappings={"/data": "/clone"},
        )

    assert excinfo.value.state == "REFUSED_EXTERNAL_LINK"


def test_rewritten_symlink_target_refuses_relative_parent_traversal() -> None:
    with pytest.raises(apfs_cow_cleanroom.CleanroomRefusal) as excinfo:
        apfs_cow_cleanroom.rewritten_symlink_target(
            source_link=Path("/data/models/link"),
            destination_link=Path("/clone/models/link"),
            raw_target="../../etc/passwd",
            mappings={"/data": "/clone"},
        )

    assert excinfo.value.state == "REFUSED_SOURCE_TOPOLOGY"


def test_zero_mutation_counters_are_all_zero() -> None:
    counters = apfs_cow_cleanroom.zero_mutation_counters()

    assert counters == {
        "clone_calls": 0,
        "checkout_calls": 0,
        "sqlite_backup_calls": 0,
        "sandbox_launch_calls": 0,
        "pytest_launch_calls": 0,
    }


def test_sandbox_profile_text_denies_by_default_and_denies_network() -> None:
    text = apfs_cow_cleanroom.sandbox_profile_text()

    assert "(deny default)" in text
    assert "(deny network*)" in text


def test_render_venv_python_shim_embeds_quoted_paths() -> None:
    shim = apfs_cow_cleanroom.render_venv_python_shim(
        shared_python=Path("/shared/bin/python3"),
        clone_source=Path("/clone/src"),
        shared_site_packages=Path("/shared/site-packages"),
    )

    assert "/shared/bin/python3" in shim
    assert "PYTHONPATH=" in shim
    assert "/clone/src" in shim
    assert shim.startswith("#!/bin/sh\n")


def test_write_venv_python_shim_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    shim_path = tmp_path / "python-shim"
    shim_path.write_text("existing")

    with pytest.raises(apfs_cow_cleanroom.CleanroomRefusal) as excinfo:
        apfs_cow_cleanroom.write_venv_python_shim(
            shim_path,
            shared_python=Path("/shared/bin/python3"),
            clone_source=Path("/clone/src"),
            shared_site_packages=Path("/shared/site-packages"),
        )

    assert excinfo.value.state == "REFUSED_VENV_LEAK"


def test_write_venv_python_shim_writes_executable_file(tmp_path: Path) -> None:
    shim_path = tmp_path / "nested" / "python-shim"

    digest = apfs_cow_cleanroom.write_venv_python_shim(
        shim_path,
        shared_python=Path("/shared/bin/python3"),
        clone_source=Path("/clone/src"),
        shared_site_packages=Path("/shared/site-packages"),
    )

    assert shim_path.exists()
    assert shim_path.stat().st_mode & 0o111  # executable bits set
    assert len(digest) == 64


def test_validate_sandbox_roots_accepts_physically_disjoint_directories(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    cleanroom = tmp_path / "cleanroom"
    canonical.mkdir()
    cleanroom.mkdir()

    result = apfs_cow_cleanroom.validate_sandbox_roots(
        canonical_root=canonical, cleanroom_root=cleanroom
    )

    assert result["physically_disjoint"] is True


def test_validate_sandbox_roots_refuses_when_cleanroom_is_nested_in_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    cleanroom = canonical / "nested-cleanroom"
    cleanroom.mkdir()

    with pytest.raises(apfs_cow_cleanroom.CleanroomRefusal) as excinfo:
        apfs_cow_cleanroom.validate_sandbox_roots(
            canonical_root=canonical, cleanroom_root=cleanroom
        )

    assert excinfo.value.state == "REFUSED_SANDBOX_RED_ARM"


def test_validate_sandbox_roots_raises_when_canonical_root_is_missing(tmp_path: Path) -> None:
    cleanroom = tmp_path / "cleanroom"
    cleanroom.mkdir()

    with pytest.raises(apfs_cow_cleanroom.CleanroomRefusal):
        apfs_cow_cleanroom.validate_sandbox_roots(
            canonical_root=tmp_path / "does-not-exist", cleanroom_root=cleanroom
        )
