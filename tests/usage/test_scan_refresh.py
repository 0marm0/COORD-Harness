"""The standalone service keeps its own scan store current.

Nothing else refreshes the store on a machine with no scheduler, so these tests
pin what the in-process refresher must guarantee: a stale store starts exactly
one background incremental scan, a request is never made to wait for it, two
processes never both walk the tree, a fresh install says its history is still
building instead of silently showing a few days, and the freshness block
survives the public proxy allowlist.

Every test runs against a synthetic HOME under ``tmp_path``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from coordharness.usage.dashboard_proxy import (
    UsageDashboardError,
    UsageDashboardProxy,
    validate_usage_dashboard,
)
from coordharness.usage.local_service import LocalUsageService, ProviderProbe
from coordharness.usage.scan_refresh import (
    MARKER_SCHEMA,
    ScanStoreRefresher,
    assess_store,
    lock_path,
    marker_path,
    write_marker,
)
from coordharness.usage.scan_store import scan_providers

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _claude(message_id: str, moment: datetime, output: int = 10) -> dict[str, Any]:
    return {
        "timestamp": _stamp(moment),
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "usage": {"input_tokens": 100, "output_tokens": output},
        },
    }


def _codex(ordinal: int, moment: datetime) -> dict[str, Any]:
    return {
        "timestamp": _stamp(moment),
        "ordinal": ordinal,
        "payload": {
            "thread_id": "t-1",
            "model": "gpt-5.6-sol",
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 900,
                "output_tokens": 5,
            },
        },
    }


def _synthetic_home(tmp_path: Path) -> Path:
    """A small clean HOME: a month of Claude history, today's Claude and Codex."""

    home = tmp_path / "home"
    now = _now()
    _write(
        home / ".claude" / "projects" / "old" / "month-ago.jsonl",
        [_claude("msg-old", now - timedelta(days=30))],
    )
    _write(
        home / ".claude" / "projects" / "today" / "session.jsonl",
        [_claude("msg-today", now - timedelta(minutes=5))],
    )
    _write(
        home / ".codex" / "sessions" / "rollout-today.jsonl",
        [_codex(1, now - timedelta(minutes=5))],
    )
    return home


def _probe() -> ProviderProbe:
    return ProviderProbe(account={"status": "inactive", "plan": "unknown", "authenticated": False})


def _service(home: Path, **kwargs: Any) -> LocalUsageService:
    return LocalUsageService(
        home=home,
        claude_probe=_probe,
        codex_probe=_probe,
        cost_cache_root=home / "no-cost-cache",
        first_read_wait_seconds=1.0,
        **kwargs,
    )


def _gated_scanner(gate: threading.Event, calls: list[str]):
    def scanner(home: Path, *, store_path: Path):
        calls.append("scan")
        assert gate.wait(timeout=10)
        return scan_providers(home, store_path=store_path)

    return scanner


def _store(home: Path) -> Path:
    return home / ".coordharness" / "usage-scan.sqlite"


def _daily_dates(document: dict[str, Any], provider: str) -> list[str]:
    return [row["date"] for row in document["providers"][provider]["history"]["daily"]]


# -- the refresher on its own -------------------------------------------------


