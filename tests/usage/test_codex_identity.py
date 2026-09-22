"""Codex replay identity, pinned against what the real rollout corpus does.

Codex writes usage as ``event_msg``/``token_count`` records that carry no
thread or turn id of their own. The identity therefore has to come from the
``session_meta`` header at the top of the rollout plus the record's ordinal.
These tests pin the three facts that make that safe, each of which was
measured against ~/.codex/sessions (3,721 files, 20GB, 307,184 usage records):

1. real records do get an identity, so ``records_unidentified`` is 0 rather
   than the whole corpus;
2. a subagent rollout embeds its PARENT's ``session_meta`` immediately after
   its own -- letting that win silently drops the child's real usage;
3. the cross-file snapshot a subagent opens with is NOT deduplicable, because
   the copy is renumbered and restamped under the child's own thread, so the
   totals must stay whole rather than quietly shrink.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.usage.local_history import (
    discover_local_cli_history,
    record_identity,
)
from coordharness.usage.scan_store import UsageScanStore

pytestmark = pytest.mark.unit

_DAY = "2026-09-19"


def _session_meta(thread_id: str, *, ordinal: int = 0, source: str = "user") -> dict:
    return {
        "timestamp": f"{_DAY}T12:00:00.000Z",
        "ordinal": ordinal,
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": "01a0-session",
            "thread_source": source,
            "cwd": "/tmp/project",
            "originator": "Codex Desktop",
        },
    }


def _turn_context(ordinal: int, *, model: str = "gpt-5.6-sol") -> dict:
    return {
        "timestamp": f"{_DAY}T12:00:01.000Z",
        "ordinal": ordinal,
        "type": "turn_context",
        "payload": {"cwd": "/tmp/project", "model": model, "effort": "high"},
    }


def _token_count(ordinal: int, *, output: int = 50, running: int | None = None) -> dict:
    """One billable turn, shaped exactly as Codex writes it: no id anywhere.

    The thread's running total advances with the ordinal by default, as it
    does in a real rollout: a record whose running total does NOT advance is a
    re-emission and is skipped (see ``iter_history_records``).
    """

    if running is None:
        running = ordinal * 1_000
    return {
        "timestamp": f"{_DAY}T12:00:0{ordinal % 10}.000Z",
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 1000 + running,
                    "cached_input_tokens": 400,
                    "cache_write_input_tokens": 0,
                    "output_tokens": output,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 1050 + running,
                },
                "last_token_usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 400,
                    "cache_write_input_tokens": 0,
                    "output_tokens": output,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 1000 + output,
                },
                "model_context_window": 258400,
            },
            "rate_limits": {"limit_id": "codex", "plan_type": "pro"},
        },
    }


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _rollout(root: Path, thread_id: str, records: list[dict]) -> Path:
    path = root / "sessions" / f"rollout-2026-09-19T12-00-00-{thread_id}.jsonl"
    _write(path, records)
    return path


def _tokens(rows) -> int:
    return sum(
        row.input_tokens
        + row.output_tokens
        + row.cache_read_tokens
        + row.cache_create_5m_tokens
        + row.cache_create_1h_tokens
        + row.cache_create_other_tokens
        for row in rows
    )


def test_codex_usage_records_carry_no_identity_of_their_own() -> None:
    """The old scheme's premise: token_count has no thread_id. Hence 304k misses."""

    record = _token_count(11)
    assert "thread_id" not in record["payload"]
    assert record_identity("codex", record, None) is None
    assert record_identity("codex", record, None, thread_id="01a0-a") == "t:01a0-a:11"


def test_real_shaped_codex_rollout_leaves_nothing_unidentified(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-a",
        [_session_meta("01a0-a"), _turn_context(1)]
        + [_token_count(ordinal) for ordinal in (11, 19, 27)],
    )
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_accepted == 3
    assert imported.records_unidentified == 0
    assert imported.records_deduplicated == 0
    assert _tokens(imported.rows) == 3 * 1050


