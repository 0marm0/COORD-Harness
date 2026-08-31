#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX installs
    fcntl = None


_CUSTODY_SCHEMA = "coordharness.tracked-job-custody.v1"
_TERMINAL_STATES = {
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
_CUSTODY_EXACT_FIELDS = {
    "pgid",
    "child_pid",
    "wrapper_pid",
    "child_started_at",
    "progress_file",
    "custody_schema",
    "wrapper_ppid",
    "wrapper_started_at",
    "launcher_pid",
    "launcher_ppid",
    "launcher_started_at",
    "child_ppid",
    "child_pgid",
    "process_ancestry",
    "original_argv",
    "original_argv_sha256",
    "resolved_argv",
    "resolved_argv_sha256",
    "child_argv",
    "child_argv_sha256",
    "canonical_wrapper_path",
    "wrapper_sha256",
    "canonical_launcher_path",
    "launcher_sha256",
    "canonical_launch_cwd",
    "canonical_progress_path",
    "canonical_pgid_path",
    "canonical_coord_db_path",
    "canonical_release_path",
    "release_token_sha256",
    "execution_token_sha256",
    "workload_release_state",
    "workload_released_at",
    "canonical_control_record_path",
    "canonical_control_sentinel_path",
    "canonical_control_lock_path",
}


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _release_phase_token(launch_id: str, phase: str) -> str:
    if phase not in {"authorize", "execute"}:
        raise ValueError(f"unsupported release phase: {phase}")
    value = f"coordharness.tracked-job-release.v1:{launch_id}:{phase}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_path(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


def _resolve_argv(argv: list[str], *, cwd: str | None = None) -> list[str]:

    if not argv:
        return []
    base = Path(cwd or os.getcwd())
    resolved: list[str] = []
    for index, raw in enumerate(argv):
        value = str(raw)
        if index == 0:
            candidate = shutil.which(value) or value
            path = Path(candidate).expanduser()
            if path.is_absolute() or path.exists() or os.sep in value:
                value = str(path.resolve())
        elif value and not value.startswith("-"):
            path = Path(value).expanduser()
            candidate = path if path.is_absolute() else base / path
            if path.is_absolute() or os.sep in value or candidate.exists():
                if candidate.exists():
                    value = str(candidate.resolve())
        resolved.append(value)
    return resolved


def _parse_original_argv(raw: str, fallback: list[str]) -> list[str]:
    if not raw:
        return list(fallback)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--original-argv-json must be valid JSON") from exc
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("--original-argv-json must be a non-empty string array")
    return value


def _atomic_write(path: str, obj: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(obj, handle)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_group_empty(pgid: int, timeout_s: float) -> bool:
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return not _group_alive(pgid)


def _waitpid_status(child_pid: int, *, nohang: bool = False) -> int | None:

    options = os.WNOHANG if nohang else 0
    try:
        waited_pid, status = os.waitpid(child_pid, options)
    except ChildProcessError:
        return 0
    if waited_pid == 0:
        return None
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 125


def _reap_group(pgid: int, child_pid: int, grace: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline:
        if not _group_alive(pgid):
            break
        time.sleep(0.1)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if _waitpid_status(child_pid, nohang=True) is not None:
            return
        time.sleep(0.05)
    _waitpid_status(child_pid, nohang=True)


def _fork_gated_child(cmd: list[str]) -> tuple[int, int]:

    release_read, release_write = os.pipe()
    ready_read, ready_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(release_write)
            os.close(ready_read)
            os.setsid()
            os.write(ready_write, b"1")
            os.close(ready_write)
            released = os.read(release_read, 1)
            os.close(release_read)
            if released != b"1":
                os._exit(125)
            os.execvp(cmd[0], cmd)
        except BaseException as exc:
            try:
                os.write(2, f"_tracked_pglaunch: child exec failed: {exc}\n".encode())
            except OSError:
                pass
            os._exit(127)
    os.close(release_read)
    os.close(ready_write)
    try:
        if os.read(ready_read, 1) != b"1":
            raise RuntimeError("gated child did not establish its process group")
    finally:
        os.close(ready_read)
    return child_pid, release_write


def _wait_for_release(
    path: str, token: str, child_pid: int, wrapper_pid: int = 0
) -> bool:

    while True:
        if wrapper_pid and not _wrapper_parent_alive(wrapper_pid):
            return False
        if _waitpid_status(child_pid, nohang=True) is not None:
            return False
        fd = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            opened = os.fstat(fd)
            linked = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != linked.st_dev
                or opened.st_ino != linked.st_ino
            ):
                print(
                    "_tracked_pglaunch: release token path identity is not exact",
                    file=sys.stderr,
                )
                return False
            observed = os.read(fd, 4097).decode("utf-8").strip()
            if len(observed) > 4096 or observed != token:
                print(
                    "_tracked_pglaunch: release token is foreign to this phase",
                    file=sys.stderr,
                )
                return False
            os.unlink(path)
            if os.fstat(fd).st_nlink != 0:
                print(
                    "_tracked_pglaunch: consumed token inode is still linked",
                    file=sys.stderr,
                )
                return False
            if wrapper_pid and not _wrapper_parent_alive(wrapper_pid):
                print(
                    "_tracked_pglaunch: wrapper parent disappeared during token consumption",
                    file=sys.stderr,
                )
                return False
            return True
        except (OSError, UnicodeError) as exc:
            if fd >= 0:
                print(
                    "_tracked_pglaunch: release token could not be consumed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return False
        finally:
            try:
                if fd >= 0:
                    os.close(fd)
            except OSError:
                pass
        time.sleep(0.01)


def _wrapper_parent_alive(wrapper_pid: int) -> bool:

    return wrapper_pid > 0 and os.getppid() == wrapper_pid


def _release_child_gate(release_write: int, wrapper_pid: int) -> bool:

    if wrapper_pid and not _wrapper_parent_alive(wrapper_pid):
        return False
    os.write(release_write, b"1")
    return not wrapper_pid or _wrapper_parent_alive(wrapper_pid)


_fcntl_unavailable_logged = False


def _terminalize_wrapper_loss(
    progress_file: str, payload: dict[str, object], *, exit_code: int = 125
) -> bool:

    lock_path = str(payload.get("canonical_control_lock_path") or "")
    if not progress_file or not lock_path:
        return False
    if fcntl is None:
        # This recovery path mutates the shared progress-file sidecar
        # under an exclusive lock so a concurrent wrapper/launcher pair
        # cannot race the same write. Without the lock there is no way
        # to make that write safe, so we report "did not terminalize"
        # (the same outcome callers already handle on any other failure
        # to acquire/verify the lock) rather than writing unprotected.
        global _fcntl_unavailable_logged
        if not _fcntl_unavailable_logged:
            _fcntl_unavailable_logged = True
            print(
                "_tracked_pglaunch: 'fcntl' is unavailable on this platform; "
                "wrapper-loss terminalization cannot safely mutate the "
                "progress file and will be skipped",
                file=sys.stderr,
            )
        return False
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                with open(progress_file, encoding="utf-8") as handle:
                    current = json.load(handle)
                if not isinstance(current, dict):
                    return False
                exact_launch = (
                    current.get("wrapper_launch_id")
                    == payload.get("wrapper_launch_id")
                    and current.get("wrapper_pid") == payload.get("wrapper_pid")
                    and current.get("wrapper_started_at")
                    == payload.get("wrapper_started_at")
                    and current.get("attempt") == payload.get("attempt")
                    and current.get("job_id") == payload.get("job")
                    and current.get("roadmap_id") == payload.get("roadmap_id")
                )
                launch_gap_sidecar = (
                    payload.get("workload_release_state") == "awaiting_wrapper"
                    and all(
                        current.get(key) is None
                        for key in (
                            "custody_schema",
                            "launcher_pid",
                            "child_pid",
                            "pgid",
                        )
                    )
                )
                if launch_gap_sidecar:
                    copied_custody_is_exact = all(
                        current.get(key) in (None, payload.get(key))
                        for key in _CUSTODY_EXACT_FIELDS
                    )
                else:
                    copied_custody_is_exact = all(
                        key not in payload
                        or (key in current and current.get(key) == payload.get(key))
                        for key in _CUSTODY_EXACT_FIELDS
                    )
                if not exact_launch or not copied_custody_is_exact:
                    return False
                if str(current.get("state") or "").strip().lower() in _TERMINAL_STATES:
                    return True
                now = time.time()
                started_at = float(
                    current.get("started_at")
                    or current.get("created_at")
                    or payload.get("wrapper_started_at")
                    or now
                )
                reason = "tracked wrapper parent disappeared; workload reaped"
                current.update(
                    {
                        "state": "failed",
                        "step": reason,
                        "reason": reason,
                        "blocked_by": reason,
                        "exit_code": int(exit_code),
                        "updated_at": now,
                        "terminal_at": now,
                        "runtime_s": max(0.0, now - started_at),
                    }
                )
                _atomic_write(progress_file, current)
                return True
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(
            "_tracked_pglaunch: could not terminalize wrapper loss: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pgid-file", required=True)
    parser.add_argument("--wrapper-pid", type=int, default=0)
    parser.add_argument("--wrapper-ppid", type=int, default=0)
    parser.add_argument("--wrapper-launch-id", default="")
    parser.add_argument("--wrapper-started-at", type=float, default=0.0)
    parser.add_argument("--wrapper-path", default="")
    parser.add_argument("--progress-file", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--roadmap-id", default="")
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--control-dir", default="")
    parser.add_argument("--control-record-path", default="")
    parser.add_argument("--control-sentinel-path", default="")
    parser.add_argument("--control-lock-path", default="")
    parser.add_argument("--original-argv-json", default="")
    parser.add_argument("--coord-db", default="")
    parser.add_argument("--release-file", default="")
    parser.add_argument("--release-token", default="")
    parser.add_argument("--execution-token", default="")
    parser.add_argument("--grace", type=float, default=4.0)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        print("_tracked_pglaunch: no command given after --", file=sys.stderr)
        return 2

    try:
        original_argv = _parse_original_argv(args.original_argv_json, cmd)
    except ValueError as exc:
        print(f"_tracked_pglaunch: {exc}", file=sys.stderr)
        return 2

    custody_enabled = bool(
        args.original_argv_json
        and args.wrapper_path
        and args.wrapper_launch_id
        and args.attempt > 0
    )
    custody_requested = bool(
        args.original_argv_json or args.wrapper_path or args.attempt > 0
    )
    if custody_requested and not custody_enabled:
        print(
            "_tracked_pglaunch: incomplete tracked-job custody arguments",
            file=sys.stderr,
        )
        return 2
    if custody_enabled and not all(
        (
            args.job,
            args.roadmap_id,
            args.progress_file,
            args.control_dir,
            args.coord_db,
            args.release_file,
            args.release_token,
            args.execution_token,
            args.wrapper_pid > 0,
            args.wrapper_ppid > 0,
            args.wrapper_started_at > 0,
        )
    ):
        print(
            "_tracked_pglaunch: tracked-job custody identity/path arguments are required",
            file=sys.stderr,
        )
        return 2
    launcher_started_at = time.time()
    launcher_pid = os.getpid()
    launcher_ppid = os.getppid()
    if custody_enabled and launcher_ppid != args.wrapper_pid:
        print(
            "_tracked_pglaunch: wrapper/launcher ancestry mismatch "
            f"({args.wrapper_pid=} {launcher_ppid=})",
            file=sys.stderr,
        )
        return 2
    if custody_enabled and (
        args.release_token
        != _release_phase_token(args.wrapper_launch_id, "authorize")
        or args.execution_token
        != _release_phase_token(args.wrapper_launch_id, "execute")
        or args.release_token == args.execution_token
    ):
        print(
            "_tracked_pglaunch: release tokens do not bind distinct launch phases",
            file=sys.stderr,
        )
        return 2

    static_custody: dict[str, object] = {}
    if custody_enabled:
        try:
            canonical_wrapper_path = _canonical_path(args.wrapper_path)
            canonical_launcher_path = _canonical_path(__file__)
            resolved_argv = _resolve_argv(original_argv)
            child_argv = list(cmd)
            static_custody = {
                "custody_schema": _CUSTODY_SCHEMA,
                "roadmap_id": args.roadmap_id,
                "attempt": args.attempt,
                "wrapper_ppid": args.wrapper_ppid,
                "wrapper_started_at": args.wrapper_started_at,
                "original_argv_sha256": _canonical_json_sha256(original_argv),
                "resolved_argv_sha256": _canonical_json_sha256(resolved_argv),
                "child_argv_sha256": _canonical_json_sha256(child_argv),
                "wrapper_sha256": _file_sha256(canonical_wrapper_path),
                "launcher_sha256": _file_sha256(canonical_launcher_path),
                "release_token_sha256": hashlib.sha256(
                    args.release_token.encode("utf-8")
                ).hexdigest(),
                "execution_token_sha256": hashlib.sha256(
                    args.execution_token.encode("utf-8")
                ).hexdigest(),
                "workload_release_state": "awaiting_wrapper",
            }
        except (OSError, ValueError) as exc:
            print(
                f"_tracked_pglaunch: cannot establish tracked custody: {exc}",
                file=sys.stderr,
            )
            return 2

    child_pid = 0
    release_write = -1

    def _handler(signum: int, _frame) -> None:
        if child_pid > 0:
            _reap_group(child_pid, child_pid, args.grace)
        raise SystemExit(143 if signum == signal.SIGTERM else 130)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    child_pid, release_write = _fork_gated_child(cmd)
    pgid = child_pid
    child_started_at = time.time()
    try:
        payload = {
            "pgid": pgid,
            "child_pid": child_pid,
            "wrapper_pid": args.wrapper_pid,
            "wrapper_launch_id": args.wrapper_launch_id,
            "progress_file": args.progress_file,
            "job": args.job,
            "child_started_at": child_started_at,
        }
        if custody_enabled:
            process_ancestry = {
                "wrapper": {"pid": args.wrapper_pid, "ppid": args.wrapper_ppid},
                "launcher": {"pid": launcher_pid, "ppid": launcher_ppid},
                "child": {
                    "pid": child_pid,
                    "ppid": launcher_pid,
                    "pgid": pgid,
                },
            }
            payload.update(
                {
                    **static_custody,
                    "launcher_pid": launcher_pid,
                    "launcher_ppid": launcher_ppid,
                    "launcher_started_at": launcher_started_at,
                    "child_ppid": launcher_pid,
                    "child_pgid": pgid,
                    "process_ancestry": process_ancestry,
                }
            )
        _atomic_write(
            args.pgid_file,
            payload,
        )
    except Exception as exc:
        level = "ERROR" if custody_enabled else "WARN"
        print(
            f"_tracked_pglaunch: {level} could not write pgid file: {exc}",
            file=sys.stderr,
        )
        if custody_enabled:
            _reap_group(pgid, child_pid, args.grace)
            return 2

    if custody_enabled:
        if not _wait_for_release(
            args.release_file,
            args.release_token,
            child_pid,
            args.wrapper_pid,
        ):
            wrapper_lost = not _wrapper_parent_alive(args.wrapper_pid)
            print(
                "_tracked_pglaunch: custody release was missing, foreign, or child exited",
                file=sys.stderr,
            )
            _reap_group(pgid, child_pid, args.grace)
            if wrapper_lost:
                _terminalize_wrapper_loss(args.progress_file, payload)
                return 125
            return 2
        try:
            payload["workload_release_state"] = "authorized"
            payload["workload_released_at"] = time.time()
            _atomic_write(args.pgid_file, payload)
        except Exception as exc:
            print(
                f"_tracked_pglaunch: ERROR could not authorize custody release: {exc}",
                file=sys.stderr,
            )
            _reap_group(pgid, child_pid, args.grace)
            return 2
        if not _wait_for_release(
            args.release_file,
            args.execution_token,
            child_pid,
            args.wrapper_pid,
        ):
            wrapper_lost = not _wrapper_parent_alive(args.wrapper_pid)
            print(
                "_tracked_pglaunch: converged custody release acknowledgement was missing",
                file=sys.stderr,
            )
            _reap_group(pgid, child_pid, args.grace)
            if wrapper_lost:
                _terminalize_wrapper_loss(args.progress_file, payload)
                return 125
            return 2
    try:
        gate_released = _release_child_gate(release_write, args.wrapper_pid)
    except OSError:
        _reap_group(pgid, child_pid, args.grace)
        return 2
    finally:
        try:
            os.close(release_write)
        except OSError:
            pass
    if not gate_released:
        print(
            "_tracked_pglaunch: wrapper parent disappeared at child release",
            file=sys.stderr,
        )
        _reap_group(pgid, child_pid, args.grace)
        _terminalize_wrapper_loss(args.progress_file, payload)
        return 125

    rc = None
    while rc is None:
        rc = _waitpid_status(child_pid, nohang=True)
        if rc is not None:
            break
        if custody_enabled and not _wrapper_parent_alive(args.wrapper_pid):
            print(
                "_tracked_pglaunch: wrapper parent disappeared; reaping workload",
                file=sys.stderr,
            )
            _reap_group(pgid, child_pid, args.grace)
            _terminalize_wrapper_loss(args.progress_file, payload)
            return 125
        time.sleep(0.05)
    if rc is None:
        rc = 125
    if not _wait_group_empty(pgid, 1.0):
        print(
            f"_tracked_pglaunch: WARN root exited with {rc} but process group {pgid} "
            "still had live descendants; reaping before exit",
            file=sys.stderr,
        )
        _reap_group(pgid, child_pid, args.grace)
        if rc == 0:
            return 124
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
