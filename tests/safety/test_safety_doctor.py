from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord.config import connect
from coordharness.safety.doctor import run_doctor
from coordharness.safety.mcp import read_config, security_issues
from coordharness.safety.paths import PathSafetyError, resolve_under_root


@pytest.fixture(autouse=True)
def _isolated_board_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """`doctor.board_port` probes whatever port is actually configured; a
    real `coord-board` on this machine's default port would otherwise leak
    into every doctor report these tests assert on. Point it at a port
    that was free the instant it was chosen instead.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("COORD_BOARD_PORT", str(free_port))


@pytest.fixture
def current_harness(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    db = state / "coord.db"
    bootstrap_database(db)
    return project, state, db


def _finding(report: dict, finding_id: str) -> dict:
    return next(item for item in report["findings"] if item["id"] == finding_id)


def test_path_traversal_and_symlink_escape_fail_closed(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    trusted.mkdir()
    outside.mkdir()
    (trusted / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathSafetyError):
        resolve_under_root("nested/../file.json", trusted, must_exist=False)
    with pytest.raises(PathSafetyError):
        resolve_under_root("escape/future.json", trusted, must_exist=False)
    with pytest.raises(PathSafetyError):
        resolve_under_root(outside / "value.json", trusted, must_exist=False, allow_absolute=True)


def test_doctor_is_read_only_on_a_current_empty_harness(current_harness) -> None:
    project, state, db = current_harness
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    before_entries = sorted(path.relative_to(state).as_posix() for path in state.rglob("*"))
    before_mtime = db.stat().st_mtime_ns

    report = run_doctor(
        db_path=db,
        project_root=project,
        state_root=state,
        now=2_000_000_000,
    )

    assert report["status"] == "PASS"
    assert report["read_only"] is True
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    assert db.stat().st_mtime_ns == before_mtime
    assert sorted(path.relative_to(state).as_posix() for path in state.rglob("*")) == before_entries
    assert {item["id"] for item in report["findings"]} == {
        "doctor.board_port",
        "doctor.db_file_modes",
        "doctor.jobs_projection",
        "doctor.leases_reviews",
        "doctor.lifecycle_writers",
        "doctor.mcp_security",
        "doctor.public_paths",
        "doctor.schema",
    }


def test_traversal_pointer_blocks_without_echoing_value(current_harness) -> None:
    project, state, db = current_harness
    secretish = "../outside/PRIVATE_VALUE.txt"
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO work_items(work_id,title,intent_state,done_signal,created_at,updated_at)"
            " VALUES ('WORK-1','portable test','planned',?,1,1)",
            (secretish,),
        )
        conn.commit()
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    finding = _finding(report, "doctor.public_paths")
    assert finding["status"] == "BLOCKED"
    assert finding["details"]["invalid_pointer_fields"] == ["WORK-1:done_signal"]
    assert secretish not in json.dumps(report)


def test_sidecar_symlink_blocks_before_target_read(current_harness, tmp_path: Path) -> None:
    project, state, db = current_harness
    external = tmp_path / "outside.json"
    external.write_text('{"job_id":"evil","roadmap_id":"WORK-1"}', encoding="utf-8")
    progress = state / "job_progress"
    progress.mkdir()
    (progress / "evil.json").symlink_to(external)

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    finding = _finding(report, "doctor.jobs_projection")
    assert finding["status"] == "BLOCKED"
    assert "sidecar_symlink" in finding["details"]["problem_codes"]
    assert external.name not in json.dumps(finding)


def test_mcp_inventory_redacts_and_detects_literal_secrets(current_harness) -> None:
    project, state, db = current_harness
    literal = "".join(("sk", "-", "public-test", "-", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    config = project / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "example": {
                        "command": "npx",
                        "args": ["package@latest", f"--token={literal}"],
                        "env": {"API_TOKEN": literal, "MODE": "safe"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    records = read_config(config, source="project://mcp.json")
    assert {issue.code for issue in security_issues(records)} == {
        "mcp.literal_secret",
        "mcp.unpinned_package",
    }
    report = run_doctor(
        db_path=db,
        project_root=project,
        state_root=state,
        mcp_config_paths=[config],
        now=2,
    )
    finding = _finding(report, "doctor.mcp_security")
    assert finding["status"] == "BLOCKED"
    serialized = json.dumps(report)
    assert literal not in serialized
    assert "safe" not in serialized
    assert "<redacted>" in serialized


def test_missing_state_root_is_blocked_and_not_created(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing_state = project / ".coordharness"
    report = run_doctor(
        db_path=missing_state / "coord.db",
        project_root=project,
        state_root=missing_state,
        now=2,
    )
    assert report["status"] == "BLOCKED"
    assert report["findings"][0]["id"] == "doctor.roots"
    assert not missing_state.exists()
