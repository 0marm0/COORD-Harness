from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import time
from typing import Iterable, Mapping

from coordharness import config as harness_config

DONE = "done"
RUNNING = "running"
QUEUED = "queued"
BLOCKED = "blocked"
PLANNED = "planned"
FAILED = "failed"

DATA_ARTIFACT_MIN_BYTES = {
    ".duckdb": 64 * 1024,
    ".parquet": 4 * 1024,
    ".json": 2,
    ".csv": 2,
}

_PATH_PREFIX_RE = re.compile(
    r"(?<![\w/.-])((?:docs|src|tests|\.agents|\.claude|\.coordharness)/[^\s,;()]+)"
)
_PAGE_PREFIX_RE = re.compile(r"src/coordharness/ui/pages/\s*([A-Za-z0-9_-]+)")
_ROUTE_RE = re.compile(r"(?<![\w.-])/([A-Za-z0-9_-]+)(?:/[A-Za-z0-9_-]+)*")
_DUCKDB_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_DEFAULT_REPO_PREFIXES = ("docs", "src", "tests", ".agents", ".claude", ".coordharness")
_SAFE_PATTERN_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

# Compatibility surface: standalone defaults intentionally carry no project process names.
COMPUTE_PROC_PATTERNS: dict[str, str] = {}


def _contained_config_path(raw: str, root: Path) -> Path:
    path = Path(raw).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    allowed = (root.resolve(), harness_config.state_dir().resolve(strict=False))
    if not any(resolved == base or base in resolved.parents for base in allowed):
        raise ValueError(f"configured status path escapes project and state roots: {raw!r}")
    return resolved


def load_compute_proc_patterns(
    *, env: Mapping[str, str] | None = None, root: str | Path | None = None
) -> dict[str, str]:
    source = os.environ if env is None else env
    inline = str(source.get("COORD_JOB_PROCESS_PATTERNS_JSON") or "").strip()
    file_value = str(source.get("COORD_JOB_PROCESS_PATTERNS_FILE") or "").strip()
    if inline and file_value:
        raise ValueError("configure only one of COORD_JOB_PROCESS_PATTERNS_JSON or FILE")
    if not inline and not file_value:
        return {}
    if file_value:
        config_root = Path(root).resolve() if root is not None else harness_config.project_root()
        inline = _contained_config_path(file_value, config_root).read_text(encoding="utf-8")
    try:
        parsed = json.loads(inline)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid process-pattern JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("process-pattern configuration must be a JSON object")
    out: dict[str, str] = {}
    for raw_key, raw_pattern in parsed.items():
        key = str(raw_key).strip()
        pattern = str(raw_pattern).strip()
        if _SAFE_PATTERN_KEY_RE.fullmatch(key) is None or not pattern:
            raise ValueError(f"invalid process-pattern entry {raw_key!r}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex for process-pattern {key!r}: {exc}") from exc
        out[key] = pattern
    return out

TERMINAL_GPU_STATUSES = {
    "done",
    "success",
    "complete",
    "completed",
    "finished",
    "failed",
    "skipped",
    "superseded",
}

# The words that mean nothing further will ever be written about a job.
#
# This answers a narrower question than "is this row finished": it asks whether
# another update is still owed. `sidecar_writer` stops writing on its own copy
# of this vocabulary -- blocked and paused included, which is why they are here
# even though neither is a completion -- and `board.snapshot` counts a row done
# on a third. This set has to cover all of them, because a word missing from it
# becomes a permanent staleness warning on work that is over, and a warning
# that never clears is worse than the silence it replaced.
# `tests/test_job_staleness_is_not_only_running.py` pins the cover.
TERMINAL_JOB_STATES = frozenset(
    {
        "archived",
        "blocked",
        "canceled",
        "cancelled",
        "closed",
        "complete",
        "completed",
        "dead",
        "done",
        "error",
        "errored",
        "failed",
        "finished",
        "killed",
        "orphaned",
        "paused",
        "skipped",
        "stalled",
        "success",
        "superseded",
    }
)

PASS_RUBRIC_VERDICTS = {"pass", "passed", "ok", "green"}
BLOCKING_RUBRIC_VERDICTS = {"blocked", "block"}
FAILING_RUBRIC_VERDICTS = {"flag", "fail", "failed", "red", "reject", "rejected"}


