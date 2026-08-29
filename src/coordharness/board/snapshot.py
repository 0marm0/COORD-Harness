from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterator

from coordharness import config
from coordharness.coord import coord_db
from coordharness.coord.config import connect_ro
from coordharness.jobs.sidecar_snapshot import load_snapshot
from coordharness.jobs.status import derive_status, done_signal_exists, parse_updated_at

NATIVE_SNAPSHOT_SCHEMA = "1"
_SCHEMA_RESOURCE = "native_snapshot_v1.schema.json"
_DEFAULT_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024 * 1024
_RUNNING = {"claimed", "running"}
_ATTENTION = {"attention", "blocked", "failed", "needs_verification", "artifact_present"}
# The words a sidecar uses to call itself finished. Kept identical to the set
# `jobs.status.derive_status` matches on, because the two have to agree about
# which claims are terminal for the verification to be reached at all.
_DONE_CLAIMS = {"done", "complete", "completed", "finished", "success", "superseded"}
_DONE = {
    "archived",
    "canceled",
    "cancelled",
    "closed",
    "complete",
    "completed",
    "done",
    "skipped",
    "superseded",
    "success",
}


def _file_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return the identity and content-changing metadata of one SQLite file."""
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _snapshot_max_bytes() -> int:
    raw = os.environ.get("COORD_BOARD_SNAPSHOT_MAX_BYTES")
    if raw is None:
        return _DEFAULT_SNAPSHOT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("COORD_BOARD_SNAPSHOT_MAX_BYTES must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError("COORD_BOARD_SNAPSHOT_MAX_BYTES must be a positive integer")
    return value


@contextmanager
def stable_copy(db: Path | str) -> Iterator[Path]:
    """Yield a coherent private SQLite snapshot of `db`.

    Two callers want this. Each builder wants it so SQLite never writes beside
    the source database. The server wants it once around all four builders: the
    documents are swapped together under one lock, but a lock cannot make four
    separate reads agree, and four builders each copying for themselves read the
    database at four different instants. Pointing all four at one copy is what
    actually makes the set coherent.

    A normal ``mode=ro`` SQLite connection is not side-effect free for a WAL
    database: SQLite may create ``-shm`` beside the source. ``immutable=1`` and
    ``nolock=1`` avoid that write, but immutable mode deliberately ignores WAL
    and no-lock mode cannot safely open one. Therefore the source files are
    copied without opening SQLite and the private copy is opened later.

    The stamps bracket both copies. A checkpoint, WAL reset, append, removal,
    or database replacement changes one of them and forces a retry. If a writer
    remains continuously active we fail closed instead of serving a potentially
    incoherent combination or silently dropping committed WAL rows. The logical
    copy size is capped (4 GiB by default, configurable) and checked against
    available temporary storage before any bytes are copied. The temporary
    directory owns every partial attempt, so success, failure, and exceptions
    all remove it.
    """
    source = Path(db)
    if not source.is_file():
        raise FileNotFoundError(f"coord.db does not exist: {source}")
    wal = Path(f"{source}-wal")
    with tempfile.TemporaryDirectory(prefix="coord-board-") as directory:
        copied_db = Path(directory) / "coord.db"
        copied_wal = Path(f"{copied_db}-wal")
        for _attempt in range(8):
            before = (_file_stamp(source), _file_stamp(wal))
            required_bytes = sum(stamp[2] for stamp in before if stamp is not None)
            maximum_bytes = _snapshot_max_bytes()
            if required_bytes > maximum_bytes:
                raise RuntimeError(
                    "coord.db read-only snapshot exceeds "
                    f"COORD_BOARD_SNAPSHOT_MAX_BYTES ({required_bytes} > {maximum_bytes})"
                )
            free_bytes = shutil.disk_usage(directory).free
            if required_bytes > free_bytes:
                raise RuntimeError(
                    "insufficient temporary storage for coord.db read-only snapshot "
                    f"({required_bytes} required, {free_bytes} available)"
                )
            shutil.copyfile(source, copied_db)
            if before[1] is None:
                copied_wal.unlink(missing_ok=True)
            else:
                try:
                    shutil.copyfile(wal, copied_wal)
                except FileNotFoundError:
                    copied_wal.unlink(missing_ok=True)
                    continue
            after = (_file_stamp(source), _file_stamp(wal))
            if before == after:
                break
        else:
            raise RuntimeError("coord.db changed during read-only board materialization")
        yield copied_db


@contextmanager
def _materialized_connection(db: Path):
    """Read a stable private copy so SQLite never writes beside the source DB."""
    with stable_copy(db) as copied_db:
        conn = connect_ro(copied_db)
        try:
            yield conn
        finally:
            conn.close()


def load_schema() -> dict[str, Any]:
    resource = files("coordharness.board").joinpath(_SCHEMA_RESOURCE)
    return json.loads(resource.read_text(encoding="utf-8"))


def _string(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _progress(job: dict[str, Any]) -> float | None:
    value = job.get("pct")
    if value is not None and not isinstance(value, bool):
        try:
            return max(0.0, min(1.0, float(value) / 100.0))
        except (TypeError, ValueError, OverflowError):
            pass
    done = job.get("done")
    total = job.get("total")
    if not isinstance(done, bool) and not isinstance(total, bool):
        try:
            total_value = float(total)
            if total_value > 0:
                return max(0.0, min(1.0, float(done) / total_value))
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _eta_seconds(job: dict[str, Any]) -> int | None:
    for key, multiplier in (("eta_s", 1), ("eta_seconds", 1), ("eta_min", 60)):
        value = job.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return max(0, int(float(value) * multiplier))
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _job_stale(job: dict[str, Any], now: float) -> bool:
    if _string(job.get("state")).lower() != "running":
        return False
    # A sidecar may carry either stamp: sidecar_snapshot reads updated_at and
    # falls back to last_progress_at, and a reader that skips the fallback
    # flags a job as unreported when its own writer did report.
    updated_at = parse_updated_at(job.get("updated_at"))
    if updated_at is None:
        updated_at = parse_updated_at(job.get("last_progress_at"))
    return updated_at is None or now - updated_at > 120.0


def _work_row(row: dict[str, Any]) -> dict[str, Any]:
    work_id = _string(row.get("work_id"))
    return {
        "id": work_id,
        "title": _string(row.get("display") or row.get("title"), work_id),
        "status": _string(row.get("status") or row.get("intent_state"), "planned").lower(),
        "bucket": _string(row.get("surface"), "task"),
        "owner": _string(row.get("assignee") or row.get("owner_session_actor")),
        "module": _string(row.get("module")),
        "group": _string(row.get("group") or row.get("module") or row.get("lane")),
        "priority": max(0, _integer(row.get("priority"))),
        "progress_fraction": None,
        "eta_seconds": None,
        "stale": False,
        "current_step": _string(row.get("claim_step") or row.get("next_step") or row.get("resume_when")),
    }


def _verified_job_state(
    job: dict[str, Any], state: str, declared_signal: str, root: Path
) -> tuple[str, str]:
    """Answer the job's own `done` claim with the artifact, not with the claim.

    `docs/jobs-and-runs.md` says neither surface trusts the state string a job
    last wrote about itself, and names `derive_status` as the sidecar half of
    that discipline. The board was not calling it: a sidecar carrying
    `"state": "done"` became a done row, and a done row in the summary, with no
    artifact ever consulted. So call it, and let its `unverified` verdict --
    a done claim whose declared proof does not resolve -- land on the row as
    `needs_verification`, which the board already reads as needing attention
    rather than as finished.

    Returns the row status and a step note, empty when there is nothing to add.
    A job that declares no proof at all is left alone: there is no artifact to
    check, and inventing a refusal for it would say more than the evidence
    does.
    """
    if state not in _DONE_CLAIMS:
        return state, ""
    item = {**job, "id": _string(job.get("job_id") or job.get("roadmap_id")), "status": state}
    if declared_signal:
        item["done_signal"] = declared_signal
    try:
        evidence = derive_status(item, root, ps_text="")
        # Its `unverified` verdict, plus the artifact fact it always computes.
        # derive_status answers several questions and returns on the first one
        # that fires, so a sidecar carrying a blocking `rubric_verdict` would
        # otherwise return before the done branch and carry its done claim
        # through unchecked. A declared proof that does not resolve is not
        # verified, whatever else the sidecar says about itself.
        unverified = bool(evidence.unverified) or (
            bool(declared_signal) and not evidence.done_signal_exists
        )
    except ValueError:
        # derive_status also loads the optional process-pattern configuration,
        # and a malformed one raises. That question cannot change this answer
        # -- with no ps output to match against, the pattern branch is
        # unreachable here -- so fall back to the artifact alone rather than
        # letting an unrelated misconfiguration take the board down.
        unverified = bool(declared_signal) and not done_signal_exists(declared_signal, root)
    if not unverified:
        return state, ""
    return "needs_verification", "done reported, declared proof artifact not found"


def _apply_job(
    row: dict[str, Any],
    job: dict[str, Any],
    now: float,
    *,
    declared_signal: str = "",
    root: Path | None = None,
) -> None:
    state = _string(job.get("state")).lower()
    note = ""
    if state:
        state, note = _verified_job_state(
            job, state, declared_signal, root if root is not None else config.project_root()
        )
        row["status"] = state
    row["bucket"] = "job"
    row["owner"] = _string(job.get("owner"), row["owner"])
    row["progress_fraction"] = _progress(job)
    row["eta_seconds"] = _eta_seconds(job)
    row["stale"] = _job_stale(job, now)
    row["current_step"] = note or _string(job.get("step"), row["current_step"])


def _session_row(row: dict[str, Any]) -> dict[str, Any]:
    session_id = _string(row.get("session_id"))
    return {
        "id": session_id,
        "actor": _string(row.get("actor")),
        "label": _string(row.get("human_label") or row.get("runner_type"), session_id),
        "live": bool(row.get("live")),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"running": 0, "attention": 0, "next": 0, "done": 0}
    for row in rows:
        status = _string(row.get("status"), "planned").lower()
        if status in _RUNNING:
            counts["running"] += 1
        elif status in _ATTENTION:
            counts["attention"] += 1
        elif status in _DONE:
            counts["done"] += 1
        else:
            counts["next"] += 1
    counts["total"] = len(rows)
    return counts


def _iso8601(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_snapshot(
    db_path: str | Path | None = None,
    *,
    activity_limit: int = 100,
    job_progress_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build the native snapshot using read-only database and filesystem access.

    activity_limit remains accepted for source compatibility with the early
    board prototype. NativeSnapshotV1 intentionally exposes no event bodies.

    `job_progress_dir` names the telemetry root explicitly. Left unset it is
    derived from the database being served, so one screen is never assembled
    from two unrelated directories. A caller that has already materialized a
    private copy of the database must pass the original's telemetry root, since
    the copy carries none: see `board.server.build_documents`.
    """
    del activity_limit
    db = Path(db_path) if db_path is not None else config.coord_db_path()
    sidecars = (
        Path(job_progress_dir)
        if job_progress_dir is not None
        else config.job_progress_dir_for_database(db)
    )
    with _materialized_connection(db) as conn:
        now = config.source_date_epoch(coord_db.db_now(conn))
        work = sorted(
            (dict(row) for row in coord_db.board_rows(conn, at=now)),
            key=lambda row: _string(row.get("work_id")),
        )
        sessions = [
            _session_row(dict(row))
            for row in sorted(
                coord_db.session_rollup(conn, at=now),
                key=lambda row: _string(row.get("session_id")),
            )
        ]
    rows_by_id = {
        row["id"]: row
        for row in (_work_row(item) for item in work)
        if row["id"]
    }
    # Kept off the rendered rows on purpose -- the public snapshot carries no
    # artifact paths -- but a job bound to a work item inherits that item's
    # declared proof, which is usually the only proof anyone declared. Without
    # this the verification below has nothing to check for most real jobs.
    declared_signals = {
        _string(item.get("work_id")): _string(item.get("done_signal"))
        for item in work
        if _string(item.get("work_id"))
    }
    artifact_root = config.project_root()
    jobs = sorted(
        (dict(item) for item in load_snapshot(sidecars).items),
        key=lambda item: (_string(item.get("roadmap_id")), _string(item.get("job_id"))),
    )
    for job in jobs:
        roadmap_id = _string(job.get("roadmap_id"))
        job_id = _string(job.get("job_id"))
        run_id = _string(job.get("run_id"))
        job_identity = run_id or job_id
        if not job_identity:
            continue
        row_id = f"job:{job_identity}"
        if row_id in rows_by_id:
            row_id = f"{row_id}:{job_id or roadmap_id}"
        work_row = rows_by_id.get(roadmap_id)
        row = {
            "id": row_id,
            "title": _string(
                job.get("title"),
                _string(work_row.get("title") if work_row else None, job_id or roadmap_id),
            ),
            "status": _string(job.get("state"), "planned").lower(),
            "bucket": "job",
            # A local job is held by the runner and driven by an agent. Falling
            # back to an empty owner dropped both facts, so every job arrived on
            # the public snapshot unattributed -- it could be seen running but
            # not placed against a lane or a vertical.
            "owner": _string(
                job.get("owner"),
                _string(
                    job.get("driver"),
                    work_row["owner"] if work_row else "local",
                ),
            ),
            # The vertical, not the hardware. A job's resource class is a
            # different axis -- putting "gpu" in the same column space as "ml"
            # and "ui" reads as a subject area the board does not have.
            "module": _string(
                job.get("module"),
                work_row["module"] if work_row else "local",
            ),
            "group": _string(
                job.get("group") or job.get("module"),
                work_row["group"] if work_row else "",
            ),
            "priority": max(
                0,
                _integer(
                    job.get("priority"),
                    work_row["priority"] if work_row else 0,
                ),
            ),
            "progress_fraction": None,
            "eta_seconds": None,
            "stale": False,
            "current_step": "",
        }
        rows_by_id[row_id] = row
        _apply_job(
            row,
            job,
            now,
            declared_signal=_string(job.get("done_signal"))
            or declared_signals.get(roadmap_id, ""),
            root=artifact_root,
        )

    rows = [rows_by_id[key] for key in sorted(rows_by_id)]
    snapshot: dict[str, Any] = {
        "schema_version": NATIVE_SNAPSHOT_SCHEMA,
        "generated_at": _iso8601(now),
        "source": "coord.db+job_progress" if jobs else "coord.db",
        "stale": False,
        "summary": _summary(rows),
        "rows": rows,
        "sessions": sessions,
    }
    validate_snapshot(snapshot)
    return snapshot


