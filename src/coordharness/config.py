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

Alongside the two roots, the small amount of presentation an operator can own:
`COORD_BOARD_BRAND_NAME` and `COORD_BOARD_BRAND_TAGLINE`, read by the read-only
web board so an embedder's panels carry the embedder's name.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

__all__ = [
    "BOARD_BRAND_NAME",
    "HARNESS_ROOT",
    "actor_name",
    "board_brand_name",
    "board_brand_tagline",
    "coord_db_path",
    "deployment_profile",
    "is_strict_deployment",
    "job_progress_dir",
    "job_progress_dir_for_database",
    "knowledge_db_path",
    "project_root",
    "public_path_ref",
    "resource_modes_path",
    "source_date_epoch",
    "state_dir",
    "state_root_for_database",
]

_SAFE_ACTOR_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_GENERIC_PROFILES = {"generic", "local", "public"}
_STRICT_PROFILES = {"strict", "deployment", "exact-authority"}

# The product name the board paints on itself when nothing is configured.
BOARD_BRAND_NAME = "COORD"
_BRAND_MAX_CHARS = 64
_BRAND_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


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


def _validated_brand_text(value: str, variable: str) -> str:
    """Reject a brand string the shell cannot paint, and say which one it was."""
    if len(value) > _BRAND_MAX_CHARS:
        raise ValueError(
            f"{variable} must be at most {_BRAND_MAX_CHARS} characters "
            f"(got {len(value)})"
        )
    if _BRAND_CONTROL_CHARS.search(value):
        raise ValueError(f"{variable} must not contain control characters")
    return value


def board_brand_name() -> str:
    """The product name shown on the board's mark and in its document titles.

    Defaults to COORD. An operator embedding the read-only board inside their
    own tool sets `COORD_BOARD_BRAND_NAME` so the panels carry their name over
    their data instead of this project's.

    A blank value means unconfigured, not a blank brand: exporting the variable
    empty -- which a shell does readily -- returns the default rather than
    serving a nameless shell.
    """
    name = str(os.environ.get("COORD_BOARD_BRAND_NAME", "")).strip()
    if not name:
        return BOARD_BRAND_NAME
    return _validated_brand_text(name, "COORD_BOARD_BRAND_NAME")


def board_brand_tagline() -> str | None:
    """The second line under the mark, or None to keep each page's own wording.

    Unset is not the same as empty here. Every page ships its own sub-label --
    the board says `read-only runtime`, the map and the two atlases say
    `Intelligence` -- and leaving `COORD_BOARD_BRAND_TAGLINE` unset preserves
    that per-page wording. Setting it replaces the line on every page, which is
    the point: the lockup then reads as one operator's, not four pages'.
    """
    tagline = str(os.environ.get("COORD_BOARD_BRAND_TAGLINE", "")).strip()
    if not tagline:
        return None
    return _validated_brand_text(tagline, "COORD_BOARD_BRAND_TAGLINE")


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
    """Per-job telemetry sidecars, one JSON file per run.

    The ambient default: the telemetry belonging to the state tree this
    process is standing in. Writers -- the launcher, the sidecar writer, the
    demo seeder -- want exactly this, because they are writing into their own
    tree. A reader handed a database to serve wants
    `job_progress_dir_for_database` instead, so that both halves of what it
    shows come from the same place.
    """
    return state_dir() / "job_progress"


def state_root_for_database(db_path: str | os.PathLike[str]) -> Path:
    """The state root that owns a particular coordination database.

    Work rows come out of a named database; job telemetry came out of the
    ambient state tree, which never consulted that database. `coord-board --db
    /elsewhere/coord.db` therefore drew one screen out of two unrelated
    directories, and a genuinely empty database served whatever sidecars
    happened to be lying around -- which also put the honest empty board out of
    reach. The rule here is that telemetry belongs to the database it
    describes.

    In the default layout the database sits inside the state directory, so this
    returns exactly `state_dir()` and nothing about `coord-board` with no
    arguments changes. A database named outside that tree brings its own root:
    the directory it sits in. That is the same containment `coord doctor`
    already enforces from the other side -- it refuses to open a database it
    cannot place inside the state root it was given, and reports
    `database_outside_state_root` -- so the board no longer disagrees with the
    doctor about which files belong together.
    """
    supplied = Path(db_path).expanduser()
    ambient = state_dir()
    real_db = Path(os.path.realpath(str(supplied)))
    real_ambient = Path(os.path.realpath(str(ambient)))
    if real_db.is_relative_to(real_ambient) or supplied.resolve(
        strict=False
    ).is_relative_to(ambient):
        return ambient
    return real_db.parent


def job_progress_dir_for_database(db_path: str | os.PathLike[str]) -> Path:
    """Per-job telemetry for the state root that owns `db_path`."""
    return state_root_for_database(db_path) / "job_progress"


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
