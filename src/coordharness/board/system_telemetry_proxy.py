from __future__ import annotations

import ipaddress
import json
import math
import os
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from coordharness.board.local_system_telemetry import LocalSystemTelemetryCollector

DEFAULT_URL = None
MAX_BYTES = 32_768


def _loopback_url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme != "http" or parts.username or parts.password or parts.fragment:
        raise ValueError("system telemetry upstream must be credential-free loopback HTTP")
    host = (parts.hostname or "").lower()
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("system telemetry upstream must target loopback")
        except ValueError as exc:
            raise ValueError("system telemetry upstream must target loopback") from exc
    return raw


def _percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid system telemetry percent")
    return min(100.0, max(0.0, float(value)))


def validate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("invalid system telemetry schema")
    clean = dict(payload)
    for key, percent_key in (
        ("cpu", "usage_percent"),
        ("gpu", "usage_percent"),
        ("memory", "used_percent"),
        ("disk", "used_percent"),
    ):
        metric = clean.get(key)
        if not isinstance(metric, dict) or not isinstance(metric.get("availability"), str):
            raise ValueError("invalid system telemetry metric")
        metric = dict(metric)
        metric[percent_key] = _percent(metric.get(percent_key))
        clean[key] = metric
    return clean


class SystemTelemetryProxy:
    def __init__(
        self,
        url: str | None = None,
        timeout: float = 0.8,
        *,
        collector: LocalSystemTelemetryCollector | None = None,
    ):
        configured = url if url is not None else os.environ.get("COORD_SYSTEM_TELEMETRY_URL")
        self.url = _loopback_url(configured) if configured else None
        self.timeout = max(0.1, min(float(timeout), 3.0))
        # An explicitly empty URL keeps the previous opt-out behavior. With no URL
        # argument or environment override, COORD is a standalone local collector.
        self.collector = collector or (
            LocalSystemTelemetryCollector() if url is None and not configured else None
        )

    def get(self, *, demand: bool = False) -> dict[str, Any]:
        if self.url is None:
            if self.collector is None:
                return self._unavailable()
            try:
                return validate(self.collector.collect(demand=demand))
            except Exception:
                return self._unavailable()
        parts = urlsplit(self.url)
        query = urlencode({"demand": "1"}) if demand else ""
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - URL is loopback-validated
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("system telemetry response too large")
            return validate(json.loads(raw))
        except Exception:
            return self._unavailable()

    @staticmethod
    def _unavailable() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": None,
            "sequence": None,
            "stale_after_seconds": 0,
            "freshness": {"state": "unavailable", "age_seconds": None},
            **{
                key: {"availability": "unavailable", "source": "coord_loopback_proxy", percent: None}
                for key, percent in (
                    ("cpu", "usage_percent"),
                    ("gpu", "usage_percent"),
                    ("memory", "used_percent"),
                    ("disk", "used_percent"),
                )
            },
        }
