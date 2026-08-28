from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import threading

import pytest

from coordharness import demo
from coordharness.board.server import make_server
from coordharness.usage.dashboard_proxy import USAGE_CONTRACT


playwright_api = pytest.importorskip("playwright.sync_api")


def _provider(name: str) -> dict:
    provider = {
        "source": {
            "kind": "legacy_high_water" if name == "claude" else "usage-v2",
            "canonical": name == "codex",
            "warning": "Ever-observed custody envelope; not billed subscription credits."
            if name == "claude" else None,
        },
        "quota_source": {
            "kind": "fixture_live",
            "canonical": True,
            "label": "Fixture live quota",
        },
        "account": {"status": "authenticated", "plan": "test", "authenticated": True},
        "windows": [
            {
                "kind": "session",
                "name": "5 hour",
                "window_minutes": 300,
                "used_percent": 21 if name == "codex" else 25,
                "remaining_percent": None if name == "codex" else 75,
                "resets_at": "2026-08-26T15:00:00Z",
                "countdown_seconds": 10_800,
                "pace": {
                    "state": "reserve",
                    "delta_percent": 12,
                    "expected_used_percent": 37,
                    "will_last_to_reset": True,
                    "seconds_to_exhaustion": None,
                },
            },
            {
                "kind": "weekly",
                "pace": {
                    "state": "deficit",
                    "delta_percent": 9,
                    "expected_used_percent": 31,
                    "will_last_to_reset": False,
                    "seconds_to_exhaustion": 86_400,
                },
                "name": "Weekly",
                "window_minutes": 10_080,
                "used_percent": 40,
                "remaining_percent": 60,
                "resets_at": "2026-09-01T12:00:00Z",
                "countdown_seconds": 518_400,
            },
        ],
        "reset_credits": ([{
            "status": "inventory",
            "count": 1,
            "expires_at": None,
            "semantics": "earned_credit_inventory_not_current_reset_eligibility",
        }] if name == "codex" else []),
        "runout": {
            "kind": "current_window_linear",
            "advisory": True,
            "estimated_exhausts_at": "2026-08-27T12:00:00Z",
            "seconds_to_exhaustion": 86_400,
            "basis": "current window linear pace",
        },
        "history": {
            "daily": [
                {"date": "2026-08-25", "total_tokens": 100, "api_rate_estimate_nanos": 1_500_000_000},
                {"date": "2026-02-27", "total_tokens": 50, "provider_native_cost_nanos": 750_000_000},
                {"date": "2026-08-26", "total_tokens": 200, "api_rate_estimate_nanos": 2_500_000_000, "provider_native_cost_nanos": 1_000_000_000},
            ],
            "rolling_7d_total_tokens": 700,
            "calendar_week_total_tokens": 600,
            "all_time_total_tokens": None if name == "claude" else 1_000,
            "semantics": "canonical_correctable",
            "ever_observed_envelope": {"total_tokens": 1_500},
        },
        "breakdowns": {
            "models": {"status": "ok", "semantics": "canonical_correctable" if name == "codex" else "ever_observed_envelope", "canonical": name == "codex", "coverage_start": "2026-08-25", "coverage_end": "2026-08-26", "observed_at": "2026-08-26T12:00:00Z", "omitted_count": 0, "items": [{"key": f"{name}-model", "label": f"{name}-test-model", "total_tokens": 300, "today_total_tokens": 200, "rolling_7d_total_tokens": 300, "calendar_week_total_tokens": 300, "provider_native_cost_nanos": None, "api_rate_estimate_nanos": 500_000_000}]},
            "projects": {"status": "ok", "semantics": "fixture_project_projection", "canonical": False, "coverage_start": "2026-08-25", "coverage_end": "2026-08-26", "observed_at": "2026-08-26T12:00:00Z", "omitted_count": 0, "items": [{"key": f"{name}-project", "label": f"{name} harness", "total_tokens": 300, "today_total_tokens": 200, "rolling_7d_total_tokens": 300, "calendar_week_total_tokens": 300, "top_model": f"{name}-test-model"}]},
        },
        "costs": {
            "provider_billed": {"amount_nanos": None, "currency": None, "by_currency": {"EUR": 2_000_000_000, "USD": 3_000_000_000} if name == "codex" else None, "semantics": "provider_billed" if name == "codex" else "unknown"},
            "provider_native": {"amount_nanos": None, "semantics": "unknown"},
            "api_rate_estimate": {
                "amount_nanos": 2_500_000_000,
                "currency": "USD",
                "semantics": "api_rate_estimate",
            },
        },
        "active_sessions": {"status": "available", "count": 2, "providers": [name]},
        "errors": [],
    }
    provider["quota_groups"] = [
        {
            "key": f"{name}-account-quota",
            "label": "Account quota",
            "semantics": "provider_quota_meter",
            "windows": deepcopy(provider["windows"]),
            "runout": deepcopy(provider["runout"]),
        }
    ]
    if name == "claude":
        provider["quota_groups"].append(
            {
                "key": "claude-fable-only",
                "label": "Fable only",
                "semantics": "provider_named_quota_meter",
                "windows": [
                    {
                        "kind": "weekly",
                        "name": "Fable only",
                        "window_minutes": 10_080,
                        "used_percent": 69,
                        "remaining_percent": 31,
                        "resets_at": "2026-09-01T11:59:00Z",
                        "countdown_seconds": 518_340,
                        "pace": {
                            "state": "deficit",
                            "delta_percent": 38,
                            "expected_used_percent": 31,
                            "will_last_to_reset": False,
                            "seconds_to_exhaustion": 82_000,
                        },
                    }
                ],
                "runout": {
                    "kind": "current_window_linear",
                    "advisory": True,
                    "estimated_exhausts_at": "2026-08-27T10:46:40Z",
                    "seconds_to_exhaustion": 82_000,
                    "basis": "current window linear pace",
                },
            }
        )
    return provider


