from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.usage import LedgerIntegrityError, SourceInstance, UsageLedger
from coordharness.usage.importers import parse_codexbar_cache, parse_legacy_high_water


CAPTURED = "2026-08-11T12:00:00+00:00"


def _source(provider: str) -> SourceInstance:
    return SourceInstance(
        provider=provider,
        account_key=f"{provider}-account",
        source_kind="legacy_fixture",
        root_uri=f"fixture://{provider}",
        timezone_name="America/New_York",
        root_identity_digest="1" * 64,
    )


def test_claude_legacy_import_preserves_all_token_buckets_but_is_not_canonical(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({
        "schema": "coordharness.claude-usage-high-water.v1",
        "days": {"2026-03-01": {"claude-opus": [10, 20, 30, 40, 5_000]}},
    }))
    parsed = parse_legacy_high_water(path, provider="claude", captured_at=CAPTURED)
    row = parsed.observation.rows[0]
    assert (row.input_tokens, row.cache_create_other_tokens, row.cache_read_tokens, row.output_tokens) == (10, 20, 30, 40)

    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source("claude"))
        observation_id, _ = ledger.record_observation(source_id, parsed.observation)
        with pytest.raises(LedgerIntegrityError, match="custody evidence"):
            ledger.select_canonical(observation_id, reason="legacy is an envelope")


def test_codex_legacy_import_includes_cache_read_in_explicit_token_total(tmp_path: Path) -> None:
    path = tmp_path / "codex.json"
    path.write_text(json.dumps({
        "schema": "coordharness.codex-usage-high-water.v1",
        "days": {"2026-03-01": {"gpt": [10, 20, 30, 5_000]}},
    }))
    parsed = parse_legacy_high_water(path, provider="codex", captured_at=CAPTURED)
    row = parsed.observation.rows[0]
    assert row.input_tokens + row.cache_read_tokens + row.output_tokens == 60


def test_cache_checkpoint_is_coherent_and_non_additive_by_default(tmp_path: Path) -> None:
    path = tmp_path / "codex-v12.json"
    path.write_text(json.dumps({
        "days": {"2026-08-10": {"gpt": [10, 20, 30]}},
        "files": {"session": {"codexCostNanos": {"2026-08-10": {"gpt": 7_000}}}},
    }))
    parsed = parse_codexbar_cache(path, provider="codex", captured_at=CAPTURED)
    assert parsed.observation.observation_role == "cache_checkpoint"
    assert parsed.observation.canonical_eligible is False
    assert parsed.observation.rows[0].api_rate_estimate_nanos == 7_000


def test_importer_rejects_negative_or_malformed_values(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        "schema": "coordharness.codex-usage-high-water.v1",
        "days": {"2026-03-01": {"gpt": [10, -1, 30, 5_000]}},
    }))
    with pytest.raises(LedgerIntegrityError, match="nonnegative"):
        parse_legacy_high_water(path, provider="codex", captured_at=CAPTURED)
