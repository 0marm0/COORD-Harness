"""Bounded read-only projection of current-user local cost-usage caches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import time
from typing import Any

from .importers import parse_codexbar_cache
from .ledger import LedgerIntegrityError


_CACHE_NAME = re.compile(r"^(claude|codex)-v([1-9][0-9]*)[.]json$")
_MAX_CACHE_BYTES = 256 * 1024 * 1024
_MAX_SMALL_CACHE_BYTES = 16 * 1024 * 1024
_MAX_COMPACT_BYTES = 8 * 1024 * 1024
_MAX_WANTED_ROWS = 4_096
_MAX_SAFE_INTEGER = 9_007_199_254_740_991

_CLAUDE_JQ = r"""
. as $root
| {
    version,
    lastScanUnixMs,
    scanSinceKey,
    scanUntilKey,
    costs: (
      reduce (
        ($root.days // {} | to_entries[]) as $day
        | ($day.value // {} | to_entries[])
        | select($wanted[$day.key][.key] == true)
        | select(.value | type == "array" and length >= 5)
        | {day: $day.key, model: .key, cost: .value[4]}
      ) as $row
      ({}; .[$row.day][$row.model] = $row.cost)
    )
  }
"""

_CODEX_JQ = r"""
. as $root
| {
    version,
    lastScanUnixMs,
    scanSinceKey,
    scanUntilKey,
    costs: (
      reduce (
        ($root.files // {} | .[] | (.codexCostNanos // {}) | to_entries[]) as $day
        | ($day.value // {} | to_entries[])
        | select($wanted[$day.key][.key] == true)
        | {day: $day.key, model: .key, cost: .value}
      ) as $row
      ({};
        .[$row.day][$row.model] =
          ((.[$row.day][$row.model] // 0) + $row.cost)
      )
    )
  }
"""


@dataclass(frozen=True)
class LocalCostCacheImport:
    provider: str
    costs: Mapping[tuple[str, str], int]
    observed_at: str | None = None
    coverage_start: str | None = None
    coverage_end: str | None = None

    @classmethod
    def empty(cls, provider: str) -> "LocalCostCacheImport":
        return cls(provider=provider, costs={})

    def cost_component(self) -> dict[str, Any]:
        if not self.costs:
            return {"amount_nanos": None, "semantics": "unknown"}
        component: dict[str, Any] = {
            "amount_nanos": sum(self.costs.values()),
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
        }
        if self.observed_at is not None:
            component["observed_at"] = self.observed_at
        if self.coverage_start is not None:
            component["coverage_start"] = self.coverage_start
        if self.coverage_end is not None:
            component["coverage_end"] = self.coverage_end
        return component


def _valid_day(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _cache_path(root: Path, provider: str) -> tuple[Path, os.stat_result] | None:
    if provider not in {"claude", "codex"} or not root.is_dir() or root.is_symlink():
        return None
    candidates: list[tuple[int, Path, os.stat_result]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for path in entries:
        match = _CACHE_NAME.fullmatch(path.name)
        if match is None or match.group(1) != provider or path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file() or not 0 < stat.st_size <= _MAX_CACHE_BYTES:
            continue
        candidates.append((int(match.group(2)), path, stat))
    if not candidates:
        return None
    _, path, stat = max(candidates, key=lambda item: item[0])
    return path, stat


def _wanted_rows(values: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    valid = {
        (day, model)
        for day, model in values
        if _valid_day(day) is not None
        and isinstance(model, str)
        and 0 < len(model) <= 120
        and not any(ord(character) < 32 for character in model)
    }
    return tuple(sorted(valid, reverse=True)[:_MAX_WANTED_ROWS])


def _strict_json(payload: bytes | str) -> Any:
    return json.loads(
        payload,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
    )


def _bounded_jq(path: Path, provider: str, wanted: tuple[tuple[str, str], ...]) -> Any:
    jq = shutil.which("jq")
    if jq is None:
        raise FileNotFoundError("jq unavailable")
    wanted_map: dict[str, dict[str, bool]] = {}
    for day, model in wanted:
        wanted_map.setdefault(day, {})[model] = True
    command = [
        jq,
        "-c",
        "--argjson",
        "wanted",
        json.dumps(wanted_map, separators=(",", ":"), allow_nan=False),
        _CLAUDE_JQ if provider == "claude" else _CODEX_JQ,
        str(path),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd="/private/tmp" if os.path.isdir("/private/tmp") else None,
    )
    payload = bytearray()
    deadline = time.monotonic() + 20
    try:
        assert process.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("cost cache projection timeout")
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("cost cache projection timeout")
            chunk = os.read(
                process.stdout.fileno(),
                min(65_536, _MAX_COMPACT_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > _MAX_COMPACT_BYTES:
                raise ValueError("cost cache projection too large")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise ValueError("cost cache projection failed")
        return _strict_json(bytes(payload))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def _small_projection(
    path: Path,
    provider: str,
    wanted: tuple[tuple[str, str], ...],
    captured_at: str,
) -> dict[str, Any]:
    raw = _strict_json(path.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError("invalid cost cache root")
    parsed = parse_codexbar_cache(path, provider=provider, captured_at=captured_at)
    wanted_set = set(wanted)
    costs: dict[str, dict[str, int]] = {}
    for row in parsed.observation.rows:
        key = (row.usage_date, row.model)
        if key in wanted_set and row.api_rate_estimate_nanos is not None:
            costs.setdefault(row.usage_date, {})[row.model] = row.api_rate_estimate_nanos
    return {
        "version": raw.get("version"),
        "lastScanUnixMs": raw.get("lastScanUnixMs"),
        "scanSinceKey": raw.get("scanSinceKey"),
        "scanUntilKey": raw.get("scanUntilKey"),
        "costs": {day: models for day, models in costs.items() if models},
    }


def _validated_import(
    provider: str,
    compact: Any,
    stat: os.stat_result,
) -> LocalCostCacheImport:
    if not isinstance(compact, dict) or compact.get("version") != 1:
        raise ValueError("invalid cost cache version")
    raw_costs = compact.get("costs")
    if not isinstance(raw_costs, dict) or len(raw_costs) > _MAX_WANTED_ROWS:
        raise ValueError("invalid cost map")
    costs: dict[tuple[str, str], int] = {}
    for day, models in raw_costs.items():
        if _valid_day(day) is None or not isinstance(models, dict):
            raise ValueError("invalid cost day")
        for model, cost in models.items():
            if (
                not isinstance(model, str)
                or not model
                or len(model) > 120
                or isinstance(cost, bool)
                or not isinstance(cost, int)
                or not 0 <= cost <= _MAX_SAFE_INTEGER
            ):
                raise ValueError("invalid cost row")
            costs[(day, model)] = cost
    observed_value = compact.get("lastScanUnixMs")
    observed_seconds = (
        observed_value / 1000
        if isinstance(observed_value, int)
        and not isinstance(observed_value, bool)
        and 0 < observed_value <= _MAX_SAFE_INTEGER
        else stat.st_mtime
    )
    observed_at = (
        datetime.fromtimestamp(observed_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return LocalCostCacheImport(
        provider=provider,
        costs=costs,
        observed_at=observed_at,
        coverage_start=_valid_day(compact.get("scanSinceKey")),
        coverage_end=_valid_day(compact.get("scanUntilKey")),
    )


@lru_cache(maxsize=8)
def _read_cached(
    path_text: str,
    provider: str,
    wanted: tuple[tuple[str, str], ...],
    mtime_ns: int,
    size: int,
) -> LocalCostCacheImport:
    del mtime_ns
    path = Path(path_text)
    stat = path.stat()
    captured_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    compact = (
        _small_projection(path, provider, wanted, captured_at)
        if size <= _MAX_SMALL_CACHE_BYTES
        else _bounded_jq(path, provider, wanted)
    )
    return _validated_import(provider, compact, stat)


def read_local_cost_cache(
    root: Path | str,
    *,
    provider: str,
    wanted: Iterable[tuple[str, str]],
) -> LocalCostCacheImport:
    """Return only costs matching local history keys; never mutate or identify the cache."""

    selected = _cache_path(Path(root), provider)
    wanted_rows = _wanted_rows(wanted)
    if selected is None or not wanted_rows:
        return LocalCostCacheImport.empty(provider)
    path, stat = selected
    try:
        return _read_cached(str(path), provider, wanted_rows, stat.st_mtime_ns, stat.st_size)
    except (
        FileNotFoundError,
        LedgerIntegrityError,
        OSError,
        OverflowError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        TimeoutError,
    ):
        return LocalCostCacheImport.empty(provider)
