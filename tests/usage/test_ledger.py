from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import sqlite3

import pytest

from coordharness.usage import (
    BillingLineItem,
    DailyUsage,
    LedgerIntegrityError,
    ObservationInput,
    SourceInstance,
    UsageLedger,
)
from coordharness.usage.ledger import SCHEMA_ID


def _source(*, provider: str = "claude", account: str = "acct-A") -> SourceInstance:
    return SourceInstance(
        provider=provider,
        account_key=account,
        source_kind="fixture",
        root_uri=f"fixture://{provider}/{account}",
        timezone_name="America/New_York",
        root_identity_digest="1" * 64,
        label=f"{provider} {account}",
    )


def _observation(
    *,
    digest: str,
    input_tokens: int,
    cache_read: int = 0,
    output_tokens: int = 0,
    cost: int | None = None,
    complete: bool = True,
    eligible: bool = True,
) -> ObservationInput:
    return ObservationInput(
        artifact_digest=digest,
        artifact_version="fixture-v1",
        producer_key="fixture",
        parser_version="parser-v1",
        pricing_key="pricing-v1",
        captured_at="2026-08-11T12:00:00+00:00",
        coverage_state="complete" if complete else "partial",
        observation_role="raw",
        canonical_eligible=eligible,
        coverage_start="2026-08-10T00:00:00+00:00" if complete else None,
        coverage_end="2026-08-11T00:00:00+00:00" if complete else None,
        source_manifest_digest="f" * 64 if complete else None,
        files_scanned=1 if complete else None,
        records_scanned=1 if complete else None,
        records_accepted=1 if complete else None,
        rows=[
            DailyUsage(
                usage_date="2026-08-10",
                model="claude-test",
                input_tokens=input_tokens,
                cache_read_tokens=cache_read,
                output_tokens=output_tokens,
                api_rate_estimate_nanos=cost,
            )
        ],
    )


def test_canonical_correction_can_decrease_while_custody_retains_origins(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        high, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100, cache_read=80, output_tokens=5, cost=1_000)
        )
        low, _ = ledger.record_observation(
            source_id, _observation(digest="b" * 64, input_tokens=90, cache_read=85, output_tokens=7, cost=900)
        )
        ledger.select_canonical(high, reason="initial coherent parse")
        assert ledger.lifetime_totals()["total_tokens"] == 185

        ledger.select_canonical(low, reason="corrected parser output")

        assert ledger.lifetime_totals()["total_tokens"] == 182
        custody = {
            row["metric"]: (row["value"], row["origin_observation_id"])
            for row in ledger.custody_high_water_metrics()
        }
        assert custody["input_tokens"] == (100, high)
        assert custody["cache_read_tokens"] == (85, low)
        assert custody["output_tokens"] == (7, low)
        assert custody["api_rate_estimate_nanos"] == (1_000, high)
        assert ledger.verify_integrity()["status"] == "PASS"


def test_custody_components_are_not_exposed_as_a_coherent_total(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        first, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100, cache_read=80, output_tokens=5)
        )
        ledger.record_observation(
            source_id, _observation(digest="b" * 64, input_tokens=90, cache_read=85, output_tokens=7)
        )
        ledger.select_canonical(first, reason="only coherent selected row")

        with pytest.raises(LedgerIntegrityError, match="query custody metrics separately"):
            ledger.lifetime_totals(semantics="ever_observed_envelope")
        assert ledger.lifetime_totals()["total_tokens"] == 185


