"""Unified, machine-readable and read-only health checks for the harness.

Every non-PASS finding carries a stable machine-readable ``code``, its
``summary`` as the one-line human explanation, and a concrete ``remediation``
the reader can execute -- a command or a precise action, not just a status. A
finding that can fail for more than one independent reason at once also
carries a ``remediations`` list with one entry per triggered reason; the
top-level ``code``/``remediation`` mirror the first (highest-priority) entry
so a simple consumer can read two fields and a thorough one can read all of
them. These are additive fields on the existing v1 shape: ``id``, ``status``,
``summary`` and ``details`` are unchanged, so a consumer reading only those
still works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import hashlib
from http import HTTPStatus
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import time
import urllib.request
from typing import Any, Iterable
from urllib.parse import quote

from ..board.server import DEFAULT_PORT as _DEFAULT_BOARD_PORT
from ..coord.process_liveness import pid_matches
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
class Remediation:
    """One independently-triggered reason a finding is not PASS."""

    code: str
    summary: str
    action: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "summary": self.summary, "action": self.action}


@dataclass(frozen=True)
class DoctorFinding:
    id: str
    status: str
    summary: str
    details: dict[str, Any]
    code: str
    remediation: str | None = None
    remediations: tuple[Remediation, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
            "code": self.code,
            "remediation": self.remediation,
            "remediations": [item.as_dict() for item in self.remediations],
        }


def _finding(
    finding_id: str,
    blocked: bool,
    summary: str,
    *,
    code: str,
    remediation: str | None = None,
    remediations: tuple[Remediation, ...] = (),
    **details: Any,
) -> DoctorFinding:
    return DoctorFinding(
        id=finding_id,
        status=BLOCKED if blocked else PASS,
        summary=summary,
        details=details,
        code=code,
        remediation=remediation,
        remediations=remediations,
    )


def _prioritized(remediations: tuple[Remediation, ...]) -> tuple[str, str | None]:
    """The top-level (code, remediation) mirrored from the first entry, if any."""

    if not remediations:
        return "", None
    return remediations[0].code, remediations[0].action


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
        remediations = (
            Remediation(
                "doctor.schema.database_not_regular_file",
                "the database path exists but is not a plain regular file",
                "point --db at a real SQLite file (not a symlink or directory), "
                "or delete the entry at that path and let any writing `coord` "
                "subcommand (for example `coord demo --quiet`) recreate it",
            ),
        )
        code, remediation = _prioritized(remediations)
        return (
            _finding(
                "doctor.schema",
                True,
                "coordination database is missing or is not a regular direct file",
                code=code,
                remediation=remediation,
                remediations=remediations,
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
        # A never-bootstrapped file (the common "no database yet" state, once
        # one exists at all) has zero tables, `schema_migrations` included --
        # querying it unconditionally would raise "no such table" and fall
        # into the generic unreadable branch below, masking that state behind
        # the same code as an actually-corrupt file.
        migration_rows = (
            {
                str(row["name"]): str(row["checksum"])
                for row in conn.execute("SELECT name,checksum FROM schema_migrations")
            }
            if "schema_migrations" in tables
            else {}
        )
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
        remediations: tuple[Remediation, ...] = ()
        if blocked:
            # Priority order: a corrupt file first (nothing else can be trusted
            # until it is replaced), then a database that was never bootstrapped
            # at all (the common "no database yet" first-run state -- present
            # but with zero of the expected tables/views/migrations), then a
            # partially-migrated database (some but not all of the expected
            # schema is present, i.e. an older or interrupted bootstrap).
            if quick_check.lower() != "ok":
                remediations = (
                    Remediation(
                        "doctor.schema.integrity_check_failed",
                        "PRAGMA quick_check reported a SQLite integrity problem",
                        "the database file is corrupt; restore it from a backup, "
                        "or delete it and let a writing `coord` subcommand "
                        "re-bootstrap a fresh one (existing board state is lost)",
                    ),
                )
            elif not tables and not views and not migration_rows:
                remediations = (
                    Remediation(
                        "doctor.schema.database_empty",
                        "the database file exists but the schema was never applied",
                        "run any writing `coord` subcommand (for example "
                        "`coord demo --quiet`, or `coord session start`) to apply "
                        "the schema and migrations, then re-run `coord doctor`",
                    ),
                )
            else:
                remediations = (
                    Remediation(
                        "doctor.schema.migration_drift",
                        "the database is missing tables, views or migrations that "
                        "this installed version expects",
                        "run any writing `coord` subcommand to apply pending "
                        "migrations; if the checksum of an already-applied "
                        "migration differs, the database was created by a "
                        "different coordharness version than is installed now",
                    ),
                )
        code, remediation = _prioritized(remediations)
        return (
            _finding(
                "doctor.schema",
                blocked,
                "schema and migration inventory is current"
                if not blocked
                else "schema or migration integrity could not be proven",
                code=code or "doctor.schema.ok",
                remediation=remediation,
                remediations=remediations,
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
        remediations = (
            Remediation(
                "doctor.schema.unreadable",
                "the database could not be opened for a read-only integrity check",
                "confirm no other process holds an exclusive lock on the file and "
                "that it is a valid SQLite database",
            ),
        )
        code, remediation = _prioritized(remediations)
        return (
            _finding(
                "doctor.schema",
                True,
                "schema or migration integrity could not be read",
                code=code,
                remediation=remediation,
                remediations=remediations,
                database_present=True,
            ),
            None,
        )


def _check_writers(package_root: Path) -> DoctorFinding:
    sites, parse_errors = inventory_lifecycle_writers(package_root)
    unexpected = unexpected_writer_modules(sites, allowed_modules=_ALLOWED_WRITER_MODULES)
    modules = sorted({site.module for site in sites})
    blocked = bool(parse_errors or unexpected)
    remediations: tuple[Remediation, ...] = ()
    if parse_errors:
        remediations += (
            Remediation(
                "doctor.lifecycle_writers.parse_error",
                "a source file could not be parsed while inventorying lifecycle writers",
                "fix the syntax error(s) named in `parse_errors` so the writer "
                "inventory can be scanned again",
            ),
        )
    if unexpected:
        remediations += (
            Remediation(
                "doctor.lifecycle_writers.unexpected_module",
                "a module outside the declared writer allowlist mutates "
                "coordination state directly",
                "route the write through an already-allowed module, or add the "
                "module to `_ALLOWED_WRITER_MODULES` in "
                "src/coordharness/safety/doctor.py after review",
            ),
        )
    code, remediation = _prioritized(remediations)
    return _finding(
        "doctor.lifecycle_writers",
        blocked,
        "lifecycle writer inventory is confined to declared modules"
        if not blocked
        else "lifecycle writer inventory contains unclassified modules",
        code=code or "doctor.lifecycle_writers.ok",
        remediation=remediation,
        remediations=remediations,
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
            code="doctor.leases_reviews.unavailable",
            remediation="fix the `doctor.schema` finding first; lease and review "
            "checks need a readable current schema",
            check_available=False,
        )
    try:
        claim_marks = ",".join("?" for _ in _HELD_CLAIMS)
        # A one-shot CLI process (every `coord claim`/`coord done` invocation)
        # exits the moment the command finishes while the lease it acquired
        # stays valid until `expires_at` -- that is the normal, expected state
        # between two commands from the same session, not staleness. `pid` is
        # therefore only evidence once the lease has ALSO expired, matching
        # the one existing place this harness already reasons about process
        # liveness: `reap_zombie_sessions` gates its own dead-pid signal on
        # `lease_until` having passed, and never reaps on a dead pid alone.
        # The join also carries pid/pid_started_at so the confirmed-dead
        # subset below can be told apart from an expired lease with no
        # process evidence either way.
        expired_claims = conn.execute(
            f"SELECT c.claim_id AS claim_id, c.work_id AS work_id,"
            f" s.pid AS pid, s.pid_started_at AS pid_started_at"
            f" FROM claims c JOIN agent_sessions s ON s.session_id=c.session_id"
            f" WHERE c.status IN ({claim_marks}) AND c.expires_at<=?",
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
        # The confirmed-dead subset of the already-expired claims above:
        # `pid_matches` also verifies the recorded start time when one was
        # captured, so a reused pid is not mistaken for the original holder.
        # The remainder of `expired_claims` (pid unknown, or a pid that still
        # answers) is reported too, just without the stronger "the process is
        # gone" claim.
        dead_process_claims = [
            row
            for row in expired_claims
            if row["pid"] is not None and not pid_matches(row["pid"], row["pid_started_at"])
        ]
    except sqlite3.Error:
        return _finding(
            "doctor.leases_reviews",
            True,
            "lease or review query failed closed",
            code="doctor.leases_reviews.query_failed",
            remediation="re-run `coord doctor`; if this persists, the database "
            "file may be corrupt (see `doctor.schema`)",
            check_available=False,
        )

    blocked = bool(
        expired_claims
        or expired_sessions
        or orphan_running
        or unresolved_reviews
        or dead_process_claims
    )
    dead_claim_ids = {row["claim_id"] for row in dead_process_claims}
    uncertain_expired_claims = [
        row for row in expired_claims if row["claim_id"] not in dead_claim_ids
    ]
    remediations: tuple[Remediation, ...] = ()
    if dead_process_claims:
        remediations += (
            Remediation(
                "doctor.leases_reviews.dead_process_claim",
                "a held claim's lease has expired and its owning session's "
                "process is confirmed no longer running",
                "run `coord release <claim_id> --status released --reason "
                '"owning process is gone"` to free the row now',
            ),
        )
    if uncertain_expired_claims:
        remediations += (
            Remediation(
                "doctor.leases_reviews.expired_claim",
                "a held claim's lease has already expired without being "
                "renewed or released",
                "run `coord heartbeat-claim <claim_id>` if the work is still "
                "active, or `coord release <claim_id> --status released` to "
                "free it",
            ),
        )
    if orphan_running:
        remediations += (
            Remediation(
                "doctor.leases_reviews.orphan_running",
                "a work item's intent_state is running but no claim currently "
                "holds it",
                "run `coord claim <work_id>` to resume it under a real claim, "
                "or correct its intent_state if nothing is working it",
            ),
        )
    if unresolved_reviews:
        remediations += (
            Remediation(
                "doctor.leases_reviews.unresolved_review",
                "a work item reached a terminal state with an outstanding "
                "audit request and no verdict recorded after it",
                "have the counterpart lane record a verdict, or record an "
                "explicit `coord sign-off` if a human is overriding the gate",
            ),
        )
    if expired_sessions:
        remediations += (
            Remediation(
                "doctor.leases_reviews.expired_session",
                "an agent session is marked active but its lease has expired "
                "without a heartbeat",
                "end the session with `coord session end` if it is still "
                "reachable, or let the reaper reclaim it on its next pass",
            ),
        )
    code, remediation = _prioritized(remediations)
    return _finding(
        "doctor.leases_reviews",
        blocked,
        "leases and terminal review requests are coherent"
        if not blocked
        else "lease or terminal review debt is present",
        code=code or "doctor.leases_reviews.ok",
        remediation=remediation,
        remediations=remediations,
        expired_claim_count=len(expired_claims),
        expired_claim_work_ids=_sample_ids(expired_claims),
        expired_session_count=len(expired_sessions),
        expired_session_ids=_sample_ids(expired_sessions, "session_id"),
        orphan_running_count=len(orphan_running),
        orphan_running_work_ids=_sample_ids(orphan_running),
        unresolved_terminal_review_count=len(unresolved_reviews),
        unresolved_terminal_review_work_ids=_sample_ids(unresolved_reviews),
        dead_process_claim_count=len(dead_process_claims),
        dead_process_claim_work_ids=_sample_ids(dead_process_claims),
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


_JOB_PROJECTION_GUIDANCE: dict[str, tuple[str, str]] = {
    "unsafe_job_progress_root": (
        "the job telemetry directory is a symlink or not a plain directory",
        "remove the entry at `job_progress/` under the state root and let a "
        "job launcher recreate it as a plain directory",
    ),
    "job_progress_unreadable": (
        "the job telemetry directory could not be listed",
        "check filesystem permissions on `job_progress/` under the state root",
    ),
    "sidecar_symlink": (
        "a job telemetry sidecar file is a symlink instead of a plain file",
        "delete the symlinked sidecar under `job_progress/` and let the job "
        "launcher rewrite it",
    ),
    "sidecar_invalid_json": (
        "a job telemetry sidecar file is not valid JSON",
        "delete the corrupt sidecar under `job_progress/`; a live job "
        "rewrites it, or remove it if the job is gone",
    ),
    "sidecar_identity_mismatch": (
        "a job telemetry sidecar's job_id does not match its filename",
        "delete the mismatched sidecar under `job_progress/` so a rerun can "
        "regenerate it correctly",
    ),
    "sidecar_work_binding_missing": (
        "a job telemetry sidecar does not name the work row it belongs to",
        "delete the unbound sidecar under `job_progress/`, or re-launch the "
        "job so it writes `roadmap_id`",
    ),
    "sidecar_work_binding_unknown": (
        "a job telemetry sidecar names a work row that does not exist in "
        "this database",
        "delete the stale sidecar under `job_progress/`, or confirm `--db` "
        "points at the database this job was launched against",
    ),
    "projection_database_unavailable": (
        "the projection views could not be checked because the database is "
        "unreadable",
        "fix the `doctor.schema` finding first",
    ),
    "projection_view_unreadable": (
        "a projection view query failed",
        "fix the `doctor.schema` finding first; the view may be missing a "
        "migration",
    ),
    "live_run_sidecar_missing": (
        "a run recorded as live in the database names no sidecar file",
        "stop the run and re-launch it so a sidecar is written, or mark the "
        "run terminal",
    ),
    "live_run_sidecar_unsafe": (
        "a run recorded as live points at a sidecar outside `job_progress/`, "
        "or at a file that is not a plain readable JSON object",
        "correct or clear the run's `sidecar_path`, or mark the run terminal",
    ),
    "live_run_projection_unreadable": (
        "the `runs` table could not be queried for live rows",
        "fix the `doctor.schema` finding first",
    ),
}


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
        remediations = (
            Remediation(
                "doctor.jobs_projection.unsafe_root",
                *_JOB_PROJECTION_GUIDANCE["unsafe_job_progress_root"],
            ),
        )
        code, remediation = _prioritized(remediations)
        return _finding(
            "doctor.jobs_projection",
            True,
            "job telemetry root is not safely contained",
            code=code,
            remediation=remediation,
            remediations=remediations,
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
    remediations = tuple(
        Remediation(f"doctor.jobs_projection.{code}", *_JOB_PROJECTION_GUIDANCE[code])
        for code in unique
        if code in _JOB_PROJECTION_GUIDANCE
    )
    finding_code, finding_remediation = _prioritized(remediations)
    return _finding(
        "doctor.jobs_projection",
        bool(unique),
        "job sidecars and projection views are coherent"
        if not unique
        else "job sidecar or projection integrity could not be proven",
        code=finding_code or "doctor.jobs_projection.ok",
        remediation=finding_remediation,
        remediations=remediations,
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
            code="doctor.public_paths.unavailable",
            remediation="fix the `doctor.schema` finding first; pointer checks "
            "need a readable current schema",
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
            code="doctor.public_paths.query_failed",
            remediation="re-run `coord doctor`; if this persists, the database "
            "file may be corrupt (see `doctor.schema`)",
            check_available=False,
        )
    invalid = sorted(set(invalid))
    pending = sorted(set(pending))
    absolute = sorted(set(absolute))
    remediations: tuple[Remediation, ...] = ()
    if invalid:
        remediations = (
            Remediation(
                "doctor.public_paths.invalid_pointer",
                "a stored path either escapes the project root, or (for a "
                "terminal row) does not exist",
                "fix the field named in `invalid_pointer_fields` to a real, "
                "project-relative path, or move the row out of a terminal "
                "state until the proof it names actually exists",
            ),
        )
    code, remediation = _prioritized(remediations)
    return _finding(
        "doctor.public_paths",
        bool(invalid),
        "all local pointers are contained, and every produced pointer exists"
        if not invalid
        else "one or more local pointers lack existence or containment proof",
        code=code or "doctor.public_paths.ok",
        remediation=remediation,
        remediations=remediations,
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


def _classify_mcp_command(command: str, *, project_root: Path) -> str | None:
    """Whether ``command`` can plausibly be launched, without launching it.

    A bare name (no path separator) is resolved on ``PATH``, matching how the
    parent process (Claude Code, Codex, ...) will look it up. Anything else is
    treated as a path -- relative paths resolve against the project root,
    exactly like ``.mcp.json``'s checked-in ``./scripts/coord-mcp-launch.sh``
    -- and containment is proven the same way every other stored path in this
    module proves it, so a config cannot use this check to probe outside the
    project root.
    """

    text = command.strip()
    if not text:
        return None
    if "/" not in text and "\\" not in text:
        return None if shutil.which(text) else "mcp.command_not_found"
    try:
        resolved = resolve_under_root(
            text, project_root, must_exist=False, allow_absolute=True, allow_root=False
        )
    except PathSafetyError:
        return "mcp.command_not_found"
    if not resolved.exists() or resolved.is_dir():
        return "mcp.command_not_found"
    if not os.access(resolved, os.X_OK):
        return "mcp.command_not_executable"
    return None


_MCP_GUIDANCE: dict[str, tuple[str, str]] = {
    "mcp.shell_command": (
        "an MCP server launches a shell with -c, letting argument text become code",
        "invoke the target script or interpreter directly instead of wrapping "
        "it in `sh -c \"...\"` / `bash -c \"...\"`",
    ),
    "mcp.unpinned_package": (
        "an MCP server launches an npx package pinned to @latest, so the "
        "version launched is not reproducible",
        "pin the package to an exact version (for example `package@1.2.3`) "
        "instead of `@latest`",
    ),
    "mcp.literal_secret": (
        "an MCP server configuration stores a secret-looking value directly "
        "instead of an environment reference",
        "move the value into an environment variable and reference it as "
        "`$VAR` (or `${VAR}`) instead of a literal in the config file",
    ),
    "mcp.untrusted_config_path": (
        "an MCP configuration path is outside the trusted project or state root",
        "point `--mcp-config` at a file inside the project root or the state root",
    ),
    "mcp.invalid_config": (
        "an MCP configuration file could not be parsed",
        "fix the JSON/TOML syntax in the MCP configuration file",
    ),
    "mcp.command_not_found": (
        "an MCP server's command could not be located",
        "if it names a local script (like ./scripts/coord-mcp-launch.sh), "
        "confirm the path is correct and run ./scripts/setup.sh; if it names "
        "a program expected on PATH, install it or correct the command",
    ),
    "mcp.command_not_executable": (
        "an MCP server's command exists but lacks the executable bit",
        "run `chmod +x` on the script the MCP configuration points at, then "
        "re-run `coord doctor`",
    ),
}


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
    for record in records:
        command_problem = _classify_mcp_command(record.command, project_root=project_root)
        if command_problem is not None:
            problem_codes.append(command_problem)
    unique = sorted(set(problem_codes))
    remediations = tuple(
        Remediation(code, *_MCP_GUIDANCE[code]) for code in unique if code in _MCP_GUIDANCE
    )
    finding_code, finding_remediation = _prioritized(remediations)
    return _finding(
        "doctor.mcp_security",
        bool(unique),
        "MCP configuration inventory contains no unsafe literals or launch patterns"
        if not unique
        else "MCP configuration security could not be proven",
        code=finding_code or "doctor.mcp_security.ok",
        remediation=finding_remediation,
        remediations=remediations,
        config_count=len(paths),
        server_count=len(records),
        problem_codes=unique,
        issues=[issue.as_dict() for issue in issues],
        inventory=redacted_inventory(records),
        values_redacted=True,
    )


def _configured_board_port() -> int:
    """The port `coord-board` would bind, read the same way it reads it:
    `--port` is a CLI flag this read-only check has no access to, so this
    mirrors only the fallback chain `coord-board` itself applies when the
    flag is absent -- `COORD_BOARD_PORT`, then the packaged default.
    """
    return int(os.environ.get("COORD_BOARD_PORT", _DEFAULT_BOARD_PORT))


def _board_answers_on(port: int) -> bool:
    """Whether the process holding `port` identifies itself as `coord-board`.

    The bind probe alone cannot distinguish our own running board from a
    foreign squatter, and those two states need opposite verdicts. The
    board's `/healthz` names the service, so one short loopback request
    settles it; anything that does not answer that way is treated as
    foreign, which keeps the check fail-closed toward reporting a conflict.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=0.5
        ) as response:
            if response.status != HTTPStatus.OK:
                return False
            payload = json.loads(response.read(512).decode("utf-8", "replace"))
    except Exception:
        return False
    return isinstance(payload, dict) and payload.get("service") == "coord-board"


