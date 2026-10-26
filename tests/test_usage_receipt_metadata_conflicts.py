"""Actual immutable receipt metadata cannot depend on source traversal order."""
import json
from pathlib import Path

import pytest

from coordharness.usage.historical_reconcile import CanonicalReceipt, reconcile_receipts
from coordharness.usage.scan_store import UsageScanStore, ScanStoreError
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.request_pricing import price_band

SLOT=1792670400//900*900

@pytest.mark.parametrize('field,first,second',[
    ('inference_speed','fast','standard'),('inference_speed','standard','fast'),
    ('service_tier','standard','priority'),('service_tier','priority','standard'),
    ('region','global','us'),('region','us','global'),
])
def test_recognized_metadata_conflict_rolls_back_entire_import(tmp_path,field,first,second):
    root=tmp_path/'.claude'
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        initial=CanonicalReceipt(1,SLOT,'claude-opus-4-8',{'input_tokens':100},pricing_metadata={field:first})
        reconcile_receipts(store,[initial],provider='claude',root=root,source_digest='a'*64)
        offered=[CanonicalReceipt(2,SLOT,'claude-opus-4-8',{'input_tokens':500}),
                 CanonicalReceipt(1,SLOT,'claude-opus-4-8',{'input_tokens':200},pricing_metadata={field:second})]
        with pytest.raises(ScanStoreError,match='conflicting recognized receipt metadata'):
            reconcile_receipts(store,offered,provider='claude',root=root,source_digest='b'*64)
        assert store.connection.execute('select count(*) from seen_message').fetchone()[0]==1
        assert store.connection.execute('select input_tokens from seen_message').fetchone()[0]==100
        assert store.connection.execute(f'select {field} from usage_receipt_metadata').fetchone()[0]==first
        assert store.connection.execute('select count(*) from usage_reconciliation_run').fetchone()[0]==1
        assert sum(r.total_tokens for r in store.slot_totals('claude'))==100

@pytest.mark.parametrize('offered',[None,{}, {'inference_speed':'unrecognized'}])
def test_missing_or_unrecognized_metadata_preserves_prior_evidence(tmp_path,offered):
    root=tmp_path/'.claude'
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        for index,metadata in enumerate([{'inference_speed':'fast'},offered]):
            reconcile_receipts(store,[CanonicalReceipt(1,SLOT,'claude-opus-4-8',{'input_tokens':100},pricing_metadata=metadata)],
                               provider='claude',root=root,source_digest=str(index)*64)
        assert store.connection.execute('select inference_speed from usage_receipt_metadata').fetchone()[0]=='fast'


def test_parser_metadata_conflict_rolls_back_whole_source_refresh(tmp_path):
    root=tmp_path/'.claude';path=root/'projects/a/source.jsonl';path.parent.mkdir(parents=True)
    def row(identity,speed):
        return {'timestamp':'2026-10-22T12:00:00Z','message':{'id':identity,'model':'claude-opus-4-8',
            'usage':{'input_tokens':100,'output_tokens':2,'speed':speed}}}
    path.write_text(json.dumps(row('first','fast'))+'\n')
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        store.scan(root,provider='claude')
        path.write_text(json.dumps(row('new-response','standard'))+'\n'+json.dumps(row('first','standard'))+'\n')
        with pytest.raises(ScanStoreError,match='inference_speed'):
            store.scan(root,provider='claude')
        assert store.connection.execute('select count(*) from seen_message').fetchone()[0]==1
        assert sum(r.total_tokens for r in store.slot_totals('claude'))==102
        assert store.connection.execute('select inference_speed from usage_receipt_metadata').fetchone()[0]=='fast'

@pytest.mark.parametrize('region',['us','eu','fedramp'])
def test_actual_region_survives_parser_store_and_regional_price(tmp_path,region):
    root=tmp_path/'.codex';path=root/'sessions/rollout-2026-10-22T12-00-00-11111111-2222-4333-8444-555555555555.jsonl'
    path.parent.mkdir(parents=True)
    rows=[{'timestamp':'2026-10-22T12:00:00Z','type':'turn_context','payload':{'model':'gpt-6.1-sol'},'ordinal':0},
          {'timestamp':'2026-10-22T12:00:00Z','type':'token_usage_record','payload':{'response_id':'native-regional-response','turn_id':'turn-1',
           'usage':{'input_tokens':1000,'output_tokens':10,'cached_input_tokens':0,'service_tier':'default','inference_speed':'standard','region':region}},'ordinal':1}]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    with UsageScanStore(tmp_path/'store.sqlite') as store:
        store.scan(root,provider='codex')
        assert store.connection.execute('select region from usage_receipt_metadata').fetchone()[0]==region
        band=store.request_pricing_bands('codex')[0]
        priced=price_band(band['model'],band,card=load_rate_card(),context_long=band['context_long'],
                          service_tier=band['service_tier'],inference_speed=band['inference_speed'],region=band['region'])
        assert priced['adjusted_numerator']*10==priced['base_numerator']*11
        assert priced['coverage']['unknown_region_requests']==0
