"""`coord doctor` on the board a first reader actually gets.

Three defects made the opening minute of the harness red, and each one is
asserted here against the surface a first user touches -- the CLI, from a
throwaway directory -- rather than against an internal helper:

  * doctor blocked on the board `python -m coordharness.demo` seeds, because a
    row that declares where its report will land before anyone has made the
    directory was treated as an unproven pointer, and because the seeded job
    sidecars named no work row;
  * doctor resolved `--db` against the state root while every other verb
    resolves it against the working directory, and reported a database that
    exists as absent;
  * `coord done` stored the resolved absolute path of the proof, which no
    containment check can accept, so doctor went red on the first success and
    stayed red.

Each test also pins the property that must survive the fix: a pointer that
escapes the project root is still invalid, whether or not it has been produced.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord.config import connect
from coordharness.safety.doctor import run_doctor

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _env(project: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The fixture models one Claude session. Do not let a parent Codex or
    # explicitly routed harness process turn that into an ambiguous identity.
    for leaked in (
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_WORKTREE_ID",
        "CODEX_CONVERSATION_ID",
        "COORD_ACTOR",
        "COORD_SESSION_ID",
        "COORD_PARENT_SESSION_ID",
    ):
        env.pop(leaked, None)
    env.update({
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
        "CLAUDE_CODE_SESSION_ID": "claude:frontend",
    })
    return env


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@invalid",
        },
    )


def _run(project: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd or project),
        capture_output=True,
        text=True,
        env=_env(project),
    )


def _doctor(project: Path, *args: str, cwd: Path | None = None):
    result = _run(project, "-m", "coordharness.coord.cli", *args, "doctor", cwd=cwd)
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def _finding(report: dict, finding_id: str) -> dict:
    return next(item for item in report["findings"] if item["id"] == finding_id)


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """A throwaway project seeded exactly the way the README tells a reader to."""
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    (project / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
    reports = project / "docs" / "reports"
    reports.mkdir(parents=True)
    (reports / "ui-104.md").write_text(
        "# UI-104\n\nKeyboard navigation pass complete.\n", encoding="utf-8"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "initial")

    result = _run(project, "-m", "coordharness.demo", "--quiet")
    assert result.returncode == 0, result.stderr
    return project


def test_doctor_passes_on_the_board_the_demo_seeds(seeded: Path) -> None:
    """Bug 1: the officially seeded board was BLOCKED with exit 2."""
    code, report = _doctor(seeded)

    assert (code, report["status"]) == (0, "PASS")

    pointers = _finding(report, "doctor.public_paths")["details"]
    assert pointers["invalid_pointer_count"] == 0
    # Not silenced: a row that has declared where its proof will land and has
    # not produced it yet is still counted and named.
    assert pointers["pending_pointer_count"] > 0

    jobs = _finding(report, "doctor.jobs_projection")["details"]
    assert jobs["problem_codes"] == []
    assert jobs["sidecar_count"] > 0


def test_every_seeded_sidecar_names_a_work_row_that_exists(seeded: Path) -> None:
    """The seeded telemetry is bound to the board, as the launcher requires."""
    db = seeded / ".coordharness" / "coord.db"
    conn = sqlite3.connect(db)
    try:
        work_ids = {row[0] for row in conn.execute("SELECT work_id FROM work_items")}
    finally:
        conn.close()

    sidecars = sorted((seeded / ".coordharness" / "job_progress").glob("*.json"))
    assert sidecars
    for path in sidecars:
        payload = json.loads(path.read_text(encoding="utf-8"))
        bound = str(payload.get("roadmap_id") or payload.get("work_id") or "")
        assert bound in work_ids, f"{path.name} names {bound!r}"


def test_a_pointer_that_escapes_the_project_root_is_still_invalid(tmp_path: Path) -> None:
    """The ablation for bug 1: not-yet-produced must not mean not-checked."""
    project = tmp_path / "project"
    state = project / ".coordharness"
    outside = tmp_path / "outside"
    project.mkdir()
    state.mkdir()
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    db = state / "coord.db"
    bootstrap_database(db)

    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO work_items(work_id,title,intent_state,done_signal,created_at,updated_at)"
            " VALUES ('ESCAPE-1','escaping proof','planned','escape/report.md',1,1)"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    pointers = _finding(report, "doctor.public_paths")
    assert pointers["status"] == "BLOCKED"
    assert pointers["details"]["invalid_pointer_fields"] == ["ESCAPE-1:done_signal"]
    assert pointers["details"]["pending_pointer_fields"] == []


def test_doctor_reads_the_relative_db_the_caller_typed(seeded: Path, tmp_path: Path) -> None:
    """Bug 2: an existing database was reported absent from another directory."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    relative = os.path.relpath(seeded / ".coordharness" / "coord.db", elsewhere)

    code, report = _doctor(seeded, "--db", relative, cwd=elsewhere)

    schema = _finding(report, "doctor.schema")
    assert schema["status"] == "PASS", schema
    assert schema["details"].get("database_present") is not False
    assert (code, report["status"]) == (0, "PASS")


