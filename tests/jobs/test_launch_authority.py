from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.jobs import launch, roadmap_binding


def seed_binding(
    root: Path, *, work_id: str = "WORK-1", actor: str = "local"
) -> dict[str, str | Path]:
    db = root / "state" / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        session_id = f"{actor}:tracked-launch"
        coord_db.register_session(conn, session_id, actor, lease_s=600)
        coord_db.upsert_work(conn, work_id, title="Tracked launch fixture", assignee=actor)
        claim_id = coord_db.claim_work(conn, session_id, work_id, step="launch", lease_s=600)
        claim_fence = conn.execute(
            "SELECT lease_token FROM claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()["lease_token"]
    finally:
        conn.close()
    return {
        "db": db,
        "work_id": work_id,
        "actor": actor,
        "session_id": session_id,
        "claim_id": claim_id,
        "claim_fence": claim_fence,
    }


def snapshot_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def row_counts(db: Path) -> tuple[int, ...]:
    conn = sqlite3.connect(db)
    try:
        return tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("work_items", "claims", "agent_sessions", "runs", "events")
        )
    finally:
        conn.close()


def launch_args(
    binding: dict[str, str | Path], *, work_id: str | None = None, owner: str | None = None
) -> list[str]:
    return [
        "--job-id",
        "authority-test",
        "--roadmap-id",
        work_id or str(binding["work_id"]),
        "--owner",
        owner or str(binding["actor"]),
        "--session-id",
        str(binding["session_id"]),
        "--claim-id",
        str(binding["claim_id"]),
        "--claim-fence",
        str(binding["claim_fence"]),
        "--coord-db",
        str(binding["db"]),
        "--",
        sys.executable,
        "-c",
        "from pathlib import Path; Path('child-canary').write_text('ran')",
    ]


@pytest.mark.parametrize(
    "case", ["unknown", "terminal", "archived", "wrong_owner", "mismatched_work"]
)
def test_invalid_binding_is_side_effect_free_before_popen(
    tmp_path: Path, monkeypatch, case: str
) -> None:
    binding = seed_binding(tmp_path)
    db = Path(binding["db"])
    requested_work = str(binding["work_id"])
    owner = str(binding["actor"])
    conn = connect(db)
    try:
        if case == "terminal":
            conn.execute(
                "UPDATE work_items SET intent_state='done' WHERE work_id=?", (requested_work,)
            )
        elif case == "archived":
            conn.execute("UPDATE work_items SET archived_at=1 WHERE work_id=?", (requested_work,))
        elif case == "mismatched_work":
            conn.execute(
                "INSERT INTO work_items(work_id,title,assignee,intent_state,acceptance_json,priority,visibility,version,created_at,updated_at)"
                " VALUES (?,?,?,?,?,0,'operator',0,1,1)",
                ("WORK-2", "Other", owner, "planned", "[]"),
            )
            requested_work = "WORK-2"
        conn.commit()
    finally:
        conn.close()
    if case == "unknown":
        requested_work = "UNKNOWN"
    elif case == "wrong_owner":
        owner = "other"

    monkeypatch.setenv("COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COORD_DB", str(db))
    before_tree = snapshot_tree(tmp_path)
    before_rows = row_counts(db)

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("Popen must not run for an invalid binding")

    monkeypatch.setattr(launch.subprocess, "Popen", forbidden_popen)
    assert launch.main(launch_args(binding, work_id=requested_work, owner=owner)) == 2
    assert not (tmp_path / "child-canary").exists()
    assert row_counts(db) == before_rows
    assert snapshot_tree(tmp_path) == before_tree
    assert not (tmp_path / "state" / "job_progress").exists()


