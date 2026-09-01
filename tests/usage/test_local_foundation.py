from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from coordharness.usage.account_actions import UsageAccountActionForwarder
from coordharness.usage.dashboard_proxy import UsageDashboardProxy
from coordharness.usage.local_account_actions import LocalAccountActionService
from coordharness.usage.local_history import discover_local_cli_history
from coordharness.usage.local_profiles import LocalProfileRegistry, LocalRoutingPolicyStore
from coordharness.usage.local_provider_management import LocalProviderManagementService
from coordharness.usage.local_service import (
    LocalUsageService,
    ProviderProbe,
    _quota_pace,
    probe_codex_account,
)
from coordharness.usage.provider_management import ProviderManagementForwarder


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).with_name("fixtures")


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "clean-home"
    _write_jsonl(
        home / ".claude" / "projects" / "synthetic" / "session.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-08-28T10:00:00Z",
                "message": {"content": "private prompt"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-28T10:01:00Z",
                "message": {
                    "model": "claude-test",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 4,
                    },
                    "content": "private answer",
                },
            },
        ],
    )
    _write_jsonl(
        home / ".codex" / "sessions" / "2026" / "08" / "session.jsonl",
        [
            {
                "timestamp": "2026-08-28T11:00:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-test", "cwd": "/private/project"},
            },
            {
                "timestamp": "2026-08-28T11:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 30,
                            "cached_input_tokens": 5,
                            "output_tokens": 7,
                        }
                    },
                    "private": "secret",
                },
            },
        ],
    )
    return home


def _claude() -> ProviderProbe:
    return ProviderProbe(
        account={"status": "active", "plan": "max", "authenticated": True},
        errors=("claude_quota_unavailable",),
    )


def _codex() -> ProviderProbe:
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
            {
                "kind": "weekly",
                "name": "weekly",
                "window_minutes": 10080,
                "used_percent": 40,
                "remaining_percent": 60,
                "resets_at": "2026-09-01T12:00:00Z",
                "countdown_seconds": 345600,
            },
        ),
        observed_at="2026-08-28T12:00:00Z",
        account_source="codex_app_server",
        quota_source="codex_app_server",
    )


def test_clean_home_history_import_is_bounded_partial_and_nonidentifying(tmp_path: Path) -> None:
    home = _home(tmp_path)
    claude = discover_local_cli_history(home / ".claude", provider="claude")
    codex = discover_local_cli_history(home / ".codex", provider="codex")

    assert claude.coverage_state == codex.coverage_state == "partial"
    assert claude.rows[0].input_tokens == 10
    assert claude.rows[0].cache_create_other_tokens == 4
    assert codex.rows[0].input_tokens == 30
    assert codex.rows[0].cache_read_tokens == 5
    assert claude.records_accepted == codex.records_accepted == 1
    provenance = json.dumps([claude.provenance(), codex.provenance()])
    assert str(home) not in provenance
    assert "private prompt" not in provenance and "private answer" not in provenance


def test_bounded_history_scan_prefers_recent_files(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    older = root / "sessions" / "z-older.jsonl"
    newer = root / "sessions" / "a-newer.jsonl"
    for path, model in ((older, "model-old"), (newer, "model-new")):
        _write_jsonl(
            path,
            [
                {
                    "timestamp": "2026-08-28T11:00:00Z",
                    "type": "turn_context",
                    "payload": {"model": model},
                },
                {
                    "timestamp": "2026-08-28T11:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 1,
                                "cached_input_tokens": 0,
                                "output_tokens": 1,
                            }
                        },
                    },
                },
            ],
        )
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    imported = discover_local_cli_history(root, provider="codex", max_files=1)

    assert imported.files_scanned == 1
    assert [row.model for row in imported.rows] == ["model-new"]


def test_redacted_real_shape_fixture_emits_per_day_model_detail(tmp_path: Path) -> None:
    """The fixture preserves the turn_context/token_count shape emitted by Codex history."""

    home = tmp_path / "clean-home"
    target = home / ".codex" / "sessions" / "2026" / "08" / "session.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes((FIXTURES / "codex_model_attribution.jsonl").read_bytes())

    service = LocalUsageService(
        home=home, now=lambda: NOW, claude_probe=_claude, codex_probe=_codex
    )
    day = UsageDashboardProxy(url="", local_provider=service.dashboard).get()["providers"]["codex"][
        "history"
    ]["daily"][0]

    assert day["total_tokens"] == 44
    assert [item["label"] for item in day["model_breakdowns"]] == [
        "model-beta",
        "model-alpha",
    ]
    assert [item["total_tokens"] for item in day["model_breakdowns"]] == [29, 15]


