from __future__ import annotations

from copy import deepcopy
import http.client
import json
from pathlib import Path
import threading

from coordharness import demo
from coordharness.board.server import make_server
from coordharness.usage.dashboard_proxy import USAGE_CONTRACT


def _payload() -> dict:
    provider = {
        "source": {"kind": "usage-v2", "canonical": True},
        "account": {"status": "authenticated", "plan": "test", "authenticated": True},
        "windows": [],
        "reset_credits": [],
        "runout": {
            "kind": "current_window_linear",
            "advisory": True,
            "estimated_exhausts_at": None,
            "seconds_to_exhaustion": None,
            "basis": "insufficient pace",
        },
        "history": {
            "daily": [],
            "rolling_7d_total_tokens": None,
            "calendar_week_total_tokens": None,
            "all_time_total_tokens": None,
            "semantics": "canonical_correctable",
        },
        "costs": {
            "provider_billed": {"amount_nanos": None, "currency": None, "semantics": "unknown"},
            "provider_native": {"amount_nanos": None, "semantics": "unknown"},
            "api_rate_estimate": {"amount_nanos": None, "semantics": "unknown"},
        },
        "active_sessions": {"status": "unknown", "count": None, "providers": []},
        "errors": [],
    }
    return {
        "schema": USAGE_CONTRACT,
        "generated_at": "2026-08-26T12:00:00Z",
        "stale_after": "2026-08-26T12:05:00Z",
        "refresh": {"state": "fresh", "generated_at": "2026-08-26T12:00:00Z"},
        "providers": {"claude": deepcopy(provider), "codex": deepcopy(provider)},
        "errors": [],
    }


class _FixedProxy:
    url = "http://127.0.0.1:7870/api/usage/v1"

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def get(self) -> dict:
        self.calls += 1
        return deepcopy(self.payload)


