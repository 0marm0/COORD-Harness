from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordharness.jobs import sidecar_snapshot
from coordharness.jobs.sidecar_snapshot import SidecarSnapshot, load_snapshot


def _write(dir_path: Path, name: str, payload: dict) -> Path:
    p = dir_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _clear_memo():
    sidecar_snapshot.clear_cache()
    yield
    sidecar_snapshot.clear_cache()


def test_loads_each_sidecar_once_and_indexes_by_canonical_id(tmp_path: Path):
    _write(tmp_path, "a.json", {"roadmap_id": "N-A", "pct": 10, "updated_at": 100.0})
    _write(tmp_path, "b.json", {"roadmap_id": "N-B", "pct": 20, "updated_at": 200.0})
    snap = load_snapshot(tmp_path)
    assert isinstance(snap, SidecarSnapshot)
    assert len(snap) == 2
    assert set(snap.by_id) == {"N-A", "N-B"}
    assert snap.by_id["N-A"]["pct"] == 10
    assert snap.get("N-B")["pct"] == 20
    assert snap.get("MISSING") is None


def test_dedup_by_canonical_id_freshest_wins(tmp_path: Path):
    _write(tmp_path, "stale.json",
           {"roadmap_id": "N-DUP", "pct": 30, "step": "old", "updated_at": 100.0})
    _write(tmp_path, "fresh.json",
           {"roadmap_id": "N-DUP", "pct": 75, "step": "new", "updated_at": 500.0})
    snap = load_snapshot(tmp_path)
    assert len(snap) == 1
    rep = snap.by_id["N-DUP"]
    assert rep["pct"] == 75
    assert rep["step"] == "new"
    assert rep["updated_at"] == 500.0
    assert "merged_from" in rep and len(rep["merged_from"]) == 2


def test_merge_fills_missing_fields_from_duplicate(tmp_path: Path):
    _write(tmp_path, "fresh.json",
           {"roadmap_id": "N-M", "pct": 90, "updated_at": 900.0})
    _write(tmp_path, "stale.json",
           {"roadmap_id": "N-M", "pct": 50, "updated_at": 100.0,
            "done_signal": "data_local/x.parquet"})
    snap = load_snapshot(tmp_path)
    rep = snap.by_id["N-M"]
    assert rep["pct"] == 90
    assert rep["done_signal"] == "data_local/x.parquet"


