"""The scan store keeps UTC time slots and derives calendar days when it serves.

What has to hold, because each one failed when a machine's timezone changed
while its store was in use:

* a scan under any timezone writes the identical store;
* a timezone change needs no rescan -- the next read answers in the new zone;
* the day a record lands on is exact in fractional-offset zones too;
* a pre-slot (schema 4) store is migrated by discarding its day-bucketed rows,
  keeping its frozen legacy rows, and re-parsing on the next scan;
* a legacy day is superseded by measured usage on the same UTC day, never on a
  zone-dependent local day.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

import pytest

from coordharness.usage.ledger import DailyUsage
from coordharness.usage.local_history import (
    SLOT_SECONDS,
    SlotUsage,
    day_slots,
    discover_local_cli_history,
    rollup_days,
    slot_day,
)
from coordharness.usage.scan_store import SCHEMA_VERSION, UsageScanStore

pytestmark = pytest.mark.unit


@pytest.fixture()
def system_zone() -> Iterator[callable]:
    """Switch the PROCESS timezone the way a machine move does, then restore it."""

    before = os.environ.get("TZ")

    def switch(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield switch
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _claude(message_id: str, stamp: str, output: int = 10) -> dict:
    return {
        "timestamp": stamp,
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "usage": {"input_tokens": 100, "output_tokens": output},
        },
    }


def _home(tmp_path: Path) -> Path:
    root = tmp_path / "home" / ".claude"
    _write(
        root / "projects" / "a" / "one.jsonl",
        [
            # 02:30Z is still the previous evening in New York.
            _claude("m-1", "2026-09-22T02:30:00Z", output=1),
            # 18:14Z is 23:59 in Kathmandu (+05:45); 18:15Z is its next midnight.
            _claude("m-2", "2026-09-22T18:14:59Z", output=2),
            _claude("m-3", "2026-09-22T18:15:00Z", output=4),
        ],
    )
    return root


def _dump(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT f.path, d.usage_slot, d.model, d.output_tokens, d.request_count "
            "FROM file_slot AS d JOIN scanned_file AS f USING(file_id) ORDER BY 1, 2, 3"
        ).fetchall()
    finally:
        conn.close()


def test_a_scan_under_any_timezone_writes_the_identical_store(
    tmp_path: Path, system_zone
) -> None:
    root = _home(tmp_path)
    dumps = []
    for zone in ("America/New_York", "Europe/Berlin", "Asia/Kathmandu", "UTC"):
        system_zone(zone)
        path = tmp_path / f"{zone.replace('/', '_')}.sqlite"
        with UsageScanStore(path) as store:
            store.scan(root, provider="claude")
        dumps.append(_dump(path))

    assert all(dump == dumps[0] for dump in dumps)
    assert all(slot % SLOT_SECONDS == 0 for _path, slot, *_ in dumps[0])


def test_a_timezone_change_needs_no_rescan(tmp_path: Path, system_zone) -> None:
    root = _home(tmp_path)
    system_zone("America/New_York")
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="claude")
        new_york = {row.usage_date: row.output_tokens for row in store.totals("claude")}

        system_zone("Europe/Berlin")
        rescan = store.scan(root, provider="claude")
        berlin = {row.usage_date: row.output_tokens for row in store.totals("claude")}

    assert rescan.files_parsed == 0
    assert new_york == {"2026-09-21": 1, "2026-09-22": 6}
    assert berlin == {"2026-09-22": 7}


def test_days_are_exact_in_a_quarter_hour_offset_zone(tmp_path: Path) -> None:
    root = _home(tmp_path)
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="claude")
        kathmandu = store.totals("claude", tz=ZoneInfo("Asia/Kathmandu"))
        utc = store.totals("claude", tz=timezone.utc)

    assert {row.usage_date: row.output_tokens for row in kathmandu} == {
        "2026-09-22": 1 + 2,
        "2026-09-23": 4,
    }
    assert {row.usage_date: row.output_tokens for row in utc} == {"2026-09-22": 7}


def test_the_live_bounded_scan_derives_the_same_days(tmp_path: Path) -> None:
    root = _home(tmp_path)
    zone = ZoneInfo("America/New_York")
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="claude")
        stored = store.totals("claude", tz=zone)
    live = discover_local_cli_history(root, provider="claude", tz=zone)

    assert live.rows == stored
    assert sum(row.total_tokens for row in live.slot_rows) == sum(
        row.input_tokens + row.output_tokens for row in stored
    )


def test_rollup_carries_every_component_and_request_count() -> None:
    rows = (
        SlotUsage(usage_slot=0, model="m", input_tokens=1, cache_read_tokens=2, request_count=1),
        SlotUsage(usage_slot=900, model="m", output_tokens=3, cache_create_1h_tokens=4,
                  request_count=2),
    )
    assert rollup_days(rows, timezone.utc) == (
        DailyUsage(
            usage_date="1970-01-01",
            model="m",
            input_tokens=1,
            output_tokens=3,
            cache_read_tokens=2,
            cache_create_1h_tokens=4,
            request_count=3,
        ),
    )


def test_day_slots_cover_a_dst_day_exactly() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    # 2026-10-25 is the autumn change in Berlin: 25 hours long.
    slots = day_slots("2026-10-25", berlin)
    assert len(slots) == 25 * 4
    assert {slot_day(slot, berlin) for slot in slots} == {"2026-10-25"}
    assert len(day_slots("2026-09-22", timezone.utc)) == 96


def test_a_timestamp_without_an_offset_is_refused(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    _write(root / "projects" / "naive.jsonl", [_claude("m-1", "2026-09-22T12:00:00")])
    assert discover_local_cli_history(root, provider="claude").rows == ()


# -- migration --------------------------------------------------------------


_SCHEMA_4 = """
CREATE TABLE scanned_file(
    file_id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, provider TEXT NOT NULL,
    root_digest TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    scanned_at TEXT NOT NULL, records_scanned INTEGER NOT NULL,
    records_accepted INTEGER NOT NULL, records_rejected INTEGER NOT NULL,
    records_deduplicated INTEGER NOT NULL, records_unidentified INTEGER NOT NULL,
    parse_error_count INTEGER NOT NULL) STRICT;