def test_missing_runtime_binding_is_rejected_without_bootstrap(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "missing" / "coord.db"
    monkeypatch.setenv("COORD_DB", str(db))
    called = False

    def forbidden_popen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(launch.subprocess, "Popen", forbidden_popen)
    result = launch.main(
        [
            "--job-id",
            "missing",
            "--roadmap-id",
            "NOPE",
            "--owner",
            "local",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    assert result == 2
    assert not called
    assert not db.exists()
    assert not db.parent.exists()


def test_coord_db_precedence_matches_config(tmp_path: Path, monkeypatch) -> None:
    primary = seed_binding(tmp_path / "primary")
    legacy = tmp_path / "legacy" / "coord.db"
    monkeypatch.setenv("COORD_DB", str(primary["db"]))
    monkeypatch.setenv("COORD_COORD_DB", str(legacy))
    assert roadmap_binding.resolve_coord_db_path() == primary["db"]
    result = roadmap_binding.validate(
        work_id=str(primary["work_id"]),
        job_id="precedence",
        claim_id=str(primary["claim_id"]),
        claim_fence=str(primary["claim_fence"]),
        session_id=str(primary["session_id"]),
        actor=str(primary["actor"]),
    )
    assert result.ok
    monkeypatch.delenv("COORD_DB")
    assert roadmap_binding.resolve_coord_db_path() == legacy


def test_owner_must_match_controller_and_private_assignee(tmp_path: Path) -> None:
    binding = seed_binding(tmp_path)
    common = {
        "work_id": str(binding["work_id"]),
        "job_id": "binding",
        "claim_id": str(binding["claim_id"]),
        "claim_fence": str(binding["claim_fence"]),
        "session_id": str(binding["session_id"]),
        "coord_db": Path(binding["db"]),
    }
    assert (
        roadmap_binding.validate(actor="other", **common).reason
        == "owner does not match the claim session controller"
    )
    wrong_fence = {**common, "claim_fence": "stale-fence"}
    assert (
        roadmap_binding.validate(actor="local", **wrong_fence).reason
        == "explicit claim fence does not match current custody"
    )
    conn = sqlite3.connect(binding["db"])
    try:
        conn.execute(
            "UPDATE work_items SET assignee='other' WHERE work_id=?", (binding["work_id"],)
        )
        conn.commit()
    finally:
        conn.close()
    assert (
        roadmap_binding.validate(actor="local", **common).reason
        == "owner is incompatible with the work assignee"
    )


def test_operator_assignee_is_not_a_wildcard(tmp_path: Path) -> None:
    binding = seed_binding(tmp_path)
    conn = sqlite3.connect(binding["db"])
    try:
        conn.execute(
            "UPDATE work_items SET assignee='operator' WHERE work_id=?",
            (binding["work_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    result = roadmap_binding.validate(
        work_id=str(binding["work_id"]),
        job_id="operator-owner",
        claim_id=str(binding["claim_id"]),
        claim_fence=str(binding["claim_fence"]),
        session_id=str(binding["session_id"]),
        actor="local",
        coord_db=Path(binding["db"]),
    )
    assert not result.ok
    assert result.reason == "owner is incompatible with the work assignee"


def test_revoked_after_initial_validation_is_rejected_before_process_or_sidecar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binding = seed_binding(tmp_path)
    db = Path(binding["db"])
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COORD_DB", str(db))
    canary = tmp_path / "child-canary"

    def revoke_during_policy(*_args, **_kwargs) -> dict[str, bool]:
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE claims SET status='released', release_reason='adversarial hook' "
                "WHERE claim_id=?",
                (binding["claim_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        return {"blocked": False}

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("Popen must not run after custody revocation")

    monkeypatch.setattr(launch, "_run_launch_policy", revoke_during_policy)
    monkeypatch.setattr(launch.subprocess, "Popen", forbidden_popen)
    args = launch_args(binding)
    args[-1] = f"from pathlib import Path; Path({str(canary)!r}).write_text('ran')"
    assert launch.main(args) == 2
    assert not canary.exists()
    assert not (tmp_path / "state" / "job_progress").exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["release", "expire", "reassign"])
def test_custody_change_after_reservation_is_rejected_before_popen(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    binding = seed_binding(tmp_path)
    db = Path(binding["db"])
    if mutation == "reassign":
        conn = connect(db)
        try:
            coord_db.register_session(conn, "local:replacement", "local", lease_s=600)
        finally:
            conn.close()
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COORD_DB", str(db))
    original_reserve = roadmap_binding.reserve_run

    def reserve_then_change_custody(**kwargs):
        result = original_reserve(**kwargs)
        assert result.ok
        conn = sqlite3.connect(db)
        try:
            if mutation == "release":
                conn.execute(
                    "UPDATE claims SET status='released' WHERE claim_id=?",
                    (binding["claim_id"],),
                )
            elif mutation == "expire":
                conn.execute(
                    "UPDATE claims SET expires_at=0 WHERE claim_id=?",
                    (binding["claim_id"],),
                )
            else:
                conn.execute(
                    "UPDATE claims SET session_id='local:replacement' WHERE claim_id=?",
                    (binding["claim_id"],),
                )
            conn.commit()
        finally:
            conn.close()
        return result

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("Popen must not run after reserved custody changes")

    monkeypatch.setattr(roadmap_binding, "reserve_run", reserve_then_change_custody)
    monkeypatch.setattr(launch.subprocess, "Popen", forbidden_popen)
    assert launch.main(launch_args(binding)) == 2
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT work_id,session_id,state,pid FROM runs",
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(binding["work_id"], binding["session_id"], "failed", None)]
    assert not list((tmp_path / "state" / "job_progress").glob("*.json"))


def test_same_work_launches_have_distinct_run_and_sidecar_custody(tmp_path: Path) -> None:
    binding = seed_binding(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(tmp_path / "state"),
        "COORD_DB": str(binding["db"]),
        "COORD_COORD_DB": str(tmp_path / "wrong.db"),
        "COORD_ACTOR": str(binding["actor"]),
    }

    def command(job_id: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "coordharness.jobs.cli",
            "launch",
            "--job-id",
            job_id,
            "--roadmap-id",
            str(binding["work_id"]),
            "--session-id",
            str(binding["session_id"]),
            "--claim-id",
            str(binding["claim_id"]),
            "--claim-fence",
            str(binding["claim_fence"]),
            "--cap-gb",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(0.15)",
        ]

    first = subprocess.Popen(
        command("attempt-one"),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command("attempt-two"),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_out, first_err = first.communicate(timeout=15)
    second_out, second_err = second.communicate(timeout=15)
    assert first.returncode == 0, (first_out, first_err)
    assert second.returncode == 0, (second_out, second_err)

    conn = sqlite3.connect(binding["db"])
    try:
        runs = conn.execute(
            "SELECT run_id,work_id,session_id,state FROM runs WHERE work_id=? ORDER BY run_id",
            (binding["work_id"],),
        ).fetchall()
    finally:
        conn.close()
    assert len(runs) == 2
    assert len({row[0] for row in runs}) == 2
    assert all(row[1:] == (binding["work_id"], binding["session_id"], "done") for row in runs)
    sidecars = sorted((tmp_path / "state" / "job_progress").glob("attempt-*.json"))
    assert [path.name for path in sidecars] == ["attempt-one.json", "attempt-two.json"]


def test_revoker_cannot_commit_between_final_guard_and_popen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binding = seed_binding(tmp_path)
    db = Path(binding["db"])
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("COORD_DB", str(db))
    canary = tmp_path / "guard-canary"
    real_popen = launch.subprocess.Popen
    attempted = threading.Event()
    committed = threading.Event()
    revoker_threads: list[threading.Thread] = []

    def popen_with_contending_revoker(*args, **kwargs):
        if "COORD_WRAPPER_LAUNCH_ID" not in kwargs.get("env", {}):
            return real_popen(*args, **kwargs)

        def revoke() -> None:
            conn = sqlite3.connect(db, timeout=5.0)
            try:
                attempted.set()
                conn.execute(
                    "UPDATE claims SET status='released' WHERE claim_id=?",
                    (binding["claim_id"],),
                )
                conn.commit()
                committed.set()
            finally:
                conn.close()

        revoker = threading.Thread(target=revoke, daemon=True)
        revoker_threads.append(revoker)
        revoker.start()
        assert attempted.wait(1.0)
        assert not committed.wait(0.1)
        assert not (tmp_path / "state" / "job_progress" / "authority-test.json").exists()
        proc = real_popen(*args, **kwargs)
        assert not committed.is_set()
        return proc

    monkeypatch.setattr(launch.subprocess, "Popen", popen_with_contending_revoker)
    args = launch_args(binding)
    args[-1] = f"from pathlib import Path; Path({str(canary)!r}).write_text('ran')"
    assert launch.main(args) == 0
    for revoker in revoker_threads:
        revoker.join(timeout=3.0)
    assert committed.is_set()
    assert canary.read_text() == "ran"
    conn = sqlite3.connect(db)
    try:
        run = conn.execute(
            "SELECT work_id,session_id,state FROM runs",
        ).fetchone()
    finally:
        conn.close()
    assert run == (binding["work_id"], binding["session_id"], "done")


def test_binding_module_cli_probe(tmp_path: Path) -> None:
    binding = seed_binding(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "coordharness.jobs.roadmap_binding",
            "--work-id",
            str(binding["work_id"]),
            "--job-id",
            "probe",
            "--claim-id",
            str(binding["claim_id"]),
            "--claim-fence",
            str(binding["claim_fence"]),
            "--session-id",
            str(binding["session_id"]),
            "--actor",
            str(binding["actor"]),
            "--coord-db",
            str(binding["db"]),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok: WORK-1")
