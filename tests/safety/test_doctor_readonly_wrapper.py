from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import stat

from coordharness.bootstrap import bootstrap_database
from coordharness.coord.config import connect
from coordharness.entry import main
from coordharness.safety.doctor import run_doctor


def _tree_identity(root: Path) -> list[tuple[str, int, int, str]]:
    identity: list[tuple[str, int, int, str]] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        digest = ""
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        identity.append(
            (path.relative_to(root).as_posix(), metadata.st_mode, metadata.st_size, digest)
        )
    return identity


def test_entry_doctor_missing_state_blocks_without_creating(capsys, tmp_path: Path) -> None:
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()

    exit_code = main(
        [
            "--db",
            str(state / "coord.db"),
            "doctor",
            "--project-root",
            str(project),
            "--state-root",
            str(state),
            "--now",
            "2",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert not state.exists()


def test_existing_wal_is_fail_closed_and_source_tree_is_invariant(tmp_path: Path) -> None:
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    db = state / "coord.db"
    bootstrap_database(db)

    writer = connect(db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO events(ts,kind) VALUES (1,'wal-test')")
        writer.commit()
        assert Path(f"{db}-wal").stat().st_size > 0
        before = _tree_identity(state)

        first = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
        middle = _tree_identity(state)
        second = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
        after = _tree_identity(state)

        assert first["status"] == "BLOCKED"
        assert second["status"] == "BLOCKED"
        assert before == middle == after
    finally:
        writer.close()


def test_base_schema_checksum_tamper_is_blocked(tmp_path: Path) -> None:
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    db = state / "coord.db"
    bootstrap_database(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE name='coord_v1_initial'",
            ("0" * 64,),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    schema = next(item for item in report["findings"] if item["id"] == "doctor.schema")
    assert schema["status"] == "BLOCKED"
    assert schema["details"]["checksum_mismatches"] == ["coord_v1_initial"]
