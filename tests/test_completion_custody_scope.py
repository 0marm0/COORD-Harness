"""Completion custody is required of every artifact type, not just Markdown.

`src/coordharness/jobs/status.py`'s `done_signal_custodied()` decides whether
a declared proof is enough to complete a claim. Until 0.1.0 its deciding line
read `path.suffix.lower() != ".md" or completion_proof_is_tracked(path, root)`
-- so a `.txt`, `.json` or `.rst` proof satisfied the gate on existence alone,
tracked by git or not, and the product's headline promise ("completion is
refused until the declared proof is in the index") was true of exactly one file
extension.

This is a **contract test**, not a characterization one. It pins the rule as
ruled: existence is never waived, custody is required of every suffix except a
small declared list of kinds that structurally cannot live in git, and one
environment variable rebinds that list -- including all the way back to the old
behavior. The refusal is exercised through `complete_claim`, the real
completion path a caller reaches, so a widening that the predicate honours but
the gate never consults would still fail here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.jobs import status

WORK_ID = "DEMO-CLA-CUSTODY-SCOPE"
SESSION = "claude:custody-scope"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def project(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with a bootstrapped coord.db, for the real completion path."""
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("COORD_HOME", str(repo / ".coordharness"))
    bootstrap_database(repo / ".coordharness" / "coord.db")
    return repo


@pytest.fixture
def conn(project: Path):
    connection = connect(project / ".coordharness" / "coord.db")
    try:
        yield connection
    finally:
        connection.close()


def _write(repo: Path, relpath: str, content: str) -> Path:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _git_add(repo: Path, relpath: str) -> None:
    subprocess.run(["git", "add", relpath], cwd=repo, check=True)


def _claimed(conn, proof: str) -> str:
    coord_db.register_session(conn, SESSION, "claude")
    coord_db.upsert_work(
        conn,
        WORK_ID,
        title="A work item the custody gate is measured against",
        assignee="claude",
        done_signal=proof,
        acceptance_json=json.dumps(["the proof artifact is in git's index"]),
        intent_state="queued",
    )
    return coord_db.claim_work(conn, SESSION, WORK_ID, step="starting")


