"""Standalone current-user Claude and Codex account, quota, and history view."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import time
from typing import Any

from .local_history import LocalHistoryImport, discover_local_cli_history


JsonlRunner = Callable[[Sequence[str], Sequence[Mapping[str, Any]], float], list[Mapping[str, Any]]]
AccountProbe = Callable[[], "ProviderProbe"]
_SENSITIVE_MODEL_LABEL = re.compile(
    r"(?:bearer|password|credential|cookie|keychain|api[ _-]?key|token=|secret|private)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderProbe:
    account: Mapping[str, Any]
    windows: tuple[Mapping[str, Any], ...] = ()
    observed_at: str | None = None
    account_source: str = "official_cli_status"
    quota_source: str | None = None
    errors: tuple[str, ...] = ()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_model_label(value: str) -> str:
    allowed = " ._:+()'%,;!?&$#=-"
    cleaned = "".join(
        character if character.isascii() and (character.isalnum() or character in allowed) else " "
        for character in value
    )
    cleaned = " ".join(cleaned.split())[:80]
    if not cleaned or _SENSITIVE_MODEL_LABEL.search(cleaned):
        return "Unknown model"
    return cleaned


def _safe_plan(value: object) -> str:
    plan = str(value or "unknown").strip().lower()
    return (
        plan
        if plan in {"free", "go", "plus", "pro", "max", "team", "business", "enterprise", "api"}
        else "unknown"
    )


def _clean_env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


# The vendor CLIs are run from a directory outside the caller's tree, so a
# project-local config file cannot steer what they report. `/private/tmp` is the
# macOS spelling of that directory, and these probes are macOS-only. Where it
# does not exist, `subprocess` raises FileNotFoundError for the *working
# directory* -- which the broad handlers below read as "the CLI did not answer"
# and report as `unavailable`. A signed-in CLI then reads exactly like a
# signed-out one. Detecting the platform instead costs one stat and says so.
_PROBE_CWD = "/private/tmp"


def _probe_cwd() -> str | None:
    """The directory the vendor CLIs run in, or None where this platform has none."""
    return _PROBE_CWD if os.path.isdir(_PROBE_CWD) else None


def _unsupported_platform_probe(provider: str) -> ProviderProbe:
    """Say the platform is unsupported rather than answer as if we had asked."""
    return ProviderProbe(
        account={"status": "unsupported", "plan": "unknown", "authenticated": None},
        errors=(f"{provider}_probe_platform_unsupported", f"{provider}_quota_unavailable"),
    )


def probe_claude_account(home: Path | str, *, timeout_seconds: float = 3.0) -> ProviderProbe:
    """Read the official Claude CLI's bounded JSON auth status."""

    home_path = Path(home)
    executable = shutil.which("claude", path=_clean_env(home_path)["PATH"])
    if not executable:
        return ProviderProbe(
            account={"status": "unavailable", "plan": "unknown", "authenticated": None},
            errors=("claude_cli_unavailable", "claude_quota_unavailable"),
        )
    sandbox = _probe_cwd()
    if sandbox is None:
        return _unsupported_platform_probe("claude")
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=sandbox,
            env=_clean_env(home_path),
            timeout=max(0.2, min(float(timeout_seconds), 5.0)),
            check=False,
        )
        if len(result.stdout) > 32 * 1024:
            raise ValueError("auth output too large")
        raw = json.loads(
            result.stdout, parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("constant"))
        )
        if not isinstance(raw, Mapping):
            raise ValueError("invalid auth status")
        logged_in = raw.get("loggedIn")
        if type(logged_in) is not bool:
            raise ValueError("ambiguous auth status")
        plan = (
            _safe_plan(raw.get("subscriptionType") or raw.get("plan")) if logged_in else "unknown"
        )
        return ProviderProbe(
            account={
                "status": "active" if logged_in else "inactive",
                "plan": plan,
                "authenticated": logged_in,
            },
            errors=("claude_quota_unavailable",),
        )
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        return ProviderProbe(
            account={"status": "unavailable", "plan": "unknown", "authenticated": None},
            errors=("claude_auth_status_unavailable", "claude_quota_unavailable"),
        )


