from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from coordharness import config, entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.coord.ingest import resolve_identity
from coordharness.jobs import sidecar_writer


REPO = Path(__file__).resolve().parents[1]


def test_shared_bootstrap_is_idempotent_and_applies_numbered_migrations(tmp_path: Path):
    db = tmp_path / "state" / "coord.db"
    first = bootstrap_database(db)
    second = bootstrap_database(db)
    assert first["migrations_applied"] == [
        "002_exact_authority.sql",
        "003_provenance_causal_trace.sql",
    ]
    assert second["migrations_applied"] == []


@pytest.mark.parametrize("actor", ["claude", "codex", "local", "worker-7", "review.bot"])
def test_coord_actor_supports_builtin_and_safe_arbitrary_names(actor: str):
    identity = resolve_identity({"COORD_ACTOR": actor, "COORD_SESSION_ID": "task-1"})
    assert identity["actor"] == actor
    assert identity["session_id"] == f"{actor}:task-1"


@pytest.mark.parametrize("actor", ["", "7bad", "bad actor", "../escape"])
def test_coord_actor_rejects_unsafe_names(actor: str):
    with pytest.raises(ValueError):
        config.actor_name(actor)


def test_coord_home_isolates_database_jobs_and_knowledge(tmp_path: Path):
    project = tmp_path / "project"
    state = tmp_path / "isolated-state"
    project.mkdir()
    code = (
        "import json; from coordharness import config; "
        "from coordharness.knowledge import context_federator as cf; "
        "from coordharness.bootstrap import bootstrap_database; bootstrap_database(); "
        "print(json.dumps([str(config.coord_db_path()),str(config.job_progress_dir()),"
        "str(config.knowledge_db_path()),str(cf.DEFAULT_ACCEPTED_MEMORY_STORE),"
        "str(cf.DEFAULT_COORD_DB)]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(REPO / "src"),
             "COORD_PROJECT_ROOT": str(project), "COORD_HOME": str(state)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    paths = [Path(value) for value in json.loads(result.stdout)]
    assert paths[0] == state / "coord.db"
    assert paths[1] == state / "job_progress"
    assert paths[2] == state / "knowledge.db"
    assert paths[3] == state / "accepted_memory_r4"
    assert paths[4] == state / "coord.db"
    assert not (project / ".coordharness").exists()


def test_sidecar_writer_never_persists_raw_absolute_host_paths(tmp_path: Path):
    destination = tmp_path / "job.json"
    secret_prefix = "/" + "/".join(("Users", "example", "private", "repository"))
    sidecar_writer._atomic_write(
        {
            "job_id": "privacy",
            "script": f"{secret_prefix}/scripts/task.py",
            "nested": {"progress_file": f"{secret_prefix}/out/progress.json"},
        },
        destination,
    )
    raw = destination.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert secret_prefix not in raw
    assert payload["script"].startswith("external://")
    assert payload["nested"]["progress_file"].startswith("external://")


def test_coord_claim_returns_exact_custody_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "state" / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn,
            "FENCE-1",
            title="Synthetic exact-fence claim",
            assignee="local",
        )
    finally:
        conn.close()

    monkeypatch.setenv("COORD_ACTOR", "local")
    monkeypatch.setenv("COORD_SESSION_ID", "fence-test")
    assert (
        entry.main(
            [
                "--db",
                str(db),
                "claim",
                "FENCE-1",
                "--step",
                "capture launch authority",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response["claim_id"]
    assert response["claim_fence"]

    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT lease_token FROM claims WHERE claim_id=?",
            (response["claim_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert response["claim_fence"] == str(row["lease_token"])


def test_shell_scripts_are_syntax_valid():
    scripts = sorted((REPO / "scripts").glob("*.sh"))
    result = subprocess.run(["bash", "-n", *map(str, scripts)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_short_tracked_job_and_public_status(tmp_path: Path):
    project = tmp_path / "project"
    state = tmp_path / "state"
    project.mkdir()
    db = state / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        session_id = "local:smoke"
        coord_db.register_session(conn, session_id, "local", lease_s=600)
        coord_db.upsert_work(
            conn, "SMOKE-1", title="Synthetic tracked-job smoke", assignee="local"
        )
        claim_id = coord_db.claim_work(
            conn, session_id, "SMOKE-1", step="run synthetic command", lease_s=600
        )
        claim_fence = str(
            conn.execute(
                "SELECT lease_token FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO / "src"),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(state),
        "COORD_ACTOR": "local",
        "COORD_DB": str(db),
    }
    command_source = "PRIVATE_COMMAND_SOURCE_7a8df = 'never-persist'"
    process_pattern = "PRIVATE_PROCESS_PATTERN_91ec2"
    command = [sys.executable, "-c", command_source]
    launch = subprocess.run(
        [sys.executable, "-m", "coordharness.jobs.cli", "launch",
         "--job-id", "smoke", "--roadmap-id", "SMOKE-1", "--cap-gb", "1",
         "--session-id", session_id, "--claim-id", claim_id,
         "--claim-fence", claim_fence, "--coord-db", str(db),
         "--proc-pattern", process_pattern, "--", *command],
        cwd=project, env=env, capture_output=True, text=True, timeout=15,
    )
    assert launch.returncode == 0, launch.stderr
    status = subprocess.run(
        [sys.executable, "-m", "coordharness.jobs.cli", "status"],
        cwd=project, env=env, capture_output=True, text=True,
    )
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["jobs"][0]["state"] == "done"
    assert str(tmp_path) not in status.stdout
    persisted = (state / "job_progress" / "smoke.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in persisted
    assert command_source not in persisted
    assert command_source not in status.stdout
    assert process_pattern not in persisted
    assert process_pattern not in status.stdout
    sidecar = json.loads(persisted)
    assert sidecar["script"] == "python"
    assert sidecar["executable_class"] == "python"
    assert sidecar["allowed_option_names"] == ["-c"]
    assert sidecar["option_count"] == 1
    assert sidecar["argument_count"] == 1
    assert "command_sha256" not in sidecar
    assert "proc_pattern_sha256" not in sidecar
