"""Qualified Codex native timestamps move existing ownership conservatively."""

from datetime import datetime, timezone
import pytest

from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
from coordharness.usage.scan_store import UsageScanStore, ScanStoreError

OLD = 1792584000 // 900 * 900
NEW = OLD + 86400
MODEL = "gpt-6.1-sol"
METRICS = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_tokens": 30,
    "cache_create_5m_tokens": 4,
    "cache_create_1h_tokens": 5,
    "cache_create_other_tokens": 6,
}


def day(slot):
    return datetime.fromtimestamp(slot, timezone.utc).date().isoformat()


def apply(store, receipts, root, provider="codex", digest="a"):
    return reconcile_receipts(
        store, receipts, provider=provider, root=root, source_digest=digest * 64
    )


def seed(store, root, provider="codex", model=MODEL):
    apply(store, [CanonicalReceipt(1, OLD, model, METRICS)], root, provider)


def test_rebase_moves_retained_components_one_request_and_adds_only_positive_counts(tmp_path):
    root = tmp_path / ".codex"
    offered = {
        "input_tokens": 110,
        "output_tokens": 15,
        "cache_read_tokens": 35,
        "cache_create_5m_tokens": 3,
        "cache_create_1h_tokens": 6,
        "cache_create_other_tokens": 2,
    }
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root)
        receipt = CanonicalReceipt(1, NEW, MODEL, offered, rebase_attribution=True)
        result = apply(store, [receipt], root, digest="b")
        assert result.rebased == 1 and result.rebased_tokens == 165
        assert result.added_tokens == 16 and result.raised == 1
        assert result.attribution_token_delta_by_utc_day_model == {
            (day(OLD), MODEL): -165,
            (day(NEW), MODEL): 165,
        }
        assert result.attribution_request_delta_by_utc_day_model == {
            (day(OLD), MODEL): -1,
            (day(NEW), MODEL): 1,
        }
        assert sum(result.attribution_token_delta_by_utc_day_model.values()) == 0
        assert sum(result.attribution_request_delta_by_utc_day_model.values()) == 0
        assert result.added_by_utc_day_model == {(day(NEW), MODEL): 16}
        assert store.connection.execute("select usage_slot from seen_message").fetchone()[0] == NEW
        rows = store.connection.execute("select * from file_slot").fetchall()
        assert len(rows) == 1 and rows[0]["usage_slot"] == NEW and rows[0]["request_count"] == 1
        assert tuple(rows[0][n] for n in METRICS) == (110, 20, 35, 4, 6, 6)
        again = apply(store, [receipt], root, digest="c")
        assert again.rebased == again.raised == again.inserted == again.added_tokens == 0
        assert sum(r.total_tokens for r in store.slot_totals("codex")) == 181


def test_default_receipt_preserves_old_bucket_without_explicit_audit_flag(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root)
        result = apply(store, [CanonicalReceipt(1, NEW, MODEL, METRICS)], root, digest="b")
        assert result.rebased == 0 and result.slot_conflicts_preserved == 1
        assert store.connection.execute("select usage_slot from seen_message").fetchone()[0] == OLD


def test_claude_wrapper_timestamp_cannot_opt_into_rebase(tmp_path):
    root = tmp_path / ".claude"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root, provider="claude", model="claude-opus-4-8")
        with pytest.raises(ScanStoreError, match="only audited Codex"):
            apply(
                store,
                [CanonicalReceipt(1, NEW, "claude-opus-4-8", METRICS, rebase_attribution=True)],
                root,
                provider="claude",
                digest="b",
            )
        assert store.connection.execute("select usage_slot from seen_message").fetchone()[0] == OLD
        assert sum(r.total_tokens for r in store.slot_totals("claude")) == 165


def test_rebase_refuses_model_change_and_rolls_back_other_receipts(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root)
        rows = [
            CanonicalReceipt(2, NEW, MODEL, {"input_tokens": 10}),
            CanonicalReceipt(1, NEW, "gpt-6-sol", METRICS, rebase_attribution=True),
        ]
        with pytest.raises(ScanStoreError, match="cannot change the retained model"):
            apply(store, rows, root, digest="b")
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 1
        assert store.connection.execute("select usage_slot from seen_message").fetchone()[0] == OLD