@dataclass(frozen=True)
class StatusEvidence:
    status: str
    done_signal_exists: bool
    proc_running: bool
    raw_status: str
    blocked_by: tuple[str, ...] = ()
    unverified: bool = False
    # Whether the item declared a proof at all, which `unverified` cannot say.
    # A done claim whose artifact resolved and a done claim that named no
    # artifact both come back unverified=False; without this they are one
    # label, and a caller counting verified work counts the second as the
    # first. Recorded on every branch, because it is a fact about the item
    # rather than about the question that happened to answer it.
    declared_proof: bool = False


def _within_allowed_status_roots(path: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    allowed = (root.resolve(), harness_config.state_dir().resolve(strict=False))
    return any(resolved == base or base in resolved.parents for base in allowed)


def resolve_done_signal(sig: str | None, root: str | Path) -> Path | None:
    if not isinstance(sig, str):
        return None
    raw = sig.strip()
    if not raw:
        return None
    base = Path(root).resolve()
    path = Path(raw).expanduser()
    candidate = path if path.is_absolute() else base / path
    resolved = candidate.resolve(strict=False)
    return resolved if _within_allowed_status_roots(resolved, base) else None


def _candidate_path(raw: str, root: Path) -> Path:
    candidates = tuple(_candidate_paths(raw, root))
    if not candidates:
        return Path(root).resolve() / "__invalid_uncontained_signal__"
    for path in candidates:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return candidates[0]


def _candidate_paths(raw: str, root: Path) -> Iterable[Path]:
    cleaned = raw.strip().strip("'\"`[]{}<>")
    if not cleaned:
        return
    base = Path(root).resolve()
    path = Path(cleaned).expanduser()
    candidate = path if path.is_absolute() else base / path
    resolved = candidate.resolve(strict=False)
    if _within_allowed_status_roots(resolved, base):
        yield resolved


def _valid_done_dir(path: Path) -> bool:
    try:
        if (path / "_SUCCESS").exists():
            return True
        for child in path.iterdir():
            if not child.is_file():
                continue
            try:
                size = child.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            min_bytes = DATA_ARTIFACT_MIN_BYTES.get(child.suffix.lower())
            if min_bytes is None or size >= min_bytes:
                return True
    except OSError:
        return False
    return False


def _valid_tiny_parquet(path: Path) -> bool:
    try:
        import pyarrow.parquet as pq
    except Exception:
        return False
    try:
        return int(pq.ParquetFile(path).metadata.num_rows) > 0
    except Exception:
        return False


def _valid_done_path(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if path.is_dir():
        return _valid_done_dir(path)
    min_bytes = DATA_ARTIFACT_MIN_BYTES.get(path.suffix.lower())
    if min_bytes is not None and st.st_size < min_bytes:
        if path.suffix.lower() == ".parquet" and st.st_size > 0:
            return _valid_tiny_parquet(path)
        return False
    if path.is_file() and st.st_size == 0:
        return False
    return True


def _valid_completion_candidate(path: Path) -> bool:

    if not _valid_done_path(path):
        return False
    from coordharness.coord.config import _WAREHOUSE_MARKERS

    data_layer_names = set(_WAREHOUSE_MARKERS)
    resolved_path = path.resolve(strict=False)
    path_views = (path, resolved_path)
    for candidate in (path, *path.parents, resolved_path, *resolved_path.parents):
        if candidate.name == "control" and candidate.parent.name in data_layer_names:
            return False
    if any(candidate.suffix.lower() in {".managed", ".lock", ".marker"} for candidate in path_views):
        return False
    canonical_namespace = any(
        candidate.parent.name == "job_progress"
        and candidate.parent.parent.name in data_layer_names
        for candidate in path_views
    )
    marker_namespace = any(
        candidate.parent.name == ".diagnostic_only"
        and candidate.parent.parent.name == "job_progress"
        and candidate.parent.parent.parent.name in data_layer_names
        for candidate in path_views
    )
    progress_directory = any(
        candidate.name == "job_progress"
        and candidate.parent.name in data_layer_names
        for candidate in path_views
    )
    marker_directory = any(
        candidate.name == ".diagnostic_only"
        and candidate.parent.name == "job_progress"
        and candidate.parent.parent.name in data_layer_names
        for candidate in path_views
    )
    if progress_directory or marker_directory or marker_namespace:
        return False
    try:
        multiply_linked = int(path.stat().st_nlink) > 1
    except OSError:
        multiply_linked = False
    if not path.is_file():
        return True
    if multiply_linked:
        return False
    if path.suffix.lower() != ".json" and not canonical_namespace:
        return True
    try:
        from coordharness.jobs.diagnostic_marker import read_sidecar_with_authority

        payload, authority = read_sidecar_with_authority(path)
    except Exception:
        payload, authority = None, None
    sidecar_shaped = bool(
        isinstance(payload, dict)
        and str(payload.get("job_id") or "").strip()
        and str(payload.get("roadmap_id") or "").strip()
        and (payload.get("state") is not None or payload.get("status") is not None)
    )
    control_shaped = bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and str(payload.get("job_id") or "").strip()
        and str(payload.get("launch_id") or "").strip()
        and payload.get("wrapper_pid") is not None
        and payload.get("sidecar_path") is not None
        and isinstance(payload.get("diagnostic_only"), bool)
    )
    if control_shaped:
        return False
    sidecar_namespace = bool(
        canonical_namespace
        or marker_namespace
        or (
            sidecar_shaped
            and authority is not None
            and authority.control_state != "legacy"
        )
        or (isinstance(payload, dict) and (
            payload.get("wrapper_managed") is True
            or payload.get("diagnostic_only") is True
        ))
    )
    if not sidecar_namespace:
        return True
    if not sidecar_shaped or authority is None or authority.diagnostic_only:
        return False
    state = str(payload.get("state") or payload.get("status") or "").strip().lower()
    return state in {"done", "complete", "completed", "success", "finished"}


def _done_signal_candidates(sig: str | None, root: str | Path) -> Iterable[Path]:
    if isinstance(sig, (list, tuple)):
        for member in sig:
            yield from _done_signal_candidates(member, root)
        return
    if not isinstance(sig, str):
        return
    raw = (sig or "").strip()
    if not raw:
        return
    base = Path(root)
    exact_raw = raw.strip().strip("'\"`[]{}<>")
    yield _candidate_path(exact_raw, base)

    if len(raw) > 4096:
        return

    for match in _PATH_PREFIX_RE.finditer(raw):
        token = match.group(1).rstrip(".:;,+")
        if token.endswith("/"):
            continue
        if token.endswith(".py") and token != exact_raw:
            continue
        if any(ch in token for ch in "*?[]"):
            seen_globs: set[Path] = set()
            for path in _candidate_paths(token, base):
                parent = path.parent
                pattern = path.name
                key = parent / pattern
                if key in seen_globs:
                    continue
                seen_globs.add(key)
                yield from path.parent.glob(path.name)
        else:
            yield from _candidate_paths(token, base)

    route_like = bool(re.fullmatch(r"(?:route[:\s]+)?/[\w/-]+", exact_raw, re.IGNORECASE)) \
        or exact_raw.startswith("src/coordharness/ui/pages/")
    if route_like:
        for match in _PAGE_PREFIX_RE.finditer(raw):
            slug = match.group(1).replace("-", "_")
            if slug:
                yield (base / "src" / "coordharness" / "ui" / "pages" / f"{slug}.py").resolve(strict=False)

        if "route" in raw.lower():
            for match in _ROUTE_RE.finditer(raw):
                slug = match.group(1).replace("-", "_")
                if slug:
                    yield (base / "src" / "coordharness" / "ui" / "pages" / f"{slug}.py").resolve(strict=False)


def _duckdb_table_signal(sig: str | None, root: str | Path) -> tuple[Path, str] | None:
    if not isinstance(sig, str):
        return None
    raw = (sig or "").strip().strip("'\"`[]{}<>")
    if "::" not in raw:
        return None
    path_raw, table = raw.split("::", 1)
    path = _candidate_path(path_raw, Path(root))
    table = table.strip().strip("'\"`[]{}<>")
    if path.suffix.lower() != ".duckdb" or not table:
        return None
    if not _DUCKDB_TABLE_RE.fullmatch(table):
        return None
    return path, table


def _quoted_duckdb_identifier(name: str) -> str:
    return ".".join(f'"{part.replace(chr(34), chr(34) * 2)}"' for part in name.split("."))


def _duckdb_table_has_row(path: Path, table: str) -> bool:
    if not _valid_done_path(path):
        return False
    try:
        import duckdb
    except Exception:
        return False
    con = None
    try:
        con = duckdb.connect(str(path), read_only=True)
        ident = _quoted_duckdb_identifier(table)
        return con.execute(f"SELECT 1 FROM {ident} LIMIT 1").fetchone() is not None
    except Exception:
        return False
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def done_signal_exists(sig: str | None, root: str | Path) -> bool:
    table_signal = _duckdb_table_signal(sig, root)
    if table_signal is not None:
        path, table = table_signal
        return _duckdb_table_has_row(path, table)
    return any(_valid_completion_candidate(p) for p in _done_signal_candidates(sig, root))


def _git_index_file(repo_root: str) -> str | None:
    dot_git = Path(repo_root) / ".git"
    try:
        if dot_git.is_dir():
            return str(dot_git / "index")
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip()).expanduser()
                if not target.is_absolute():
                    target = (Path(repo_root) / target).resolve(strict=False)
                return str(target / "index")
    except OSError:
        return None
    return None