def _check_db_file_modes(db_path: Path) -> DoctorFinding:
    """Whether the database and its sidecars actually hold their intended mode.

    The trust boundary for this control plane is the filesystem, so the mode on
    these files IS the boundary. Enforcement elsewhere is best effort by design
    -- a database owned by another account cannot be tightened by this process,
    and refusing to open it would be the worse outcome -- so the guarantee only
    exists if something reads the result back afterwards. That is this check.

    It only ever stats: this module is read-only, and a health check that
    quietly repaired the thing it was asked to report on could never tell you
    the mode had been wrong.
    """
    from ..coord.config import COORD_DB_FILE_MODE, DB_SIDECAR_SUFFIXES

    observed: dict[str, str] = {}
    widened: dict[str, str] = {}
    for suffix in ("", *DB_SIDECAR_SUFFIXES):
        target = Path(str(db_path) + suffix)
        name = suffix or "db"
        try:
            info = target.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            observed[name] = "not-a-regular-file"
            widened[name] = observed[name]
            continue
        mode = stat.S_IMODE(info.st_mode)
        observed[name] = oct(mode)
        if mode & 0o077:
            widened[name] = observed[name]
    remediations: tuple[Remediation, ...] = ()
    if widened:
        names = ", ".join(
            f"{'the database' if name == 'db' else name} is {mode}"
            for name, mode in sorted(widened.items())
        )
        remediations = (
            Remediation(
                "doctor.db_file_modes.readable_by_others",
                f"{names}; these carry the coordination record",
                f"restrict them with `chmod {oct(COORD_DB_FILE_MODE)[2:]} "
                f"{db_path}` (SQLite recreates its sidecars from the database "
                "file's own mode, so tightening the database is usually "
                "enough), or move the database somewhere this account owns",
            ),
        )
    code, remediation = _prioritized(remediations)
    return _finding(
        "doctor.db_file_modes",
        bool(widened),
        "database and sidecar files are not readable by other accounts"
        if not widened
        else "database or sidecar files are readable by other accounts",
        code=code or "doctor.db_file_modes.ok",
        remediation=remediation,
        remediations=remediations,
        modes=observed,
    )


