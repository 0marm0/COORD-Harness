"""Persistent incremental scan-store tests.

The store exists because a live bounded scan can only see a few days of a
multi-gigabyte transcript tree. What has to hold for it to be worth trusting:
a file is parsed once, a file that changed replaces its own contribution
rather than adding a second one, a replayed message is counted once across
files, and a run killed halfway loses only the file it was in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

import pytest

from coordharness.usage import scan_store as scan_store_module
from coordharness.usage.local_history import discover_local_cli_history
from coordharness.usage.scan_store import (
    ScanStoreError,
    UsageScanStore,
    read_store_history,
    scan_providers,
)

pytestmark = pytest.mark.unit


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _touch(path: Path, *, seconds: int) -> None:
    """Move a file's mtime by a whole second so no clock granularity hides it."""

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + seconds * 1_000_000_000))


def _claude_record(message_id: str, day: str = "2026-09-03", output: int = 10) -> dict:
    return {
        "timestamp": f"{day}T12:00:00+00:00",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "usage": {"input_tokens": 100, "output_tokens": output},
        },
    }


def _codex_record(ordinal: int, *, day: str = "2026-09-19") -> dict:
    return {
        "timestamp": f"{day}T12:00:00+00:00",
        "ordinal": ordinal,
        "payload": {
            "thread_id": "t-1",
            "model": "gpt-5.6-sol",
            "last_token_usage": {
                "input_tokens": 1_000,
                "cached_input_tokens": 900,
                "output_tokens": 5,
            },
        },
    }


@pytest.fixture()
def store(tmp_path: Path) -> UsageScanStore:
    with UsageScanStore(tmp_path / "scan.sqlite") as opened:
        yield opened


def _claude_home(tmp_path: Path) -> Path:
    root = tmp_path / "home" / ".claude"
    _write(root / "projects" / "a" / "first.jsonl", [_claude_record("msg-1")])
    _write(root / "projects" / "b" / "second.jsonl", [_claude_record("msg-2")])
    return root


def test_a_file_scanned_twice_contributes_once(tmp_path: Path, store: UsageScanStore) -> None:
    root = _claude_home(tmp_path)

    first = store.scan(root, provider="claude")
    second = store.scan(root, provider="claude")

    assert (first.files_parsed, first.files_unchanged) == (2, 0)
    assert (second.files_parsed, second.files_unchanged) == (0, 2)
    assert second.records_scanned == 0
    rows = store.totals("claude")
    assert sum(row.input_tokens for row in rows) == 200
    assert sum(row.request_count or 0 for row in rows) == 2


