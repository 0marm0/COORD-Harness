"""Small helpers shared across the harness."""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["sha256_file"]

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so large files do not load into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