def _git_index_token(repo_root: str) -> tuple[int, int, int]:
    index_file = _git_index_file(repo_root)
    if index_file:
        try:
            st = os.stat(index_file)
        except OSError:
            pass
        else:
            return (st.st_mtime_ns, st.st_size, st.st_ino)
    return (int(time.time()), -1, -1)


@lru_cache(maxsize=8)
def _git_tracked_files_at(repo_root: str, index_token: tuple[int, int, int]) -> frozenset[str]:
    proc = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return frozenset()
    return frozenset(
        part.decode("utf-8", errors="surrogateescape")
        for part in proc.stdout.split(b"\0")
        if part
    )


def _git_tracked_files(repo_root: str) -> frozenset[str]:
    return _git_tracked_files_at(repo_root, _git_index_token(repo_root))


def completion_proof_index_state(root: str | Path) -> dict[str, object]:
    resolved_root = str(Path(root).resolve(strict=False))
    repo_root = _git_repo_root(resolved_root)
    if not repo_root:
        return {
            "generation": "git-index-unavailable",
            "repo_root": None,
            "index_file": None,
            "index_token": None,
            "tracked_count": None,
        }
    token = _git_index_token(repo_root)
    generation_subject = (
        f"{Path(repo_root).resolve(strict=False)}\0"
        f"{token[0]}\0{token[1]}\0{token[2]}"
    ).encode("utf-8", errors="surrogateescape")
    return {
        "generation": f"git-index-{hashlib.sha256(generation_subject).hexdigest()[:20]}",
        "repo_root": repo_root,
        "index_file": _git_index_file(repo_root),
        "index_token": token,
        "tracked_count": len(_git_tracked_files(repo_root)),
    }