def _json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = [part.strip() for part in value.split(",")]
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]
    return sorted({_string(item) for item in parsed if _string(item)})


def build_graph(
    db_path: str | Path | None = None,
    *,
    job_progress_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build a safe source-bound graph separate from NativeSnapshotV1.

    Telemetry is bound to the served database exactly as in `build_snapshot`;
    the graph draws job nodes from the same sidecars the snapshot counts.
    """
    db = Path(db_path) if db_path is not None else config.coord_db_path()
    sidecars = (
        Path(job_progress_dir)
        if job_progress_dir is not None
        else config.job_progress_dir_for_database(db)
    )
    with _materialized_connection(db) as conn:
        now = config.source_date_epoch(coord_db.db_now(conn))
        work = sorted(
            (dict(row) for row in coord_db.board_rows(conn, at=now)),
            key=lambda row: _string(row.get("work_id")),
        )
        artifacts = [
            dict(row)
            for row in conn.execute(
                "SELECT artifact_id,work_id,kind FROM artifacts ORDER BY artifact_id"
            ).fetchall()
        ]
    jobs = sorted(
        (dict(item) for item in load_snapshot(sidecars).items),
        key=lambda item: (_string(item.get("roadmap_id")), _string(item.get("job_id"))),
    )
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def add_node(
        node_id: str,
        kind: str,
        label: str,
        *,
        status: str = "",
        missing: bool = False,
    ) -> None:
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": kind,
                "label": label,
                "status": status,
                "missing": missing,
            },
        )

    def add_edge(
        source: str,
        target: str,
        kind: str,
        source_field: str,
        relationship_state: str,
    ) -> None:
        edge_id = f"{kind}:{source}:{target}"
        edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "kind": kind,
            "source_field": source_field,
            "relationship_state": relationship_state,
        }

    known_work = {_string(row.get("work_id")) for row in work}
    for row in work:
        work_id = _string(row.get("work_id"))
        source = f"work:{work_id}"
        add_node(
            source,
            "work",
            _string(row.get("display") or row.get("title"), work_id),
            status=_string(row.get("status")),
        )
        relationships = [
            ("parent", _string(row.get("parent_id")), "work_items.parent_id"),
            *[
                ("depends_on", dependency, "work_items.depends_on")
                for dependency in _json_list(row.get("depends_on"))
            ],
        ]
        for kind, related_id, source_field in relationships:
            if not related_id:
                continue
            target = f"work:{related_id}"
            state = "source_bound" if related_id in known_work else "missing_target"
            if state == "missing_target":
                add_node(
                    target,
                    "missing_work",
                    f"Missing work: {related_id}",
                    missing=True,
                )
            add_edge(source, target, kind, source_field, state)

    for artifact in artifacts:
        artifact_id = _string(artifact.get("artifact_id"))
        if not artifact_id:
            continue
        target = f"artifact:{artifact_id}"
        add_node(target, "artifact", _string(artifact.get("kind"), artifact_id))
        work_id = _string(artifact.get("work_id"))
        if work_id:
            source = f"work:{work_id}"
            state = "source_bound" if work_id in known_work else "missing_target"
            if state == "missing_target":
                add_node(source, "missing_work", f"Missing work: {work_id}", missing=True)
            add_edge(source, target, "evidence", "artifacts.work_id", state)

    for job in jobs:
        job_id = _string(job.get("job_id"))
        run_id = _string(job.get("run_id"))
        identity = run_id or job_id
        if not identity:
            continue
        target = f"job:{identity}"
        add_node(target, "job", job_id or identity, status=_string(job.get("state")))
        work_id = _string(job.get("roadmap_id"))
        if work_id:
            source = f"work:{work_id}"
            state = "source_bound" if work_id in known_work else "missing_target"
            if state == "missing_target":
                add_node(source, "missing_work", f"Missing work: {work_id}", missing=True)
            add_edge(source, target, "runtime_evidence", "job_progress.roadmap_id", state)

    return {
        "schema_version": "1",
        "generated_at": _iso8601(now),
        "source": "coord.db+job_progress" if jobs else "coord.db",
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [edges[key] for key in sorted(edges)],
    }


def _require_exact_keys(value: dict[str, Any], spec: dict[str, Any], path: str) -> None:
    required = set(spec.get("required") or ())
    properties = spec.get("properties") or {}
    missing = sorted(required - set(value))
    extra = sorted(set(value) - set(properties))
    if missing:
        raise ValueError(f"{path} missing required keys: {missing}")
    if spec.get("additionalProperties") is False and extra:
        raise ValueError(f"{path} contains unsupported keys: {extra}")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_snapshot(snapshot: Any, schema: dict[str, Any] | None = None) -> None:
    """Validate the exact dependency-free NativeSnapshotV1 JSON Schema subset."""
    contract = schema or load_schema()
    if not isinstance(contract, dict) or contract.get("title") != "NativeSnapshotV1":
        raise ValueError("invalid NativeSnapshotV1 schema resource")
    if not isinstance(snapshot, dict):
        raise ValueError("NativeSnapshotV1 must be an object")
    _require_exact_keys(snapshot, contract, "NativeSnapshotV1")
    if snapshot.get("schema_version") != NATIVE_SNAPSHOT_SCHEMA:
        raise ValueError("unsupported NativeSnapshotV1 schema version")
    generated_at = snapshot.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("NativeSnapshotV1 generated_at must be a string")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("NativeSnapshotV1 generated_at must be ISO-8601") from exc
    if generated.tzinfo is None:
        raise ValueError("NativeSnapshotV1 generated_at must include a timezone")
    if not isinstance(snapshot.get("source"), str) or not snapshot["source"]:
        raise ValueError("NativeSnapshotV1 source must be a non-empty string")
    if not isinstance(snapshot.get("stale"), bool):
        raise ValueError("NativeSnapshotV1 stale must be a boolean")

    summary = snapshot.get("summary")
    summary_spec = contract["properties"]["summary"]
    if not isinstance(summary, dict):
        raise ValueError("NativeSnapshotV1 summary must be an object")
    _require_exact_keys(summary, summary_spec, "NativeSnapshotV1.summary")
    for key in summary_spec["required"]:
        if not _is_integer(summary.get(key)) or summary[key] < 0:
            raise ValueError(f"NativeSnapshotV1.summary.{key} must be non-negative")

    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise ValueError("NativeSnapshotV1 rows must be an array")
    row_spec = contract["properties"]["rows"]["items"]
    row_ids: set[str] = set()
    for index, row in enumerate(rows):
        path = f"NativeSnapshotV1.rows[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{path} must be an object")
        _require_exact_keys(row, row_spec, path)
        for key in ("id", "title", "status", "bucket", "owner", "module", "group", "current_step"):
            if not isinstance(row.get(key), str):
                raise ValueError(f"{path}.{key} must be a string")
        if not row["id"] or row["id"] in row_ids:
            raise ValueError("NativeSnapshotV1 row identifiers must be non-empty and unique")
        row_ids.add(row["id"])
        if not _is_integer(row.get("priority")) or row["priority"] < 0:
            raise ValueError(f"{path}.priority must be a non-negative integer")
        progress = row.get("progress_fraction")
        if progress is not None and (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not 0 <= float(progress) <= 1
        ):
            raise ValueError(f"{path}.progress_fraction must be null or between zero and one")
        eta = row.get("eta_seconds")
        if eta is not None and (not _is_integer(eta) or eta < 0):
            raise ValueError(f"{path}.eta_seconds must be null or non-negative")
        if not isinstance(row.get("stale"), bool):
            raise ValueError(f"{path}.stale must be a boolean")

    if summary["total"] != len(rows):
        raise ValueError("NativeSnapshotV1 summary.total must equal row count")
    if sum(summary[key] for key in ("running", "attention", "next", "done")) != len(rows):
        raise ValueError("NativeSnapshotV1 summary buckets must account for every row")

    sessions = snapshot.get("sessions")
    if sessions is not None:
        if not isinstance(sessions, list):
            raise ValueError("NativeSnapshotV1 sessions must be an array")
        session_spec = contract["properties"]["sessions"]["items"]
        for index, session in enumerate(sessions):
            path = f"NativeSnapshotV1.sessions[{index}]"
            if not isinstance(session, dict):
                raise ValueError(f"{path} must be an object")
            _require_exact_keys(session, session_spec, path)
            for key in ("id", "actor", "label"):
                if not isinstance(session.get(key), str):
                    raise ValueError(f"{path}.{key} must be a string")
            if not isinstance(session.get("live"), bool):
                raise ValueError(f"{path}.live must be a boolean")


__all__ = [
    "NATIVE_SNAPSHOT_SCHEMA",
    "build_graph",
    "build_pulse",
    "build_snapshot",
    "load_schema",
    "validate_snapshot",
]


CONTEXT_SCHEMA = "ContextV1"


def _claim_present(row: dict[str, Any]) -> bool:
    """Whether a claim row exists at all, which is not the same as running.

    `v_work_owner` left-joins the claim, so a null `claim_expires_at` means no
    claim in any of the states that view carries. A row can be not-running
    because its claim was released with a disposition or because the lease ran
    out and the claim went away; those leave different residue, and until this
    field existed the board published the same silence for both.
    """
    return row.get("claim_expires_at") is not None


def _lease_remaining_s(row: dict[str, Any], now: float) -> int | None:
    """Seconds left on the claim, floored, or None when there is no claim.

    Floored rather than truncated: `int()` rounds toward zero, which would
    round a lease that expired 0.4s ago up to `0` and file it with the ones
    still alive.
    """
    if not _claim_present(row):
        return None
    expires = row.get("claim_expires_at")
    try:
        return int(math.floor(float(expires) - float(now)))
    except (TypeError, ValueError, OverflowError):
        return None


def build_context(db_path: str | Path | None = None) -> dict[str, Any]:
    """Structural context for every row: what it hangs off, what waits on it.

    Deliberately separate from NativeSnapshotV1, which is a sealed contract the
    native clients decode; widening it to carry navigation would version a
    schema for the sake of a web view.

    Deliberately structural. The board is read-only and unauthenticated, and the
    reason it has never carried event bodies, decisions or knowledge text is
    that none of those are public. What it adds here is the shape of the work --
    parents, dependencies, dependents, siblings, whether a completion artifact
    exists -- which is what makes the graph navigable without disclosing
    anything the snapshot did not already show.
    """
    db = Path(db_path) if db_path is not None else config.coord_db_path()
    with _materialized_connection(db) as conn:
        now = config.source_date_epoch(coord_db.db_now(conn))
        rows = [dict(row) for row in coord_db.board_rows(conn, at=now)]
        artifacts = {
            _string(row["work_id"])
            for row in conn.execute("SELECT DISTINCT work_id FROM artifacts").fetchall()
            if _string(row["work_id"])
        }

    by_id = {_string(r.get("work_id")): r for r in rows if _string(r.get("work_id"))}
    dependents: dict[str, list[str]] = {work_id: [] for work_id in by_id}
    children: dict[str, list[str]] = {work_id: [] for work_id in by_id}

    for work_id, row in by_id.items():
        for dependency in _json_list(row.get("depends_on")):
            dependency = _string(dependency)
            if dependency in dependents:
                dependents[dependency].append(work_id)
        parent = _string(row.get("parent_id"))
        if parent in children:
            children[parent].append(work_id)

    items = []
    for work_id in sorted(by_id):
        row = by_id[work_id]
        parent = _string(row.get("parent_id"))
        depends = [_string(d) for d in _json_list(row.get("depends_on")) if _string(d)]
        # Siblings are what a reader reaches for next when a row is not the
        # answer, so they are computed here rather than left to the client to
        # rediscover by scanning every row.
        siblings = sorted(set(children.get(parent, [])) - {work_id}) if parent else []
        items.append(
            {
                "id": work_id,
                "parent": parent,
                "children": sorted(children.get(work_id, [])),
                "depends_on": depends,
                "dependents": sorted(dependents.get(work_id, [])),
                "siblings": siblings,
                "done_signal": _string(row.get("done_signal")),
                "artifact_recorded": work_id in artifacts,
                "blocked_reason_class": _string(row.get("blocked_reason_class")),
                "resume_when": _string(row.get("resume_when")),
                "next_step": _string(row.get("next_step")),
                # Custody, as two scalars derived from one integer column.
                # `v_work_owner` already joins `claims.expires_at` onto every
                # work row and `build_context` discarded it, so the board could
                # show that a row was not running and never whether its claim
                # had been released or had simply run out.
                #
                # The remainder, not the deadline. It is relative to
                # `generated_at`, which this document already publishes, so it
                # discloses strictly less than the timestamp would: the same
                # subtraction `sessions[].live` already publishes thresholded to
                # one bit, here left un-thresholded. It may be negative -- an
                # expired lease is a fact, and clamping it to zero would make an
                # hour-dead claim look like one that just lapsed.
                "claim_present": _claim_present(row),
                "lease_remaining_s": _lease_remaining_s(row, now),
            }
        )

    return {
        "schema_version": CONTEXT_SCHEMA,
        "generated_at": _iso8601(now),
        "source": "coord.db",
        "items": items,
    }


TIMELINE_SCHEMA = "TimelineV1"


def build_timeline(db_path: str | Path | None = None) -> dict[str, Any]:
    """When each row moved, and who moved it -- occurrence only, never prose.

    The board is read-only and unauthenticated. That a row was claimed at
    09:14 by the codex lane is the same class of fact the snapshot already
    publishes; what the agent wrote about it is not. So this reads exactly
    four columns from `events` and carries three of them. `title`, `body`,
    `refs_json`, `payload_json`, `session_id`, `to_selector`, `severity`,
    `verdict` and `trust` are never selected, which is stronger than
    selecting and dropping them: a later edit cannot leak a field the query
    never fetched.

    Events with no `work_id` are excluded rather than pooled under a
    placeholder row. They exist -- lease sweeps and other estate-wide
    bookkeeping post without a work item -- but a timeline is per row, and
    inventing a bucket for them would imply a row the board does not have.

    An event whose `work_id` names no work item is excluded for exactly that
    reason and not a weaker one. `events.work_id` carries no foreign key
    (schema.sql), so a mistyped selector, an id from another board, or a work
    item deleted after its events were written all leave events pointing at
    nothing. Publishing them mints a timeline item whose id is not on the board:
    the drawer that opens rows by id opens an empty one, and the item asserts a
    row exists. Membership is read from `work_items`, the base table `board_rows`
    projects through `v_work_owner`, so the timeline's ids are a subset of the
    snapshot's coord rows by construction rather than by coincidence.
    """
    db = Path(db_path) if db_path is not None else config.coord_db_path()
    with _materialized_connection(db) as conn:
        now = config.source_date_epoch(coord_db.db_now(conn))
        # Ordered in SQL by (ts, event_id) so events that share a timestamp --
        # the seeded board writes five of them in one transaction -- still come
        # out in a fixed order instead of whatever the scan happens to yield.
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT work_id,ts,kind,actor FROM events"
                " WHERE work_id IS NOT NULL AND TRIM(work_id) <> ''"
                " AND work_id IN (SELECT work_id FROM work_items)"
                " ORDER BY ts, event_id"
            ).fetchall()
        ]

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        work_id = _string(row.get("work_id"))
        if not work_id:
            continue
        timestamp = row.get("ts")
        if timestamp is None or isinstance(timestamp, bool):
            continue
        try:
            at = _iso8601(float(timestamp))
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        grouped.setdefault(work_id, []).append(
            {
                "at": at,
                "kind": _string(row.get("kind")),
                "actor": _string(row.get("actor")),
            }
        )

    return {
        "schema_version": TIMELINE_SCHEMA,
        "generated_at": _iso8601(now),
        "source": "coord.db",
        "items": [
            {"id": work_id, "events": grouped[work_id]}
            for work_id in sorted(grouped)
        ],
    }


PULSE_SCHEMA = "PulseV1"

# The three kinds that are a coordination act between lanes rather than a note
# to the record. Named here rather than discovered, because the traffic section
# asserts "this is who handed what to whom" and that claim needs a fixed
# vocabulary; every other kind is still counted, just not routed.
_TRAFFIC_KINDS = ("audit_request", "audit_verdict", "handoff")
# `to_selector` is a routing address written by the typed handoff writer as
# `actor:<lane>`. Anything that does not match this exactly serves null: the
# raw column never reaches the wire, so a future writer that puts prose in it
# cannot leak through this endpoint.
_ACTOR_SELECTOR = re.compile(r"^actor:([a-z0-9][a-z0-9_-]*)$")
_PULSE_RECENT = 12


def _lane_of(actor: Any) -> str:
    """The lane an actor belongs to: the prefix before the first colon.

    Sessions are namespaced (`claude:cloud-a`) and row owners are not
    (`claude`), so a roster that matched session ids against owners would find
    nothing. Both reduce to a lane here, which is the only vocabulary the two
    have in common.
    """
    return _string(actor).split(":")[0].strip().lower()


def build_pulse(db_path: str | Path | None = None) -> dict[str, Any]:
    """The record as a record: which lane wrote what kind of event, and when.

    Three facts live here that none of the other four documents carry.

    The first is direction. `events.to_selector` is written by the typed
    handoff writer as `actor:<lane>`, and TimelineV1 does not select it -- its
    event tuple is sealed at (at, kind, actor) by test, and widening it would
    be a wire change for every client that decodes it. So a reader could see
    that a handoff happened and never see who it was to. This publishes the
    pair as counts: kind, from-lane, to-lane, how many.

    The second is the kind vocabulary. `events.kind` is unconstrained TEXT with
    no registry, so the only honest way to say what kinds exist on a board is
    to count the ones that do rather than to enumerate the ones that should.

    The third is the shape of the record over time -- how many distinct
    instants, across how many UTC days, and the newest handful in write order.

    What this does not carry is what the timeline does not carry, for the same
    reason and by the same means: `title`, `body`, `refs_json`, `payload_json`,
    `session_id`, `severity`, `verdict` and `trust` are never SELECTed, and
    `to_selector` is matched against a lane grammar server-side and serialized
    only as the lane it names. A rate is never computed: these instants are
    sparse and irregular, and dividing them by a wall-clock span would invent a
    continuous process the record does not contain.

    Population is exactly TimelineV1's -- events attached to a row the board
    actually carries -- so the two documents can never disagree about how many
    events exist. Events with no work item, or with a work item this board does
    not have, or with a timestamp no calendar can express, are counted in
    `counts` rather than dropped in silence. So are events whose actor names no
    lane: `traffic` routes only acts with a named sender, because a lane-pair
    row reading `from: ""` would assert a lane that appears in no roster, and
    `counts.events_unattributed` carries them instead so the lanes section can
    be checked against the total on any board rather than only on a seeded one.
    """
    db = Path(db_path) if db_path is not None else config.coord_db_path()
    with _materialized_connection(db) as conn:
        now = config.source_date_epoch(coord_db.db_now(conn))
        known = {
            _string(row["work_id"])
            for row in conn.execute("SELECT work_id FROM work_items").fetchall()
            if _string(row["work_id"])
        }
        raw = [
            dict(row)
            for row in conn.execute(
                "SELECT work_id,ts,kind,actor,to_selector FROM events"
                " ORDER BY ts, event_id"
            ).fetchall()
        ]
        sessions = [
            _session_row(dict(row))
            for row in coord_db.session_rollup(conn, at=now)
        ]
        rows_total = len(known)

    detached = 0
    unrepresentable = 0
    events: list[dict[str, Any]] = []
    for row in raw:
        work_id = _string(row.get("work_id"))
        if work_id not in known:
            detached += 1
            continue
        timestamp = row.get("ts")
        if timestamp is None or isinstance(timestamp, bool):
            unrepresentable += 1
            continue
        try:
            at = _iso8601(float(timestamp))
        except (TypeError, ValueError, OverflowError, OSError):
            unrepresentable += 1
            continue
        match = _ACTOR_SELECTOR.match(_string(row.get("to_selector")).lower())
        events.append(
            {
                "at": at,
                "kind": _string(row.get("kind")),
                "actor": _string(row.get("actor")),
                "lane": _lane_of(row.get("actor")),
                "to": match.group(1) if match else None,
                "row": work_id,
            }
        )

    kinds: dict[str, int] = {}
    lane_events: dict[str, dict[str, int]] = {}
    days: dict[str, dict[str, str | int]] = {}
    traffic: dict[tuple[str, str, str], int] = {}
    undirected: dict[tuple[str, str], int] = {}
    instants: set[str] = set()
    rows_with_events: set[str] = set()

    events_unattributed = 0

    for event in events:
        kind = event["kind"]
        lane = event["lane"]
        kinds[kind] = kinds.get(kind, 0) + 1
        instants.add(event["at"])
        rows_with_events.add(event["row"])
        date = event["at"][:10]
        bucket = days.setdefault(date, {"date": date, "events": 0, "first_at": event["at"], "last_at": event["at"]})
        bucket["events"] = int(bucket["events"]) + 1
        bucket["last_at"] = event["at"]
        if not lane:
            # `events.actor` is nullable TEXT with no registry, so an actor can
            # be absent, blank, or a bare `:suffix` -- all of which name no
            # lane. Counted here rather than filed under the empty string:
            # publishing a lane whose name is "" would put a row in the lane
            # section that no roster contains, and discarding it silently would
            # make `sum(lanes[].events)` disagree with `counts.events` with no
            # term to explain the gap. Same rule as `events_without_row`:
            # counted, never rehomed.
            events_unattributed += 1
            continue
        lane_events.setdefault(lane, {})
        lane_events[lane][kind] = lane_events[lane].get(kind, 0) + 1
        if kind in _TRAFFIC_KINDS:
            if event["to"]:
                key = (kind, lane, event["to"])
                traffic[key] = traffic.get(key, 0) + 1
            else:
                undirected[(kind, lane)] = undirected.get((kind, lane), 0) + 1

    session_lanes = [_lane_of(session["actor"]) for session in sessions]
    sessions_unattributed = sum(1 for lane in session_lanes if not lane)
    sessions_live_unattributed = sum(
        1
        for session, lane in zip(sessions, session_lanes)
        if not lane and session["live"]
    )

    lanes_seen = set(lane_events) | set(session_lanes)
    lanes_seen.discard("")

    def _kind_list(counts: dict[str, int]) -> list[dict[str, Any]]:
        # Descending count then kind, so two reads of one board serialise
        # identically and a tie is broken by name rather than by scan order.
        return [
            {"kind": kind, "count": count}
            for kind, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

    lanes = []
    for lane in sorted(lanes_seen):
        mine = [session for session in sessions if _lane_of(session["actor"]) == lane]
        lanes.append(
            {
                "lane": lane,
                "events": sum(lane_events.get(lane, {}).values()),
                "kinds": _kind_list(lane_events.get(lane, {})),
                "sessions": len(mine),
                "sessions_live": sum(1 for session in mine if session["live"]),
            }
        )

    return {
        "schema_version": PULSE_SCHEMA,
        "generated_at": _iso8601(now),
        "source": "coord.db",
        "counts": {
            "events": len(events),
            # Counted, never rehomed. An event with no work item, or with one
            # this board does not carry, exists; publishing it under an invented
            # row id would assert a row the drawer cannot open.
            "events_without_row": detached,
            "events_unrepresentable_time": unrepresentable,
            # Events whose actor names no lane, and so appear in `kinds` and
            # `days` but in no `lanes[]` entry. Published so that
            # `sum(lanes[].events) + events_unattributed == events` holds on
            # every board rather than only on one where every actor is set.
            "events_unattributed": events_unattributed,
            "rows": rows_total,
            "rows_with_events": len(rows_with_events),
            "distinct_instants": len(instants),
            "days": len(days),
            "lanes": len(lanes),
            "sessions": len(sessions),
            "sessions_live": sum(1 for session in sessions if session["live"]),
            # The same gap on the session leg: `agent_sessions.actor` is NOT
            # NULL but may still be blank, which names no lane.
            "sessions_unattributed": sessions_unattributed,
            "sessions_live_unattributed": sessions_live_unattributed,
        },
        "kinds": _kind_list(kinds),
        "lanes": lanes,
        "days": [days[date] for date in sorted(days)],
        "traffic": [
            {"kind": kind, "from": source, "to": target, "count": count}
            for (kind, source, target), count in sorted(traffic.items())
        ],
        # A coordination act whose selector names no lane. Kept separate rather
        # than folded into `traffic` with a null target, because "we do not know
        # where this went" is a different fact from "this went nowhere".
        "traffic_undirected": [
            {"kind": kind, "from": source, "count": count}
            for (kind, source), count in sorted(undirected.items())
        ],
        # Newest first. Ties are broken by (ts, event_id) in SQL and reversed
        # here, so events sharing an instant keep a fixed order rather than
        # whichever the scan yields -- but that order is insertion, not
        # recency, and a reader of this list is told so by the count of
        # distinct instants beside it.
        "recent": [
            {key: event[key] for key in ("at", "kind", "actor", "to", "row")}
            for event in reversed(events[-_PULSE_RECENT:])
        ],
    }