def _default_jsonl_runner(
    command: Sequence[str],
    requests: Sequence[Mapping[str, Any]],
    timeout: float,
    *,
    env: Mapping[str, str] | None = None,
) -> list[Mapping[str, Any]]:
    sandbox = _probe_cwd()
    if sandbox is None:
        raise NotADirectoryError(
            f"{_PROBE_CWD} does not exist: the local usage probes run the vendor CLIs "
            "from that directory and are supported on macOS only"
        )
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env) if env is not None else None,
        cwd=sandbox,
        text=False,
        bufsize=0,
    )
    responses: list[Mapping[str, Any]] = []
    buffer = bytearray()
    total = frames = 0
    deadline = time.monotonic() + timeout
    try:
        assert process.stdin is not None and process.stdout is not None

        def send(value: Mapping[str, Any]) -> None:
            payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
            process.stdin.write(payload)
            process.stdin.flush()

        def read(ids: set[int], *, require_all: bool) -> None:
            nonlocal buffer, total, frames
            while ids and time.monotonic() < deadline:
                ready, _, _ = select.select(
                    [process.stdout], [], [], max(0.0, deadline - time.monotonic())
                )
                if not ready:
                    break
                chunk = os.read(process.stdout.fileno(), min(65_536, 1_048_577 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > 1_048_576:
                    raise ValueError("app server output too large")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    frame, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    frames += 1
                    if frames > 64 or len(frame) > 256 * 1024:
                        raise ValueError("app server frame too large")
                    try:
                        value = json.loads(
                            frame,
                            parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("constant")),
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                        continue
                    response_id = value.get("id") if isinstance(value, Mapping) else None
                    if (
                        isinstance(response_id, int)
                        and not isinstance(response_id, bool)
                        and response_id in ids
                    ):
                        responses.append(value)
                        ids.remove(response_id)
            if require_all and ids:
                raise TimeoutError("app server handshake timeout")

        send(requests[0])
        read({1}, require_all=True)
        send(requests[1])
        for request in requests[2:]:
            send(request)
        read({2, 3}, require_all=False)
        return responses
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)


def _first(value: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if name in value:
            return value[name]
    return None


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value) / 1000 if value > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(number, tz=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            )
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _windows(raw: object, now: datetime) -> tuple[Mapping[str, Any], ...]:
    candidates: list[tuple[str, Mapping[str, Any]]] = []

    def visit(value: object, name: str = "bucket", depth: int = 0) -> None:
        if depth > 8 or len(candidates) >= 32:
            return
        if isinstance(value, list):
            for child in value[:64]:
                visit(child, name, depth + 1)
            return
        if not isinstance(value, Mapping):
            return
        used = _first(value, "used_percent", "usedPercent", "percent_used", "percentUsed")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            candidates.append((name, value))
            return
        for key, child in list(value.items())[:128]:
            if isinstance(child, (Mapping, list)):
                visit(child, key if key in {"primary", "secondary", "weekly"} else name, depth + 1)

    visit(raw)
    result = []
    for fallback, value in candidates:
        used_raw = _first(value, "used_percent", "usedPercent", "percent_used", "percentUsed")
        if not isinstance(used_raw, (int, float)) or isinstance(used_raw, bool):
            continue
        used = round(max(0.0, min(100.0, float(used_raw))), 4)
        minutes_raw = _first(value, "window_minutes", "windowMinutes", "windowDurationMins")
        minutes = (
            int(minutes_raw) if isinstance(minutes_raw, (int, float)) and minutes_raw > 0 else None
        )
        name_raw = str(_first(value, "name", "kind", "window") or fallback).lower()
        kind = (
            "weekly"
            if "week" in name_raw or minutes == 10080
            else "session"
            if "session" in name_raw or minutes == 300
            else "bucket"
        )
        reset = _timestamp(_first(value, "resets_at", "resetsAt", "reset_at", "resetAt"))
        result.append(
            {
                "kind": kind,
                "name": kind if kind in {"session", "weekly"} else "bucket",
                "window_minutes": minutes,
                "used_percent": used,
                "remaining_percent": round(100 - used, 4),
                "resets_at": _utc_iso(reset) if reset else None,
                "countdown_seconds": max(0, int((reset - now).total_seconds())) if reset else None,
            }
        )
    unique = {}
    for item in result:
        unique.setdefault(
            (item["kind"], item["window_minutes"], item["used_percent"], item["resets_at"]), item
        )
    return tuple(unique.values())


def probe_codex_account(
    home: Path | str,
    *,
    timeout_seconds: float = 2.0,
    runner: JsonlRunner = _default_jsonl_runner,
    now: datetime | None = None,
) -> ProviderProbe:
    home_path = Path(home)
    executable = shutil.which("codex", path=_clean_env(home_path)["PATH"])
    if not executable and runner is _default_jsonl_runner:
        return ProviderProbe(
            account={"status": "unavailable", "plan": "unknown", "authenticated": None},
            errors=("codex_cli_unavailable", "codex_quota_unavailable"),
        )
    if runner is _default_jsonl_runner and _probe_cwd() is None:
        return _unsupported_platform_probe("codex")
    command = [executable or "codex", "app-server"]
    requests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "coord-local-usage", "version": "1"},
                "capabilities": {},
            },
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/read", "params": {"refreshToken": False}},
        {"id": 3, "method": "account/rateLimits/read", "params": {}},
    ]
    try:
        timeout = max(0.2, min(float(timeout_seconds), 5.0))
        if runner is _default_jsonl_runner:
            responses = _default_jsonl_runner(command, requests, timeout, env=_clean_env(home_path))
        else:
            responses = runner(command, requests, timeout)
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError):
        return ProviderProbe(
            account={"status": "unavailable", "plan": "unknown", "authenticated": None},
            errors=("codex_app_server_unavailable", "codex_quota_unavailable"),
        )
    results: dict[int, Mapping[str, Any]] = {}
    for response in responses:
        response_id, result = response.get("id"), response.get("result")
        if response_id in {2, 3} and isinstance(result, Mapping) and "error" not in response:
            results[int(response_id)] = result
    account_raw = results.get(2, {})
    nested = account_raw.get("account") if isinstance(account_raw, Mapping) else None
    account = nested if isinstance(nested, Mapping) else account_raw
    account_type = str(account.get("type") or account.get("accountType") or "").lower()
    authenticated = True if account_type in {"chatgpt", "api"} else False if not account else None
    public_account = {
        "status": "active"
        if authenticated is True
        else "inactive"
        if authenticated is False
        else "unavailable",
        "plan": _safe_plan(account.get("planType") or account.get("plan")),
        "authenticated": authenticated,
    }
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windows = _windows(results.get(3, {}), observed)
    errors = []
    if 2 not in results:
        errors.append("codex_account_unavailable")
    if not windows:
        errors.append("codex_quota_unavailable")
    return ProviderProbe(
        account=public_account,
        windows=windows,
        observed_at=_utc_iso(observed),
        account_source="codex_app_server",
        quota_source="codex_app_server" if windows else None,
        errors=tuple(errors),
    )


