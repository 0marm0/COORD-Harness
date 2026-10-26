"""Modern request identities conserve old mirror ownership and EOF receipts."""
import json
from pathlib import Path
import pytest

from coordharness.usage.local_history import FileParseCounters, iter_history_records, discover_local_cli_history

STAMP = "2026-10-22T12:00:00Z"

def event(kind, payload, ordinal):
    return {"timestamp":STAMP, "type":kind, "payload":payload, "ordinal":ordinal}

def modern(ordinal, response="resp-1", **usage):
    return event("token_usage_record", {"response_id":response,"turn_id":"turn-1","usage":{
        "input_tokens":100,"cached_input_tokens":80,"output_tokens":9,**usage}}, ordinal)

def legacy(ordinal, zero=False, running=100):
    return event("event_msg", {"type":"token_count","info":{
        "last_token_usage":{"input_tokens":0 if zero else 100,"cached_input_tokens":0 if zero else 80,"output_tokens":0 if zero else 9},
        "total_token_usage":{"input_tokens":running,"cached_input_tokens":80,"output_tokens":9}}}, ordinal)

def read(tmp_path, records, provider="codex"):
    path=tmp_path/"rollout-2026-10-22T12-00-00-11111111-2222-4333-8444-555555555555.jsonl"
    path.write_text("".join(json.dumps(r)+"\n" for r in records))
    return list(iter_history_records(path,provider=provider,counters=FileParseCounters()))

def test_positive_modern_receipt_recovers_zeroed_legacy_and_keeps_alias(tmp_path):
    records=read(tmp_path,[legacy(1),modern(2),legacy(7,zero=True)])
    assert len(records)==2
    assert records[1].identity=="codex-response:resp-1"
    assert records[1].alias_identities==("t:11111111-2222-4333-8444-555555555555:7",)
    assert sum(records[1].metrics.values())==109

def test_mirrored_modern_is_one_request_with_legacy_alias(tmp_path):
    records=read(tmp_path,[modern(1),legacy(2)])
    assert len(records)==1
    assert records[0].identity=="codex-response:resp-1"
    assert records[0].alias_identities==("t:11111111-2222-4333-8444-555555555555:2",)

def test_eof_identity_stays_stable_when_legacy_is_appended(tmp_path):
    at_eof=read(tmp_path,[modern(1)])[0]
    appended=read(tmp_path,[modern(1),legacy(7)])[0]
    assert at_eof.identity==appended.identity=="codex-response:resp-1"
    assert at_eof.alias_identities==()
    assert len(appended.alias_identities)==1

def test_modern_records_without_a_legacy_mirror_each_keep_own_response_id(tmp_path):
    rows=read(tmp_path,[modern(1),modern(2,response="resp-2")])
    assert [r.identity for r in rows]==["codex-response:resp-1","codex-response:resp-2"]

def test_actual_modern_receipt_pricing_metadata_only(tmp_path):
    rows=read(tmp_path,[modern(1,service_tier="priority",inference_speed="fast",region="us"),legacy(2)])
    assert rows[0].pricing_metadata=={"service_tier":"priority","inference_speed":"fast","region":"us"}
    assert read(tmp_path,[modern(1),legacy(2)])[0].pricing_metadata is None

def test_claude_actual_receipt_fields_are_normalized(tmp_path):
    rows=read(tmp_path,[{"timestamp":STAMP,"message":{"id":"m1","model":"claude-opus-4-8","usage":{
        "input_tokens":100,"output_tokens":9,"service_tier":"standard","speed":"fast","inference_geo":"global"}}}],provider="claude")
    assert rows[0].pricing_metadata=={"service_tier":"standard","inference_speed":"fast","region":"global"}

def test_compaction_snapshot_is_not_an_additional_request(tmp_path):
    receipt=modern(1)
    rows=read(tmp_path,[receipt,event("compacted",{"latest_token_usage_record":receipt["payload"]},2),legacy(3,zero=True)])
    assert len(rows)==1 and rows[0].identity=="codex-response:resp-1"

def test_zero_components_are_a_valid_receipt(tmp_path):
    rows=read(tmp_path,[modern(1,input_tokens=0,cached_input_tokens=0,output_tokens=0),legacy(2,zero=True)])
    assert len(rows)==1 and sum(rows[0].metrics.values())==0

def test_archived_zstd_receipt_stream_and_discovery(tmp_path):
    path=tmp_path/"archived_sessions"/"rollout-2026-10-22T12-00-00-11111111-2222-4333-8444-555555555555.jsonl.zst"
    path.parent.mkdir()
    data="".join(json.dumps(r)+"\n" for r in [modern(1),legacy(2)]).encode()
    try:
        from compression import zstd
    except ImportError:
        try:
            import zstandard
        except ImportError:
            path.write_bytes(b"decoder absence is tested before reading compressed content")
            with pytest.raises(OSError,match="zstd history decoder unavailable"):
                list(iter_history_records(path,provider="codex",counters=FileParseCounters()))
            return
        compressed=zstandard.ZstdCompressor().compress(data)
    else:
        compressed=zstd.compress(data)
    path.write_bytes(compressed)
    rows=list(iter_history_records(path,provider="codex",counters=FileParseCounters()))
    assert len(rows)==1 and rows[0].identity=="codex-response:resp-1"
    imported=discover_local_cli_history(tmp_path,provider="codex")
    assert imported.files_scanned==1 and imported.records_accepted==1
    assert sum(row.input_tokens+row.output_tokens+row.cache_read_tokens for row in imported.rows)==109
