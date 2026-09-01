"""Bounded read-only import of current-user Claude and Codex CLI histories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import heapq
import json
from pathlib import Path
from typing import Any, Mapping

from .ledger import DailyUsage


@dataclass(frozen=True)
class LocalHistoryImport:
    provider: str
    rows: tuple[DailyUsage, ...]
    coverage_state: str
    root_identity_digest: str
    manifest_digest: str
    files_scanned: int
    records_scanned: int
    records_accepted: int
    records_rejected: int
    parse_error_count: int

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_kind": f"{self.provider}_cli_jsonl",
            "parser_version": "coord-local-cli-jsonl-v1",
            "coverage_state": self.coverage_state,
            "canonical": False,
            "root_identity_digest": self.root_identity_digest,
            "manifest_digest": self.manifest_digest,
            "files_scanned": self.files_scanned,
            "records_scanned": self.records_scanned,
            "records_accepted": self.records_accepted,
            "records_rejected": self.records_rejected,
            "parse_error_count": self.parse_error_count,
        }


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _day(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().date().isoformat() if parsed.tzinfo is not None else None


def _model(value: object, fallback: str = "unknown") -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if text and len(text) <= 120 and not any(ord(c) < 32 for c in text) else fallback


def _last_usage(value: object, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        found = value.get("last_token_usage") or value.get("lastTokenUsage")
        if isinstance(found, Mapping):
            return found
        for child in value.values():
            found = _last_usage(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value[:64]:
            found = _last_usage(child, depth + 1)
            if found is not None:
                return found
    return None


def _record(provider: str, value: Mapping[str, Any], model: str):
    if provider == "claude":
        message = value.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
        if not isinstance(usage, Mapping):
            return None
        day = _day(value.get("timestamp") or message.get("timestamp"))
        model = _model(message.get("model"))
        input_tokens, output_tokens = (
            _count(usage.get("input_tokens")),
            _count(usage.get("output_tokens")),
        )
        cache_read = _count(usage.get("cache_read_input_tokens")) or 0
        cache_other = _count(usage.get("cache_creation_input_tokens")) or 0
        detail = usage.get("cache_creation")
        cache_5m = (
            _count(detail.get("ephemeral_5m_input_tokens")) or 0
            if isinstance(detail, Mapping)
            else 0
        )
        cache_1h = (
            _count(detail.get("ephemeral_1h_input_tokens")) or 0
            if isinstance(detail, Mapping)
            else 0
        )
        if cache_5m or cache_1h:
            cache_other = 0
    else:
        usage = _last_usage(value)
        if usage is None:
            return None
        day = _day(value.get("timestamp"))
        input_tokens = _count(usage.get("input_tokens") or usage.get("inputTokens"))
        output_tokens = _count(usage.get("output_tokens") or usage.get("outputTokens"))
        cache_read = _count(usage.get("cached_input_tokens") or usage.get("cachedInputTokens")) or 0
        cache_5m = cache_1h = cache_other = 0
    if day is None or input_tokens is None or output_tokens is None:
        return None
    return (
        day,
        model,
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_create_5m_tokens": cache_5m,
            "cache_create_1h_tokens": cache_1h,
            "cache_create_other_tokens": cache_other,
        },
    )


def discover_local_cli_history(
    root: Path | str,
    *,
    provider: str,
    max_files: int = 2_000,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_records: int = 100_000,
) -> LocalHistoryImport:
    """Scan only bounded, nonsymlink JSONL files beneath one CLI root.

    The result is always partial or unknown: local histories can be deleted or
    compacted and therefore are never proof of complete provider usage.
    """

    provider = provider.lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("unsupported provider")
    root = Path(root)
    identity = hashlib.sha256(str(root.expanduser().resolve()).encode()).hexdigest()
    scan_root = root / ("projects" if provider == "claude" else "sessions")
    files = []
    if scan_root.is_dir() and not scan_root.is_symlink():

        def recency(path: Path) -> tuple[int, str]:
            try:
                return path.stat().st_mtime_ns, path.as_posix()
            except OSError:
                return -1, path.as_posix()

        files = heapq.nlargest(
            max_files,
            (
                path
                for path in scan_root.rglob("*.jsonl")
                if path.is_file() and not path.is_symlink()
            ),
            key=recency,
        )
    totals: dict[tuple[str, str], dict[str, int]] = {}
    manifest = hashlib.sha256()
    file_count = scanned = accepted = rejected = errors = total_bytes = 0
    for path in files:
        try:
            size = path.stat().st_size
            relative = path.relative_to(scan_root).as_posix()
        except (OSError, ValueError):
            continue
        if size > 16 * 1024 * 1024 or total_bytes + size > max_total_bytes:
            rejected += 1
            continue
        total_bytes += size
        file_count += 1
        manifest.update(f"{relative}\0{size}\0".encode())
        model = "unknown"
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    if scanned >= max_records:
                        break
                    scanned += 1
                    if len(raw) > 2 * 1024 * 1024:
                        rejected += 1
                        errors += 1
                        continue
                    try:
                        value = json.loads(
                            raw,
                            parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("constant")),
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                        rejected += 1
                        errors += 1
                        continue
                    if not isinstance(value, Mapping):
                        rejected += 1
                        continue
                    if provider == "codex":
                        payload = value.get("payload")
                        if isinstance(payload, Mapping) and payload.get("model") is not None:
                            model = _model(payload.get("model"), model)
                    record = _record(provider, value, model)
                    if record is None:
                        continue
                    day, row_model, metrics = record
                    bucket = totals.setdefault(
                        (day, row_model), {**{name: 0 for name in metrics}, "request_count": 0}
                    )
                    for name, amount in metrics.items():
                        bucket[name] += amount
                    bucket["request_count"] += 1
                    accepted += 1
        except OSError:
            rejected += 1
            errors += 1
    rows = tuple(
        DailyUsage(usage_date=day, model=model, **metrics)
        for (day, model), metrics in sorted(totals.items())
    )
    return LocalHistoryImport(
        provider=provider,
        rows=rows,
        coverage_state="partial" if rows else "unknown",
        root_identity_digest=identity,
        manifest_digest=manifest.hexdigest(),
        files_scanned=file_count,
        records_scanned=scanned,
        records_accepted=accepted,
        records_rejected=rejected,
        parse_error_count=errors,
    )