def _history(imported: LocalHistoryImport, now: datetime) -> dict[str, Any]:
    by_day: dict[str, dict[str, int]] = {}
    models_by_day: dict[str, dict[str, dict[str, int]]] = {}
    for row in imported.rows:
        bucket = by_day.setdefault(
            row.usage_date,
            {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_create_other_tokens": 0,
            },
        )
        bucket["input_tokens"] += row.input_tokens
        bucket["output_tokens"] += row.output_tokens
        bucket["cache_read_tokens"] += row.cache_read_tokens
        bucket["cache_create_other_tokens"] += (
            row.cache_create_other_tokens + row.cache_create_5m_tokens + row.cache_create_1h_tokens
        )
        bucket["total_tokens"] += sum(
            (
                row.input_tokens,
                row.output_tokens,
                row.cache_read_tokens,
                row.cache_create_other_tokens,
                row.cache_create_5m_tokens,
                row.cache_create_1h_tokens,
            )
        )
        model_bucket = models_by_day.setdefault(row.usage_date, {}).setdefault(
            row.model,
            {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_create_5m_tokens": 0,
                "cache_create_1h_tokens": 0,
                "cache_create_other_tokens": 0,
                "provider_native_cost_nanos": 0,
                "api_rate_estimate_nanos": 0,
            },
        )
        model_bucket["input_tokens"] += row.input_tokens
        model_bucket["output_tokens"] += row.output_tokens
        model_bucket["cache_read_tokens"] += row.cache_read_tokens
        model_bucket["cache_create_5m_tokens"] += row.cache_create_5m_tokens
        model_bucket["cache_create_1h_tokens"] += row.cache_create_1h_tokens
        model_bucket["cache_create_other_tokens"] += row.cache_create_other_tokens
        model_bucket["provider_native_cost_nanos"] += row.provider_native_cost_nanos or 0
        model_bucket["api_rate_estimate_nanos"] += row.api_rate_estimate_nanos or 0
        model_bucket["total_tokens"] += sum(
            (
                row.input_tokens,
                row.output_tokens,
                row.cache_read_tokens,
                row.cache_create_other_tokens,
                row.cache_create_5m_tokens,
                row.cache_create_1h_tokens,
            )
        )

    daily = []
    for day, values in sorted(by_day.items()):
        model_rows = []
        for model, metrics in sorted(
            models_by_day.get(day, {}).items(),
            key=lambda item: (-item[1]["total_tokens"], item[0].casefold()),
        )[:50]:
            item: dict[str, Any] = {
                "key": f"model-{hashlib.sha256(model.encode('utf-8')).hexdigest()[:16]}",
                "label": _public_model_label(model),
                **metrics,
            }
            if item["provider_native_cost_nanos"] == 0:
                item["provider_native_cost_nanos"] = None
            if item["api_rate_estimate_nanos"] == 0:
                item["api_rate_estimate_nanos"] = None
            model_rows.append(item)
        daily.append({"date": day, **values, "model_breakdowns": model_rows})
    today = now.date()
    week = today - timedelta(days=today.weekday())
    seven = today - timedelta(days=6)

    def total(start):
        return sum(
            row["total_tokens"]
            for row in daily
            if datetime.fromisoformat(row["date"]).date() >= start
        )

    return {
        "daily": daily[-400:],
        "today_total_tokens": by_day.get(today.isoformat(), {}).get("total_tokens", 0)
        if daily
        else None,
        "rolling_7d_total_tokens": total(seven) if daily else None,
        "calendar_week_total_tokens": total(week) if daily else None,
        "all_time_total_tokens": sum(row["total_tokens"] for row in daily) if daily else None,
        "semantics": "local_cli_history_partial",
    }