def test_the_header_names_the_thread_even_without_a_usable_filename(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    _write(
        root / "sessions" / "not-a-rollout-name.jsonl",
        [_session_meta("01a0-a"), _token_count(11)],
    )
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_accepted == 1
    assert imported.records_unidentified == 0


def test_the_filename_uuid_rescues_a_rollout_whose_header_is_missing(tmp_path: Path) -> None:
    """A truncated or oversized header must not cost the file its identity."""

    root = tmp_path / ".codex"
    _rollout(root, "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0000", [_token_count(11)])
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_accepted == 1
    assert imported.records_unidentified == 0


def test_an_embedded_parent_header_does_not_steal_the_childs_identity(tmp_path: Path) -> None:
    """Regression: a subagent rollout embeds its parent's session_meta at ordinal 1.

    Both files number ordinals independently, so adopting the parent's thread
    id makes the child's own turns collide with unrelated parent records. On
    the real corpus that silently dropped 307 records worth 45,081,243 tokens.
    """

    root = tmp_path / ".codex"
    parent_ordinals = (11, 19, 27)
    _rollout(
        root,
        "01a0-parent",
        [_session_meta("01a0-parent"), _turn_context(1)]
        + [_token_count(ordinal) for ordinal in parent_ordinals],
    )
    # The child re-uses the very same ordinals for entirely different turns.
    _rollout(
        root,
        "01a0-child",
        [
            _session_meta("01a0-child", source="subagent"),
            _session_meta("01a0-parent", ordinal=1, source="user"),
            _turn_context(2),
        ]
        + [_token_count(ordinal, output=77) for ordinal in parent_ordinals],
    )
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_unidentified == 0
    # Six distinct billable turns, none of them a replay of another.
    assert imported.records_deduplicated == 0
    assert imported.records_accepted == 6
    assert _tokens(imported.rows) == 3 * 1050 + 3 * 1077


def test_a_subagent_snapshot_is_not_deduplicated_and_that_is_the_finding(
    tmp_path: Path,
) -> None:
    """Codex's only cross-file replay is a snapshot that carries no origin.

    The copied turns are rewritten under the child's thread id with fresh
    ordinals and a fresh timestamp, so no identity can match them back to the
    parent. This test exists to pin that the scan does NOT pretend otherwise:
    the copy is counted, and the totals say so.
    """

    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-parent",
        [_session_meta("01a0-parent"), _turn_context(1)]
        + [_token_count(ordinal) for ordinal in (11, 19, 27)],
    )
    # The child snapshots those same three turns: renumbered from 2, restamped.
    snapshot = [_token_count(ordinal) for ordinal in (2, 3, 4)]
    for record in snapshot:
        record["timestamp"] = f"{_DAY}T18:00:00.000Z"
    _rollout(
        root,
        "01a0-child",
        [_session_meta("01a0-child", source="subagent"), _turn_context(1)] + snapshot,
    )
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_deduplicated == 0
    assert imported.records_accepted == 6
    assert _tokens(imported.rows) == 6 * 1050


def test_a_resumed_rollout_continues_the_ordinal_sequence_without_false_dedup(
    tmp_path: Path,
) -> None:
    """A resumed rollout keeps its thread id and carries the ordinals forward.

    Three such pairs exist on this machine. Because the ordinals continue
    rather than restart, (thread_id, ordinal) neither collides nor loses rows.
    """

    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-a",
        [_session_meta("01a0-a"), _turn_context(1)]
        + [_token_count(ordinal) for ordinal in (11, 19)],
    )
    _write(
        root / "sessions" / "rollout-2026-09-19T18-00-00-01a0-a_01a0-b.jsonl",
        [_session_meta("01a0-a"), _turn_context(30)]
        + [_token_count(ordinal) for ordinal in (31, 39)],
    )
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_deduplicated == 0
    assert imported.records_accepted == 4
    assert _tokens(imported.rows) == 4 * 1050


def test_a_genuine_replay_of_the_same_thread_and_ordinal_is_deduplicated(
    tmp_path: Path,
) -> None:
    """The identity is not decorative: an actual verbatim replay collapses."""

    root = tmp_path / ".codex"
    records = [_session_meta("01a0-a"), _turn_context(1)] + [
        _token_count(ordinal) for ordinal in (11, 19)
    ]
    _rollout(root, "01a0-a", records)
    _write(root / "sessions" / "rollout-2026-09-19T13-00-00-01a0-a_copy.jsonl", records)
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_deduplicated == 2
    assert imported.records_accepted == 2
    assert _tokens(imported.rows) == 2 * 1050


def test_the_scan_store_owns_codex_identities_across_files(tmp_path: Path) -> None:
    """The store must reach the same verdict the live scan does, and persist it."""

    root = tmp_path / ".codex"
    records = [_session_meta("01a0-a"), _turn_context(1)] + [
        _token_count(ordinal) for ordinal in (11, 19)
    ]
    _rollout(root, "01a0-a", records)
    _write(root / "sessions" / "rollout-2026-09-19T13-00-00-01a0-a_copy.jsonl", records)
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        result = store.scan(root, provider="codex")
        assert result.records_unidentified == 0
        assert result.records_deduplicated == 2
        assert _tokens(store.totals("codex")) == 2 * 1050
        # A second scan changes nothing: every file is unchanged.
        again = store.scan(root, provider="codex")
        assert again.files_parsed == 0
        assert _tokens(store.totals("codex")) == 2 * 1050


# -- naming the model, and the cache write ---------------------------------


def _thread_settings(ordinal: int, *, model: str = "gpt-5.6-sol") -> dict:
    """Where 58 of the 69 `unknown` rollouts actually name their model first.

    Not `payload.model` and not the session_meta header: a `thread_settings`
    block inside an ordinary event_msg, well past the first usage record.
    """

    return {
        "timestamp": f"{_DAY}T12:00:05.000Z",
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "thread_settings_update",
            "thread_settings": {"model": model, "model_provider_id": "openai"},
        },
    }


