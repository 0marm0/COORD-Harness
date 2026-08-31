from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordharness.runtime import (
    console_release_retention,
    native_thread_guard,
    release_inventory_policy,
    served_build_identity,
)


# =====================================================================
# native_thread_guard
# =====================================================================


def test_guard_enabled_defaults_true_on_empty_env() -> None:
    assert native_thread_guard.guard_enabled({}) is True


def test_guard_enabled_false_when_env_says_off() -> None:
    assert native_thread_guard.guard_enabled({"COORD_NATIVE_THREAD_GUARD": "0"}) is False
    assert native_thread_guard.guard_enabled({"COORD_NATIVE_THREAD_GUARD": "off"}) is False


def test_canonical_profile_unknown_value_falls_back_to_serial() -> None:
    assert native_thread_guard.canonical_profile("nonsense") == "serial"


def test_canonical_profile_inherit_aliases() -> None:
    assert native_thread_guard.canonical_profile("respect") == "inherit"
    assert native_thread_guard.canonical_profile(None) == "serial"


def test_profile_defaults_inherit_only_sets_openmp_dup_flag() -> None:
    defaults = native_thread_guard.profile_defaults("inherit")

    assert defaults == {"KMP_DUPLICATE_LIB_OK": "TRUE"}


def test_profile_defaults_perf_sets_eight_threads_everywhere() -> None:
    defaults = native_thread_guard.profile_defaults("perf")

    for key in native_thread_guard.THREAD_ENV_KEYS:
        assert defaults[key] == "8"


def test_apply_startup_guard_disabled_marks_inactive_and_touches_nothing_else() -> None:
    env: dict[str, str] = {"COORD_NATIVE_THREAD_GUARD": "0"}

    result = native_thread_guard.apply_startup_guard(env)

    assert result == {}
    assert env["COORD_NATIVE_THREAD_GUARD_ACTIVE"] == "0"
    assert "OMP_NUM_THREADS" not in env


def test_apply_startup_guard_overwrites_by_default() -> None:
    env: dict[str, str] = {"OMP_NUM_THREADS": "99"}

    native_thread_guard.apply_startup_guard(env)

    assert env["OMP_NUM_THREADS"] == "1"  # serial default
    assert env["COORD_NATIVE_THREAD_GUARD_ACTIVE"] == "1"
    assert env["COORD_NATIVE_THREAD_PROFILE_ACTIVE"] == "serial"


def test_apply_startup_guard_respects_overrides_when_asked() -> None:
    env: dict[str, str] = {
        "OMP_NUM_THREADS": "99",
        "COORD_NATIVE_THREAD_GUARD_RESPECT_OVERRIDES": "1",
    }

    native_thread_guard.apply_startup_guard(env)

    assert env["OMP_NUM_THREADS"] == "99"


# =====================================================================
# release_inventory_policy
# =====================================================================


def test_is_reviewed_ambient_release_path_flags_ds_store_and_pycache() -> None:
    assert release_inventory_policy.is_reviewed_ambient_release_path("a/.DS_Store") is True
    assert release_inventory_policy.is_reviewed_ambient_release_path("a/__pycache__/x.pyc") is True


def test_is_reviewed_ambient_release_path_does_not_flag_ordinary_file() -> None:
    assert release_inventory_policy.is_reviewed_ambient_release_path("src/module.py") is False


def test_is_reviewed_ambient_release_path_on_empty_string() -> None:
    assert release_inventory_policy.is_reviewed_ambient_release_path("") is False


def test_reviewed_runtime_inventory_hashes_regular_files_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / "b.txt").write_text("world")
    (root / ".DS_Store").write_text("junk")

    digest_a, count_a = release_inventory_policy.reviewed_runtime_inventory(root)
    digest_b, count_b = release_inventory_policy.reviewed_runtime_inventory(root)

    assert count_a == 2  # .DS_Store excluded
    assert digest_a == digest_b