def _payload() -> dict:
    return {
        "schema": USAGE_CONTRACT,
        "generated_at": "2026-08-26T12:00:00Z",
        "stale_after": "2026-08-26T12:05:00Z",
        "refresh": {"state": "fresh", "generated_at": "2026-08-26T12:00:00Z"},
        "providers": {"claude": _provider("claude"), "codex": _provider("codex")},
        "errors": [],
    }


class _FixedProxy:
    url = "http://127.0.0.1:7870/api/usage/v1"

    def get(self) -> dict:
        return deepcopy(_payload())


class _FixedSystemTelemetryProxy:
    def get(self, *, demand: bool = False) -> dict:
        gib = 1024**3
        return {
            "schema_version": 1,
            "generated_at": "2026-08-26T12:00:00Z",
            "sequence": 7,
            "freshness": {"state": "fresh", "age_seconds": 0},
            "cadence": {"mode": "demand" if demand else "idle"},
            "cpu": {
                "availability": "available",
                "source": "fixture_cpu",
                "usage_percent": 12,
            },
            "gpu": {
                "availability": "available",
                "source": "fixture_gpu",
                "usage_percent": 23,
                "renderer_percent": 19,
                "tiler_percent": 7,
            },
            "memory": {
                "availability": "available",
                "source": "fixture_memory",
                "used_percent": 34,
                "used_bytes": 34 * gib,
                "total_bytes": 100 * gib,
                "free_bytes": 66 * gib,
                "swap_used_bytes": 2 * gib,
            },
            "disk": {
                "availability": "available",
                "source": "fixture_disk",
                "used_percent": 45,
                "used_bytes": 450 * gib,
                "total_bytes": 1000 * gib,
                "free_bytes": 550 * gib,
                "read_bps": 1024**2,
                "write_bps": 2 * 1024**2,
            },
        }

class _FixedAccountForwarder:
    def status(self) -> tuple[int, dict]:
        return 200, {
            "schema": "coord.usage-account-actions.v1",
            "codex": {"state": "idle", "can_start": True, "can_cancel": False},
            "claude": {
                "state": "manual_connect_required",
                "connect_available": True,
                "opened": False,
            },
        }

    def forward(self, _action: str) -> dict:
        raise AssertionError("browser QA must never initiate provider account actions")


