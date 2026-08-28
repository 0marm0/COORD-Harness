from __future__ import annotations

import copy
import importlib
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

_AUTO = object()
_JXA_CAPACITY_SCRIPT = """\
ObjC.import("Foundation");
const url = $.NSURL.fileURLWithPath("/");
const error = Ref();
const keys = [
  "NSURLVolumeTotalCapacityKey",
  "NSURLVolumeAvailableCapacityForImportantUsageKey",
  "NSURLVolumeAvailableCapacityKey"
];
const values = url.resourceValuesForKeysError(keys, error);
if (!values) { throw new Error("volume capacity unavailable"); }
JSON.stringify(ObjC.deepUnwrap(values));
"""


class TelemetryProbeError(RuntimeError):
    """A local probe failed without exposing command output to the API."""


class BoundedCommandRunner:
    def __init__(self, *, timeout: float = 0.8, max_bytes: int = 512_000):
        self.timeout = max(0.1, min(float(timeout), 2.0))
        self.max_bytes = max(4_096, min(int(max_bytes), 2_000_000))

    def __call__(self, argv: Sequence[str]) -> bytes:
        try:
            result = subprocess.run(
                tuple(argv),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TelemetryProbeError("native telemetry probe failed") from exc
        if result.returncode != 0 or len(result.stdout) > self.max_bytes:
            raise TelemetryProbeError("native telemetry probe failed")
        return result.stdout


class _RateTracker:
    def __init__(self) -> None:
        self._previous: tuple[float, int, int] | None = None

    def update(self, now: float, read_bytes: int, write_bytes: int) -> tuple[float | None, float | None]:
        previous = self._previous
        self._previous = (now, read_bytes, write_bytes)
        if previous is None:
            return None, None
        elapsed = now - previous[0]
        if elapsed <= 0 or read_bytes < previous[1] or write_bytes < previous[2]:
            return None, None
        return (read_bytes - previous[1]) / elapsed, (write_bytes - previous[2]) / elapsed


def _unavailable(percent_key: str, source: str, error: str, *, unsupported: bool = False) -> dict[str, Any]:
    return {
        "availability": "unsupported" if unsupported else "unavailable",
        "source": source,
        "error": error,
        percent_key: None,
    }


def _finite_percent(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryProbeError("invalid percentage from native telemetry probe")
    value = float(value)
    if not math.isfinite(value):
        raise TelemetryProbeError("invalid percentage from native telemetry probe")
    return min(100.0, max(0.0, value))


def _parse_top_cpu(output: bytes) -> float:
    match = re.search(rb"CPU usage:\s*([0-9.]+)% user,\s*([0-9.]+)% sys", output)
    if not match:
        raise TelemetryProbeError("CPU utilization is unavailable")
    return _finite_percent(float(match.group(1)) + float(match.group(2)))


def _parse_vm_stat(output: bytes, *, total_bytes: int) -> dict[str, Any]:
    page_match = re.search(rb"page size of\s+(\d+) bytes", output)
    if not page_match or total_bytes <= 0:
        raise TelemetryProbeError("memory counters are unavailable")
    page_size = int(page_match.group(1))
    counters = {
        key.decode("ascii"): int(value)
        for key, value in re.findall(rb"^Pages ([a-z ]+):\s+(\d+)\.\s*$", output, re.MULTILINE)
    }
    required = ("free", "inactive", "speculative")
    if not all(key in counters for key in required):
        raise TelemetryProbeError("memory counters are unavailable")
    available = min(total_bytes, sum(counters[key] for key in required) * page_size)
    used = max(0, total_bytes - available)
    return {
        "availability": "available",
        "source": "macos_vm_stat_available_pages",
        "used_percent": used / total_bytes * 100.0,
        "used_bytes": used,
        "total_bytes": total_bytes,
        "free_bytes": available,
        "swap_used_bytes": None,
        "swap_total_bytes": None,
        "pressure": None,
    }


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_gpu_ioreg(output: bytes) -> dict[str, Any]:
    try:
        payload = plistlib.loads(output)
    except Exception as exc:
        raise TelemetryProbeError("IOAccelerator telemetry is unavailable") from exc
    samples: list[dict[str, Any]] = []
    for node in _walk_dicts(payload):
        stats = node.get("PerformanceStatistics")
        if isinstance(stats, dict) and isinstance(stats.get("Device Utilization %"), (int, float)):
            samples.append(stats)
    if not samples:
        raise TelemetryProbeError("IOAccelerator Device Utilization is unavailable")
    # Multiple accelerators have independent 0-100 scales; max is meaningful while summing is not.
    usage = max(_finite_percent(stats["Device Utilization %"]) for stats in samples)
    metric: dict[str, Any] = {
        "availability": "available",
        "source": "macos_ioreg_ioaccelerator_device_utilization",
        "usage_percent": usage,
    }
    for source_key, target_key in (
        ("Renderer Utilization %", "renderer_percent"),
        ("Tiler Utilization %", "tiler_percent"),
    ):
        values = [stats[source_key] for stats in samples if isinstance(stats.get(source_key), (int, float))]
        metric[target_key] = max((_finite_percent(value) for value in values), default=None)
    return metric


def _parse_capacity(output: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(output)
        total = int(payload["NSURLVolumeTotalCapacityKey"])
        available = payload.get("NSURLVolumeAvailableCapacityForImportantUsageKey")
        if available is None:
            available = payload.get("NSURLVolumeAvailableCapacityKey")
        free = int(available)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelemetryProbeError("important-usage disk capacity is unavailable") from exc
    if total <= 0 or free < 0:
        raise TelemetryProbeError("important-usage disk capacity is unavailable")
    free = min(total, free)
    used = total - free
    return {
        "availability": "available",
        "source": "macos_foundation_important_usage_capacity",
        "capacity_semantics": "available_for_important_usage",
        "used_percent": used / total * 100.0,
        "used_bytes": used,
        "total_bytes": total,
        "free_bytes": free,
    }


def _parse_ioreg_disk_counters(output: bytes) -> tuple[int, int]:
    try:
        payload = plistlib.loads(output)
    except Exception as exc:
        raise TelemetryProbeError("disk I/O counters are unavailable") from exc
    read_bytes = 0
    write_bytes = 0
    found = False
    for node in _walk_dicts(payload):
        stats = node.get("Statistics")
        if not isinstance(stats, dict):
            continue
        read = stats.get("Bytes (Read)")
        written = stats.get("Bytes (Write)")
        if isinstance(read, int) and isinstance(written, int) and read >= 0 and written >= 0:
            found = True
            read_bytes += read
            write_bytes += written
    if not found:
        raise TelemetryProbeError("disk I/O counters are unavailable")
    return read_bytes, write_bytes


class LocalSystemTelemetryCollector:
    """Small, dependency-free local sampler with optional psutil acceleration."""

    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str]], bytes] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        platform_name: str = sys.platform,
        psutil_module: Any = _AUTO,
        cache_seconds: float = 4.0,
        demand_cache_seconds: float = 1.0,
    ) -> None:
        self._runner = runner or BoundedCommandRunner()
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._platform = platform_name
        self._psutil = self._load_psutil() if psutil_module is _AUTO else psutil_module
        self._cache_seconds = max(0.1, min(float(cache_seconds), 30.0))
        self._demand_cache_seconds = max(0.1, min(float(demand_cache_seconds), self._cache_seconds))
        self._disk_rates = _RateTracker()
        self._sequence = 0
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _load_psutil() -> Any:
        try:
            return importlib.import_module("psutil")
        except (ImportError, OSError):
            return None

    def collect(self, *, demand: bool = False) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            ttl = self._demand_cache_seconds if demand else self._cache_seconds
            if self._cached is not None and now - self._cached_at < ttl:
                cached = copy.deepcopy(self._cached)
                age = int(max(0.0, now - self._cached_at))
                cached["freshness"] = {"state": "fresh", "age_seconds": age}
                cached["cadence"] = {
                    "mode": "demand" if demand else "idle",
                    "interval_seconds": ttl,
                    "demand_active": demand,
                }
                return cached
            snapshot = self._collect_uncached(now=now, demand=demand, ttl=ttl)
            self._cached = copy.deepcopy(snapshot)
            self._cached_at = now
            return snapshot

    def _collect_uncached(self, *, now: float, demand: bool, ttl: float) -> dict[str, Any]:
        self._sequence += 1
        cpu = self._collect_cpu()
        memory = self._collect_memory()
        gpu = self._collect_gpu()
        disk = self._collect_disk(now)
        generated = self._wall_clock().astimezone(timezone.utc).isoformat(timespec="milliseconds")
        return {
            "schema_version": 1,
            "generated_at": generated.replace("+00:00", "Z"),
            "sequence": self._sequence,
            "stale_after_seconds": max(2.0, ttl * 2.0),
            "enabled": True,
            "profile": "coord_local_system",
            "cadence": {
                "mode": "demand" if demand else "idle",
                "interval_seconds": ttl,
                "demand_active": demand,
            },
            "freshness": {"state": "fresh", "age_seconds": 0},
            "cpu": cpu,
            "gpu": gpu,
            "memory": memory,
            "disk": disk,
        }

    def _collect_cpu(self) -> dict[str, Any]:
        if self._psutil is not None:
            try:
                usage = _finite_percent(self._psutil.cpu_percent(interval=None))
                return {
                    "availability": "available",
                    "source": "psutil_cpu_percent",
                    "usage_percent": usage,
                }
            except Exception:
                pass
        if self._platform != "darwin":
            return _unavailable("usage_percent", "coord_local_collector", "CPU probe unsupported", unsupported=True)
        try:
            output = self._runner(("/usr/bin/top", "-l", "1", "-n", "0", "-s", "0"))
            return {
                "availability": "available",
                "source": "macos_top_cpu_usage",
                "usage_percent": _parse_top_cpu(output),
            }
        except Exception:
            return _unavailable("usage_percent", "macos_top_cpu_usage", "CPU probe unavailable")

    def _collect_memory(self) -> dict[str, Any]:
        if self._psutil is not None:
            try:
                memory = self._psutil.virtual_memory()
                swap = self._psutil.swap_memory()
                return {
                    "availability": "available",
                    "source": "psutil_virtual_memory",
                    "used_percent": _finite_percent(memory.percent),
                    "used_bytes": int(memory.used),
                    "total_bytes": int(memory.total),
                    "free_bytes": int(memory.available),
                    "swap_used_bytes": int(swap.used),
                    "swap_total_bytes": int(swap.total),
                    "pressure": None,
                }
            except Exception:
                pass
        if self._platform != "darwin":
            return _unavailable("used_percent", "coord_local_collector", "memory probe unsupported", unsupported=True)
        try:
            total = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
            return _parse_vm_stat(self._runner(("/usr/bin/vm_stat",)), total_bytes=total)
        except Exception:
            return _unavailable("used_percent", "macos_vm_stat_available_pages", "memory probe unavailable")

    def _collect_gpu(self) -> dict[str, Any]:
        source = "macos_ioreg_ioaccelerator_device_utilization"
        if self._platform != "darwin":
            return _unavailable("usage_percent", source, "IOAccelerator unsupported", unsupported=True)
        try:
            output = self._runner(("/usr/sbin/ioreg", "-r", "-c", "IOAccelerator", "-d", "3", "-a"))
            return _parse_gpu_ioreg(output)
        except Exception:
            return _unavailable("usage_percent", source, "IOAccelerator Device Utilization unavailable")

    def _disk_capacity(self) -> dict[str, Any]:
        if self._platform == "darwin":
            output = self._runner(("/usr/bin/osascript", "-l", "JavaScript", "-e", _JXA_CAPACITY_SCRIPT))
            return _parse_capacity(output)
        usage = shutil.disk_usage("/")
        if usage.total <= 0:
            raise TelemetryProbeError("disk capacity is unavailable")
        return {
            "availability": "available",
            "source": "python_shutil_disk_usage",
            "capacity_semantics": "filesystem_available",
            "used_percent": usage.used / usage.total * 100.0,
            "used_bytes": usage.used,
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        }

    def _disk_counters(self) -> tuple[tuple[int, int], str]:
        if self._psutil is not None:
            try:
                counters = self._psutil.disk_io_counters()
                if counters is not None and counters.read_bytes >= 0 and counters.write_bytes >= 0:
                    return (int(counters.read_bytes), int(counters.write_bytes)), "psutil_disk_io_counters"
            except Exception:
                pass
        if self._platform != "darwin":
            raise TelemetryProbeError("disk I/O counters unsupported")
        output = self._runner(("/usr/sbin/ioreg", "-r", "-c", "IOBlockStorageDriver", "-d", "1", "-a"))
        return _parse_ioreg_disk_counters(output), "macos_ioreg_block_storage_counters"

    def _collect_disk(self, now: float) -> dict[str, Any]:
        try:
            metric = self._disk_capacity()
        except Exception:
            metric = _unavailable(
                "used_percent",
                "macos_foundation_important_usage_capacity" if self._platform == "darwin" else "coord_local_collector",
                "important-usage disk capacity unavailable" if self._platform == "darwin" else "disk capacity unavailable",
            )
        metric.update({"read_bps": None, "write_bps": None, "io_source": None})
        try:
            counters, source = self._disk_counters()
            metric["read_bps"], metric["write_bps"] = self._disk_rates.update(now, *counters)
            metric["io_source"] = source
        except Exception:
            # Capacity remains independently useful; I/O rates need two valid cumulative samples.
            pass
        return metric