def refresh_completion_proof_index(root: str | Path) -> dict[str, object]:
    before = completion_proof_index_state(root)
    _git_tracked_files_at.cache_clear()
    after = completion_proof_index_state(root)
    return {
        "refresh_attempted": True,
        "generation_before": before["generation"],
        "generation_after": after["generation"],
        "tracked_count_after": after["tracked_count"],
    }


@lru_cache(maxsize=8)
def _git_repo_root(path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def completion_proof_is_tracked(path: str | Path, root: str | Path) -> bool:
    """Whether git's current index carries this declared proof.

    A file is carried when its repo-relative path is in ``git ls-files``. A
    *directory* proof is carried when at least one file beneath it is: git's
    index holds files and never directories, so asking the literal question of
    a directory would answer "no" for every directory that ever existed --
    including one whose entire contents are committed.
    """

    repo_root = _git_repo_root(str(Path(root).resolve(strict=False)))
    if not repo_root:
        return False
    candidate = Path(path).resolve(strict=False)
    try:
        relative = candidate.relative_to(Path(repo_root).resolve(strict=False)).as_posix()
    except ValueError:
        return False
    tracked = _git_tracked_files(repo_root)
    if relative in tracked:
        return True
    if not candidate.is_dir():
        return False
    prefix = f"{relative}/"
    return any(name.startswith(prefix) for name in tracked)


#: The env var that rebinds the exemption list, read only by
#: :func:`custody_exempt_suffixes` so no second surface can drift from it.
CUSTODY_EXEMPT_ENV = "COORD_COMPLETION_CUSTODY_EXEMPT"

#: The token that turns the custody requirement off entirely.
CUSTODY_EXEMPT_ALL = "*"

_SUFFIX_TOKEN_RE = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9._-]{0,31}")

