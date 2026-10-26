from pathlib import Path
import json
import os
import sqlite3

from coordharness.usage.scan_store import UsageScanStore


def write(path, outputs, unidentified=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows=[]
    for output in outputs:
        message={"model":"claude-sonnet-5-5", "usage":{"input_tokens":100,"output_tokens":output}}
        if not unidentified: message["id"]="one-paid-response"
        rows.append({"timestamp":"2026-10-22T12:00:00Z","message":message})
    path.write_text("".join(json.dumps(row)+"\n" for row in rows))
    st=path.stat()
    os.utime(path, ns=(st.st_atime_ns,st.st_mtime_ns+1_000_000_000))


def totals(store):
    return tuple(sum(getattr(row,col) for row in store.slot_totals("claude"))
                 for col in ["input_tokens","output_tokens","request_count"])


def test_owner_reparse_preserves_a_larger_receipt_from_unchanged_peer(tmp_path):
    root=tmp_path/".claude"
    owner=root/"projects/a/owner.jsonl"
    peer=root/"projects/b/peer.jsonl"
    write(owner,[2]); write(peer,[286])
    with UsageScanStore(tmp_path/"scan.sqlite") as store:
        store.scan(root,provider="claude")
        assert totals(store)==(100,286,1)
        write(owner,[2,2])
        store.scan(root,provider="claude")
        assert totals(store)==(100,286,1)
        peer.unlink()
        write(owner,[2])
        store.scan(root,provider="claude")
        assert totals(store)==(100,286,1)
        owner.write_text("")
        store.scan(root,provider="claude")
        assert totals(store)==(100,286,1)


def test_anonymous_receipts_are_preserved_separately_without_rescan_duplication(tmp_path):
    root=tmp_path/".claude"; path=root/"projects/a/anonymous.jsonl"
    write(path,[10,20],unidentified=True)
    with UsageScanStore(tmp_path/"scan.sqlite") as store:
        store.scan(root,provider="claude")
        assert totals(store)==(200,30,2)
        write(path,[10,20],unidentified=True)
        store.scan(root,provider="claude")
        assert totals(store)==(200,30,2)
        path.write_text(""); store.scan(root,provider="claude")
        assert totals(store)==(200,30,2)


def test_preextension_anonymous_aggregate_bootstraps_without_adding_it_twice(tmp_path):
    root=tmp_path/".claude"; path=root/"projects/a/anonymous.jsonl"
    write(path,[10],unidentified=True)
    db=tmp_path/"scan.sqlite"
    with UsageScanStore(db) as store: store.scan(root,provider="claude")
    with sqlite3.connect(db) as conn: conn.execute("DROP TABLE file_unidentified_slot")
    write(path,[10,20],unidentified=True)
    with UsageScanStore(db) as store:
        store.scan(root,provider="claude")
        assert totals(store)==(200,30,2)
        store.rebuild(root,provider="claude")
        assert totals(store)==(200,30,2)


def test_identity_disappearing_cannot_double_previously_identified_usage(tmp_path):
    from coordharness.usage.scan_store import ScanStoreError
    import pytest
    root=tmp_path/".claude"; path=root/"projects/a/receipt.jsonl"
    write(path,[10])
    with UsageScanStore(tmp_path/"scan.sqlite") as store:
        store.scan(root,provider="claude")
        write(path,[10],unidentified=True)
        with pytest.raises(ScanStoreError,match="anonymous receipts may overlap"):
            store.scan(root,provider="claude")
        assert totals(store)==(100,10,1)


def test_anonymous_receipt_becoming_identified_needs_overlap_proof(tmp_path):
    from coordharness.usage.scan_store import ScanStoreError
    import pytest
    root=tmp_path/".claude"; path=root/"projects/a/receipt.jsonl"
    write(path,[10],unidentified=True)
    with UsageScanStore(tmp_path/"scan.sqlite") as store:
        store.scan(root,provider="claude")
        write(path,[10])
        with pytest.raises(ScanStoreError,match="anonymous and newly identified"):
            store.scan(root,provider="claude")
        assert totals(store)==(100,10,1)


def test_source_parse_counters_do_not_include_retained_but_unread_requests(tmp_path):
    root=tmp_path/".claude"; path=root/"projects/a/receipt.jsonl"
    write(path,[10])
    with UsageScanStore(tmp_path/"scan.sqlite") as store:
        store.scan(root,provider="claude")
        path.write_text("")
        result=store.scan(root,provider="claude")
        assert result.records_scanned==result.records_accepted==result.records_deduplicated==0
        assert totals(store)==(100,10,1)


def test_historical_receipts_merge_once_and_survive_owner_refresh(tmp_path):
    from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
    from coordharness.usage.local_history import fingerprint_identity
    root=tmp_path/".claude"; path=root/"projects/a/source.jsonl"
    write(path,[2])
    with UsageScanStore(tmp_path/"scan.sqlite") as store:
        store.scan(root,provider="claude")
        held=store.connection.execute("SELECT * FROM seen_message").fetchone()
        receipt=CanonicalReceipt(held['fingerprint'],held['usage_slot'],held['model'],
                                 {'input_tokens':100,'output_tokens':286},
                                 pricing_metadata={'service_tier':'standard','region':'global'})
        first=reconcile_receipts(store,[receipt],provider='claude',root=root,source_digest='a'*64)
        assert first.raised==1 and first.added_tokens==284
        second=reconcile_receipts(store,[receipt],provider='claude',root=root,source_digest='a'*64)
        assert second.inserted==second.raised==second.added_tokens==0
        write(path,[2,2]); store.scan(root,provider='claude')
        assert totals(store)==(100,286,1)
        assert store.connection.execute('SELECT service_tier FROM usage_receipt_metadata').fetchone()[0]=='standard'


def test_modern_and_legacy_receipt_aliases_never_add_a_second_request(tmp_path):
    from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
    from coordharness.usage.local_history import fingerprint_identity
    root=tmp_path/'.codex'
    slot=1761048000//900*900
    old=fingerprint_identity('t:thread:1'); modern=fingerprint_identity('codex-response:response-1')
    for primary,alias in [(old,modern),(modern,old)]:
        with UsageScanStore(tmp_path/f'{primary}.sqlite') as store:
            first=CanonicalReceipt(primary,slot,'gpt-6.1-sol',{'input_tokens':100,'output_tokens':5})
            reconcile_receipts(store,[first],provider='codex',root=root,source_digest='b'*64)
            second=CanonicalReceipt(modern,slot,'gpt-6.1-sol',first.metrics,alias_fingerprints=(old,))
            repeat=reconcile_receipts(store,[second],provider='codex',root=root,source_digest='c'*64)
            assert repeat.inserted==repeat.added_tokens==0
            assert sum(row.total_tokens for row in store.slot_totals('codex'))==105
            assert store.connection.execute('SELECT COUNT(*) FROM seen_message').fetchone()[0]==1
            assert store._resolve_fingerprints(store.connection,'codex',old)==store._resolve_fingerprints(store.connection,'codex',modern)


def test_a_partial_new_receipt_does_not_replace_or_duplicate_a_legacy_day(tmp_path):
    from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
    from coordharness.usage.local_history import DailyUsage
    root=tmp_path/'.codex'; slot=1761048000//900*900
    with UsageScanStore(tmp_path/'scan.sqlite') as store:
        from datetime import datetime,timezone
        day=datetime.fromtimestamp(slot,timezone.utc).date().isoformat()
        store.import_legacy('codex',source='fixture',rows=[DailyUsage(day,'gpt-5.6-sol',input_tokens=1000)])
        receipt=CanonicalReceipt(1,slot,'gpt-5.6-sol',{'input_tokens':100})
        result=reconcile_receipts(store,[receipt],provider='codex',root=root,source_digest='d'*64)
        assert result.inserted==result.added_tokens==0
        assert result.legacy_covered_tokens==100
        assert len(store.legacy_totals('codex'))==1
        assert store.slot_totals('codex')==()


def test_invalid_receipt_rolls_back_the_entire_historical_import(tmp_path):
    from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
    import pytest
    root=tmp_path/'.claude'; slot=1761048000//900*900
    with UsageScanStore(tmp_path/'scan.sqlite') as store:
        receipts=[CanonicalReceipt(1,slot,'claude-sonnet-5-5',{'input_tokens':100}),
                  CanonicalReceipt(2,slot,'claude-sonnet-5-5',{'input_tokens':-1})]
        with pytest.raises(ValueError,match='canonical token'):
            reconcile_receipts(store,receipts,provider='claude',root=root,source_digest='e'*64)
        assert store.connection.execute('SELECT COUNT(*) FROM seen_message').fetchone()[0]==0
