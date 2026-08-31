#!/usr/bin/env python3

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from coordharness.jobs import roadmap_binding
from coordharness.jobs import sidecar_writer as launch_sidecar
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.coord.policy.pipeline import run_boundary_policy
from coordharness import config as harness_config


class ProcessGroupUnsupportedError(RuntimeError):
    """Raised when os.killpg is required but this platform does not
    provide it (there is no portable equivalent -- see docs/compatibility.md)."""


# os.killpg is used unconditionally below because there is no cross-platform
# equivalent of POSIX process-group signaling to fall back to. Probe once at
# import time so every call site fails with a named error instead of a bare
# AttributeError.
POSIX_PROCESS_GROUPS_AVAILABLE = hasattr(os, "killpg")


def _require_process_groups() -> None:
    if not POSIX_PROCESS_GROUPS_AVAILABLE:
        raise ProcessGroupUnsupportedError(
            "this platform has no os.killpg; tracked process-group "
            "signal/reap is POSIX-only and has no portable equivalent"
        )


def _job_progress_dir() -> Path:
    return harness_config.job_progress_dir()


@contextmanager
def _pinned_sidecar_coord_db(coord_path: Path):
    previous = os.environ.get("COORD_COORD_DB")
    os.environ["COORD_COORD_DB"] = str(coord_path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COORD_COORD_DB", None)
        else:
            os.environ["COORD_COORD_DB"] = previous


POLL_SECONDS = 1.0
GB = 1024 * 1024 * 1024
KIB = 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_ALLOWED_OPTION_NAMES = {
    "-B",
    "-E",
    "-I",
    "-O",
    "-OO",
    "-P",
    "-S",
    "-V",
    "-W",
    "-X",
    "-c",
    "-m",
    "-u",
    "-v",
    "--isolated",
    "--version",
}


def _option_name(token: str) -> str:
    name = token.split("=", 1)[0]
    if name.startswith("-W") and name != "-W":
        return "-W"
    if name.startswith("-X") and name != "-X":
        return "-X"
    return name


def _executable_class(command: list[str]) -> str:
    if not command:
        return "unknown"
    name = os.path.basename(command[0]).lower()
    if name.startswith(("python", "pypy")):
        return "python"
    if name in {"bash", "dash", "fish", "sh", "zsh"}:
        return "shell"
    if name in {"node", "nodejs", "deno", "bun"}:
        return "javascript"
    if name.startswith("ruby"):
        return "ruby"
    if name.startswith("perl"):
        return "perl"
    if name.startswith("java"):
        return "java"
    return "executable"


def _command_receipt(command: list[str], proc_pattern: str | None = None) -> dict:
    options = [_option_name(token) for token in command[1:] if token.startswith("-")]
    receipt = {
        "executable_class": _executable_class(command),
        "allowed_option_names": sorted({name for name in options if name in _ALLOWED_OPTION_NAMES}),
        "option_count": len(options),
        "argument_count": sum(1 for token in command[1:] if not token.startswith("-")),
    }
    del proc_pattern
    return receipt


def _group_alive(pgid: int) -> bool:
    _require_process_groups()
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_group_empty(pgid: int, *, deadline_s: float) -> bool:
    deadline = time.time() + max(0.0, deadline_s)
    while time.time() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.1)
    return not _group_alive(pgid)


def _child_rss_bytes(root_pid: int) -> int:
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,rss="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return 0

    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            kib = int(parts[2])
        except ValueError:
            continue
        rss[pid] = kib * KIB
        children.setdefault(ppid, []).append(pid)

    if root_pid not in rss:
        return 0

    total = 0
    stack = [root_pid]
    seen = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        total += rss.get(p, 0)
        stack.extend(children.get(p, []))
    return total


def _run_launch_policy(
    args: argparse.Namespace,
    command: list[str],
    *,
    coord_path: Path,
) -> dict:
    conn = connect(coord_path)
    try:
        return run_boundary_policy(
            boundary="job_launch",
            action="launch",
            work_id=args.roadmap_id,
            session_id=args.session_id,
            actor=args.owner,
            payload={
                "job_id": args.job_id,
                "claim_id": args.claim_id,
                "claim_fence": args.claim_fence,
                "run_event_category": "tool",
            },
            conn=conn,
        )
    finally:
        conn.close()


