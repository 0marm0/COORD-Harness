"""Where the harness looks for your project, and where it keeps its own state.

Two roots, deliberately separate:

  PROJECT ROOT  the repository the agents are working in. Done-signal artifacts
                are resolved relative to this, because that is how agents refer
                to them -- `docs/reports/thing.md`, not an absolute path.
                Defaults to the current working directory; set `COORD_PROJECT_ROOT`
                to override.

  STATE DIR     where the harness keeps its database and job telemetry.
                Defaults to `.coordharness/` inside the project root; set
                `COORD_HOME` to put it elsewhere, for instance on a different
                disk or outside a repository you do not want to dirty.

The system this was extracted from ran these together, which meant a completion
proof was looked up beneath the database directory rather than beneath the
project. Keeping them apart is the fix.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "HARNESS_ROOT",
    "actor_name",
    "coord_db_path",
    "deployment_profile",
    "is_strict_deployment",
    "job_progress_dir",
    "knowledge_db_path",
    "project_root",
    "public_path_ref",
    "resource_modes_path",
    "source_date_epoch",
    "state_dir",
]

_SAFE_ACTOR_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_GENERIC_PROFILES = {"generic", "local", "public"}
_STRICT_PROFILES = {"strict", "deployment", "exact-authority"}


def actor_name(value: str | None = None) -> str:
    """Return the configured actor after validating its public identifier."""
    raw = str(value if value is not None else os.environ.get("COORD_ACTOR", "local"))
    clean = raw.strip().lower()
    if _SAFE_ACTOR_RE.fullmatch(clean) is None:
        raise ValueError(
            "COORD_ACTOR must start with a letter and contain only "
            "lowercase letters, digits, dot, underscore, or hyphen (max 64 chars)"
        )
    return clean


def deployment_profile() -> str:
    """Runtime profile. Generic is portable; strict enables deployment custody gates."""
    raw = str(os.environ.get("COORD_DEPLOYMENT_PROFILE", "generic")).strip().lower()
    if raw in _GENERIC_PROFILES:
        return "generic"
    if raw in _STRICT_PROFILES:
        return "strict"
    raise ValueError(
        "COORD_DEPLOYMENT_PROFILE must be generic or strict "
        f"(got {raw!r})"
    )


def is_strict_deployment() -> bool:
    return deployment_profile() == "strict"


def source_date_epoch(default: float | None = None) -> float | None:
    """Return the standard reproducible-build clock when configured."""
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative number") from exc
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative finite number")
    return value


def project_root() -> Path:
    """The repository being coordinated. Artifact paths resolve against this."""
    override = os.environ.get("COORD_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def state_dir() -> Path:
    """Where the harness keeps its own files.

    Path discovery is deliberately pure. Callers performing writes are
    responsible for creating the directory at their write boundary; read-only
    surfaces such as the board must never change the state tree merely by
    resolving a configured path.
    """
    override = os.environ.get("COORD_HOME")
    path = Path(override).expanduser().resolve() if override else project_root() / ".coordharness"
    return path


def coord_db_path() -> Path:
    """The coordination database. `COORD_DB` overrides the location outright."""
    override = os.environ.get("COORD_DB") or os.environ.get("COORD_COORD_DB")
    if override:
        return Path(override).expanduser()
    return state_dir() / "coord.db"


def knowledge_db_path() -> Path:
    """The full-text knowledge store used by fact lookup and search."""
    override = os.environ.get("COORD_KNOWLEDGE_DB")
    if override:
        return Path(override).expanduser()
    return state_dir() / "knowledge.db"


def job_progress_dir() -> Path:
    """Per-job telemetry sidecars, one JSON file per run."""
    return state_dir() / "job_progress"


def resource_modes_path() -> Path:
    """The current resource mode, read by the governor and the job launcher."""
    return state_dir() / "resource_modes.json"


def public_path_ref(value: str | os.PathLike[str]) -> str:
    """Render an absolute host path without persisting machine-specific prefixes."""
    path = Path(value)
    if not path.is_absolute():
        return str(value)
    resolved = path.resolve(strict=False)
    for prefix, root in (("state", state_dir()), ("project", project_root())):
        try:
            return f"{prefix}://{resolved.relative_to(root.resolve()).as_posix()}"
        except ValueError:
            continue
    return f"external://{resolved.name or 'path'}"


# Ported modules import this name directly and resolve completion artifacts
# beneath it, so it must be the project root rather than the state directory.
HARNESS_ROOT = project_root()