def test_a_database_outside_the_state_root_is_named_not_called_absent(
    tmp_path: Path,
) -> None:
    """Bug 2's other half: containment still fails closed, but says why."""
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    db = outside / "coord.db"
    bootstrap_database(db)

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    schema = _finding(report, "doctor.schema")
    assert schema["status"] == "BLOCKED"
    assert schema["details"]["database_outside_state_root"] is True
    assert schema["details"]["database_present"] is True
    assert str(tmp_path) not in json.dumps(report)


def test_completion_stores_a_repo_relative_proof_and_doctor_stays_green(
    seeded: Path,
) -> None:
    """Bug 3: the first successful completion turned doctor permanently red."""
    before_code, _ = _doctor(seeded)
    assert before_code == 0

    claim = _run(
        seeded, "-m", "coordharness.coord.cli", "claim", "UI-104", "--step", "starting"
    )
    assert claim.returncode == 0, claim.stderr
    done = _run(seeded, "-m", "coordharness.coord.cli", "done", "UI-104")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["artifact_path"] == "docs/reports/ui-104.md"

    conn = sqlite3.connect(seeded / ".coordharness" / "coord.db")
    try:
        stored = [row[0] for row in conn.execute("SELECT path FROM artifacts")]
    finally:
        conn.close()
    assert stored == ["docs/reports/ui-104.md"]
    assert not any(Path(value).is_absolute() for value in stored)

    after_code, after = _doctor(seeded)
    assert (after_code, after["status"]) == (0, "PASS")
    assert _finding(after, "doctor.public_paths")["details"]["invalid_pointer_count"] == 0


def test_an_absolute_artifact_row_is_reported_and_only_blocks_when_it_escapes(
    tmp_path: Path,
) -> None:
    """Databases written by the old writer already hold absolute proofs.

    Containment and existence are still proven for them, so they are reported
    rather than treated as unproven -- otherwise a user who completed one claim
    before the fix could never get a green doctor again. A path that leaves the
    project root still blocks.
    """
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    reports = project / "docs" / "reports"
    reports.mkdir(parents=True)
    proof = reports / "legacy.md"
    proof.write_text("# legacy\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    stray = outside / "stray.md"
    stray.write_text("# stray\n", encoding="utf-8")
    db = state / "coord.db"
    bootstrap_database(db)

    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO work_items(work_id,title,intent_state,created_at,updated_at)"
            " VALUES ('LEG-1','legacy proof','done',1,1)"
        )
        conn.execute(
            "INSERT INTO artifacts(artifact_id,work_id,path,kind,validation_json,created_at)"
            " VALUES ('art-legacy','LEG-1',?,'done_signal','{}',1)",
            (str(proof.resolve()),),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    report = run_doctor(db_path=db, project_root=project, state_root=state, now=2)
    pointers = _finding(report, "doctor.public_paths")
    assert pointers["status"] == "PASS"
    assert pointers["details"]["absolute_pointer_fields"] == ["art-legacy:artifact_path"]
    assert pointers["details"]["invalid_pointer_count"] == 0

    conn = connect(db)
    try:
        conn.execute(
            "UPDATE artifacts SET path=? WHERE artifact_id='art-legacy'",
            (str(stray.resolve()),),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    escaped = _finding(
        run_doctor(db_path=db, project_root=project, state_root=state, now=2),
        "doctor.public_paths",
    )
    assert escaped["status"] == "BLOCKED"
    assert escaped["details"]["invalid_pointer_fields"] == ["art-legacy:artifact_path"]