# Artifact kinds that structurally cannot live in git's index, so "staged" is
# not a question that can be asked of them. Every other suffix -- every kind of
# plain text -- must be tracked before a completion is allowed.
#
# The list is derived from a read-only census of 6,249 declared completion
# artifacts on a long-lived coordination corpus, not from imagination: 27.4% of
# declared proofs were non-Markdown, and of those only the 124 rows below (~2%
# of all rows) were binary, large, or mutable enough that committing them is
# not a thing anyone does. The other ~93% of the non-Markdown share is plain
# text that should have been custodied all along.
#
#   .parquet  92 rows -- columnar dataset dump, binary and regenerable
#   .duckdb   21 rows -- embedded database file, binary and mutable in place
#   .db        5 rows -- embedded database file, same
#   .joblib    4 rows -- serialized model weights, binary
#   .bz2       1 row  -- compressed archive
#   .backup    1 row  -- backup snapshot bundle
#
# Deliberately NOT here: an IDE project bundle (`.xcodeproj`), the one other
# kind the census flagged as untrackable. It is a directory, and
# `completion_proof_is_tracked` already answers directories by looking for a
# tracked file inside -- which is the right answer for a bundle whose contents
# are normally committed, and a stricter one than a blanket exemption.
#
# This list grows by declaration, never by a silent "not Markdown" default: an
# operator with an artifact kind that is genuinely untrackable rebinds
# `COORD_COMPLETION_CUSTODY_EXEMPT` rather than waiting for a release.
DEFAULT_CUSTODY_EXEMPT_SUFFIXES: frozenset[str] = frozenset(
    {".parquet", ".duckdb", ".db", ".joblib", ".bz2", ".backup"}
)


def custody_exempt_suffixes() -> frozenset[str]:
    """Suffixes exempt from the git-custody requirement, for this process.

    Read from ``COORD_COMPLETION_CUSTODY_EXEMPT`` (comma-separated, leading dot
    optional, case-insensitive) on every call so a test or an operator can
    rebind the set without reimporting the gate. An explicit value *replaces*
    :data:`DEFAULT_CUSTODY_EXEMPT_SUFFIXES` rather than adding to it, so the
    effective list is always exactly what the variable says.

    Two boundary values are deliberate, and they fail in opposite directions:

    * ``"*"`` exempts every suffix -- the emergency off switch, which restores
      existence-only completion for artifacts of any kind, Markdown included.
    * an empty value exempts nothing, the strictest setting. Unlike
      ``COORD_LANES``, an empty list here is a coherent request rather than a
      broken one (it refuses nothing that a stricter deployment would not want
      refused), so it is honoured instead of raising.
    """

    raw = os.environ.get(CUSTODY_EXEMPT_ENV)
    if raw is None:
        return DEFAULT_CUSTODY_EXEMPT_SUFFIXES
    suffixes: set[str] = set()
    for token in str(raw).split(","):
        clean = token.strip().lower()
        if not clean:
            continue
        if clean == CUSTODY_EXEMPT_ALL:
            return frozenset({CUSTODY_EXEMPT_ALL})
        if _SUFFIX_TOKEN_RE.fullmatch(clean) is None:
            raise ValueError(
                f"{CUSTODY_EXEMPT_ENV} entry {token!r} is not a file suffix "
                f"(expected e.g. '.parquet,.duckdb', or '*' to exempt everything)"
            )
        suffixes.add(clean if clean.startswith(".") else f".{clean}")
    return frozenset(suffixes)


def custody_requires_tracking(path: str | Path) -> bool:
    """Whether this declared proof has to be in git's index to complete.

    True for every suffix that is not exempt -- including a proof with no
    suffix at all, which is the ambiguous case and is resolved toward custody.
    """

    exempt = custody_exempt_suffixes()
    if CUSTODY_EXEMPT_ALL in exempt:
        return False
    return Path(path).suffix.lower() not in exempt


def done_signal_custodied(sig: str | None, root: str | Path) -> bool:
    """Whether a declared proof is admissible *and* in the custody git can prove.

    Existence is never waived. On top of it, a proof must be carried by git's
    index unless its suffix is exempt (:func:`custody_exempt_suffixes`) -- the
    exemption is about custody, not about proof.

    Before 0.1.0 this requirement was scoped to ``.md`` and every other suffix
    short-circuited to True, so the headline promise ("completion is refused
    until the declared proof is in the index") held for exactly one file
    extension. It now holds generally.
    """

    table_signal = _duckdb_table_signal(sig, root)
    if table_signal is not None:
        # A `path.duckdb::table` signal names rows inside a database file, not
        # a file to stage. It is answered by reading the table, exactly as
        # before, and never reaches the custody question.
        path, table = table_signal
        return _duckdb_table_has_row(path, table)
    valid = [
        path
        for path in _done_signal_candidates(sig, root)
        if _valid_completion_candidate(path)
    ]
    if not valid:
        return False
    return any(
        not custody_requires_tracking(path) or completion_proof_is_tracked(path, root)
        for path in valid
    )


