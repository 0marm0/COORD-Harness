from __future__ import annotations

import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess

import pytest

from coordharness.usage.replica import (
    CONFIG_SCHEMA,
    ReplicaError,
    VolumeIdentity,
    restore_drill,
    seed_replica,
    verify_generation,
)
from coordharness.usage import replica as replica_module


def _identity(volume: Path, *, uuid: str = "fixture-volume") -> VolumeIdentity:
    return VolumeIdentity(
        mount_point=str(volume.resolve()),
        volume_uuid=uuid,
        device_identifier="disk-fixture",
        container_reference="container-fixture",
        physical_stores=("disk-fixture-store",),
        encryption_scope="volume",
        backing_image=None,
        backing_volume_uuid=None,
        image_container_reference=None,
        image_physical_stores=(),
        filesystem="apfs",
        volume_name="Fixture",
        total_bytes=20 * 1024**3,
        free_bytes=15 * 1024**3,
        encrypted=True,
        internal=False,
        read_only=False,
    )


def _config(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"schema": CONFIG_SCHEMA, "items": rows}), encoding="utf-8")
    return path


def test_seed_verify_and_restore_are_lossless_and_source_read_only(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("preserve me\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "memory.md").write_text("selective context\n", encoding="utf-8")
    database = tmp_path / "ledger.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("create table facts(id integer primary key, value text not null)")
    connection.execute("insert into facts(value) values ('usage')")
    connection.commit()
    connection.close()
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (source, tree / "memory.md", database)
    }
    config = _config(
        tmp_path / "config.json",
        [
            {"label": "ledger", "path": str(database), "kind": "sqlite"},
            {"label": "catalog", "path": str(source), "kind": "file"},
            {"label": "memory", "path": str(tree), "kind": "tree"},
        ],
    )

    seeded = seed_replica(volume=volume, config=config, identity=_identity(volume))
    generation = Path(seeded["generation"])
    verified = verify_generation(generation)
    restored = restore_drill(generation)

    assert seeded["status"] == "REPLICA_VERIFIED"
    assert seeded["safety"] == {
        "source_mutation": False,
        "atomic_publication": True,
        "overwrite": False,
        "packing_authorized": False,
        "quarantine_authorized": False,
        "deletion_authorized": False,
    }
    assert verified["status"] == "PASS"
    assert verified["entry_count"] == 3
    assert verified["sqlite_integrity_checks"] == 1
    assert restored["status"] == "RESTORE_DRILL_PASS"
    assert restored["isolated_temporary_restore_removed"] is True
    assert not list((volume / "Usage-Archive" / "v1").glob(".staging-*"))
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (source, tree / "memory.md", database)
    }


def test_archive_identity_conflict_fails_without_overwrite(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("one", encoding="utf-8")
    config = _config(
        tmp_path / "config.json",
        [{"label": "source", "path": str(source), "kind": "file"}],
    )
    first = seed_replica(volume=volume, config=config, identity=_identity(volume, uuid="one"))

    with pytest.raises(ReplicaError, match="different volume UUID"):
        seed_replica(volume=volume, config=config, identity=_identity(volume, uuid="two"))

    assert verify_generation(Path(first["generation"]))["status"] == "PASS"


def test_tree_symlink_fails_before_generation_publication(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    tree = tmp_path / "tree"
    tree.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (tree / "escape").symlink_to(target)
    config = _config(
        tmp_path / "config.json",
        [{"label": "tree", "path": str(tree), "kind": "tree"}],
    )

    with pytest.raises(ReplicaError, match="non-regular file"):
        seed_replica(volume=volume, config=config, identity=_identity(volume))

    assert not (volume / "Usage-Archive").exists()


def test_config_rejects_relative_paths_and_duplicate_labels(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    relative = _config(
        tmp_path / "relative.json",
        [{"label": "bad", "path": "relative", "kind": "file"}],
    )
    with pytest.raises(ReplicaError, match="path must be absolute"):
        seed_replica(volume=volume, config=relative, identity=_identity(volume))

    source = tmp_path / "source"
    source.write_text("x", encoding="utf-8")
    duplicate = _config(
        tmp_path / "duplicate.json",
        [
            {"label": "same", "path": str(source), "kind": "file"},
            {"label": "same", "path": str(source), "kind": "file"},
        ],
    )
    with pytest.raises(ReplicaError, match="duplicate replica label"):
        seed_replica(volume=volume, config=duplicate, identity=_identity(volume))


def test_encrypted_sparsebundle_resolves_to_real_backing_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volumes = tmp_path / "Volumes"
    backing = volumes / "Backup SSD"
    mount = volumes / "UsageBackupV2"
    image = backing / "ClaudeUsageVault-v2.sparsebundle"
    image.mkdir(parents=True)
    mount.mkdir()
    payload = plistlib.dumps(
        {
            "images": [
                {
                    "image-encrypted": True,
                    "image-path": str(image),
                    "system-entities": [{"mount-point": str(mount)}],
                }
            ]
        }
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, payload, b"")

    monkeypatch.setattr(replica_module.subprocess, "run", fake_run)

    assert replica_module._encrypted_image_for_mount(
        mount, volumes_root=volumes
    ) == (image.resolve(), backing.resolve())