def test_missing_model_attribution_keeps_daily_totals_with_public_fallback(
    tmp_path: Path,
) -> None:
    home = tmp_path / "clean-home"
    _write_jsonl(
        home / ".codex" / "sessions" / "2026" / "08" / "session.jsonl",
        [
            {
                "timestamp": "2026-08-28T11:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 30,
                            "cached_input_tokens": 5,
                            "output_tokens": 7,
                        }
                    },
                },
            }
        ],
    )

    service = LocalUsageService(
        home=home, now=lambda: NOW, claude_probe=_claude, codex_probe=_codex
    )
    day = UsageDashboardProxy(url="", local_provider=service.dashboard).get()["providers"]["codex"][
        "history"
    ]["daily"][0]

    assert day["total_tokens"] == 42
    assert day["model_breakdowns"] == [
        {
            "key": day["model_breakdowns"][0]["key"],
            "label": "Unknown model",
            "total_tokens": 42,
            "input_tokens": 30,
            "output_tokens": 7,
            "cache_read_tokens": 5,
            "cache_create_5m_tokens": 0,
            "cache_create_1h_tokens": 0,
            "cache_create_other_tokens": 0,
            "provider_native_cost_nanos": None,
            "api_rate_estimate_nanos": None,
        }
    ]


def _install_cost_cache_fixtures(home: Path) -> None:
    cache_root = home / "Library" / "Caches" / "CodexBar" / "cost-usage"
    cache_root.mkdir(parents=True)
    for provider in ("claude", "codex"):
        source = FIXTURES / f"{provider}_cost_cache.json"
        (cache_root / f"{provider}-v1.json").write_bytes(source.read_bytes())


def test_local_cost_caches_add_today_cost_without_replacing_history(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _install_cost_cache_fixtures(home)
    service = LocalUsageService(
        home=home, now=lambda: NOW, claude_probe=_claude, codex_probe=_codex
    )
    document = UsageDashboardProxy(url="", local_provider=service.dashboard).get()

    expected = {"claude": (37, 1_250_000_000), "codex": (42, 2_500_000_000)}
    for provider, (tokens, cost) in expected.items():
        provider_doc = document["providers"][provider]
        day = provider_doc["history"]["daily"][0]
        assert day["total_tokens"] == tokens
        assert day["api_rate_estimate_nanos"] == cost
        assert day["model_breakdowns"][0]["total_tokens"] == tokens
        assert day["model_breakdowns"][0]["api_rate_estimate_nanos"] == cost
        assert provider_doc["costs"]["api_rate_estimate"] == {
            "amount_nanos": cost,
            "currency": "USD",
            "semantics": "local_cost_usage_cache_projection_noncanonical",
            "source": {
                "kind": "local_cost_usage_cache",
                "canonical": False,
                "label": "Local API-rate estimate cache",
                "warning": (
                    "Read-only local cache estimate matched to local history; not provider billing"
                ),
            },
            "observed_at": "2026-08-28T12:00:00Z",
            "coverage_start": "2026-08-28",
            "coverage_end": "2026-08-28",
        }

    serialized = json.dumps(document)
    assert "must-not-cross" not in serialized
    assert "canonicalProjectPath" not in serialized


def test_no_upstream_dashboard_uses_only_local_state_and_honest_quota(tmp_path: Path) -> None:
    home = _home(tmp_path)
    service = LocalUsageService(
        home=home, now=lambda: NOW, claude_probe=_claude, codex_probe=_codex
    )
    document = UsageDashboardProxy(url="", local_provider=service.dashboard).get()

    assert document["refresh"]["state"] == "fresh"
    assert document["providers"]["claude"]["history"]["rolling_7d_total_tokens"] == 37
    assert document["providers"]["claude"]["windows"] == []
    assert (
        document["providers"]["claude"]["live_observation_state"] == "quota_observation_unavailable"
    )
    assert document["providers"]["codex"]["windows"][0]["remaining_percent"] == 80
    assert document["providers"]["codex"]["history"]["rolling_7d_total_tokens"] == 42
    pace = document["providers"]["codex"]["windows"][0]["pace"]
    assert pace == {
        "state": "reserve",
        "delta_percent": 20.0,
        "expected_used_percent": 40.0,
        "will_last_to_reset": True,
        "seconds_to_exhaustion": None,
        "advisory": True,
        "basis": "elapsed_window_linear_projection",
        "source": "local_projection",
        "marker_remaining_percent": 60.0,
        "marker_kind": "reserve",
    }
    assert document["providers"]["codex"]["runout"]["basis"] == "would_cross_reset_boundary"
    claude_day = document["providers"]["claude"]["history"]["daily"][0]
    codex_day = document["providers"]["codex"]["history"]["daily"][0]
    assert claude_day["model_breakdowns"][0]["label"] == "claude-test"
    assert claude_day["model_breakdowns"][0]["total_tokens"] == 37
    assert codex_day["model_breakdowns"][0]["label"] == "gpt-test"
    assert codex_day["model_breakdowns"][0]["total_tokens"] == 42
    assert claude_day["api_rate_estimate_nanos"] is None
    assert codex_day["api_rate_estimate_nanos"] is None
    assert document["providers"]["claude"]["costs"]["api_rate_estimate"] == {
        "amount_nanos": None,
        "semantics": "unknown",
    }
    assert document["providers"]["codex"]["costs"]["api_rate_estimate"] == {
        "amount_nanos": None,
        "semantics": "unknown",
    }
    serialized = json.dumps(document)
    for private in (str(home), "/private/project", "private prompt", "private answer", "secret"):
        assert private not in serialized


def test_local_pace_has_a_two_point_deadband_and_reset_bounded_runout() -> None:
    reset = NOW.replace(hour=12, minute=50)
    on_pace = _quota_pace(48.0, 100, reset, NOW)
    reserve = _quota_pace(47.99, 100, reset, NOW)
    deficit = _quota_pace(52.01, 100, reset, NOW)

    assert on_pace is not None and on_pace["state"] == "on_pace"
    assert on_pace["marker_kind"] is None
    assert reserve is not None and reserve["state"] == "reserve"
    assert reserve["marker_kind"] == "reserve"
    assert deficit is not None and deficit["state"] == "deficit"
    assert deficit["marker_kind"] == "deficit"
    assert deficit["will_last_to_reset"] is False
    assert deficit["seconds_to_exhaustion"] is not None


def test_codex_official_probe_sanitizes_account_and_quota_frames(tmp_path: Path) -> None:
    def runner(command, requests, timeout):
        assert command[-1] == "app-server" and [row.get("id") for row in requests] == [
            1,
            None,
            2,
            3,
        ]
        return [
            {
                "id": 2,
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "planType": "plus",
                        "email": "private@example.test",
                        "token": "secret",
                    }
                },
            },
            {
                "id": 3,
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 300,
                            "resetsAt": "2026-08-28T15:00:00Z",
                        }
                    }
                },
            },
        ]

    probe = probe_codex_account(tmp_path, runner=runner, now=NOW)

    assert probe.account == {"status": "active", "plan": "plus", "authenticated": True}
    assert probe.windows[0]["remaining_percent"] == 75
    assert "private@example.test" not in json.dumps(probe.__dict__)
    assert "secret" not in json.dumps(probe.__dict__)