def _check_board_port() -> DoctorFinding:
    """A cheap loopback bind probe for the configured `coord-board` port.

    `coord-board` fails at startup with an opaque `OSError: [Errno 48]
    Address already in use` when something else already holds its port --
    surfacing that here, before the board is ever started, gives the reader
    the same fix `coord-board`'s own `_address_in_use_message` prints. Any
    bind failure other than address-in-use (a permission-denied low port, no
    loopback route, and so on) is not evidence of a port conflict and must
    not be reported as one -- the probe fails open on everything except the
    one errno this check exists to name.
    """
    port = _configured_board_port()
    bound = False
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
    except OSError as exc:
        bound = exc.errno == errno.EADDRINUSE
    # A bound port is only a fault when a FOREIGN process holds it. The
    # ordinary healthy state of a working installation is `coord-board`
    # already running, and reporting that as a problem would make the
    # doctor fail precisely when the product is working.
    conflict = bound and not _board_answers_on(port)
    remediations: tuple[Remediation, ...] = ()
    if conflict:
        remediations = (
            Remediation(
                "doctor.board_port.address_in_use",
                f"port {port} is already bound on the loopback interface",
                "another process is already listening on this port; find it "
                f"with `lsof -i :{port}`, then either stop it or point "
                f"`coord-board` at a free one with `--port <port>` or by "
                f"setting `COORD_BOARD_PORT=<port>`",
            ),
        )
    code, remediation = _prioritized(remediations)
    return _finding(
        "doctor.board_port",
        conflict,
        "configured board port is free on the loopback interface"
        if not conflict
        else "configured board port is already bound on the loopback interface",
        code=code or "doctor.board_port.ok",
        remediation=remediation,
        remediations=remediations,
        port=port,
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
        remediations = (
            Remediation(
                "doctor.roots.missing_directory",
                "the project root or the state root does not exist as a directory",
                "create the project directory and its state root (the state "
                "root defaults to `.coordharness/` under the project root), "
                "then re-run `coord doctor`",
            ),
        )
        code, remediation = _prioritized(remediations)
        finding = _finding(
            "doctor.roots",
            True,
            "project and state roots must already exist as directories",
            code=code,
            remediation=remediation,
            remediations=remediations,
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
        if outside:
            remediations = (
                Remediation(
                    "doctor.schema.outside_state_root",
                    "the database exists but sits outside the state root doctor "
                    "was given",
                    "pass a matching `--state-root`, or keep the database at "
                    "the default `.coordharness/coord.db` under the project root",
                ),
            )
        else:
            remediations = (
                Remediation(
                    "doctor.schema.database_missing",
                    "no database file was found at the resolved --db path",
                    "create it with any writing `coord` subcommand (for example "
                    "`coord demo --quiet`, or `coord session start`), which "
                    "bootstraps `.coordharness/coord.db` automatically, then "
                    "re-run `coord doctor`",
                ),
            )
        code, remediation = _prioritized(remediations)
        schema_finding = _finding(
            "doctor.schema",
            True,
            "coordination database is outside the trusted state root"
            if outside
            else "coordination database is missing or is not a regular direct file",
            code=code,
            remediation=remediation,
            remediations=remediations,
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
        _check_board_port(),
        _check_db_file_modes(supplied_db),
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