def any_done_signal_exists(item: Mapping, root: str | Path,
                           keys: tuple[str, ...] = ("done_signal", "done_signal_alt")) -> bool:
    return any(done_signal_exists(item.get(k), root) for k in keys)


def artifact_settled(sig: str | None, root: str | Path, *,
                     settle_s: float = 10.0, now: float | None = None) -> bool:
    import time

    now = time.time() if now is None else now
    table_signal = _duckdb_table_signal(sig, root)
    if table_signal is not None:
        candidates = (table_signal[0],) if _duckdb_table_has_row(*table_signal) else ()
    else:
        candidates = _done_signal_candidates(sig, root)
    for p in candidates:
        if not _valid_completion_candidate(p):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age >= settle_s or age < 0:
            return True
    return False


def any_artifact_settled(item: Mapping, root: str | Path, *,
                         keys: tuple[str, ...] = ("done_signal", "done_signal_alt"),
                         settle_s: float = 10.0, now: float | None = None) -> bool:
    return any(artifact_settled(item.get(k), root, settle_s=settle_s, now=now) for k in keys)


def _proc_running(pattern: str, ps_text: str) -> bool:
    if not pattern or not ps_text:
        return False
    try:
        return re.search(pattern, ps_text) is not None
    except re.error:
        return pattern in ps_text


def derive_status(item: Mapping, root: str | Path, *,
                  ps_text: str = "",
                  proc_patterns: Mapping[str, str] | None = None,
                  dependency_statuses: Mapping[str, str] | None = None) -> StatusEvidence:
    """Answer what an item is doing from evidence, not from its own status word.

    A `done` claim is answered three ways, and callers need to tell them apart.
    The artifact resolved: `unverified=False, declared_proof=True`. An artifact
    was declared and is not there: `unverified=True`. No artifact was ever
    declared: `unverified=False, declared_proof=False`.

    That last case is a deliberate limitation, not an oversight. `coord create`
    requires `--done-signal`, so a job reaching here with no signal is an
    orphan or unlinked sidecar rather than one dodging a gate, and refusing it
    would enforce a requirement its author was never given. It is still weaker
    evidence than a resolved artifact, so it is labelled rather than counted as
    equal: `declared_proof` is what separates "checked" from "never asked".
    """
    iid = str(item.get("id") or item.get("job") or item.get("job_id") or "")
    raw = str(item.get("status") or item.get("state") or "").strip().lower()
    rubric = str(item.get("rubric_verdict") or "").strip().lower()
    sig = item.get("done_signal")
    has_signal = bool(str(sig or "").strip())
    done = done_signal_exists(sig, root)

    patterns = load_compute_proc_patterns(root=root)
    if proc_patterns:
        patterns.update(proc_patterns)
    pattern = item.get("proc_pattern") or patterns.get(iid)
    proc = _proc_running(str(pattern), ps_text) if pattern else False

    if proc:
        return StatusEvidence(RUNNING, done, True, raw, declared_proof=has_signal)
    if rubric in BLOCKING_RUBRIC_VERDICTS:
        return StatusEvidence(BLOCKED, done, False, raw, declared_proof=has_signal)
    if rubric in FAILING_RUBRIC_VERDICTS:
        return StatusEvidence(QUEUED, done, False, raw, declared_proof=has_signal)
    if done:
        return StatusEvidence(DONE, True, False, raw, declared_proof=has_signal)
    done_words = {"done", "complete", "completed", "finished", "success", "superseded"}
    # Three cases, three labels. A claim that named no artifact is not refused
    # -- see the docstring -- but it is no longer reported as though an
    # artifact had been read.
    if raw in done_words and not has_signal:
        return StatusEvidence(DONE, False, False, raw, declared_proof=False)
    if raw in done_words and has_signal:
        return StatusEvidence(DONE, False, False, raw, unverified=True, declared_proof=True)

    reaped = str(item.get("_reaped") or "").strip().lower()
    if raw in {"failed", "error", "errored", "killed", "dead", "stalled"} \
            or reaped in {"failed", "killed", "stalled", "error"}:
        return StatusEvidence(FAILED, False, False, raw, declared_proof=has_signal)

    blocked_by: tuple[str, ...] = ()
    deps = [str(d) for d in (item.get("depends_on") or [])]
    if dependency_statuses and deps:
        blocked_by = tuple(d for d in deps if dependency_statuses.get(d) != DONE)
    if raw == BLOCKED or blocked_by:
        return StatusEvidence(BLOCKED, False, False, raw, blocked_by, declared_proof=has_signal)
    if raw in {"queued", "running", "active", "in_progress", "in-progress", "live"}:
        return StatusEvidence(QUEUED, False, False, raw, declared_proof=has_signal)
    return StatusEvidence(PLANNED, False, False, raw, declared_proof=has_signal)


