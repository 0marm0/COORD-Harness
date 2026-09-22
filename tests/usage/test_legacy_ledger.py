"""Frozen import of pre-transcript Codex days from a third-party ledger.

``~/.codex/sessions`` begins the day Codex started keeping rollouts, so spend
before that is unrecoverable from transcripts. The only record left is a
CodexBar-derived high-water JSON whose accounting is both foreign and known
wrong for Codex -- it counted the cached input prefix a second time at the
full input rate, so its cost overstates.

These tests pin the properties that make importing it safe anyway: a
self-computed day always wins, the two kinds stay distinguishable all the way
into the dashboard payload, the import is idempotent and reversible, and
nothing here writes to the source document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.usage.ledger import DailyUsage
from coordharness.usage.legacy_ledger import (
    CLAUDE_LAYOUT,
    CLAUDE_LEGACY_SEMANTICS,
    CLAUDE_LEGACY_SOURCE,
    CLAUDE_LEGACY_WARNING,
    LEGACY_SEMANTICS,
    LEGACY_SOURCE,
    LEGACY_WARNING,
    LegacyLedgerError,
    legacy_ledger_days,
    read_legacy_ledger,
)
from coordharness.usage.local_service import _history
from coordharness.usage.scan_store import SCHEMA_VERSION, UsageScanStore

pytestmark = pytest.mark.unit

_CUTOFF = "2026-07-17"


def _ledger(path: Path, days: dict | None = None) -> Path:
    document = {
        "schema": "example.codex-usage-high-water.v1",
        "accounting": "provider_volume_anchored_local_mix_estimate",
        "pricing_key": "models-dev-v1-abc",
        "total_cost_nanos": 0,
        "days": days
        if days is not None
        else {
            # [input_inclusive_of_cache, cached, output, cost_nanos]
            "2026-03-20": {"gpt-5.4": [1000, 400, 50, 7_000_000_000]},
            "2026-07-16": {"gpt-5.5": [2000, 900, 60, 9_000_000_000]},
            # On or after the cutoff: this machine computes these itself.
            "2026-07-17": {"gpt-5.6-sol": [4000, 1000, 70, 11_000_000_000]},
            "2026-08-01": {"gpt-5.6-sol": [5000, 1000, 80, 13_000_000_000]},
        },
    }
    path.write_text(json.dumps(document))
    return path


def _read(tmp_path: Path, **kwargs):
    return read_legacy_ledger(_ledger(tmp_path / "legacy.json"), before=_CUTOFF, **kwargs)


# -- reading the source ----------------------------------------------------


def test_only_days_before_the_cutoff_are_imported(tmp_path: Path) -> None:
    read = _read(tmp_path)
    assert [row.usage_date for row in read.rows] == ["2026-03-20", "2026-07-16"]
    assert read.first_day == "2026-03-20"
    assert read.last_day == "2026-07-16"
    assert read.days_available == 4
    assert read.days_selected == 2
    assert read.rows_skipped == 0


def test_inclusive_input_is_restated_without_changing_the_token_total(
    tmp_path: Path,
) -> None:
    """The ledger's input includes the cached prefix; ours does not.

    Reshaping it lets one set of column names describe both kinds of day. The
    token total must survive that reshaping untouched.
    """

    row = _read(tmp_path).rows[0]
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (600, 400, 50)
    assert row.input_tokens + row.cache_read_tokens + row.output_tokens == 1000 + 50


def test_the_upstream_cost_is_carried_verbatim_and_labelled_as_overstating(
    tmp_path: Path,
) -> None:
    """Repricing it at our rates would invent a figure the ledger never asserted."""

    read = _read(tmp_path)
    assert [row.api_rate_estimate_nanos for row in read.rows] == [
        7_000_000_000,
        9_000_000_000,
    ]
    provenance = read.provenance()
    assert provenance["semantics"] == LEGACY_SEMANTICS
    assert provenance["self_computed"] is False
    assert provenance["canonical"] is False
    assert provenance["frozen"] is True
    assert "overstate" in provenance["warning"]
    assert provenance["upstream_accounting"] == "provider_volume_anchored_local_mix_estimate"


def test_a_foreign_or_broken_document_is_refused_rather_than_guessed(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(missing, before=_CUTOFF)
    not_json = tmp_path / "bad.json"
    not_json.write_text("{ not json")
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(not_json, before=_CUTOFF)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema": "something.else.v9", "days": {}}))
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(wrong, before=_CUTOFF)
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(_ledger(tmp_path / "ok.json"), before="not-a-date")


def test_malformed_entries_are_skipped_without_losing_the_good_ones(
    tmp_path: Path,
) -> None:
    path = _ledger(
        tmp_path / "mixed.json",
        days={
            "2026-03-20": {"gpt-5.4": [1000, 400, 50, 7_000_000_000]},
            "2026-03-21": {"gpt-5.4": [1000, 400]},
            "2026-03-22": {"gpt-5.4": [1000, 400, 50, -1]},
            "not-a-day": {"gpt-5.4": [1, 1, 1, 1]},
        },
    )
    read = read_legacy_ledger(path, before=_CUTOFF)
    assert [row.usage_date for row in read.rows] == ["2026-03-20"]
    assert read.rows_skipped == 3


def test_reading_never_writes_to_the_source_document(tmp_path: Path) -> None:
    path = _ledger(tmp_path / "legacy.json")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    read_legacy_ledger(path, before=_CUTOFF)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# -- storing it ------------------------------------------------------------


def _store_with_self_computed(tmp_path: Path) -> UsageScanStore:
    """A store holding one self-computed Codex day, 2026-07-17."""

    root = tmp_path / ".codex"
    path = root / "sessions" / "rollout-2026-07-17T12-00-00-01a0-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-17T12:00:00.000Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"id": "01a0-a", "model": "gpt-5.6-sol"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-07-17T12:00:01.000Z",
                "ordinal": 1,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 500,
                            "cached_input_tokens": 100,
                            "output_tokens": 25,
                            "total_tokens": 525,
                        }
                    },
                },
            }
        )
        + "\n"
    )
    store = UsageScanStore(tmp_path / "scan.sqlite")
    store.scan(root, provider="codex")
    return store


def test_import_is_idempotent(tmp_path: Path) -> None:
    read = _read(tmp_path)
    with _store_with_self_computed(tmp_path) as store:
        first = store.import_legacy("codex", source=read.source, rows=read.rows)
        assert (first.rows_inserted, first.rows_updated, first.rows_stored) == (2, 0, 2)
        second = store.import_legacy("codex", source=read.source, rows=read.rows)
        assert (second.rows_inserted, second.rows_updated, second.rows_stored) == (0, 2, 2)
        assert len(store.legacy_totals("codex")) == 2


def test_a_self_computed_day_always_beats_a_legacy_one(tmp_path: Path) -> None:
    """The store must not serve a legacy row for a day it can compute itself."""

    with _store_with_self_computed(tmp_path) as store:
        # Offer a legacy row for the day the transcripts already cover.
        store.import_legacy(
            "codex",
            source=LEGACY_SOURCE,
            rows=(
                DailyUsage(
                    usage_date="2026-07-17",
                    model="gpt-5.4",
                    input_tokens=999_999,
                    api_rate_estimate_nanos=99_000_000_000,
                ),
                DailyUsage(usage_date="2026-03-20", model="gpt-5.4", input_tokens=600),
            ),
        )
        served = store.legacy_totals("codex")
        assert [row.usage_date for row in served] == ["2026-03-20"]
        counts = store.legacy_counts("codex")
        assert counts["rows_stored"] == 2
        assert counts["days_served"] == 1
        assert counts["days_superseded_by_self_computed"] == 1
        # The self-computed day is untouched and still says 525 tokens.
        computed = {row.usage_date: row for row in store.totals("codex")}
        assert computed["2026-07-17"].input_tokens == 400
        assert computed["2026-07-17"].cache_read_tokens == 100


def test_totals_keeps_its_existing_contract_and_extends_by_request(
    tmp_path: Path,
) -> None:
    read = _read(tmp_path)
    with _store_with_self_computed(tmp_path) as store:
        store.import_legacy("codex", source=read.source, rows=read.rows)
        assert [row.usage_date for row in store.totals("codex")] == ["2026-07-17"]
        merged = store.totals("codex", include_legacy=True)
        assert [row.usage_date for row in merged] == [
            "2026-03-20",
            "2026-07-16",
            "2026-07-17",
        ]


def test_dropping_legacy_rows_leaves_self_computed_rows_alone(tmp_path: Path) -> None:
    read = _read(tmp_path)
    with _store_with_self_computed(tmp_path) as store:
        store.import_legacy("codex", source=read.source, rows=read.rows)
        assert store.drop_legacy("codex", source=read.source) == 2
        assert store.legacy_totals("codex") == ()
        assert [row.usage_date for row in store.totals("codex")] == ["2026-07-17"]
        assert store.legacy_counts("codex")["rows_stored"] == 0
        # Dropping again is not an error, and still touches nothing else.
        assert store.drop_legacy("codex") == 0
        assert [row.usage_date for row in store.totals("codex")] == ["2026-07-17"]


def test_history_import_hands_the_two_kinds_over_separately(tmp_path: Path) -> None:
    read = _read(tmp_path)
    root = tmp_path / ".codex"
    with _store_with_self_computed(tmp_path) as store:
        store.import_legacy("codex", source=read.source, rows=read.rows)
        imported = store.history_import(root, provider="codex")
    assert imported is not None
    assert [row.usage_date for row in imported.rows] == ["2026-07-17"]
    assert [row.usage_date for row in imported.legacy_rows] == ["2026-03-20", "2026-07-16"]
    provenance = imported.provenance()["legacy"]
    assert provenance["semantics"] == LEGACY_SEMANTICS
    assert provenance["cost_bias"] == "overstates"
    assert provenance["self_computed"] is False


# -- reaching the payload --------------------------------------------------


def test_the_payload_labels_every_day_and_never_reprices_a_legacy_one(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    read = _read(tmp_path)
    root = tmp_path / ".codex"
    with _store_with_self_computed(tmp_path) as store:
        store.import_legacy("codex", source=read.source, rows=read.rows)
        imported = store.history_import(root, provider="codex")
    history = _history(imported, datetime(2026, 9, 21, tzinfo=timezone.utc))
    by_day = {row["date"]: row for row in history["daily"]}
    assert by_day["2026-03-20"]["provenance"] == "legacy_import"
    assert by_day["2026-07-16"]["provenance"] == "legacy_import"
    assert by_day["2026-07-17"]["provenance"] == "self_computed"
    # The legacy cost is the ledger's own, carried through untouched.
    assert by_day["2026-03-20"]["api_rate_estimate_nanos"] == 7_000_000_000
    coverage = history["legacy_coverage"]
    assert coverage["days"] == 2
    assert coverage["self_computed_days"] == 1
    assert coverage["total_tokens"] == 1050 + 2060
    assert coverage["api_rate_estimate_nanos"] == 16_000_000_000
    assert coverage["semantics"] == LEGACY_SEMANTICS
    assert "overstate" in coverage["warning"]


def test_the_labels_survive_the_public_proxy(tmp_path: Path) -> None:
    """The proxy rebuilds the document from an allowlist, so it must allow these.

    Without this the distinction dies at the boundary and the UI is handed a
    continuous series it cannot label -- the exact failure this import must
    not cause.
    """

    from tests.usage.test_dashboard_proxy import _payload
    from coordharness.usage.dashboard_proxy import validate_usage_dashboard

    payload = _payload()
    history = payload["providers"]["claude"]["history"]
    history["daily"] = [
        {"date": "2026-03-20", "total_tokens": 1050, "provenance": "legacy_import"},
        {"date": "2026-08-26", "total_tokens": 123, "provenance": "self_computed"},
    ]
    history["legacy_coverage"] = {
        "semantics": LEGACY_SEMANTICS,
        # The real constant, not a stand-in: the proxy's bounded-text pattern
        # caps warnings at 240 characters, and one it refuses is one the UI
        # never shows. An earlier draft of this warning was 242.
        "warning": LEGACY_WARNING,
        "cost_bias": "overstates",
        "days": 1,
        "self_computed_days": 1,
        "total_tokens": 1050,
        "api_rate_estimate_nanos": 7_000_000_000,
        "first_day": "2026-03-20",
        "last_day": "2026-03-20",
        "canonical": False,
        "self_computed": False,
        "frozen": True,
        # Internal keys that must NOT cross the boundary.
        "sources": [LEGACY_SOURCE],
        "upstream_pricing_key": "models-dev-v1-abc",
    }
    clean = validate_usage_dashboard(payload)["providers"]["claude"]["history"]
    assert [row.get("provenance") for row in clean["daily"]] == [
        "legacy_import",
        "self_computed",
    ]
    coverage = clean["legacy_coverage"]
    assert coverage["semantics"] == LEGACY_SEMANTICS
    assert coverage["cost_bias"] == "overstates"
    assert coverage["frozen"] is True
    assert coverage["self_computed"] is False
    assert coverage["api_rate_estimate_nanos"] == 7_000_000_000
    assert "sources" not in coverage
    assert "upstream_pricing_key" not in coverage


def test_a_dashboard_with_no_legacy_import_looks_exactly_as_it_did(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    root = tmp_path / ".codex"
    with _store_with_self_computed(tmp_path) as store:
        imported = store.history_import(root, provider="codex")
    history = _history(imported, datetime(2026, 9, 21, tzinfo=timezone.utc))
    assert history["legacy_coverage"] is None
    assert imported.provenance()["legacy"] is None
    assert [row["provenance"] for row in history["daily"]] == ["self_computed"]


# -- the Claude ledger: an interior gap rather than a floor -----------------
#
# `~/.claude/projects` covers 2026-02-27..2026-09-21, but three days inside
# that range hold no surviving records at all, so the Codex rule -- "import
# everything before the transcripts begin" -- cannot express what needs
# importing. These tests pin the second selector and the second packing.


# Copied verbatim from the real ledger, index order included, so these rows
# also document the packing rather than merely exercising it.
_CLAUDE_DAYS = {
    # [input, cache_read, cache_creation, output, cost_nanos, reqs, reqs, derived]
    "2026-04-29": {"claude-opus-4-7": [5865, 159789180, 3903702, 984717, 143471190000,
                                       326, 326, 3874990]},
    "2026-05-01": {"claude-opus-4-7": [149, 22717328, 1293640, 209582, 29535359000,
                                       84, 84, 1293640]},
    # A day the transcripts DO cover, to prove the selector leaves it behind.
    "2026-09-21": {"claude-opus-5": [1, 2, 3, 4, 5_000_000_000, 1, 1, 0]},
}


def _claude_ledger(path: Path, days: dict | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "example.claude-usage-high-water.v1",
                "policy": "component-wise maximum across every preserved observation",
                "days": days if days is not None else _CLAUDE_DAYS,
            }
        )
    )
    return path


def test_the_claude_layout_unpacks_cache_write_and_output_the_right_way_round(
    tmp_path: Path,
) -> None:
    """Index 2 is the cache write and index 3 the output, not the reverse.

    Swapping them still reproduces the document's own total_tokens, so only a
    component-for-component match against a real day distinguishes them -- and
    getting it backwards would misprice the day by roughly 4x.
    """

    read = read_legacy_ledger(
        _claude_ledger(tmp_path / "claude.json"),
        only_days=["2026-04-29"],
        layout=CLAUDE_LAYOUT,
    )
    assert len(read.rows) == 1
    row = read.rows[0]
    assert row.usage_date == "2026-04-29"
    assert row.model == "claude-opus-4-7"
    assert row.input_tokens == 5865
    assert row.cache_read_tokens == 159789180
    assert row.cache_create_other_tokens == 3903702
    assert row.output_tokens == 984717
    # The ledger's own cost, carried through and never recomputed.
    assert row.api_rate_estimate_nanos == 143471190000
    # Claude's input is already exclusive of the cached prefix, unlike Codex's,
    # so nothing is subtracted from it.
    assert row.input_tokens + row.cache_read_tokens == 5865 + 159789180


def test_only_days_imports_exactly_the_named_days(tmp_path: Path) -> None:
    read = read_legacy_ledger(
        _claude_ledger(tmp_path / "claude.json"),
        only_days=["2026-05-01", "2026-04-29"],
        layout=CLAUDE_LAYOUT,
    )
    assert sorted({row.usage_date for row in read.rows}) == ["2026-04-29", "2026-05-01"]
    assert read.days_available == 3
    assert read.days_selected == 2
    assert read.selected_days == ("2026-04-29", "2026-05-01")
    assert read.source == CLAUDE_LEGACY_SOURCE
    # The provenance says which rule admitted these rows, and does not imply a
    # cutoff that was never applied.
    provenance = read.provenance()
    assert provenance["imported_before"] == ""
    assert provenance["imported_days"] == ["2026-04-29", "2026-05-01"]
    assert provenance["semantics"] == CLAUDE_LEGACY_SEMANTICS
    assert provenance["warning"] == CLAUDE_LEGACY_WARNING


def test_exactly_one_selector_is_required(tmp_path: Path) -> None:
    path = _claude_ledger(tmp_path / "claude.json")
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(path, layout=CLAUDE_LAYOUT)
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(
            path, before="2026-04-29", only_days=["2026-04-29"], layout=CLAUDE_LAYOUT
        )
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(path, only_days=[], layout=CLAUDE_LAYOUT)
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(path, only_days=["nonsense"], layout=CLAUDE_LAYOUT)


def test_each_ledger_is_refused_under_the_other_layout(tmp_path: Path) -> None:
    """A packing mismatch is a schema mismatch, and is caught as one."""

    claude = _claude_ledger(tmp_path / "claude.json")
    codex = _ledger(tmp_path / "codex.json")
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(claude, before=_CUTOFF)
    with pytest.raises(LegacyLedgerError):
        read_legacy_ledger(codex, only_days=["2026-03-20"], layout=CLAUDE_LAYOUT)


def test_a_claude_entry_too_short_for_its_layout_is_skipped_not_padded(
    tmp_path: Path,
) -> None:
    path = _claude_ledger(
        tmp_path / "claude.json",
        days={
            "2026-04-29": {
                "claude-opus-4-7": [1, 2, 3],  # no output, no cost
                "claude-sonnet-5": [1, 2, 3, 4, 5_000_000_000],
            }
        },
    )
    read = read_legacy_ledger(path, only_days=["2026-04-29"], layout=CLAUDE_LAYOUT)
    assert [row.model for row in read.rows] == ["claude-sonnet-5"]
    assert read.rows_skipped == 1


def test_legacy_ledger_days_lists_what_a_caller_may_select(tmp_path: Path) -> None:
    path = _claude_ledger(tmp_path / "claude.json")
    assert legacy_ledger_days(path, layout=CLAUDE_LAYOUT) == (
        "2026-04-29",
        "2026-05-01",
        "2026-09-21",
    )


def test_a_claude_cache_write_survives_the_store_round_trip(tmp_path: Path) -> None:
    """The cache-write column is why the store needed a schema past 2.

    Dropping it would let an imported day understate its own token count while
    still reporting the ledger's full cost.
    """

    read = read_legacy_ledger(
        _claude_ledger(tmp_path / "claude.json"),
        only_days=["2026-04-29"],
        layout=CLAUDE_LAYOUT,
    )
    with UsageScanStore(tmp_path / "scan.sqlite") as store:
        store.import_legacy("claude", source=read.source, rows=read.rows)
        served = store.legacy_totals("claude")
        assert len(served) == 1
        assert served[0].cache_create_other_tokens == 3903702
        assert served[0].cache_read_tokens == 159789180
        assert served[0].output_tokens == 984717
        assert served[0].api_rate_estimate_nanos == 143471190000
        # Claude rows must not be labelled with the Codex ledger's reason.
        provenance = store.legacy_provenance("claude")
        assert provenance["semantics"] == CLAUDE_LEGACY_SEMANTICS
        assert provenance["warning"] == CLAUDE_LEGACY_WARNING
        # Not "overstates". The Claude ledger's high-water rule is a
        # component-wise maximum, which is the CORRECT representative for
        # Claude Code's per-content-block streaming records -- the same rule
        # this project's own scan now uses. Only Codex's ledger has a measured
        # direction, from its cached-token double-count.
        assert provenance["cost_bias"] == "indeterminate"
        assert len(CLAUDE_LEGACY_WARNING) <= 240
        assert provenance["frozen"] is True


def test_a_store_written_by_schema_2_gains_the_column_without_losing_rows(
    tmp_path: Path,
) -> None:
    """The migration is additive, so an existing store keeps what it holds."""

    import sqlite3

    path = tmp_path / "scan.sqlite"
    with UsageScanStore(path) as store:
        store.import_legacy(
            "codex",
            source=LEGACY_SOURCE,
            rows=(DailyUsage(usage_date="2026-03-20", model="gpt-5.4", input_tokens=7),),
        )
    # Put the store back into its schema-2 shape.
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE legacy_daily DROP COLUMN cache_create_other_tokens")
    raw.execute("PRAGMA user_version=2")
    raw.commit()
    raw.close()

    with UsageScanStore(path) as store:
        served = store.legacy_totals("codex")
        assert [row.usage_date for row in served] == ["2026-03-20"]
        assert served[0].input_tokens == 7
        assert served[0].cache_create_other_tokens == 0
        # And the migrated store now accepts a row that carries a cache write.
        store.import_legacy(
            "claude",
            source=CLAUDE_LEGACY_SOURCE,
            rows=(
                DailyUsage(
                    usage_date="2026-04-29",
                    model="claude-opus-4-7",
                    cache_create_other_tokens=984717,
                ),
            ),
        )
        assert store.legacy_totals("claude")[0].cache_create_other_tokens == 984717
    assert (
        sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    )


def test_the_schema_check_reads_the_format_not_the_publisher(tmp_path: Path) -> None:
    """Any ``<publisher>.<format>`` of the right format imports; nothing else does.

    The header sums are what prove a file readable; the schema only stops the
    importer being pointed at the wrong kind of file.
    """

    for schema in ("codex-usage-high-water.v1", "someone-else.codex-usage-high-water.v1"):
        path = _ledger(tmp_path / f"{schema}.json")
        document = json.loads(path.read_text())
        document["schema"] = schema
        path.write_text(json.dumps(document))
        assert read_legacy_ledger(path, before=_CUTOFF).rows

    for schema in ("example.claude-usage-high-water.v1", "codex-usage-high-water.v2", "xcodex-usage-high-water.v1"):
        path = _ledger(tmp_path / f"wrong-{schema}.json")
        document = json.loads(path.read_text())
        document["schema"] = schema
        path.write_text(json.dumps(document))
        with pytest.raises(LegacyLedgerError):
            read_legacy_ledger(path, before=_CUTOFF)


def test_there_is_no_default_ledger_location() -> None:
    """A guessed path would tie the importer to one producer's disk layout."""

    with pytest.raises(LegacyLedgerError, match="name the legacy ledger"):
        read_legacy_ledger(None, before=_CUTOFF)
