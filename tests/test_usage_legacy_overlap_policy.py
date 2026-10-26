"""Partial archive receipts cannot suppress a whole frozen historical day."""

import json
from datetime import datetime, timezone
import pytest

from coordharness.usage.local_history import fingerprint_identity
from coordharness.usage.ledger import DailyUsage
from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
from coordharness.usage.scan_store import UsageScanStore, ScanStoreError

SLOT = 1792584000 // 900 * 900
DAY = datetime.fromtimestamp(SLOT, timezone.utc).date().isoformat()
STAMP = datetime.fromtimestamp(SLOT, timezone.utc).isoformat()
MODEL = "gpt-6.1-sol"


def seed(store, root, receipts):
    return reconcile_receipts(store, receipts, provider="codex", root=root, source_digest="a" * 64)


def legacy(store, provider="codex"):
    store.import_legacy(
        provider, source="frozen-fixture", rows=[DailyUsage(DAY, MODEL, input_tokens=10000)]
    )


def native_rows(response="partial", amount=100):
    return [
        {"timestamp": STAMP, "type": "turn_context", "payload": {"model": MODEL}, "ordinal": 0},
        {
            "timestamp": STAMP,
            "type": "token_usage_record",
            "payload": {
                "response_id": response,
                "turn_id": "t",
                "usage": {"input_tokens": amount, "output_tokens": 0, "cached_input_tokens": 0},
            },
            "ordinal": 1,
        },
    ]


def test_normal_compressed_archive_scan_rejects_new_legacy_day_identity(tmp_path):
    zstd = pytest.importorskip("zstandard")
    root = tmp_path / ".codex"
    path = (
        root
        / "archived_sessions/rollout-2026-10-21T12-00-00-11111111-2222-4333-8444-555555555555.jsonl.zst"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(
        zstd.ZstdCompressor().compress(
            "".join(json.dumps(r) + "\n" for r in native_rows()).encode()
        )
    )
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        legacy(store)
        scanned = store.scan(root, provider="codex")
        assert scanned.records_accepted == 0 and scanned.records_rejected >= 1
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 0
        assert store.slot_totals("codex") == ()
        assert sum(r.input_tokens for r in store.legacy_totals("codex")) == 10000


def test_sixteen_retained_partial_identities_are_hidden_without_deleting_ownership(tmp_path):
    root = tmp_path / ".codex"
    db = tmp_path / "store.sqlite"
    with UsageScanStore(db) as store:
        seed(
            store,
            root,
            [CanonicalReceipt(i, SLOT, MODEL, {"input_tokens": 100}) for i in range(1, 17)],
        )
        legacy(store)
        assert store.legacy_totals("codex") == ()
        assert sum(r.total_tokens for r in store.slot_totals("codex")) == 1600
        assert (
            store.mark_legacy_overlap_days(
                "codex", [DAY], reason="exact sixteen-receipt partial archive proof"
            )
            == 1
        )
        assert store.slot_totals("codex") == ()
        assert store.request_pricing_bands("codex") == ()
        assert sum(r.input_tokens for r in store.legacy_totals("codex")) == 10000
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 16
        assert store.connection.execute(
            "select sum(input_tokens),sum(request_count) from file_slot"
        ).fetchone()[:] == (1600, 16)
    with UsageScanStore(db, read_only=True) as store:
        assert store.slot_totals("codex") == ()
        assert sum(r.input_tokens for r in store.legacy_totals("codex")) == 10000
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 16


def test_existing_legacy_day_identity_can_raise_while_new_identity_stays_withheld(tmp_path):
    root = tmp_path / ".codex"
    path = root / "sessions/rollout-2026-10-21T12-00-00-11111111-2222-4333-8444-555555555555.jsonl"
    path.parent.mkdir(parents=True)
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(
            store,
            root,
            [
                CanonicalReceipt(
                    fingerprint_identity("codex-response:existing"),
                    SLOT,
                    MODEL,
                    {"input_tokens": 100},
                )
            ],
        )
        legacy(store)
        store.mark_legacy_overlap_days("codex", [DAY], reason="fixture explicit partial overlap")
        path.write_text(
            "".join(
                json.dumps(r) + "\n" for r in native_rows("existing", 150) + native_rows("new", 200)
            )
        )
        store.scan(root, provider="codex")
        assert store.connection.execute("select count(*) from seen_message").fetchone()[0] == 1
        assert (
            store.connection.execute("select input_tokens from seen_message").fetchone()[0] == 150
        )
        assert store.slot_totals("codex") == ()
        assert sum(r.input_tokens for r in store.legacy_totals("codex")) == 10000


def test_flagging_requires_frozen_source_and_rolls_back_all_requested_days(tmp_path):
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        legacy(store)
        missing = datetime.fromtimestamp(SLOT + 86400, timezone.utc).date().isoformat()
        with pytest.raises(ScanStoreError, match="no frozen source"):
            store.mark_legacy_overlap_days(
                "codex", [DAY, missing], reason="not a complete qualification"
            )
        assert (
            store.connection.execute("select count(*) from legacy_overlap_day").fetchone()[0] == 0
        )


def test_read_only_old_schema5_store_without_policy_table_keeps_reader_compatibility(tmp_path):
    root = tmp_path / ".codex"
    db = tmp_path / "store.sqlite"
    with UsageScanStore(db) as store:
        seed(store, root, [CanonicalReceipt(1, SLOT, MODEL, {"input_tokens": 100})])
        legacy(store)
        store.connection.execute("drop table legacy_overlap_day")
    with UsageScanStore(db, read_only=True) as store:
        assert sum(r.total_tokens for r in store.slot_totals("codex")) == 100
        assert store.legacy_totals("codex") == ()
        assert len(store.request_pricing_bands("codex")) == 1
        assert (
            store.connection.execute(
                "select 1 from sqlite_master where name='legacy_overlap_day'"
            ).fetchone()
            is None
        )


def test_withdrawing_frozen_source_reveals_preserved_partial_receipts(tmp_path):
    root = tmp_path / ".codex"
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        seed(store, root, [CanonicalReceipt(1, SLOT, MODEL, {"input_tokens": 100})])
        legacy(store)
        store.mark_legacy_overlap_days("codex", [DAY], reason="partial source")
        store.drop_legacy("codex")
        assert sum(r.total_tokens for r in store.slot_totals("codex")) == 100
        assert len(store.request_pricing_bands("codex")) == 1


def test_anonymous_normal_scan_cannot_replace_a_frozen_legacy_day(tmp_path):
    root = tmp_path / ".claude"
    path = root / "projects/a/anonymous.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": STAMP,
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 2},
                },
            }
        )
        + "\n"
    )
    with UsageScanStore(tmp_path / "store.sqlite") as store:
        legacy(store, provider="claude")
        store.scan(root, provider="claude")
        assert store.slot_totals("claude") == ()
        assert sum(r.input_tokens for r in store.legacy_totals("claude")) == 10000
        assert (
            store.connection.execute("select count(*) from file_unidentified_slot").fetchone()[0]
            == 0
        )