def test_reviewed_runtime_inventory_hash_changes_with_content(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    digest_before, _ = release_inventory_policy.reviewed_runtime_inventory(root)

    (root / "a.txt").write_text("hello!")
    digest_after, _ = release_inventory_policy.reviewed_runtime_inventory(root)

    assert digest_before != digest_after


def test_reviewed_runtime_inventory_records_symlinks_without_following(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside content")
    (root / "link.txt").symlink_to(target)

    digest, count = release_inventory_policy.reviewed_runtime_inventory(root)

    assert count == 1
    assert digest  # non-empty hex digest


def test_reviewed_runtime_inventory_raises_on_unsupported_node(tmp_path: Path) -> None:
    root = tmp_path / "release"
    root.mkdir()
    fifo_path = root / "pipe"
    os.mkfifo(fifo_path)
    try:
        with pytest.raises(release_inventory_policy.ReleaseInventoryError):
            release_inventory_policy.reviewed_runtime_inventory(root)
    finally:
        fifo_path.unlink()


def test_reviewed_runtime_inventory_on_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "empty-release"
    root.mkdir()

    digest, count = release_inventory_policy.reviewed_runtime_inventory(root)

    assert count == 0
    assert digest


def test_reviewed_runtime_inventory_on_nonexistent_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        release_inventory_policy.reviewed_runtime_inventory(tmp_path / "does-not-exist")


# =====================================================================
# served_build_identity
# =====================================================================


def test_capture_served_build_identity_uses_valid_release_env_pins() -> None:
    commit = "a" * 40
    tree = "b" * 40
    identity = served_build_identity.capture_served_build_identity(
        environ={"COORD_RELEASE_COMMIT": commit, "COORD_RELEASE_TREE": tree},
        captured_at_utc="2026-01-01T00:00:00+00:00",
    )

    assert identity.commit == commit
    assert identity.tree == tree
    assert identity.source == "release_env"
    assert identity.exact_commit is True
    assert identity.checkout_dirty_at_boot is False


def test_capture_served_build_identity_malformed_pins_without_integrity_required() -> None:
    identity = served_build_identity.capture_served_build_identity(
        environ={"COORD_RELEASE_COMMIT": "not-a-sha"},
        captured_at_utc="2026-01-01T00:00:00+00:00",
    )

    assert identity.source == "invalid_release_env"
    assert identity.commit is None
    assert identity.exact_commit is False


def test_capture_served_build_identity_malformed_pins_with_integrity_required_raises() -> None:
    with pytest.raises(RuntimeError):
        served_build_identity.capture_served_build_identity(
            environ={
                "COORD_RELEASE_COMMIT": "not-a-sha",
                "COORD_RELEASE_INTEGRITY_REQUIRED": "1",
            },
        )


def test_capture_served_build_identity_falls_back_to_git_identity_callable() -> None:
    commit = "c" * 40
    tree = "d" * 40

    def fake_git_identity(repo: Path) -> tuple[str, str, bool]:
        return commit, tree, False

    identity = served_build_identity.capture_served_build_identity(
        environ={},
        repo=Path("/irrelevant"),
        git_identity=fake_git_identity,
        captured_at_utc="2026-01-01T00:00:00+00:00",
    )

    assert identity.commit == commit
    assert identity.source == "checkout_head_at_boot"
    assert identity.exact_commit is True
    assert identity.checkout_dirty_at_boot is False


def test_capture_served_build_identity_dirty_checkout_is_not_exact() -> None:
    def fake_git_identity(repo: Path) -> tuple[str, str, bool]:
        return "e" * 40, "f" * 40, True

    identity = served_build_identity.capture_served_build_identity(
        environ={},
        repo=Path("/irrelevant"),
        git_identity=fake_git_identity,
    )

    assert identity.exact_commit is False
    assert identity.checkout_dirty_at_boot is True


def test_capture_served_build_identity_handles_git_failure() -> None:
    def failing_git_identity(repo: Path) -> tuple[str, str, bool]:
        raise RuntimeError("git unavailable")

    identity = served_build_identity.capture_served_build_identity(
        environ={},
        repo=Path("/irrelevant"),
        git_identity=failing_git_identity,
    )

    assert identity.source == "unavailable"
    assert identity.commit is None


def test_compare_served_build_matched() -> None:
    commit = "1" * 40
    observed = served_build_identity.ServedBuildIdentity(
        commit=commit,
        tree="2" * 40,
        source="checkout_head_at_boot",
        captured_at_utc="2026-01-01T00:00:00+00:00",
        exact_commit=True,
        checkout_dirty_at_boot=False,
    ).as_payload()

    result = served_build_identity.compare_served_build(commit, observed)

    assert result["verdict"] == "MATCHED"


def test_compare_served_build_stale() -> None:
    observed = served_build_identity.ServedBuildIdentity(
        commit="1" * 40,
        tree="2" * 40,
        source="checkout_head_at_boot",
        captured_at_utc="2026-01-01T00:00:00+00:00",
        exact_commit=True,
        checkout_dirty_at_boot=False,
    ).as_payload()

    result = served_build_identity.compare_served_build("3" * 40, observed)

    assert result["verdict"] == "STALE_BUILD"


def test_compare_served_build_unknown_expected_on_malformed_expected_commit() -> None:
    result = served_build_identity.compare_served_build("not-a-sha", {})

    assert result["verdict"] == "UNKNOWN_EXPECTED_BUILD"


def test_compare_served_build_unknown_served_when_not_exact_commit() -> None:
    observed = served_build_identity.ServedBuildIdentity(
        commit="1" * 40,
        tree="2" * 40,
        source="checkout_head_at_boot",
        captured_at_utc="2026-01-01T00:00:00+00:00",
        exact_commit=False,
        checkout_dirty_at_boot=True,
    ).as_payload()

    result = served_build_identity.compare_served_build("1" * 40, observed)

    assert result["verdict"] == "UNKNOWN_SERVED_BUILD"


def test_compare_served_build_on_empty_observed_mapping() -> None:
    result = served_build_identity.compare_served_build("1" * 40, {})

    assert result["verdict"] == "UNKNOWN_SERVED_BUILD"
    assert result["observed_commit"] is None


# =====================================================================
# console_release_retention
# =====================================================================


def _build_runtime(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A runtime root with current/previous pointers plus extra releases."""
    runtime = tmp_path / "runtime"
    releases = runtime / "releases"
    releases.mkdir(parents=True)

    current_name = "a" * 40
    previous_name = "b" * 40
    (releases / current_name).mkdir()
    (releases / previous_name).mkdir()
    (runtime / "current").symlink_to(releases / current_name)
    (runtime / "previous").symlink_to(releases / previous_name)
    return runtime, releases, current_name, previous_name


def test_build_retention_plan_protects_current_and_previous(tmp_path: Path) -> None:
    runtime, releases, current_name, previous_name = _build_runtime(tmp_path)

    plan = console_release_retention.build_retention_plan(runtime)

    assert set(plan["protected"]) == {current_name, previous_name}
    delete_names = {entry["name"] for entry in plan["delete"]}
    assert delete_names.isdisjoint(plan["protected"])
    assert plan["plan_sha256"]


def test_build_retention_plan_keeps_only_the_newest_unreferenced_completes(tmp_path: Path) -> None:
    runtime, releases, current_name, previous_name = _build_runtime(tmp_path)
    now = time.time()
    extras = ["c" * 40, "d" * 40, "e" * 40]
    for offset, name in enumerate(extras):
        path = releases / name
        path.mkdir()
        # Oldest first: extras[0] is oldest, extras[-1] is newest.
        stamp = now - (len(extras) - offset) * 100
        os.utime(path, (stamp, stamp))

    plan = console_release_retention.build_retention_plan(
        runtime, keep_unreferenced_complete=2, now=now
    )

    deleted = {entry["name"] for entry in plan["delete"]}
    assert deleted == {extras[0]}  # only the single oldest extra is beyond the keep-2 window
    assert set(plan["retained_rollback"]) == {extras[1], extras[2]}


def test_build_retention_plan_deletes_old_unreferenced_partial(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    partial_name = ("f" * 40) + ".partial"
    partial_path = releases / partial_name
    partial_path.mkdir()
    now = time.time()
    old_stamp = now - console_release_retention.DEFAULT_PARTIAL_MIN_AGE_S - 3600
    os.utime(partial_path, (old_stamp, old_stamp))

    plan = console_release_retention.build_retention_plan(runtime, now=now)

    deleted = {entry["name"] for entry in plan["delete"]}
    assert partial_name in deleted


def test_build_retention_plan_keeps_referenced_partial_even_when_old(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    partial_name = ("f" * 40) + ".partial"
    partial_path = releases / partial_name
    partial_path.mkdir()
    now = time.time()
    old_stamp = now - console_release_retention.DEFAULT_PARTIAL_MIN_AGE_S - 3600
    os.utime(partial_path, (old_stamp, old_stamp))
    (runtime / "pointer.json").write_text(f'{{"pinned": "{partial_name}"}}')

    plan = console_release_retention.build_retention_plan(runtime, now=now)

    deleted = {entry["name"] for entry in plan["delete"]}
    assert partial_name not in deleted


def test_build_retention_plan_rejects_min_age_below_floor(tmp_path: Path) -> None:
    runtime, *_ = _build_runtime(tmp_path)

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.build_retention_plan(runtime, partial_min_age_s=1)


def test_build_retention_plan_rejects_negative_keep_count(tmp_path: Path) -> None:
    runtime, *_ = _build_runtime(tmp_path)

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.build_retention_plan(runtime, keep_unreferenced_complete=-1)


def test_build_retention_plan_rejects_unknown_release_entry(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    (releases / "not-a-recognized-name").mkdir()

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.build_retention_plan(runtime)


def test_build_retention_plan_on_missing_releases_dir_raises(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-empty"
    runtime.mkdir()

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.build_retention_plan(runtime)


def test_build_retention_plan_on_nonexistent_runtime_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        console_release_retention.build_retention_plan(tmp_path / "does-not-exist")


def test_assert_prepare_capacity_passes_under_the_complete_bound(tmp_path: Path) -> None:
    runtime, *_ = _build_runtime(tmp_path)

    console_release_retention.assert_prepare_capacity(runtime)  # no raise


def test_assert_prepare_capacity_raises_when_a_partial_is_present(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    (releases / (("f" * 40) + ".partial")).mkdir()

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.assert_prepare_capacity(runtime)


def test_assert_prepare_capacity_raises_over_complete_bound(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    for i in range(console_release_retention.MAX_COMPLETE_BEFORE_PREPARE + 1):
        (releases / (chr(ord("g") + i) * 40)).mkdir()

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.assert_prepare_capacity(runtime)


def test_assert_prepare_capacity_on_missing_releases_dir_is_a_noop(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-empty"
    runtime.mkdir()

    console_release_retention.assert_prepare_capacity(runtime)  # no raise: nothing to police yet


def test_apply_retention_plan_removes_only_planned_entries(tmp_path: Path) -> None:
    runtime, releases, current_name, previous_name = _build_runtime(tmp_path)
    stale_name = "c" * 40
    (releases / stale_name).mkdir()
    now = time.time()

    plan = console_release_retention.build_retention_plan(
        runtime, keep_unreferenced_complete=0, now=now
    )
    assert {entry["name"] for entry in plan["delete"]} == {stale_name}

    receipt = console_release_retention.apply_retention_plan(
        plan, confirm_plan_sha256=plan["plan_sha256"]
    )

    assert receipt["removed_count"] == 1
    assert not (releases / stale_name).exists()
    assert (releases / current_name).exists()
    assert (releases / previous_name).exists()


def test_apply_retention_plan_rejects_hash_mismatch(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    stale_name = "c" * 40
    (releases / stale_name).mkdir()
    plan = console_release_retention.build_retention_plan(runtime, keep_unreferenced_complete=0)

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.apply_retention_plan(plan, confirm_plan_sha256="0" * 64)


def test_apply_retention_plan_rejects_tampered_plan_body(tmp_path: Path) -> None:
    runtime, releases, *_ = _build_runtime(tmp_path)
    stale_name = "c" * 40
    (releases / stale_name).mkdir()
    plan = console_release_retention.build_retention_plan(runtime, keep_unreferenced_complete=0)
    confirm = plan["plan_sha256"]
    plan["delete"] = []  # tamper after the hash was computed

    with pytest.raises(console_release_retention.RetentionError):
        console_release_retention.apply_retention_plan(plan, confirm_plan_sha256=confirm)
