import json
import plistlib
import unittest
from datetime import datetime, timezone

from coordharness.board.local_system_telemetry import (
    LocalSystemTelemetryCollector,
    _RateTracker,
    _parse_capacity,
    _parse_gpu_ioreg,
    _parse_ioreg_disk_counters,
    _parse_top_cpu,
    _parse_vm_stat,
)


class LocalSystemTelemetryParserTests(unittest.TestCase):
    def test_top_cpu_uses_user_plus_system(self):
        output = b"CPU usage: 12.5% user, 7.25% sys, 80.25% idle\n"
        self.assertEqual(_parse_top_cpu(output), 19.75)

    def test_vm_stat_treats_reclaimable_inactive_pages_as_available(self):
        output = b"""Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free: 10.
Pages active: 50.
Pages inactive: 20.
Pages speculative: 5.
Pages wired down: 15.
"""
        metric = _parse_vm_stat(output, total_bytes=100 * 4096)
        self.assertEqual(metric["free_bytes"], 35 * 4096)
        self.assertEqual(metric["used_percent"], 65.0)

    def test_gpu_is_sourced_from_ioaccelerator_device_utilization(self):
        payload = plistlib.dumps(
            [
                {
                    "PerformanceStatistics": {
                        "Device Utilization %": 37,
                        "Renderer Utilization %": 31,
                        "Tiler Utilization %": 8,
                    }
                }
            ]
        )
        metric = _parse_gpu_ioreg(payload)
        self.assertEqual(metric["usage_percent"], 37.0)
        self.assertEqual(metric["renderer_percent"], 31.0)
        self.assertEqual(metric["source"], "macos_ioreg_ioaccelerator_device_utilization")

    def test_disk_capacity_prefers_important_usage_capacity(self):
        metric = _parse_capacity(
            b'{"NSURLVolumeTotalCapacityKey":1000,'
            b'"NSURLVolumeAvailableCapacityForImportantUsageKey":400,'
            b'"NSURLVolumeAvailableCapacityKey":40}'
        )
        self.assertEqual(metric["free_bytes"], 400)
        self.assertEqual(metric["used_percent"], 60.0)
        self.assertEqual(metric["capacity_semantics"], "available_for_important_usage")

    def test_ioreg_disk_counters_are_cumulative_totals(self):
        payload = plistlib.dumps(
            [
                {"Statistics": {"Bytes (Read)": 100, "Bytes (Write)": 40}},
                {"Statistics": {"Bytes (Read)": 50, "Bytes (Write)": 10}},
            ]
        )
        self.assertEqual(_parse_ioreg_disk_counters(payload), (150, 50))


class DiskRateTrackerTests(unittest.TestCase):
    def test_rates_require_two_monotonic_samples(self):
        tracker = _RateTracker()
        self.assertEqual(tracker.update(10.0, 1_000, 2_000), (None, None))
        self.assertEqual(tracker.update(12.0, 1_400, 2_100), (200.0, 50.0))

    def test_counter_reset_never_becomes_a_negative_rate(self):
        tracker = _RateTracker()
        tracker.update(10.0, 1_000, 2_000)
        self.assertEqual(tracker.update(12.0, 10, 20), (None, None))
        self.assertEqual(tracker.update(14.0, 30, 60), (10.0, 20.0))


class LocalSystemTelemetryCollectorTests(unittest.TestCase):
    def test_each_metric_fails_soft_when_native_probes_fail(self):
        def failing_runner(_argv):
            raise OSError("not installed")

        collector = LocalSystemTelemetryCollector(
            runner=failing_runner,
            platform_name="darwin",
            psutil_module=None,
            clock=lambda: 4.0,
            wall_clock=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        snapshot = collector.collect(demand=True)
        self.assertEqual(snapshot["freshness"]["state"], "fresh")
        for name in ("cpu", "gpu", "memory", "disk"):
            self.assertEqual(snapshot[name]["availability"], "unavailable")
        self.assertIsNone(snapshot["disk"]["read_bps"])

    def test_cache_avoids_repeated_probe_work(self):
        calls = 0

        def failing_runner(_argv):
            nonlocal calls
            calls += 1
            raise OSError("not installed")

        times = iter((10.0, 10.25))
        collector = LocalSystemTelemetryCollector(
            runner=failing_runner,
            platform_name="darwin",
            psutil_module=None,
            clock=lambda: next(times),
            demand_cache_seconds=1.0,
        )
        first = collector.collect(demand=True)
        first_bytes = json.dumps(first, separators=(",", ":")).encode()
        call_count = calls
        second = collector.collect(demand=True)
        self.assertEqual(calls, call_count)
        self.assertEqual(second["sequence"], first["sequence"])
        self.assertEqual(second["freshness"]["age_seconds"], 0)
        second_bytes = json.dumps(second, separators=(",", ":")).encode()
        self.assertEqual(len(second_bytes), len(first_bytes))


if __name__ == "__main__":
    unittest.main()
