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

from .local_cost_cache import LocalCostCacheImport, read_local_cost_cache
from .local_history import LocalHistoryImport, discover_local_cli_history
from .pricing import RateCard, RateCardError
from .provider_anchor import (
    REASON_SERIES_INVALID,
    REASON_SERIES_UNAVAILABLE,
    AnchorPlan,
    apply_anchor_plan,
    not_applicable_plan,
    plan_codex_anchor,
    unavailable_plan,
)
from .provider_series import (
    CodexUsageSeriesSource,
    ProviderSeriesUnavailable,
    SeriesObservation,
    default_cache_path,
)
from .rate_card_refresh import default_override_path, load_effective_rate_card
from .scan_refresh import DEFAULT_STALE_AFTER_SECONDS, StoreFreshness, assess_store
from .scan_store import read_store_history


JsonlRunner = Callable[[Sequence[str], Sequence[Mapping[str, Any]], float], list[Mapping[str, Any]]]
AccountProbe = Callable[[], "ProviderProbe"]
CostCacheLoader = Callable[..., LocalCostCacheImport]
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
    if (
        not cleaned
        or cleaned.casefold() in {"unknown", "unknown model"}
        or _SENSITIVE_MODEL_LABEL.search(cleaned)
    ):
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
        # The ids actually asked for, so a caller that sends one request is not
        # held to the full timeout waiting on an id it never sent.
        read(
            {
                int(request["id"])
                for request in requests[2:]
                if isinstance(request.get("id"), int) and not isinstance(request["id"], bool)
            },
            require_all=False,
        )
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


