
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from coordharness.testing import apfs_cow_cleanroom as cow

# All fixture roots below come from pytest's `tmp_path`, which on this platform is
# already realpath-resolved (verified: `/private/var/folders/...`, not the symlinked
# `/var/folders/...`). The module's no-follow directory-descent refuses to open a
# path through a symlinked component -- using an unresolved tmp root would trip
# REFUSED_SOURCE_TOPOLOGY on the harness's own physical-root validation, not on
# anything under test.


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_repo(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "coord-harness-test@example.invalid")
    _git(root, "config", "user.name", "coord-harness-test")
    (root / "f.txt").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


# --- sha256_file / stable_manifest --------------------------------------


def test_sha256_file_matches_hashlib_reference(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    payload = bytes(range(256)) * 4096  # exceed the 1MiB chunk size
    target.write_bytes(payload)
    assert cow.sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_stable_manifest_is_order_independent_and_self_hashed() -> None:
    first = cow.TopologyEntry(
        store_id="s", path="z.txt", kind="regular", mode=0o644, size=1, source_sha256="ab"
    )
    second = cow.TopologyEntry(
        store_id="s", path="a.txt", kind="regular", mode=0o644, size=1, source_sha256="cd"
    )
    forward = cow.stable_manifest([first, second])
    reversed_input = cow.stable_manifest([second, first])
    assert forward["sha256"] == reversed_input["sha256"]
    assert [row["path"] for row in forward["entries"]] == ["a.txt", "z.txt"]
    assert forward["sha256"] == hashlib.sha256(
        cow._canonical_json_bytes(
            {k: v for k, v in forward.items() if k != "sha256"}
        )
    ).hexdigest()


# --- longest_prefix_mapping / rewritten_symlink_target ------------------


def test_longest_prefix_mapping_prefers_the_deepest_match(tmp_path: Path) -> None:
    mappings = {tmp_path / "a": tmp_path / "A", tmp_path / "a" / "b": tmp_path / "A" / "B"}
    source, destination, relative = cow.longest_prefix_mapping(
        tmp_path / "a" / "b" / "c.txt", mappings
    )
    assert source == tmp_path / "a" / "b"
    assert destination == tmp_path / "A" / "B"
    assert relative == Path("c.txt")


def test_longest_prefix_mapping_returns_none_outside_every_root(tmp_path: Path) -> None:
    mappings = {tmp_path / "a": tmp_path / "A"}
    assert cow.longest_prefix_mapping(tmp_path / "elsewhere", mappings) is None


def test_rewritten_symlink_target_mapped_store(tmp_path: Path) -> None:
    src_store = tmp_path / "src_store"
    dst_store = tmp_path / "dst_store"
    mappings = {src_store: dst_store}
    source_link = src_store / "sub" / "l"
    destination_link = dst_store / "sub" / "l"
    target, namespace, rewritten = cow.rewritten_symlink_target(
        source_link=source_link,
        destination_link=destination_link,
        raw_target=str(src_store / "sub" / "target.txt"),
        mappings=mappings,
    )
    assert target == src_store / "sub" / "target.txt"
    assert namespace == "mapped_store"
    assert rewritten == "target.txt"


def test_rewritten_symlink_target_external_allowlist(tmp_path: Path) -> None:
    src_store = tmp_path / "src_store"
    dst_store = tmp_path / "dst_store"
    outside = tmp_path / "outside"
    _target, namespace, rewritten = cow.rewritten_symlink_target(
        source_link=src_store / "l",
        destination_link=dst_store / "l",
        raw_target=str(outside / "readonly.txt"),
        mappings={src_store: dst_store},
        external_allowlist=[outside],
    )
    assert namespace == "external_readonly"
    assert rewritten == str(outside / "readonly.txt")


def test_rewritten_symlink_target_refuses_unmapped_unallowlisted_target(
    tmp_path: Path,
) -> None:
    src_store = tmp_path / "src_store"
    dst_store = tmp_path / "dst_store"
    outside = tmp_path / "outside"
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.rewritten_symlink_target(
            source_link=src_store / "l",
            destination_link=dst_store / "l",
            raw_target=str(outside / "nope.txt"),
            mappings={src_store: dst_store},
        )
    assert excinfo.value.state == "REFUSED_EXTERNAL_LINK"


def test_rewritten_symlink_target_refuses_relative_parent_traversal(tmp_path: Path) -> None:
    src_store = tmp_path / "src_store"
    dst_store = tmp_path / "dst_store"
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.rewritten_symlink_target(
            source_link=src_store / "l",
            destination_link=dst_store / "l",
            raw_target="../escape.txt",
            mappings={src_store: dst_store},
        )
    assert excinfo.value.state == "REFUSED_SOURCE_TOPOLOGY"


# --- inventory_topology ---------------------------------------------------


def _build_source_store(tmp_path: Path) -> Path:
    store = tmp_path / "src_store"
    (store / "sub").mkdir(parents=True)
    (store / "sub" / "file.txt").write_bytes(b"payload-data")
    os.link(store / "sub" / "file.txt", store / "sub" / "file2.txt")
    os.symlink("sub/file.txt", store / "link_to_file")
    return store


def test_inventory_topology_walks_a_real_store_and_is_deterministic(tmp_path: Path) -> None:
    store = _build_source_store(tmp_path)
    first = cow.inventory_topology({"store": store}, hash_regular_files=True)
    assert first["state"] == "TOPOLOGY_PASS"
    assert first["entry_count"] == 4  # sub/, sub/file.txt, sub/file2.txt, link_to_file
    assert first["symlink_count"] == 1
    assert first["regular_count"] == 2
    second = cow.inventory_topology({"store": store}, hash_regular_files=True)
    assert first["manifest"]["sha256"] == second["manifest"]["sha256"]


def test_inventory_topology_refuses_special_files(tmp_path: Path) -> None:
    store = tmp_path / "special_store"
    store.mkdir()
    os.mkfifo(store / "afifo")
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.inventory_topology({"store": store})
    assert excinfo.value.state == "REFUSED_SPECIAL_FILE"


def test_inventory_topology_refuses_symlink_escaping_every_root(tmp_path: Path) -> None:
    store = tmp_path / "ext_store"
    store.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    os.symlink(str(outside / "secret.txt"), store / "escape_link")
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.inventory_topology({"store": store})
    assert excinfo.value.state == "REFUSED_EXTERNAL_LINK"
    # allowlisting the escape target admits the same tree
    allowed = cow.inventory_topology({"store": store}, external_allowlist=[outside])
    assert allowed["state"] == "TOPOLOGY_PASS"


def test_inventory_topology_refuses_duplicate_and_missing_roots(tmp_path: Path) -> None:
    store = tmp_path / "dup_store"
    store.mkdir()
    with pytest.raises(cow.CleanroomRefusal) as dup:
        cow.inventory_topology({"a": store, "b": store})
    assert dup.value.state == "REFUSED_SOURCE_TOPOLOGY"
    with pytest.raises(cow.CleanroomRefusal) as empty:
        cow.inventory_topology({})
    assert empty.value.state == "REFUSED_SOURCE_TOPOLOGY"


# --- clone_regular_file_cow / clone_or_link_regular_file -----------------


def test_clone_regular_file_cow_real_clone_is_independent_and_byte_identical(
    tmp_path: Path,
) -> None:
    # MEASURED on this filesystem: fclonefileat succeeds (macOS/APFS tmp dir), so
    # this exercises a real Copy-on-Write clone, not a fallback. Verified below by
    # asserting the destination inode differs from the source (not a hardlink)
    # while the bytes are identical.
    source = tmp_path / "source.bin"
    payload = b"cow-data" * 4096
    source.write_bytes(payload)
    destination_dir = tmp_path / "dest"
    destination_dir.mkdir()
    source_fd = os.open(source, os.O_RDONLY)
    destination_dir_fd = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        cow.clone_regular_file_cow(source_fd, destination_dir_fd, "cloned.bin")
    finally:
        os.close(source_fd)
        os.close(destination_dir_fd)
    cloned = destination_dir / "cloned.bin"
    assert cloned.read_bytes() == payload
    assert os.stat(cloned).st_ino != os.stat(source).st_ino


def test_clone_regular_file_cow_refuses_unsafe_destination_names(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"x")
    destination_dir = tmp_path / "dest"
    destination_dir.mkdir()
    source_fd = os.open(source, os.O_RDONLY)
    destination_dir_fd = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(cow.CleanroomRefusal) as excinfo:
            cow.clone_regular_file_cow(source_fd, destination_dir_fd, "nested/escape.bin")
        assert excinfo.value.state == "REFUSED_COW"
    finally:
        os.close(source_fd)
        os.close(destination_dir_fd)


def test_clone_regular_file_cow_refuses_non_regular_source(tmp_path: Path) -> None:
    a_directory = tmp_path / "adir"
    a_directory.mkdir()
    destination_dir = tmp_path / "dest"
    destination_dir.mkdir()
    source_fd = os.open(a_directory, os.O_RDONLY | os.O_DIRECTORY)
    destination_dir_fd = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(cow.CleanroomRefusal) as excinfo:
            cow.clone_regular_file_cow(source_fd, destination_dir_fd, "x.bin")
        assert excinfo.value.state == "REFUSED_COW"
    finally:
        os.close(source_fd)
        os.close(destination_dir_fd)


def test_clone_regular_file_cow_has_no_silent_byte_copy_fallback(tmp_path: Path) -> None:
    # The module's own failure message says the quiet part out loud: "byte-copy
    # fallback is forbidden". This asserts that promise -- a filesystem/clone_call
    # that cannot clone makes the whole operation REFUSE, it does not silently
    # degrade to a plain copy. (Not a bug: this is the documented contract.)
    source = tmp_path / "source.bin"
    source.write_bytes(b"x")
    destination_dir = tmp_path / "dest"
    destination_dir.mkdir()
    source_fd = os.open(source, os.O_RDONLY)
    destination_dir_fd = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY)

    def _unsupported_clone(*_args: object) -> None:
        raise OSError(45, "Operation not supported")  # ENOTSUP-shaped failure

    try:
        with pytest.raises(cow.CleanroomRefusal) as excinfo:
            cow.clone_regular_file_cow(
                source_fd, destination_dir_fd, "never.bin", clone_call=_unsupported_clone
            )
        assert excinfo.value.state == "REFUSED_COW"
        assert "byte-copy fallback is forbidden" in excinfo.value.detail
        assert not (destination_dir / "never.bin").exists()
    finally:
        os.close(source_fd)
        os.close(destination_dir_fd)