def _claim_status(conn, claim_id: str) -> str:
    return str(
        conn.execute(
            "SELECT status FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()["status"]
    )


# --- the predicate ---------------------------------------------------------


@pytest.mark.parametrize(
    ("relpath", "content"),
    [
        ("proof/a.md", "# proof\n"),
        ("proof/b.txt", "proof body\n"),
        ("proof/c.json", '{"ok": true}'),
        ("proof/d.rst", "proof\n=====\n"),
        ("proof/e.html", "<p>proof</p>\n"),
        ("proof/no-suffix-at-all", "proof body\n"),
    ],
)
def test_an_untracked_proof_is_refused_whatever_its_suffix(
    repo: Path, relpath: str, content: str
) -> None:
    """The rule is general. A proof with no suffix resolves toward custody too."""
    _write(repo, relpath, content)

    assert status.done_signal_custodied(relpath, repo) is False

    _git_add(repo, relpath)

    assert status.done_signal_custodied(relpath, repo) is True


def test_an_exempt_suffix_is_accepted_untracked_but_only_if_it_exists(
    repo: Path,
) -> None:
    """The exemption is about custody, never about proof."""
    relpath = "artifacts/warehouse.duckdb"
    assert ".duckdb" in status.DEFAULT_CUSTODY_EXEMPT_SUFFIXES

    assert status.done_signal_custodied(relpath, repo) is False, (
        "an exempt suffix that does not exist must still be refused"
    )

    _write(repo, relpath, "x" * (64 * 1024))

    assert status.done_signal_custodied(relpath, repo) is True


def test_every_exempt_suffix_is_a_kind_that_cannot_live_in_git() -> None:
    """The list stays small, named and justified -- not a second `!= '.md'`."""
    assert status.DEFAULT_CUSTODY_EXEMPT_SUFFIXES == frozenset(
        {".parquet", ".duckdb", ".db", ".joblib", ".bz2", ".backup"}
    )
    # The kinds the census found as plain text are pointedly absent.
    for text_kind in (".md", ".txt", ".json", ".jsonl", ".csv", ".py", ".html", ".rst"):
        assert text_kind not in status.DEFAULT_CUSTODY_EXEMPT_SUFFIXES


def test_a_directory_proof_is_custodied_by_the_files_inside_it(repo: Path) -> None:
    """Git's index holds no directories, so the literal question is unaskable."""
    _write(repo, "bundle.d/part.txt", "content\n")

    assert status.done_signal_custodied("bundle.d", repo) is False

    _git_add(repo, "bundle.d/part.txt")

    assert status.done_signal_custodied("bundle.d", repo) is True


def test_the_duckdb_table_signal_still_answers_by_reading_the_table(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`path::table` names rows, not a file to stage; custody never applies."""
    duckdb = pytest.importorskip("duckdb")
    db_path = repo / "artifacts" / "results.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE TABLE findings(id INTEGER)")
        # Exempt-suffix carve-outs must not be what makes this pass, so the
        # exemption list is emptied for the whole assertion.
        monkeypatch.setenv(status.CUSTODY_EXEMPT_ENV, "")
        assert status.done_signal_custodied("artifacts/results.duckdb::findings", repo) is False

        con.execute("INSERT INTO findings VALUES (1)")
    finally:
        con.close()

    assert status.done_signal_custodied("artifacts/results.duckdb::findings", repo) is True


# --- the environment override ----------------------------------------------


def test_the_override_restores_the_old_behavior(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`*` is the emergency off switch: existence-only for every suffix."""
    _write(repo, "proof/b.txt", "proof body\n")
    _write(repo, "proof/c.json", '{"ok": true}')

    monkeypatch.setenv(status.CUSTODY_EXEMPT_ENV, status.CUSTODY_EXEMPT_ALL)

    assert status.done_signal_custodied("proof/b.txt", repo) is True
    assert status.done_signal_custodied("proof/c.json", repo) is True
    # Existence is still not waived, even with the gate off.
    assert status.done_signal_custodied("proof/never-written.json", repo) is False


def test_the_override_admits_one_unusual_artifact_kind(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, "artifacts/weights.myformat", "binary-ish\n")
    _write(repo, "artifacts/report.json", "{}")

    monkeypatch.setenv(status.CUSTODY_EXEMPT_ENV, "myformat")

    assert status.done_signal_custodied("artifacts/weights.myformat", repo) is True
    # An explicit value replaces the default list rather than extending it, so
    # the built-in kinds are no longer exempt and plain text is still gated.
    assert status.done_signal_custodied("artifacts/report.json", repo) is False


def test_an_empty_override_exempts_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, "artifacts/warehouse.duckdb", "x" * (64 * 1024))

    monkeypatch.setenv(status.CUSTODY_EXEMPT_ENV, "   ")

    assert status.done_signal_custodied("artifacts/warehouse.duckdb", repo) is False


def test_a_garbled_override_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(status.CUSTODY_EXEMPT_ENV, "../etc/passwd")

    with pytest.raises(ValueError) as excinfo:
        status.custody_exempt_suffixes()

    assert status.CUSTODY_EXEMPT_ENV in str(excinfo.value)


# --- the real completion path ----------------------------------------------


def test_complete_claim_refuses_an_untracked_json_proof(conn, project: Path) -> None:
    """The gate, not just the predicate: this completion succeeded before 0.1.0."""
    proof = "artifacts/report.json"
    claim_id = _claimed(conn, proof)
    _write(project, proof, '{"rows": 3}')

    with pytest.raises(ValueError) as excinfo:
        coord_db.complete_claim(
            conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
        )

    message = str(excinfo.value)
    assert proof in message, "the refusal must name the artifact"
    assert f"git add {proof}" in message, "the refusal must carry the fix"
    assert status.CUSTODY_EXEMPT_ENV in message, "the refusal must name the way out"
    assert "not only Markdown" in message, "the refusal must own the behavior change"
    assert _claim_status(conn, claim_id) == "running"
    assert coord_db.done_signal_satisfied(conn, proof, project) is False


def test_complete_claim_accepts_the_same_json_proof_once_it_is_tracked(
    conn, project: Path
) -> None:
    proof = "artifacts/report.json"
    claim_id = _claimed(conn, proof)
    _write(project, proof, '{"rows": 3}')
    _git_add(project, proof)

    coord_db.complete_claim(
        conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
    )

    assert _claim_status(conn, claim_id) == "completed"


def test_complete_claim_accepts_an_untracked_exempt_proof(conn, project: Path) -> None:
    proof = "artifacts/dataset.parquet"
    claim_id = _claimed(conn, proof)
    _write(project, proof, "x" * (4 * 1024))

    coord_db.complete_claim(
        conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
    )

    assert _claim_status(conn, claim_id) == "completed"


def test_complete_claim_refuses_an_exempt_proof_that_does_not_exist(
    conn, project: Path
) -> None:
    """Custody is exempt; existence is not."""
    proof = "artifacts/dataset.parquet"
    claim_id = _claimed(conn, proof)

    with pytest.raises(ValueError) as excinfo:
        coord_db.complete_claim(
            conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
        )

    assert "does not exist" in str(excinfo.value)
    assert _claim_status(conn, claim_id) == "running"


def test_complete_claim_still_refuses_an_untracked_markdown_proof(
    conn, project: Path
) -> None:
    """Markdown behaves exactly as it did; its refusal gained no new prose."""
    proof = "reports/summary.md"
    claim_id = _claimed(conn, proof)
    _write(project, proof, "# summary\n")

    with pytest.raises(ValueError) as excinfo:
        coord_db.complete_claim(
            conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
        )

    message = str(excinfo.value)
    assert "not carried by git's index" in message
    assert f"git add {proof}" in message
    assert "not only Markdown" not in message


def test_complete_claim_accepts_an_untracked_json_proof_under_the_override(
    conn, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one knob reaches the gate, not only the predicate."""
    proof = "artifacts/report.json"
    claim_id = _claimed(conn, proof)
    _write(project, proof, '{"rows": 3}')
    monkeypatch.setenv(status.CUSTODY_EXEMPT_ENV, status.CUSTODY_EXEMPT_ALL)

    coord_db.complete_claim(
        conn, claim_id, proof_root=project, session_id=SESSION, actor="claude"
    )

    assert _claim_status(conn, claim_id) == "completed"
