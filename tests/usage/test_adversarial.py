from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from coordharness.usage import (
    DailyUsage,
    LedgerIntegrityError,
    ObservationInput,
    SourceInstance,
    UsageLedger,
)


CAPTURED_AT = "2026-08-11T12:00:00+00:00"


def _source(
    *,
    account_key: str = "acct-primary",
    label: str = "shared@example.test",
    timezone_name: str = "America/New_York",
    root_suffix: str = "primary",
) -> SourceInstance:
    return SourceInstance(
        provider="claude",
        account_key=account_key,
        source_kind="adversarial_fixture",
        root_uri=f"fixture://claude/{root_suffix}",
        timezone_name=timezone_name,
        root_identity_digest="1" * 64,
        label=label,
    )


def _observation(
    *,
    digest: str,
    usage_date: str = "2026-08-10",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read_tokens: int = 0,
    role: str = "raw",
    eligible: bool = True,
    coverage_state: str = "complete",
    producer_key: str = "adversarial-raw-parser",
    **coverage_overrides: object,
) -> ObservationInput:
    complete = coverage_state == "complete"
    coverage = {
        "coverage_start": "2026-08-10T00:00:00+00:00" if complete else None,
        "coverage_end": "2026-08-11T00:00:00+00:00" if complete else None,
        "source_manifest_digest": "f" * 64 if complete else None,
        "files_scanned": 1 if complete else None,
        "records_scanned": 1 if complete else None,
        "records_accepted": 1 if complete else None,
        "records_rejected": 0,
        "records_conflicted": 0,
        "parse_error_count": 0,
    }
    coverage.update(coverage_overrides)
    return ObservationInput(
        artifact_digest=digest,
        artifact_version="adversarial-fixture-v1",
        producer_key=producer_key,
        parser_version="adversarial-parser-v1",
        pricing_key="fixture",
        captured_at=CAPTURED_AT,
        coverage_state=coverage_state,
        observation_role=role,
        canonical_eligible=eligible,
        rows=[
            DailyUsage(
                usage_date=usage_date,
                model="claude-adversarial",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        ],
        **coverage,
    )


def test_concurrent_duplicate_observation_ingestion_converges_without_double_count(
    tmp_path: Path,
) -> None:
    database = tmp_path / "usage.sqlite"
    with UsageLedger(database) as ledger:
        source_id = ledger.register_source(_source())

    observation = _observation(digest="a" * 64)
    worker_count = 8
    ready = threading.Barrier(worker_count)

    def ingest() -> tuple[str, bool]:
        with UsageLedger(database) as ledger:
            ready.wait(timeout=10)
            return ledger.record_observation(source_id, observation)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = [future.result() for future in [executor.submit(ingest) for _ in range(worker_count)]]

    assert len({observation_id for observation_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with UsageLedger(database) as ledger:
        assert ledger.connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 1
        assert ledger.connection.execute("SELECT COUNT(*) FROM daily_observation").fetchone()[0] == 1
        assert ledger.connection.execute(
            "SELECT COUNT(*) FROM ledger_commit WHERE entity_kind = 'observation'"
        ).fetchone()[0] == 1
        observation_id = results[0][0]
        ledger.select_canonical(observation_id, reason="one coherent concurrent import")
        assert ledger.lifetime_totals()["total_tokens"] == 110
        assert ledger.verify_integrity()["status"] == "PASS"


def test_overlapping_cache_and_raw_observations_select_one_coherent_cell_not_a_sum(
    tmp_path: Path,
) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        raw_id, _ = ledger.record_observation(
            source_id,
            _observation(
                digest="a" * 64,
                input_tokens=100,
                output_tokens=10,
                role="raw",
                producer_key="raw-parser",
            ),
        )
        cache_id, _ = ledger.record_observation(
            source_id,
            _observation(
                digest="b" * 64,
                input_tokens=95,
                output_tokens=12,
                role="cache_checkpoint",
                producer_key="cache-parser",
            ),
        )

        ledger.select_canonical(raw_id, reason="initial raw parse")
        assert ledger.lifetime_totals()["total_tokens"] == 110
        ledger.select_canonical(cache_id, reason="replacement coherent checkpoint")

        assert ledger.lifetime_totals()["total_tokens"] == 107
        corrected = ledger.corrected_rows()
        assert len(corrected) == 1
        assert corrected[0]["observation_id"] == cache_id
        assert corrected[0]["input_tokens"] + corrected[0]["output_tokens"] == 107


@pytest.mark.parametrize(
    "invalid_counts",
    [
        {"records_scanned": 2, "records_accepted": 1, "records_rejected": 1},
        {"records_conflicted": 1},
        {"parse_error_count": 1},
    ],
    ids=("rejected-record", "conflicting-record", "parse-error"),
)
def test_complete_coverage_rejects_any_reject_conflict_or_parse_error(
    tmp_path: Path, invalid_counts: dict[str, int]
) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        with pytest.raises(
            LedgerIntegrityError,
            match="complete coverage cannot contain rejects, conflicts, or parse errors",
        ):
            ledger.record_observation(
                source_id,
                _observation(digest="a" * 64, **invalid_counts),
            )
        assert ledger.connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 0


def test_same_email_like_labels_do_not_collapse_distinct_account_keys(tmp_path: Path) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        first_source = ledger.register_source(
            _source(account_key="account-workspace-A", root_suffix="profile-a")
        )
        second_source = ledger.register_source(
            _source(account_key="account-workspace-B", root_suffix="profile-b")
        )
        first_id, _ = ledger.record_observation(
            first_source,
            _observation(digest="a" * 64, input_tokens=10, output_tokens=1),
        )
        second_id, _ = ledger.record_observation(
            second_source,
            _observation(digest="b" * 64, input_tokens=20, output_tokens=2),
        )
        ledger.select_canonical(first_id, reason="account A")
        ledger.select_canonical(second_id, reason="account B")

        totals = ledger.lifetime_totals()
        assert totals["total_tokens"] == 33
        assert [group["account_key"] for group in totals["groups"]] == [
            "account-workspace-A",
            "account-workspace-B",
        ]
        assert [group["total_tokens"] for group in totals["groups"]] == [11, 22]


def test_timezone_day_correction_moves_cell_without_changing_lifetime_tokens(
    tmp_path: Path,
) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        utc_source = ledger.register_source(
            _source(timezone_name="UTC", root_suffix="utc-profile")
        )
        local_source = ledger.register_source(
            _source(timezone_name="America/New_York", root_suffix="local-profile")
        )
        utc_id, _ = ledger.record_observation(
            utc_source,
            _observation(
                digest="a" * 64,
                usage_date="2026-08-11",
                input_tokens=90,
                output_tokens=10,
            ),
        )
        local_id, _ = ledger.record_observation(
            local_source,
            _observation(
                digest="b" * 64,
                usage_date="2026-08-10",
                input_tokens=90,
                output_tokens=10,
            ),
        )
        ledger.select_canonical(utc_id, reason="initial UTC attribution")
        before = ledger.lifetime_totals()["total_tokens"]
        ledger.withdraw_canonical(
            provider="claude",
            account_key="acct-primary",
            usage_date="2026-08-11",
            timezone_name="UTC",
            model="claude-adversarial",
            reason="retire UTC cell before local-day reattribution",
        )
        ledger.select_canonical(local_id, reason="corrected local-day attribution")

        corrected = ledger.corrected_rows()
        assert before == 100
        assert ledger.lifetime_totals()["total_tokens"] == before
        assert [(row["usage_date"], row["timezone_name"]) for row in corrected] == [
            ("2026-08-10", "America/New_York")
        ]
        assert ledger.verify_integrity()["status"] == "PASS"


@pytest.mark.parametrize("role", ["legacy_high_water", "manual"])
def test_legacy_and_manual_evidence_remain_noncanonical(
    tmp_path: Path, role: str
) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        observation_id, _ = ledger.record_observation(
            source_id,
            _observation(
                digest="a" * 64,
                role=role,
                eligible=False,
                coverage_state="unknown",
                producer_key=f"{role}-importer",
            ),
        )

        with pytest.raises(LedgerIntegrityError, match="custody evidence"):
            ledger.select_canonical(
                observation_id,
                reason="adversarial attempt to promote noncanonical evidence",
                allow_partial=True,
            )
        assert ledger.corrected_rows() == []
        assert ledger.lifetime_totals()["total_tokens"] == 0


def test_commit_chain_suffix_tampering_is_detected_after_privileged_trigger_drop(
    tmp_path: Path,
) -> None:
    with UsageLedger(tmp_path / "usage.sqlite") as ledger:
        source_id = ledger.register_source(_source())
        observation_id, _ = ledger.record_observation(
            source_id, _observation(digest="a" * 64)
        )
        ledger.select_canonical(observation_id, reason="fixture")
        commit_sequences = [
            row[0]
            for row in ledger.connection.execute(
                "SELECT commit_seq FROM ledger_commit ORDER BY commit_seq"
            )
        ]
        assert len(commit_sequences) >= 3

        ledger.connection.execute("DROP TRIGGER ledger_commit_no_update")
        ledger.connection.execute(
            "UPDATE ledger_commit SET payload_json = ?, commit_hash = ? WHERE commit_seq = ?",
            (
                '{"privileged_attacker":"rewrote_suffix"}',
                "0" * 64,
                commit_sequences[-2],
            ),
        )
        ledger.connection.commit()

        verification = ledger.verify_integrity()
        assert verification["status"] == "FAIL"
        failures = {failure["code"] for failure in verification["failures"]}
        assert "COMMIT_HASH_MISMATCH" in failures
        assert "COMMIT_CHAIN_BREAK" in failures