def _quota_pace(
    used_percent: float,
    window_minutes: int | None,
    resets_at: datetime | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Return a bounded, advisory pace projection from a live quota window."""

    if window_minutes is None or window_minutes <= 0 or resets_at is None or resets_at <= now:
        return None
    window_seconds = float(window_minutes) * 60
    elapsed = window_seconds - (resets_at - now).total_seconds()
    if elapsed < 0 or elapsed > window_seconds:
        return None
    expected = max(0.0, min(100.0, elapsed * 100 / window_seconds))
    signed_delta = used_percent - expected
    state = "deficit" if signed_delta > 2 else "reserve" if signed_delta < -2 else "on_pace"
    seconds_to_exhaustion = None
    will_last_to_reset = True
    if used_percent > 0 and elapsed > 0:
        projected = int(max(0.0, (100.0 - used_percent) / (used_percent / elapsed)))
        if projected <= int((resets_at - now).total_seconds()):
            seconds_to_exhaustion = projected
            will_last_to_reset = False
    return {
        "state": state,
        "delta_percent": round(abs(signed_delta), 4),
        "expected_used_percent": round(expected, 4),
        "will_last_to_reset": will_last_to_reset,
        "seconds_to_exhaustion": seconds_to_exhaustion,
        "advisory": True,
        "basis": "elapsed_window_linear_projection",
        "source": "local_projection",
        "marker_remaining_percent": round(100 - expected, 4),
        "marker_kind": state if state != "on_pace" else None,
    }


def _with_local_pace(windows: Sequence[Mapping[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Attach advisory pace only where a live window provides complete timing."""

    result: list[dict[str, Any]] = []
    for window in windows:
        item = dict(window)
        used = item.get("used_percent")
        minutes = item.get("window_minutes")
        reset = _timestamp(item.get("resets_at"))
        if (
            isinstance(used, (int, float))
            and not isinstance(used, bool)
            and isinstance(minutes, (int, float))
            and not isinstance(minutes, bool)
        ):
            pace = _quota_pace(max(0.0, min(100.0, float(used))), int(minutes), reset, now)
            if pace is not None:
                item["pace"] = pace
        result.append(item)
    return result


def _runout(windows: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Expose only a reset-bounded advisory for the current quota window."""

    current = next((window for window in windows if window.get("kind") == "session"), None)
    current = current or (windows[0] if windows else None)
    result: dict[str, Any] = {
        "kind": "current_window_linear",
        "advisory": True,
        "estimated_exhausts_at": None,
        "seconds_to_exhaustion": None,
        "basis": "insufficient_countdown_inputs",
    }
    if current is None:
        return result
    pace = current.get("pace")
    if not isinstance(pace, Mapping):
        return result
    if pace.get("will_last_to_reset") is True:
        return {**result, "basis": "would_cross_reset_boundary"}
    seconds = pace.get("seconds_to_exhaustion")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        return result
    return {
        **result,
        "estimated_exhausts_at": _utc_iso(now + timedelta(seconds=seconds)),
        "seconds_to_exhaustion": seconds,
        "basis": "linear_used_percent_since_window_start",
    }


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
        item: dict[str, Any] = {
            "kind": kind,
            "name": kind if kind in {"session", "weekly"} else "bucket",
            "window_minutes": minutes,
            "used_percent": used,
            "remaining_percent": round(100 - used, 4),
            "resets_at": _utc_iso(reset) if reset else None,
            "countdown_seconds": max(0, int((reset - now).total_seconds())) if reset else None,
        }
        pace = _quota_pace(used, minutes, reset, now)
        if pace is not None:
            item["pace"] = pace
        result.append(item)
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


def fetch_codex_account_usage(
    home: Path | str,
    *,
    timeout_seconds: float = 8.0,
    runner: JsonlRunner = _default_jsonl_runner,
) -> Mapping[str, Any]:
    """Ask the official Codex app-server for the account's daily token series.

    The same executable, sandbox directory, clean environment and bounded JSONL
    runner as ``probe_codex_account``, with one request: ``account/usage/read``.
    It is separate from the quota probe on purpose -- the probe runs on every
    dashboard refresh under a two-second budget, while this series changes
    slowly and is fetched in the background (``provider_series``).

    Raises ``ProviderSeriesUnavailable`` with a machine-readable code.
    """

    home_path = Path(home)
    executable = shutil.which("codex", path=_clean_env(home_path)["PATH"])
    if not executable and runner is _default_jsonl_runner:
        raise ProviderSeriesUnavailable("codex_cli_unavailable")
    if runner is _default_jsonl_runner and _probe_cwd() is None:
        raise ProviderSeriesUnavailable("codex_probe_platform_unsupported")
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
        {"id": 2, "method": "account/usage/read", "params": {}},
    ]
    timeout = max(0.5, min(float(timeout_seconds), 20.0))
    try:
        if runner is _default_jsonl_runner:
            responses = _default_jsonl_runner(command, requests, timeout, env=_clean_env(home_path))
        else:
            responses = runner(command, requests, timeout)
    except TimeoutError as error:
        raise ProviderSeriesUnavailable("codex_app_server_timeout") from error
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ProviderSeriesUnavailable("codex_app_server_unavailable") from error
    for response in responses:
        if response.get("id") != 2:
            continue
        if "error" in response:
            raise ProviderSeriesUnavailable("codex_account_usage_rpc_error")
        result = response.get("result")
        if isinstance(result, Mapping):
            return result
    raise ProviderSeriesUnavailable("codex_account_usage_timeout")


def _cost_component(
    imported: LocalHistoryImport,
    card: RateCard | None,
    cost_import: LocalCostCacheImport,
) -> dict[str, Any]:
    """Price the whole import from the rate card, falling back to the cache.

    The rate card wins whenever it is loadable, so the figure tracks the local
    transcripts rather than a third-party cache's freshness. The cache is only
    consulted when the card itself is unavailable.
    """

    if card is None:
        return cost_import.cost_component()
    total = 0
    priced_tokens = unpriced_tokens = 0
    for row in imported.rows:
        priced = card.price(
            row.model,
            {
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cache_read_tokens": row.cache_read_tokens,
                "cache_create_5m_tokens": row.cache_create_5m_tokens,
                "cache_create_1h_tokens": row.cache_create_1h_tokens,
                "cache_create_other_tokens": row.cache_create_other_tokens,
            },
        )
        if priced.priced:
            total += priced.amount_nanos or 0
            priced_tokens += priced.priced_tokens
        else:
            unpriced_tokens += priced.unpriced_tokens
    if priced_tokens == 0 and unpriced_tokens == 0:
        return {"amount_nanos": None, "semantics": "unknown"}
    if priced_tokens == 0:
        # No model on record carries a published rate. Reporting 0 here would
        # read as a free period; the truth is that the amount is unknown.
        return {
            "amount_nanos": None,
            "semantics": "all_local_tokens_unpriceable",
            "unpriced_tokens": unpriced_tokens,
            "priced_tokens": 0,
            "pricing_key": card.pricing_key,
        }
    return {
        "amount_nanos": total,
        "currency": card.currency,
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_tokens,
        "coverage_state": imported.coverage_state,
        "pricing_key": card.pricing_key,
        "rate_card_version": card.rate_card_version,
        "rate_card_digest": card.digest,
        # How many models the user-local override card re-prices or adds over
        # the vendored card; 0 means the vendored card is in force unchanged.
        "rate_card_override_models": int(card.source.get("override_models", 0) or 0),
        "semantics": "self_computed_api_list_price_estimate",
        "source": {
            "kind": "harness_rate_card",
            "canonical": False,
            "label": "API list-price estimate",
            "warning": (
                "Priced locally from this machine's CLI transcripts at API list "
                "rates; not provider-billed spend."
            ),
        },
    }


def _unpriced_model_reasons(imported: LocalHistoryImport, card: RateCard) -> dict[str, str]:
    """Every measured model the card cannot price, with the card's reason."""

    reasons: dict[str, str] = {}
    for model in {row.model for row in imported.rows}:
        resolved, reason = card.resolve(model)
        if resolved is None and reason is not None:
            reasons[model] = reason
    return reasons


def _legacy_coverage(
    imported: LocalHistoryImport, daily: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Describe the imported stretch, or say nothing when there is none.

    ``None`` rather than a zeroed block, so a dashboard with no legacy import
    renders exactly as it did before one existed.
    """

    if not imported.legacy_rows:
        return None
    legacy_days = [row for row in daily if row.get("provenance") == "legacy_import"]
    provenance = dict(imported.legacy_provenance or {})
    return {
        **provenance,
        "days": len(legacy_days),
        "total_tokens": sum(row["total_tokens"] for row in legacy_days),
        "api_rate_estimate_nanos": sum(
            row["api_rate_estimate_nanos"] or 0 for row in legacy_days
        ),
        "self_computed_days": len(daily) - len(legacy_days),
        "semantics": provenance.get(
            "semantics", "third_party_list_price_estimate_known_to_overstate"
        ),
    }


def _history(
    imported: LocalHistoryImport,
    now: datetime,
    api_costs: Mapping[tuple[str, str], int] | None = None,
    card: RateCard | None = None,
) -> dict[str, Any]:
    """Aggregate local rows by day, pricing each (day, model) from the rate card.

    ``api_costs`` remains accepted as an optional corroboration overlay, but the
    rate card is authoritative: a third-party cache that stops updating must not
    be able to present a busy day as a free one.
    """

    api_costs = api_costs or {}
    priceable_by_day: dict[str, int] = {}
    unpriced_by_day: dict[str, int] = {}
    unpriced_models: dict[str, str] = {}
    by_day: dict[str, dict[str, Any]] = {}
    models_by_day: dict[str, dict[str, dict[str, Any]]] = {}
    # Legacy days come from another tool's ledger and are never repriced here:
    # they carry their own cost, and the rate card has nothing to say about a
    # day this machine holds no transcripts for. They also never collide with
    # a self-computed day -- the store suppresses one that does -- so a day
    # bucket is wholly one kind or the other and can be labelled as such.
    #
    # WHY by row ORIGIN rather than by date: days are now derived per reader
    # zone from UTC slots, while a legacy row keeps the (UTC) day its source
    # recorded. West of UTC, the first measured hours can fall on a legacy
    # row's date, and pricing those measured rows as if they were legacy (no
    # card, no own cost) would drop them from the dollars.
    legacy_days = {row.usage_date for row in imported.legacy_rows}
    measured_days = {row.usage_date for row in imported.rows}
    tagged = tuple((row, False) for row in imported.rows) + tuple(
        (row, True) for row in imported.legacy_rows
    )
    for row, is_legacy in tagged:
        cost_key = (row.usage_date, row.model)
        metrics_for_pricing = {
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_create_5m_tokens": row.cache_create_5m_tokens,
            "cache_create_1h_tokens": row.cache_create_1h_tokens,
            "cache_create_other_tokens": row.cache_create_other_tokens,
        }
        priced = (
            card.price(row.model, metrics_for_pricing)
            if card is not None and not is_legacy
            else None
        )
        if is_legacy:
            cost_observed = row.api_rate_estimate_nanos is not None
            api_cost = row.api_rate_estimate_nanos or 0
        elif priced is not None and priced.priced:
            cost_observed = True
            api_cost = priced.amount_nanos or 0
            priceable_by_day[row.usage_date] = (
                priceable_by_day.get(row.usage_date, 0) + priced.priced_tokens
            )
        elif priced is not None:
            cost_observed = False
            api_cost = 0
            unpriced_by_day[row.usage_date] = (
                unpriced_by_day.get(row.usage_date, 0) + priced.unpriced_tokens
            )
            if priced.reason is not None:
                unpriced_models.setdefault(row.model, priced.reason)
        else:
            cost_observed = cost_key in api_costs or row.api_rate_estimate_nanos is not None
            api_cost = api_costs.get(cost_key, row.api_rate_estimate_nanos or 0)
        bucket = by_day.setdefault(
            row.usage_date,
            {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_create_other_tokens": 0,
                "api_rate_estimate_nanos": 0,
                "_api_cost_observed": False,
                # A day holding ANY imported row is labelled legacy, even when
                # measured hours share its date: the conservative label, so an
                # imported figure is never presented as one this machine made.
                "_legacy": row.usage_date in legacy_days,
            },
        )
        bucket["input_tokens"] += row.input_tokens
        bucket["output_tokens"] += row.output_tokens
        bucket["cache_read_tokens"] += row.cache_read_tokens
        bucket["cache_create_other_tokens"] += (
            row.cache_create_other_tokens + row.cache_create_5m_tokens + row.cache_create_1h_tokens
        )
        if cost_observed:
            bucket["api_rate_estimate_nanos"] += api_cost
            bucket["_api_cost_observed"] = True
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
                "_api_cost_observed": False,
            },
        )
        model_bucket["input_tokens"] += row.input_tokens
        model_bucket["output_tokens"] += row.output_tokens
        model_bucket["cache_read_tokens"] += row.cache_read_tokens
        model_bucket["cache_create_5m_tokens"] += row.cache_create_5m_tokens
        model_bucket["cache_create_1h_tokens"] += row.cache_create_1h_tokens
        model_bucket["cache_create_other_tokens"] += row.cache_create_other_tokens
        model_bucket["provider_native_cost_nanos"] += row.provider_native_cost_nanos or 0
        if cost_observed:
            model_bucket["api_rate_estimate_nanos"] += api_cost
            model_bucket["_api_cost_observed"] = True
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
        day_values = dict(values)
        api_cost_observed = bool(day_values.pop("_api_cost_observed"))
        day_is_legacy = bool(day_values.pop("_legacy"))
        if not api_cost_observed:
            day_values["api_rate_estimate_nanos"] = None
        # Every day says which accounting produced it, so a chart or hover can
        # mark the imported stretch instead of drawing one continuous series
        # that implies one method throughout.
        day_values["provenance"] = "legacy_import" if day_is_legacy else "self_computed"
        model_rows = []
        for model, metrics in sorted(
            models_by_day.get(day, {}).items(),
            key=lambda item: (-item[1]["total_tokens"], item[0].casefold()),
        )[:50]:
            model_metrics = dict(metrics)
            model_cost_observed = bool(model_metrics.pop("_api_cost_observed"))
            item: dict[str, Any] = {
                "key": f"model-{hashlib.sha256(model.encode('utf-8')).hexdigest()[:16]}",
                "label": _public_model_label(model),
                **model_metrics,
            }
            if item["provider_native_cost_nanos"] == 0:
                item["provider_native_cost_nanos"] = None
            if not model_cost_observed:
                item["api_rate_estimate_nanos"] = None
            model_rows.append(item)
        daily.append({"date": day, **day_values, "model_breakdowns": model_rows})
    today = now.date()
    week = today - timedelta(days=today.weekday())
    seven = today - timedelta(days=6)

    def total(start):
        return sum(
            row["total_tokens"]
            for row in daily
            if datetime.fromisoformat(row["date"]).date() >= start
        )

    today_key = today.isoformat()
    priced_today = priceable_by_day.get(today_key, 0)
    unpriced_today = unpriced_by_day.get(today_key, 0)
    observed_today = priced_today + unpriced_today
    del measured_days
    return {
        # Full history; the caller caps the served rows AFTER anchoring, so
        # every total is taken over every day rather than the served window.
        "daily": daily,
        "pricing_coverage": {
            "priceable_tokens_today": priced_today,
            "unpriced_tokens_today": unpriced_today,
            "priced_coverage_percent": (
                round(priced_today / observed_today * 100, 3) if observed_today else None
            ),
            "priceable_tokens_all_time": sum(priceable_by_day.values()),
            "unpriced_tokens_all_time": sum(unpriced_by_day.values()),
            "unpriced_models": [
                {"label": _public_model_label(model), "reason": reason}
                for model, reason in sorted(unpriced_models.items())
            ],
            "semantics": "rate_card_priced_vs_unpriceable_local_tokens",
        },
        "legacy_coverage": _legacy_coverage(imported, daily),
        "today_total_tokens": by_day.get(today_key, {}).get("total_tokens", 0) if daily else None,
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
        cost_cache_root: Path | str | None = None,
        cost_cache_loader: CostCacheLoader | None = None,
        rate_card_loader: Callable[[], RateCard] | None = None,
        scan_store_path: Path | str | None = None,
        codex_series: Callable[[], SeriesObservation] | None = None,
    ) -> None:
        # Background network work (the provider series fetch here, the rate
        # card refresh in the cached service) runs only for the real user: a
        # service pointed at a fixture home must never reach the network.
        self._serves_real_user = home is None
        self.home = Path(home) if home is not None else Path.home()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._claude_probe = claude_probe or (lambda: probe_claude_account(self.home))
        self._codex_probe = codex_probe or (lambda: probe_codex_account(self.home))
        # Derived from this service's home rather than from the real user's, so
        # a service pointed at a fixture home can never be served the operator's
        # own scan store.
        self._scan_store_path = (
            Path(scan_store_path)
            if scan_store_path is not None
            else self.home / ".coordharness" / "usage-scan.sqlite"
        )
        self._history_loader = history_loader or self._local_history
        self._cost_cache_root = (
            Path(cost_cache_root)
            if cost_cache_root is not None
            else self.home / "Library" / "Caches" / "CodexBar" / "cost-usage"
        )
        self._cost_cache_loader = cost_cache_loader or read_local_cost_cache
        # The EFFECTIVE card: the user-local override (``rate_card_refresh``)
        # layered over the vendored one. With no override file this is the
        # vendored card exactly -- same pricing_key, same digest.
        self._rate_card_override_path = default_override_path(self.home)
        self._rate_card_loader = rate_card_loader or (
            lambda: load_effective_rate_card(self._rate_card_override_path)
        )
        if codex_series is None:
            source = CodexUsageSeriesSource(
                fetch=(
                    (lambda: fetch_codex_account_usage(self.home))
                    if self._serves_real_user
                    else None
                ),
                cache_path=default_cache_path(self._scan_store_path),
                now=self._now,
                on_update=self._on_provider_series_update,
            )
            codex_series = source.current
        self._codex_series = codex_series

    def _on_provider_series_update(self) -> None:
        """A new provider series arrived; the cached service drops its snapshot."""

    def _local_history(self, root: Path | str, *, provider: str) -> LocalHistoryImport:
        """Serve the persistent scan store when it has this root, else scan live.

        The live scan is bounded by bytes and files, so on a machine with years
        of transcripts it can only ever see the last few days; the store is the
        same parse made once and kept. An absent or empty store is not an empty
        history, so it falls through rather than reporting zero.
        """

        # While the first scan of an empty store runs, the store holds whichever
        # files happened to be parsed first -- usually the oldest -- so serving
        # it would show weeks-old history and nothing from today. The bounded
        # live scan is the honest answer until that first pass completes.
        stored = (
            None
            if self._store_is_building()
            else read_store_history(root, provider=provider, store_path=self._scan_store_path)
        )
        return stored if stored is not None else discover_local_cli_history(root, provider=provider)

    def _store_is_building(self) -> bool:
        """Whether a first scan of an empty store is in progress (none here)."""

        return False

    def _history_store_freshness(self, observed: datetime) -> StoreFreshness:
        """Describe the store as found; the cached service adds its refresher."""

        return assess_store(
            self._scan_store_path,
            observed,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        )

    def _rate_card(self) -> tuple[RateCard | None, str | None]:
        """Load the vendored rate card, degrading to an explicit error code."""

        try:
            return self._rate_card_loader(), None
        except RateCardError:
            return None, "rate_card_unavailable"

    def _probe_account_status(self) -> dict[str, ProviderProbe]:
        return {"claude": self._claude_probe(), "codex": self._codex_probe()}

    def _anchor_plan(
        self, provider: str, imported: LocalHistoryImport, card: RateCard | None, observed: datetime
    ) -> AnchorPlan:
        """Provider-anchored estimate for Codex; Claude has nothing to anchor to."""

        if provider != "codex":
            return not_applicable_plan()
        if card is None:
            return unavailable_plan("rate_card_unavailable")
        try:
            observation = self._codex_series()
        except Exception:  # noqa: BLE001 - a series fault degrades to measured-only
            observation = SeriesObservation(series=None, reason=REASON_SERIES_UNAVAILABLE)
        if observation.series is None:
            return unavailable_plan(observation.reason or REASON_SERIES_UNAVAILABLE)
        try:
            return plan_codex_anchor(
                imported.slot_rows,
                card,
                observation.series,
                excluded_utc_days={row.usage_date for row in imported.legacy_rows},
                series_observed_at=observation.observed_at,
                now=observed,
            )
        except ValueError:
            return unavailable_plan(REASON_SERIES_INVALID, observed_at=observation.observed_at)

    def _after_pricing(self, unpriced_models: Mapping[str, str]) -> None:
        """Hook for the cached service's rate-card refresher; nothing here."""

    def dashboard(self) -> dict[str, Any]:
        # Re-read the system timezone rules: days are derived from UTC slots
        # at serve time, so a machine that changed zone is answered in its new
        # zone by the next refresh, with no rescan and no restart.
        if hasattr(time, "tzset"):
            time.tzset()
        observed = self._now().astimezone(timezone.utc)
        local = observed.astimezone()
        probes = self._probe_account_status()
        card, card_error = self._rate_card()
        providers: dict[str, Any] = {}
        all_errors: list[dict[str, str]] = []
        unpriced_models: dict[str, str] = {}
        for provider in ("claude", "codex"):
            root = self.home / (".claude" if provider == "claude" else ".codex")
            imported = self._history_loader(root, provider=provider)
            cost_import = self._cost_cache_loader(
                self._cost_cache_root,
                provider=provider,
                wanted=((row.usage_date, row.model) for row in imported.rows),
            )
            probe = probes[provider]
            errors = list(probe.errors)
            if card_error is not None:
                errors.append(card_error)
            if imported.parse_error_count:
                errors.append(f"{provider}_history_partial")
            windows = _with_local_pace(probe.windows, observed)
            runout = _runout(windows, observed)
            history = _history(imported, local, cost_import.costs, card)
            cost = _cost_component(imported, card, cost_import)
            if card is not None:
                unpriced_models.update(_unpriced_model_reasons(imported, card))
                plan = self._anchor_plan(provider, imported, card, observed)
                apply_anchor_plan(
                    history,
                    cost,
                    plan,
                    measured_nanos=cost.get("amount_nanos"),
                    legacy_nanos=sum(
                        row.api_rate_estimate_nanos or 0 for row in imported.legacy_rows
                    ),
                )
                if plan.state == "unavailable":
                    errors.append(f"{provider}_provider_anchor_unavailable")
            history["daily"] = history["daily"][-400:]
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
                "runout": runout,
                "history": history,
                "costs": {
                    "provider_billed": {
                        "amount_nanos": None,
                        "currency": None,
                        "semantics": "unknown",
                    },
                    "provider_native": {"amount_nanos": None, "semantics": "unknown"},
                    "api_rate_estimate": cost,
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
                        "runout": runout,
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
        self._after_pricing(unpriced_models)
        generated = _utc_iso(observed)
        return {
            "schema": "coordharness.usage-intelligence.v1",
            "generated_at": generated,
            "stale_after": _utc_iso(observed + timedelta(seconds=30)),
            "refresh": {"state": "fresh", "generated_at": generated},
            # Without this a reader cannot tell "no usage today" from "the store
            # has not been scanned today"; both would render as a zero.
            "history_store": self._history_store_freshness(observed).to_payload(),
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