def test_profile_metadata_is_private_and_never_persists_credentials(tmp_path: Path) -> None:
    path = tmp_path / "clean-home" / ".coord" / "provider-profiles.json"
    registry = LocalProfileRegistry(path)
    profile_id = registry.add("claude", "Work account")

    assert registry.public_status()["claude"]["active"] == profile_id
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text(encoding="utf-8")
    assert set(json.loads(raw)) == {"version", "active", "profiles"}
    for forbidden in ("credential", "token", "secret", "api_key", "email"):
        assert forbidden not in raw.lower()


def test_local_login_actions_are_explicitly_unsupported_never_connected(tmp_path: Path) -> None:
    usage = LocalUsageService(
        home=tmp_path, now=lambda: NOW, claude_probe=_claude, codex_probe=_codex
    )
    local = LocalAccountActionService(usage=usage, profiles=LocalProfileRegistry(home=tmp_path))
    forwarder = UsageAccountActionForwarder(dashboard_url="", local_service=local)

    status, document = forwarder.forward("codex_login_start")

    assert status == 501
    assert document["ok"] is False
    assert document["result"] == "unsupported_local_action"
    assert document["codex"] == {"state": "idle", "can_start": False, "can_cancel": False}
    assert document["claude"]["connect_available"] is False


def test_local_provider_routing_consumes_quota_and_is_advisory_by_default(tmp_path: Path) -> None:
    home = _home(tmp_path)
    usage = LocalUsageService(home=home, now=lambda: NOW, claude_probe=_claude, codex_probe=_codex)
    profiles = LocalProfileRegistry(home=home)
    policy = LocalRoutingPolicyStore(home=home)
    local = LocalProviderManagementService(usage=usage, profiles=profiles, policy=policy)
    forwarder = ProviderManagementForwarder(dashboard_url="", local_service=local)

    status, document = forwarder.status()

    assert status == 200
    assert document["routing_policy"]["mode"] == "advisory"
    assert document["recommendation"]["selected"]["provider"] == "codex"
    assert document["recommendation"]["automatic_execution"] is False
    claude = next(
        row for row in document["recommendation"]["candidates"] if row["provider"] == "claude"
    )
    assert claude["eligible"] is False
    assert "current provider quota unavailable" in claude["reasons"]

    status, response = forwarder.forward(
        {
            "action": "credential_set",
            "provider_id": "codex",
            "profile_id": "default",
            "credential": "sk-private",
        }
    )
    assert status == 501 and response["ok"] is False
    assert "sk-private" not in json.dumps(response)
    assert not (home / ".coord" / "provider-profiles.json").exists()
