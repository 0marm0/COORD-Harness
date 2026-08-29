"""A job saying it is done is a claim, and the board has to check it.

`docs/jobs-and-runs.md` puts this in writing: neither surface "trusts the
`state`/`status` string a job last wrote about itself", and "a `done` claim
with no matching artifact on disk is marked `unverified` rather than accepted".
It names the function that implements the sidecar half, `derive_status` in
`coordharness/jobs/status.py`.

That function existed, was unit-tested, and had no caller on the serving path.
The board read `state` straight out of the sidecar, so any job that wrote
`"state": "done"` -- including one whose declared artifact was never produced,
or was produced and later deleted -- arrived on the board as done and in the
summary's done count. The documentation described a check that nothing ran.

These tests hold the board to the documented rule from both sides: an
unverified done claim must not be counted as finished, and a verified one must
still be accepted. A gate that refuses everything is not a gate.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordharness.board import snapshot as snapshot_module
from coordharness.board.snapshot import build_snapshot
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.jobs import sidecar_snapshot

WORK_ID = "ML-201"
JOB_ID = "embed-shards"
SIGNAL = "artifacts/embeddings.json"


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


def _work_row(project: Path, **fields: str) -> None:
    conn = connect(_db(project))
    try:
        coord_db.upsert_work(conn, WORK_ID, title="Embed the shards", **fields)
    finally:
        conn.close()


def _sidecar(project: Path, **overrides: object) -> None:
    directory = project / ".coordharness" / "job_progress"
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload: dict[str, object] = {
        "job_id": JOB_ID,
        "roadmap_id": WORK_ID,
        "state": "done",
        "step": "finished",
        "pct": 100.0,
        "updated_at": now,
        "last_progress_at": now,
    }
    payload.update(overrides)
    (directory / f"{JOB_ID}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _artifact(project: Path) -> None:
    path = project / SIGNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"vectors": list(range(64))}), encoding="utf-8")


def _job_row(project: Path) -> dict:
    snapshot = build_snapshot(_db(project))
    rows = [row for row in snapshot["rows"] if row["id"].startswith("job:")]
    assert len(rows) == 1, rows
    return {**rows[0], "_summary": snapshot["summary"]}


def test_a_done_claim_without_its_artifact_is_not_counted_as_finished(
    project: Path,
) -> None:
    _sidecar(project, done_signal=SIGNAL)
    assert not (project / SIGNAL).exists(), "the artifact must be absent for this to mean anything"

    row = _job_row(project)

    assert row["status"] != "done", (
        "the board took the job's word for it: a done row with no artifact behind it"
    )
    assert row["status"] == "needs_verification"
    assert row["_summary"]["done"] == 0, "an unproved claim was counted in the done total"
    assert row["_summary"]["attention"] == 1
    assert "not found" in row["current_step"], (
        "the row has to say why it is held back, or the refusal is its own silence"
    )


def test_a_done_claim_with_its_artifact_is_accepted(project: Path) -> None:
    """The other half. A gate that refuses proved work is not a gate."""
    _sidecar(project, done_signal=SIGNAL)
    _artifact(project)

    row = _job_row(project)

    assert row["status"] == "done"
    assert row["_summary"]["done"] == 1
    assert row["current_step"] == "finished", "the job's own step text was thrown away"


def test_the_gate_reads_the_work_row_s_proof_when_the_sidecar_names_none(
    project: Path,
) -> None:
    """Most sidecars declare no artifact; the work item they belong to does.

    Checking only the sidecar's own `done_signal` would leave the gate
    unreachable for nearly every real job, which is indistinguishable from not
    having built it.
    """
    _work_row(project, done_signal=SIGNAL)
    _sidecar(project)

    assert _job_row(project)["status"] == "needs_verification"

    _artifact(project)
    sidecar_snapshot.clear_cache()
    assert _job_row(project)["status"] == "done"


def test_a_job_that_declares_no_proof_anywhere_is_left_alone(project: Path) -> None:
    """No artifact was ever declared, so there is nothing to check.

    Inventing a refusal here would claim more than the evidence supports, and
    it is the behaviour `test_distinct_jobs_for_one_work_remain_distinct_rows`
    already pins.
    """
    _work_row(project)
    _sidecar(project)

    assert _job_row(project)["status"] == "done"


def test_a_running_job_is_untouched_by_the_done_gate(project: Path) -> None:
    _work_row(project, done_signal=SIGNAL)
    _sidecar(project, state="running", step="encoding shard 5 of 8", pct=62.0)

    row = _job_row(project)
    assert row["status"] == "running"
    assert row["current_step"] == "encoding shard 5 of 8"


def test_the_board_calls_the_verification_the_docs_name(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`derive_status` shipped with zero callers on the serving path.

    Pinning the call itself is unusual, but the defect was precisely that the
    documented check existed and nothing invoked it -- a state every assertion
    about statuses can be satisfied in by accident.
    """
    calls: list[str] = []
    real = snapshot_module.derive_status

    def spy(item, root, **kwargs):
        calls.append(str(item.get("id") or ""))
        return real(item, root, **kwargs)

    monkeypatch.setattr(snapshot_module, "derive_status", spy)
    _work_row(project, done_signal=SIGNAL)
    _sidecar(project)
    build_snapshot(_db(project))

    assert calls == [JOB_ID], (
        "the board reached its done decision without consulting derive_status"
    )