def test_corrupt_and_partial_files_skipped(tmp_path: Path):
    _write(tmp_path, "good.json", {"roadmap_id": "N-GOOD", "pct": 5})
    (tmp_path / "corrupt.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "partial.json").write_text('{"roadmap_id": "N-P", "pct":', encoding="utf-8")
    (tmp_path / "notdict.json").write_text("[1, 2, 3]", encoding="utf-8")
    snap = load_snapshot(tmp_path)
    assert set(snap.by_id) == {"N-GOOD"}
    assert len(snap.skipped) == 3


def test_non_json_files_ignored(tmp_path: Path):
    _write(tmp_path, "j.json", {"roadmap_id": "N-J"})
    (tmp_path / "readme.txt").write_text("ignore me", encoding="utf-8")
    (tmp_path / "x.json.backup").write_text("{}", encoding="utf-8")
    snap = load_snapshot(tmp_path)
    assert set(snap.by_id) == {"N-J"}


def test_realpath_dedup_symlinked_duplicate(tmp_path: Path):
    real = _write(tmp_path, "real.json", {"roadmap_id": "N-LINK", "pct": 42})
    link = tmp_path / "alias.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    snap = load_snapshot(tmp_path)
    assert len(snap) == 1
    assert set(snap.by_id) == {"N-LINK"}


def test_unresolvable_ids_not_collapsed(tmp_path: Path):
    x_path = _write(tmp_path, "x.json", {"pct": 1})
    _write(tmp_path, "y.json", {"pct": 2})
    snap = load_snapshot(tmp_path)
    assert len(snap) == 2
    assert snap.by_id == {}
    x_row = next(row for row in snap.items if row["pct"] == 1)
    assert x_row["merged_from"] == [os.path.realpath(x_path)]


def test_memo_avoids_rescan_but_returns_independent_copy(tmp_path: Path):
    _write(tmp_path, "a.json", {"roadmap_id": "N-A", "pct": 10, "updated_at": 1.0})
    first = load_snapshot(tmp_path)
    second = load_snapshot(tmp_path)
    assert first == second
    assert first is not second
    assert first.items[0] is not second.items[0]
    first.items[0]["pct"] = 999
    third = load_snapshot(tmp_path)
    assert third.by_id["N-A"]["pct"] == 10


def test_concurrent_instances_sharing_roadmap_id_not_collapsed(tmp_path: Path):
    _write(tmp_path, "shard1.json",
           {"roadmap_id": "N-SWARM", "job_id": "N-SWARM-1", "pct": 10, "updated_at": 100.0})
    _write(tmp_path, "shard2.json",
           {"roadmap_id": "N-SWARM", "job_id": "N-SWARM-2", "pct": 80, "updated_at": 200.0})
    snap = load_snapshot(tmp_path)
    assert len(snap) == 2
    assert sorted(it["pct"] for it in snap.items) == [10, 80]
    assert snap.by_id["N-SWARM"]["pct"] == 80


def test_counter_merge_takes_max_across_true_duplicates(tmp_path: Path):
    _write(tmp_path, "early.json",
           {"roadmap_id": "N-C", "job_id": "N-C", "done": 5, "total": 1000, "updated_at": 100.0})
    _write(tmp_path, "late.json",
           {"roadmap_id": "N-C", "job_id": "N-C", "done": 900, "total": 0, "updated_at": 200.0})
    snap = load_snapshot(tmp_path)
    rep = snap.by_id["N-C"]
    assert rep["done"] == 900
    assert rep["total"] == 1000


def test_hidden_diagnostic_sidecars_do_not_merge_into_live_instance(tmp_path: Path):
    _write(tmp_path, "circuit_embed.json", {
        "roadmap_id": "JOB-0623-LAYER",
        "job_id": "circuit_embed",
        "state": "running",
        "pct": 1.1,
        "done": 504,
        "total": 2761,
        "updated_at": 200.0,
        "authoritative_work_status": True,
    })
    _write(tmp_path, "JOB-0623-LAYER.json", {
        "roadmap_id": "JOB-0623-LAYER",
        "job_id": "circuit_embed",
        "state": "queued",
        "pct": 100,
        "done": 25,
        "total": 25,
        "updated_at": 300.0,
        "visibility": "diagnostic",
        "hide_from_operator": True,
    })

    snap = load_snapshot(tmp_path)
    assert len(snap.items) == 1
    row = snap.items[0]
    assert row["state"] == "running", row
    assert row["pct"] == 1.1, row
    assert row["done"] == 504, row
    assert row["total"] == 2761, row
    assert row.get("hide_from_operator") is not True, row
    assert len(row.get("merged_from") or []) == 1, row


def test_cache_bounded_to_latest_fingerprint_per_dir(tmp_path: Path):
    p = _write(tmp_path, "a.json", {"roadmap_id": "N-A", "pct": 1, "updated_at": 1.0})
    for i in range(2, 7):
        load_snapshot(tmp_path)
        time.sleep(0.005)
        os.utime(p, ns=(time.time_ns(), time.time_ns()))
        p.write_text(json.dumps({"roadmap_id": "N-A", "pct": i, "updated_at": float(i)}),
                     encoding="utf-8")
    load_snapshot(tmp_path)
    dir_real = os.path.realpath(str(tmp_path))
    keys_for_dir = [k for k in sidecar_snapshot._CACHE if k[0] == dir_real]
    assert len(keys_for_dir) == 1


def test_memo_busts_on_change(tmp_path: Path):
    p = _write(tmp_path, "a.json", {"roadmap_id": "N-A", "pct": 10, "updated_at": 1.0})
    first = load_snapshot(tmp_path)
    time.sleep(0.01)
    os.utime(p, ns=(time.time_ns(), time.time_ns()))
    p.write_text(json.dumps({"roadmap_id": "N-A", "pct": 99, "updated_at": 2.0}),
                 encoding="utf-8")
    second = load_snapshot(tmp_path)
    assert second is not first
    assert second.by_id["N-A"]["pct"] == 99


def test_memo_busts_on_new_file(tmp_path: Path):
    _write(tmp_path, "a.json", {"roadmap_id": "N-A"})
    first = load_snapshot(tmp_path)
    _write(tmp_path, "b.json", {"roadmap_id": "N-B"})
    second = load_snapshot(tmp_path)
    assert second is not first
    assert set(second.by_id) == {"N-A", "N-B"}


def test_missing_dir_returns_empty_snapshot(tmp_path: Path):
    snap = load_snapshot(tmp_path / "does_not_exist")
    assert isinstance(snap, SidecarSnapshot)
    assert len(snap) == 0
    assert snap.by_id == {}


def test_default_dir_uses_configured_state_root():
    from coordharness import config

    assert sidecar_snapshot.default_job_progress_dir() == config.job_progress_dir()