def test_clone_or_link_regular_file_dedupes_hardlinks_via_real_clone(tmp_path: Path) -> None:
    source_dir = tmp_path / "cl_src"
    source_dir.mkdir()
    first_alias = source_dir / "f1.txt"
    first_alias.write_bytes(b"hi" * 10)
    os.link(first_alias, source_dir / "f2.txt")
    destination_dir = tmp_path / "cl_dst"
    destination_dir.mkdir()
    destination_root_fd = os.open(destination_dir, os.O_RDONLY | os.O_DIRECTORY)
    hardlinks: dict[tuple[int, int], str] = {}
    try:
        for name in ("f1.txt", "f2.txt"):
            source_fd = os.open(source_dir / name, os.O_RDONLY)
            source_stat = os.fstat(source_fd)
            method = cow.clone_or_link_regular_file(
                source_fd=source_fd,
                source_stat=source_stat,
                destination_root_fd=destination_root_fd,
                destination_directory_fd=destination_root_fd,
                destination_relative=Path(name),
                hardlinks=hardlinks,
            )
            os.close(source_fd)
            if name == "f1.txt":
                assert method == "fclonefileat"
            else:
                assert method == "hardlink"
    finally:
        os.close(destination_root_fd)
    assert (
        os.stat(destination_dir / "f1.txt").st_ino
        == os.stat(destination_dir / "f2.txt").st_ino
    )


