from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from coordharness.jobs import diagnostic_marker as job_authority
from coordharness import config as harness_config

SIDECAR_DIR = harness_config.job_progress_dir()

_PRESERVE = ("script", "kind", "owner", "proc_pattern", "total", "roadmap_id",
             "started_at", "created_at")
_CHILD_PROGRESS_KEYS = (
    "pct", "done", "rows_done", "total", "eta_s", "eta_min", "rate",
    "rate_unit", "docs_per_s", "done_signal", "blocked_by",
)
TRACKED_CUSTODY_SCHEMA = "coordharness.tracked-job-custody.v1"
_CUSTODY_PGID_KEYS = (
    "custody_schema",
    "roadmap_id",
    "attempt",
    "wrapper_ppid",
    "wrapper_started_at",
    "launcher_pid",
    "launcher_ppid",
    "launcher_started_at",
    "child_ppid",
    "child_pgid",
    "process_ancestry",
    "original_argv_sha256",
    "resolved_argv_sha256",
    "child_argv_sha256",
    "wrapper_sha256",
    "launcher_sha256",
    "release_token_sha256",
    "execution_token_sha256",
    "workload_release_state",
    "workload_released_at",
)
_PGID_KEYS = (
    "pgid",
    "child_pid",
    "wrapper_pid",
    "child_started_at",
    "progress_file",
    *(key for key in _CUSTODY_PGID_KEYS if key not in {"roadmap_id", "attempt"}),
)
_DEFAULT_TERMINAL_STATES = {
    "done",
    "failed",
    "blocked",
    "paused",
    "skipped",
    "superseded",
    "complete",
    "completed",
    "success",
}
DEAD_RECONCILIATION_SCHEMA = "coordharness.job-progress-dead-reconciliation.v1"
_DEAD_RECONCILIATION_PID_FIELDS = (
    "pid",
    "runner_pid",
    "child_pid",
    "wrapper_pid",
    "pgid",
)


class WrapperPolicyError(RuntimeError):
    pass


class DeadReconciliationError(RuntimeError):
    pass


def _iso(now: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).astimezone().isoformat()


def _path(job_id: str) -> Path:
    return SIDECAR_DIR / f"{job_id}.json"