def _request(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_usage_dashboard_route_preserves_canonical_payload_and_serves_assets(tmp_path: Path) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    expected = _payload()
    expected["providers"]["claude"]["live_observation_state"] = "stale_last_good"
    expected["providers"]["claude"]["quota_source"] = {
        "kind": "codexbar_widget_snapshot",
        "canonical": False,
        "label": "Codex Bar widget quota snapshot · stale last-good",
        "warning": "Stale last-good quota observation retained; values may lag until Codex Bar refreshes.",
    }
    proxy = _FixedProxy(expected)
    server = make_server(
        port=0,
        db_path=str(database),
        usage_dashboard_proxy=proxy,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _request(server.server_port, "/api/v1/usage-dashboard")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        actual = json.loads(body)
        assert actual == expected
        assert actual["providers"]["claude"]["live_observation_state"] == "stale_last_good"
        assert "Stale last-good" in actual["providers"]["claude"]["quota_source"]["warning"]
        assert proxy.calls == 1

        for path, content_type in (
            ("/static/usage-dashboard.js", "text/javascript; charset=utf-8"),
            ("/static/usage-dashboard.css", "text/css; charset=utf-8"),
        ):
            asset_status, asset_headers, asset_body = _request(server.server_port, path)
            assert asset_status == 200
            assert asset_headers["Content-Type"] == content_type
            assert asset_body

        page_status, _page_headers, page_body = _request(server.server_port, "/")
        assert page_status == 200
        page = page_body.decode("utf-8")
        assert 'id="usage-strip" class="usage-board-strip" aria-label="Compact provider usage and live system statistics"' in page
        assert 'id="usage" class="panel" aria-label="Provider Usage"' in page
        assert 'href="/static/usage-dashboard.css"' in page
        assert 'src="/static/usage-dashboard.js"' in page
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_provider_usage_dom_contract_is_distinct_from_token_ledger_and_null_safe() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        root / "src/coordharness/board/static/usage-dashboard.js"
    ).read_text(encoding="utf-8")

    assert "Provider Usage" in script
    assert "Provider account usage" in script
    assert "separate from CORD's coordination token ledger" in script
    assert "Token receipts are not subscription credits or billed spend" in script
    assert 'data-provider="${escapeHTML(providerKey)}"' in script
    assert "Provider billed" in script
    assert "Provider native" in script
    assert "API-rate estimate" in script
    assert "Independent provider meters; session and weekly windows are never paired across groups." in script
    assert "Neither history is substituted for the other." in script
    assert "provider.quota_groups" in script
    assert "data-history-range" in script
    assert "missingDays" in script and "DAY_MS" in script
    assert "metricHistory" not in script
    assert "backward-compatible meter" in script
    assert 'role="img" aria-labelledby=' in script
    assert 'return "Unknown"' in script
    assert "Object.entries(item.by_currency)" in script
    assert 'join(" + ")' in script
    assert "amount_nanos" in script
    assert "amount_nanos ?? 0" not in script
    assert "amount_nanos||0" not in script
    assert 'ACCOUNT_STATUS_PATH = "/api/v1/usage-actions/status"' in script
    assert 'ACCOUNT_ACTION_PATH = "/api/v1/usage-actions"' in script
    assert 'ACCOUNT_ACTION_HEADER = "X-Coord-Usage-Action"' in script
    assert 'new Set(["codex_login_start", "codex_login_cancel", "claude_connect_open", "profile_add", "profile_select", "profile_remove"])' in script
    assert 'data-profile-add' in script and 'data-profile-select' in script
    assert 'aria-label="Provider Accounts">Provider Accounts</button>' in script
    assert 'data-account-action="codex_login_start"' in script
    assert 'data-account-action="claude_connect_open"' in script
    assert "Open Claude Code sign-in" in script
    assert "Open direct Claude Code sign-in via the local provider service" in script
    assert "direct Claude Code sign-in" in script
    assert "Recovery" not in script
    assert 'openButton.addEventListener("click"' in script
    assert 'method: isAction ? "POST" : "GET"' in script
    assert 'body: isAction ? JSON.stringify(typeof document === "string" ? {action} : document) : undefined' in script
    assert "reportedRemaining !== null ? reportedRemaining : (used === null ? null : 100 - used)" in script
    assert "% left" in script
    assert 'data-usage-width="${width}"' in script
    assert 'Usage window"} remaining' in script
    assert "raw.auth_url" not in script and "raw.login_id" not in script
    styles = (root / "src/coordharness/board/static/usage-dashboard.css").read_text(encoding="utf-8")
    assert ".usage-accounts-dialog" in styles
    assert "provider.quota_source" in script and "canonical quota" in script
    assert "codexbar_widget_snapshot:" in script
    assert '"Legacy snapshot / fallback"' in script
    assert "directlyConnected" in script
    assert "Compatibility local advisory projection" in script
    assert "visibleSourceText" in script
    assert "neutralCompatibilityText" in script
    assert "visibleErrorCode" in script
    assert "Legacy compatibility source unavailable" in script
    assert "Earned reset-credit inventory" in script
    assert "current reset eligibility unavailable" in script
    assert "earned_credit_inventory_not_current_reset_eligibility" in script
    assert "pace.will_last_to_reset" in script
    assert "pace.seconds_to_exhaustion" in script
    assert "projected to last until reset" in script
    assert "api_rate_estimate_nanos" in script and "provider_native_cost_nanos" in script
    assert "API-rate estimate" in script and "not billed spend" in script
    assert ".usage-pace-deficit" in styles and ".usage-quota-source" in styles
    assert "normalizedDailyCost(daily, range)" in script
    assert '<title id="${chartID}-title">Daily estimated cost</title>' in script
    assert 'class="usage-history-bar"' in script
    assert "Missing days are not plotted as zero." in script
    assert "dailyCostBars(daily, currencies)" not in script
    assert "<polyline" not in script
    assert "warmingRetries < 3" in script
    assert "window.setTimeout(loadUsageDashboard, 1500)" in script
    assert ".usage-daily-cost-row" in styles
    assert "Today API estimate" in script and "API estimate · retained high-water" in script
    assert "Ever-observed tokens" in script and "not canonical all-time" in script
    assert "costValue(estimate, apiCurrency)" in script
    assert '/^[A-Z]{3}$/' in script
    assert 'Object.assign({}, costs.api_rate_estimate' not in script
    assert 'historyStore.set(chartID, {daily, currencies, metadata})' in script
    assert 'number(history.all_time_total_tokens) !== null ? "All-time" : "Retained total"' in script
    assert ".usage-overview" in styles
    assert '<details class="usage-disclosure">' in script
    assert "Details, provenance &amp; history" in script
    assert "compactProviderSummary(provider, groups)" in script
    assert "primaryQuotaGroup(groups, provider)" in script
    assert "source.find(isAccount) || source.find(hasSession)" in script
    assert "usdAmount(provider.costs && provider.costs.api_rate_estimate)" in script
    assert "Today est." in script and "latestDailyCost(provider)" in script
    assert "Total Cost Est." in script
    assert 'localStorage.getItem("coord.usage-strip-expanded")' in script
    assert 'localStorage.setItem("coord.usage-strip-expanded"' in script
    assert 'summaryWindows: [["S", session], ["W", weekly]]' in script
    assert "Total Cost" in script
    assert ".usage-compact-summary" in styles and ".usage-disclosure" in styles
    assert '.usage-board-strip summary' in styles
    assert 'min-height:36px' in styles
    assert 'body:not([data-view="overview"]) .usage-board-strip{display:none}' in styles
    assert ".usage-account-grid{grid-template-columns:minmax(0,1fr)}" in styles