class _OutcomeAccountForwarder:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def status(self) -> tuple[int, dict]:
        return 200, {
            "schema": "coord.usage-account-actions.v1",
            "codex": {"state": "idle", "can_start": True, "can_cancel": True},
            "claude": {
                "state": "waiting_user",
                "connect_available": True,
                "opened": False,
            },
        }

    def forward(self, action: str) -> tuple[int, dict]:
        self.actions.append(action)
        codex = (
            {"state": "waiting_browser", "can_start": False, "can_cancel": True}
            if action == "codex_login_start"
            else {"state": "idle", "can_start": True, "can_cancel": False}
        )
        result = (
            "login_already_active"
            if action == "codex_login_start"
            else "no_active_login"
        )
        return 409, {
            "schema": "coord.usage-account-actions.v1",
            "codex": codex,
            "claude": {
                "state": "waiting_user",
                "connect_available": True,
                "opened": False,
            },
            "ok": False,
            "result": result,
        }


@contextmanager
def _board(tmp_path: Path, *, account_forwarder=None):
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(
        host="127.0.0.1",
        port=0,
        db_path=str(database),
        usage_dashboard_proxy=_FixedProxy(),
        system_telemetry_proxy=_FixedSystemTelemetryProxy(),
        usage_account_forwarder=account_forwarder or _FixedAccountForwarder(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cockpit_usage_strip_is_single_row_minimal_collapsible_and_persistent(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 760})
        try:
            page.goto(f"{url}/#v=overview", wait_until="networkidle")
            strip = page.locator("#usage-strip")
            details = strip.locator("details")
            summary = strip.locator("summary")
            details.wait_for()
            page.locator('[data-system-card="disk"] strong').wait_for(state="attached")

            assert page.locator("#system-telemetry-strip").count() == 0
            assert details.get_attribute("open") is None
            assert summary.bounding_box()["height"] <= 40
            assert strip.locator(".usage-strip-provider").count() == 2
            assert strip.locator(".usage-strip-mini>b").all_text_contents() == [
                "S", "W", "S", "W",
            ]
            assert strip.locator(".usage-strip-mini>strong").all_text_contents() == [
                "75%", "60%", "79%", "60%",
            ]
            assert strip.locator(".usage-strip-mini>progress").count() == 4
            assert strip.locator('.usage-strip-provider [style*="width"]').count() == 0
            assert strip.locator(".usage-strip-system-metric b").all_text_contents() == [
                "CPU", "GPU", "RAM", "Disk",
            ]
            assert strip.locator(".usage-strip-system-metric strong").all_text_contents() == [
                "12%", "23%", "34%", "45%",
            ]
            assert "Cost" not in summary.inner_text()
            page.screenshot(
                path=str(tmp_path / "coord-usage-system-strip-collapsed.png"),
                full_page=True,
            )

            summary.click()
            assert details.get_attribute("open") == ""
            assert strip.get_by_text("Cost", exact=True).count() == 2
            assert strip.locator(".usage-strip-quota").count() == 5
            assert strip.locator(".usage-strip-system-card").count() == 4
            assert strip.locator('[data-system-card="disk"]').get_by_text(
                "45%", exact=True
            ).count() == 1
            assert strip.locator('[data-system-card="gpu"]').get_by_text(
                "Renderer", exact=True
            ).count() == 1
            assert strip.bounding_box()["height"] <= 250
            page.screenshot(
                path=str(tmp_path / "coord-usage-system-strip-expanded.png"),
                full_page=True,
            )

            disk_toggle = strip.locator('[data-system-metric="disk"]')
            disk_toggle.uncheck()
            page.wait_for_function(
                "() => JSON.parse(localStorage.getItem('coord.system-telemetry.metrics.v1')).disk === false"
            )
            assert strip.locator('.usage-strip-system-metric b').all_text_contents() == [
                "CPU", "GPU", "RAM",
            ]
            assert strip.locator('[data-system-card="disk"]').count() == 1

            page.reload(wait_until="networkidle")
            assert page.locator("#usage-strip details").get_attribute("open") == ""
            assert page.locator('.usage-strip-system-metric b').all_text_contents() == [
                "CPU", "GPU", "RAM",
            ]
            assert page.locator('[data-system-card="disk"]').count() == 1
            page.locator("#usage-strip summary").click()
            page.wait_for_function(
                "() => localStorage.getItem('coord.usage-strip-expanded') === '0'"
            )
            page.reload(wait_until="networkidle")
            assert page.locator("#usage-strip details").get_attribute("open") is None

            page.set_viewport_size({"width": 390, "height": 760})
            dimensions = page.evaluate(
                "() => ({body: document.body.scrollWidth, viewport: innerWidth})"
            )
            assert dimensions["body"] <= dimensions["viewport"]
        finally:
            browser.close()


def test_provider_usage_surface_renders_distinct_semantics_and_accessible_graphs(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"{url}/#v=usage", wait_until="networkidle")
            page.get_by_role("heading", name="Provider Usage", exact=True).wait_for()

            usage_destination = page.get_by_role(
                "navigation", name="Product areas"
            ).get_by_role("link", name="Usage", exact=True)
            assert usage_destination.is_visible()
            assert usage_destination.get_attribute("aria-current") == "page"
            assert page.locator("#rail").is_hidden()
            assert not page_errors
            render_error = page.evaluate(
                """async () => {
                    const payload = await (await fetch('/api/v1/usage-dashboard')).json();
                    try { window.CoordUsageDashboard.render(payload); return null; }
                    catch (error) { return String(error && error.stack || error); }
                }"""
            )
            assert render_error is None, render_error
            assert page.locator(".usage-provider").count() == 2, page.locator("#usage").inner_text()
            assert page.locator('[data-provider="claude"]').count() == 1
            assert page.locator('[data-provider="codex"]').count() == 1
            assert page.locator(".usage-provider").evaluate_all(
                "nodes => nodes.map(node => node.dataset.provider)"
            ) == ["claude", "codex"]
            assert page.locator("details.usage-disclosure[open]").count() == 0
            assert page.get_by_text("Today est.", exact=True).count() == 2
            assert page.get_by_text("Total Cost Est.", exact=True).count() == 2
            compact = page.locator(".usage-compact-summary")
            assert compact.get_by_text("Session", exact=True).count() == 2
            assert compact.get_by_text("Weekly", exact=True).count() == 2
            assert compact.count() == 2
            page.screenshot(path=str(tmp_path / "coord-usage-compact-default.png"), full_page=True)
            page.locator("details.usage-disclosure summary").evaluate_all(
                "nodes => nodes.forEach(node => node.click())"
            )
            assert page.locator("details.usage-disclosure[open]").count() == 2
            assert page.locator(".usage-quota-group").count() == 3
            assert page.locator(".usage-window").count() == 5
            assert page.locator(".usage-quota-track").count() == 5
            assert page.get_by_text("25% used", exact=True).count() == 1
            assert page.get_by_text("21% used", exact=True).count() == 1
            assert page.get_by_text("40% used", exact=True).count() == 2
            assert page.get_by_text("75% left", exact=True).count() == 2
            assert page.get_by_text("79% left", exact=True).count() == 2
            codex_session = page.locator('[data-provider="codex"] .usage-window').filter(has_text="21% used")
            assert codex_session.locator(".usage-quota-track i").evaluate("element => element.style.width") == "79%"
            assert page.get_by_text("60% left", exact=True).count() == 4
            assert page.get_by_role("heading", name="Fable only", exact=True).count() == 1
            assert page.get_by_text("69% used", exact=True).count() == 1
            assert page.get_by_text("31% left", exact=True).count() == 1
            fable = page.locator('[data-quota-group="claude-fable-only"]')
            assert fable.locator(".usage-quota-track i").evaluate("element => element.style.width") == "31%"

            content = page.locator("#usage").inner_text()
            assert "separate from CORD's coordination token ledger" in content
            assert "Token receipts are not subscription credits or billed spend" in content
            assert "Provider billed" in content
            assert "Provider native" in content
            assert "API-rate estimate" in content
            assert "to estimated exhaustion" in content
            assert "Fixture live quota · canonical quota" in content
            assert page.get_by_role("heading", name="Earned reset-credit inventory", exact=True).count() == 1
            assert "1 earned credit · current reset eligibility unavailable" in content
            assert "Reserve" in content and "Deficit" in content
            assert "projected to last until reset" in content
            assert "may run out before reset" in content
            assert "Today API estimate" in content
            assert "API estimate · retained high-water" in content
            assert "not billed spend" in content
            assert "Active sessions: 2" in content
            assert "Unknown" in content
            assert "$0" not in content
            assert "$2.50" in content
            assert "$3.00" in content
            assert "€2.00" in content
            assert "MODELS" in content and "PROJECTS" in content
            assert "claude-test-model" in content and "codex-test-model" in content
            assert "claude harness" in content and "codex harness" in content
            assert "coverage 300 tokens" in content
            assert "/private" not in content

            graphs = page.locator('svg.usage-history[role="img"]')
            assert graphs.count() == 2
            assert page.locator("svg.usage-history > title").count() == 2
            assert page.get_by_text("Daily estimated cost", exact=True).count() == 2
            assert page.locator("svg.usage-history desc").count() == 2
            assert page.locator(".usage-history-bar[tabindex='0']").count() == 6
            assert page.locator("svg.usage-history polyline").count() == 0
            assert "Feb 27" in page.locator('[data-provider="claude"] .usage-history-caption').inner_text()
            assert "missing" in page.locator('[data-provider="claude"] .usage-coverage-note').inner_text()
            assert "not plotted as zero" in page.locator('[data-provider="claude"] .usage-coverage-note').inner_text()
            page.screenshot(path=str(tmp_path / "coord-usage-desktop.png"), full_page=True)

            account_methods: list[str] = []
            page.on(
                "request",
                lambda request: account_methods.append(request.method)
                if "/api/v1/usage-actions" in request.url
                else None,
            )
            page.get_by_role("button", name="Provider Accounts", exact=True).click()
            dialog = page.locator("dialog[data-provider-accounts]")
            assert dialog.is_visible()
            assert dialog.get_by_role("button", name="Start sign-in").count() == 1
            assert dialog.get_by_role("button", name="Cancel sign-in").count() == 1
            assert dialog.get_by_role("button", name="Open Claude Code sign-in").count() == 1
            assert "Open direct Claude Code sign-in via the local provider service" in dialog.inner_text()
            assert "POST" not in account_methods
            page.screenshot(path=str(tmp_path / "coord-provider-accounts.png"), full_page=True)
            dialog.get_by_role("button", name="Close Provider Accounts").click()
            page.locator("details.usage-disclosure summary").evaluate_all(
                "nodes => nodes.forEach(node => node.click())"
            )

            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator("#usage").evaluate("(node) => node.scrollWidth <= node.clientWidth + 1")
            page.screenshot(path=str(tmp_path / "coord-usage-narrow.png"), full_page=True)

        finally:
            browser.close()


def test_usage_interactions_preserve_truth_across_ranges_days_stale_and_errors(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        try:
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"{url}/#v=usage", wait_until="networkidle")
            page.get_by_role("heading", name="Provider Usage", exact=True).wait_for()

            page.locator("details.usage-disclosure summary").evaluate_all(
                "nodes => nodes.forEach(node => node.click())"
            )
            history = page.locator('[data-provider="claude"] .usage-history-retained')
            history.locator('[data-history-range="all"]').click()
            assert "active" in (
                history.locator('[data-history-range="all"]').get_attribute("class") or ""
            )
            assert history.locator('svg.usage-history[role="img"]').count() == 1
            assert "3 observed daily cost rows" in (
                history.locator("svg desc").text_content() or ""
            )
            assert "2026-02-27" in (history.locator("svg desc").text_content() or "")
            assert "noncanonical and incomplete" in (history.locator("svg desc").text_content() or "")

            page.evaluate(
                """async () => {
                    const payload = await (await fetch('/api/v1/usage-dashboard')).json();
                    payload.providers.codex.quota_groups = [
                      {
                        key: 'codex-account-quota',
                        label: 'Account quota',
                        windows: [
                          {kind: 'weekly', name: 'Weekly', used_percent: 50, remaining_percent: 50}
                        ],
                      },
                      {
                        key: 'gpt-5.3-codex-spark',
                        label: 'GPT-5.3-Codex-Spark',
                        windows: [
                          {kind: 'session', name: 'Session', used_percent: 0, remaining_percent: 100},
                          {kind: 'weekly', name: 'Weekly', used_percent: 0, remaining_percent: 100},
                        ],
                      },
                    ];
                    window.CoordUsageDashboard.render(payload);
                }"""
            )
            codex_compact = page.locator(
                '[data-provider="codex"] .usage-compact-summary'
            )
            assert (codex_compact.locator(":scope > p").text_content() or "") == "Account quota"
            assert codex_compact.locator('[data-window-kind="session"]').count() == 0
            assert "50% left" in (
                codex_compact.locator('[data-window-kind="weekly"]').text_content() or ""
            )

            page.evaluate(
                """async () => {
                    const payload = await (await fetch('/api/v1/usage-dashboard')).json();
                    payload.generated_at = '2026-08-27T12:00:00Z';
                    payload.refresh.generated_at = payload.generated_at;
                    window.CoordUsageDashboard.render(payload);
                }"""
            )
            page.locator("details.usage-disclosure summary").evaluate_all(
                "nodes => nodes.forEach(node => node.click())"
            )
            assert page.get_by_text("Today API estimate", exact=True).count() == 0
            for provider_key in ("claude", "codex"):
                compact = page.locator(
                    f'[data-provider="{provider_key}"] .usage-compact-summary'
                )
                assert compact.get_by_text("Today est.", exact=True).count() == 1
                assert compact.get_by_text("Total Cost Est.", exact=True).count() == 1
                assert "$2.50" in (compact.text_content() or "")

            page.evaluate(
                """async () => {
                    const payload = await (await fetch('/api/v1/usage-dashboard')).json();
                    payload.providers.claude.live_observation_state = 'stale_last_good';
                    window.CoordUsageDashboard.render(payload);
                }"""
            )
            freshness = page.locator(".usage-freshness")
            assert "stale" in (freshness.get_attribute("class") or "")
            assert (freshness.locator("strong").text_content() or "") == "stale"
            assert page.locator(".usage-provider").count() == 2

            page.evaluate(
                """async () => {
                    const good = await (await fetch('/api/v1/usage-dashboard')).json();
                    window.CoordUsageDashboard.render(good);
                    window.CoordUsageDashboard.render({
                      schema: 'coordharness.usage-intelligence.v1',
                      generated_at: '2026-08-26T12:01:00Z',
                      refresh: {state: 'error', generated_at: '2026-08-26T12:01:00Z'},
                      providers: {},
                      errors: [{code: 'upstream_unavailable'}],
                    });
                }"""
            )
            assert page.locator(".usage-provider").count() == 2
            assert "stale" in (
                page.locator(".usage-freshness").get_attribute("class") or ""
            )
            alert = page.get_by_role("alert")
            assert "Showing bounded last-good usage" in alert.inner_text()
            assert "upstream_unavailable" in alert.inner_text()
            assert not page_errors
        finally:
            browser.close()


def test_account_dialog_normalizes_waiting_user_and_non_ok_known_outcomes(
    tmp_path: Path,
) -> None:
    forwarder = _OutcomeAccountForwarder()
    with _board(
        tmp_path, account_forwarder=forwarder
    ) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 800})
        try:
            page.goto(f"{url}/#v=usage", wait_until="networkidle")
            page.evaluate(
                """async () => {
                  const payload = await (await fetch('/api/v1/usage-dashboard')).json();
                  payload.providers.claude.account.authenticated = true;
                  payload.providers.claude.source = {
                    kind: 'CLAUDE_CODEXBAR_RETAINED_SNAPSHOT',
                    canonical: false,
                    label: 'CodexBar retained donor snapshot',
                    warning: 'CLAUDE_CODEXBAR_WIDGET_STALE; Codex Bar source may lag.'
                  };
                  payload.providers.claude.quota_source = {
                    kind: 'codexbar_widget_snapshot',
                    canonical: false,
                    label: 'Codex Bar widget quota snapshot',
                    warning: 'CLAUDE_CODEXBAR_QUOTA_STALE'
                  };
                  payload.errors = [
                    {code: 'CLAUDE_CODEXBAR_WIDGET_UNAVAILABLE'},
                    {code: 'upstream_unavailable'}
                  ];
                  window.CoordUsageDashboard.render(payload);
                }"""
            )
            connection = page.locator(".usage-provider-claude .usage-connection")
            playwright_api.expect(connection).to_have_text("Legacy snapshot / fallback")
            assert "connected" not in (connection.get_attribute("class") or "")
            page.locator(".usage-provider-claude details").evaluate("element => { element.open = true; }")
            visible_usage = page.locator("#usage").inner_text()
            assert "Codex Bar" not in visible_usage
            assert "CodexBar" not in visible_usage
            assert "CLAUDE_CODEXBAR" not in visible_usage
            assert "Legacy compatibility source" in visible_usage
            assert "Legacy compatibility source unavailable" in visible_usage
            assert "upstream_unavailable" in visible_usage
            assert page.get_by_role(
                "button", name="Provider Accounts", exact=True
            ).is_visible()
            page.get_by_role("button", name="Provider Accounts", exact=True).click()
            dialog = page.locator("dialog[data-provider-accounts]")

            sign_in_opened = dialog.get_by_role("button", name="Sign-in opened")
            sign_in_opened.wait_for()
            assert sign_in_opened.is_disabled()
            assert (
                "Finish the provider-owned browser flow."
                in dialog.locator("[data-account-claude-copy]").inner_text()
            )

            dialog.get_by_role("button", name="Start sign-in").click()
            playwright_api.expect(dialog.locator("[data-account-live]")).to_have_text(
                "A Codex sign-in is already active."
            )
            dialog.get_by_role("button", name="Cancel sign-in").click()
            playwright_api.expect(dialog.locator("[data-account-live]")).to_have_text(
                "There is no active Codex sign-in to cancel."
            )
            assert forwarder.actions == ["codex_login_start", "codex_login_cancel"]
        finally:
            browser.close()

