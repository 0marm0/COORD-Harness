"""`coord doctor` is what the docs send a stranger to when something is wrong.

Each test below constructs one detectable first-run state in a throwaway
environment and asserts that the specific finding carries a stable
machine-readable `code` and an executable `remediation` naming the fix -- not
just a status -- while the v1 shape (`id`/`status`/`summary`/`details`) that
existing consumers already read stays exactly as it was.
"""

from __future__ import annotations

import json
import os
import subprocess
import sqlite3
from pathlib import Path

from coordharness.bootstrap import bootstrap_database
from coordharness.coord.config import connect
from coordharness.safety.doctor import BLOCKED, PASS, REPORT_SCHEMA, run_doctor


def _finding(report: dict, finding_id: str) -> dict:
    return next(item for item in report["findings"] if item["id"] == finding_id)


def _harness(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    return project, state


# --- existing v1 consumers keep working ------------------------------------


def test_the_v1_shape_every_existing_consumer_reads_is_unchanged(tmp_path: Path) -> None:
    """`coordharness/coord/cli.py` reads only `report["status"]`; every field
    a finding already had (`id`, `status`, `summary`, `details`) must still
    be present with the same meaning -- the new `code`/`remediation`/
    `remediations` fields are additions, not replacements.
    """
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2_000_000_000)

    assert report["schema"] == REPORT_SCHEMA
    assert report["status"] == PASS
    assert report["read_only"] is True
    assert isinstance(report["observed_at"], float)
    for item in report["findings"]:
        assert {"id", "status", "summary", "details"} <= item.keys()
        assert item["status"] in (PASS, BLOCKED)
        assert isinstance(item["details"], dict)
        # The additive contract this task adds: every finding, PASS or not,
        # names a stable code, and every BLOCKED finding names a concrete
        # remediation.
        assert isinstance(item["code"], str) and item["code"]
        assert isinstance(item["remediations"], list)
        if item["status"] == BLOCKED:
            assert isinstance(item["remediation"], str) and item["remediation"]
        else:
            assert item["remediation"] is None
            assert item["remediations"] == []


def test_a_healthy_freshly_bootstrapped_board_still_exits_pass(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2_000_000_000)

    assert report["status"] == PASS
    assert {item["id"]: item["status"] for item in report["findings"]} == {
        "doctor.jobs_projection": PASS,
        "doctor.leases_reviews": PASS,
        "doctor.lifecycle_writers": PASS,
        "doctor.mcp_security": PASS,
        "doctor.public_paths": PASS,
        "doctor.schema": PASS,
    }


# --- state 1: no database yet -----------------------------------------------


def test_no_database_yet_names_the_bootstrap_command(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"  # never created

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)

    assert report["status"] == BLOCKED
    schema = _finding(report, "doctor.schema")
    assert schema["status"] == BLOCKED
    assert schema["code"] == "doctor.schema.database_missing"
    assert "coord demo" in schema["remediation"] or "coord session start" in schema["remediation"]
    assert schema["remediations"][0]["code"] == "doctor.schema.database_missing"


# --- state 2: a database that exists but is empty ---------------------------


def test_an_empty_database_is_told_apart_from_a_missing_one(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    # A plain, valid, but never-bootstrapped SQLite file -- distinct from "no
    # file at all" (doctor.schema.database_missing) and from "corrupt"
    # (doctor.schema.integrity_check_failed).
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version")
    conn.commit()
    conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)

    schema = _finding(report, "doctor.schema")
    assert schema["status"] == BLOCKED
    assert schema["code"] == "doctor.schema.database_empty"
    assert schema["details"]["missing_tables"]
    assert "bootstrap" in schema["remediation"].lower() or "coord" in schema["remediation"]


