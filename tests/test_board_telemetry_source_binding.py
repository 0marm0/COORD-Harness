"""One board, one source tree.

The board draws work rows out of a named coordination database and job rows out
of a directory of telemetry sidecars, and it used to resolve the second one
without ever consulting the first: `config.job_progress_dir()` answers from
`COORD_HOME` or the current project root, never from the database being served.
So `coord-board --db /somewhere/else/coord.db` presented one screen assembled
from two unrelated directories, and a database with no work items and no events
still served whatever sidecars happened to be lying around -- which also put the
honest empty board out of reach whenever a stray sidecar existed anywhere in the
ambient tree.

`coord doctor` had already decided this question from the other side. It refuses
to open a database it cannot place inside the state root it was given and
reports `database_outside_state_root`, and it resolves `job_progress` under that
same root, so it never reads the two halves from two places. These tests hold
the board to the same rule: telemetry belongs to the database it describes.

The pair matters more than either test alone. Binding telemetry to the served
database is only correct if the default path is untouched, and the server hands
its builders a *private copy* of the database in a temporary directory -- so a
fix that derives the telemetry root inside the builder, from the path it was
handed, empties the Jobs surface for everybody. Both layouts are asserted
through the direct builders and through the served bundle.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordharness import config
from coordharness.board.server import build_documents
from coordharness.board.snapshot import build_graph, build_snapshot
from coordharness.bootstrap import bootstrap_database
from coordharness.jobs import sidecar_snapshot


@pytest.fixture(autouse=True)
def _forget_sidecar_scans() -> None:
    """The sidecar reader caches by directory; these tests reuse job ids."""
    sidecar_snapshot.clear_cache()
    yield
    sidecar_snapshot.clear_cache()


def _write_sidecar(directory: Path, job_id: str, work_id: str) -> Path:
    """One telemetry sidecar in the shape the job launcher writes."""
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {
        "job_id": job_id,
        "roadmap_id": work_id,
        "state": "running",
        "step": "encoding shard 5 of 8",
        "pct": 62.0,
        "runtime_s": 30.0,
        "attempt": 1,
        "updated_at": now,
        "last_progress_at": now,
    }
    path = directory / f"{job_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _job_rows(document: dict) -> list[str]:
    return [row["id"] for row in document["rows"] if row["bucket"] == "job"]


def test_a_served_database_does_not_borrow_another_directory_s_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient = tmp_path / "ambient"
    _write_sidecar(ambient / "job_progress", "stray-job", "ML-201")
    monkeypatch.setenv("COORD_HOME", str(ambient))

    database = tmp_path / "elsewhere" / "coord.db"
    bootstrap_database(database)

    snapshot = build_snapshot(database)

    assert _job_rows(snapshot) == [], (
        "a database in one directory served job telemetry from another"
    )
    assert snapshot["rows"] == []
    assert snapshot["source"] == "coord.db", (
        "the provenance string claimed job_progress for sidecars this database "
        "does not own"
    )
    assert snapshot["summary"]["total"] == 0


def test_the_empty_board_is_reachable_while_stray_sidecars_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty database is an empty board, whatever the ambient tree holds."""
    ambient = tmp_path / "ambient"
    for index in range(3):
        _write_sidecar(ambient / "job_progress", f"leftover-{index}", f"OLD-{index}")
    monkeypatch.setenv("COORD_HOME", str(ambient))

    database = tmp_path / "fresh" / "coord.db"
    bootstrap_database(database)

    snapshot, graph, *_ = build_documents(str(database))

    assert snapshot["rows"] == []
    assert snapshot["source"] == "coord.db"
    assert [node for node in graph["nodes"] if node["kind"] == "job"] == []


def test_the_default_layout_still_serves_the_telemetry_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case -- database inside the state root -- must not move."""
    state = tmp_path / "state"
    monkeypatch.setenv("COORD_HOME", str(state))
    database = state / "coord.db"
    bootstrap_database(database)
    _write_sidecar(state / "job_progress", "embed-corpus-v4", "ML-201")

    snapshot = build_snapshot(database)

    assert _job_rows(snapshot) == ["job:embed-corpus-v4"]
    assert snapshot["source"] == "coord.db+job_progress"
    assert config.job_progress_dir_for_database(database) == config.job_progress_dir()


def test_the_served_bundle_keeps_the_default_layout_s_job_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the server, whose builders read a copy in a temporary directory.

    Deriving the telemetry root from the path the builder is handed would look
    correct and serve an empty Jobs surface on every default installation.
    """
    state = tmp_path / "state"
    monkeypatch.setenv("COORD_HOME", str(state))
    database = state / "coord.db"
    bootstrap_database(database)
    _write_sidecar(state / "job_progress", "embed-corpus-v4", "ML-201")

    snapshot, graph, *_ = build_documents(str(database))

    assert _job_rows(snapshot) == ["job:embed-corpus-v4"]
    assert snapshot["source"] == "coord.db+job_progress"
    assert [node["id"] for node in graph["nodes"] if node["kind"] == "job"] == [
        "job:embed-corpus-v4"
    ]


def test_an_out_of_tree_database_serves_the_telemetry_that_sits_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry follows the database rather than the ambient state tree."""
    ambient = tmp_path / "ambient"
    _write_sidecar(ambient / "job_progress", "stray-job", "ML-201")
    monkeypatch.setenv("COORD_HOME", str(ambient))

    elsewhere = tmp_path / "elsewhere"
    database = elsewhere / "coord.db"
    bootstrap_database(database)
    _write_sidecar(elsewhere / "job_progress", "own-job", "OWN-1")

    snapshot = build_snapshot(database)
    graph = build_graph(database)

    assert _job_rows(snapshot) == ["job:own-job"]
    assert snapshot["source"] == "coord.db+job_progress"
    assert [node["id"] for node in graph["nodes"] if node["kind"] == "job"] == [
        "job:own-job"
    ]


def test_an_explicit_telemetry_root_overrides_the_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server needs this to pass the original root around its private copy."""
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "ambient"))
    database = tmp_path / "elsewhere" / "coord.db"
    bootstrap_database(database)
    named = tmp_path / "named"
    _write_sidecar(named, "named-job", "NAMED-1")

    snapshot = build_snapshot(database, job_progress_dir=named)

    assert _job_rows(snapshot) == ["job:named-job"]


def test_the_state_root_of_a_database_is_the_one_that_contains_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("COORD_HOME", str(state))

    assert config.state_root_for_database(state / "coord.db") == config.state_dir()
    assert config.state_root_for_database(state / "nested" / "coord.db") == (
        config.state_dir()
    )
    assert config.state_root_for_database(tmp_path / "elsewhere" / "coord.db") == (
        tmp_path / "elsewhere"
    ).resolve()
    assert config.job_progress_dir_for_database(tmp_path / "x" / "coord.db") == (
        tmp_path / "x"
    ).resolve() / "job_progress"