class _UncachedLocalUsageService:
    """Build the public dashboard from only this user's official CLI state."""

    def __init__(
        self,
        *,
        home: Path | str | None = None,
        now: Callable[[], datetime] | None = None,
        claude_probe: AccountProbe | None = None,
        codex_probe: AccountProbe | None = None,
        history_loader: Callable[..., LocalHistoryImport] | None = None,
    ) -> None:
        self.home = Path(home) if home is not None else Path.home()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._claude_probe = claude_probe or (lambda: probe_claude_account(self.home))
        self._codex_probe = codex_probe or (lambda: probe_codex_account(self.home))
        self._history_loader = history_loader or discover_local_cli_history

    def _probe_account_status(self) -> dict[str, ProviderProbe]:
        return {"claude": self._claude_probe(), "codex": self._codex_probe()}

    def dashboard(self) -> dict[str, Any]:
        observed = self._now().astimezone(timezone.utc)
        local = observed.astimezone()
        probes = self._probe_account_status()
        providers: dict[str, Any] = {}
        all_errors: list[dict[str, str]] = []
        for provider in ("claude", "codex"):
            root = self.home / (".claude" if provider == "claude" else ".codex")
            imported = self._history_loader(root, provider=provider)
            probe = probes[provider]
            errors = list(probe.errors)
            if imported.parse_error_count:
                errors.append(f"{provider}_history_partial")
            windows = list(probe.windows)
            source_warning = "Local CLI history can be incomplete or compacted"
            provider_doc: dict[str, Any] = {
                "source": {
                    "kind": "local_cli_history",
                    "canonical": False,
                    "label": "Local CLI history",
                    "warning": source_warning,
                },
                "account_source": {
                    "kind": probe.account_source,
                    "canonical": probe.account.get("authenticated") is not None,
                    "label": "Official CLI account status",
                },
                "account": dict(probe.account),
                "windows": windows,
                "reset_credits": [],
                "runout": {
                    "kind": "current_window",
                    "advisory": True,
                    "estimated_exhausts_at": None,
                    "seconds_to_exhaustion": None,
                    "basis": "Current provider quota" if windows else "Current quota unavailable",
                },
                "history": _history(imported, local),
                "costs": {
                    "provider_billed": {
                        "amount_nanos": None,
                        "currency": None,
                        "semantics": "unknown",
                    },
                    "provider_native": {"amount_nanos": None, "semantics": "unknown"},
                    "api_rate_estimate": {"amount_nanos": None, "semantics": "unknown"},
                },
                "active_sessions": {"status": "unavailable", "count": None, "providers": []},
                "live_observation_state": "fresh" if windows else "quota_observation_unavailable",
                "errors": [{"code": code} for code in errors],
            }
            if windows:
                provider_doc["quota_source"] = {
                    "kind": probe.quota_source or "official_cli_quota",
                    "canonical": True,
                    "label": "Current provider quota",
                }
                provider_doc["live_observed_at"] = probe.observed_at or _utc_iso(observed)
                provider_doc["quota_groups"] = [
                    {
                        "key": "account",
                        "label": "Account quota",
                        "semantics": "provider_quota_meter",
                        "windows": windows,
                        "runout": provider_doc["runout"],
                    }
                ]
            else:
                provider_doc["quota_source"] = {
                    "kind": "local_quota_unavailable",
                    "canonical": False,
                    "label": "Current quota unavailable",
                    "warning": "No supported local provider quota source returned data",
                }
            providers[provider] = provider_doc
            all_errors.extend({"code": code} for code in errors)
        generated = _utc_iso(observed)
        return {
            "schema": "coordharness.usage-intelligence.v1",
            "generated_at": generated,
            "stale_after": _utc_iso(observed + timedelta(seconds=30)),
            "refresh": {"state": "fresh", "generated_at": generated},
            "calendar": {
                "time_zone": str(getattr(local.tzinfo, "key", None) or local.tzname() or "UTC"),
                "local_date": local.date().isoformat(),
                "week_start_date": (
                    local.date() - timedelta(days=local.date().weekday())
                ).isoformat(),
                "week_starts_on": "monday",
                "semantics": "system_local_calendar",
            },
            "providers": providers,
            "errors": all_errors[:64],
        }


# Imported last so the cache wrapper can subclass the completed uncached builder.
from .local_cache import LocalUsageService as LocalUsageService  # noqa: E402