def test_a_foreign_file_at_the_db_path_is_a_different_code_than_empty(tmp_path: Path) -> None:
    """A file that cannot even be opened as SQLite (garbage bytes, or a
    foreign file someone pointed --db at) fails closed through the same
    except branch a lock conflict or a genuinely corrupt page would, and
    that branch's code must still differ from the "never bootstrapped"
    empty-database state above -- the reader needs a different fix for each.
    """
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    db.write_bytes(b"not a sqlite database at all, just bytes" * 8)

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)

    schema = _finding(report, "doctor.schema")
    assert schema["status"] == BLOCKED
    assert schema["code"] == "doctor.schema.unreadable"
    assert schema["code"] != "doctor.schema.database_empty"
    assert "lock" in schema["remediation"] or "valid" in schema["remediation"]


# --- state 3 & 4: MCP config pointing at a missing interpreter / a shim ----
# --- missing its exec bit ---------------------------------------------------


def test_mcp_command_pointing_nowhere_is_reported_with_a_fix(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)
    config = project / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "coordharness": {
                        "command": "./scripts/does-not-exist.sh",
                        "args": [],
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = run_doctor(
        db_path=db,
        project_root=project,
        state_root=state,
        mcp_config_paths=[config],
        now=2,
    )

    mcp = _finding(report, "doctor.mcp_security")
    assert mcp["status"] == BLOCKED
    assert "mcp.command_not_found" in mcp["details"]["problem_codes"]
    codes = {item["code"] for item in mcp["remediations"]}
    assert "mcp.command_not_found" in codes


def test_mcp_launch_shim_missing_its_exec_bit_is_a_distinct_code(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)
    scripts = project / "scripts"
    scripts.mkdir()
    shim = scripts / "coord-mcp-launch.sh"
    shim.write_text("#!/bin/bash\nexec true\n", encoding="utf-8")
    shim.chmod(0o644)  # readable, not executable
    assert not os.access(shim, os.X_OK)

    config = project / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "coordharness": {
                        "command": "./scripts/coord-mcp-launch.sh",
                        "args": [],
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = run_doctor(
        db_path=db,
        project_root=project,
        state_root=state,
        mcp_config_paths=[config],
        now=2,
    )

    mcp = _finding(report, "doctor.mcp_security")
    assert mcp["status"] == BLOCKED
    assert "mcp.command_not_executable" in mcp["details"]["problem_codes"]
    remediation_codes = {item["code"] for item in mcp["remediations"]}
    assert "mcp.command_not_executable" in remediation_codes
    # Not conflated with the "not found" state: the file is right there.
    assert "mcp.command_not_found" not in mcp["details"]["problem_codes"]
    # No absolute host path leaks into the report.
    assert str(shim) not in json.dumps(report)


def test_a_correctly_executable_shim_does_not_block(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)
    scripts = project / "scripts"
    scripts.mkdir()
    shim = scripts / "coord-mcp-launch.sh"
    shim.write_text("#!/bin/bash\nexec true\n", encoding="utf-8")
    shim.chmod(0o755)

    config = project / "mcp.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"coordharness": {"command": "./scripts/coord-mcp-launch.sh"}}}
        ),
        encoding="utf-8",
    )

    report = run_doctor(
        db_path=db,
        project_root=project,
        state_root=state,
        mcp_config_paths=[config],
        now=2,
    )

    mcp = _finding(report, "doctor.mcp_security")
    assert mcp["status"] == PASS
    assert mcp["details"]["problem_codes"] == []


# --- state 5: a stale claim with no live process ----------------------------


def _insert_session_and_claim(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    work_id: str,
    claim_id: str,
    pid: int | None,
    expires_at: float,
    now: float,
) -> None:
    conn.execute(
        "INSERT INTO agent_sessions(session_id,actor,pid,started_at,last_heartbeat,"
        "lease_until,state,version) VALUES (?,?,?,?,?,?,?,0)",
        (session_id, "claude", pid, now, now, expires_at + 1000, "active"),
    )
    conn.execute(
        "INSERT INTO work_items(work_id,title,intent_state,created_at,updated_at)"
        " VALUES (?,?,?,?,?)",
        (work_id, "test row", "running", now, now),
    )
    conn.execute(
        "INSERT INTO claims(claim_id,work_id,session_id,status,acquired_at,"
        "heartbeat_at,expires_at,version) VALUES (?,?,?,'running',?,?,?,0)",
        (claim_id, work_id, session_id, now, now, expires_at),
    )
    conn.commit()


