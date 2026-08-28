from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

from coordharness.usage.dashboard_proxy import UsageDashboardProxy
from coordharness.usage.local_history import LocalHistoryImport
from coordharness.usage.local_service import LocalUsageService, ProviderProbe


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _probe() -> ProviderProbe:
    return ProviderProbe(
        account={"status": "active", "plan": "plus", "authenticated": True},
        windows=(
            {
                "kind": "session",
                "name": "session",
                "window_minutes": 300,
                "used_percent": 20,
                "remaining_percent": 80,
                "resets_at": "2026-08-28T15:00:00Z",
                "countdown_seconds": 10800,
            },
        ),
        observed_at="2026-08-28T12:00:00Z",
        account_source="official_cli_status",
        quota_source="official_cli_quota",
    )


def _empty_history(provider: str) -> LocalHistoryImport:
    return LocalHistoryImport(
        provider=provider,
        rows=(),
        coverage_state="unknown",
        root_identity_digest="1" * 64,
        manifest_digest="2" * 64,
        files_scanned=0,
        records_scanned=0,
        records_accepted=0,
        records_rejected=0,
        parse_error_count=0,
    )


def test_repeated_dashboard_and_account_reads_share_one_snapshot(tmp_path) -> None:
    calls = {"claude": 0, "codex": 0, "history": 0}

    def claude_probe():
        calls["claude"] += 1
        return _probe()

    def codex_probe():
        calls["codex"] += 1
        return _probe()

    def history_loader(_root, *, provider):
        calls["history"] += 1
        return _empty_history(provider)

    service = LocalUsageService(
        home=tmp_path,
        now=lambda: NOW,
        claude_probe=claude_probe,
        codex_probe=codex_probe,
        history_loader=history_loader,
        first_read_wait_seconds=0.5,
    )
    proxy = UsageDashboardProxy(url="", local_provider=service.dashboard)

    first = proxy.get()
    second = proxy.get()
    accounts = service.account_status()

    assert first["refresh"]["state"] == second["refresh"]["state"] == "fresh"
    assert accounts["codex"].account["authenticated"] is True
    assert calls == {"claude": 1, "codex": 1, "history": 2}

    forced = proxy.get(force_refresh=True)
    assert forced["refresh"]["state"] == "fresh"
    assert calls == {"claude": 2, "codex": 2, "history": 4}


def test_concurrent_cold_calls_coalesce_and_first_read_is_bounded(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = {"claude": 0, "codex": 0, "history": 0}

    def claude_probe():
        with lock:
            calls["claude"] += 1
        entered.set()
        release.wait(timeout=1)
        return _probe()

    def codex_probe():
        with lock:
            calls["codex"] += 1
        return _probe()

    def history_loader(_root, *, provider):
        with lock:
            calls["history"] += 1
        return _empty_history(provider)

    service = LocalUsageService(
        home=tmp_path,
        now=lambda: NOW,
        claude_probe=claude_probe,
        codex_probe=codex_probe,
        history_loader=history_loader,
        first_read_wait_seconds=0.04,
    )
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(service.dashboard())) for _ in range(8)
    ]

    started = time.monotonic()
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=0.5)
    for thread in threads:
        thread.join(timeout=0.25)
    elapsed = time.monotonic() - started

    assert all(not thread.is_alive() for thread in threads)
    assert elapsed < 0.25
    assert len(results) == 8
    assert all(result["refresh"]["state"] == "warming" for result in results)
    assert all(result["refresh"]["error_code"] == "local_refresh_in_progress" for result in results)
    assert calls == {"claude": 1, "codex": 0, "history": 0}

    release.set()
    deadline = time.monotonic() + 1
    while calls["history"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    fresh = service.dashboard()
    assert fresh["refresh"]["state"] == "fresh"
    assert calls == {"claude": 1, "codex": 1, "history": 2}


def test_expired_snapshot_is_stale_while_single_refresh_runs(tmp_path) -> None:
    clock = [0.0]
    block_refresh = threading.Event()
    release = threading.Event()
    calls = 0

    def claude_probe():
        nonlocal calls
        calls += 1
        if calls > 1:
            block_refresh.set()
            release.wait(timeout=1)
        return _probe()

    service = LocalUsageService(
        home=tmp_path,
        now=lambda: NOW,
        claude_probe=claude_probe,
        codex_probe=_probe,
        history_loader=lambda _root, *, provider: _empty_history(provider),
        cache_ttl_seconds=30,
        first_read_wait_seconds=0.2,
        monotonic=lambda: clock[0],
    )
    assert service.dashboard()["refresh"]["state"] == "fresh"

    clock[0] = 31
    stale = service.dashboard()
    assert block_refresh.wait(timeout=0.5)
    assert stale["refresh"]["state"] == "stale"
    assert stale["refresh"]["error_code"] == "local_refresh_in_progress"
    assert stale["refresh"]["last_good_generated_at"] == "2026-08-28T12:00:00Z"

    again = service.dashboard()
    assert again["refresh"]["state"] == "stale"
    assert calls == 2
    release.set()
