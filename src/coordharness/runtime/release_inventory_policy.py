
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any


def is_reviewed_ambient_release_path(relative_path: str) -> bool:

    parts = PurePosixPath(relative_path).parts
    return bool(parts) and (parts[-1] == ".DS_Store" or "__pycache__" in parts)


class ReleaseInventoryError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reviewed_runtime_inventory(root: Path, *, workers: int = 8) -> tuple[str, int]:

    root = root.resolve(strict=True)
    records: list[dict[str, Any]] = []
    regular: list[tuple[Path, str, int, int]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(dirs):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if is_reviewed_ambient_release_path(rel):
                dirs.remove(name)
                continue
            if path.is_symlink():
                dirs.remove(name)
                records.append(
                    {"path": rel, "kind": "symlink", "target": os.readlink(path)}
                )
        for name in files:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if is_reviewed_ambient_release_path(rel):
                continue
            if path.is_symlink():
                records.append(
                    {"path": rel, "kind": "symlink", "target": os.readlink(path)}
                )
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseInventoryError(f"unsupported release runtime node: {rel}")
            regular.append((path, rel, stat.S_IMODE(info.st_mode), info.st_size))

    paths = [item[0] for item in regular]
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 16))) as pool:
        digests = list(pool.map(_sha256_file, paths))
    records.extend(
        {
            "path": rel,
            "kind": "file",
            "mode": mode,
            "size": size,
            "sha256": digest,
        }
        for (_path, rel, mode, size), digest in zip(regular, digests, strict=True)
    )
    records.sort(key=lambda item: str(item["path"]))
    canonical = (
        json.dumps(records, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return hashlib.sha256(canonical).hexdigest(), len(records)


__all__ = [
    "ReleaseInventoryError",
    "is_reviewed_ambient_release_path",
    "reviewed_runtime_inventory",
]