@pytest.mark.parametrize("column,broken", [("input_tokens", 99), ("request_count", 0)])
def test_rebase_refuses_inexact_owner_subtraction(tmp_path, column, broken):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root)
        store.connection.execute(f"update file_slot set {column}=?", (broken,))
        store.connection.commit()
        with pytest.raises(ScanStoreError, match="cannot be subtracted exactly"):
            apply(
                store,
                [CanonicalReceipt(1, NEW, MODEL, METRICS, rebase_attribution=True)],
                root,
                digest="b",
            )
        assert store.connection.execute("select usage_slot from seen_message").fetchone()[0] == OLD
        assert store.connection.execute(f"select {column} from file_slot").fetchone()[0] == broken


def test_multiple_receipts_move_between_shared_buckets_without_consuming_other_ownership(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        apply(
            store,
            [
                CanonicalReceipt(1, OLD, MODEL, {"input_tokens": 100}),
                CanonicalReceipt(2, OLD, MODEL, {"input_tokens": 50}),
                CanonicalReceipt(3, NEW, MODEL, {"input_tokens": 25}),
            ],
            root,
        )
        result = apply(
            store,
            [
                CanonicalReceipt(1, NEW, MODEL, {"input_tokens": 110}, rebase_attribution=True),
                CanonicalReceipt(2, NEW, MODEL, {"input_tokens": 50}, rebase_attribution=True),
            ],
            root,
            digest="b",
        )
        assert result.rebased == 2 and result.rebased_tokens == 150 and result.added_tokens == 10
        rows = store.connection.execute("select * from file_slot").fetchall()
        assert len(rows) == 1 and rows[0]["input_tokens"] == 185 and rows[0]["request_count"] == 3


def test_same_import_can_insert_then_rebase_one_identity(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        result = apply(
            store,
            [
                CanonicalReceipt(1, OLD, MODEL, {"input_tokens": 100}),
                CanonicalReceipt(1, NEW, MODEL, {"input_tokens": 110}, rebase_attribution=True),
            ],
            root,
        )
        assert result.inserted == result.rebased == 1 and result.added_tokens == 110
        row = store.connection.execute("select * from file_slot").fetchone()
        assert row["usage_slot"] == NEW and row["input_tokens"] == 110 and row["request_count"] == 1


def test_rebase_flag_requires_an_actual_boolean(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        with pytest.raises(ValueError, match="explicit boolean"):
            apply(store, [CanonicalReceipt(1, OLD, MODEL, METRICS, rebase_attribution=1)], root)
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 0


def test_modern_alias_rebases_retained_legacy_identity_without_second_request(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root)
        receipt = CanonicalReceipt(
            2, NEW, MODEL, METRICS, alias_fingerprints=(1,), rebase_attribution=True
        )
        result = apply(store, [receipt], root, digest="b")
        assert result.rebased == 1 and result.inserted == result.added_tokens == 0
        row = store.connection.execute("select fingerprint,usage_slot from seen_message").fetchone()
        assert tuple(row) == (1, NEW)
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 1
        assert store._resolve_fingerprints(store.connection, "codex", 2) == 1
        bucket = store.connection.execute("select request_count from file_slot").fetchone()
        assert bucket[0] == 1


def test_zero_token_request_still_moves_exactly_one_request(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        apply(store, [CanonicalReceipt(1, OLD, MODEL, {})], root)
        result = apply(
            store, [CanonicalReceipt(1, NEW, MODEL, {}, rebase_attribution=True)], root, digest="b"
        )
        assert result.rebased == 1 and result.rebased_tokens == result.added_tokens == 0
        assert result.attribution_request_delta_by_utc_day_model == {
            (day(OLD), MODEL): -1,
            (day(NEW), MODEL): 1,
        }
        rows = store.connection.execute("select usage_slot,request_count from file_slot").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (NEW, 1)
