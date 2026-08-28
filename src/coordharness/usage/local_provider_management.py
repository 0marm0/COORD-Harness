"""Secret-free local provider profile and routing management service."""

from __future__ import annotations

from typing import Any, Mapping

from .local_profiles import LocalProfileRegistry, LocalRoutingPolicyStore
from .local_routing import recommend_local_dashboard
from .local_service import LocalUsageService


CATALOG = (
    {
        "id": "claude",
        "display_name": "Claude",
        "short_name": "Claude",
        "accent": "#F07A3D",
        "auth_modes": ["cli"],
        "default_auth_mode": "cli",
        "capabilities": ["chat", "code", "reasoning", "vision", "tools"],
        "routable": True,
        "builtin": True,
        "enabled": True,
        "priority": 50,
    },
    {
        "id": "codex",
        "display_name": "Codex",
        "short_name": "Codex",
        "accent": "#9B6AF3",
        "auth_modes": ["oauth"],
        "default_auth_mode": "oauth",
        "capabilities": ["chat", "code", "reasoning", "vision", "tools"],
        "routable": True,
        "builtin": True,
        "enabled": True,
        "priority": 50,
    },
)


class LocalProviderManagementService:
    def __init__(
        self,
        *,
        usage: LocalUsageService | None = None,
        profiles: LocalProfileRegistry | None = None,
        policy: LocalRoutingPolicyStore | None = None,
    ) -> None:
        self.usage = usage or LocalUsageService()
        self.profiles = profiles or LocalProfileRegistry(home=self.usage.home)
        self.policy = policy or LocalRoutingPolicyStore(home=self.usage.home)

    def status(self) -> dict[str, Any]:
        profile_status = self.profiles.public_status()
        policy = self.policy.get()
        dashboard = self.usage.dashboard()
        return {
            "schema": "coordharness.provider-management.v1",
            "catalog": [dict(row) for row in CATALOG],
            "profiles": profile_status,
            "routing_policy": policy,
            "recommendation": recommend_local_dashboard(
                usage=dashboard, catalog=CATALOG, profiles=profile_status, policy=policy
            ),
        }

    def dispatch(self, request: object) -> tuple[int, dict[str, Any]]:
        if not isinstance(request, Mapping):
            return 400, self._error("invalid_provider_request")
        action = request.get("action")
        try:
            if action == "account_add" and set(request) == {
                "action",
                "provider_id",
                "label",
                "auth_mode",
                "endpoint",
            }:
                if request.get("endpoint") not in {None, ""}:
                    raise ValueError("custom endpoint unsupported")
                expected_mode = "cli" if request.get("provider_id") == "claude" else "oauth"
                if request.get("auth_mode") != expected_mode:
                    raise ValueError("auth mode unsupported")
                self.profiles.add(request["provider_id"], request["label"])
            elif action == "account_select" and set(request) == {
                "action",
                "provider_id",
                "profile_id",
            }:
                self.profiles.select(request["provider_id"], request["profile_id"])
            elif action == "account_remove" and set(request) == {
                "action",
                "provider_id",
                "profile_id",
            }:
                self.profiles.remove(request["provider_id"], request["profile_id"])
            elif action == "routing_policy_update" and set(request) == {"action", "policy"}:
                self.policy.update(request["policy"])
            elif action in {
                "credential_set",
                "credential_clear",
                "provider_add",
                "provider_remove",
                "provider_configure",
                "account_configure",
            }:
                return 501, self._error("unsupported_local_action")
            else:
                raise ValueError("invalid action")
        except (KeyError, OSError, TypeError, ValueError):
            return 400, self._error("invalid_provider_request")
        document = self.status()
        document["ok"] = True
        return 200, document

    def _error(self, code: str) -> dict[str, Any]:
        document = self.status()
        document.update(ok=False, error=code)
        return document
