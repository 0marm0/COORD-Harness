"""Local, secret-free account profile and routing-policy persistence.

Only bounded display metadata is stored on disk. Provider credentials remain
owned by the official CLIs; this module intentionally has no secret setter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Mapping


PROVIDERS = ("claude", "codex")
_PROFILE_ID = re.compile(r"^p_[0-9a-f]{12}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_MAX_PROFILES = 12


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


class LocalProfileRegistry:
    """Persist profile labels and selection, never credentials or CLI tokens."""

    def __init__(self, path: Path | str | None = None, *, home: Path | str | None = None) -> None:
        base = Path(home) if home is not None else Path.home()
        configured = os.environ.get("COORD_PROVIDER_PROFILES_PATH")
        self.path = Path(path or configured or base / ".coord" / "provider-profiles.json")
        self._lock = threading.RLock()

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "version": 1,
            "active": {provider: "default" for provider in PROVIDERS},
            "profiles": {provider: [] for provider in PROVIDERS},
        }

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            return self._default()
        if not isinstance(raw, Mapping) or raw.get("version") != 1:
            return self._default()
        result = self._default()
        active_source = raw.get("active") if isinstance(raw.get("active"), Mapping) else {}
        rows_source = raw.get("profiles") if isinstance(raw.get("profiles"), Mapping) else {}
        for provider in PROVIDERS:
            rows: list[dict[str, str]] = []
            source = rows_source.get(provider)
            if isinstance(source, list):
                for row in source[:_MAX_PROFILES]:
                    if not isinstance(row, Mapping) or set(row) != {"id", "label"}:
                        continue
                    profile_id, label = row.get("id"), row.get("label")
                    if (
                        isinstance(profile_id, str)
                        and _PROFILE_ID.fullmatch(profile_id)
                        and isinstance(label, str)
                        and _LABEL.fullmatch(label)
                    ):
                        rows.append({"id": profile_id, "label": label})
            result["profiles"][provider] = rows
            known = {row["id"] for row in rows}
            active = active_source.get(provider)
            result["active"][provider] = active if active in known else "default"
        return result

    def _save(self, value: Mapping[str, Any]) -> None:
        _atomic_private_json(self.path, value)

    @staticmethod
    def _provider(value: object) -> str:
        provider = str(value or "").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError("invalid_provider")
        return provider

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            value = self._load()
        result: dict[str, Any] = {}
        for provider in PROVIDERS:
            active = value["active"][provider]
            default_mode = "cli" if provider == "claude" else "oauth"
            rows = [
                {
                    "id": "default",
                    "label": "Default system account",
                    "active": active == "default",
                    "isolated": False,
                    "auth_mode": default_mode,
                    "enabled": True,
                    "priority": 50,
                    "credential_set": False,
                }
            ]
            rows.extend(
                {
                    "id": row["id"],
                    "label": row["label"],
                    "active": active == row["id"],
                    "isolated": True,
                    "auth_mode": default_mode,
                    "enabled": True,
                    "priority": 50,
                    "credential_set": False,
                }
                for row in value["profiles"][provider]
            )
            result[provider] = {"active": active, "profiles": rows}
        return result

    def add(self, provider: object, label: object) -> str:
        provider_id = self._provider(provider)
        normalized = str(label or "").strip()
        if not _LABEL.fullmatch(normalized):
            raise ValueError("invalid_profile_label")
        with self._lock:
            value = self._load()
            if len(value["profiles"][provider_id]) >= _MAX_PROFILES:
                raise ValueError("profile_limit")
            profile_id = f"p_{secrets.token_hex(6)}"
            value["profiles"][provider_id].append({"id": profile_id, "label": normalized})
            value["active"][provider_id] = profile_id
            self._save(value)
        return profile_id

    def select(self, provider: object, profile_id: object) -> None:
        provider_id = self._provider(provider)
        selected = str(profile_id or "")
        with self._lock:
            value = self._load()
            known = {row["id"] for row in value["profiles"][provider_id]}
            if selected != "default" and selected not in known:
                raise ValueError("unknown_profile")
            value["active"][provider_id] = selected
            self._save(value)

    def remove(self, provider: object, profile_id: object) -> None:
        provider_id = self._provider(provider)
        removed = str(profile_id or "")
        if not _PROFILE_ID.fullmatch(removed):
            raise ValueError("invalid_profile")
        with self._lock:
            value = self._load()
            kept = [row for row in value["profiles"][provider_id] if row["id"] != removed]
            if len(kept) == len(value["profiles"][provider_id]):
                raise ValueError("unknown_profile")
            value["profiles"][provider_id] = kept
            if value["active"][provider_id] == removed:
                value["active"][provider_id] = "default"
            self._save(value)


class LocalRoutingPolicyStore:
    """Persist a bounded policy with advisory mode as the safe default."""

    def __init__(self, path: Path | str | None = None, *, home: Path | str | None = None) -> None:
        base = Path(home) if home is not None else Path.home()
        configured = os.environ.get("COORD_PROVIDER_ROUTING_PATH")
        self.path = Path(path or configured or base / ".coord" / "provider-routing.json")
        self._lock = threading.RLock()

    @staticmethod
    def defaults() -> dict[str, Any]:
        return {
            "version": 1,
            "mode": "advisory",
            "min_session_remaining": 15,
            "min_weekly_remaining": 20,
            "min_runway_minutes": 60,
            "allow_metered_api": False,
            "prefer_subscription": True,
            "prefer_local": False,
            "required_capabilities": ["code", "tools"],
        }

    @staticmethod
    def validate(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("invalid_policy")
        expected = set(LocalRoutingPolicyStore.defaults())
        if set(value) != expected or value.get("version") != 1:
            raise ValueError("invalid_policy")
        if value.get("mode") not in {"advisory", "automatic"}:
            raise ValueError("invalid_policy")
        for key, maximum in (
            ("min_session_remaining", 100),
            ("min_weekly_remaining", 100),
            ("min_runway_minutes", 10080),
        ):
            item = value.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= maximum:
                raise ValueError("invalid_policy")
        for key in ("allow_metered_api", "prefer_subscription", "prefer_local"):
            if type(value.get(key)) is not bool:
                raise ValueError("invalid_policy")
        capabilities = value.get("required_capabilities")
        allowed = {"chat", "code", "reasoning", "vision", "tools"}
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > 8
            or any(item not in allowed for item in capabilities)
        ):
            raise ValueError("invalid_policy")
        return {**value, "required_capabilities": list(dict.fromkeys(capabilities))}

    def get(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                return self.defaults()
        try:
            return self.validate(raw)
        except ValueError:
            return self.defaults()

    def update(self, value: object) -> dict[str, Any]:
        policy = self.validate(value)
        with self._lock:
            self._save(policy)
        return policy

    def _save(self, value: Mapping[str, Any]) -> None:
        _atomic_private_json(self.path, value)
