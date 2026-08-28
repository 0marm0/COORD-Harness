
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Iterable, Literal
from urllib.parse import quote
import uuid


ARCHIVE_SCHEMA = "coordharness.usage-storage.archive.v1"
CONFIG_SCHEMA = "coordharness.usage-storage.replica-config.v1"
GENERATION_SCHEMA = "coordharness.usage-storage.replica-generation.v1"
VERIFY_SCHEMA = "coordharness.usage-storage.replica-verification.v1"
RESTORE_SCHEMA = "coordharness.usage-storage.restore-drill.v1"
ARCHIVE_DIRECTORY = "Usage-Archive"
_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
Kind = Literal["file", "sqlite", "tree"]


class ReplicaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReplicaItem:
    label: str
    path: Path
    kind: Kind
    required: bool = True


@dataclass(frozen=True, slots=True)
class VolumeIdentity:
    mount_point: str
    volume_uuid: str
    device_identifier: str
    container_reference: str
    physical_stores: tuple[str, ...]
    encryption_scope: str
    backing_image: str | None
    backing_volume_uuid: str | None
    image_container_reference: str | None
    image_physical_stores: tuple[str, ...]
    filesystem: str
    volume_name: str
    total_bytes: int
    free_bytes: int
    encrypted: bool
    internal: bool
    read_only: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: Path) -> list[ReplicaItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise ReplicaError(f"replica config schema must be {CONFIG_SCHEMA!r}")
    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise ReplicaError("replica config requires a nonempty items list")
    result: list[ReplicaItem] = []
    labels: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReplicaError(f"replica config item {index} must be an object")
        unexpected = sorted(set(row) - {"label", "path", "kind", "required"})
        if unexpected:
            raise ReplicaError(f"replica config item {index} has unknown fields: {unexpected}")
        label = row.get("label")
        kind = row.get("kind")
        source = row.get("path")
        required = row.get("required", True)
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise ReplicaError(f"replica config item {index} has unsafe label")
        if label in labels:
            raise ReplicaError(f"duplicate replica label: {label}")
        if kind not in {"file", "sqlite", "tree"}:
            raise ReplicaError(f"replica config item {index} has unsupported kind: {kind!r}")
        if not isinstance(source, str) or not Path(source).expanduser().is_absolute():
            raise ReplicaError(f"replica config item {index} path must be absolute")
        if type(required) is not bool:
            raise ReplicaError(f"replica config item {index} required must be boolean")
        labels.add(label)
        result.append(ReplicaItem(label, Path(source).expanduser(), kind, required))
    return result


def inspect_external_volume(volume: Path, *, minimum_free_bytes: int = 0) -> VolumeIdentity:

    requested = volume.expanduser()
    if not requested.is_absolute():
        raise ReplicaError("volume path must be absolute")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ReplicaError(f"volume is not mounted: {requested}: {exc}") from exc
    if resolved.parent != Path("/Volumes"):
        raise ReplicaError(f"production replica volume must be a direct child of /Volumes: {resolved}")
    info = _diskutil_info(resolved)
    mount_point = str(info.get("MountPoint") or "")
    try:
        mount_resolved = Path(mount_point).resolve(strict=True)
    except OSError as exc:
        raise ReplicaError("diskutil mount point is unavailable") from exc
    if mount_resolved != resolved:
        raise ReplicaError(f"mount identity mismatch: requested={resolved} observed={mount_resolved}")
    internal = info.get("Internal")
    if internal is not False:
        raise ReplicaError("replica destination is not proven external")
    read_only = bool(info.get("ReadOnlyVolume", False)) or info.get("Writable") is False
    if read_only or not os.access(resolved, os.W_OK):
        raise ReplicaError("replica destination is not proven writable")
    encrypted = _encrypted_from_diskutil(info)
    volume_uuid = str(info.get("VolumeUUID") or info.get("APFSVolumeUUID") or "").strip()
    device = str(info.get("DeviceIdentifier") or "").strip()
    filesystem = str(
        info.get("FilesystemType")
        or info.get("FilesystemName")
        or info.get("FileSystemPersonality")
        or ""
    ).strip()
    if not volume_uuid or not device or not filesystem:
        raise ReplicaError("replica destination lacks UUID, device, or filesystem identity")
    image_container_reference = str(info.get("APFSContainerReference") or "").strip()
    image_physical_stores = _physical_stores(info)
    if filesystem.casefold() == "apfs" and (
        not image_container_reference or not image_physical_stores
    ):
        raise ReplicaError("APFS replica destination lacks container or physical-store identity")
    total = int(
        info.get("APFSContainerSize")
        or info.get("TotalSize")
        or info.get("VolumeTotalSpace")
        or 0
    )
    free = int(
        info.get("APFSContainerFree")
        or info.get("FreeSpace")
        or info.get("VolumeFreeSpace")
        or 0
    )
    encryption_scope = "volume"
    backing_image: str | None = None
    backing_volume_uuid: str | None = None
    container_reference = image_container_reference
    physical_stores = image_physical_stores
    if not encrypted:
        image_record = _encrypted_image_for_mount(resolved)
        if image_record is None:
            raise ReplicaError("replica destination is not proven encrypted")
        image_path, backing_mount = image_record
        backing_info = _diskutil_info(backing_mount)
        backing_observed = Path(str(backing_info.get("MountPoint") or "")).resolve(strict=True)
        if backing_observed != backing_mount:
            raise ReplicaError("encrypted image backing-volume mount identity mismatch")
        if backing_info.get("Internal") is not False:
            raise ReplicaError("encrypted image backing store is not proven external")
        if backing_info.get("Writable") is False or not os.access(backing_mount, os.W_OK):
            raise ReplicaError("encrypted image backing store is not proven writable")
        backing_volume_uuid = str(
            backing_info.get("VolumeUUID") or backing_info.get("APFSVolumeUUID") or ""
        ).strip()
        container_reference = str(backing_info.get("APFSContainerReference") or "").strip()
        physical_stores = _physical_stores(backing_info)
        if not backing_volume_uuid or not container_reference or not physical_stores:
            raise ReplicaError("encrypted image lacks strong backing-store identity")
        backing_free = int(
            backing_info.get("APFSContainerFree")
            or backing_info.get("FreeSpace")
            or backing_info.get("VolumeFreeSpace")
            or 0
        )
        free = min(free, backing_free)
        encrypted = True
        encryption_scope = "disk_image"
        backing_image = os.fspath(image_path)
    if total <= 0 or free < minimum_free_bytes:
        raise ReplicaError(
            f"replica destination lacks required free space: free={free} required={minimum_free_bytes}"
        )
    return VolumeIdentity(
        mount_point=os.fspath(resolved),
        volume_uuid=volume_uuid,
        device_identifier=device,
        container_reference=container_reference,
        physical_stores=physical_stores,
        encryption_scope=encryption_scope,
        backing_image=backing_image,
        backing_volume_uuid=backing_volume_uuid,
        image_container_reference=(
            image_container_reference if encryption_scope == "disk_image" else None
        ),
        image_physical_stores=(
            image_physical_stores if encryption_scope == "disk_image" else ()
        ),
        filesystem=filesystem,
        volume_name=str(info.get("VolumeName") or resolved.name),
        total_bytes=total,
        free_bytes=free,
        encrypted=True,
        internal=False,
        read_only=False,
    )


def _encrypted_from_diskutil(info: dict[str, Any]) -> bool:
    if info.get("Encrypted") is True or info.get("FileVault") is True:
        return True
    for key in ("Encryption", "EncryptionType", "APFSEncryption", "FileVaultState"):
        value = str(info.get(key) or "").casefold()
        if "encrypt" in value and not any(word in value for word in ("not encrypted", "unencrypted")):
            return True
    return False


def _diskutil_info(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["diskutil", "info", "-plist", os.fspath(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ReplicaError(f"diskutil identity probe failed: {detail}")
    try:
        info = plistlib.loads(completed.stdout)
    except Exception as exc:
        raise ReplicaError("diskutil returned an unreadable identity plist") from exc
    if not isinstance(info, dict):
        raise ReplicaError("diskutil identity payload is not a dictionary")
    return info


def _physical_stores(info: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(row.get("APFSPhysicalStore") or "").strip()
            for row in (info.get("APFSPhysicalStores") or [])
            if isinstance(row, dict) and str(row.get("APFSPhysicalStore") or "").strip()
        )
    )


def _encrypted_image_for_mount(
    mount: Path, *, volumes_root: Path = Path("/Volumes")
) -> tuple[Path, Path] | None:

    completed = subprocess.run(
        ["hdiutil", "info", "-plist"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = plistlib.loads(completed.stdout)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expected = mount.resolve(strict=True)
    volumes_root = volumes_root.resolve(strict=True)
    for image in payload.get("images", []):
        if not isinstance(image, dict) or image.get("image-encrypted") is not True:
            continue
        mounted_here = False
        for entity in image.get("system-entities", []):
            if not isinstance(entity, dict) or not entity.get("mount-point"):
                continue
            try:
                mounted_here = Path(str(entity["mount-point"])).resolve(strict=True) == expected
            except OSError:
                mounted_here = False
            if mounted_here:
                break
        if not mounted_here:
            continue
        raw_image = str(image.get("image-path") or "")
        try:
            image_path = Path(raw_image).resolve(strict=True)
            relative = image_path.relative_to(volumes_root)
        except (OSError, ValueError):
            raise ReplicaError("encrypted image backing path is not on a mounted /Volumes volume")
        if not relative.parts:
            raise ReplicaError("encrypted image backing path lacks a volume component")
        backing_mount = (volumes_root / relative.parts[0]).resolve(strict=True)
        if backing_mount.parent != volumes_root:
            raise ReplicaError("encrypted image backing mount escapes /Volumes")
        return image_path, backing_mount
    return None


def estimate_source_bytes(items: Iterable[ReplicaItem]) -> int:
    total = 0
    for item in items:
        if not item.path.exists():
            if item.required:
                raise ReplicaError(f"required source is missing: {item.path}")
            continue
        if item.path.is_symlink():
            raise ReplicaError(f"source symlinks are not accepted: {item.path}")
        if item.kind == "tree":
            for candidate in _tree_files(item.path):
                total += candidate.stat().st_size
        else:
            if not item.path.is_file():
                raise ReplicaError(f"source must be a regular file: {item.path}")
            total += item.path.stat().st_size
    return total


def seed_replica(
    *,
    volume: Path,
    config: Path,
    identity: VolumeIdentity | None = None,
) -> dict[str, Any]:

    items = load_config(config)
    estimated = estimate_source_bytes(items)
    required_free = max(1024**3, estimated * 3 + 256 * 1024**2)
    identity = identity or inspect_external_volume(volume, minimum_free_bytes=required_free)
    volume_resolved = Path(identity.mount_point).resolve(strict=True)
    if identity.free_bytes < required_free:
        raise ReplicaError(
            f"replica destination free space changed below reserve: {identity.free_bytes} < {required_free}"
        )
    archive_root = volume_resolved / ARCHIVE_DIRECTORY / "v1"
    _prepare_archive_root(archive_root, identity)
    generations = archive_root / "generations"
    generations.mkdir(mode=0o700, parents=True, exist_ok=True)
    generation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex
    staging = archive_root / f".staging-{generation_id}"
    published = generations / generation_id
    if staging.exists() or published.exists():
        raise ReplicaError("refusing to reuse a generation or staging path")
    staging.mkdir(mode=0o700)
    entries: list[dict[str, Any]] = []
    try:
        payload_root = staging / "payload"
        payload_root.mkdir(mode=0o700)
        for item in items:
            entries.extend(_copy_item(item, payload_root))
        entries.sort(key=lambda row: row["payload_path"])
        manifest = {
            "schema": GENERATION_SCHEMA,
            "generation_id": generation_id,
            "created_at": utc_now(),
            "archive_schema": ARCHIVE_SCHEMA,
            "volume": asdict(identity),
            "config_path": os.fspath(config.resolve(strict=True)),
            "source_mutation": False,
            "packing_authorized": False,
            "quarantine_authorized": False,
            "deletion_authorized": False,
            "entries": entries,
            "entry_count": len(entries),
            "payload_bytes": sum(int(row["size"]) for row in entries),
        }
        manifest["entries_digest"] = sha256_bytes(canonical_json(entries))
        _write_new_json(staging / "manifest.json", manifest)
        verification = verify_generation(staging)
        if verification["status"] != "PASS":
            raise ReplicaError("staging generation did not verify")
        os.replace(staging, published)
        _fsync_directory(generations)
        final_verification = verify_generation(published)
        if final_verification["status"] != "PASS":
            raise ReplicaError("published generation did not verify")
        return {
            "status": "REPLICA_VERIFIED",
            "generation": os.fspath(published),
            "generation_id": generation_id,
            "manifest_sha256": sha256_file(published / "manifest.json"),
            "entry_count": len(entries),
            "payload_bytes": manifest["payload_bytes"],
            "volume": asdict(identity),
            "required_free_bytes": required_free,
            "safety": {
                "source_mutation": False,
                "atomic_publication": True,
                "overwrite": False,
                "packing_authorized": False,
                "quarantine_authorized": False,
                "deletion_authorized": False,
            },
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _prepare_archive_root(root: Path, identity: VolumeIdentity) -> None:
    if root.is_symlink():
        raise ReplicaError(f"archive root may not be a symlink: {root}")
    identity_path = root / "archive.json"
    if root.exists():
        if not root.is_dir():
            raise ReplicaError(f"archive root collides with a non-directory: {root}")
        if identity_path.exists():
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            if payload.get("schema") != ARCHIVE_SCHEMA:
                raise ReplicaError("existing archive has an unknown schema")
            if payload.get("volume_uuid") != identity.volume_uuid:
                raise ReplicaError("existing archive belongs to a different volume UUID")
            return
        if any(root.iterdir()):
            raise ReplicaError("existing nonempty archive root lacks identity; refusing collision")
    else:
        root.mkdir(mode=0o700, parents=True)
    _write_new_json(
        identity_path,
        {
            "schema": ARCHIVE_SCHEMA,
            "archive_id": str(uuid.uuid4()),
            "created_at": utc_now(),
            "volume_uuid": identity.volume_uuid,
            "device_identifier_at_creation": identity.device_identifier,
            "encrypted_at_creation": identity.encrypted,
            "authority": "independent_replica_only",
        },
    )
    _fsync_directory(root)


def _copy_item(item: ReplicaItem, payload_root: Path) -> list[dict[str, Any]]:
    if not item.path.exists():
        if item.required:
            raise ReplicaError(f"required source disappeared: {item.path}")
        return []
    if item.path.is_symlink():
        raise ReplicaError(f"source symlinks are not accepted: {item.path}")
    label_root = payload_root / item.label
    label_root.mkdir(mode=0o700)
    if item.kind == "sqlite":
        destination = label_root / item.path.name
        _snapshot_sqlite(item.path, destination)
        return [_entry(item, destination, payload_root, item.path, "sqlite")]
    if item.kind == "file":
        if not item.path.is_file():
            raise ReplicaError(f"source must be a regular file: {item.path}")
        destination = label_root / item.path.name
        _copy_stable_file(item.path, destination)
        return [_entry(item, destination, payload_root, item.path, "file")]
    if not item.path.is_dir():
        raise ReplicaError(f"tree source must be a directory: {item.path}")
    result: list[dict[str, Any]] = []
    for source in _tree_files(item.path):
        relative = source.relative_to(item.path)
        destination = label_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _copy_stable_file(source, destination)
        result.append(_entry(item, destination, payload_root, source, "file", relative))
    return result


def _tree_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ReplicaError(f"tree root must be a real directory: {root}")
    result: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in list(dirnames):
            candidate = directory_path / name
            if candidate.is_symlink():
                raise ReplicaError(f"tree contains a directory symlink: {candidate}")
        for name in filenames:
            candidate = directory_path / name
            if candidate.is_symlink() or not candidate.is_file():
                raise ReplicaError(f"tree contains a non-regular file: {candidate}")
            result.append(candidate)
    return sorted(result)


def _copy_stable_file(source: Path, destination: Path, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            with source.open("rb") as reader:
                before = os.fstat(reader.fileno())
                with destination.open("xb") as writer:
                    os.chmod(destination, 0o600)
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                after = os.fstat(reader.fileno())
            if (
                before.st_size == after.st_size == destination.stat().st_size
                and before.st_mtime_ns == after.st_mtime_ns
            ):
                return
        except FileExistsError:
            raise ReplicaError(f"replica destination already exists: {destination}")
        destination.unlink(missing_ok=True)
        if attempt + 1 == attempts:
            raise ReplicaError(f"source changed during copy after {attempts} attempts: {source}")


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ReplicaError(f"SQLite source must be a regular file: {source}")
    if destination.exists():
        raise ReplicaError(f"replica destination already exists: {destination}")
    uri = f"file:{quote(os.fspath(source), safe='/')}?mode=ro"
    source_connection = sqlite3.connect(uri, uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        check = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if check != ("ok",):
            raise ReplicaError(f"SQLite snapshot integrity failed: {source}: {check}")
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def _entry(
    item: ReplicaItem,
    destination: Path,
    payload_root: Path,
    source: Path,
    stored_kind: str,
    tree_relative: Path | None = None,
) -> dict[str, Any]:
    return {
        "label": item.label,
        "configured_kind": item.kind,
        "stored_kind": stored_kind,
        "source_path": os.fspath(source),
        "tree_relative_path": os.fspath(tree_relative) if tree_relative is not None else None,
        "payload_path": os.fspath(destination.relative_to(payload_root.parent)),
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def verify_generation(generation: Path) -> dict[str, Any]:
    generation = generation.resolve(strict=True)
    manifest_path = generation / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema") != GENERATION_SCHEMA:
        raise ReplicaError("generation manifest has an unknown schema")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ReplicaError("generation entries are missing")
    if payload.get("entries_digest") != sha256_bytes(canonical_json(entries)):
        raise ReplicaError("generation entry digest mismatch")
    checked = 0
    total = 0
    sqlite_checked = 0
    for row in entries:
        if not isinstance(row, dict):
            raise ReplicaError("generation entry is not an object")
        relative = Path(str(row.get("payload_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ReplicaError(f"unsafe generation payload path: {relative}")
        candidate = (generation / relative).resolve(strict=True)
        if generation not in candidate.parents:
            raise ReplicaError(f"generation payload escapes root: {relative}")
        observed_size = candidate.stat().st_size
        if observed_size != row.get("size") or sha256_file(candidate) != row.get("sha256"):
            raise ReplicaError(f"generation payload mismatch: {relative}")
        if row.get("stored_kind") == "sqlite":
            connection = sqlite3.connect(f"file:{quote(os.fspath(candidate), safe='/')}?mode=ro", uri=True)
            try:
                check = connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                connection.close()
            if check != ("ok",):
                raise ReplicaError(f"replica SQLite integrity failed: {relative}: {check}")
            sqlite_checked += 1
        checked += 1
        total += observed_size
    if checked != payload.get("entry_count") or total != payload.get("payload_bytes"):
        raise ReplicaError("generation aggregate counts do not match")
    return {
        "schema": VERIFY_SCHEMA,
        "status": "PASS",
        "verified_at": utc_now(),
        "generation": os.fspath(generation),
        "generation_id": payload.get("generation_id"),
        "manifest_sha256": sha256_file(manifest_path),
        "entry_count": checked,
        "payload_bytes": total,
        "sqlite_integrity_checks": sqlite_checked,
    }


def restore_drill(generation: Path) -> dict[str, Any]:

    source = generation.resolve(strict=True)
    source_verification = verify_generation(source)
    with tempfile.TemporaryDirectory(prefix="usage-replica-restore-") as temporary:
        restored = Path(temporary) / "generation"
        shutil.copytree(source, restored, symlinks=False)
        restored_verification = verify_generation(restored)
        if (
            source_verification["manifest_sha256"]
            != restored_verification["manifest_sha256"]
            or source_verification["entry_count"] != restored_verification["entry_count"]
            or source_verification["payload_bytes"] != restored_verification["payload_bytes"]
        ):
            raise ReplicaError("isolated restore differs from source generation")
    return {
        "schema": RESTORE_SCHEMA,
        "status": "RESTORE_DRILL_PASS",
        "verified_at": utc_now(),
        "generation": os.fspath(source),
        "generation_id": source_verification["generation_id"],
        "manifest_sha256": source_verification["manifest_sha256"],
        "entry_count": source_verification["entry_count"],
        "payload_bytes": source_verification["payload_bytes"],
        "sqlite_integrity_checks": source_verification["sqlite_integrity_checks"],
        "isolated_temporary_restore_removed": True,
        "packing_authorized": False,
        "quarantine_authorized": False,
        "deletion_authorized": False,
    }


def write_receipt(path: Path, value: object) -> None:

    path = path.expanduser()
    if not path.is_absolute():
        path = path.absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_new_json(path, value)


def _write_new_json(path: Path, value: object) -> None:
    raw = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