# --- materialize_stores_cow ------------------------------------------------


def test_materialize_stores_cow_reproduces_topology_and_content(tmp_path: Path) -> None:
    source = _build_source_store(tmp_path)
    destination = tmp_path / "dst_store"
    result = cow.materialize_stores_cow({"store": source}, {"store": destination})
    assert result["state"] == "FIXTURE_PASS"
    assert result["content_hashes_equal"] is True
    assert result["clone_calls"] >= 1
    assert result["hardlink_calls"] >= 1
    assert result["symlink_calls"] == 1
    assert (destination / "sub" / "file.txt").read_bytes() == b"payload-data"
    assert os.readlink(destination / "link_to_file") == "sub/file.txt"
    assert (
        os.stat(destination / "sub" / "file.txt").st_ino
        != os.stat(source / "sub" / "file.txt").st_ino
    )
    assert (
        os.stat(destination / "sub" / "file.txt").st_ino
        == os.stat(destination / "sub" / "file2.txt").st_ino
    )


def test_materialize_stores_cow_refuses_when_destination_already_exists(
    tmp_path: Path,
) -> None:
    source = _build_source_store(tmp_path)
    destination = tmp_path / "dst_store"
    cow.materialize_stores_cow({"store": source}, {"store": destination})
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.materialize_stores_cow({"store": source}, {"store": destination})
    assert excinfo.value.state == "REFUSED_COW"


