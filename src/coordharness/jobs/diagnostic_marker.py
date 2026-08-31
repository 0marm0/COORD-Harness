from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX installs
    fcntl = None

from coordharness import config as harness_config


MARKER_DIRNAME = ".diagnostic_only"
LEGACY_NONAUTHORITATIVE_SNAPSHOT_NAMES: frozenset[str] = frozenset()


def configured_nonauthoritative_snapshot_names(
    *, env: Mapping[str, str] | None = None
) -> frozenset[str]:
    source = os.environ if env is None else env
    inline = str(source.get("COORD_NONAUTHORITATIVE_SNAPSHOT_NAMES_JSON") or "").strip()
    file_value = str(source.get("COORD_NONAUTHORITATIVE_SNAPSHOT_NAMES_FILE") or "").strip()
    if inline and file_value:
        raise ValueError(
            "configure only one of COORD_NONAUTHORITATIVE_SNAPSHOT_NAMES_JSON or FILE"
        )
    if not inline and not file_value:
        return LEGACY_NONAUTHORITATIVE_SNAPSHOT_NAMES
    if file_value:
        root = harness_config.project_root().resolve()
        path = Path(file_value).expanduser()
        candidate = path if path.is_absolute() else root / path
        resolved = candidate.resolve(strict=False)
        allowed = (root, harness_config.state_dir().resolve(strict=False))
        if not any(resolved == base or base in resolved.parents for base in allowed):
            raise ValueError("snapshot-name configuration escapes project and state roots")
        inline = resolved.read_text(encoding="utf-8")
    try:
        parsed = json.loads(inline)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot-name JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("snapshot-name configuration must be a JSON list")
    names: set[str] = set()
    for raw in parsed:
        name = str(raw).strip()
        if (
            not name
            or name != Path(name).name
            or Path(name).suffix.lower() != ".json"
            or len(name) > 255
        ):
            raise ValueError(f"invalid nonauthoritative snapshot basename {raw!r}")
        names.add(name)
    return frozenset(names)
def _control_dir_default() -> Path:
    # Resolved at call time, never at import: a module-level constant would
    # freeze whatever COORD_HOME said when Python first imported this file, so
    # a test that pins its state dir afterwards would still write job-control
    # records into the developer's real repository state — which is exactly
    # what happened, and a run killed mid-test then poisons every later run
    # with a live-looking managed sentinel until someone hand-deletes hashes.
    return harness_config.state_dir() / "job_control"


def configured_control_dir(*, env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    raw = str(source.get("COORD_JOB_CONTROL_DIR") or "").strip()
    if not raw:
        return _control_dir_default().resolve(strict=False)
    root = harness_config.project_root().resolve()
    path = Path(raw).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    state = harness_config.state_dir().resolve(strict=False)
    if not any(resolved == base or base in resolved.parents for base in (root, state)):
        raise ValueError("COORD_JOB_CONTROL_DIR escapes project and state roots")
    return resolved
_LAUNCH_RE = re.compile(r"^[0-9a-f]{32}$")


class WrapperControlLockError(RuntimeError):
    """Raised when the wrapper control lock cannot be honored on this platform."""


@dataclass(frozen=True)
class ControlObservation:
    state: str
    job_id: str | None = None
    launch_id: str | None = None
    diagnostic_only: bool | None = None
    wrapper_started_at: float | None = None
    roadmap_id: str | None = None
    sidecar_path: str | None = None
    wrapper_pid: int | None = None
    attempt: int | None = None
    claim_binding_state: str | None = None
    claim_id: str | None = None
    claim_session_id: str | None = None
    record_signature: tuple[int, int, int, int, int, int, int] | None = None
    sentinel_signature: tuple[int, int, int, int, int, int, int] | None = None

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.state,
            self.job_id,
            self.launch_id,
            self.diagnostic_only,
            self.wrapper_started_at,
            self.roadmap_id,
            self.sidecar_path,
            self.wrapper_pid,
            self.attempt,
            self.claim_binding_state,
            self.claim_id,
            self.claim_session_id,
            self.record_signature,
            self.sentinel_signature,
        )


@dataclass(frozen=True)
class AuthorityObservation:
    marker_present: bool
    marker_signature: tuple[int, int, int, int, int, int, int] | None
    control: ControlObservation
    sidecar_safe: bool
    sidecar_signature: tuple[int, int, int, int, int, int, int] | None

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.marker_present,
            self.marker_signature,
            self.sidecar_safe,
            self.sidecar_signature,
            *self.control.fingerprint,
        )


@dataclass(frozen=True)
class AuthorityEvaluation:
    diagnostic_only: bool
    managed: bool
    stable: bool
    launch_id: str | None
    control_state: str
    marker_present: bool
    claim_binding_state: str | None = None
    claim_id: str | None = None
    claim_session_id: str | None = None