def test_a_model_named_after_the_first_usage_record_still_labels_it(
    tmp_path: Path,
) -> None:
    """The bug: a name the file only states later left the tokens priced at $0.

    Codex names the model once per session, and in 56 of the 69 rollouts that
    produced an `unknown` model here it does so only AFTER the first usage
    record -- 431,902,456 tokens' worth. `unknown` resolves to no rate, so
    those tokens read as free while still counting as volume.
    """

    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-a",
        [_session_meta("01a0-a"), _token_count(11), _thread_settings(12), _token_count(19)],
    )

    imported = discover_local_cli_history(root, provider="codex")

    assert [row.model for row in imported.rows] == ["gpt-5.6-sol"]
    assert _tokens(imported.rows) == 2 * 1050
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="codex")
        assert [row.model for row in store.totals("codex")] == ["gpt-5.6-sol"]


def test_an_explicit_payload_model_still_overrides_the_declaration(
    tmp_path: Path,
) -> None:
    """The declaration is a starting value, not a relabelling of the whole file.

    `codex-auto-review` is a Codex-internal label written as `payload.model`,
    and a session that switches model mid-run states the new one the same way.
    Either must keep its own name on the records that follow it.
    """

    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-a",
        [
            _session_meta("01a0-a"),
            _token_count(11),
            _thread_settings(12),
            _turn_context(13, model="codex-auto-review"),
            _token_count(19),
        ],
    )

    imported = discover_local_cli_history(root, provider="codex")

    assert sorted(row.model for row in imported.rows) == ["codex-auto-review", "gpt-5.6-sol"]


def test_a_rollout_that_names_no_model_stays_visibly_unpriced(tmp_path: Path) -> None:
    """A gap must read as unknown, never as free. No rate is imputed here."""

    from coordharness.usage.pricing import load_rate_card

    root = tmp_path / ".codex"
    _rollout(root, "01a0-a", [_session_meta("01a0-a"), _token_count(11)])

    imported = discover_local_cli_history(root, provider="codex")

    assert [row.model for row in imported.rows] == ["unknown"]
    priced = load_rate_card().price("unknown", {"input_tokens": 1_000})
    assert priced.amount_nanos is None
    assert priced.reason == "model_not_in_rate_card"
    assert priced.unpriced_tokens == 1_000


def test_a_codex_cache_write_is_read_into_the_five_minute_component(
    tmp_path: Path,
) -> None:
    """`cache_write_input_tokens` was parsed as nothing at all.

    It is 0 in every record sampled on this machine, so this costs nothing
    today -- but the rate card already prices a Codex cache write, so the day
    OpenAI populates the key the tokens would have vanished silently.
    """

    root = tmp_path / ".codex"
    record = _token_count(11)
    record["payload"]["info"]["last_token_usage"]["cache_write_input_tokens"] = 700

    _rollout(root, "01a0-a", [_session_meta("01a0-a"), _turn_context(1), record])
    imported = discover_local_cli_history(root, provider="codex")

    row = imported.rows[0]
    assert row.cache_create_5m_tokens == 700
    assert (row.cache_create_1h_tokens, row.cache_create_other_tokens) == (0, 0)
    # The cached READ prefix is still stripped out of input, unchanged.
    assert (row.input_tokens, row.cache_read_tokens) == (600, 400)
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.scan(root, provider="codex")
        assert store.totals("codex")[0].cache_create_5m_tokens == 700


def test_a_re_emitted_token_count_is_skipped_not_counted(tmp_path: Path) -> None:
    """Codex repeats a record under a new ordinal without the total advancing.

    Measured 2026-09-22: 5,829 such records, ~0.826B tokens. Each has a fresh
    (thread, ordinal) identity, so dedup cannot see it; the running total is
    what says nothing new was requested.
    """

    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-a",
        [_session_meta("01a0-a"), _turn_context(1)]
        + [
            _token_count(11, running=0),
            # Same running total as the record before it: a re-emission.
            _token_count(12, running=0),
            _token_count(19, running=1_000),
            _token_count(20, running=1_000),
            _token_count(21, running=1_000),
            _token_count(27, running=2_000),
        ],
    )
    imported = discover_local_cli_history(root, provider="codex")
    assert imported.records_accepted == 3
    assert imported.records_reemitted == 3
    assert _tokens(imported.rows) == 3 * 1050

    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        result = store.scan(root, provider="codex")
        assert result.records_accepted == 3
        assert _tokens(store.totals("codex")) == 3 * 1050


def test_a_running_total_that_falls_is_a_new_thread_state_not_a_repeat(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    _rollout(
        root,
        "01a0-a",
        [_session_meta("01a0-a"), _turn_context(1)]
        + [_token_count(11, running=5_000), _token_count(12, running=0)],
    )
    assert discover_local_cli_history(root, provider="codex").records_accepted == 2
