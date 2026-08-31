from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from coordharness.usage.account_actions import UsageAccountActionForwarder
from coordharness.usage.dashboard_proxy import UsageDashboardProxy
from coordharness.usage.local_account_actions import LocalAccountActionService
from coordharness.usage.local_history import discover_local_cli_history
from coordharness.usage.local_profiles import LocalProfileRegistry, LocalRoutingPolicyStore
from coordharness.usage.local_provider_management import LocalProviderManagementService
from coordharness.usage.local_service import LocalUsageService, ProviderProbe, probe_codex_account
from coordharness.usage.provider_management import ProviderManagementForwarder


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True)
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
    claude_day = document["providers"]["claude"]["history"]["daily"][0]
    codex_day = document["providers"]["codex"]["history"]["daily"][0]
    assert claude_day["model_breakdowns"][0]["label"] == "claude-test"
    assert claude_day["model_breakdowns"][0]["total_tokens"] == 37
    assert codex_day["model_breakdowns"][0]["label"] == "gpt-test"
    assert codex_day["model_breakdowns"][0]["total_tokens"] == 42
    serialized = json.dumps(document)
    for private in (str(home), "/private/project", "private prompt", "private answer", "secret"):
        assert private not in serialized


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
