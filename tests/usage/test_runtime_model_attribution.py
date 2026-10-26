"""Runtime model attribution follows each declaration without relabelling history."""

import json

import pytest

from coordharness.usage.local_history import FileParseCounters, iter_history_records


def _usage(ordinal):
    return {
        "timestamp": "2026-10-22T12:00:00Z",
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 30,
                    "output_tokens": 7,
                },
                "total_token_usage": {
                    "input_tokens": ordinal * 100,
                    "cached_input_tokens": ordinal * 30,
                    "output_tokens": ordinal * 7,
                },
            },
        },
    }


def _read(tmp_path, records):
    path = tmp_path / "rollout-2026-10-22T12-00-00-11111111-2222-4333-8444-555555555555.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return list(iter_history_records(path, provider="codex", counters=FileParseCounters()))


def _declare(payload, kind="event_msg"):
    return {"timestamp": "2026-10-22T12:00:00Z", "type": kind, "payload": payload}


@pytest.mark.parametrize(
    "declaration",
    [
        _declare({"model": "model-new"}, "turn_context"),
        _declare({"type": "thread_settings_applied", "thread_settings": {"model": "model-new"}}),
        _declare({"state": {"model": "model-new"}}, "world_state"),
        _declare({"collaboration_mode": {"settings": {"model": "model-new"}}}, "turn_context"),
        _declare({"thread_settings": {"collaboration_mode": {"settings": {"model": "model-new"}}}}),
        _declare({"state": {"collaboration_mode": {"model": "model-new"}}}, "world_state"),
        _declare(
            {"resume_metadata": {"previous_turn_settings": {"model": "model-new"}}}, "compacted"
        ),
    ],
)
def test_each_runtime_schema_updates_later_usage_beyond_prefix(tmp_path, declaration):
    records = [_declare({"state": {"model": "model-old"}}, "world_state"), _usage(1)]
    records += [_declare({})] * 513
    records += [declaration, _usage(2)]
    parsed = _read(tmp_path, records)
    assert [row.model for row in parsed] == ["model-old", "model-new"]
    assert [sum(row.metrics.values()) for row in parsed] == [107, 107]
    assert [row.identity for row in parsed] == [
        "t:11111111-2222-4333-8444-555555555555:1",
        "t:11111111-2222-4333-8444-555555555555:2",
    ]


def test_resumed_settings_then_explicit_turn_model_preserve_all_switches(tmp_path):
    parsed = _read(
        tmp_path,
        [
            _declare({"state": {"model": "initial"}}, "world_state"),
            _usage(1),
            _declare(
                {"resume_metadata": {"previous_turn_settings": {"model": "resumed"}}}, "compacted"
            ),
            _usage(2),
            _declare({"type": "thread_settings_applied", "thread_settings": {"model": "selected"}}),
            _usage(3),
            _declare({"model": "codex-auto-review"}, "turn_context"),
            _usage(4),
        ],
    )
    assert [row.model for row in parsed] == ["initial", "resumed", "selected", "codex-auto-review"]


def test_instruction_provenance_does_not_override_explicit_role_model(tmp_path):
    # The two real rollouts behind 2026-10-22's 5.6 rows have this disagreement.
    parsed = _read(
        tmp_path,
        [
            _declare(
                {"base_instructions": {"provenance": {"type": "model", "model": "gpt-6.1-sol"}}},
                "session_meta",
            ),
            _declare({"state": {"model": "gpt-5.6-sol"}}, "world_state"),
            _declare(
                {
                    "model": "gpt-5.6-sol",
                    "collaboration_mode": {"settings": {"model": "gpt-5.6-sol"}},
                },
                "turn_context",
            ),
            _usage(1),
        ],
    )
    assert [row.model for row in parsed] == ["gpt-5.6-sol"]


def test_opening_usage_uses_first_runtime_declaration_and_missing_stays_unknown(tmp_path):
    parsed = _read(
        tmp_path, [_usage(1), _declare({"thread_settings": {"model": "late"}}), _usage(2)]
    )
    assert [row.model for row in parsed] == ["late", "late"]
    parsed = _read(
        tmp_path,
        [
            _declare({"base_instructions": {"provenance": {"model": "template"}}}, "session_meta"),
            _usage(1),
        ],
    )
    assert [row.model for row in parsed] == ["unknown"]


def test_invalid_model_declarations_cannot_poison_valid_settings(tmp_path):
    parsed = _read(
        tmp_path,
        [
            _declare({"model": "valid"}, "turn_context"),
            _usage(1),
            _declare({"state": {"model": "\t"}}),
            _usage(2),
            _declare({"type": "task_complete"}),
            _declare({"model": "bad\nname", "thread_settings": {"model": "next-valid"}}),
            _usage(3),
        ],
    )
    assert [row.model for row in parsed] == ["valid", "valid", "next-valid"]


def test_settings_changed_mid_request_do_not_relabel_its_owning_turn(tmp_path):
    parsed = _read(
        tmp_path,
        [
            _declare({"turn_id": "old-turn", "model": "gpt-6-luna"}, "turn_context"),
            _usage(1),
            _declare(
                {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-6.1-sol"}}
            ),
            _declare({"turn_id": "old-turn"}, "token_usage_record"),
            _usage(2),
            _declare({"type": "turn_aborted", "turn_id": "old-turn"}),
            _declare({"type": "task_started", "turn_id": "new-turn"}),
            _declare({"turn_id": "new-turn", "model": "gpt-6.1-sol"}, "turn_context"),
            _usage(3),
        ],
    )
    assert [row.model for row in parsed] == ["gpt-6-luna", "gpt-6-luna", "gpt-6.1-sol"]


def test_delayed_receipt_uses_its_turn_model_after_next_context(tmp_path):
    parsed = _read(
        tmp_path,
        [
            _declare({"turn_id": "old-turn", "model": "model-old"}, "turn_context"),
            _declare({"type": "task_complete", "turn_id": "old-turn"}),
            _declare({"turn_id": "new-turn", "model": "model-new"}, "turn_context"),
            _declare({"turn_id": "old-turn"}, "token_usage_record"),
            _usage(1),
            _usage(2),
        ],
    )
    assert [row.model for row in parsed] == ["model-old", "model-new"]
