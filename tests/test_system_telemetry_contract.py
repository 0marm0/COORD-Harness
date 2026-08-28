import unittest
from pathlib import Path
from unittest.mock import patch

from coordharness.board.system_telemetry_proxy import SystemTelemetryProxy, _loopback_url, validate


class SystemTelemetryContractTests(unittest.TestCase):
    def test_proxy_rejects_non_loopback(self):
        with self.assertRaises(ValueError):
            _loopback_url("http://example.com/api/v1/system-telemetry")

    def test_missing_percent_remains_none(self):
        metric = {"availability": "unsupported", "source": "test"}
        payload = {"schema_version": 1, "cpu": metric, "gpu": metric, "memory": metric, "disk": metric}
        self.assertIsNone(validate(payload)["gpu"]["usage_percent"])

    def test_failure_is_unavailable_never_zero(self):
        payload = SystemTelemetryProxy("http://127.0.0.1:1/api/v1/system-telemetry", timeout=0.1).get(demand=True)
        self.assertEqual(payload["freshness"]["state"], "unavailable")
        self.assertIsNone(payload["cpu"]["usage_percent"])

    def test_explicit_empty_upstream_is_structured_unavailable(self):
        payload = SystemTelemetryProxy(url="").get()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["freshness"]["state"], "unavailable")
        self.assertEqual(payload["gpu"]["availability"], "unavailable")
        self.assertIsNone(payload["gpu"]["usage_percent"])

    def test_no_upstream_uses_standalone_local_collector(self):
        class StubCollector:
            def collect(self, *, demand=False):
                metric = {"availability": "available", "source": "local-test"}
                return {
                    "schema_version": 1,
                    "freshness": {"state": "fresh", "age_seconds": 0},
                    "cpu": {**metric, "usage_percent": 12},
                    "gpu": {**metric, "usage_percent": 23},
                    "memory": {**metric, "used_percent": 34},
                    "disk": {**metric, "used_percent": 45},
                    "demand": demand,
                }

        with patch.dict("os.environ", {"COORD_SYSTEM_TELEMETRY_URL": ""}):
            payload = SystemTelemetryProxy(collector=StubCollector()).get(demand=True)
        self.assertEqual(payload["freshness"]["state"], "fresh")
        self.assertEqual(payload["cpu"]["usage_percent"], 12.0)
        self.assertTrue(payload["demand"])

    def test_server_and_web_contracts_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / "src/coordharness/board/server.py").read_text()
        script = (root / "src/coordharness/board/static/app.js").read_text()
        content = (root / "apps/menubar/Sources/UI/ContentStackAndRows.swift").read_text()
        config = (root / "apps/menubar/Sources/Data/Config.swift").read_text()
        settings = (root / "apps/menubar/Sources/UI/SettingsView.swift").read_text()
        self.assertIn('path == "/api/v1/system-telemetry"', server)
        self.assertIn("request_queue_size = 64", server)
        self.assertIn('fetch("/api/v1/system-telemetry?demand=1",{cache:"no-store"})', script)
        self.assertIn('document.visibilityState!=="visible"', script)
        self.assertNotIn('title: "Usage"', content)
        self.assertNotIn('title: "Comms"', content)
        self.assertNotIn('title: "Dependencies"', content)
        self.assertIn("var systemTelemetryEnabled: Bool = true", config)
        self.assertIn("var systemTelemetryInPopover: Bool = true", config)
        self.assertIn("var systemTelemetryInStatusItem: Bool = true", config)
        self.assertIn("systemTelemetryStatusPreferenceVersion: Int = 1", config)
        self.assertIn("var systemTelemetryShowDisk: Bool = true", config)
        self.assertLess(settings.index("System stats · Menu bar"), settings.index("Usage display"))


if __name__ == "__main__":
    unittest.main()
