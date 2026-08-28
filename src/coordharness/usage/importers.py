
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .ledger import DailyUsage, LedgerIntegrityError, ObservationInput, sha256_file
from .local_history import (
    LocalHistoryImport as LocalHistoryImport,
    discover_local_cli_history as discover_local_cli_history,
)


CLAUDE_HIGH_WATER_SCHEMA = "coordharness.claude-usage-high-water.v1"
CODEX_HIGH_WATER_SCHEMA = "coordharness.codex-usage-high-water.v1"


@dataclass(frozen=True)
class ParsedObservation:
    provider: str
    source_schema: str
    observation: ObservationInput


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerIntegrityError(f"cannot parse usage artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerIntegrityError(f"usage artifact must be a JSON object: {path}")
    return payload


def _nonnegative(value: Any, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerIntegrityError(f"invalid nonnegative integer at {context}")
    return value


def _base_observation(
    *,
    path: Path,
    schema: str,
    captured_at: str,
    rows: list[DailyUsage],
    role: str,
    canonical_eligible: bool,
    coverage_state: str,
    producer_key: str,
    parser_version: str,
    pricing_key: str,
    notes: str,
) -> ObservationInput:
    return ObservationInput(
        artifact_digest=sha256_file(path),
        artifact_version=schema,
        producer_key=producer_key,
        parser_version=parser_version,
        pricing_key=pricing_key,
        captured_at=captured_at,
        coverage_state=coverage_state,
        observation_role=role,
        canonical_eligible=canonical_eligible,
        rows=rows,
        artifact_uri=str(path.resolve()),
        notes=notes,
    )


def parse_legacy_high_water(
    path: Path,
    *,
    provider: str,
    captured_at: str,
    parser_version: str = "usage-v2-legacy-importer-v1",
    pricing_key: str = "legacy-cache-reported-cost",
) -> ParsedObservation:

    payload = _load_json(path)
    expected = CLAUDE_HIGH_WATER_SCHEMA if provider == "claude" else CODEX_HIGH_WATER_SCHEMA
    if payload.get("schema") != expected:
        raise LedgerIntegrityError(
            f"expected {expected!r}, observed {payload.get('schema')!r} in {path}"
        )
    days = payload.get("days")
    if not isinstance(days, dict):
        raise LedgerIntegrityError("legacy ledger has no days object")
    rows: list[DailyUsage] = []
    for usage_date, models in sorted(days.items()):
        if not isinstance(usage_date, str) or not isinstance(models, dict):
            raise LedgerIntegrityError("legacy ledger has an invalid day/model shape")
        for model, packed in sorted(models.items()):
            if not isinstance(model, str) or not isinstance(packed, list):
                raise LedgerIntegrityError(f"legacy ledger has an invalid row at {usage_date}")
            if provider == "claude":
                if len(packed) < 5:
                    raise LedgerIntegrityError(f"short Claude row at {usage_date}/{model}")
                rows.append(
                    DailyUsage(
                        usage_date=usage_date,
                        model=model,
                        input_tokens=_nonnegative(packed[0], context="Claude input"),
                        cache_create_other_tokens=_nonnegative(
                            packed[1], context="Claude cache create (undifferentiated)"
                        ),
                        cache_read_tokens=_nonnegative(packed[2], context="Claude cache read"),
                        output_tokens=_nonnegative(packed[3], context="Claude output"),
                        api_rate_estimate_nanos=_nonnegative(packed[4], context="Claude cost"),
                    )
                )
            else:
                if len(packed) < 4:
                    raise LedgerIntegrityError(f"short Codex row at {usage_date}/{model}")
                rows.append(
                    DailyUsage(
                        usage_date=usage_date,
                        model=model,
                        input_tokens=_nonnegative(packed[0], context="Codex input"),
                        cache_read_tokens=_nonnegative(packed[1], context="Codex cache read"),
                        output_tokens=_nonnegative(packed[2], context="Codex output"),
                        api_rate_estimate_nanos=_nonnegative(packed[3], context="Codex cost"),
                    )
                )
    observation = _base_observation(
        path=path,
        schema=expected,
        captured_at=captured_at,
        rows=rows,
        role="legacy_high_water",
        canonical_eligible=False,
        coverage_state="unknown",
        producer_key="coordharness-v1-component-high-water",
        parser_version=parser_version,
        pricing_key=pricing_key,
        notes=(
            "Component-wise historical envelope. Individual metric origins are unknown; "
            "the vector may never have appeared in one source observation."
        ),
    )
    return ParsedObservation(provider=provider, source_schema=expected, observation=observation)


def parse_codexbar_cache(
    path: Path,
    *,
    provider: str,
    captured_at: str,
    canonical_eligible: bool = False,
    coverage_state: str = "unknown",
    parser_version: str = "usage-v2-codexbar-cache-v1",
    pricing_key: str = "codexbar-cache-cost",
) -> ParsedObservation:

    payload = _load_json(path)
    days = payload.get("days")
    if not isinstance(days, dict):
        raise LedgerIntegrityError("CodexBar cache has no days object")
    aggregated_costs: dict[tuple[str, str], int] = {}
    for file_row in (payload.get("files") or {}).values():
        if not isinstance(file_row, dict):
            continue
        cost_key = "claudeCostNanos" if provider == "claude" else "codexCostNanos"
        for day, models in (file_row.get(cost_key) or {}).items():
            if not isinstance(models, dict):
                raise LedgerIntegrityError(f"invalid cost map for {day}")
            for model, value in models.items():
                key = (str(day), str(model))
                aggregated_costs[key] = aggregated_costs.get(key, 0) + _nonnegative(
                    value, context=f"{day}/{model}/cost"
                )
    rows: list[DailyUsage] = []
    for usage_date, models in sorted(days.items()):
        if not isinstance(usage_date, str) or not isinstance(models, dict):
            raise LedgerIntegrityError("CodexBar cache has an invalid day/model shape")
        for model, packed in sorted(models.items()):
            if not isinstance(model, str) or not isinstance(packed, list):
                raise LedgerIntegrityError(f"invalid cache row at {usage_date}/{model}")
            embedded_cost: int | None = None
            if provider == "claude":
                if len(packed) < 4:
                    raise LedgerIntegrityError(f"short Claude cache row at {usage_date}/{model}")
                if len(packed) >= 5:
                    embedded_cost = _nonnegative(packed[4], context="Claude embedded cost")
                rows.append(
                    DailyUsage(
                        usage_date=usage_date,
                        model=model,
                        input_tokens=_nonnegative(packed[0], context="Claude input"),
                        cache_create_other_tokens=_nonnegative(packed[1], context="Claude cache create"),
                        cache_read_tokens=_nonnegative(packed[2], context="Claude cache read"),
                        output_tokens=_nonnegative(packed[3], context="Claude output"),
                        api_rate_estimate_nanos=aggregated_costs.get(
                            (usage_date, model), embedded_cost
                        ),
                    )
                )
            else:
                if len(packed) < 3:
                    raise LedgerIntegrityError(f"short Codex cache row at {usage_date}/{model}")
                rows.append(
                    DailyUsage(
                        usage_date=usage_date,
                        model=model,
                        input_tokens=_nonnegative(packed[0], context="Codex input"),
                        cache_read_tokens=_nonnegative(packed[1], context="Codex cache read"),
                        output_tokens=_nonnegative(packed[2], context="Codex output"),
                        api_rate_estimate_nanos=aggregated_costs.get((usage_date, model)),
                    )
                )
    schema = f"codexbar.{provider}-cache"
    return ParsedObservation(
        provider=provider,
        source_schema=schema,
        observation=_base_observation(
            path=path,
            schema=schema,
            captured_at=captured_at,
            rows=rows,
            role="cache_checkpoint",
            canonical_eligible=canonical_eligible,
            coverage_state=coverage_state,
            producer_key=f"codexbar-{provider}",
            parser_version=parser_version,
            pricing_key=pricing_key,
            notes="One coherent cache checkpoint; never additive with overlapping raw evidence.",
        ),
    )