def _canonical_parent(path: str | Path) -> Path:
    return Path(os.path.realpath(str(Path(path).parent)))


def diagnostic_marker_path(sidecar_path: str | Path) -> Path:
    path = Path(sidecar_path)
    return _canonical_parent(path) / MARKER_DIRNAME / f"{path.name}.marker"


def _lstat_presence(path: Path) -> tuple[bool, bool]:

    try:
        os.lstat(path)
        return True, False
    except FileNotFoundError:
        return False, False
    except OSError:
        return True, True


def _regular_file_signature(
    path: Path, *, single_link: bool = False
) -> tuple[int, int, int, int, int, int, int] | None:

    try:
        value = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(value.st_mode):
        return None
    if single_link and int(value.st_nlink) != 1:
        return None
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_size),
    )


def canonical_sidecar_path(path: str | Path) -> str:

    normalized = Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    return str(Path(os.path.realpath(str(normalized.parent))) / normalized.name)


def diagnostic_marker_present(sidecar_path: str | Path) -> bool:
    return _lstat_presence(diagnostic_marker_path(sidecar_path))[0]


def is_reserved_legacy_snapshot(sidecar_path: str | Path) -> bool:

    path = Path(sidecar_path)
    return (
        path.name in configured_nonauthoritative_snapshot_names()
        and _canonical_parent(path).name == "job_progress"
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"), allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_diagnostic_marker(
    sidecar_path: str | Path,
    *,
    job_id: str,
    roadmap_id: str,
    now: float | None = None,
) -> Path:
    marker = diagnostic_marker_path(sidecar_path)
    _atomic_json(
        marker,
        {
            "schema_version": 1,
            "diagnostic_only": True,
            "job_id": str(job_id),
            "roadmap_id": str(roadmap_id),
            "sidecar_name": Path(sidecar_path).name,
            "marked_at": float(time.time() if now is None else now),
        },
    )
    return marker


def clear_diagnostic_marker(sidecar_path: str | Path) -> None:
    marker = diagnostic_marker_path(sidecar_path)
    try:
        marker.unlink()
    except FileNotFoundError:
        return


def _job_key(job_id: str) -> str:
    text = str(job_id or "").strip()
    if not text:
        raise ValueError("job_id is required for wrapper authority")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def control_record_path(job_id: str) -> Path:
    return configured_control_dir() / "records" / f"{_job_key(job_id)}.json"


def managed_sentinel_path(job_id: str) -> Path:
    return configured_control_dir() / "managed" / f"{_job_key(job_id)}.managed"


def _control_lock_path(job_id: str) -> Path:
    return configured_control_dir() / "locks" / f"{_job_key(job_id)}.lock"


@contextmanager
def wrapper_control_lock(job_id: str) -> Iterator[None]:
    if fcntl is None:
        # publish_wrapper_control() / _ensure_managed_sentinel() and their
        # callers in sidecar_writer.py depend on this lock to serialize
        # concurrent wrapper launches for the same job_id -- without it,
        # two launches racing the same control record/sentinel pair could
        # interleave writes and hand out inconsistent custody. There is no
        # non-POSIX substitute, so refuse rather than let callers write
        # unprotected.
        raise WrapperControlLockError(
            "operating-system file locks are unavailable on this platform; "
            "wrapper_control_lock() cannot serialize concurrent job-control "
            "writes without it"
        )
    path = _control_lock_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def new_launch_id() -> str:
    return secrets.token_hex(16)


def normalize_launch_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _LAUNCH_RE.fullmatch(text) else None


