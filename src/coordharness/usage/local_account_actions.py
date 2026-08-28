"""Honest local account actions: metadata mutations work; login is unsupported."""

from __future__ import annotations

from typing import Any, Mapping

from .local_profiles import LocalProfileRegistry
from .local_service import LocalUsageService


class LocalAccountActionService:
    def __init__(
        self,
        *,
        usage: LocalUsageService | None = None,
        profiles: LocalProfileRegistry | None = None,
    ) -> None:
        self.usage = usage or LocalUsageService()
        self.profiles = profiles or LocalProfileRegistry(home=self.usage.home)

    def status(self) -> dict[str, Any]:
        probes = self.usage.account_status()
        claude_auth = probes["claude"].account.get("authenticated")
        return {
            "schema": "coordharness.usage-account-actions.v1",
            "codex": {"state": "idle", "can_start": False, "can_cancel": False},
            "claude": {
                "state": "connected"
                if claude_auth is True
                else "sign_in_required"
                if claude_auth is False
                else "unavailable",
                "connect_available": False,
            },
            "profiles": self.profiles.public_status(),
        }

    def dispatch(self, request: object) -> tuple[int, dict[str, Any]]:
        if not isinstance(request, Mapping):
            return self._invalid()
        action = request.get("action")
        if action in {"codex_login_start", "claude_connect_open", "claude_recovery_open"}:
            document = self.status()
            document.update(ok=False, result="unsupported_local_action")
            return 501, document
        if action == "codex_login_cancel" and set(request) == {"action"}:
            document = self.status()
            document.update(ok=False, result="no_active_login")
            return 409, document
        try:
            if action == "profile_add" and set(request) == {"action", "provider", "label"}:
                self.profiles.add(request["provider"], request["label"])
            elif action == "profile_select" and set(request) == {
                "action",
                "provider",
                "profile_id",
            }:
                self.profiles.select(request["provider"], request["profile_id"])
            elif action == "profile_remove" and set(request) == {
                "action",
                "provider",
                "profile_id",
            }:
                self.profiles.remove(request["provider"], request["profile_id"])
            else:
                return self._invalid()
        except (KeyError, OSError, TypeError, ValueError):
            return self._invalid()
        document = self.status()
        document.update(ok=True, result=action)
        return 200, document

    def _invalid(self) -> tuple[int, dict[str, Any]]:
        document = self.status()
        document["ok"] = False
        return 400, document
