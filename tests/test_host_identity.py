"""A pid only means something on the machine that recorded it.

``runs.pid`` and ``agent_sessions.pid`` carried no host identity, so every
liveness probe answered a question it could not actually see: ``os.kill(pid, 0)``
on machine A says nothing about a run on machine B, and what it says instead is
"dead". These tests pin the three parts of the smallest fix (docs/roadmap.md
"Near", docs/ideas.md "Multi-machine coordination"): the column exists on a
fresh database and arrives on an existing one without disturbing its rows, new
writes stamp it, and a run recorded on a foreign host is reported UNKNOWN by the
pid path rather than dead.

A follow-up closes a second gap in that same fix: `socket.gethostname()` is
not itself stable. A macOS Bonjour/DHCP rename changes it, so every row this
machine wrote before the rename starts reading as foreign the moment after
it -- every PID probe for those rows switches off, and a dead local run can
read as running indefinitely. `stable_host_id()` persists a random id once,
as a file in the same state directory that holds `coord.db`, and new writes
stamp that instead; `pid_liveness_is_meaningful` now reads a row as local
when its `host_id` matches either the current stable id (new writes) or the
current `socket.gethostname()` (writes made before this existed). The tests
below in "stable_host_id()" and the two new cases under "Liveness" pin that.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
import stat
import time
import uuid
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db, create_schema
from coordharness.coord.config import connect

LANE_SESSION = "claude:host-identity-fixture"
FOREIGN_HOST = "not-this-machine.invalid"
# A pid no process holds: the probe must come back dead for the local control,
# so the only thing separating it from the foreign case is host_id.
DEAD_PID = 4_194_303
AT = time.time()


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# The migration


def test_a_fresh_database_carries_host_id_on_both_pid_tables(project: Path) -> None:
    database = project / "coord.db"
    report = bootstrap_database(database)
    assert "004_host_identity.sql" in report["migrations_applied"]
    conn = connect(database)
    try:
        assert "host_id" in _columns(conn, "runs")
        assert "host_id" in _columns(conn, "agent_sessions")
        assert {"ix_runs_host", "ix_sessions_host"} <= _indexes(conn)
    finally:
        conn.close()


def test_host_id_is_nullable_so_pre_migration_rows_stay_legal(project: Path) -> None:
    """NULL is "not stated", which the liveness path reads as local.

    A NOT NULL column would have made this migration touch live data, which is
    exactly the retrofit cost the roadmap item exists to avoid.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        for table in ("runs", "agent_sessions"):
            notnull = {
                row[1]: row[3]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert notnull["host_id"] == 0, table
    finally:
        conn.close()


def test_the_migration_reaches_an_existing_database_without_losing_rows(
    project: Path,
) -> None:
    """Bootstrap an old-shaped database, then bring it forward.

    The pre-migration shape is reconstructed rather than fixtured: the columns
    and indexes are dropped and the migration row deleted, so the second
    bootstrap runs against a database that genuinely lacks them while carrying
    rows a real deployment would have.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        coord_db.upsert_work(
            conn,
            "HOST-001",
            title="a row written before host_id existed",
            assignee="claude",
            module="host",
            surface="job",
            done_signal="artifacts/host-001.json",
            acceptance_json='["the row survives the migration"]',
            intent_state="queued",
        )
        run_id = coord_db.appear_run(
            conn, work_id="HOST-001", session_id=LANE_SESSION, runner_kind="local"
        )
    finally:
        conn.close()

    raw = sqlite3.connect(database)
    try:
        raw.execute("DROP INDEX IF EXISTS ix_runs_host")
        raw.execute("DROP INDEX IF EXISTS ix_sessions_host")
        raw.execute("ALTER TABLE runs DROP COLUMN host_id")
        raw.execute("ALTER TABLE agent_sessions DROP COLUMN host_id")
        raw.execute(
            "DELETE FROM schema_migrations WHERE name='004_host_identity.sql'"
        )
        raw.commit()
        assert "host_id" not in {
            r[1] for r in raw.execute("PRAGMA table_info(runs)").fetchall()
        }
    finally:
        raw.close()

    report = bootstrap_database(database)
    assert "004_host_identity.sql" in report["migrations_applied"]
    conn = connect(database)
    try:
        assert "host_id" in _columns(conn, "runs")
        assert "host_id" in _columns(conn, "agent_sessions")
        assert {"ix_runs_host", "ix_sessions_host"} <= _indexes(conn)
        # The pre-existing rows are still there, and unstated rather than
        # guessed at: nothing backfills a host onto a row this machine may not
        # have written.
        assert conn.execute(
            "SELECT host_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT host_id FROM agent_sessions WHERE session_id=?", (LANE_SESSION,)
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE work_id='HOST-001'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_schema_alone_still_yields_a_writable_runs_table(project: Path) -> None:
    """coord/modeld_lite.py and runners/mlx_runner.py call apply_schema directly.

    They never run the migration runner, so a host_id added only by the
    migration file would be missing from the databases they create and every
    run write against one would fail.
    """
    database = project / "direct.db"
    create_schema.apply_schema(database)
    conn = connect(database)
    try:
        assert "host_id" in _columns(conn, "runs")
        assert "host_id" in _columns(conn, "agent_sessions")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# New writes


def test_new_sessions_and_runs_are_stamped_with_the_local_host(project: Path) -> None:
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        coord_db.upsert_work(
            conn,
            "HOST-002",
            title="a stamped row",
            assignee="claude",
            module="host",
            surface="job",
            done_signal="artifacts/host-002.json",
            acceptance_json='["the write carries a host"]',
            intent_state="queued",
        )
        run_id = coord_db.appear_run(
            conn, work_id="HOST-002", session_id=LANE_SESSION, runner_kind="local"
        )
        # Rows stamp the rename-stable id, not the (rename-fragile) hostname.
        expected = coord_db.stable_host_id()
        assert conn.execute(
            "SELECT host_id FROM agent_sessions WHERE session_id=?", (LANE_SESSION,)
        ).fetchone()[0] == expected
        assert conn.execute(
            "SELECT host_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0] == expected
    finally:
        conn.close()


# --------------------------------------------------------------------------
# stable_host_id(): the rename-stable identity, persisted once per state root


def test_stable_host_id_persists_across_calls(project: Path) -> None:
    first = coord_db.stable_host_id()
    second = coord_db.stable_host_id()
    assert first == second
    on_disk = (project / ".coordharness" / "host_id").read_text(
        encoding="utf-8"
    ).strip()
    assert on_disk == first


def test_stable_host_id_file_is_written_once_with_owner_only_permissions(
    project: Path,
) -> None:
    value = coord_db.stable_host_id()
    id_path = project / ".coordharness" / "host_id"
    assert id_path.exists()
    assert stat.S_IMODE(id_path.stat().st_mode) == 0o600
    # A second call must return the persisted value, not mint a new one.
    assert coord_db.stable_host_id() == value


def test_stable_host_id_does_not_read_the_network_hostname(project: Path) -> None:
    """The whole point of the fix: unlike `local_host_id()`, this never reads
    `socket.gethostname()`, so a rename cannot change it. It is a
    `uuid4().hex`, as documented.
    """
    stable = coord_db.stable_host_id()
    assert stable != coord_db.local_host_id()
    assert len(stable) == 32
    uuid.UUID(hex=stable)  # raises ValueError if this is not a uuid hex


def test_stable_host_id_falls_back_to_gethostname_when_the_file_is_unreadable(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory sitting where the id file should be can't be `read_text`'d.
    This must degrade to `local_host_id()` with a logged warning, never raise
    -- a locked-down or corrupted state directory must not break every write.
    """
    id_path = project / ".coordharness" / "host_id"
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.mkdir()  # a directory, not a file
    with caplog.at_level(logging.WARNING, logger="coordharness.coord.coord_db"):
        result = coord_db.stable_host_id()
    assert result == socket.gethostname()
    assert any(
        "stable host id" in record.getMessage() for record in caplog.records
    )


# --------------------------------------------------------------------------
# Liveness


@pytest.mark.parametrize(
    "recorded, meaningful",
    [
        (None, True),
        ("", True),
        ("   ", True),
        (socket.gethostname(), True),
        (FOREIGN_HOST, False),
    ],
)
def test_pid_liveness_is_meaningful_only_for_local_or_unstated_rows(
    project: Path, recorded, meaningful: bool
) -> None:
    # `project` isolates `stable_host_id()`'s state dir to tmp_path -- this
    # now calls it internally, and the ambient repo state must never be
    # touched by a unit test.
    assert coord_db.pid_liveness_is_meaningful(recorded) is meaningful


def test_pid_liveness_is_meaningful_true_for_the_current_stable_host_id(
    project: Path,
) -> None:
    """The new code path directly: a row stamped with `stable_host_id()` --
    not merely a legacy hostname -- is recognised local.
    """
    assert coord_db.pid_liveness_is_meaningful(coord_db.stable_host_id()) is True


def test_pid_liveness_is_meaningful_false_for_a_foreign_stable_id_shaped_value(
    project: Path,
) -> None:
    """A foreign row need not look like a hostname to be rejected -- a
    `uuid4().hex` this machine did not mint (the exact shape `stable_host_id`
    writes) must still answer unknown, not local.
    """
    foreign = uuid.uuid4().hex
    assert foreign != coord_db.stable_host_id()
    assert coord_db.pid_liveness_is_meaningful(foreign) is False


def _seed_running_work(conn, work_id: str, *, host_id: str | None) -> str:
    coord_db.upsert_work(
        conn,
        work_id,
        title=f"a run recorded on {host_id or 'this machine'}",
        assignee="claude",
        module="host",
        surface="job",
        done_signal=f"artifacts/{work_id.lower()}.json",
        acceptance_json='["the run is not misread"]',
        intent_state="queued",
    )
    coord_db.claim_work(conn, LANE_SESSION, work_id, lease_s=600)
    run_id = coord_db.appear_run(
        conn,
        work_id=work_id,
        session_id=LANE_SESSION,
        runner_kind="local",
        pid=DEAD_PID,
        pid_started_at=AT,
    )
    conn.execute("UPDATE runs SET host_id=? WHERE run_id=?", (host_id, run_id))
    conn.commit()
    return run_id


def _status(conn, work_id: str, at: float = AT) -> str:
    rows = {str(r["work_id"]): r for r in coord_db.board_rows(conn, at=at)}
    return str(rows[work_id]["status"])


def _live_pid_count(conn, work_id: str):
    rows = {str(r["work_id"]): r for r in coord_db.board_rows(conn, at=AT)}
    return rows[work_id].get("live_pid_count", "unstated")


def test_a_foreign_host_run_is_not_read_as_dead_by_the_local_pid_probe(
    project: Path,
) -> None:
    """The differential: same dead pid, same lease, only host_id differs.

    Without the guard both rows get ``live_pid_count = 0`` and the board stops
    calling the work running on the strength of a probe that could not see the
    process. With it, the foreign row gets no pid verdict at all and falls back
    to the lease/heartbeat, which is the only authority that crosses machines.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        _seed_running_work(conn, "HOST-LOCAL", host_id=socket.gethostname())
        _seed_running_work(conn, "HOST-REMOTE", host_id=FOREIGN_HOST)

        # The local control: the probe can see this machine, the pid is dead,
        # so a pid verdict of zero is recorded and honoured.
        assert _live_pid_count(conn, "HOST-LOCAL") == 0

        # The foreign row is UNKNOWN, not zero -- no pid verdict is published
        # for it at all.
        assert _live_pid_count(conn, "HOST-REMOTE") == "unstated"

        # Read past the claim's lease, where the claim path no longer answers
        # "running" on its own. Only the live-run fallback can, and only the
        # foreign row reaches it -- so the two statuses now differ on host_id
        # alone. Inside the lease both rows derive "running" from the claim and
        # the assertion would hold whether or not the guard existed.
        past_lease = AT + 10_000.0
        assert _status(conn, "HOST-LOCAL", at=past_lease) == "attention"
        assert _status(conn, "HOST-REMOTE", at=past_lease) == "running"
    finally:
        conn.close()


def test_one_foreign_run_makes_the_whole_work_items_pid_count_unknown(
    project: Path,
) -> None:
    """A local dead run beside a foreign one must not answer for both.

    Counting only the local run would report zero live pids for a work item
    that may well be running elsewhere -- the same false "dead", reached by
    arithmetic instead of by probe.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        _seed_running_work(conn, "HOST-MIXED", host_id=socket.gethostname())
        second = coord_db.appear_run(
            conn,
            work_id="HOST-MIXED",
            session_id=LANE_SESSION,
            runner_kind="local",
            pid=DEAD_PID,
            pid_started_at=AT,
        )
        conn.execute(
            "UPDATE runs SET host_id=? WHERE run_id=?", (FOREIGN_HOST, second)
        )
        conn.commit()
        assert _live_pid_count(conn, "HOST-MIXED") == "unstated"
        assert _status(conn, "HOST-MIXED", at=AT + 10_000.0) == "running"
    finally:
        conn.close()


def test_an_unstated_host_keeps_the_single_machine_behaviour(project: Path) -> None:
    """The pre-migration row: NULL host_id must still be probed locally.

    If NULL were treated as foreign, every legacy row would go permanently
    unknown and the board would stop noticing genuine local crashes.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        _seed_running_work(conn, "HOST-LEGACY", host_id=None)
        assert _live_pid_count(conn, "HOST-LEGACY") == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The second board surface
#
# ``board_rows`` is not the only place a pid probe runs. The MCP per-work-id
# path ``_coord_board_row`` builds the same derived row for a single work id,
# and it carried its own copy of the probe. A guard installed on one of two
# surfaces is not installed: the two answered differently about the same row on
# the same connection, which is how a foreign run reads as dead on whichever
# surface was missed. These pin the two together.


def _mcp_row(conn, work_id: str, at: float = AT) -> dict:
    from coordharness.coord import mcp_coord_server

    row = mcp_coord_server._coord_board_row(conn, work_id)
    assert row is not None
    # ``_coord_board_row`` derives status at the database's own clock; re-derive
    # at the requested instant so this can read past the lease exactly as
    # ``_status`` does for ``board_rows``.
    row["status"] = coord_db.derive_work_status(row, at)
    return row


def test_the_mcp_per_work_id_row_applies_the_same_host_guard(
    project: Path,
) -> None:
    """The differential, on the surface that was missed.

    Same construction as the ``board_rows`` differential: identical dead pid,
    identical lease, only ``host_id`` differs. Read past the lease so the claim
    can no longer answer "running" on its own -- inside the lease both rows
    derive "running" from the claim and the assertion would pass with or
    without the guard.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        _seed_running_work(conn, "MCP-LOCAL", host_id=socket.gethostname())
        _seed_running_work(conn, "MCP-REMOTE", host_id=FOREIGN_HOST)

        # The local control: the probe can see this machine, so a pid verdict
        # of zero is published and honoured.
        assert _mcp_row(conn, "MCP-LOCAL").get("live_pid_count", "unstated") == 0

        # The foreign row publishes no pid verdict at all.
        assert (
            _mcp_row(conn, "MCP-REMOTE").get("live_pid_count", "unstated")
            == "unstated"
        )

        past_lease = AT + 10_000.0
        assert _mcp_row(conn, "MCP-LOCAL", at=past_lease)["status"] == "attention"
        assert _mcp_row(conn, "MCP-REMOTE", at=past_lease)["status"] == "running"
    finally:
        conn.close()


def test_one_foreign_run_makes_the_mcp_rows_pid_count_unknown_too(
    project: Path,
) -> None:
    """A local dead run beside a foreign one must not answer for both here
    either -- the same false "dead" reached by arithmetic instead of by probe.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        _seed_running_work(conn, "MCP-MIXED", host_id=socket.gethostname())
        second = coord_db.appear_run(
            conn,
            work_id="MCP-MIXED",
            session_id=LANE_SESSION,
            runner_kind="local",
            pid=DEAD_PID,
            pid_started_at=AT,
        )
        conn.execute(
            "UPDATE runs SET host_id=? WHERE run_id=?", (FOREIGN_HOST, second)
        )
        conn.commit()
        assert (
            _mcp_row(conn, "MCP-MIXED").get("live_pid_count", "unstated")
            == "unstated"
        )
        assert _mcp_row(conn, "MCP-MIXED", at=AT + 10_000.0)["status"] == "running"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "work_id, host_id",
    [
        ("PARITY-LOCAL", socket.gethostname()),
        ("PARITY-REMOTE", FOREIGN_HOST),
        ("PARITY-LEGACY", None),
    ],
)
def test_both_board_surfaces_derive_the_same_status_for_the_same_row(
    project: Path, work_id: str, host_id: str | None
) -> None:
    """The parity that the missed surface broke.

    Asserting each surface's answer separately is what let them drift: both
    were "correct" against their own expectation. This compares them to each
    other, so installing a guard on one and not the other goes red.
    """
    database = project / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.register_session(conn, LANE_SESSION, "claude")
        _seed_running_work(conn, work_id, host_id=host_id)
        past_lease = AT + 10_000.0
        board = {
            str(r["work_id"]): r
            for r in coord_db.board_rows(conn, at=past_lease)
        }[work_id]
        mcp = _mcp_row(conn, work_id, at=past_lease)
        assert mcp["status"] == board["status"]
        assert mcp.get("live_pid_count", "unstated") == board.get(
            "live_pid_count", "unstated"
        )
    finally:
        conn.close()
