"""`coord route` existed; an agent could not ask it anything.

The usage-headroom advisor was reachable only from a shell, so the one question
a coordination server should be able to answer about itself -- which lane has
room right now -- had to be relayed through the operator. This adds the MCP face
of the same function, and the tests here are about the two things that make
exposing it safe rather than about the recommendation.

**It reads.** ``UsageLedger`` initializes an empty ledger at any path it is
handed, so a mistyped path would otherwise mint a second, empty accounting store
and then route off it -- confidently, because an empty ledger has no rows to
contradict. The tool refuses a path that does not exist, and leaves what an
existing ledger SAYS unchanged when it does -- asserted over a dump of its rows
rather than the file, because opening any SQLite database touches WAL
bookkeeping that has nothing to do with the accounting.

**Incomplete coverage stays a refusal.** ``summarize_rows`` records an absent
``coverage_state`` as ``"unknown"`` and treats anything that is not
``"complete"`` as incomplete; ``ProviderUsage.complete`` then demands every
expected day present AND every observation complete. That strictness is passed
through, not re-derived: a caller reading one field gets ``unknown``, never a
total that happens to be a floor.
"""

from __future__ import annotations

import datetime
import hashlib
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.coord import mcp_coord_server  # noqa: E402
from coordharness.usage import DailyUsage, ObservationInput, SourceInstance, UsageLedger  # noqa: E402

CAPTURED_AT = "2026-08-11T12:00:00+00:00"
WINDOW_DAYS = 7


def _write_week(
    ledger: UsageLedger,
    provider: str,
    *,
    digest_seed: str,
    per_day: int,
    days: int = WINDOW_DAYS,
    coverage_state: str = "complete",
    today: datetime.date,
) -> None:
    """One provider's week, ending today so it lands inside the default window."""
    source_id = ledger.register_source(
        SourceInstance(
            provider=provider,
            account_key="acct",
            source_kind="route_fixture",
            root_uri=f"fixture://{provider}",
            timezone_name="UTC",
            root_identity_digest="1" * 64,
            label=f"{provider}@example.test",
        )
    )
    complete = coverage_state == "complete"
    rows = [
        DailyUsage(
            usage_date=(today - datetime.timedelta(days=offset)).isoformat(),
            model="m",
            input_tokens=per_day,
            output_tokens=0,
        )
        for offset in range(days)
    ]
    observation_id, _ = ledger.record_observation(
        source_id,
        ObservationInput(
            artifact_digest=digest_seed * 64,
            artifact_version="route-fixture-v1",
            producer_key="route-fixture",
            parser_version="route-fixture-v1",
            pricing_key="fixture",
            captured_at=CAPTURED_AT,
            coverage_state=coverage_state,
            observation_role="raw",
            canonical_eligible=True,
            coverage_start=f"{today}T00:00:00+00:00" if complete else None,
            coverage_end=f"{today}T23:59:59+00:00" if complete else None,
            source_manifest_digest="f" * 64 if complete else None,
            files_scanned=1 if complete else None,
            records_scanned=len(rows) if complete else None,
            records_accepted=len(rows) if complete else None,
            records_rejected=0,
            records_conflicted=0,
            parse_error_count=0,
            rows=rows,
        ),
    )
    # allow_partial mirrors what an importer must say out loud to canonicalize
    # an incomplete observation; the fixture has to say it too, or the partial
    # week never reaches corrected_rows() and the coverage test is vacuous.
    ledger.select_canonical(
        observation_id, reason="route fixture", allow_partial=not complete
    )


@pytest.fixture
def today() -> datetime.date:
    return datetime.date.today()


@pytest.fixture
def full_ledger(tmp_path: Path, today: datetime.date) -> Path:
    """Two providers, both fully covered, with codex plainly the emptier lane."""
    path = tmp_path / "usage.sqlite"
    with UsageLedger(path) as ledger:
        _write_week(ledger, "claude", digest_seed="a", per_day=1_000, today=today)
        _write_week(ledger, "codex", digest_seed="b", per_day=100, today=today)
    return path


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        names = [
            str(name)
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in sorted(names)
        }
    finally:
        conn.close()