def is_terminal_gpu_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in TERMINAL_GPU_STATUSES


def is_terminal_job_state(state: str | None) -> bool:
    """Whether this state means no further update is owed. See the set above."""
    return str(state or "").strip().lower() in TERMINAL_JOB_STATES


_SLUG_RE = re.compile(r"[^a-z0-9]+")
DEFAULT_STALE_WINDOW_S = 15 * 60.0


def parse_updated_at(value: object) -> float | None:
    import math

    if isinstance(value, bool):
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        pass
    raw = str(value or "").strip()
    if not raw:
        return None
    from datetime import datetime, timezone

    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def age_seconds(updated_at: object, now: float) -> float | None:
    ts = parse_updated_at(updated_at)
    if ts is None:
        return None
    return max(0.0, now - ts)


def is_stale(updated_at: object, now: float,
             window_s: float = DEFAULT_STALE_WINDOW_S) -> bool:
    age = age_seconds(updated_at, now)
    if age is None:
        return True
    return age > window_s


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")


def canonical_id(item: Mapping) -> str:
    for key in ("roadmap_id", "id", "job_id"):
        val = str(item.get(key) or "").strip()
        if val:
            return val
    for key in ("name", "title"):
        slug = _slug(str(item.get(key) or ""))
        if slug:
            return slug
    return ""


def dedup_key(item: Mapping) -> str:
    return canonical_id(item).lower()


MAX_ETA_S = 400 * 86400


def format_eta(seconds: float | None) -> str:
    import math

    if seconds is None:
        return "—"
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(sec) or sec < 0 or sec > MAX_ETA_S:
        return "—"
    s = int(sec)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def pct_display(done: object = None, total: object = None,
                pct: object = None) -> str:
    import math

    def _num(v: object) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    p = _num(pct)
    if p is None:
        d, t = _num(done), _num(total)
        if d is not None and t and t > 0:
            p = 100.0 * d / t
    if p is None:
        return "—"
    p = max(0.0, min(100.0, p))
    return f"{p:.1f}%"


_DATA_DIR = harness_config.state_dir()
_MODE_FRESH_S = 90.0


def read_mode_sot(now: float | None = None, data_dir: object = None) -> dict:
    import json
    import time

    base = Path(str(data_dir)) if data_dir is not None else _DATA_DIR
    now = time.time() if now is None else now

    gov_mode = None
    gov_fresh = False
    try:
        g = json.loads((base / "governor_status.json").read_text())
        gm = str(g.get("mode") or "").strip().lower()
        ts = parse_updated_at(g.get("timestamp") or g.get("updated_at") or g.get("ts"))
        if gm:
            gov_mode = gm
            age = (now - ts) if ts is not None else None
            gov_fresh = age is not None and 0.0 <= age <= _MODE_FRESH_S
    except Exception:
        pass

    txt_mode = None
    try:
        raw = (base / "resource_mode.txt").read_text().strip().lower()
        txt_mode = (raw.split()[0] if raw else "") or None
    except Exception:
        pass

    if gov_mode and gov_fresh:
        mode, source = gov_mode, "governor_status"
    elif txt_mode:
        mode, source = txt_mode, "resource_mode_txt"
    elif gov_mode:
        mode, source = gov_mode, "governor_status_stale"
    else:
        mode, source = "unknown", "offline"

    pending = bool(txt_mode and gov_mode and txt_mode != gov_mode)
    return {"mode": mode, "pending": pending, "source": source}