CREATE TABLE file_daily(
    file_id INTEGER NOT NULL, usage_date TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, cache_create_5m_tokens INTEGER NOT NULL,
    cache_create_1h_tokens INTEGER NOT NULL, cache_create_other_tokens INTEGER NOT NULL,
    request_count INTEGER NOT NULL, PRIMARY KEY(file_id, usage_date, model)) STRICT;
CREATE TABLE seen_message(
    fingerprint INTEGER PRIMARY KEY, provider TEXT NOT NULL, file_id INTEGER NOT NULL,
    representative INTEGER NOT NULL DEFAULT 0, usage_date TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '') STRICT;
CREATE TABLE legacy_daily(
    provider TEXT NOT NULL, source TEXT NOT NULL, usage_date TEXT NOT NULL,
    model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, api_rate_estimate_nanos INTEGER,
    imported_at TEXT NOT NULL, cache_create_other_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(provider, source, usage_date, model)) STRICT;
PRAGMA user_version=4;
"""


def test_a_schema_4_store_is_migrated_and_rebuilt_by_the_next_scan(tmp_path: Path) -> None:
    root = _home(tmp_path)
    path = tmp_path / "scan.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(_SCHEMA_4)
    stat = next(root.rglob("*.jsonl")).stat()
    # The file is recorded as already scanned, unchanged: the trap. Without the
    # migration forgetting it, no incremental scan would ever re-read it.
    raw.execute(
        "INSERT INTO scanned_file VALUES(1, ?, 'claude', 'x', ?, ?, 'now', 3, 3, 0, 0, 0, 0)",
        (next(root.rglob("*.jsonl")).as_posix(), stat.st_size, stat.st_mtime_ns),
    )
    raw.execute(
        "INSERT INTO file_daily VALUES(1, '2026-09-21', 'claude-opus-5', 300, 7, 0, 0, 0, 0, 3)"
    )
    raw.execute(
        "INSERT INTO legacy_daily VALUES('claude', 'src', '2026-01-02', 'claude-opus-5', "
        "5, 6, 0, 99, 'now', 0)"
    )
    raw.commit()
    raw.close()

    with UsageScanStore(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0]
            for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "file_daily" not in tables
        assert store.file_counts("claude")["files"] == 0
        # Frozen legacy rows are not derived from transcripts, so they survive.
        assert [row.usage_date for row in store.legacy_totals("claude")] == ["2026-01-02"]
        result = store.scan(root, provider="claude")
        assert result.files_parsed == 1
        assert sum(row.output_tokens for row in store.totals("claude")) == 7


def test_reopening_a_current_store_discards_nothing(tmp_path: Path) -> None:
    root = _home(tmp_path)
    path = tmp_path / "scan.sqlite"
    with UsageScanStore(path) as store:
        store.scan(root, provider="claude")
    with UsageScanStore(path) as store:
        assert store.scan(root, provider="claude").files_unchanged == 1
        assert sum(row.output_tokens for row in store.totals("claude")) == 7


# -- legacy precedence ------------------------------------------------------


def test_legacy_days_are_superseded_on_utc_days_whatever_the_reader_zone(
    tmp_path: Path, system_zone
) -> None:
    root = _home(tmp_path)
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="claude")
        store.import_legacy(
            "claude",
            source="src",
            rows=[
                # A New York reader sees measured usage on 09-21 (the 02:30Z
                # record); that must NOT suppress the legacy 09-21, because no
                # measured usage exists on UTC 09-21.
                DailyUsage(usage_date="2026-09-21", model="claude-opus-5", input_tokens=5),
                # UTC 09-22 holds measured usage, so this one is superseded.
                DailyUsage(usage_date="2026-09-22", model="claude-opus-5", input_tokens=5),
            ],
        )
        for zone in ("America/New_York", "Europe/Berlin"):
            system_zone(zone)
            assert [row.usage_date for row in store.legacy_totals("claude")] == ["2026-09-21"]


def test_the_dashboard_answers_in_the_zone_the_machine_is_in_now(
    tmp_path: Path, system_zone
) -> None:
    from coordharness.usage.local_service import ProviderProbe, _UncachedLocalUsageService

    home = tmp_path / "home"
    root = _home(tmp_path)
    with UsageScanStore(home / ".coordharness" / "usage-scan.sqlite") as store:
        store.scan(root, provider="claude")

    def probe() -> ProviderProbe:
        return ProviderProbe(account={"status": "inactive", "plan": "unknown", "authenticated": False})

    service = _UncachedLocalUsageService(
        home=home,
        now=lambda: datetime(2026, 9, 22, 3, tzinfo=timezone.utc),
        claude_probe=probe,
        codex_probe=probe,
        cost_cache_root=home / "none",
    )
    # `root` is the service's own home: its `.claude` directory sits beneath the temporary home.
    system_zone("America/New_York")
    new_york = service.dashboard()
    system_zone("Europe/Berlin")
    berlin = service.dashboard()

    assert new_york["calendar"]["local_date"] == "2026-09-21"
    assert new_york["providers"]["claude"]["history"]["today_total_tokens"] == 101
    assert berlin["calendar"]["local_date"] == "2026-09-22"
    assert berlin["providers"]["claude"]["history"]["today_total_tokens"] == 100 * 3 + 7



def test_schema_4_code_can_read_a_slot_store_but_never_write_one(tmp_path: Path) -> None:
    """An installed runtime not yet upgraded must fail closed, not ping-pong."""

    root = _home(tmp_path)
    path = tmp_path / "scan.sqlite"
    with UsageScanStore(path) as store:
        store.scan(root, provider="claude")

    raw = sqlite3.connect(path)
    try:
        # What a schema-4 reader asks: per-day rows. It gets UTC days.
        days = raw.execute(
            "SELECT d.usage_date, SUM(d.output_tokens) FROM file_daily AS d "
            "JOIN scanned_file AS f ON f.file_id = d.file_id GROUP BY d.usage_date"
        ).fetchall()
        assert days == [("2026-09-22", 7)]
        # What a schema-4 writer runs first on open.
        with pytest.raises(sqlite3.OperationalError, match="views may not be indexed"):
            raw.executescript(
                "CREATE TABLE IF NOT EXISTS file_daily(file_id INTEGER) STRICT;"
                "CREATE INDEX IF NOT EXISTS file_daily_rollup ON file_daily(usage_date, model);"
            )
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()
    with UsageScanStore(path) as store:
        assert store.scan(root, provider="claude").files_unchanged == 1
