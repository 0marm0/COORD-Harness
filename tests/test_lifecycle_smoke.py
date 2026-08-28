"""End-to-end proof that a clone of this repository actually coordinates work.

These tests build a database from the shipped schema, seed the demo board, and
drive a work item through its whole life. They assert on behaviour that would
break silently if the extraction had gone wrong -- in particular the two guards
that make the board trustworthy:

  * you cannot claim work assigned to someone else, and
  * you cannot complete work without a claim and a committed artifact.

A test that only checked "done sets status to done" would pass against a harness
whose guards had been stripped, which is exactly the failure worth catching.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@invalid",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@invalid"},
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project with a git repository and a seeded board."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "reports").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(".coordharness/\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")

    from coordharness import demo

    demo.seed(tmp_path / ".coordharness" / "coord.db", quiet=True)
    return tmp_path


def coord(project: Path, *args: str, session: str = "claude:frontend") -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
    }
    for key in (
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_CONVERSATION_ID",
        "CODEX_WORKTREE_ID",
        "STARSHIP_SESSION_KEY",
        "COORD_ACTOR",
        "COORD_SESSION_ID",
    ):
        env.pop(key, None)
    if session.startswith("codex:"):
        env["CODEX_SESSION_ID"] = session
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return subprocess.run(
        [sys.executable, "-m", "coordharness.coord.cli",
         "--db", str(project / ".coordharness" / "coord.db"), *args],
        cwd=project, capture_output=True, text=True, env=env,
    )


def board(project: Path) -> dict[str, str]:
    result = coord(project, "board")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["rows"]
    return {row["work_id"]: row.get("status") for row in rows}


def owners(project: Path) -> dict[str, str]:
    result = coord(project, "board")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["rows"]
    return {row["work_id"]: (row.get("owner") or row.get("assignee") or "") for row in rows}


def test_schema_builds_from_source(tmp_path: Path) -> None:
    """A fresh database comes up from the shipped schema with no product data."""
    from coordharness.coord import create_schema

    db = tmp_path / "coord.db"
    create_schema.apply_schema(db)

    import sqlite3

    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"work_items", "claims", "events", "agent_sessions", "runs"} <= tables
    assert {"v_work_owner", "v_session_rollup"} <= views
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def test_demo_seeds_a_board(project: Path) -> None:
    statuses = board(project)
    assert "INIT-UI" in statuses
    assert len([k for k in statuses if k.startswith("UI-")]) >= 5


def test_fresh_cli_can_create_claim_note_prove_and_complete(tmp_path: Path) -> None:
    """The installed CLI can bootstrap real work without the demo seeder."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")

    work_id = "DEMO-CDX-FIRST-REAL-WORK"
    created = coord(
        tmp_path,
        "create",
        work_id,
        "--title",
        "Create the first standalone work item",
        "--module",
        "runtime",
        "--done-signal",
        "artifacts/first-work.json",
        "--acceptance",
        "The proof artifact records the completed lifecycle",
        "--note",
        "Exercise the public fresh-database workflow",
        session="codex:fresh",
    )
    assert created.returncode == 0, created.stderr
    created_payload = json.loads(created.stdout)
    assert created_payload["created"] is True
    assert created_payload["done_signal"] == "artifacts/first-work.json"
    assert board(tmp_path)[work_id] == "queued"

    claimed = coord(
        tmp_path,
        "claim",
        work_id,
        "--step",
        "writing standalone proof",
        session="codex:fresh",
    )
    assert claimed.returncode == 0, claimed.stderr
    assert json.loads(claimed.stdout)["claim_fence"]

    noted = coord(
        tmp_path,
        "note",
        work_id,
        "--body",
        "Proof is ready for the declared completion gate.",
        "--to",
        "claude",
        session="codex:fresh",
    )
    assert noted.returncode == 0, noted.stderr
    assert json.loads(noted.stdout)["event_id"]

    artifact = tmp_path / "artifacts" / "first-work.json"
    artifact.parent.mkdir()
    artifact.write_text('{"status":"complete"}\n', encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "add standalone proof")

    completed = coord(
        tmp_path,
        "done",
        work_id,
        "--artifact",
        "artifacts/first-work.json",
        session="codex:fresh",
    )
    assert completed.returncode == 0, completed.stderr
    completed_payload = json.loads(completed.stdout)
    assert completed_payload["canonical_event_id"] > 0
    assert board(tmp_path)[work_id] == "done"


