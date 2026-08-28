#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time


def _atomic_write(path: str, obj: dict) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.write("\n")
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


def _reap_group(pgid: int, proc: subprocess.Popen, grace: float) -> None:
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
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pgid-file", required=True)
    ap.add_argument("--wrapper-pid", type=int, default=0)
    ap.add_argument("--progress-file", default="")
    ap.add_argument("--job", default="")
    ap.add_argument("--grace", type=float, default=4.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()

    cmd = a.cmd[1:] if (a.cmd and a.cmd[0] == "--") else a.cmd
    if not cmd:
        print("_gpu_pglaunch: no command given after --", file=sys.stderr)
        return 2

    proc = subprocess.Popen(cmd, start_new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        _atomic_write(a.pgid_file, {
            "pgid": pgid,
            "child_pid": proc.pid,
            "wrapper_pid": a.wrapper_pid,
            "job": a.job,
            "child_started_at": time.time(),
        })
    except Exception as exc:
        print(f"_gpu_pglaunch: WARN could not write pgid file: {exc}", file=sys.stderr)

    def _handler(signum, _frame):
        _reap_group(pgid, proc, a.grace)
        sys.exit(143 if signum == signal.SIGTERM else 130)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    rc = proc.wait()
    if not _wait_group_empty(pgid, 1.0):
        print(
            f"_gpu_pglaunch: WARN root exited with {rc} but process group {pgid} "
            "still had live descendants; reaping before exit",
            file=sys.stderr,
        )
        _reap_group(pgid, proc, a.grace)
        if rc == 0:
            return 124
    return (128 + (-rc)) if rc < 0 else rc


if __name__ == "__main__":
    sys.exit(main())
