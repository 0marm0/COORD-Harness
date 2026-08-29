"""Two documents built from one sidecar have to reach one answer.

`test_board_done_is_verified.py` taught the row path to answer a job's `done`
claim with the artifact instead of with the claim. It taught only the row path.
`build_graph` kept writing the sidecar's raw `state` string onto its job node,
so a single sidecar carrying `"state": "done"` with no artifact produced
`needs_verification` on the board and `done` on the graph -- from the same
input, in the same process, over the same served API.

A verification a caller can route around is not a verification. These tests
hold both documents to one derivation, from both sides: an unproved claim must
not read `done` anywhere, and a proved one must still read `done` everywhere.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordharness.board.snapshot import build_graph, build_snapshot
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.jobs import sidecar_snapshot

WORK_ID = "ML-201"
JOB_ID = "embed-shards"
SIGNAL = "artifacts/embeddings.json"


@pytest.fixture(autouse=True)
def _forget_sidecar_scans():
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


def _row_status(project: Path) -> str:
    snapshot = build_snapshot(_db(project))
    rows = [row for row in snapshot["rows"] if row["id"].startswith("job:")]
    assert len(rows) == 1, rows
    return str(rows[0]["status"])


def _node_status(project: Path) -> str:
    sidecar_snapshot.clear_cache()
    graph = build_graph(_db(project))
    nodes = [node for node in graph["nodes"] if node["kind"] == "job"]
    assert len(nodes) == 1, nodes
    return str(nodes[0]["status"])


def test_the_graph_does_not_serve_a_done_claim_the_board_refused(project: Path) -> None:
    _sidecar(project, done_signal=SIGNAL)
    assert not (project / SIGNAL).exists(), "the artifact must be absent for this to mean anything"

    assert _row_status(project) == "needs_verification"
    assert _node_status(project) != "done", (
        "the graph took the job's word for it after the board had already refused it"
    )
    assert _node_status(project) == "needs_verification"


def test_the_graph_reads_the_work_row_s_proof_exactly_as_the_board_does(
    project: Path,
) -> None:
    """Most sidecars declare no artifact of their own; their work item does.

    A graph that consulted only the sidecar's own `done_signal` would agree
    with the board on the rare job and disagree on the ordinary one.
    """
    _work_row(project, done_signal=SIGNAL)
    _sidecar(project)

    assert _row_status(project) == "needs_verification"
    assert _node_status(project) == "needs_verification"


def test_a_proved_done_claim_still_reads_done_on_both_documents(project: Path) -> None:
    """The other half. A graph that refuses everything agrees by accident."""
    _work_row(project, done_signal=SIGNAL)
    _sidecar(project)
    _artifact(project)

    assert _row_status(project) == "done"
    assert _node_status(project) == "done"


def test_a_running_job_reads_running_on_both_documents(project: Path) -> None:
    _work_row(project, done_signal=SIGNAL)
    _sidecar(project, state="running", step="encoding shard 5 of 8", pct=62.0)

    assert _row_status(project) == "running"
    assert _node_status(project) == "running"


def test_a_job_with_no_declared_proof_reads_done_on_both_documents(project: Path) -> None:
    """The documents agree on the exemption too, not only on the refusal."""
    _work_row(project)
    _sidecar(project)

    assert _row_status(project) == "done"
    assert _node_status(project) == "done"