def test_stale_claim_with_a_confirmed_dead_process_names_the_release(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)

    # A pid that definitely once existed but has since exited and been
    # reaped, rather than a magic-number guess that might collide with a
    # real process on the machine running this test.
    finished = subprocess.Popen(["true"])
    finished.wait()
    dead_pid = finished.pid

    conn = connect(db)
    try:
        _insert_session_and_claim(
            conn,
            session_id="claude:dead",
            work_id="DEAD-1",
            claim_id="clm-dead",
            pid=dead_pid,
            expires_at=100.0,
            now=1.0,
        )
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=200.0)

    leases = _finding(report, "doctor.leases_reviews")
    assert leases["status"] == BLOCKED
    assert leases["details"]["dead_process_claim_count"] == 1
    assert leases["details"]["dead_process_claim_work_ids"] == ["DEAD-1"]
    codes = {item["code"] for item in leases["remediations"]}
    assert "doctor.leases_reviews.dead_process_claim" in codes
    assert leases["code"] == "doctor.leases_reviews.dead_process_claim"
    assert "release" in leases["remediation"]


def test_an_expired_lease_whose_process_is_still_alive_is_not_called_dead(
    tmp_path: Path,
) -> None:
    """The ablation: a lease-expired claim owned by a live process (this test
    process itself) must not be reported as a confirmed-dead process -- only
    as the plainer, pre-existing `expired_claim` state.
    """
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)

    conn = connect(db)
    try:
        _insert_session_and_claim(
            conn,
            session_id="claude:alive",
            work_id="ALIVE-1",
            claim_id="clm-alive",
            pid=os.getpid(),
            expires_at=100.0,
            now=1.0,
        )
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=200.0)

    leases = _finding(report, "doctor.leases_reviews")
    assert leases["status"] == BLOCKED
    assert leases["details"]["expired_claim_count"] == 1
    assert leases["details"]["dead_process_claim_count"] == 0
    codes = {item["code"] for item in leases["remediations"]}
    assert "doctor.leases_reviews.dead_process_claim" not in codes
    assert "doctor.leases_reviews.expired_claim" in codes


def test_a_claim_not_yet_expired_is_never_flagged_even_with_a_dead_pid(
    tmp_path: Path,
) -> None:
    """The invariant this check must not violate: the ordinary gap between two
    one-shot CLI commands from the same session -- lease still valid, process
    already exited -- is not staleness. `reap_zombie_sessions` establishes the
    same gate (dead pid is only acted on once the lease has also expired) and
    this check must not be looser than that.
    """
    project, state = _harness(tmp_path)
    db = state / "coord.db"
    bootstrap_database(db)

    finished = subprocess.Popen(["true"])
    finished.wait()
    dead_pid = finished.pid

    conn = connect(db)
    try:
        _insert_session_and_claim(
            conn,
            session_id="claude:between-commands",
            work_id="BETWEEN-1",
            claim_id="clm-between",
            pid=dead_pid,
            expires_at=1_000_000.0,
            now=1.0,
        )
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=200.0)

    leases = _finding(report, "doctor.leases_reviews")
    assert leases["status"] == PASS
    assert leases["details"]["dead_process_claim_count"] == 0
    assert leases["details"]["expired_claim_count"] == 0


# --- exit-code contract stays exactly what CI gates on ----------------------


def test_exit_code_is_still_2_on_blocked_and_0_on_pass(tmp_path: Path) -> None:
    project, state = _harness(tmp_path)
    db = state / "coord.db"  # missing -> BLOCKED
    blocked = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    assert blocked["status"] == BLOCKED

    bootstrap_database(db)
    passing = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    assert passing["status"] == PASS
