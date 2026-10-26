"""Public pending counters cannot leak identities or enter charged totals."""

from datetime import datetime, timezone
import json
from coordharness.usage.scan_store import UsageScanStore
from coordharness.usage.pricing import load_rate_card
from coordharness.usage.local_service import _history
from coordharness.usage.dashboard_proxy import _sanitize_history, UsageDashboardError
import pytest


def test_pending_only_history_is_visible_and_privacy_bounded(tmp_path):
    root = tmp_path / ".claude"
    source = root / "projects/a/source.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "message": {
                    "id": "pending-native-response",
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 100, "output_tokens": 10, "speed": "fast"},
                }
            }
        )
        + "\n"
    )
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="claude")
        imported = store.history_import(root, provider="claude", tz=timezone.utc)
        assert imported is not None and imported.rows == ()
        history = _history(
            imported, datetime(2026, 10, 22, tzinfo=timezone.utc), card=load_rate_card()
        )
        history["unplaced_usage"]["private_identity"] = "secret-response-id"
        clean = _sanitize_history(history)
        assert clean["unplaced_usage"]["total_tokens"] == 110
        assert clean["unplaced_usage"]["charged"] is False
        assert "secret-response-id" not in json.dumps(clean)
        assert clean["daily"] == []
        history["unplaced_usage"]["charged"] = True
        with pytest.raises(UsageDashboardError):
            _sanitize_history(history)
