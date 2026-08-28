"""Unified, machine-readable and read-only health checks for the harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any, Iterable
from urllib.parse import quote

from .mcp import McpConfigError, McpServer, read_config, redacted_inventory, security_issues
from .paths import (
    PathSafetyError,
    is_within_root,
    public_ref,
    realpath_nearest_existing,
    resolve_under_root,
)
from .writers import inventory_lifecycle_writers, unexpected_writer_modules


REPORT_SCHEMA = "coordharness.doctor.v1"
PASS = "PASS"
BLOCKED = "BLOCKED"
_HELD_CLAIMS = ("running", "paused", "blocked")
_TERMINAL_WORK = ("done", "closed", "superseded", "archived", "cancelled")
_POINTER_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_POINTER_OK = "ok"
_POINTER_PENDING = "pending"
_POINTER_ABSOLUTE = "absolute"
_POINTER_INVALID = "invalid"
_EXPECTED_TABLES = {
    "agent_sessions",
    "claims",
    "events",
    "runs",
    "schema_migrations",
    "work_items",
}
_EXPECTED_VIEWS = {"v_runs_read_model", "v_session_rollup", "v_work_owner"}
_ALLOWED_WRITER_MODULES = {
    "bootstrap.py",
    "coord/coord_db.py",
    "coord/create_schema.py",
    "coord/ingest.py",
    "coord/native_cockpit.py",
    "coord/reaper.py",
    "coord/run_events.py",
    "jobs/launch.py",
    "jobs/roadmap_binding.py",
}


@dataclass(frozen=True)
class DoctorFinding:
    id: str
    status: str
    summary: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finding(
    finding_id: str, blocked: bool, summary: str, **details: Any
) -> DoctorFinding:
    return DoctorFinding(
        id=finding_id,
        status=BLOCKED if blocked else PASS,
        summary=summary,
        details=details,
    )


def _expected_migrations() -> dict[str, str]:
    package = files("coordharness.coord")
    schema = package.joinpath("schema.sql")
    expected: dict[str, str] = {
        "coord_v1_initial": hashlib.sha256(schema.read_bytes()).hexdigest()
    }
    root = package.joinpath("migrations")
    for resource in root.iterdir():
        if not resource.name.endswith(".sql"):
            continue
        payload = resource.read_bytes()
        expected[resource.name] = hashlib.sha256(payload).hexdigest()
    return dict(sorted(expected.items()))


def _connect_read_only(path: Path) -> sqlite3.Connection:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise sqlite3.OperationalError("unsafe SQLite sidecar")
        if suffix == "-wal" and metadata.st_size > 0:
            raise sqlite3.OperationalError("live WAL snapshot refused")

    uri = f"file:{quote(str(path.resolve(strict=True)))}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _is_readable_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _check_schema(db_path: Path) -> tuple[DoctorFinding, sqlite3.Connection | None]:
    if not db_path.is_file() or db_path.is_symlink():
        return (
            _finding(
                "doctor.schema",
                True,
                "coordination database is missing or is not a regular direct file",
                database_present=False,
            ),
            None,
        )
    try:
        conn = _connect_read_only(db_path)
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        objects = conn.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        tables = {str(row["name"]) for row in objects if row["type"] == "table"}
        views = {str(row["name"]) for row in objects if row["type"] == "view"}
        migration_rows = {
            str(row["name"]): str(row["checksum"])
            for row in conn.execute("SELECT name,checksum FROM schema_migrations")
        }
        expected = _expected_migrations()
        missing_migrations = sorted(set(expected) - set(migration_rows))
        checksum_mismatches = sorted(
            name
            for name, checksum in expected.items()
            if name in migration_rows and migration_rows[name] != checksum
        )
        missing_tables = sorted(_EXPECTED_TABLES - tables)
        missing_views = sorted(_EXPECTED_VIEWS - views)
        blocked = bool(
            quick_check.lower() != "ok"
            or missing_tables
            or missing_views
            or missing_migrations
            or checksum_mismatches
        )
        return (
            _finding(
                "doctor.schema",
                blocked,
                "schema and migration inventory is current"
                if not blocked
                else "schema or migration integrity could not be proven",
                quick_check=quick_check,
                migration_count=len(migration_rows),
                missing_migrations=missing_migrations,
                checksum_mismatches=checksum_mismatches,
                missing_tables=missing_tables,
                missing_views=missing_views,
            ),
            conn,
        )
    except (OSError, sqlite3.Error, ValueError):
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return (
            _finding(
                "doctor.schema",
                True,
                "schema or migration integrity could not be read",
                database_present=True,
            ),
            None,
        )


def _check_writers(package_root: Path) -> DoctorFinding:
    sites, parse_errors = inventory_lifecycle_writers(package_root)
    unexpected = unexpected_writer_modules(sites, allowed_modules=_ALLOWED_WRITER_MODULES)
    modules = sorted({site.module for site in sites})
    blocked = bool(parse_errors or unexpected)
    return _finding(
        "doctor.lifecycle_writers",
        blocked,
        "lifecycle writer inventory is confined to declared modules"
        if not blocked
        else "lifecycle writer inventory contains unclassified modules",
        direct_writer_modules=modules,
        direct_writer_site_count=len(sites),
        unexpected_modules=unexpected,
        parse_errors=parse_errors,
    )


def _sample_ids(rows: Iterable[sqlite3.Row], key: str = "work_id") -> list[str]:
    return sorted({str(row[key]) for row in rows if row[key] is not None})[:8]


def _check_leases_reviews(conn: sqlite3.Connection | None, *, now: float) -> DoctorFinding:
    if conn is None:
        return _finding(
            "doctor.leases_reviews",
            True,
            "lease and review checks require a readable current schema",
            check_available=False,
        )
    try:
        claim_marks = ",".join("?" for _ in _HELD_CLAIMS)
        expired_claims = conn.execute(
            f"SELECT work_id FROM claims WHERE status IN ({claim_marks}) AND expires_at<=?",
            (*_HELD_CLAIMS, now),
        ).fetchall()
        expired_sessions = conn.execute(
            "SELECT session_id FROM agent_sessions WHERE state='active' AND lease_until<=?",
            (now,),
        ).fetchall()
        orphan_running = conn.execute(
            "SELECT w.work_id FROM work_items w WHERE lower(w.intent_state)='running'"
            " AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.work_id=w.work_id"
            f" AND c.status IN ({claim_marks}))",
            _HELD_CLAIMS,
        ).fetchall()
        terminal_marks = ",".join("?" for _ in _TERMINAL_WORK)
        unresolved_reviews = conn.execute(
            "SELECT DISTINCT req.work_id FROM events req JOIN work_items w ON w.work_id=req.work_id"
            " WHERE req.kind='audit_request'"
            f" AND lower(w.intent_state) IN ({terminal_marks})"
            " AND NOT EXISTS (SELECT 1 FROM events verdict WHERE verdict.work_id=req.work_id"
            " AND verdict.kind='audit_verdict' AND verdict.event_id>req.event_id)",
            _TERMINAL_WORK,
        ).fetchall()
    except sqlite3.Error:
        return _finding(
            "doctor.leases_reviews",
            True,
            "lease or review query failed closed",
            check_available=False,
        )

    blocked = bool(expired_claims or expired_sessions or orphan_running or unresolved_reviews)
    return _finding(
        "doctor.leases_reviews",
        blocked,
        "leases and terminal review requests are coherent"
        if not blocked
        else "lease or terminal review debt is present",
        expired_claim_count=len(expired_claims),
        expired_claim_work_ids=_sample_ids(expired_claims),
        expired_session_count=len(expired_sessions),
        expired_session_ids=_sample_ids(expired_sessions, "session_id"),
        orphan_running_count=len(orphan_running),
        orphan_running_work_ids=_sample_ids(orphan_running),
        unresolved_terminal_review_count=len(unresolved_reviews),
        unresolved_terminal_review_work_ids=_sample_ids(unresolved_reviews),
    )


def _read_json_object(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _table_ids(conn: sqlite3.Connection | None, table: str, column: str) -> set[str]:
    if conn is None:
        return set()
    try:
        return {str(row[0]) for row in conn.execute(f"SELECT {column} FROM {table}")}
    except sqlite3.Error:
        return set()


def _check_jobs_projection(
    conn: sqlite3.Connection | None, *, state_root: Path
) -> DoctorFinding:
    problems: list[str] = []
    sidecar_count = 0
    work_ids = _table_ids(conn, "work_items", "work_id")
    try:
        job_dir = resolve_under_root(
            "job_progress", state_root, must_exist=False, allow_root=False
        )
    except PathSafetyError:
        return _finding(
            "doctor.jobs_projection",
            True,
            "job telemetry root is not safely contained",
            sidecar_count=0,
            problem_codes=["unsafe_job_progress_root"],
        )

    if job_dir.exists():
        if job_dir.is_symlink() or not job_dir.is_dir():
            problems.append("unsafe_job_progress_root")
        else:
            try:
                entries = sorted(job_dir.iterdir(), key=lambda item: item.name)
            except OSError:
                entries = []
                problems.append("job_progress_unreadable")
            for entry in entries:
                if entry.suffix != ".json":
                    continue
                sidecar_count += 1
                if entry.is_symlink():
                    problems.append("sidecar_symlink")
                    continue
                payload = _read_json_object(entry)
                if payload is None:
                    problems.append("sidecar_invalid_json")
                    continue
                job_id = str(payload.get("job_id") or "").strip()
                work_id = str(payload.get("roadmap_id") or payload.get("work_id") or "").strip()
                if not job_id or job_id != entry.stem or Path(job_id).name != job_id:
                    problems.append("sidecar_identity_mismatch")
                if not work_id:
                    problems.append("sidecar_work_binding_missing")
                elif conn is None or work_id not in work_ids:
                    problems.append("sidecar_work_binding_unknown")

    if conn is None:
        problems.append("projection_database_unavailable")
        live_run_count = 0
    else:
        live_run_count = 0
        for view in sorted(_EXPECTED_VIEWS):
            try:
                conn.execute(f"SELECT COUNT(*) FROM {view}").fetchone()
            except sqlite3.Error:
                problems.append("projection_view_unreadable")
        try:
            live_runs = conn.execute(
                "SELECT run_id,sidecar_path FROM runs WHERE state='live'"
            ).fetchall()
            live_run_count = len(live_runs)
            for row in live_runs:
                raw = str(row["sidecar_path"] or "").strip()
                if not raw:
                    problems.append("live_run_sidecar_missing")
                    continue
                try:
                    resolved = resolve_under_root(
                        raw,
                        state_root,
                        must_exist=True,
                        allow_absolute=True,
                        allow_root=False,
                    )
                    resolved.relative_to(job_dir)
                    if resolved.is_symlink() or _read_json_object(resolved) is None:
                        raise PathSafetyError("invalid live sidecar")
                except (PathSafetyError, ValueError):
                    problems.append("live_run_sidecar_unsafe")
        except sqlite3.Error:
            problems.append("live_run_projection_unreadable")
            live_run_count = 0

    unique = sorted(set(problems))
    return _finding(
        "doctor.jobs_projection",
        bool(unique),
        "job sidecars and projection views are coherent"
        if not unique
        else "job sidecar or projection integrity could not be proven",
        sidecar_count=sidecar_count,
        live_run_count=live_run_count,
        problem_codes=unique,
    )


def _classify_pointer(
    raw: str,
    *,
    project_root: Path,
    must_exist: bool,
    allow_legacy_absolute: bool = False,
) -> str:
    """Classify one stored pointer.

    Containment is the property being proven, and ``realpath_nearest_existing``
    proves it whether or not the pointer has been produced yet: a symlinked
    ancestor is resolved before the missing suffix is re-appended. A row that
    has not reached a terminal state and has not yet written its declared proof
    is therefore PENDING, not invalid -- the previous check additionally
    required the proof's parent directory to already exist, which is not a
    containment property and blocked every board that declares where a report
    will land before anyone has made the directory.
    """

    value = raw.strip()
    absolute = False
    if value.startswith("project://"):
        value = value.removeprefix("project://")
    elif _POINTER_SCHEME.match(value):
        return _POINTER_INVALID
    else:
        absolute = Path(value).is_absolute() if value else False
    if absolute and not allow_legacy_absolute:
        return _POINTER_INVALID
    try:
        resolved = resolve_under_root(
            value,
            project_root,
            must_exist=must_exist,
            allow_absolute=absolute,
            allow_root=False,
        )
    except (OSError, PathSafetyError, ValueError):
        return _POINTER_INVALID
    if absolute:
        return _POINTER_ABSOLUTE
    if must_exist or resolved.exists():
        return _POINTER_OK
    return _POINTER_PENDING


def _check_public_paths(
    conn: sqlite3.Connection | None, *, project_root: Path
) -> DoctorFinding:
    if conn is None:
        return _finding(
            "doctor.public_paths",
            True,
            "pointer checks require a readable current schema",
            check_available=False,
        )
    invalid: list[str] = []
    pending: list[str] = []
    absolute: list[str] = []
    checked = 0

    def record(field: str, verdict: str) -> None:
        if verdict == _POINTER_INVALID:
            invalid.append(field)
        elif verdict == _POINTER_PENDING:
            pending.append(field)
        elif verdict == _POINTER_ABSOLUTE:
            absolute.append(field)

    try:
        rows = conn.execute(
            "SELECT work_id,intent_state,context_pack_ref,done_signal FROM work_items"
            " WHERE archived_at IS NULL"
        ).fetchall()
        for row in rows:
            work_id = str(row["work_id"])
            context = str(row["context_pack_ref"] or "").strip()
            if context:
                checked += 1
                record(
                    f"{work_id}:context_pack_ref",
                    _classify_pointer(
                        context, project_root=project_root, must_exist=True
                    ),
                )

            signal = str(row["done_signal"] or "").strip()
            if not signal:
                continue
            checked += 1
            event_match = re.fullmatch(r"coord:event:([1-9][0-9]*)", signal)
            if event_match:
                exists = conn.execute(
                    "SELECT 1 FROM events WHERE event_id=?", (int(event_match.group(1)),)
                ).fetchone()
                if exists is None:
                    invalid.append(f"{work_id}:done_signal")
                continue
            terminal = str(row["intent_state"] or "").lower() in _TERMINAL_WORK
            record(
                f"{work_id}:done_signal",
                _classify_pointer(
                    signal, project_root=project_root, must_exist=terminal
                ),
            )

        # Artifact rows written before completion proofs were stored
        # repo-relative carry a resolved absolute path. Containment and
        # existence are still proven here; the pointer is reported as absolute
        # rather than treated as unproven, so one successful completion under
        # the old writer does not leave this check permanently blocked.
        for row in conn.execute("SELECT artifact_id,path FROM artifacts"):
            checked += 1
            record(
                f"{row['artifact_id']}:artifact_path",
                _classify_pointer(
                    str(row["path"] or ""),
                    project_root=project_root,
                    must_exist=True,
                    allow_legacy_absolute=True,
                ),
            )
    except sqlite3.Error:
        return _finding(
            "doctor.public_paths",
            True,
            "pointer queries failed closed",
            check_available=False,
        )
    invalid = sorted(set(invalid))
    pending = sorted(set(pending))
    absolute = sorted(set(absolute))
    return _finding(
        "doctor.public_paths",
        bool(invalid),
        "all local pointers are contained, and every produced pointer exists"
        if not invalid
        else "one or more local pointers lack existence or containment proof",
        checked_pointer_count=checked,
        invalid_pointer_count=len(invalid),
        invalid_pointer_fields=invalid[:12],
        pending_pointer_count=len(pending),
        pending_pointer_fields=pending[:12],
        absolute_pointer_count=len(absolute),
        absolute_pointer_fields=absolute[:12],
    )


def _resolve_config_path(raw: str, *, roots: tuple[Path, Path]) -> Path:
    for root in roots:
        try:
            return resolve_under_root(
                raw, root, must_exist=True, allow_absolute=True, allow_root=False
            )
        except PathSafetyError:
            continue
    raise PathSafetyError("MCP configuration is outside trusted roots")


def _check_mcp(
    config_paths: Iterable[str | os.PathLike[str]],
    *,
    project_root: Path,
    state_root: Path,
) -> DoctorFinding:
    paths = list(config_paths)
    records: list[McpServer] = []
    problem_codes: list[str] = []
    for raw in paths:
        try:
            path = _resolve_config_path(str(raw), roots=(project_root, state_root))
            source = public_ref(path, project_root=project_root, state_root=state_root)
            records.extend(read_config(path, source=source))
        except PathSafetyError:
            problem_codes.append("mcp.untrusted_config_path")
        except McpConfigError:
            problem_codes.append("mcp.invalid_config")
    issues = security_issues(records)
    problem_codes.extend(issue.code for issue in issues)
    unique = sorted(set(problem_codes))
    return _finding(
        "doctor.mcp_security",
        bool(unique),
        "MCP configuration inventory contains no unsafe literals or launch patterns"
        if not unique
        else "MCP configuration security could not be proven",
        config_count=len(paths),
        server_count=len(records),
        problem_codes=unique,
        issues=[issue.as_dict() for issue in issues],
        inventory=redacted_inventory(records),
        values_redacted=True,
    )


def run_doctor(
    *,
    db_path: str | os.PathLike[str],
    project_root: str | os.PathLike[str],
    state_root: str | os.PathLike[str],
    mcp_config_paths: Iterable[str | os.PathLike[str]] = (),
    now: float | None = None,
    package_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run all checks without creating, migrating, repairing, or writing state."""

    observed_at = float(time.time() if now is None else now)
    configs = list(mcp_config_paths)
    try:
        project = Path(project_root).expanduser().resolve(strict=True)
        state = Path(state_root).expanduser().resolve(strict=True)
        if not project.is_dir() or not state.is_dir():
            raise OSError("root is not a directory")
    except (OSError, RuntimeError):
        finding = _finding(
            "doctor.roots",
            True,
            "project and state roots must already exist as directories",
            check_available=False,
        )
        return {
            "schema": REPORT_SCHEMA,
            "status": BLOCKED,
            "read_only": True,
            "observed_at": observed_at,
            "findings": [finding.as_dict()],
        }

    # `--db` is a working-directory-relative path for every other verb, because
    # that is where the caller typed it. Resolving it against the state root
    # instead made doctor read a different file than the rest of the CLI, and a
    # database that escaped the root was reported as absent rather than as
    # uncontained.
    supplied_db = Path(db_path).expanduser()
    if not supplied_db.is_absolute():
        supplied_db = Path.cwd() / supplied_db
    try:
        supplied_db = realpath_nearest_existing(supplied_db)
    except (OSError, RuntimeError):
        supplied_db = Path(os.path.normpath(supplied_db))
    try:
        database: Path | None = resolve_under_root(
            str(supplied_db),
            state,
            must_exist=True,
            allow_absolute=True,
            allow_root=False,
        )
    except PathSafetyError:
        database = None

    package = (
        Path(package_root).resolve(strict=True)
        if package_root is not None
        else Path(__file__).resolve().parents[1]
    )
    if database is None:
        outside = is_within_root(supplied_db, state) is False and _is_readable_file(
            supplied_db
        )
        schema_finding = _finding(
            "doctor.schema",
            True,
            "coordination database is outside the trusted state root"
            if outside
            else "coordination database is missing or is not a regular direct file",
            database_present=_is_readable_file(supplied_db),
            database_outside_state_root=outside,
            state_root_ref=public_ref(state, project_root=project, state_root=state),
        )
        conn = None
    else:
        schema_finding, conn = _check_schema(database)
    findings = [
        schema_finding,
        _check_writers(package),
        _check_leases_reviews(conn, now=observed_at),
        _check_jobs_projection(conn, state_root=state),
        _check_public_paths(conn, project_root=project),
        _check_mcp(configs, project_root=project, state_root=state),
    ]
    if conn is not None:
        conn.close()
    findings.sort(key=lambda item: item.id)
    status = BLOCKED if any(item.status == BLOCKED for item in findings) else PASS
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "read_only": True,
        "observed_at": observed_at,
        "findings": [item.as_dict() for item in findings],
    }