def test_stale_store_starts_one_background_scan_and_never_waits(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = _store(home)
    scan_providers(home, store_path=store)
    write_marker(store, [], _now() - timedelta(minutes=10))
    gate = threading.Event()
    calls: list[str] = []
    refresher = ScanStoreRefresher(
        home=home, store_path=store, scanner=_gated_scanner(gate, calls)
    )

    started = time.monotonic()
    assert refresher.request_refresh() is True
    # Returned while the scan is still blocked on the gate.
    assert time.monotonic() - started < 1.0
    assert refresher.refreshing
    # Single flight: a second request while one is running starts nothing.
    assert refresher.request_refresh() is False
    assert refresher.freshness().state == "stale"
    assert refresher.freshness().refreshing is True

    gate.set()
    assert refresher.wait(timeout=10)
    assert calls == ["scan"]
    fresh = refresher.freshness()
    assert fresh.state == "fresh" and fresh.refreshing is False
    marker = json.loads(marker_path(store).read_text(encoding="utf-8"))
    assert marker["schema"] == MARKER_SCHEMA and marker["incremental"] is True


def test_fresh_store_is_not_rescanned(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = _store(home)
    scan_providers(home, store_path=store)
    write_marker(store, [], _now())
    calls: list[str] = []
    gate = threading.Event()
    gate.set()
    refresher = ScanStoreRefresher(
        home=home, store_path=store, scanner=_gated_scanner(gate, calls)
    )

    assert refresher.request_refresh() is False
    assert calls == []


def test_a_scan_lock_held_by_another_process_means_no_second_walk(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = _store(home)
    store.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            "handle = open(sys.argv[1], 'a+b')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(30)\n",
            str(lock_path(store)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        calls: list[str] = []
        gate = threading.Event()
        gate.set()
        refresher = ScanStoreRefresher(
            home=home, store_path=store, scanner=_gated_scanner(gate, calls)
        )
        assert refresher.request_refresh() is True
        assert refresher.wait(timeout=10)
        assert calls == []
        assert not marker_path(store).exists()
        # Backs off instead of retrying on every request.
        assert refresher.request_refresh() is False
    finally:
        holder.kill()
        holder.wait()


def test_a_failed_scan_is_reported_and_backs_off(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = _store(home)

    def broken(_home: Path, *, store_path: Path):
        raise OSError("disk full")

    refresher = ScanStoreRefresher(home=home, store_path=store, scanner=broken)
    assert refresher.request_refresh() is True
    assert refresher.wait(timeout=10)
    assert refresher.freshness().error_code == "scan_refresh_failed"
    assert refresher.request_refresh() is False


def test_without_a_marker_the_newest_scanned_file_is_a_lower_bound(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = _store(home)
    scan_providers(home, store_path=store)

    status = assess_store(store, _now() + timedelta(hours=1))

    assert status.last_scan_at is not None
    assert status.state == "stale"
    assert status.serving == "scan_store"


# -- the service a board hosts ------------------------------------------------


def test_fresh_install_says_history_is_building_then_serves_the_store(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    service = _service(home)
    refresher = service._store_refresher
    assert refresher is not None
    gate = threading.Event()
    calls: list[str] = []
    refresher._scanner = _gated_scanner(gate, calls)
    proxy = UsageDashboardProxy(url="", local_provider=service.dashboard)

    building = proxy.get()

    history_store = building["history_store"]
    assert history_store["state"] == "building"
    assert history_store["serving"] == "live_bounded_scan"
    assert history_store["refreshing"] is True
    # The bounded live scan still shows today while the full history builds.
    assert _local_today() in _daily_dates(building, "claude")

    gate.set()
    assert refresher.wait(timeout=10)
    built = proxy.get(force_refresh=True)

    assert built["history_store"]["state"] == "fresh"
    assert built["history_store"]["serving"] == "scan_store"
    assert built["history_store"]["last_scan_at"] is not None
    assert _local_today() in _daily_dates(built, "claude")
    assert _local_today() in _daily_dates(built, "codex")
    assert calls == ["scan"]


def test_stale_store_serves_last_good_and_picks_up_today_incrementally(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = _store(home)
    scan_providers(home, store_path=store)
    write_marker(store, [], _now() - timedelta(hours=6))
    # New activity after the last scan, appended to today's transcript.
    today = home / ".claude" / "projects" / "today" / "session.jsonl"
    with today.open("a") as handle:
        handle.write(json.dumps(_claude("msg-new", _now(), output=5_000)) + "\n")
    service = _service(home)
    refresher = service._store_refresher
    assert refresher is not None
    gate = threading.Event()
    calls: list[str] = []
    refresher._scanner = _gated_scanner(gate, calls)
    proxy = UsageDashboardProxy(url="", local_provider=service.dashboard)

    before = proxy.get()

    assert before["history_store"]["state"] == "stale"
    assert before["history_store"]["refreshing"] is True
    assert before["history_store"]["serving"] == "scan_store"
    before_today = before["providers"]["claude"]["history"]["today_total_tokens"]

    gate.set()
    assert refresher.wait(timeout=10)
    after = proxy.get(force_refresh=True)

    assert after["history_store"]["state"] == "fresh"
    assert after["providers"]["claude"]["history"]["today_total_tokens"] == before_today + 5_100
    marker = json.loads(marker_path(store).read_text(encoding="utf-8"))
    claude_summary = next(row for row in marker["providers"] if row["provider"] == "claude")
    # Incremental: only the appended transcript was parsed again.
    assert claude_summary["files_parsed"] == 1
    assert claude_summary["files_unchanged"] >= 1


def test_the_request_is_answered_while_a_scan_is_still_running(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    service = _service(home)
    refresher = service._store_refresher
    assert refresher is not None
    gate = threading.Event()
    refresher._scanner = _gated_scanner(gate, [])
    try:
        started = time.monotonic()
        document = service.dashboard()
        assert time.monotonic() - started < 5.0
        assert document["providers"]
    finally:
        gate.set()
        refresher.wait(timeout=10)


def test_an_injected_history_source_has_no_store_to_refresh(tmp_path: Path) -> None:
    service = _service(tmp_path, history_loader=lambda _root, *, provider: None)
    assert service._store_refresher is None


def test_auto_refresh_can_be_turned_off(tmp_path: Path) -> None:
    service = _service(_synthetic_home(tmp_path), store_auto_refresh=False)
    assert service._store_refresher is None
    document = UsageDashboardProxy(url="", local_provider=service.dashboard).get()
    assert document["history_store"]["state"] == "unavailable"
    assert not _store(tmp_path / "home").exists()


# -- the proxy allowlist ------------------------------------------------------


_UPSTREAM = {
    "schema": "coordharness.usage-intelligence.v1",
    "generated_at": "2026-09-22T19:30:00Z",
    "stale_after": None,
    "refresh": {"state": "fresh", "generated_at": "2026-09-22T19:30:00Z"},
    "history_store": {
        "state": "stale",
        "last_scan_at": "2026-09-22T02:16:43.739667Z",
        "age_seconds": 62_000,
        "stale_after_seconds": 900,
        "refreshing": False,
        "serving": "scan_store",
        "semantics": "local_transcript_scan_store_freshness",
        "unexpected": "must not cross",
    },
    "providers": {},
    "errors": [],
}


class _Response:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}
        self._body = body

    def read(self, _limit: int) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_history_store_survives_the_upstream_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COORD_USAGE_UPSTREAM_SCHEMA", raising=False)
    body = json.dumps(_UPSTREAM).encode()
    proxy = UsageDashboardProxy(
        url="http://127.0.0.1:8780/api/usage/v1",
        opener=lambda _request, _timeout: _Response(body),
    )

    document = proxy.get()

    assert document["history_store"] == {
        key: value for key, value in _UPSTREAM["history_store"].items() if key != "unexpected"
    }


def test_malformed_history_store_is_rejected_not_passed_through() -> None:
    payload = json.loads(json.dumps(_UPSTREAM))
    payload["history_store"]["refreshing"] = "yes"
    with pytest.raises(UsageDashboardError):
        validate_usage_dashboard(payload)


def test_scan_cli_takes_the_lock_and_records_a_completed_pass(tmp_path: Path) -> None:
    home = _synthetic_home(tmp_path)
    store = tmp_path / "cli-store" / "usage-scan.sqlite"
    env = {**os.environ, "HOME": str(tmp_path)}

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "coordharness.coord.cli",
            "usage-scan",
            "--home",
            str(home),
            "--store",
            str(store),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    marker = json.loads(marker_path(store).read_text(encoding="utf-8"))
    assert marker["schema"] == MARKER_SCHEMA
    assert assess_store(store, _now()).state == "fresh"