def _content_digest(path: Path) -> str:
    """A hash of the ledger's rows, not of the file.

    The file itself carries WAL bookkeeping that any open touches; what must not
    change is what the ledger says.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# It advises
# --------------------------------------------------------------------------


def test_the_tool_recommends_the_lane_with_headroom(full_ledger: Path) -> None:
    advice = mcp_coord_server._tool_route(
        usage_db=str(full_ledger),
        budgets={"claude": 10_000, "codex": 10_000},
        days=WINDOW_DAYS,
    )

    assert advice["recommended"] == "codex"
    assert advice["confident"] is True
    assert advice["coverage_state"] == "complete"
    assert advice["read_only"] is True
    assert advice["lifecycle_mutation"] is False
    # The evidence rides along with the recommendation, as the CLI's does.
    assert {verdict["provider"] for verdict in advice["verdicts"]} == {
        "claude",
        "codex",
    }
    assert advice["schema_version"] == "UsageRoutingAdviceV1"


def test_budgets_may_be_given_as_provider_equals_tokens(full_ledger: Path) -> None:
    advice = mcp_coord_server._tool_route(
        usage_db=str(full_ledger),
        budgets=["claude=10000", "codex=10000"],
        days=WINDOW_DAYS,
    )

    assert advice["recommended"] == "codex"
    assert advice["budgets"] == {"claude": 10_000, "codex": 10_000}


def test_a_budget_that_is_not_a_token_count_is_refused(full_ledger: Path) -> None:
    with pytest.raises(ValueError, match="PROVIDER=TOKENS"):
        mcp_coord_server._tool_route(
            usage_db=str(full_ledger), budgets={"claude": "lots"}
        )


# --------------------------------------------------------------------------
# It refuses rather than guesses
# --------------------------------------------------------------------------


def test_incomplete_coverage_returns_unknown_not_a_total(
    tmp_path: Path, today: datetime.date
) -> None:
    """A partial week sums to a smaller number than the truth. Routing on it
    would send work to the lane that is *most* used."""
    path = tmp_path / "usage.sqlite"
    with UsageLedger(path) as ledger:
        _write_week(
            ledger, "claude", digest_seed="a", per_day=1_000,
            coverage_state="partial", today=today,
        )
        _write_week(ledger, "codex", digest_seed="b", per_day=100, today=today)

    advice = mcp_coord_server._tool_route(
        usage_db=str(path),
        budgets={"claude": 10_000, "codex": 10_000},
        days=WINDOW_DAYS,
    )

    assert advice["coverage_state"] == "unknown"
    assert any("floor, not a total" in caveat for caveat in advice["caveats"])


def test_a_missing_day_is_incomplete_even_when_every_row_reads_complete(
    tmp_path: Path, today: datetime.date
) -> None:
    """The missing day is exactly the one that could put a lane over its limit."""
    path = tmp_path / "usage.sqlite"
    with UsageLedger(path) as ledger:
        _write_week(
            ledger, "claude", digest_seed="a", per_day=1_000, days=5, today=today
        )
        _write_week(ledger, "codex", digest_seed="b", per_day=100, today=today)

    advice = mcp_coord_server._tool_route(
        usage_db=str(path),
        budgets={"claude": 10_000, "codex": 10_000},
        days=WINDOW_DAYS,
    )

    assert advice["coverage_state"] == "unknown"


def test_require_complete_excludes_the_incomplete_provider(
    tmp_path: Path, today: datetime.date
) -> None:
    """The stricter mode is passed through exactly as the CLI passes it."""
    path = tmp_path / "usage.sqlite"
    with UsageLedger(path) as ledger:
        _write_week(
            ledger, "claude", digest_seed="a", per_day=1_000,
            coverage_state="partial", today=today,
        )
        _write_week(ledger, "codex", digest_seed="b", per_day=100, today=today)

    advice = mcp_coord_server._tool_route(
        usage_db=str(path),
        budgets={"claude": 10_000, "codex": 10_000},
        days=WINDOW_DAYS,
        require_complete=True,
    )

    excluded = {
        verdict["provider"]: verdict
        for verdict in advice["verdicts"]
        if not verdict["eligible"]
    }
    assert "claude" in excluded
    assert "complete coverage was required" in excluded["claude"]["reason"]
    assert advice["recommended"] == "codex"


def test_no_declared_budget_is_a_refusal_not_a_default(full_ledger: Path) -> None:
    """Headroom is meaningless without a limit, and "use the usual one" is
    indistinguishable from a real recommendation at the call site.

    The two refusals stay separate on purpose. Coverage here IS complete, and
    saying otherwise would blame the ledger for a missing argument; what is
    absent is the budget, and that is what the caveat names.
    """
    advice = mcp_coord_server._tool_route(usage_db=str(full_ledger), days=WINDOW_DAYS)

    assert advice["recommended"] is None
    assert advice["confident"] is False
    assert advice["coverage_state"] == "complete"
    assert "no declared budgets" in advice["caveats"]
    assert all(
        verdict["reason"] == "no declared budget for this provider"
        for verdict in advice["verdicts"]
    )


def test_an_empty_window_recommends_nothing(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite"
    with UsageLedger(path):
        pass

    advice = mcp_coord_server._tool_route(
        usage_db=str(path), budgets={"claude": 10_000}, days=WINDOW_DAYS
    )

    assert advice["recommended"] is None
    assert advice["coverage_state"] == "unknown"
    assert "no observations in window" in advice["caveats"]


# --------------------------------------------------------------------------
# It writes nothing
# --------------------------------------------------------------------------


def test_a_ledger_that_does_not_exist_is_refused_and_not_created(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "typo.sqlite"

    with pytest.raises(ValueError, match="will not create one"):
        mcp_coord_server._tool_route(usage_db=str(absent), budgets={"claude": 10})

    assert not absent.exists()


def test_an_empty_usage_db_argument_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path of an existing usage ledger"):
        mcp_coord_server._tool_route(usage_db="")


def test_advising_does_not_change_what_the_ledger_says(full_ledger: Path) -> None:
    before_counts = _table_counts(full_ledger)
    before_digest = _content_digest(full_ledger)

    mcp_coord_server._tool_route(
        usage_db=str(full_ledger),
        budgets={"claude": 10_000, "codex": 10_000},
        days=WINDOW_DAYS,
    )

    assert _table_counts(full_ledger) == before_counts
    assert _content_digest(full_ledger) == before_digest


def test_the_route_tool_is_in_the_served_catalog() -> None:
    catalog = mcp_coord_server._server_tool_catalog(env={})

    assert "route" in catalog["visible"]
