from __future__ import annotations

import json

from coordharness.usage.provider_management import ProviderManagementForwarder


def document():
    return {"schema": "coordharness.provider-management.v1", "catalog": [{
        "id": "openai", "display_name": "OpenAI API", "short_name": "OpenAI",
        "accent": "#10A37F", "auth_modes": ["api_key"], "default_auth_mode": "api_key",
        "capabilities": ["chat", "code", "tools"], "routable": True, "builtin": True,
        "enabled": True, "priority": 70, "credential_env": "OPENAI_API_KEY",
        "dashboard_url": "https://example.invalid/private"}],
        "profiles": {"openai": {"active": "p_0123456789ab", "profiles": [{
            "id": "p_0123456789ab", "label": "Team API", "active": True,
            "isolated": True, "auth_mode": "api_key", "enabled": True,
            "priority": 50, "credential_set": True, "endpoint": "https://private.internal"}]}},
        "routing_policy": {"version": 1, "mode": "advisory", "min_session_remaining": 15,
            "min_weekly_remaining": 20, "min_runway_minutes": 60,
            "allow_metered_api": True, "prefer_subscription": True, "prefer_local": False,
            "required_capabilities": ["code", "tools"]},
        "recommendation": {"decision": "recommendation_available", "candidates": [{
            "provider": "openai", "account": "p_0123456789ab", "display_name": "OpenAI API",
            "eligible": True, "score": 100, "session_remaining": None,
            "weekly_remaining": None, "runway_minutes": None, "metered": True,
            "reasons": ["meets routing policy"]}], "selected": {"provider": "openai"}}}


def test_proxy_sanitizes_provider_document_and_omits_private_fields():
    raw = json.dumps(document()).encode()
    forwarder = ProviderManagementForwarder(transport=lambda method, body: (200, raw))
    status, result = forwarder.status()
    assert status == 200
    assert result["schema"] == "coord.provider-management.v1"
    assert result["profiles"]["openai"]["profiles"][0]["credential_set"] is True
    serialized = json.dumps(result)
    assert "OPENAI_API_KEY" not in serialized
    assert "private.internal" not in serialized
    assert "example.invalid" not in serialized


def test_proxy_forwards_write_only_credential_but_never_returns_it():
    captured = {}
    raw = json.dumps(document()).encode()
    def transport(method, body):
        captured["method"] = method
        captured["body"] = body
        return 200, raw
    forwarder = ProviderManagementForwarder(transport=transport)
    status, result = forwarder.forward({"action": "credential_set", "provider_id": "openai",
        "profile_id": "p_0123456789ab", "credential": "sk-private"})
    assert status == 200 and captured["method"] == "POST"
    assert b"sk-private" in captured["body"]
    assert "sk-private" not in json.dumps(result)