def _canonical_path(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


def tracked_control_paths(job_id: str) -> dict[str, str]:
    """Return configured canonical custody paths for one tracked job."""

    job_key = hashlib.sha256(str(job_id).strip().encode("utf-8")).hexdigest()
    control_dir = job_authority.configured_control_dir()
    return {
        "canonical_control_record_path": _canonical_path(
            job_authority.control_record_path(job_id)
        ),
        "canonical_control_sentinel_path": _canonical_path(
            job_authority.managed_sentinel_path(job_id)
        ),
        "canonical_control_lock_path": _canonical_path(
            control_dir / "locks" / f"{job_key}.lock"
        ),
    }


def _read(job_id: str) -> dict:
    try:
        with open(_path(job_id)) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _atomic_write(payload: dict, path: Path) -> None:
    def public_value(value):
        if isinstance(value, dict):
            return {key: public_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [public_value(item) for item in value]
        if isinstance(value, tuple):
            return [public_value(item) for item in value]
        if isinstance(value, str) and os.path.isabs(value):
            return harness_config.public_path_ref(value)
        return value

    payload = public_value(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _require_roadmap(roadmap_id: str | None) -> str:
    rid = str(roadmap_id or "").strip()
    if not rid:
        raise ValueError(
            "sidecar_writer: roadmap_id is required; every sidecar must be bound "
            "to a real work item, never inferred from the job name.")
    return rid


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _progress_num(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        num = _num(value)
        if num is not None:
            return num
    return None


def _timestamp(v: Any) -> float | None:
    n = _num(v)
    if n is not None:
        return n
    if not isinstance(v, str):
        return None
    text = v.strip()
    if not text:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: Any) -> bool:
    value = _int(pid)
    if value is None or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _regular_sidecar_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_size),
    )


def _exact_regular_sidecar(path: Path) -> tuple[dict[str, Any], tuple[int, ...]]:

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DeadReconciliationError(
            f"sidecar is unavailable or unsafe: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or int(before.st_nlink) != 1:
            raise DeadReconciliationError(
                "sidecar must be an exact single-link regular file"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            try:
                payload = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise DeadReconciliationError(
                    f"sidecar JSON is unreadable: {type(exc).__name__}: {exc}"
                ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(payload, dict):
        raise DeadReconciliationError("sidecar JSON must be an object")
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise DeadReconciliationError(
            f"sidecar changed during validation: {type(exc).__name__}: {exc}"
        ) from exc
    before_signature = _regular_sidecar_signature(before)
    after_signature = _regular_sidecar_signature(after)
    if (
        not stat.S_ISREG(after.st_mode)
        or int(after.st_nlink) != 1
        or after_signature != before_signature
    ):
        raise DeadReconciliationError("sidecar changed during validation")
    return payload, after_signature


def _positive_process_identity(field: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DeadReconciliationError(f"{field} process identity is malformed")
    if isinstance(value, float) and not value.is_integer():
        raise DeadReconciliationError(f"{field} process identity is malformed")
    try:
        identity = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DeadReconciliationError(
            f"{field} process identity is malformed"
        ) from exc
    return identity if identity > 0 else None


def _prove_process_identity_absent(field: str, identity: int) -> None:
    target = -identity if field == "pgid" else identity
    try:
        os.kill(target, 0)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise DeadReconciliationError(
            f"{field}={identity} absence is uncertain (permission denied)"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise DeadReconciliationError(
            f"{field}={identity} absence is uncertain ({type(exc).__name__}: {exc})"
        ) from exc
    raise DeadReconciliationError(f"{field}={identity} is still live")


def reconcile_dead(
    job_id: str,
    *,
    stale_after_s: float,
    now: float | None = None,
) -> dict[str, Any]:

    normalized_job_id = str(job_id or "").strip()
    if (
        not normalized_job_id
        or normalized_job_id in {".", ".."}
        or Path(normalized_job_id).name != normalized_job_id
        or "\x00" in normalized_job_id
    ):
        raise DeadReconciliationError("job_id must be one exact sidecar basename")
    stale_threshold = _num(stale_after_s)
    observed_now = _num(time.time() if now is None else now)
    if stale_threshold is None or stale_threshold <= 0:
        raise DeadReconciliationError("stale_after_s must be a positive finite number")
    if observed_now is None:
        raise DeadReconciliationError("reconciliation time must be finite")

    sidecar_path = _path(normalized_job_id)
    with job_authority.wrapper_control_lock(normalized_job_id):
        current, signature = _exact_regular_sidecar(sidecar_path)
        if str(current.get("job_id") or "").strip() != normalized_job_id:
            raise DeadReconciliationError(
                "sidecar job_id does not match the requested exact identity"
            )

        already_reconciled = (
            _state(current.get("state")) == "failed"
            and current.get("dead_reconciliation_schema")
            == DEAD_RECONCILIATION_SCHEMA
            and current.get("diagnostic_only") is True
        )
        if already_reconciled:
            if not job_authority.diagnostic_marker_present(sidecar_path):
                roadmap_id = str(current.get("roadmap_id") or "").strip()
                if not roadmap_id:
                    raise DeadReconciliationError(
                        "reconciled sidecar lacks roadmap_id for diagnostic marker"
                    )
                job_authority.set_diagnostic_marker(
                    sidecar_path,
                    job_id=normalized_job_id,
                    roadmap_id=roadmap_id,
                    now=observed_now,
                )
            return current
        if _state(current.get("state")) != "running":
            raise DeadReconciliationError("sidecar state must be running")

        updated_at = _timestamp(current.get("updated_at"))
        if updated_at is None:
            raise DeadReconciliationError(
                "running sidecar lacks a valid updated_at freshness identity"
            )
        age_s = observed_now - updated_at
        if age_s <= stale_threshold:
            raise DeadReconciliationError(
                f"sidecar is not stale enough ({age_s:.3f}s <= {stale_threshold:.3f}s)"
            )

        identities: dict[str, int] = {}
        for field in _DEAD_RECONCILIATION_PID_FIELDS:
            identity = _positive_process_identity(field, current.get(field))
            if identity is not None:
                identities[field] = identity
        if not identities:
            raise DeadReconciliationError(
                "sidecar has no positive process identity to prove absent"
            )
        for field, identity in identities.items():
            _prove_process_identity_absent(field, identity)

        roadmap_id = str(current.get("roadmap_id") or "").strip()
        if not roadmap_id:
            raise DeadReconciliationError(
                "running sidecar lacks roadmap_id for diagnostic marker"
            )
        reason = (
            "orphaned running telemetry: every recorded positive process identity "
            "is absent via kill(0)"
        )
        job_authority.set_diagnostic_marker(
            sidecar_path,
            job_id=normalized_job_id,
            roadmap_id=roadmap_id,
            now=observed_now,
        )
        try:
            after = os.lstat(sidecar_path)
        except OSError as exc:
            raise DeadReconciliationError(
                f"sidecar changed before reconciliation: {type(exc).__name__}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(after.st_mode)
            or int(after.st_nlink) != 1
            or _regular_sidecar_signature(after) != signature
        ):
            raise DeadReconciliationError("sidecar changed before reconciliation")

        reconciled = dict(current)
        reconciled["state"] = "failed"
        reconciled["diagnostic_only"] = True
        reconciled["dead_reconciliation_schema"] = DEAD_RECONCILIATION_SCHEMA
        reconciled["dead_reconciliation_reason"] = reason
        reconciled["dead_reconciled_at"] = _iso(observed_now)
        reconciled["dead_reconciliation_stale_after_s"] = stale_threshold
        reconciled["dead_reconciliation_age_s"] = age_s
        reconciled["dead_reconciliation_identities"] = identities
        _atomic_write(reconciled, sidecar_path)
        return reconciled


def _state(v: Any) -> str:
    return str(v or "").strip().lower()


def _terminal_set(states: set[str] | None = None) -> set[str]:
    return _DEFAULT_TERMINAL_STATES | {_state(s) for s in (states or set()) if _state(s)}


def _terminal_fenced(cur: dict, incoming_state: str | None, *,
                     terminal_states: set[str] | None = None) -> bool:
    terminals = _terminal_set(terminal_states)
    cur_state = _state(cur.get("state"))
    if cur_state not in terminals:
        return False
    incoming = _state(incoming_state)
    return incoming not in terminals or incoming != cur_state


def _attempt_fenced(cur: dict, incoming_attempt: int | None) -> bool:
    if incoming_attempt is None:
        return False
    cur_attempt = _int(cur.get("attempt"))
    return cur_attempt is not None and cur_attempt != incoming_attempt


def appear(job_id: str, roadmap_id: str, *, script: str | None = None,
           total: int | None = None, kind: str | None = None,
           owner: str = "claude", proc_pattern: str | None = None,
           wrapper_launch_id: str | None = None,
           now: float | None = None) -> dict:
    rid = _require_roadmap(roadmap_id)
    now = time.time() if now is None else now
    sidecar_path = _path(job_id)
    with job_authority.wrapper_control_lock(job_id):
        cur = _read(job_id)
        control = job_authority.observe_control(job_id)
        cur_nonterminal = _state(cur.get("state")) not in _terminal_set()
        if control.state == "invalid" and cur_nonterminal:
            raise WrapperPolicyError(
                f"managed control is invalid for active job_id {job_id!r}; refusing reset"
            )
        if (
            control.state == "valid"
            and cur_nonterminal
            and _pid_alive(control.wrapper_pid)
        ):
            raise WrapperPolicyError(
                f"concurrent appear refused for live job_id {job_id!r}"
            )
        if (
            control.state == "legacy"
            and cur_nonterminal
            and _pid_alive(cur.get("child_pid") or cur.get("pid") or cur.get("wrapper_pid"))
        ):
            raise WrapperPolicyError(
                f"concurrent appear refused for live job_id {job_id!r}"
            )
        attempt = (_int(control.attempt) or _int(cur.get("attempt")) or 0) + 1
        launch_id = (
            job_authority.normalize_launch_id(wrapper_launch_id)
            if wrapper_launch_id is not None
            else job_authority.new_launch_id()
        )
        if launch_id is None:
            raise WrapperPolicyError("wrapper_launch_id must be 32 lowercase hex characters")
        job_authority.publish_wrapper_control(
            job_id=job_id,
            roadmap_id=rid,
            launch_id=launch_id,
            wrapper_started_at=now,
            diagnostic_only=False,
            sidecar_path=sidecar_path,
            wrapper_pid=os.getpid(),
            attempt=attempt,
            now=now,
        )
        payload = {
            "job_id": job_id, "roadmap_id": rid, "owner": owner,
            "attempt": attempt,
            "wrapper_managed": True, "wrapper_launch_id": launch_id,
            "wrapper_pid": os.getpid(),
            "diagnostic_only": False,
            "state": "running", "pct": None, "done": False, "total": total or 0,
            "step": "starting", "rate": None, "eta_s": None,
            "script": script, "kind": kind, "proc_pattern": proc_pattern,
            "started_at": now, "created_at": now, "updated_at": _iso(now),
        }
        _atomic_write(payload, sidecar_path)
        job_authority.clear_diagnostic_marker(sidecar_path)
        return payload


def update(job_id: str, roadmap_id: str, *, state: str | None = None,
           pct: float | None = None, done: bool | None = None,
           total: int | None = None, step: str | None = None,
           rate: float | None = None, eta_s: float | None = None,
           pid: int | None = None, script: str | None = None,
           kind: str | None = None, owner: str | None = None,
           proc_pattern: str | None = None, now: float | None = None,
           attempt: int | None = None,
           **extra: Any) -> dict:

    with job_authority.wrapper_control_lock(job_id):
        return _update_locked(
            job_id,
            roadmap_id,
            state=state,
            pct=pct,
            done=done,
            total=total,
            step=step,
            rate=rate,
            eta_s=eta_s,
            pid=pid,
            script=script,
            kind=kind,
            owner=owner,
            proc_pattern=proc_pattern,
            now=now,
            attempt=attempt,
            **extra,
        )


def _update_locked(job_id: str, roadmap_id: str, *, state: str | None = None,
           pct: float | None = None, done: bool | None = None,
           total: int | None = None, step: str | None = None,
           rate: float | None = None, eta_s: float | None = None,
           pid: int | None = None, script: str | None = None,
           kind: str | None = None, owner: str | None = None,
           proc_pattern: str | None = None, now: float | None = None,
           attempt: int | None = None,
           **extra: Any) -> dict:
    rid = _require_roadmap(roadmap_id)
    now = time.time() if now is None else now
    cur = _read(job_id)
    control = job_authority.observe_control(job_id)
    supplied_raw = extra.get("wrapper_launch_id") or os.environ.get("COORD_WRAPPER_LAUNCH_ID")
    if control.state == "legacy" and supplied_raw:
        return cur
    if control.state == "legacy" and not cur:
        return cur
    if control.state == "invalid":
        return cur
    if control.state == "valid":
        if (
            control.roadmap_id != rid
            or control.sidecar_path != job_authority.canonical_sidecar_path(_path(job_id))
        ):
            return cur
        supplied_launch_id = job_authority.normalize_launch_id(supplied_raw)
        if supplied_launch_id != control.launch_id:
            return cur
        extra["wrapper_managed"] = True
        extra["wrapper_launch_id"] = control.launch_id
        extra["diagnostic_only"] = bool(control.diagnostic_only)
    if _attempt_fenced(cur, attempt):
        return cur
    if _terminal_fenced(cur, state):
        return cur
    payload = dict(cur)
    payload["job_id"] = job_id
    payload["roadmap_id"] = rid
    if attempt is not None:
        payload["attempt"] = int(attempt)

    new_pct = _num(pct)
    if new_pct is not None:
        old_pct = _num(cur.get("pct"))
        payload["pct"] = new_pct if old_pct is None else max(old_pct, new_pct)
    if done is not None:
        payload["done"] = bool(cur.get("done")) or bool(done)
    for key, val in (("state", state), ("step", step), ("rate", rate),
                     ("eta_s", eta_s), ("pid", pid), ("total", total),
                     ("kind", kind), ("owner", owner), ("script", script),
                     ("proc_pattern", proc_pattern)):
        if val is not None:
            payload[key] = val
    if pid is not None and _int(cur.get("pid")) != int(pid):
        # pid changed (or is new) -- (re)stamp pid_started_at so a later PID
        # reuse can be told apart from this process via process_liveness.
        # payload already carries any prior pid_started_at forward via
        # dict(cur) above when the pid is unchanged, so this only runs on a
        # real transition.
        from coordharness.coord import process_liveness
        payload["pid_started_at"] = process_liveness.pid_start_time(pid)
    payload.update(extra)
    payload.setdefault("created_at", now)
    payload["updated_at"] = _iso(now)
    _atomic_write(payload, _path(job_id))
    return payload


def finalize(job_id: str, roadmap_id: str, *, state: str,
             done_signal: str | None = None, root: str | Path | None = None,
             settle_s: float = 10.0, step: str | None = None,
             now: float | None = None, attempt: int | None = None,
             **extra: Any) -> dict:
    rid = _require_roadmap(roadmap_id)
    if state == "done" and done_signal:
        from coordharness.jobs import status
        proof_root = root if root is not None else harness_config.project_root()
        if not status.artifact_settled(done_signal, proof_root, settle_s=settle_s, now=now):
            return update(job_id, rid, state="running",
                          step="finalizing (artifact still settling)", now=now,
                          attempt=attempt, **extra)
    return update(job_id, rid, state=state, done=(state == "done"),
                  pct=100.0 if state == "done" else None,
                  step=step or state, now=now, attempt=attempt, **extra)


def _wrapper_started_at(now: float) -> float:
    started = _num(os.environ.get("COORD_WRAPPER_STARTED_AT"))
    return started if started is not None else now


def _wrapper_started_before_current_run(cur: dict, now: float) -> bool:
    cur_started = _num(cur.get("started_at") or cur.get("created_at"))
    if cur_started is None:
        return False
    return _wrapper_started_at(now) < cur_started


def _wrapper_start_resets_terminal(cur: dict, state: str, step: str, *,
                                   terminal_states: set[str],
                                   heartbeat_marker: str, now: float) -> bool:
    if _state(state) != "running" or step == heartbeat_marker:
        return False
    if _state(cur.get("state")) not in terminal_states:
        return False
    terminal_at = (
        _timestamp(cur.get("terminal_at"))
        or _timestamp(cur.get("updated_at"))
        or _timestamp(cur.get("started_at"))
        or _timestamp(cur.get("created_at"))
    )
    return terminal_at is not None and _wrapper_started_at(now) > terminal_at


def _copy_pgid_file(payload: dict, pgid_file: str | Path | None) -> None:
    if not pgid_file:
        return
    pgid_path = str(pgid_file)
    payload["pgid_file"] = pgid_path
    try:
        with open(pgid_path) as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key in _PGID_KEYS:
        if data.get(key) is not None:
            payload[key] = data.get(key)


def _run_events_enabled() -> bool:
    raw = str(os.environ.get("COORD_RUNEVENTSTORE_V2", "1") or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _running_claim_binding(work_id: str, *, at: float) -> dict[str, Any] | None:
    try:
        from coordharness.coord import coord_db
        from coordharness.coord.config import connect

        conn = connect(os.environ.get("COORD_COORD_DB") or None)
        try:
            return coord_db.running_claim_binding(conn, work_id=work_id, at=at)
        finally:
            conn.close()
    except Exception:
        return None


def _renew_bound_wrapper_claim(
    control: job_authority.ControlObservation,
    *,
    work_id: str,
    at: float,
    heartbeat_s: int | float | None,
) -> bool:
    if (
        control.state != "valid"
        or control.claim_binding_state != "bound"
        or not control.claim_id
        or not control.claim_session_id
        or control.roadmap_id != work_id
    ):
        return False
    try:
        from coordharness.coord import coord_db
        from coordharness.coord.config import connect

        lease_s = max(
            float(coord_db.LEASE_DEFAULT_S),
            max(1.0, float(heartbeat_s or 30.0)) * 3.0,
        )
        conn = connect(os.environ.get("COORD_COORD_DB") or None)
        try:
            return coord_db.renew_claim_from_sidecar(
                conn,
                claim_id=control.claim_id,
                work_id=work_id,
                session_id=control.claim_session_id,
                lease_s=lease_s,
                at=at,
            )
        finally:
            conn.close()
    except Exception:
        return False


def _wrapper_run_event_phase(payload: dict, *, heartbeat_marker: str) -> str | None:
    state = _state(payload.get("state"))
    step = str(payload.get("step") or "")
    if state == "running":
        return None if step == heartbeat_marker else "start"
    if state == "done":
        return "result"
    if state:
        return "error"
    return None


def _record_wrapper_run_event(payload: dict, *, heartbeat_marker: str) -> None:
    if not _run_events_enabled():
        return
    phase = _wrapper_run_event_phase(payload, heartbeat_marker=heartbeat_marker)
    if phase is None:
        return
    work_id = str(payload.get("roadmap_id") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    if not work_id or not job_id:
        return
    attempt = _int(payload.get("attempt")) or 1
    launch_id = str(payload.get("wrapper_launch_id") or "").strip()
    run_id = f"job:{job_id}:{launch_id}" if launch_id else f"job:{job_id}"
    tool_name = str(payload.get("kind") or "local_job").strip() or "local_job"
    content = {
        "job_id": job_id,
        "state": payload.get("state"),
        "step": str(payload.get("step") or "")[:300],
        "exit_code": payload.get("exit_code"),
        "script": payload.get("script"),
        "resource_class": payload.get("resource_class"),
        "owner": payload.get("owner"),
        "pid": payload.get("pid"),
        "diagnostic_only": bool(payload.get("diagnostic_only")),
    }
    metadata = {
        "adapter": "sidecar_writer.wrapper_write.v2" if launch_id else "sidecar_writer.wrapper_write.v1",
        "append_only_evidence": True,
        "authoritative_work_status": bool(payload.get("authoritative_work_status")),
        "diagnostic_only": bool(payload.get("diagnostic_only")),
        "attempt": attempt,
        "wrapper_launch_id": launch_id or None,
        "sidecar_path": str(_path(job_id)),
    }
    try:
        from coordharness.coord.config import connect
        from coordharness.coord.run_events import record_tool_event

        conn = connect(os.environ.get("COORD_COORD_DB") or None)
        try:
            record_tool_event(
                conn,
                work_id=work_id,
                run_id=run_id,
                tool_name=tool_name,
                phase=phase,
                content=content,
                metadata=metadata,
                idempotency_key=(
                    f"sidecar_writer:v2:{job_id}:{launch_id}:{phase}"
                    if launch_id
                    else f"sidecar_writer:v1:{job_id}:{attempt}:{phase}"
                ),
                enabled=True,
            )
        finally:
            conn.close()
    except Exception:
        return


def _run_wrapper_write_policy(payload: dict, *, heartbeat_marker: str) -> dict | None:
    phase = _wrapper_run_event_phase(payload, heartbeat_marker=heartbeat_marker)
    if phase is None:
        return None
    work_id = str(payload.get("roadmap_id") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    if not work_id or not job_id:
        raise WrapperPolicyError("sidecar_writer wrapper policy requires roadmap_id and job_id")
    launch_id = str(payload.get("wrapper_launch_id") or "").strip()
    run_id = f"job:{job_id}:{launch_id}" if launch_id else f"job:{job_id}"
    try:
        from coordharness.coord.config import connect
        from coordharness.coord.policy.pipeline import run_boundary_policy

        conn = connect(os.environ.get("COORD_COORD_DB") or None)
        try:
            return run_boundary_policy(
                boundary="sidecar_writer",
                action="wrapper_write",
                work_id=work_id,
                run_id=run_id,
                actor=str(payload.get("owner") or payload.get("assignee") or "").strip() or None,
                payload={
                    "job_id": job_id,
                    "state": payload.get("state"),
                    "step": str(payload.get("step") or "")[:300],
                    "kind": payload.get("kind"),
                    "resource_class": payload.get("resource_class"),
                    "script": payload.get("script"),
                    "run_event_category": "tool",
                    "wrapper_phase": phase,
                    "wrapper_launch_id": launch_id or None,
                    "authoritative_work_status": bool(payload.get("authoritative_work_status")),
                    "diagnostic_only": bool(payload.get("diagnostic_only")),
                },
                conn=conn,
            )
        finally:
            conn.close()
    except WrapperPolicyError:
        raise
    except Exception as exc:
        raise WrapperPolicyError(f"sidecar_writer wrapper policy unavailable: {type(exc).__name__}: {exc}") from exc


def wrapper_write(job_id: str, roadmap_id: str, *, state: str, step: str,
                  exit_code: int | None = None, pid: int | None = None,
                  kind: str | None = None, owner: str | None = None,
                  script: str | None = None, pgid_file: str | Path | None = None,
                  resource_class: str | None = None,
                  authoritative_work_status: bool = False,
                  diagnostic_only: bool = False,
                  wrapper_launch_id: str | None = None,
                  terminal_states: set[str] | None = None,
                  heartbeat_marker: str = "__heartbeat__",
                  heartbeat_s: int | float | None = None,
                  now: float | None = None) -> dict:

    with job_authority.wrapper_control_lock(job_id):
        return _wrapper_write_locked(
            job_id,
            roadmap_id,
            state=state,
            step=step,
            exit_code=exit_code,
            pid=pid,
            kind=kind,
            owner=owner,
            script=script,
            pgid_file=pgid_file,
            resource_class=resource_class,
            authoritative_work_status=authoritative_work_status,
            diagnostic_only=diagnostic_only,
            wrapper_launch_id=wrapper_launch_id,
            terminal_states=terminal_states,
            heartbeat_marker=heartbeat_marker,
            heartbeat_s=heartbeat_s,
            now=now,
        )


def _wrapper_write_locked(job_id: str, roadmap_id: str, *, state: str, step: str,
                          exit_code: int | None = None, pid: int | None = None,
                          kind: str | None = None, owner: str | None = None,
                          script: str | None = None, pgid_file: str | Path | None = None,
                          resource_class: str | None = None,
                          authoritative_work_status: bool = False,
                          diagnostic_only: bool = False,
                          wrapper_launch_id: str | None = None,
                          terminal_states: set[str] | None = None,
                          heartbeat_marker: str = "__heartbeat__",
                          heartbeat_s: int | float | None = None,
                          now: float | None = None) -> dict:
    rid = _require_roadmap(roadmap_id)
    now = time.time() if now is None else now
    cur = _read(job_id)
    sidecar_path = _path(job_id)
    terminal = _terminal_set(terminal_states or {"done", "failed"})
    is_heartbeat = state == "running" and step == heartbeat_marker
    is_start = _state(state) == "running" and not is_heartbeat
    wrapper_started_at = _wrapper_started_at(now)
    control = job_authority.observe_control(job_id)
    supplied_raw = wrapper_launch_id or os.environ.get("COORD_WRAPPER_LAUNCH_ID")
    supplied_launch_id = job_authority.normalize_launch_id(supplied_raw)
    if supplied_raw and supplied_launch_id is None:
        raise WrapperPolicyError("wrapper_launch_id must be 32 lowercase hex characters")
    if (
        not is_start
        and control.state == "valid"
        and _num(os.environ.get("COORD_WRAPPER_STARTED_AT")) is None
    ):
        wrapper_started_at = float(control.wrapper_started_at)

    fresh_start = False
    fresh_attempt: int | None = None
    claim_binding: dict[str, Any] | None = None
    if is_start:
        same_wrapper = (
            control.state == "valid"
            and supplied_launch_id is not None
            and control.launch_id == supplied_launch_id
            and control.wrapper_started_at == wrapper_started_at
        )
        if same_wrapper:
            if (
                control.diagnostic_only != bool(diagnostic_only)
                or control.roadmap_id != rid
                or control.sidecar_path != job_authority.canonical_sidecar_path(sidecar_path)
            ):
                return cur
            launch_id = str(control.launch_id)
        else:
            if control.state == "invalid":
                raise WrapperPolicyError(
                    f"managed control is invalid for job_id {job_id!r}; refusing new attempt"
                )
            recorded_pid = (
                control.wrapper_pid
                if control.state == "valid"
                else cur.get("child_pid") or cur.get("pid") or cur.get("wrapper_pid")
            )
            if _pid_alive(recorded_pid):
                raise WrapperPolicyError(
                    f"concurrent wrapper start refused for live job_id {job_id!r}"
                )
            launch_id = supplied_launch_id or job_authority.new_launch_id()
            fresh_start = True
            fresh_attempt = (_int(control.attempt) or _int(cur.get("attempt")) or 0) + 1
            if not diagnostic_only and authoritative_work_status:
                claim_binding = _running_claim_binding(rid, at=now)
    elif control.state == "invalid":
        return cur
    elif control.state == "valid":
        if (
            supplied_launch_id != control.launch_id
            or
            control.wrapper_started_at != wrapper_started_at
            or control.diagnostic_only != bool(diagnostic_only)
            or control.roadmap_id != rid
            or control.sidecar_path != job_authority.canonical_sidecar_path(sidecar_path)
        ):
            return cur
        launch_id = str(control.launch_id)
    else:
        return cur

    resets_terminal = _wrapper_start_resets_terminal(
        cur, state, step, terminal_states=terminal,
        heartbeat_marker=heartbeat_marker, now=now,
    )
    if _wrapper_started_before_current_run(cur, now):
        return cur
    if not resets_terminal and _terminal_fenced(cur, state, terminal_states=terminal):
        return cur
    is_terminal = _state(state) in terminal
    preserve_history = is_heartbeat or is_terminal
    started_at = (
        float(
            cur.get("started_at")
            or cur.get("created_at")
            or (control.wrapper_started_at if control.state == "valid" else wrapper_started_at)
        )
        if preserve_history else wrapper_started_at
    )
    created_at = cur.get("created_at") if (preserve_history and cur.get("created_at")) else started_at

    payload = dict(cur) if is_heartbeat else {}
    payload.update({
        "roadmap_id": rid,
        "job_id": job_id,
        "state": state,
        "step": cur.get("step") if is_heartbeat else step,
        "created_at": created_at,
        "started_at": started_at,
        "updated_at": now,
        "runtime_s": max(0.0, now - float(started_at)),
        "wrapper_managed": True,
        "wrapper_launch_id": launch_id,
        "wrapper_pid": int(control.wrapper_pid or pid or os.getpid()) if not fresh_start else int(pid or os.getpid()),
        "wrapper_started_at": wrapper_started_at,
        "canonical_progress_path": _canonical_path(sidecar_path),
        **tracked_control_paths(job_id),
    })
    if diagnostic_only:
        payload["diagnostic_only"] = True
        payload.pop("authoritative_work_status", None)
    else:
        payload["diagnostic_only"] = False
        if authoritative_work_status:
            payload["authoritative_work_status"] = True
        else:
            payload.pop("authoritative_work_status", None)
    if pid is not None:
        payload["pid"] = int(pid)
        prev_pid = _int(cur.get("pid"))
        if prev_pid == int(pid) and cur.get("pid_started_at") is not None:
            # same pid as last write -- reuse the recorded start time rather
            # than re-shelling out to `ps` on every heartbeat.
            payload["pid_started_at"] = cur.get("pid_started_at")
        else:
            from coordharness.coord import process_liveness
            payload["pid_started_at"] = process_liveness.pid_start_time(pid)
    if kind:
        payload["kind"] = kind
    if resource_class:
        payload["resource_class"] = resource_class
    if script is not None:
        payload["script"] = script
    if heartbeat_s is not None:
        payload["heartbeat_s"] = max(1, int(float(heartbeat_s)))
    _copy_pgid_file(payload, pgid_file)

    normalized_owner = str(owner or "").strip().lower()
    if normalized_owner in {"claude", "codex", "operator"}:
        payload["owner"] = normalized_owner
        payload["assignee"] = normalized_owner
        payload["agent_owner"] = normalized_owner
        payload["launched_by"] = normalized_owner

    if is_heartbeat:
        payload["runner_heartbeat_at"] = now
        if pid is not None:
            payload["runner_pid"] = int(pid)

    if preserve_history:
        for key in _CHILD_PROGRESS_KEYS:
            if key in cur:
                payload[key] = cur.get(key)

    runtime_s = float(payload.get("runtime_s") or 0)
    done_f = _progress_num(payload, "done", "rows_done")
    total_f = _progress_num(payload, "total")
    if done_f is not None and total_f and done_f > 0 and runtime_s > 0:
        derived_rate = done_f / runtime_s
        if payload.get("rate") is None and payload.get("docs_per_s") is None:
            payload["rate"] = round(derived_rate, 3)
        if payload.get("eta_s") is None:
            payload["eta_s"] = int(max(total_f - done_f, 0) / max(derived_rate, 1e-6))

    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
    effective_state = state
    partial_terminal = (
        is_terminal
        and _state(state) == "done"
        and done_f is not None
        and total_f is not None
        and total_f > 0
        and done_f < total_f
    )
    if partial_terminal:
        effective_state = "failed"
        payload["state"] = effective_state
        payload["partial_terminal"] = True
        payload["reason"] = f"wrapper exited done with partial progress {int(done_f)}/{int(total_f)}"
        payload["blocked_by"] = payload["reason"]
    if is_terminal:
        payload["terminal_at"] = now
        payload["attempt"] = control.attempt or cur.get("attempt") or 1
        payload["last_progress_at"] = cur.get("last_progress_at") or cur.get("updated_at") or now
        if effective_state == "done":
            payload["pct"] = 100
        else:
            if cur.get("pct") is not None:
                payload["pct"] = cur.get("pct")
            if step and step != heartbeat_marker:
                payload.setdefault("reason", step)
                payload.setdefault("blocked_by", step)
    elif is_start:
        payload["attempt"] = (
            fresh_attempt
            if fresh_start
            else (_int(control.attempt) or _int(cur.get("attempt")) or 1)
        )

    policy = _run_wrapper_write_policy(payload, heartbeat_marker=heartbeat_marker)
    if policy is not None:
        if policy.get("blocked"):
            raise WrapperPolicyError(f"sidecar_writer wrapper policy blocked write: {policy.get('block_reason')}")
        payload["policy"] = policy

    if fresh_start or (not is_start and control.state == "legacy"):
        job_authority.publish_wrapper_control(
            job_id=job_id,
            roadmap_id=rid,
            launch_id=launch_id,
            wrapper_started_at=wrapper_started_at,
            diagnostic_only=diagnostic_only,
            sidecar_path=sidecar_path,
            wrapper_pid=int(pid or os.getpid()),
            attempt=int(payload["attempt"]),
            claim_id=(str(claim_binding["claim_id"]) if claim_binding else None),
            claim_session_id=(
                str(claim_binding["session_id"]) if claim_binding else None
            ),
            now=now,
        )
    if diagnostic_only:
        job_authority.set_diagnostic_marker(
            sidecar_path,
            job_id=job_id,
            roadmap_id=rid,
            now=now,
        )
    _atomic_write(payload, sidecar_path)
    if not diagnostic_only:
        job_authority.clear_diagnostic_marker(sidecar_path)
    _record_wrapper_run_event(payload, heartbeat_marker=heartbeat_marker)
    if _state(payload.get("state")) == "running":
        current_control = job_authority.observe_control(job_id)
        _renew_bound_wrapper_claim(
            current_control,
            work_id=rid,
            at=now,
            heartbeat_s=heartbeat_s,
        )
    return payload


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m coordharness.jobs.sidecar_writer")
    sub = parser.add_subparsers(dest="cmd", required=True)
    wrap = sub.add_parser("wrapper-write", help="write a wrapper lifecycle sidecar")
    wrap.add_argument("--job-id", required=True)
    wrap.add_argument("--roadmap-id", required=True)
    wrap.add_argument("--state", required=True)
    wrap.add_argument("--step", required=True)
    wrap.add_argument("--exit-code", type=int, default=None)
    wrap.add_argument("--pid", type=int, default=None)
    wrap.add_argument("--kind", default=None)
    wrap.add_argument("--owner", default=None)
    wrap.add_argument("--script", default=None)
    wrap.add_argument("--pgid-file", default=None)
    wrap.add_argument("--resource-class", default=None)
    wrap.add_argument("--authoritative-work-status", action="store_true")
    wrap.add_argument("--diagnostic-only", action="store_true")
    wrap.add_argument("--wrapper-launch-id", default=None)
    wrap.add_argument("--terminal-state", action="append", default=None)
    wrap.add_argument("--heartbeat-s", type=float, default=None)
    reconcile = sub.add_parser(
        "reconcile-dead",
        help="fail closed while reconciling provably orphaned RUNNING telemetry",
    )
    reconcile.add_argument("--job-id", required=True)
    reconcile.add_argument("--stale-after-seconds", required=True, type=float)
    args = parser.parse_args(argv)
    if args.cmd == "wrapper-write":
        try:
            wrapper_write(
                args.job_id,
                args.roadmap_id,
                state=args.state,
                step=args.step,
                exit_code=args.exit_code,
                pid=args.pid,
                kind=args.kind,
                owner=args.owner,
                script=args.script,
                pgid_file=args.pgid_file,
                resource_class=args.resource_class,
                authoritative_work_status=bool(args.authoritative_work_status),
                diagnostic_only=bool(args.diagnostic_only),
                wrapper_launch_id=args.wrapper_launch_id,
                terminal_states=set(args.terminal_state) if args.terminal_state else None,
                heartbeat_s=args.heartbeat_s,
            )
        except WrapperPolicyError as exc:
            print(f"sidecar_writer: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "reconcile-dead":
        try:
            payload = reconcile_dead(
                args.job_id,
                stale_after_s=args.stale_after_seconds,
            )
        except DeadReconciliationError as exc:
            print(f"sidecar_writer: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "job_id": payload.get("job_id"),
                    "state": payload.get("state"),
                    "diagnostic_only": payload.get("diagnostic_only"),
                    "dead_reconciled_at": payload.get("dead_reconciled_at"),
                    "dead_reconciliation_reason": payload.get(
                        "dead_reconciliation_reason"
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    parser.error(f"unknown command: {args.cmd}")
    return 2


def main(argv: list[str] | None = None) -> int:
    return _cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
