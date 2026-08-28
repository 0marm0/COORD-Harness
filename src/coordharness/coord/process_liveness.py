from __future__ import annotations

import os
import subprocess
from datetime import datetime

START_TIME_TOLERANCE_S = 2.0


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
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return None
    if not out:
        return None
    try:
        return datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
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
