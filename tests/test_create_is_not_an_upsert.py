"""`coord create` claims authorship, so it has to actually create the row.

Two agents picking the same date-and-lane work id is not an exotic failure. It
is what happens whenever two sessions choose the next free suffix without
talking to each other, which is the normal case for a fleet. The create path
used to answer that with a silent merge: its existence check ran outside the
write transaction, the write itself was an upsert, and the loser was told
``"created": true`` while its title, assignee, done signal and acceptance
criteria were overwritten by -- or overwrote -- someone else's.

These tests pin the three parts of the repair that can regress independently:

  * a create that lost the race is refused, and the winner's row survives;
  * no two creates of one id both report having created it;
  * the shared upsert path still amends an existing row, because claim-time
    born-complete metadata legitimately fills fields on a row that exists. The
    defect was ``create`` claiming authorship, never the amend itself.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from coordharness import entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db, creation_lint
from coordharness.coord.config import connect

WORK_ID = "DEMO-CLA-SAME-ID"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project running as one exact lane."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-qm", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    for name in (
        "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
        "CODEX_CONVERSATION_ID", "CODEX_WORKTREE_ID", "STARSHIP_SESSION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setenv("COORD_ACTOR", "claude")
    monkeypatch.setenv("COORD_SESSION_ID", "claude:racer")
    bootstrap_database(tmp_path / ".coordharness" / "coord.db")
    return tmp_path


def _db(project: Path) -> Path:
    return project / ".coordharness" / "coord.db"


def _create_argv(project: Path, *, title: str, assignee: str, tag: str) -> list[str]:
    return [
        "--db", str(_db(project)), "create", WORK_ID,
        "--title", title,
        "--assignee", assignee,
        "--module", tag,
        "--done-signal", f"artifacts/{tag}.json",
        "--acceptance", f"{tag} acceptance",
        "--note", f"{tag} note",
        "--tier", "T2",
    ]


def _row(project: Path) -> dict:
    conn = connect(_db(project))
    try:
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def test_a_create_that_lost_the_race_is_refused_and_the_winner_survives(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loser is told no, and does not overwrite the row it lost to.

    The interleaving is made deterministic by parking the second create at a
    point every version of this code passes through, *after* the moment the old
    pre-check consulted the database and *before* anything is written. That is
    exactly the window two live sessions share; the barrier only removes the
    timing luck from reproducing it.
    """
    reached = threading.Event()
    release = threading.Event()
    real_normalize = creation_lint.normalize_creation_fields

    def parked(*args, **kwargs):
        reached.set()
        assert release.wait(timeout=20), "the winning create never finished"
        return real_normalize(*args, **kwargs)

    loser_code: list[int] = []

    def run_loser() -> None:
        loser_code.append(
            entry.main(_create_argv(project, title="Agent B task", assignee="codex", tag="beta"))
        )

    creation_lint.normalize_creation_fields = parked
    thread = threading.Thread(target=run_loser)
    try:
        thread.start()
        assert reached.wait(timeout=20), "the second create never started"
        # The winner runs to completion while the loser is parked: it holds no
        # lock, so this is a clean commit, not contention.
        creation_lint.normalize_creation_fields = real_normalize
        winner_code = entry.main(
            _create_argv(project, title="Agent A task", assignee="claude", tag="alpha")
        )
        release.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
    finally:
        creation_lint.normalize_creation_fields = real_normalize
        release.set()

    assert winner_code == 0, "the first create to commit must succeed"
    assert loser_code == [1], (
        "the create that found the id already taken must be refused, not merged"
    )

    row = _row(project)
    assert row["title"] == "Agent A task", "the winner's row was overwritten by the loser"
    assert row["assignee"] == "claude"
    assert row["done_signal"] == "artifacts/alpha.json"
    assert json.loads(row["acceptance_json"]) == ["alpha acceptance"]

    captured = capsys.readouterr()
    emitted = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert len([item for item in emitted if item.get("created")]) == 1, (
        f"exactly one create may report having created this row: {captured.out!r}"
    )
    assert "Traceback" not in captured.err
    assert WORK_ID in captured.err and "already exists" in captured.err


def test_create_refuses_an_existing_id_from_inside_its_own_transaction(
    project: Path,
) -> None:
    """The check that matters is the one the write cannot outrun.

    Two connections both read a free id -- the state the old pre-check left the
    caller in -- and only then write. The second write has to fail even though
    its own read said the id was free.
    """
    first = connect(_db(project))
    second = connect(_db(project))
    try:
        assert first.execute(
            "SELECT 1 FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone() is None
        assert second.execute(
            "SELECT 1 FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone() is None

        assert coord_db.create_work(
            first, WORK_ID, title="Agent A task", assignee="claude",
            done_signal="artifacts/alpha.json",
            acceptance_json=json.dumps(["alpha acceptance"]),
        ) is True

        with pytest.raises(coord_db.WorkIdCollisionError):
            coord_db.create_work(
                second, WORK_ID, title="Agent B task", assignee="codex",
                done_signal="artifacts/beta.json",
                acceptance_json=json.dumps(["beta acceptance"]),
            )

        row = second.execute(
            "SELECT title, assignee, done_signal FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        assert dict(row) == {
            "title": "Agent A task",
            "assignee": "claude",
            "done_signal": "artifacts/alpha.json",
        }
    finally:
        first.close()
        second.close()


def test_upsert_still_amends_an_existing_row(project: Path) -> None:
    """The repair is aimed at `create` alone.

    ``_upsert_work_unlocked`` is shared, and the amend branch carries real work:
    claim-time born-complete metadata fills fields on rows that already exist.
    A fix that turned every write into an insert-or-die would break that
    quietly, so pin it here rather than discover it in a lifecycle test.
    """
    conn = connect(_db(project))
    try:
        coord_db.upsert_work(conn, WORK_ID, title="Original", assignee="claude")
        coord_db.upsert_work(conn, WORK_ID, done_signal="artifacts/proof.json")
        row = conn.execute(
            "SELECT title, assignee, done_signal FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        assert dict(row) == {
            "title": "Original",
            "assignee": "claude",
            "done_signal": "artifacts/proof.json",
        }
    finally:
        conn.close()


def test_upsert_reports_whether_it_inserted(project: Path) -> None:
    """`created` has to be measured, not asserted, or it can drift back."""
    conn = connect(_db(project))
    try:
        with coord_db.tx(conn):
            assert coord_db._upsert_work_unlocked(conn, WORK_ID, {"title": "Original"}) is True
        with coord_db.tx(conn):
            assert coord_db._upsert_work_unlocked(conn, WORK_ID, {"title": "Amended"}) is False
    finally:
        conn.close()
