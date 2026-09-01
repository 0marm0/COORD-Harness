"""`/api/v1/snapshot` serves the rows an operator can act on, not the table.

`build_snapshot` used to put every work row the database held into `rows`. On
the live board that is 8,841 rows -- 3,853 done, 3,202 archived, 209
superseded, 254 closed, and around 900 queued or planned items nobody had
ranked -- and the menu bar re-reads the document every twenty seconds. The
rows a person could act on were all in there, and the NEXT UP section was
unusable because roughly nine hundred pieces of unranked backlog were in there
with them.

So `rows` is now a surface: what is under way, what is stuck and still recent,
what somebody deliberately ranked next, and what finished in the last day.
`summary` is unchanged and stays the census of the whole board, because the
one thing worse than a screen that shows too much is a screen that quietly
shows less than it claims. These tests hold both halves: that the filter drops
what it says it drops, that it keeps what a person needs, and that the counts
never shrink to match the picture.

Job rows are exempt. A local job is running on this machine now and its row is
the only place the snapshot reports that, so no filter may take it away.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordharness.board.snapshot import build_snapshot
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.jobs import sidecar_snapshot

DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _forget_sidecar_scans():
    """The sidecar reader caches by directory; these tests reuse job ids."""
    sidecar_snapshot.clear_cache()
    yield
    sidecar_snapshot.clear_cache()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.delenv("COORD_JOB_PROCESS_PATTERNS_JSON", raising=False)
    monkeypatch.delenv("COORD_JOB_PROCESS_PATTERNS_FILE", raising=False)
    bootstrap_database(tmp_path / ".coordharness" / "coord.db")
    return tmp_path


def _db(project: Path) -> Path:
    return project / ".coordharness" / "coord.db"


def _seed(
    project: Path,
    work_id: str,
    *,
    intent: str,
    priority: int = 0,
    age_s: float = 0.0,
    artifact: bool = False,
) -> None:
    """One work row in a named intent state, aged by hand.

    ``age_s`` backdates ``updated_at`` after the write, because every writer
    stamps it with the current time and the age is the thing under test.
    ``artifact`` records the proof a ``done`` intent needs before
    ``derive_work_status`` will call it done rather than attention -- without
    it the done cases would be testing the wrong branch.

    Tier is declared T2 so the review gate does not ask a ``done`` row for a
    rubric verdict as well; the review tier is a different subject.
    """
    conn = connect(_db(project))
    try:
        coord_db.upsert_work(
            conn,
            work_id,
            title=f"{work_id} seeded row",
            assignee="claude",
            module="board",
            intent_state=intent,
            priority=priority,
            tier="T2",
            done_signal=f"artifacts/{work_id}.json",
        )
        if artifact:
            conn.execute(
                "INSERT INTO artifacts(artifact_id, work_id, path, kind,"
                " validation_json, created_at) VALUES (?,?,?,?,?,?)",
                (
                    coord_db.new_id("art"),
                    work_id,
                    f"artifacts/{work_id}.json",
                    "report",
                    "{}",
                    time.time(),
                ),
            )
        if age_s:
            conn.execute(
                "UPDATE work_items SET updated_at=? WHERE work_id=?",
                (time.time() - age_s, work_id),
            )
        conn.commit()
    finally:
        conn.close()


def _sidecar(project: Path, job_id: str, state: str) -> None:
    directory = project / ".coordharness" / "job_progress"
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (directory / f"{job_id}.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "roadmap_id": "",
                "state": state,
                "step": "seeded",
                "pct": 0.0,
                "updated_at": now,
                "last_progress_at": now,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _row_ids(project: Path) -> set[str]:
    return {row["id"] for row in build_snapshot(_db(project))["rows"]}


def _statuses(project: Path) -> dict[str, str]:
    conn = connect(_db(project))
    try:
        return {
            str(row["work_id"]): str(row["status"])
            for row in coord_db.board_rows(conn, at=time.time())
        }
    finally:
        conn.close()


def test_the_finished_and_filed_never_reach_the_screen(project: Path) -> None:
    """Archived, superseded and closed rows are history, not a work surface.

    They are the largest block on the live board -- 3,665 of 8,841 rows -- and
    no operator acts on one. The derived statuses are asserted first so the
    test cannot pass because the fixture failed to produce them.
    """
    _seed(project, "OLD-ARCHIVED", intent="archived", priority=9)
    _seed(project, "OLD-SUPERSEDED", intent="superseded", priority=9)
    _seed(project, "OLD-CLOSED", intent="closed", priority=9)
    _seed(project, "NOW-BLOCKED", intent="blocked")

    assert _statuses(project) == {
        "OLD-ARCHIVED": "archived",
        "OLD-SUPERSEDED": "superseded",
        "OLD-CLOSED": "closed",
        "NOW-BLOCKED": "blocked",
    }
    # Priority 9 on all three: a filter that let them through on rank alone
    # would pass a test that seeded them unranked.
    assert _row_ids(project) == {"NOW-BLOCKED"}


def test_queued_work_ships_only_where_somebody_ranked_it(project: Path) -> None:
    """Priority 0 means nobody put this in a queue; >= 1 means somebody did.

    Around nine hundred unranked rows were arriving in NEXT UP, which is what
    made that section unreadable. The ranked row is the other half of the
    check: a filter that dropped all queued work would be no better.
    """
    _seed(project, "BACKLOG-UNRANKED", intent="queued", priority=0)
    _seed(project, "QUEUE-RANKED", intent="queued", priority=1)
    _seed(project, "PLAN-UNRANKED", intent="planned", priority=0)

    assert _row_ids(project) == {"QUEUE-RANKED"}


def test_a_completion_stays_up_for_a_day_and_no_longer(project: Path) -> None:
    """The recent-done tail, bounded so the whole done history does not return.

    A person who just finished something should see it land. Three days later
    it is a record, and 3,853 of them were being posted every twenty seconds.
    """
    _seed(project, "DONE-TODAY", intent="done", artifact=True)
    _seed(project, "DONE-3-DAYS", intent="done", artifact=True, age_s=3 * DAY)

    assert _statuses(project) == {"DONE-TODAY": "done", "DONE-3-DAYS": "done"}
    assert _row_ids(project) == {"DONE-TODAY"}


def test_a_stuck_row_keeps_its_place_for_a_fortnight(project: Path) -> None:
    """Attention is a claim about now, and it expires.

    A blocked row nobody has touched in twenty days is not a live problem; it
    is backlog wearing an urgent colour, and leaving it on the screen is how
    the urgent colour stops meaning anything. Yesterday's is the live one.
    """
    _seed(project, "STUCK-20-DAYS", intent="blocked", age_s=20 * DAY)
    _seed(project, "STUCK-YESTERDAY", intent="blocked", age_s=1 * DAY)

    assert _statuses(project) == {
        "STUCK-20-DAYS": "blocked",
        "STUCK-YESTERDAY": "blocked",
    }
    assert _row_ids(project) == {"STUCK-YESTERDAY"}


def test_the_summary_still_counts_the_board_the_rows_do_not_show(
    project: Path,
) -> None:
    """The census does not shrink to fit the picture.

    This is the failure the filter could have introduced: a panel showing one
    next row and reporting one next row, with nothing anywhere saying that a
    second exists. `summary` is computed over every row before the filter runs,
    so the surface can always be described as a subset of something.
    """
    _seed(project, "QUEUE-RANKED", intent="queued", priority=1)
    _seed(project, "BACKLOG-UNRANKED", intent="queued", priority=0)
    _seed(project, "OLD-ARCHIVED", intent="archived")
    _seed(project, "DONE-3-DAYS", intent="done", artifact=True, age_s=3 * DAY)

    snapshot = build_snapshot(_db(project))

    assert {row["id"] for row in snapshot["rows"]} == {"QUEUE-RANKED"}
    # Both queued rows are counted, though only the ranked one is drawn.
    assert snapshot["summary"]["next"] == 2
    # The archived row and the older completion are counted too.
    assert snapshot["summary"]["done"] == 2
    assert snapshot["summary"]["total"] == 4
    assert snapshot["summary"]["total"] > len(snapshot["rows"])


def test_a_local_job_is_never_filtered_off_the_board(project: Path) -> None:
    """A job row is the only report that this machine is running something.

    Both sidecars carry a state that would remove a work row: `queued` with no
    rank, and `archived`. Neither may remove a job.
    """
    _sidecar(project, "queued-shard", "queued")
    _sidecar(project, "archived-shard", "archived")

    snapshot = build_snapshot(_db(project))
    jobs = {row["id"]: row["status"] for row in snapshot["rows"]}

    assert jobs == {"job:queued-shard": "queued", "job:archived-shard": "archived"}
    assert all(row["priority"] == 0 for row in snapshot["rows"])


# ---------------------------------------------------------------------------
# The surface must not leak into RESOLUTION. The action registry answers
# "does this row exist" and "are its dependencies done"; reading either off
# the display surface makes an archived-or-aged dependency invert to
# unsatisfied and an off-surface target 404. The server therefore keeps a
# full status census beside the served documents, built from the same frozen
# copy of the database.


def _quiet_server(project: Path):
    from coordharness.board.server import make_server

    return make_server(port=0, db_path=str(_db(project)), refresh_interval=3600)


def test_a_dependency_off_the_surface_still_counts_as_satisfied(project: Path) -> None:
    _seed(project, "DEP-DONE-LONG-AGO", intent="done", artifact=True, age_s=3 * DAY)
    conn = connect(_db(project))
    try:
        coord_db.upsert_work(
            conn,
            "TARGET-UNBLOCKED",
            title="target",
            assignee="claude",
            module="board",
            intent_state="queued",
            priority=1,
            tier="T2",
            depends_on=json.dumps(["DEP-DONE-LONG-AGO"]),
        )
        conn.commit()
    finally:
        conn.close()

    server = _quiet_server(project)
    try:
        # The premise first: the dependency must actually be off the surface,
        # or this test would pass against the defect.
        surface = {row["id"] for row in server.snapshot()["rows"]}
        assert "DEP-DONE-LONG-AGO" not in surface
        assert "TARGET-UNBLOCKED" in surface

        registry = server.action_registry("TARGET-UNBLOCKED")
        assert registry is not None
        # dependencies_satisfied is not echoed verbatim; its effect is the
        # structural_context check on the actions that declare it.
        gated = [
            action
            for action in registry["actions"]
            if "dependencies_satisfied" in action.get("preconditions", ())
        ]
        assert gated, "no action declares the dependencies precondition"
        for action in gated:
            structural = next(
                check for check in action["checks"] if check["id"] == "structural_context"
            )
            assert structural["passed"] is True, structural["reason"]
    finally:
        server.server_close()


def test_an_off_surface_row_still_resolves_in_the_action_registry(project: Path) -> None:
    _seed(project, "DONE-AGED-OFF", intent="done", artifact=True, age_s=3 * DAY)

    server = _quiet_server(project)
    try:
        assert "DONE-AGED-OFF" not in {row["id"] for row in server.snapshot()["rows"]}
        registry = server.action_registry("DONE-AGED-OFF")
        assert registry is not None, "an off-surface row must resolve, not 404"
        assert registry["target"]["id"] == "DONE-AGED-OFF"
        assert registry["target"]["state"] == "done"
    finally:
        server.server_close()


def test_the_census_is_server_private_and_covers_the_whole_board(project: Path) -> None:
    from coordharness.board.snapshot import build_status_census

    _seed(project, "ARCHIVED-STILL-COUNTED", intent="archived")
    census = build_status_census(_db(project))
    assert "ARCHIVED-STILL-COUNTED" in census

    server = _quiet_server(project)
    try:
        assert "_status_census" not in server.snapshot()
    finally:
        server.server_close()
