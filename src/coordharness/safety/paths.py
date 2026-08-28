"""Fail-closed resolution for state-driven paths.

The nearest existing ancestor is resolved before a missing suffix is appended.
That matters for future output files: a symlinked parent cannot smuggle a write
outside the root merely because the final file does not exist yet.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath


class PathSafetyError(ValueError):
    """A supplied path is not provably contained by its trusted root."""


def realpath_nearest_existing(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    missing: list[str] = []
    current = candidate
    while not current.exists() and current != current.parent:
        missing.append(current.name)
        current = current.parent
    resolved = current.resolve(strict=True)
    for part in reversed(missing):
        resolved = resolved / part
    return resolved


def _has_traversal(value: str | os.PathLike[str]) -> bool:
    return ".." in PurePath(os.fspath(value).replace("\\", "/")).parts


def is_within_root(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        resolved = realpath_nearest_existing(path)
        trusted = Path(root).expanduser().resolve(strict=True)
        resolved.relative_to(trusted)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def resolve_under_root(
    value: str | os.PathLike[str],
    root: str | os.PathLike[str],
    *,
    must_exist: bool = True,
    allow_absolute: bool = False,
    allow_root: bool = False,
) -> Path:
    """Resolve one untrusted path beneath ``root`` or raise.

    Lexical traversal is rejected even when normalisation would land back under
    the root. Absolute values are opt-in and still undergo realpath containment.
    """

    raw = os.fspath(value)
    if not raw or "\x00" in raw:
        raise PathSafetyError("path must be non-empty and contain no NUL byte")
    if _has_traversal(raw):
        raise PathSafetyError("path traversal is not allowed")

    try:
        trusted = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathSafetyError("trusted root does not resolve") from exc

    supplied = Path(raw).expanduser()
    if supplied.is_absolute():
        if not allow_absolute:
            raise PathSafetyError("absolute paths are not allowed")
        candidate = supplied
    else:
        candidate = trusted / supplied

    try:
        resolved = realpath_nearest_existing(candidate)
        resolved.relative_to(trusted)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathSafetyError("path escapes the trusted root") from exc

    if not allow_root and resolved == trusted:
        raise PathSafetyError("path must name an entry below the trusted root")
    if must_exist and not resolved.exists():
        raise PathSafetyError("path does not exist")
    return resolved


def public_ref(path: Path, *, project_root: Path, state_root: Path) -> str:
    """Return a host-independent reference without disclosing absolute paths."""

    resolved = path.resolve(strict=False)
    for prefix, root in (("project", project_root), ("state", state_root)):
        try:
            relative = resolved.relative_to(root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
        return f"{prefix}://{relative.as_posix()}"
    return "external://untrusted"