def _ensure_managed_sentinel(job_id: str) -> None:
    path = managed_sentinel_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    try:
        os.write(fd, (str(job_id).strip() + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_wrapper_control(
    *,
    job_id: str,
    roadmap_id: str,
    launch_id: str,
    wrapper_started_at: float,
    diagnostic_only: bool,
    sidecar_path: str | Path,
    wrapper_pid: int,
    attempt: int,
    claim_id: str | None = None,
    claim_session_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    job_id = str(job_id or "").strip()
    roadmap_id = str(roadmap_id or "").strip()
    if not job_id or not roadmap_id:
        raise ValueError("job_id and roadmap_id are required for wrapper authority")
    if not _LAUNCH_RE.fullmatch(str(launch_id or "")):
        raise ValueError("launch_id must be 32 lowercase hex characters")
    try:
        owner_pid = int(wrapper_pid)
    except (TypeError, ValueError):
        raise ValueError("wrapper_pid must be a positive integer") from None
    if owner_pid <= 0:
        raise ValueError("wrapper_pid must be a positive integer")
    try:
        attempt_number = int(attempt)
    except (TypeError, ValueError):
        raise ValueError("attempt must be a positive integer") from None
    if attempt_number <= 0:
        raise ValueError("attempt must be a positive integer")
    started = float(wrapper_started_at)
    if not math.isfinite(started) or started <= 0:
        raise ValueError("wrapper_started_at must be a positive finite timestamp")
    _ensure_managed_sentinel(job_id)
    clean_claim_id = str(claim_id or "").strip()
    clean_claim_session_id = str(claim_session_id or "").strip()
    if bool(clean_claim_id) != bool(clean_claim_session_id):
        raise ValueError("claim_id and claim_session_id must be supplied together")
    payload = {
        "schema_version": 2,
        "job_id": job_id,
        "roadmap_id": roadmap_id,
        "launch_id": launch_id,
        "wrapper_started_at": started,
        "diagnostic_only": bool(diagnostic_only),
        "sidecar_path": canonical_sidecar_path(sidecar_path),
        "wrapper_pid": owner_pid,
        "attempt": attempt_number,
        "claim_binding_state": "bound" if clean_claim_id else "unbound",
        "claim_id": clean_claim_id or None,
        "claim_session_id": clean_claim_session_id or None,
        "updated_at": float(time.time() if now is None else now),
    }
    _atomic_json(control_record_path(job_id), payload)
    return payload


def _valid_control_payload(payload: Any, expected_job_id: str) -> bool:
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
        return False
    if str(payload.get("job_id") or "").strip() != expected_job_id:
        return False
    if not _LAUNCH_RE.fullmatch(str(payload.get("launch_id") or "")):
        return False
    if not isinstance(payload.get("diagnostic_only"), bool):
        return False
    roadmap_id = str(payload.get("roadmap_id") or "").strip()
    sidecar_path = str(payload.get("sidecar_path") or "").strip()
    if not roadmap_id or not sidecar_path or not os.path.isabs(sidecar_path):
        return False
    try:
        wrapper_pid = int(payload.get("wrapper_pid"))
    except (TypeError, ValueError):
        return False
    if wrapper_pid <= 0:
        return False
    try:
        attempt = int(payload.get("attempt"))
    except (TypeError, ValueError):
        return False
    if attempt <= 0:
        return False
    try:
        started = float(payload.get("wrapper_started_at"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(started) or started <= 0:
        return False
    if payload.get("schema_version") == 2:
        binding_state = str(payload.get("claim_binding_state") or "")
        claim_id = str(payload.get("claim_id") or "").strip()
        session_id = str(payload.get("claim_session_id") or "").strip()
        if binding_state not in {"bound", "unbound"}:
            return False
        if binding_state == "bound" and (not claim_id or not session_id):
            return False
        if binding_state == "unbound" and (claim_id or session_id):
            return False
    return True


def observe_control(*job_ids: str) -> ControlObservation:
    seen: set[str] = set()
    for raw_job_id in job_ids:
        job_id = str(raw_job_id or "").strip()
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        sentinel_present, sentinel_uncertain = _lstat_presence(managed_sentinel_path(job_id))
        record_present, record_uncertain = _lstat_presence(control_record_path(job_id))
        if not sentinel_present and not record_present:
            continue
        if sentinel_uncertain or record_uncertain or not sentinel_present or not record_present:
            return ControlObservation(state="invalid", job_id=job_id)
        sentinel_signature_before = _regular_file_signature(
            managed_sentinel_path(job_id), single_link=True
        )
        record_signature_before = _regular_file_signature(
            control_record_path(job_id), single_link=True
        )
        if sentinel_signature_before is None or record_signature_before is None:
            return ControlObservation(state="invalid", job_id=job_id)
        try:
            sentinel_value = managed_sentinel_path(job_id).read_text(encoding="utf-8").strip()
            payload = json.loads(control_record_path(job_id).read_text(encoding="utf-8"))
        except Exception:
            return ControlObservation(state="invalid", job_id=job_id)
        sentinel_signature_after = _regular_file_signature(
            managed_sentinel_path(job_id), single_link=True
        )
        record_signature_after = _regular_file_signature(
            control_record_path(job_id), single_link=True
        )
        if (
            sentinel_value != job_id
            or sentinel_signature_after is None
            or sentinel_signature_after != sentinel_signature_before
            or record_signature_after is None
            or record_signature_after != record_signature_before
        ):
            return ControlObservation(state="invalid", job_id=job_id)
        if not _valid_control_payload(payload, job_id):
            return ControlObservation(state="invalid", job_id=job_id)
        return ControlObservation(
            state="valid",
            job_id=job_id,
            launch_id=str(payload["launch_id"]),
            diagnostic_only=bool(payload["diagnostic_only"]),
            wrapper_started_at=float(payload["wrapper_started_at"]),
            roadmap_id=str(payload.get("roadmap_id") or ""),
            sidecar_path=canonical_sidecar_path(str(payload["sidecar_path"])),
            wrapper_pid=int(payload["wrapper_pid"]),
            attempt=int(payload["attempt"]),
            claim_binding_state=(
                str(payload.get("claim_binding_state"))
                if payload.get("schema_version") == 2 else "legacy"
            ),
            claim_id=str(payload.get("claim_id") or "") or None,
            claim_session_id=str(payload.get("claim_session_id") or "") or None,
            record_signature=record_signature_after,
            sentinel_signature=sentinel_signature_after,
        )
    return ControlObservation(state="legacy")


def observe_authority(sidecar_path: str | Path, *, job_id: str | None = None) -> AuthorityObservation:
    path = Path(sidecar_path)
    control = observe_control(str(job_id or ""), path.stem)
    marker = diagnostic_marker_path(path)
    marker_present, _marker_uncertain = _lstat_presence(marker)
    sidecar_signature = _regular_file_signature(path, single_link=True)
    return AuthorityObservation(
        marker_present=marker_present,
        marker_signature=_regular_file_signature(marker),
        control=control,
        sidecar_safe=sidecar_signature is not None,
        sidecar_signature=sidecar_signature,
    )


def evaluate_authority(
    sidecar: dict[str, Any],
    sidecar_path: str | Path,
    *,
    before: AuthorityObservation | None = None,
) -> AuthorityEvaluation:
    after = observe_authority(sidecar_path, job_id=str(sidecar.get("job_id") or ""))
    stable = before is None or before.fingerprint == after.fingerprint
    control = after.control
    managed = control.state != "legacy"
    diagnostic = bool(sidecar.get("diagnostic_only") is True)
    diagnostic = diagnostic or is_reserved_legacy_snapshot(sidecar_path)
    diagnostic = diagnostic or after.marker_present or (before.marker_present if before else False)
    if not stable:
        diagnostic = True
    if (
        control.state != "legacy" or sidecar.get("wrapper_managed") is True
    ) and (not after.sidecar_safe or (before is not None and not before.sidecar_safe)):
        diagnostic = True
    if control.state == "invalid":
        diagnostic = True
    elif control.state == "valid":
        if control.diagnostic_only is True:
            diagnostic = True
        elif not (
            sidecar.get("wrapper_managed") is True
            and str(sidecar.get("job_id") or "").strip() == control.job_id
            and str(sidecar.get("wrapper_launch_id") or "") == control.launch_id
            and str(sidecar.get("roadmap_id") or "").strip() == control.roadmap_id
            and canonical_sidecar_path(sidecar_path) == control.sidecar_path
        ):
            diagnostic = True
    elif sidecar.get("wrapper_managed") is True:
        diagnostic = True
    return AuthorityEvaluation(
        diagnostic_only=diagnostic,
        managed=managed,
        stable=stable,
        launch_id=control.launch_id if control.state == "valid" else None,
        control_state=control.state,
        marker_present=after.marker_present or (before.marker_present if before else False),
        claim_binding_state=(control.claim_binding_state if control.state == "valid" else None),
        claim_id=(control.claim_id if control.state == "valid" else None),
        claim_session_id=(control.claim_session_id if control.state == "valid" else None),
    )


def read_sidecar_with_authority(
    sidecar_path: str | Path,
) -> tuple[dict[str, Any] | None, AuthorityEvaluation | None]:
    path = Path(sidecar_path)
    before = observe_authority(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return payload, evaluate_authority(payload, path, before=before)


def authority_cache_stamp(sidecar_dir: str | Path) -> tuple[tuple, ...]:

    paths = (
        _canonical_parent(Path(sidecar_dir) / "placeholder") / MARKER_DIRNAME,
        configured_control_dir() / "records",
        configured_control_dir() / "managed",
    )
    out: list[tuple] = []
    for path in paths:
        try:
            value = path.stat()
            out.append((
                os.path.realpath(str(path)),
                int(value.st_ino), int(value.st_mode), int(value.st_nlink),
                int(value.st_mtime_ns), int(value.st_ctime_ns), int(value.st_size),
            ))
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        child = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    out.append((
                        os.path.abspath(entry.path),
                        int(child.st_ino), int(child.st_mode), int(child.st_nlink),
                        int(child.st_mtime_ns), int(child.st_ctime_ns), int(child.st_size),
                    ))
        except OSError:
            out.append((os.path.realpath(str(path)), -1, -1, -1, -1, -1, -1))
    return tuple(sorted(out))