def test_provider_scoped_last_good_retains_only_future_claude_quota(
    tmp_path: Path,
) -> None:
    with _board(tmp_path) as url, playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        try:
            page.goto(f"{url}/#v=usage", wait_until="networkidle")
            page.evaluate(
                """async () => {
                  const good = await (await fetch("/api/v1/usage-dashboard")).json();
                  for (const group of good.providers.claude.quota_groups) {
                    for (const window of group.windows) window.resets_at = "2099-01-01T00:00:00Z";
                  }
                  for (const window of good.providers.claude.windows) {
                    window.resets_at = "2099-01-01T00:00:00Z";
                  }
                  window.__usageGood = good;
                  CoordUsageDashboard.render(good);
                  const partial = structuredClone(good);
                  partial.generated_at = "2026-08-27T12:05:00Z";
                  partial.refresh = {state: "fresh", generated_at: partial.generated_at};
                  partial.providers.claude = {
                    account: {authenticated: true, status: "authenticated"},
                    quota_groups: [], windows: [], errors: []
                  };
                  partial.providers.codex.quota_groups[0].windows[0].remaining_percent = 44;
                  partial.providers.codex.windows[0].remaining_percent = 44;
                  window.__usagePartial = partial;
                  CoordUsageDashboard.render(partial);
                }"""
            )
            claude = page.locator(".usage-provider-claude")
            codex = page.locator(".usage-provider-codex")
            assert "75% left" in claude.inner_text()
            assert "44% left" in codex.inner_text()
            assert page.locator(".usage-freshness strong").inner_text().lower() == "stale"
            assert "Showing bounded last-good usage" in page.locator("#usage").inner_text()
            claude.locator("details").evaluate("element => { element.open = true; }")
            assert "Showing bounded last-good Claude quota" in claude.inner_text()

            page.evaluate(
                """() => {
                  const signedOut = structuredClone(window.__usagePartial);
                  signedOut.providers.claude.account = {
                    authenticated: false, status: "signed_out"
                  };
                  CoordUsageDashboard.render(signedOut);
                }"""
            )
            assert "75% left" not in page.locator(".usage-provider-claude").inner_text()

            page.evaluate(
                """() => {
                  CoordUsageDashboard.render(structuredClone(window.__usageGood));
                  const expired = structuredClone(window.__usagePartial);
                  expired.providers.claude.live_observation_state = "quota_observation_expired";
                  CoordUsageDashboard.render(expired);
                }"""
            )
            assert "75% left" not in page.locator(".usage-provider-claude").inner_text()

            page.evaluate(
                """() => {
                  const missingReset = structuredClone(window.__usageGood);
                  for (const group of missingReset.providers.claude.quota_groups) {
                    for (const window of group.windows) delete window.resets_at;
                  }
                  for (const window of missingReset.providers.claude.windows) {
                    delete window.resets_at;
                  }
                  CoordUsageDashboard.render(missingReset);
                  const partial = structuredClone(window.__usagePartial);
                  partial.providers.claude.live_observation_state = "quota_observation_unavailable";
                  CoordUsageDashboard.render(partial);
                }"""
            )
            assert "75% left" not in page.locator(".usage-provider-claude").inner_text()
        finally:
            browser.close()
