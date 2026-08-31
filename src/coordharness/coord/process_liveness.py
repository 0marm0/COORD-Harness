from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime

_logger = logging.getLogger(__name__)

START_TIME_TOLERANCE_S = 2.0

# 'ps -o lstart=' has no non-POSIX equivalent -- there is no fallback to
# invent, per the module's own instructions. Probe once at import time
# rather than on every pid_start_time() call: the answer cannot change
# within a process's lifetime, and re-probing would mean re-logging.
PS_LSTART_AVAILABLE: bool = shutil.which("ps") is not None
_ps_unavailable_logged = False


def _log_ps_unavailable_once() -> None:
    global _ps_unavailable_logged
    if _ps_unavailable_logged:
        return
    _ps_unavailable_logged = True
    _logger.warning(
        "process_liveness: 'ps' is not available on this platform; PID "
        "start-time verification (pid_start_time/pid_matches) is "
        "permanently unavailable this run, so PID-reuse protection "
        "degrades to a bare pid_exists() liveness check "
        "(PS_LSTART_AVAILABLE=False).",
    )


def pid_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False


def pid_start_time(pid: int | None) -> float | None:
    if not pid:
        return None
    if not PS_LSTART_AVAILABLE:
        _log_ps_unavailable_once()
        return None
    # ps's `lstart` format is locale-dependent (e.g. "Mon Aug 31 ..." only
    # under an English locale); force LC_ALL=C so the strptime format below
    # is stable regardless of the caller's environment.
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
            env=env,
        ).strip()
    except Exception as exc:
        _logger.warning(
            "process_liveness: 'ps -o lstart=' failed for pid=%s: %s: %s",
            pid, type(exc).__name__, exc,
        )
        return None
    if not out:
        _logger.warning(
            "process_liveness: 'ps -o lstart=' returned empty output for pid=%s",
            pid,
        )
        return None
    try:
        return datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        _logger.warning(
            "process_liveness: unparseable ps lstart output for pid=%s: %r",
            pid, out,
        )
        return None


def pid_matches(pid: int | None, expected_start_time: float | None) -> bool:
    if not pid_exists(pid):
        return False
    if expected_start_time is None:
        return True
    actual = pid_start_time(pid)
    if actual is None:
        return False
    return abs(float(actual) - float(expected_start_time)) <= START_TIME_TOLERANCE_S