def test_same_artifact_is_idempotent_but_conflicting_parse_fails_closed(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        observation = _observation(digest="a" * 64, input_tokens=100)
        first, created = ledger.record_observation(source_id, observation)
        second, created_again = ledger.record_observation(source_id, observation)
        assert first == second
        assert created is True
        assert created_again is False

        conflicting = replace(observation, rows=[replace(observation.rows[0], input_tokens=101)])
        with pytest.raises(LedgerIntegrityError, match="conflicting content"):
            ledger.record_observation(source_id, conflicting)


def test_partial_and_legacy_observations_are_not_silently_canonical(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        partial, _ = ledger.record_observation(
            source_id,
            _observation(digest="a" * 64, input_tokens=10, complete=False),
        )
        with pytest.raises(LedgerIntegrityError, match="explicit allow_partial"):
            ledger.select_canonical(partial, reason="should fail")
        ledger.select_canonical(partial, reason="explicit bounded cell", allow_partial=True)

        legacy, _ = ledger.record_observation(
            source_id,
            replace(
                _observation(digest="b" * 64, input_tokens=20),
                observation_role="legacy_high_water",
                canonical_eligible=False,
            ),
        )
        with pytest.raises(LedgerIntegrityError, match="custody evidence"):
            ledger.select_canonical(legacy, reason="should fail")


def test_accounts_and_providers_never_collapse(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        for provider, account, digest, tokens in (
            ("claude", "acct-A", "a" * 64, 10),
            ("claude", "acct-B", "b" * 64, 20),
            ("codex", "codex-profile", "c" * 64, 30),
        ):
            source_id = ledger.register_source(_source(provider=provider, account=account))
            observation = replace(
                _observation(digest=digest, input_tokens=tokens),
                rows=[DailyUsage(usage_date="2026-08-10", model=f"{provider}-test", input_tokens=tokens)],
            )
            observation_id, _ = ledger.record_observation(source_id, observation)
            ledger.select_canonical(observation_id, reason="fixture")

        totals = ledger.lifetime_totals()
        assert [(row["provider"], row["account_key"], row["total_tokens"]) for row in totals["groups"]] == [
            ("claude", "acct-A", 10),
            ("claude", "acct-B", 20),
            ("codex", "codex-profile", 30),
        ]


def test_global_unknown_account_is_rejected_but_source_scoped_unknowns_are_distinct(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        with pytest.raises(LedgerIntegrityError, match="source-scoped"):
            ledger.register_source(_source(account="unknown"))
        first = ledger.register_source(_source(account="unknown:profile-aaaa"))
        second = ledger.register_source(_source(account="unknown:profile-bbbb"))
        assert first != second


def test_tampering_is_detected_even_when_sqlite_integrity_is_ok(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite"
    with UsageLedger(path) as ledger:
        source_id = ledger.register_source(_source())
        observation_id, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100)
        )
        ledger.select_canonical(observation_id, reason="fixture")
        ledger.connection.execute("DROP TRIGGER daily_observation_no_update")
        ledger.connection.execute(
            "UPDATE daily_observation SET input_tokens = 999 WHERE observation_id = ?",
            (observation_id,),
        )
        ledger.connection.commit()
        result = ledger.verify_integrity()
        assert result["status"] == "FAIL"
        assert {failure["code"] for failure in result["failures"]} >= {
            "DAILY_ROW_DIGEST_MISMATCH",
            "OBSERVATION_DIGEST_MISMATCH",
        }


def test_immutable_tables_reject_update_and_delete(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        observation_id, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100)
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ledger.connection.execute(
                "UPDATE daily_observation SET input_tokens = 1 WHERE observation_id = ?",
                (observation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ledger.connection.execute(
                "DELETE FROM observation WHERE observation_id = ?", (observation_id,)
            )


def test_transaction_rolls_back_observation_and_commit_together(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        ledger.connection.execute(
            """
            CREATE TRIGGER fail_second_row BEFORE INSERT ON daily_observation
            WHEN NEW.model = 'fail' BEGIN SELECT RAISE(ABORT, 'injected crash'); END
            """
        )
        observation = replace(
            _observation(digest="a" * 64, input_tokens=1),
            rows=[
                DailyUsage(usage_date="2026-08-10", model="ok", input_tokens=1),
                DailyUsage(usage_date="2026-08-10", model="fail", input_tokens=1),
            ],
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected crash"):
            ledger.record_observation(source_id, observation)
        assert ledger.connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 0
        assert ledger.connection.execute(
            "SELECT COUNT(*) FROM ledger_commit WHERE entity_kind = 'observation'"
        ).fetchone()[0] == 0


def test_versioned_anchor_detects_rollback_and_anchor_tampering(tmp_path: Path) -> None:
    live_path = tmp_path / "live.sqlite"
    old_path = tmp_path / "old.sqlite"
    anchors = tmp_path / "anchors"
    with UsageLedger(live_path) as ledger:
        source_id = ledger.register_source(_source())
        with sqlite3.connect(old_path) as old:
            ledger.connection.backup(old)
        observation_id, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100)
        )
        ledger.select_canonical(observation_id, reason="fixture")
        anchor_path = ledger.write_anchor(
            anchors, created_at="2026-08-11T12:30:00+00:00"
        )
        assert ledger.verify_anchor(anchor_path)["status"] == "PASS"

        tampered = tmp_path / "tampered-anchor.json"
        payload = json.loads(anchor_path.read_text())
        payload["commit_hash"] = "0" * 64
        tampered.write_text(json.dumps(payload))
        result = ledger.verify_anchor(tampered)
        assert result["status"] == "FAIL"
        assert {failure["code"] for failure in result["failures"]} >= {
            "ANCHOR_DIGEST_MISMATCH",
            "ANCHOR_COMMIT_HASH_MISMATCH",
        }

    with UsageLedger(old_path) as rolled_back:
        result = rolled_back.verify_anchor(anchor_path)
        assert result["status"] == "FAIL"
        assert "ANCHOR_COMMIT_MISSING_OR_ROLLBACK" in {
            failure["code"] for failure in result["failures"]
        }


def test_provider_billing_is_signed_currency_separated_and_does_not_change_tokens(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        observation_id, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100, cost=700)
        )
        ledger.select_canonical(observation_id, reason="fixture")
        for key, currency, amount, category, digest in (
            ("invoice-1", "USD", 1_000, "usage", "b" * 64),
            ("refund-1", "USD", -250, "refund", "c" * 64),
            ("invoice-eur", "EUR", 500, "subscription", "d" * 64),
        ):
            ledger.record_billing_line_item(
                BillingLineItem(
                    provider="claude",
                    account_key="acct-A",
                    provider_item_key=key,
                    occurred_at="2026-08-11T13:00:00+00:00",
                    currency=currency,
                    amount_nanos=amount,
                    category=category,
                    artifact_digest=digest,
                    source_id=source_id,
                )
            )

        assert ledger.lifetime_totals()["total_tokens"] == 100
        assert ledger.lifetime_totals()["api_rate_estimate_nanos"] == 700
        assert ledger.provider_billed_totals() == [
            {"provider": "claude", "account_key": "acct-A", "currency": "EUR", "amount_nanos": 500},
            {"provider": "claude", "account_key": "acct-A", "currency": "USD", "amount_nanos": 750},
        ]
        assert ledger.verify_integrity()["status"] == "PASS"


def test_timezone_rebucket_withdraws_old_day_without_changing_lifetime_tokens(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        old, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64, input_tokens=100)
        )
        corrected_input = replace(
            _observation(digest="b" * 64, input_tokens=100),
            rows=[DailyUsage(usage_date="2026-08-11", model="claude-test", input_tokens=100)],
        )
        new, _ = ledger.record_observation(source_id, corrected_input)
        ledger.select_canonical(old, reason="original local-day policy")
        ledger.withdraw_canonical(
            provider="claude",
            account_key="acct-A",
            usage_date="2026-08-10",
            timezone_name="America/New_York",
            model="claude-test",
            reason="timezone policy correction",
        )
        ledger.select_canonical(new, reason="corrected local-day policy")

        assert ledger.lifetime_totals()["total_tokens"] == 100
        assert [row["usage_date"] for row in ledger.corrected_rows()] == ["2026-08-11"]
        assert ledger.verify_integrity()["status"] == "PASS"


def test_privileged_entity_deletion_creates_orphan_commit_and_invalidates_anchor(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite"
    anchors = tmp_path / "anchors"
    with UsageLedger(path) as ledger:
        source_id = ledger.register_source(_source())
        anchor = ledger.write_anchor(anchors)
        ledger.connection.execute("DROP TRIGGER source_instance_no_delete")
        ledger.connection.execute(
            "DELETE FROM source_instance WHERE source_id = ?", (source_id,)
        )
        ledger.connection.commit()

        verification = ledger.verify_integrity()
        assert verification["status"] == "FAIL"
        assert {
            (row.get("code"), row.get("entity_kind"), row.get("entity_id"))
            for row in verification["failures"]
        } >= {("ORPHAN_COMMIT", "source", source_id)}
        anchor_result = ledger.verify_anchor(anchor)
        assert anchor_result["status"] == "FAIL"
        assert "ANCHOR_LEDGER_INTEGRITY_FAILURE" in {
            row["code"] for row in anchor_result["failures"]
        }


def test_anchor_recomputes_entity_roots(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        ledger.register_source(_source())
        anchor = ledger.write_anchor(tmp_path / "anchors")
        payload = json.loads(anchor.read_text())
        payload["entity_roots"]["sources"] = "0" * 64
        tampered = tmp_path / "tampered-roots.json"
        tampered.write_text(json.dumps(payload))

        result = ledger.verify_anchor(tampered)
        assert result["status"] == "FAIL"
        assert {row["code"] for row in result["failures"]} >= {
            "ANCHOR_DIGEST_MISMATCH",
            "ANCHOR_ENTITY_ROOT_MISMATCH",
        }


def test_stale_or_spoofed_schema_is_rejected_without_auto_repair(tmp_path: Path) -> None:
    stale = tmp_path / "stale.sqlite"
    with sqlite3.connect(stale) as connection:
        connection.execute("CREATE TABLE ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO ledger_meta(key, value) VALUES('schema', 'coordharness.usage-v2.sqlite.v1')"
        )
    before = stale.read_bytes()
    with pytest.raises(LedgerIntegrityError, match="unsupported ledger schema"):
        UsageLedger(stale)
    assert stale.read_bytes() == before

    spoofed = tmp_path / "spoofed.sqlite"
    with sqlite3.connect(spoofed) as connection:
        connection.execute("CREATE TABLE ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO ledger_meta(key, value) VALUES('schema', ?)", (SCHEMA_ID,)
        )
        connection.execute(
            "INSERT INTO ledger_meta(key, value) VALUES('ledger_uuid', 'spoofed')"
        )
    before = spoofed.read_bytes()
    with pytest.raises(LedgerIntegrityError, match="schema shape mismatch"):
        UsageLedger(spoofed)
    assert spoofed.read_bytes() == before