def test_a_changed_file_replaces_its_own_contribution(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """An appended transcript must not double what the file already reported."""

    root = _claude_home(tmp_path)
    store.scan(root, provider="claude")

    appended = root / "projects" / "a" / "first.jsonl"
    _write(appended, [_claude_record("msg-1"), _claude_record("msg-3")])
    _touch(appended, seconds=5)
    result = store.scan(root, provider="claude")

    assert (result.files_parsed, result.files_unchanged) == (1, 1)
    rows = store.totals("claude")
    assert sum(row.input_tokens for row in rows) == 300
    assert sum(row.request_count or 0 for row in rows) == 3


def test_a_shrunken_file_loses_the_records_it_no_longer_holds(
    tmp_path: Path, store: UsageScanStore
) -> None:
    root = _claude_home(tmp_path)
    store.scan(root, provider="claude")

    truncated = root / "projects" / "b" / "second.jsonl"
    truncated.write_text("")
    _touch(truncated, seconds=5)
    store.scan(root, provider="claude")

    assert sum(row.input_tokens for row in store.totals("claude")) == 100


def test_a_message_replayed_across_two_files_is_counted_once(
    tmp_path: Path, store: UsageScanStore
) -> None:
    root = tmp_path / ".claude"
    _write(root / "projects" / "a" / "first.jsonl", [_claude_record("msg-1")])
    # A resumed session replays the earlier turn verbatim and adds one of its own.
    _write(
        root / "projects" / "b" / "resumed.jsonl",
        [_claude_record("msg-1"), _claude_record("msg-2")],
    )

    result = store.scan(root, provider="claude")

    assert result.records_accepted == 2
    assert result.records_deduplicated == 1
    assert sum(row.input_tokens for row in store.totals("claude")) == 200


def test_a_replay_owner_that_is_reparsed_reclaims_its_own_identities(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """Clearing a file's ownership must not delete the message from the store."""

    root = tmp_path / ".claude"
    owner = root / "projects" / "a" / "first.jsonl"
    _write(owner, [_claude_record("msg-1")])
    _write(root / "projects" / "b" / "resumed.jsonl", [_claude_record("msg-1")])
    store.scan(root, provider="claude")

    _write(owner, [_claude_record("msg-1"), _claude_record("msg-9")])
    _touch(owner, seconds=5)
    store.scan(root, provider="claude")

    assert sum(row.input_tokens for row in store.totals("claude")) == 200


def test_totals_match_a_live_bounded_scan_of_the_same_tree(
    tmp_path: Path, store: UsageScanStore
) -> None:
    root = tmp_path / ".claude"
    _write(
        root / "projects" / "a" / "first.jsonl",
        [_claude_record("msg-1"), _claude_record("msg-2", day="2026-09-04")],
    )
    _write(
        root / "projects" / "b" / "resumed.jsonl",
        [_claude_record("msg-2", day="2026-09-04"), _claude_record("msg-3", output=77)],
    )
    _write(root / "projects" / "b" / "unidentified.jsonl", [{
        "timestamp": "2026-09-05T01:00:00+00:00",
        "message": {"model": "claude-opus-5", "usage": {"input_tokens": 3, "output_tokens": 1}},
    }])

    store.scan(root, provider="claude")
    live = discover_local_cli_history(root, provider="claude")

    assert store.totals("claude") == live.rows
    stored = store.history_import(root, provider="claude")
    assert stored is not None
    assert stored.rows == live.rows
    assert stored.records_accepted == live.records_accepted
    assert stored.records_deduplicated == live.records_deduplicated
    assert stored.records_unidentified == live.records_unidentified
    assert stored.root_identity_digest == live.root_identity_digest


def test_a_capped_run_resumes_where_it_stopped(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    for index in range(4):
        _write(root / "projects" / f"p{index}.jsonl", [_claude_record(f"msg-{index}")])
    path = tmp_path / "scan.sqlite"

    with UsageScanStore(path) as first:
        partial = first.scan(root, provider="claude", max_files=2)
        assert partial.truncated is True
        assert partial.files_parsed == 2
    # A separate process would open its own connection; so does this.
    with UsageScanStore(path) as second:
        resumed = second.scan(root, provider="claude")
        assert resumed.truncated is False
        assert (resumed.files_parsed, resumed.files_unchanged) == (2, 2)
        assert sum(row.input_tokens for row in second.totals("claude")) == 400
        assert second.totals("claude") == discover_local_cli_history(
            root, provider="claude"
        ).rows


def test_a_file_that_failed_mid_write_is_left_unscanned(
    tmp_path: Path, store: UsageScanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed process must lose the current file entirely, not half of it."""

    root = tmp_path / ".claude"
    _write(
        root / "projects" / "a.jsonl",
        [_claude_record("msg-1"), _claude_record("msg-2"), _claude_record("msg-3")],
    )
    real_claim = UsageScanStore._claim
    calls = {"n": 0}

    def exploding_claim(conn, provider, identity, record, file_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("interrupted")
        return real_claim(conn, provider, identity, record, file_id)

    monkeypatch.setattr(UsageScanStore, "_claim", staticmethod(exploding_claim))
    with pytest.raises(sqlite3.OperationalError):
        store.scan(root, provider="claude")

    assert store.totals("claude") == ()
    assert store.file_counts("claude")["files"] == 0

    monkeypatch.setattr(UsageScanStore, "_claim", staticmethod(real_claim))
    result = store.scan(root, provider="claude")

    assert result.files_parsed == 1
    assert sum(row.input_tokens for row in store.totals("claude")) == 300


def test_a_transcript_that_dies_mid_read_is_retried_rather_than_half_kept(
    tmp_path: Path, store: UsageScanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".claude"
    _write(
        root / "projects" / "a.jsonl",
        [_claude_record("msg-1"), _claude_record("msg-2")],
    )
    real_iter = scan_store_module.iter_history_records

    def dying_iter(path, *, provider, counters, max_records=None):
        for index, record in enumerate(real_iter(
            path, provider=provider, counters=counters, max_records=max_records
        )):
            if index == 1:
                raise OSError("input/output error")
            yield record

    monkeypatch.setattr(scan_store_module, "iter_history_records", dying_iter)
    result = store.scan(root, provider="claude")

    assert (result.files_parsed, result.files_failed) == (0, 1)
    assert store.totals("claude") == ()

    monkeypatch.setattr(scan_store_module, "iter_history_records", real_iter)
    assert store.scan(root, provider="claude").files_parsed == 1
    assert sum(row.input_tokens for row in store.totals("claude")) == 200


def test_codex_cached_tokens_survive_the_store_round_trip(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """Codex reports input inclusive of cache; the subtraction must persist."""

    root = tmp_path / ".codex"
    _write(root / "sessions" / "s.jsonl", [_codex_record(1), _codex_record(2)])

    store.scan(root, provider="codex")
    row = store.totals("codex")[0]

    assert row.model == "gpt-5.6-sol"
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (200, 1_800, 10)
    assert store.totals("codex") == discover_local_cli_history(root, provider="codex").rows


def test_providers_are_stored_separately(tmp_path: Path, store: UsageScanStore) -> None:
    home = tmp_path / "home"
    _write(home / ".claude" / "projects" / "a.jsonl", [_claude_record("msg-1")])
    _write(home / ".codex" / "sessions" / "s.jsonl", [_codex_record(1)])

    store.scan(home / ".claude", provider="claude")
    store.scan(home / ".codex", provider="codex")

    assert [row.model for row in store.totals("claude")] == ["claude-opus-5"]
    assert [row.model for row in store.totals("codex")] == ["gpt-5.6-sol"]


def test_rebuild_drops_and_rescans_one_provider(tmp_path: Path, store: UsageScanStore) -> None:
    root = _claude_home(tmp_path)
    store.scan(root, provider="claude")
    # A deleted file keeps its contribution until a rebuild; that is the
    # documented limitation, and this is the repair for it.
    (root / "projects" / "b" / "second.jsonl").unlink()

    assert sum(row.input_tokens for row in store.totals("claude")) == 200
    rebuilt = store.rebuild(root, provider="claude")

    assert rebuilt.files_parsed == 1
    assert sum(row.input_tokens for row in store.totals("claude")) == 100


def test_a_store_from_another_home_is_not_served(tmp_path: Path, store: UsageScanStore) -> None:
    root = _claude_home(tmp_path)
    store.scan(root, provider="claude")
    other = tmp_path / "elsewhere" / ".claude"
    (other / "projects").mkdir(parents=True)

    assert store.history_import(other, provider="claude") is None


def test_an_absent_store_reads_as_nothing_rather_than_as_an_empty_history(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope" / "scan.sqlite"

    assert read_store_history(tmp_path / ".claude", provider="claude", store_path=missing) is None
    assert not missing.exists()


def test_a_read_only_store_refuses_to_scan(tmp_path: Path) -> None:
    path = tmp_path / "scan.sqlite"
    root = _claude_home(tmp_path)
    with UsageScanStore(path) as writable:
        writable.scan(root, provider="claude")
    with UsageScanStore(path, read_only=True) as reader:
        with pytest.raises(ScanStoreError):
            reader.scan(root, provider="claude")
        assert reader.history_import(root, provider="claude") is not None


def test_scan_providers_walks_both_cli_homes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".claude" / "projects" / "a.jsonl", [_claude_record("msg-1")])
    _write(home / ".codex" / "sessions" / "s.jsonl", [_codex_record(1)])
    path = tmp_path / "scan.sqlite"

    results = scan_providers(home, store_path=path)

    assert [result.provider for result in results] == ["claude", "codex"]
    assert [result.files_parsed for result in results] == [1, 1]
    assert (
        read_store_history(home / ".codex", provider="codex", store_path=path).rows[0].input_tokens
        == 100
    )


def _service(home: Path):
    from datetime import datetime, timezone

    from coordharness.usage.local_service import ProviderProbe, _UncachedLocalUsageService

    def probe() -> ProviderProbe:
        return ProviderProbe(
            account={"status": "inactive", "plan": "unknown", "authenticated": False}
        )

    return _UncachedLocalUsageService(
        home=home,
        now=lambda: datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
        claude_probe=probe,
        codex_probe=probe,
        cost_cache_root=home / "no-cost-cache",
    )


def test_the_dashboard_prefers_the_scan_store_and_falls_back_without_it(tmp_path: Path) -> None:
    """The store is the only thing that can see a transcript that is now gone."""

    home = tmp_path / "home"
    _write(home / ".claude" / "projects" / "kept.jsonl", [_claude_record("msg-1")])
    pruned = home / ".claude" / "projects" / "pruned.jsonl"
    _write(pruned, [_claude_record("msg-2", day="2026-02-01")])
    store_path = home / ".coordharness" / "usage-scan.sqlite"
    with UsageScanStore(store_path) as store:
        store.scan(home / ".claude", provider="claude")
    pruned.unlink()

    stored_days = [
        day["date"]
        for day in _service(home).dashboard()["providers"]["claude"]["history"]["daily"]
    ]
    store_path.unlink()
    live_days = [
        day["date"]
        for day in _service(home).dashboard()["providers"]["claude"]["history"]["daily"]
    ]

    assert stored_days == ["2026-02-01", "2026-09-03"]
    assert live_days == ["2026-09-03"]


def test_an_unsupported_provider_is_refused(store: UsageScanStore) -> None:
    with pytest.raises(ValueError):
        store.totals("gemini")


# -- the representative is the component-wise maximum ----------------------


def _streaming_lines(message_id: str, outputs: tuple[int, ...], day: str = "2026-09-03") -> list:
    """One assistant response as Claude Code writes it: a line per content block.

    Every line repeats `message.id` and the running usage snapshot, so the
    early lines are PARTIAL and only the last one is complete. Shaped after
    request req_011CZAv7bGwRRPQSUy5VaQ3E, whose five lines report 2, 2, 2, 2
    and then 286 output tokens.
    """

    return [
        {
            "timestamp": f"{day}T12:00:0{index}+00:00",
            "requestId": f"req-{message_id}",
            "message": {
                "id": message_id,
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": output,
                    "cache_read_input_tokens": 1_000 if index == len(outputs) - 1 else 0,
                },
            },
        }
        for index, output in enumerate(outputs)
    ]


def test_a_streaming_request_aggregates_to_the_complete_snapshot_not_the_partial(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """Keeping the first line kept a 2-token snapshot of a 286-token response."""

    root = tmp_path / ".claude"
    _write(
        root / "projects" / "a" / "stream.jsonl",
        _streaming_lines("msg-stream", (2, 2, 2, 2, 286)),
    )

    result = store.scan(root, provider="claude")
    rows = store.totals("claude")

    assert len(rows) == 1
    assert rows[0].output_tokens == 286
    assert rows[0].cache_read_tokens == 1_000
    assert rows[0].input_tokens == 4
    # One response, one request -- a raise must not invent a second.
    assert rows[0].request_count == 1
    assert (result.records_accepted, result.records_deduplicated) == (1, 4)
    # Only the last line carries anything the first did not, so three of the
    # four repeats add nothing and one raises.
    assert result.records_raised == 1
    assert result.records_raised_across_files == 0
    # And the live bounded scan has to reach the same figure, or the dashboard
    # reports one number with the store and another without it.
    assert rows == discover_local_cli_history(root, provider="claude").rows


def test_a_partial_snapshot_arriving_last_does_not_lower_the_representative(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """The maximum is order-independent; the last occurrence is not."""

    root = tmp_path / ".claude"
    _write(
        root / "projects" / "a" / "stream.jsonl",
        _streaming_lines("msg-stream", (286, 2, 2)),
    )

    store.scan(root, provider="claude")

    assert store.totals("claude")[0].output_tokens == 286


def test_a_raise_from_another_file_lands_on_the_owning_files_aggregate(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """A fuller observation elsewhere must raise, not be discarded as a replay."""

    root = tmp_path / ".claude"
    _write(root / "projects" / "a" / "partial.jsonl", _streaming_lines("msg-1", (2,)))
    _write(root / "projects" / "b" / "complete.jsonl", _streaming_lines("msg-1", (286,)))

    result = store.scan(root, provider="claude")

    assert store.totals("claude")[0].output_tokens == 286
    assert result.records_raised_across_files == 1
    owners = store.connection.execute(
        "SELECT f.path AS path, d.output_tokens AS output FROM file_slot AS d "
        "JOIN scanned_file AS f ON f.file_id = d.file_id ORDER BY f.path"
    ).fetchall()
    # The whole representative sits with the file that CLAIMED the identity,
    # increase included, so one reparse of that file replaces the identity's
    # contribution whole. The raising file, having nothing of its own, keeps
    # no aggregate row at all.
    assert [(Path(row["path"]).name, row["output"]) for row in owners] == [
        ("partial.jsonl", 286)
    ]
    assert store.rebuild(root, provider="claude").records_raised_across_files == 1
    assert store.totals("claude")[0].output_tokens == 286


def test_a_full_double_rescan_leaves_the_totals_unchanged(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """Idempotence is the property that makes the incremental store trustworthy.

    Every file is forced to reparse twice, in the same order a real run would
    walk them, and the totals have to come back byte-identical -- otherwise a
    raise applied to another file's aggregate would accumulate.
    """

    root = tmp_path / ".claude"
    _write(
        root / "projects" / "a" / "stream.jsonl",
        _streaming_lines("msg-1", (2, 2, 286)) + _streaming_lines("msg-2", (5, 70)),
    )
    # A resumed session that replays the whole response, complete snapshot and
    # all, which is what a real replay copies.
    _write(
        root / "projects" / "b" / "resumed.jsonl",
        _streaming_lines("msg-1", (2, 2, 286)) + _streaming_lines("msg-3", (9,)),
    )
    store.scan(root, provider="claude")
    first = store.totals("claude")

    for shift in (5, 10):
        for path in sorted(root.rglob("*.jsonl")):
            _touch(path, seconds=shift)
        store.scan(root, provider="claude")
        assert store.totals("claude") == first

    assert sum(row.output_tokens for row in first) == 286 + 70 + 9


def test_an_identity_stored_by_schema_3_keeps_the_first_claimant_rule(
    tmp_path: Path,
) -> None:
    """A migrated store must not double-count what it already counted.

    A schema-3 row holds no representative, and its zeros are absence rather
    than an observation of zero. Raising from them would add a whole record on
    top of a contribution the owner already has, so the old rule stands until
    a rebuild replaces the row.
    """

    path = tmp_path / "scan.sqlite"
    root = tmp_path / ".claude"
    _write(root / "projects" / "a" / "stream.jsonl", _streaming_lines("msg-1", (2, 286)))
    with UsageScanStore(path) as store:
        store.scan(root, provider="claude")
    raw = sqlite3.connect(path)
    raw.execute("UPDATE seen_message SET representative=0")
    raw.execute("UPDATE file_slot SET output_tokens=2")
    raw.commit()
    raw.close()

    with UsageScanStore(path) as store:
        # Present the same identity again from a second file.
        _write(root / "projects" / "b" / "again.jsonl", _streaming_lines("msg-1", (286,)))
        result = store.scan(root, provider="claude")

        assert result.records_raised == 0
        assert store.totals("claude")[0].output_tokens == 2
        # The repair is a rebuild, which rewrites every row under this schema.
        store.rebuild(root, provider="claude")
        assert store.totals("claude")[0].output_tokens == 286


# -- the second Claude transcript tree -------------------------------------


def _desktop_transcript(home: Path, session: str, name: str) -> Path:
    """Where the Claude desktop app's agent mode nests a Claude Code transcript."""

    return (
        home
        / "Library"
        / "Application Support"
        / "Claude"
        / "local-agent-mode-sessions"
        / session
        / f"local_{session}"
        / ".claude"
        / "projects"
        / "desktop"
        / name
    )


def test_the_claude_desktop_tree_is_scanned_and_dedups_against_the_main_tree(
    tmp_path: Path, store: UsageScanStore
) -> None:
    """Two trees, one identity space: new spend is added, shared spend is not."""

    home = tmp_path / "home"
    root = home / ".claude"
    _write(root / "projects" / "cli" / "a.jsonl", [_claude_record("msg-shared")])
    _write(
        _desktop_transcript(home, "s1", "b.jsonl"),
        [_claude_record("msg-shared"), _claude_record("msg-desktop-only")],
    )

    result = store.scan(root, provider="claude")

    assert result.files_parsed == 2
    assert result.records_deduplicated == 1
    assert sum(row.input_tokens for row in store.totals("claude")) == 200
    # The two trees hang off one home, so one root digest still identifies the
    # whole set and a store built elsewhere stays refused.
    assert store.history_import(root, provider="claude") is not None
    assert store.history_import(tmp_path / "other" / ".claude", provider="claude") is None
    # The fallback live scan must see the same set, or removing the store would
    # change the number the dashboard shows.
    assert store.totals("claude") == discover_local_cli_history(root, provider="claude").rows


def test_the_desktop_tree_raises_a_partial_snapshot_recorded_by_the_cli(
    tmp_path: Path, store: UsageScanStore
) -> None:
    home = tmp_path / "home"
    root = home / ".claude"
    _write(root / "projects" / "cli" / "a.jsonl", _streaming_lines("msg-1", (2,)))
    _write(_desktop_transcript(home, "s1", "b.jsonl"), _streaming_lines("msg-1", (286,)))

    store.scan(root, provider="claude")

    assert store.totals("claude")[0].output_tokens == 286


def test_the_codex_root_set_is_only_the_rollout_directory(tmp_path: Path) -> None:
    """Archived Codex sessions stay out, deliberately.

    They contribute $0.83 over four days that all predate the first rollout --
    days the frozen legacy import already covers. Adding them would replace a
    complete third-party figure for those days with a partial self-computed
    one, because a self-computed day suppresses the legacy row for it.
    """

    from coordharness.usage.local_history import history_scan_roots

    assert history_scan_roots(tmp_path / ".codex", provider="codex") == (
        tmp_path / ".codex" / "sessions",
    )
    assert history_scan_roots(tmp_path / "home" / ".claude", provider="claude") == (
        tmp_path / "home" / ".claude" / "projects",
        tmp_path / "home" / "Library" / "Application Support" / "Claude"
        / "local-agent-mode-sessions",
    )
    with pytest.raises(ValueError):
        history_scan_roots(tmp_path, provider="gemini")
