"""A job that died before it started still stops reporting.

`_job_stale` opened with an unconditional early return: any state other than
`running` was declared fresh, forever, whatever its timestamp said. So the one
shape the flag exists to catch -- a job that stopped without saying so -- was
invisible for every state a job can stop in *before* it runs. A `queued` job
whose GPU slot was never granted, three hours without an update, reported
`stale: False` and sat on the board looking like work that was about to start.

The flag has to distinguish two silences. A job that is still owed an update
and has not sent one is stale. A job whose writer has stopped for good owes
nothing, and flagging it would put a permanent warning on finished work --
a worse failure than the one being fixed, because it never clears.

These tests pin both: the ancient non-terminal states now flag, and every
terminal one stays quiet no matter how old its stamp is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.board import snapshot as snapshot_module
from coordharness.board.snapshot import build_snapshot
from coordharness.bootstrap import bootstrap_database
from coordharness.jobs import sidecar_snapshot, sidecar_writer
from coordharness.jobs.status import TERMINAL_JOB_STATES, is_terminal_job_state

NOW = 1_772_442_600.0
THREE_HOURS = 3 * 3600.0

# Every state in this list leaves an update outstanding: the job is waiting,
# starting, backing off, or saying nothing intelligible at all. None of them
# mean the writer is finished, so none of them excuse a three-hour silence.
UNFINISHED = ["running", "queued", "starting", "backoff", "waiting", "", "wedged", "retrying"]


@pytest.fixture(autouse=True)
def _forget_sidecar_scans():
    sidecar_snapshot.clear_cache()
    yield
    sidecar_snapshot.clear_cache()


@pytest.mark.parametrize("state", UNFINISHED)
def test_an_unfinished_job_that_stopped_reporting_is_stale(state: str) -> None:
    job = {"state": state, "updated_at": NOW - THREE_HOURS}

    assert snapshot_module._job_stale(job, NOW) is True, (
        f"a {state or 'stateless'!r} job three hours silent reported itself fresh"
    )


# Written out rather than read from TERMINAL_JOB_STATES on purpose: a test
# parametrised over the set it is testing cannot notice a word missing from it,
# and a word missing from it is exactly how a finished job starts warning.
FINISHED = [
    "archived", "blocked", "canceled", "cancelled", "closed", "complete", "completed",
    "dead", "done", "error", "errored", "failed", "finished", "killed", "orphaned",
    "paused", "skipped", "stalled", "success", "superseded",
]


@pytest.mark.parametrize("state", FINISHED)
def test_a_state_the_harness_calls_finished_never_reports_stale(state: str) -> None:
    """The regression this fix must not cause, named word by word."""
    job = {"state": state, "updated_at": NOW - THREE_HOURS}

    assert snapshot_module._job_stale(job, NOW) is False, (
        f"a {state!r} job was flagged stale for not updating after it had stopped"
    )


@pytest.mark.parametrize("state", sorted(TERMINAL_JOB_STATES))
def test_a_finished_job_never_reports_itself_stale(state: str) -> None:
    """And whatever else the set claims is terminal, it has to honour."""
    job = {"state": state, "updated_at": NOW - THREE_HOURS}

    assert snapshot_module._job_stale(job, NOW) is False, (
        f"a {state!r} job was flagged stale for not updating after it had stopped"
    )


@pytest.mark.parametrize("state", UNFINISHED)
def test_a_recent_report_is_not_stale_in_any_unfinished_state(state: str) -> None:
    """The age check is the whole test; widening the states must not skip it."""
    job = {"state": state, "updated_at": NOW - 5.0}

    assert snapshot_module._job_stale(job, NOW) is False


def test_the_fallback_stamp_is_still_read_for_a_queued_job() -> None:
    """`sidecar_snapshot` writes either stamp; a reader that skips one lies."""
    job = {"state": "queued", "last_progress_at": NOW - 5.0}

    assert snapshot_module._job_stale(job, NOW) is False
    assert snapshot_module._job_stale({"state": "queued"}, NOW) is True


def test_terminal_covers_every_state_the_rest_of_the_harness_calls_finished() -> None:
    """The set has to stay a superset, or a finished job starts warning.

    `sidecar_writer` decides on its own copy of this vocabulary when to stop
    writing, and `board.snapshot` decides on a third when to count a row done.
    A word added to either one and not here becomes a permanent stale flag on
    work that is over.
    """
    for name, vocabulary in (
        ("sidecar_writer._DEFAULT_TERMINAL_STATES", sidecar_writer._DEFAULT_TERMINAL_STATES),
        ("board.snapshot._DONE", snapshot_module._DONE),
        ("board.snapshot._DONE_CLAIMS", snapshot_module._DONE_CLAIMS),
    ):
        missing = sorted(set(vocabulary) - set(TERMINAL_JOB_STATES))
        assert not missing, f"{name} calls these finished and TERMINAL_JOB_STATES does not: {missing}"


def test_terminal_is_matched_case_and_whitespace_insensitively() -> None:
    assert is_terminal_job_state(" Done ") is True
    assert is_terminal_job_state("QUEUED") is False
    assert is_terminal_job_state(None) is False


def test_a_queued_job_reaches_the_board_flagged(tmp_path: Path, monkeypatch) -> None:
    """End to end: the flag `board.operations` builds `stale_rows` from."""
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(int(NOW)))
    database = tmp_path / ".coordharness" / "coord.db"
    bootstrap_database(database)
    directory = tmp_path / ".coordharness" / "job_progress"
    directory.mkdir(parents=True, exist_ok=True)
    for job_id, state in (("slot-never-granted", "queued"), ("shards-written", "done")):
        (directory / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "roadmap_id": "ML-201",
                    "state": state,
                    "updated_at": NOW - THREE_HOURS,
                    "last_progress_at": NOW - THREE_HOURS,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    rows = {row["id"]: row for row in build_snapshot(database)["rows"]}

    assert rows["job:slot-never-granted"]["stale"] is True, (
        "a job that never started sat on the board indistinguishable from one about to"
    )
    assert rows["job:shards-written"]["stale"] is False
