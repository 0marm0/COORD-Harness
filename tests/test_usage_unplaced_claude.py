"""Timestamp-less native snapshots only raise retained canonical responses."""
import json
from pathlib import Path

import pytest

from coordharness.usage.local_history import FileParseCounters, discover_local_cli_history, iter_history_records, parse_usage_record
from coordharness.usage.scan_store import UsageScanStore, ScanStoreError


def receipt(identity='known', *, dated=True, amount=10, model='claude-opus-4-8'):
    value={'message':{'id':identity,'model':model,'usage':{'input_tokens':2,'output_tokens':3,
        'cache_read_input_tokens':5,'cache_creation':{'ephemeral_1h_input_tokens':amount}}}}
    if dated:
        value['timestamp']='2026-10-21T12:01:00Z'
    return value


def write(path,*rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))


def total(store):
    return sum(row.total_tokens for row in store.slot_totals('claude'))


@pytest.mark.parametrize('unplaced_first',[False,True])
def test_same_file_source_order_keeps_canonical_owner_and_max(tmp_path,unplaced_first):
    root=tmp_path/'.claude';path=root/'projects/a/source.jsonl'
    dated=receipt();unplaced=receipt(dated=False,amount=200,model='claude-sonnet-5')
    write(path,*([unplaced,dated] if unplaced_first else [dated,unplaced]))
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        result=store.scan(root,provider='claude')
        assert total(store)==210
        assert result.records_raised==(0 if unplaced_first else 1)
        assert result.files_parsed==result.files_seen==1
        rows=store.connection.execute('select usage_slot,model,cache_create_1h_tokens from seen_message').fetchall()
        assert len(rows)==1 and rows[0]['model']=='claude-opus-4-8'
        assert rows[0]['usage_slot']>1700000000
        assert rows[0]['cache_create_1h_tokens']==200
        assert not store.connection.execute('select * from unplaced_claude_receipt').fetchall()
        again=store.scan(root,provider='claude')
        assert again.files_parsed==0 and total(store)==210
        write(path,unplaced)
        store.scan(root,provider='claude')
        assert total(store)==210


def test_new_unplaced_id_never_claims_epoch_or_erases_retained_usage(tmp_path):
    root=tmp_path/'.claude';path=root/'projects/a/source.jsonl'
    write(path,receipt())
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        store.scan(root,provider='claude')
        write(path,receipt('new',dated=False,amount=999))
        result=store.scan(root,provider='claude')
        assert result.records_rejected==1 and total(store)==20
        assert store.connection.execute('select count(*) from seen_message').fetchone()[0]==1
        assert store.connection.execute('select count(*) from unplaced_claude_receipt').fetchone()[0]==1
        pending=store.unplaced_claude_totals()
        assert pending['unplaced_identities']==1 and pending['total_tokens']==1009
        assert pending['charged'] is False and pending['attribution_complete'] is False
        assert store.scan(root,provider='claude').files_parsed==0
        # A later source establishes the date even if the undated source vanished.
        path.unlink()
        write(root/'projects/a/z-canonical.jsonl',receipt('new',amount=10))
        store.scan(root,provider='claude')
        assert total(store)==1029
        assert not store.connection.execute('select * from unplaced_claude_receipt').fetchall()
        assert store.unplaced_claude_totals()['attribution_complete'] is True


@pytest.mark.parametrize('unplaced_first',[False,True])
def test_cross_file_order_raises_original_owner(tmp_path,unplaced_first):
    root=tmp_path/'.claude'
    a=root/'projects/a/a.jsonl';z=root/'projects/a/z.jsonl'
    write(a,receipt(dated=not unplaced_first,amount=200 if unplaced_first else 10))
    write(z,receipt(dated=unplaced_first,amount=10 if unplaced_first else 200))
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        store.scan(root,provider='claude')
        assert total(store)==210
        owner=store.connection.execute('select f.path from seen_message s join scanned_file f using(file_id)').fetchone()[0]
        assert owner==str(z if unplaced_first else a)
        assert store.connection.execute('select sum(request_count) from file_slot').fetchone()[0]==1
        assert store.scan(root,provider='claude').files_parsed==0


def test_discovery_and_default_parser_never_place_missing_or_invalid_timestamp(tmp_path):
    root=tmp_path/'.claude';path=root/'projects/a/source.jsonl'
    unplaced=receipt(dated=False,amount=999)
    assert parse_usage_record('claude',unplaced,'unknown') is None
    write(path,unplaced)
    parsed=list(iter_history_records(path,provider='claude',counters=FileParseCounters()))
    assert len(parsed)==1 and parsed[0].attribution_unknown and parsed[0].usage_slot is None
    found=discover_local_cli_history(root,provider='claude')
    assert not found.rows and not found.slot_rows and found.records_rejected==1
    invalid=receipt();invalid['timestamp']='not-a-date'
    write(path,invalid)
    assert not list(iter_history_records(path,provider='claude',counters=FileParseCounters()))


@pytest.mark.parametrize('contradiction',[False,True])
def test_pending_actual_pricing_metadata_survives_or_rolls_back_conflict(tmp_path,contradiction):
    root=tmp_path/'.claude';path=root/'projects/a/source.jsonl'
    undated=receipt(dated=False,amount=200)
    undated['message']['usage']['speed']='fast'
    write(path,undated)
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        store.scan(root,provider='claude')
        assert total(store)==0 and store.unplaced_claude_totals()['total_tokens']==210
        assert store.connection.execute('select inference_speed from usage_receipt_metadata').fetchone()[0]=='fast'
        canonical=receipt()
        if contradiction:
            canonical['message']['usage']['speed']='standard'
        write(root/'projects/a/dated.jsonl',canonical)
        if contradiction:
            with pytest.raises(ScanStoreError,match='inference_speed'):
                store.scan(root,provider='claude')
            assert total(store)==0 and store.unplaced_claude_totals()['total_tokens']==210
        else:
            result=store.scan(root,provider='claude')
            assert result.files_parsed==1 and total(store)==210
            assert store.unplaced_claude_totals()['unplaced_identities']==0
            assert store.request_pricing_bands('claude')[0]['inference_speed']=='fast'


def test_read_only_old_store_reports_unplaced_coverage_untracked(tmp_path):
    database=tmp_path/'store.sqlite'
    with UsageScanStore(database) as store:
        store.connection.execute('drop table unplaced_claude_receipt')
    with UsageScanStore(database,read_only=True) as store:
        coverage=store.unplaced_claude_totals()
        assert coverage['coverage_tracked'] is False
        assert coverage['attribution_complete'] is False