def test_claim_makes_work_running(project: Path) -> None:
    result = coord(project, "claim", "UI-101", "--step", "splitting the preferences panel")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    assert board(project)["UI-101"] == "running"


def test_cannot_claim_another_actors_work(project: Path) -> None:
    """UI-103 belongs to codex; a claude session must not be able to take it.

    The guarantee is about *ownership*, not status: the demo board seeds this
    row already running under its codex owner, so asserting it is not running
    would only be asserting that the seed left it idle. What must hold is that
    a refused claim leaves the row with the owner it had.
    """
    before = owners(project)["UI-103"]
    assert before.startswith("codex"), f"fixture expects a codex-owned row, got {before!r}"

    result = coord(project, "claim", "UI-103", session="claude:frontend")
    assert result.returncode != 0
    assert "handoff" in result.stderr.lower()
    assert owners(project)["UI-103"] == before, "a refused claim moved the row's owner"


def test_handoff_diagram_quotes_the_cli_refusal_exactly(project: Path) -> None:
    result = coord(project, "claim", "UI-103", session="claude:frontend")
    expected = (
        "cannot claim work assigned to 'codex' from 'claude' session "
        "'claude:frontend'; use a typed handoff/controller transition"
    )
    assert result.returncode != 0
    assert expected in result.stderr
    diagram = (REPO / "docs/assets/handoff-sequence.svg").read_text(encoding="utf-8")
    normalized = " ".join(
        fragment.split("</text>")[0].split(">")[-1]
        for fragment in diagram.split('<text class="error"')[1:]
    )
    assert expected in normalized


def test_completion_requires_a_committed_artifact(project: Path) -> None:
    """An artifact that exists but is not in version control is not proof."""
    coord(project, "claim", "UI-101", "--step", "splitting the preferences panel")
    artifact = project / "docs" / "reports" / "ui-101.md"
    artifact.write_text("# Settings screen\n")

    uncommitted = coord(project, "done", "UI-101", "--artifact", "docs/reports/ui-101.md")
    assert uncommitted.returncode != 0, "an uncommitted artifact was accepted as proof"
    assert "git add docs/reports/ui-101.md" in uncommitted.stderr
    assert board(project)["UI-101"] == "running"

    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "artifact")

    committed = coord(project, "done", "UI-101", "--artifact", "docs/reports/ui-101.md")
    assert committed.returncode == 0, committed.stderr
    assert board(project)["UI-101"] == "done"


def test_completion_reports_empty_proof_as_incomplete(project: Path) -> None:
    coord(project, "claim", "UI-101", "--step", "writing proof")
    artifact = project / "docs" / "reports" / "ui-101.md"
    artifact.write_bytes(b"")
    _git(project, "add", "-A")

    result = coord(project, "done", "UI-101", "--artifact", "docs/reports/ui-101.md")

    assert result.returncode != 0
    assert "empty, incomplete" in result.stderr
    assert "git add" not in result.stderr


def test_completion_reports_missing_proof_as_missing(project: Path) -> None:
    coord(project, "claim", "UI-101", "--step", "writing proof")

    result = coord(project, "done", "UI-101", "--artifact", "docs/reports/ui-101.md")

    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert "git add" not in result.stderr


def test_policy_pipeline_runs_on_every_write(project: Path) -> None:
    """Each lifecycle write reports the checks it passed, in a stable order."""
    result = coord(project, "claim", "UI-101", "--step", "splitting the preferences panel")
    policy = json.loads(result.stdout)["policy"]
    assert policy["ok"] is True
    assert policy["pass_order"][:3] == ["creation_lint", "loop_doctor", "token_budget"]
    assert all(check["status"] == "ok" for check in policy["results"])