def _coord_run_id(job_id: str, launch_id: str) -> str:
    return f"job:{job_id}:{launch_id}"


def _resource_class_for_command(command: list[str]) -> str:
    joined = " ".join(command).lower()
    return "local_gpu" if "gpu_job.sh" in joined or "gpu" in joined else "local_cpu"


def _appear_coord_run(
    args: argparse.Namespace,
    command: list[str],
    *,
    coord_path: Path,
    progress_dir: Path,
    run_id: str,
    pid: int,
    pgid: int,
) -> None:
    conn = connect(coord_path)
    try:
        sidecar_path = str(progress_dir / f"{args.job_id}.json")
        resource_class = _resource_class_for_command(command)
        with coord_db.tx(conn):
            cursor = conn.execute(
                "UPDATE runs SET pid=?,pgid=?,resource_class=?,heartbeat_at=?,"
                "state='live',version=version+1 "
                "WHERE run_id=? AND work_id=? AND session_id=? AND state='reserved' "
                "AND runner_kind='local_job' AND progress_mode='sidecar' "
                "AND sidecar_path=?",
                (
                    pid,
                    pgid,
                    resource_class,
                    coord_db.db_now(conn),
                    run_id,
                    args.roadmap_id,
                    args.session_id,
                    sidecar_path,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("reserved run activation lost exact custody binding")
    finally:
        conn.close()


def _finalize_coord_run(coord_path: Path, run_id: str, state: str) -> str | None:
    try:
        conn = connect(coord_path)
        try:
            coord_db.finalize_run(
                conn,
                run_id,
                state="done" if state == "done" else "failed",
            )
        finally:
            conn.close()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _terminate_process_group(proc: subprocess.Popen, pgid: int, *, grace_s: float = 2.0) -> None:
    _require_process_groups()
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
    if not _wait_group_empty(pgid, deadline_s=grace_s):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        _wait_group_empty(pgid, deadline_s=1.0)
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="launch.py",
        description="Tracked-launch wrapper with a hard RSS cap (runaway guard).",
    )
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--roadmap-id", required=True)
    ap.add_argument(
        "--cap-gb",
        type=float,
        default=3.0,
        help="Hard RSS cap in GB for the child tree (default 3).",
    )
    ap.add_argument("--owner", default=harness_config.actor_name())
    ap.add_argument("--session-id", default=os.environ.get("COORD_SESSION_ID"))
    ap.add_argument("--claim-id", default=os.environ.get("COORD_CLAIM_ID"))
    ap.add_argument("--claim-fence", default=os.environ.get("COORD_CLAIM_FENCE"))
    ap.add_argument("--coord-db", default=None)
    ap.add_argument(
        "--proc-pattern", default=None, help="Optional runtime process matcher; never persisted."
    )
    ap.add_argument(
        "--total",
        type=int,
        default=0,
        help="Optional total units (sidecar field; pct stays wrapper-driven).",
    )
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw_argv:
        sep = raw_argv.index("--")
        wrapper_args, command = raw_argv[:sep], raw_argv[sep + 1 :]
    else:
        wrapper_args, command = raw_argv, []
    args = ap.parse_args(wrapper_args)

    if not command:
        ap.error("no command given; put the command after a bare `--`.")

    if not str(args.roadmap_id or "").strip() or not str(args.job_id or "").strip():
        print("launch.py: --roadmap-id and --job-id must both be non-empty", file=sys.stderr)
        return 2

    coord_path = roadmap_binding.resolve_coord_db_path(args.coord_db)
    binding_result = roadmap_binding.validate(
        work_id=args.roadmap_id,
        job_id=args.job_id,
        claim_id=args.claim_id,
        claim_fence=args.claim_fence,
        session_id=args.session_id,
        actor=args.owner,
        coord_db=coord_path,
    )
    if not binding_result.ok or binding_result.binding is None:
        print(f"launch.py: runtime binding rejected: {binding_result.reason}", file=sys.stderr)
        return 2
    binding = binding_result.binding
    args.owner = binding.actor
    args.session_id = binding.session_id
    args.claim_id = binding.claim_id
    args.claim_fence = binding.claim_fence
    coord_path = binding.coord_db

    progress_dir = _job_progress_dir()
    cap_bytes = args.cap_gb * GB
    command_receipt = _command_receipt(command, args.proc_pattern)
    executable_class = command_receipt["executable_class"]
    try:
        policy_result = _run_launch_policy(args, command, coord_path=coord_path)
    except Exception as exc:
        print(
            f"launch.py: policy failed before launch: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 2
    if policy_result.get("blocked"):
        print(
            f"launch.py: policy blocked launch: {policy_result.get('block_reason')}",
            file=sys.stderr,
        )
        return 2

    requested_launch_id = launch_sidecar.job_authority.new_launch_id()
    coord_run_id = _coord_run_id(args.job_id, requested_launch_id)
    reservation = roadmap_binding.reserve_run(
        work_id=args.roadmap_id,
        job_id=args.job_id,
        claim_id=args.claim_id,
        claim_fence=args.claim_fence,
        session_id=args.session_id,
        actor=args.owner,
        run_id=coord_run_id,
        sidecar_path=str(progress_dir / f"{args.job_id}.json"),
        resource_class=_resource_class_for_command(command),
        coord_db=coord_path,
    )
    if not reservation.ok:
        print(f"launch.py: runtime reservation rejected: {reservation.reason}", file=sys.stderr)
        return 2
    try:
        _require_process_groups()
    except ProcessGroupUnsupportedError as exc:
        print(f"launch.py: {exc}", file=sys.stderr)
        return 2
    try:
        os.makedirs(progress_dir, exist_ok=True)
        launch_sidecar.SIDECAR_DIR = progress_dir
    except OSError as exc:
        _finalize_coord_run(coord_path, coord_run_id, "failed")
        print(f"launch.py: sidecar directory creation failed: {exc}", file=sys.stderr)
        return 2

    start_t = time.time()
    peak_rss = 0
    finalized = {"done": False}
    sidecar_attempt = {"value": None}
    sidecar_launch_id = {"value": None}
    pgid_holder = {"pgid": None}

    def _telemetry(rss_bytes: int = 0, pid: int | None = None) -> dict:
        nonlocal peak_rss
        peak_rss = max(peak_rss, rss_bytes)
        return {
            "pid": pid if pid is not None else os.getpid(),
            "pgid": pgid_holder["pgid"],
            "cap_gb": args.cap_gb,
            "rss_gb": round(rss_bytes / GB, 3),
            "peak_rss_gb": round(peak_rss / GB, 3),
            "elapsed_s": round(time.time() - start_t, 1),
            "policy": policy_result,
            **command_receipt,
        }

    def update_sidecar(
        state: str, pct: int | None, step: str, rss_bytes: int = 0, pid: int | None = None
    ) -> None:
        with _pinned_sidecar_coord_db(coord_path):
            launch_sidecar.update(
                args.job_id,
                args.roadmap_id,
                state=state,
                pct=pct,
                done=(state == "done"),
                total=args.total,
                step=step,
                script=executable_class,
                owner=args.owner,
                proc_pattern=None,
                attempt=sidecar_attempt["value"],
                wrapper_launch_id=sidecar_launch_id["value"],
                **_telemetry(rss_bytes=rss_bytes, pid=pid),
            )

    proc: subprocess.Popen | None = None
    child_env = os.environ.copy()
    child_env["COORD_WRAPPER_LAUNCH_ID"] = requested_launch_id
    child_env["COORD_JOB_ID"] = args.job_id
    child_env["COORD_ROADMAP_ID"] = args.roadmap_id
    child_env["COORD_CLAIM_ID"] = args.claim_id
    child_env["COORD_CLAIM_FENCE"] = args.claim_fence
    child_env["COORD_SESSION_ID"] = args.session_id
    child_env["COORD_JOB_OWNER"] = args.owner
    try:
        with roadmap_binding.reserved_launch_guard(
            work_id=args.roadmap_id,
            job_id=args.job_id,
            claim_id=args.claim_id,
            claim_fence=args.claim_fence,
            session_id=args.session_id,
            actor=args.owner,
            run_id=coord_run_id,
            coord_db=coord_path,
        ):
            proc = subprocess.Popen(command, start_new_session=True, env=child_env)
    except roadmap_binding.ReservationGuardError as exc:
        if proc is not None:
            _terminate_process_group(proc, proc.pid)
        _finalize_coord_run(coord_path, coord_run_id, "failed")
        print(f"launch.py: reservation guard rejected launch: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        _finalize_coord_run(coord_path, coord_run_id, "failed")
        print(f"launch.py: launch failed before sidecar write: {exc}", file=sys.stderr)
        return 127

    child_pid = proc.pid
    pgid = child_pid
    pgid_holder["pgid"] = pgid
    try:
        _appear_coord_run(
            args,
            command,
            coord_path=coord_path,
            progress_dir=progress_dir,
            run_id=coord_run_id,
            pid=child_pid,
            pgid=pgid,
        )
    except Exception as exc:
        _terminate_process_group(proc, pgid)
        _finalize_coord_run(coord_path, coord_run_id, "failed")
        print(
            f"launch.py: coord run registration failed before sidecar write: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        with _pinned_sidecar_coord_db(coord_path):
            appeared = launch_sidecar.appear(
                args.job_id,
                args.roadmap_id,
                script=executable_class,
                total=args.total,
                owner=args.owner,
                proc_pattern=None,
                wrapper_launch_id=requested_launch_id,
            )
    except launch_sidecar.WrapperPolicyError as exc:
        _terminate_process_group(proc, pgid)
        _finalize_coord_run(coord_path, coord_run_id, "failed")
        print(f"launch.py: sidecar authority rejected launch: {exc}", file=sys.stderr)
        return 2
    sidecar_attempt["value"] = appeared.get("attempt")
    sidecar_launch_id["value"] = appeared.get("wrapper_launch_id")
    update_sidecar("running", None, f"launched pid={child_pid}", pid=child_pid)

    killed_for_cap = {"flag": False}

    def _signal_group(sig: int) -> None:
        _require_process_groups()
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _reap_group(grace_s: float = 4.0) -> None:
        _signal_group(signal.SIGTERM)
        if not _wait_group_empty(pgid, deadline_s=grace_s):
            _signal_group(signal.SIGKILL)
            _wait_group_empty(pgid, deadline_s=2.0)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def finalize(state: str, step: str, rss_bytes: int = 0) -> None:
        if finalized["done"]:
            return
        finalized["done"] = True
        with _pinned_sidecar_coord_db(coord_path):
            launch_sidecar.finalize(
                args.job_id,
                args.roadmap_id,
                state=state,
                step=step,
                total=args.total,
                script=executable_class,
                owner=args.owner,
                proc_pattern=None,
                attempt=sidecar_attempt["value"],
                wrapper_launch_id=sidecar_launch_id["value"],
                **_telemetry(rss_bytes=rss_bytes, pid=child_pid),
            )
        coord_error = _finalize_coord_run(coord_path, coord_run_id, state)
        if coord_error:
            print(f"launch.py: coord run finalize failed: {coord_error}", file=sys.stderr)

    def _on_term(signum, _frame):
        _reap_group()
        finalize("failed", f"wrapper received signal {signum}; child terminated")
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    try:
        while True:
            ret = proc.poll()
            if ret is not None:
                break
            rss = _child_rss_bytes(child_pid)
            if rss > cap_bytes:
                killed_for_cap["flag"] = True
                _reap_group(grace_s=2.0)
                peak_rss = max(peak_rss, rss)
                finalize(
                    "failed",
                    f"KILLED: exceeded {args.cap_gb:g} GB RSS cap (peak {peak_rss / GB:.2f} GB)",
                    rss_bytes=rss,
                )
                return 137
            update_sidecar(
                "running",
                None,
                f"running pid={child_pid} rss={rss / GB:.2f}GB/{args.cap_gb:g}GB",
                rss_bytes=rss,
                pid=child_pid,
            )
            time.sleep(POLL_SECONDS)
    finally:
        if proc.poll() is None and not killed_for_cap["flag"]:
            _reap_group()

    rc = proc.returncode
    if _group_alive(pgid):
        _reap_group()
        if rc == 0:
            finalize(
                "failed",
                f"root exited 0 but process group {pgid} still had live descendants; reaped",
            )
            return 124
    if rc == 0:
        finalize("done", "done")
    else:
        finalize("failed", f"child exited nonzero (code {rc})")
    return rc if rc is not None else 1


if __name__ == "__main__":
    sys.exit(main())
