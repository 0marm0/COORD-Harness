from pathlib import Path
from types import SimpleNamespace
import json

from coordharness.usage import scan_store as module


def test_streaming_scan_accepts_large_transcript_and_explicit_budget_still_works(tmp_path, monkeypatch):
    root = tmp_path / ".claude"
    path = root / "projects" / "sample" / "large.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"timestamp": "2026-10-12T12:00:00Z", "message": {
        "id": "message-1", "model": "claude-sonnet-5", "usage": {
            "input_tokens": 100, "output_tokens": 5}}}) + "\n")
    actual_stat = Path.stat
    def reported_stat(self, *args, **kwargs):
        result = actual_stat(self, *args, **kwargs)
        if self == path:
            return SimpleNamespace(st_size=11 * 1024**3, st_mtime_ns=result.st_mtime_ns,
                                   st_mode=result.st_mode)
        return result
    monkeypatch.setattr(Path, "stat", reported_stat)
    with module.UsageScanStore(tmp_path / "scan.sqlite") as store:
        limited = store.scan(root, provider="claude", max_file_bytes=8 * 1024**3)
        assert limited.files_oversized == 1
        assert limited.files_parsed == 0
        complete = store.scan(root, provider="claude")
        assert complete.files_oversized == 0
        assert complete.files_parsed == 1
        assert sum(row.total_tokens for row in store.slot_totals("claude")) == 105
        repeated = store.scan(root, provider="claude")
        assert repeated.files_unchanged == 1