def test_materialize_stores_cow_refuses_destination_nested_in_source(
    tmp_path: Path,
) -> None:
    source = _build_source_store(tmp_path)
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.materialize_stores_cow({"store": source}, {"store": source / "nested_dest"})
    assert excinfo.value.state == "REFUSED_SOURCE_TOPOLOGY"


# --- backup_sqlite_online ---------------------------------------------------


def _make_sqlite_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (42)")
    connection.commit()
    connection.close()


def test_backup_sqlite_online_round_trips_data_and_passes_quick_check(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src.db"
    _make_sqlite_db(source)
    destination = tmp_path / "backup.db"
    result = cow.backup_sqlite_online(source, destination)
    assert result["quick_check"] == "ok"
    assert result["page_count"] > 0
    assert result["destination_sha256"] == cow.sha256_file(destination)
    replayed = sqlite3.connect(destination)
    assert replayed.execute("SELECT x FROM t").fetchone() == (42,)
    replayed.close()


def test_backup_sqlite_online_refuses_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "src.db"
    _make_sqlite_db(source)
    destination = tmp_path / "backup.db"
    cow.backup_sqlite_online(source, destination)
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.backup_sqlite_online(source, destination)
    assert excinfo.value.state == "REFUSED_SQLITE_BACKUP"


def test_backup_sqlite_online_refuses_non_regular_source(tmp_path: Path) -> None:
    a_directory = tmp_path / "adir"
    a_directory.mkdir()
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.backup_sqlite_online(a_directory, tmp_path / "backup.db")
    assert excinfo.value.state == "REFUSED_SQLITE_BACKUP"


def test_backup_sqlite_online_refuses_symlinked_source(tmp_path: Path) -> None:
    source = tmp_path / "src.db"
    _make_sqlite_db(source)
    link = tmp_path / "src_link.db"
    os.symlink(source, link)
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.backup_sqlite_online(link, tmp_path / "backup.db")
    assert excinfo.value.state == "REFUSED_SQLITE_BACKUP"


# --- validate_physical_git_checkout -----------------------------------------


def test_validate_physical_git_checkout_accepts_clean_matching_checkout(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "gitrepo"
    oid = _init_repo(repo)
    result = cow.validate_physical_git_checkout(
        repo, candidate_ref="refs/heads/main", expected_oid=oid
    )
    assert result["worktree_clean"] is True
    assert result["candidate_oid"] == oid


def test_validate_physical_git_checkout_refuses_unqualified_ref(tmp_path: Path) -> None:
    repo = tmp_path / "gitrepo"
    oid = _init_repo(repo)
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_physical_git_checkout(repo, candidate_ref="main", expected_oid=oid)
    assert excinfo.value.state == "REFUSED_CANDIDATE_REF"


def test_validate_physical_git_checkout_refuses_malformed_oid(tmp_path: Path) -> None:
    repo = tmp_path / "gitrepo"
    _init_repo(repo)
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_physical_git_checkout(
            repo, candidate_ref="refs/heads/main", expected_oid="deadbeef"
        )
    assert excinfo.value.state == "REFUSED_CANDIDATE_REF"


def test_validate_physical_git_checkout_refuses_dirty_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "gitrepo"
    oid = _init_repo(repo)
    (repo / "untracked.txt").write_text("dirty")
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_physical_git_checkout(
            repo, candidate_ref="refs/heads/main", expected_oid=oid
        )
    assert excinfo.value.state == "REFUSED_GIT_CHECKOUT"


def test_validate_physical_git_checkout_refuses_missing_git_dir(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_physical_git_checkout(
            not_a_repo, candidate_ref="refs/heads/main", expected_oid="a" * 40
        )
    assert excinfo.value.state == "REFUSED_GIT_CHECKOUT"


# --- render/write_venv_python_shim ------------------------------------------


def test_render_venv_python_shim_embeds_python_path_and_pins_pytest_env(
    tmp_path: Path,
) -> None:
    text = cow.render_venv_python_shim(
        shared_python=tmp_path / "shared" / "python3",
        clone_source=tmp_path / "clone_src",
        shared_site_packages=tmp_path / "site-packages",
    )
    assert text.startswith("#!/bin/sh\n")
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in text
    assert str(tmp_path / "clone_src") in text
    assert str(tmp_path / "site-packages") in text
    assert os.pathsep in text


def test_write_venv_python_shim_writes_executable_file_and_refuses_reuse(
    tmp_path: Path,
) -> None:
    shim_path = tmp_path / "shim" / "python"
    digest = cow.write_venv_python_shim(
        shim_path,
        shared_python=tmp_path / "shared" / "python3",
        clone_source=tmp_path / "clone_src",
        shared_site_packages=tmp_path / "site-packages",
    )
    assert digest == hashlib.sha256(shim_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert stat.S_IMODE(shim_path.stat().st_mode) == 0o755
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.write_venv_python_shim(
            shim_path,
            shared_python=tmp_path / "shared" / "python3",
            clone_source=tmp_path / "clone_src",
            shared_site_packages=tmp_path / "site-packages",
        )
    assert excinfo.value.state == "REFUSED_VENV_LEAK"


# --- validate_venv_probe_payload --------------------------------------------


def _clone_and_canonical(tmp_path: Path) -> tuple[Path, Path, Path]:
    clone_root = tmp_path / "clone_root"
    canonical_root = tmp_path / "canonical_root"
    (clone_root / "coordharness").mkdir(parents=True)
    canonical_root.mkdir()
    module_file = clone_root / "coordharness" / "__init__.py"
    module_file.write_text("# clone-local\n")
    return clone_root, canonical_root, module_file


def test_validate_venv_probe_payload_accepts_a_clone_local_probe(tmp_path: Path) -> None:
    clone_root, canonical_root, module_file = _clone_and_canonical(tmp_path)
    payload = {
        "coord_file": str(module_file),
        "sys_path": [str(clone_root)],
        "pytest_disable_plugin_autoload": "1",
        "loaded_modules": {"coordharness": str(module_file)},
    }
    result = cow.validate_venv_probe_payload(
        payload, clone_root=clone_root, canonical_root=canonical_root
    )
    assert result["clone_local_coordharness"] == str(module_file.resolve())
    assert result["canonical_sys_path_entries"] == []


def test_validate_venv_probe_payload_refuses_coord_file_outside_clone(
    tmp_path: Path,
) -> None:
    clone_root, canonical_root, module_file = _clone_and_canonical(tmp_path)
    leaked = canonical_root / "leak.py"
    leaked.write_text("# leak\n")
    payload = {
        "coord_file": str(leaked),
        "sys_path": [str(clone_root)],
        "pytest_disable_plugin_autoload": "1",
        "loaded_modules": {"coordharness": str(module_file)},
    }
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_venv_probe_payload(
            payload, clone_root=clone_root, canonical_root=canonical_root
        )
    assert excinfo.value.state == "REFUSED_VENV_LEAK"
    assert "not clone-local" in excinfo.value.detail


def test_validate_venv_probe_payload_refuses_canonical_leak_through_sys_path(
    tmp_path: Path,
) -> None:
    clone_root, canonical_root, module_file = _clone_and_canonical(tmp_path)
    payload = {
        "coord_file": str(module_file),
        "sys_path": [str(clone_root), str(canonical_root)],
        "pytest_disable_plugin_autoload": "1",
        "loaded_modules": {"coordharness": str(module_file)},
    }
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_venv_probe_payload(
            payload, clone_root=clone_root, canonical_root=canonical_root
        )
    assert excinfo.value.state == "REFUSED_VENV_LEAK"
    assert "leaked through sys.path" in excinfo.value.detail


# --- validate_sandbox_roots / sandbox_profile_text --------------------------


def test_validate_sandbox_roots_accepts_physically_disjoint_roots(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    cleanroom = tmp_path / "cleanroom"
    canonical.mkdir()
    cleanroom.mkdir()
    result = cow.validate_sandbox_roots(canonical_root=canonical, cleanroom_root=cleanroom)
    assert result["physically_disjoint"] is True


def test_validate_sandbox_roots_refuses_identical_roots(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.validate_sandbox_roots(canonical_root=canonical, cleanroom_root=canonical)
    assert excinfo.value.state == "REFUSED_SANDBOX_RED_ARM"


def test_sandbox_profile_text_is_deterministic_and_denies_by_default() -> None:
    first = cow.sandbox_profile_text()
    second = cow.sandbox_profile_text()
    assert first == second
    assert "(deny default)" in first
    assert "(deny network*)" in first
    assert "CLEANROOM_ROOT" in first
    assert "CANONICAL_ROOT" in first


# --- red_arm_probe -----------------------------------------------------------


def test_red_arm_probe_confirms_protected_write_denied_and_cleanroom_write_allowed(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    protected = canonical / "protected.txt"
    protected.write_bytes(b"secret")
    writable = tmp_path / "cleanroom"
    writable.mkdir()
    # UF_IMMUTABLE makes even the owner's O_WRONLY fail with EPERM (MEASURED on
    # this filesystem), which is the specific errno the probe requires -- a plain
    # chmod 0o444 denial surfaces as EACCES here, not EPERM, and would not satisfy
    # the probe's contract.
    os.chflags(protected, stat.UF_IMMUTABLE)
    try:
        result = cow.red_arm_probe([protected], writable, canonical_root=canonical)
    finally:
        os.chflags(protected, 0)
    assert result["clone_local_write"] == "PASS"
    assert result["protected_writes"][0]["errno_name"] == "EPERM"
    assert (writable / ".sandbox-write-probe").read_bytes() == b"clone-local\n"


def test_red_arm_probe_refuses_when_protected_write_unexpectedly_succeeds(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    not_actually_protected = canonical / "writable.txt"
    not_actually_protected.write_bytes(b"oops")
    writable = tmp_path / "cleanroom"
    writable.mkdir()
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.red_arm_probe([not_actually_protected], writable, canonical_root=canonical)
    assert excinfo.value.state == "REFUSED_SANDBOX_RED_ARM"
    assert "unexpectedly succeeded" in excinfo.value.detail


def test_red_arm_probe_refuses_a_protected_file_outside_canonical_root(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    writable = tmp_path / "cleanroom"
    writable.mkdir()
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.red_arm_probe([outside], writable, canonical_root=canonical)
    assert excinfo.value.state == "REFUSED_SANDBOX_RED_ARM"
    assert "outside CANONICAL_ROOT" in excinfo.value.detail


# --- zero_mutation_counters / create_one_level_physical_link ---------------


def test_zero_mutation_counters_starts_every_counter_at_zero() -> None:
    counters = cow.zero_mutation_counters()
    assert counters == {
        "clone_calls": 0,
        "checkout_calls": 0,
        "sqlite_backup_calls": 0,
        "sandbox_launch_calls": 0,
        "pytest_launch_calls": 0,
    }


def test_create_one_level_physical_link_produces_a_single_hop_symlink(
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / "phys_target"
    target_dir.mkdir()
    link = tmp_path / "links" / "l1"
    result = cow.create_one_level_physical_link(link, target_dir)
    assert result["one_hop"] is True
    assert os.readlink(link) == str(target_dir)
    assert result["access_posture"] == "PRELAUNCH_MEASUREMENT_ONLY_NO_CHILD_EXECUTION"


def test_create_one_level_physical_link_refuses_reuse(tmp_path: Path) -> None:
    target_dir = tmp_path / "phys_target"
    target_dir.mkdir()
    link = tmp_path / "links" / "l1"
    cow.create_one_level_physical_link(link, target_dir)
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.create_one_level_physical_link(link, target_dir)
    assert excinfo.value.state == "REFUSED_ATTEMPT_REUSE"


def test_create_one_level_physical_link_refuses_non_directory_target(
    tmp_path: Path,
) -> None:
    not_a_dir = tmp_path / "plain.txt"
    not_a_dir.write_text("x")
    with pytest.raises(cow.CleanroomRefusal) as excinfo:
        cow.create_one_level_physical_link(tmp_path / "links" / "l2", not_a_dir)
    assert excinfo.value.state == "REFUSED_SOURCE_TOPOLOGY"


# --- CleanroomRefusal / DEFAULT_STORE_SPECS ---------------------------------


def test_cleanroom_refusal_receipt_reports_errno_name() -> None:
    error = cow.CleanroomRefusal(
        "REFUSED_COW", "detail text", path="/tmp/x", error_number=1
    )
    receipt = error.receipt()
    assert receipt["state"] == "REFUSED_COW"
    assert receipt["errno"] == 1
    assert receipt["errno_name"] == "EPERM"


def test_cleanroom_refusal_rejects_unknown_terminal_states() -> None:
    with pytest.raises(ValueError):
        cow.CleanroomRefusal("NOT_A_REAL_STATE", "detail")


def test_default_store_specs_cover_the_three_named_stores() -> None:
    assert len(cow.DEFAULT_STORE_SPECS) == 3
    ids = {spec.store_id for spec in cow.DEFAULT_STORE_SPECS}
    assert ids == {"project_data", "models", "repositories"}
    assert all(
        spec.relative_path.startswith(".coordharness/stores/")
        for spec in cow.DEFAULT_STORE_SPECS
    )
