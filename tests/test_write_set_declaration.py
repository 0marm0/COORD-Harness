"""Declared write sets have to be reachable from a client, not just implemented.

``work_contracts.py`` has carried ``declare_write_set``/``write_set_overlaps``
for a while: the overlap query works, the ungrantable-scope refusal works, and
the finding names both rows and both sessions. None of that was reachable. No
CLI verb, no MCP tool, and no test touched it, while the copy-paste agent system
prompt in ``docs/agent-protocol.md`` instructed agents to "declare your write set
and check for overlaps before you start editing" -- an instruction no client
could carry out.

Two failures are asserted here that a test written against the implementation
alone would miss:

  * the table was not created by the schema bootstrap, only lazily on first
    write. The conflict query is a READ, and a read-only connection cannot run
    a CREATE, so the first caller to ask "who collides with me" on an untouched
    database got ``attempt to write a readonly database`` -- a write error about
    a table it never mentioned;
  * an undeclared claim is unknown, not safe. A report that silently omitted
    them would read identically whether the board was clean or nobody had
    declared anything at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.bootstrap import bootstrap_database  # noqa: E402
from coordharness.coord import mcp_coord_server, work_contracts  # noqa: E402
from coordharness.coord.config import connect, connect_ro  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project with a git repository and no coord database yet."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def db_path(project: Path) -> Path:
    return project / ".coordharness" / "coord.db"


def coord(
    project: Path,
    *args: str,
    session: str | None = "claude:alpha",
) -> subprocess.CompletedProcess:
    """Run the CLI as one named lane session."""
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
    }
    # Build the identity under test rather than inheriting whichever agent
    # launched pytest.
    for leaked in (
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_WORKTREE_ID",
        "CODEX_CONVERSATION_ID",
        "STARSHIP_SESSION_KEY",
        "COORD_ACTOR",
        "COORD_SESSION_ID",
        "COORD_PARENT_SESSION_ID",
    ):
        env.pop(leaked, None)
    if session is not None:
        env["COORD_ACTOR"] = session.split(":", 1)[0]
        env["COORD_SESSION_ID"] = session
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "coordharness.coord.cli",
            "--db",
            str(db_path(project)),
            *args,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )


def out(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def row(project: Path, work_id: str, *, session: str) -> None:
    lane = session.split(":", 1)[0]
    out(
        coord(
            project,
            "create",
            work_id,
            "--title",
            f"work for {lane}",
            "--module",
            "billing",
            "--tier",
            "T1",
            "--done-signal",
            f"artifacts/{work_id.lower()}.json",
            "--acceptance",
            "the edit lands where it was declared",
            "--note",
            "exercise the write-set declaration loop",
            session=session,
        )
    )


def counts(project: Path) -> tuple[int, int]:
    conn = sqlite3.connect(db_path(project))
    try:
        return (
            int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]),
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Defect 1: the table was missing from the bootstrap, so the first READ crashed
# --------------------------------------------------------------------------


def test_bootstrap_creates_the_write_set_table(tmp_path: Path) -> None:
    """A freshly bootstrapped database carries the table, not a promise of it."""
    database = tmp_path / "coord.db"
    bootstrap_database(database)

    conn = sqlite3.connect(database)
    try:
        tables = {
            str(name)
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "work_contract_write_sets" in tables
    # The other two work-contract tables share the same lazy-creation path and
    # therefore the same failure mode; bootstrapping one and not the others
    # would only move the crash.
    assert {"work_contract_done_signals", "work_contract_child_attempts"} <= tables


def test_the_conflict_query_answers_on_a_read_only_connection(tmp_path: Path) -> None:
    """The regression itself: this raised OperationalError before the fix.

    ``attempt to write a readonly database`` -- because the query reached for
    ``CREATE TABLE`` on a database where nothing had ever been declared.
    """
    database = tmp_path / "coord.db"
    bootstrap_database(database)

    conn = connect_ro(database)
    try:
        report = work_contracts.write_set_overlaps(conn)
    finally:
        conn.close()

    assert report.count == 0
    assert report.scanned_claims == 0
    # Distinguishable from "the query never ran": an empty finding list alone
    # cannot tell those two apart.
    assert report.schema_present is True


def test_a_database_without_the_table_reports_that_rather_than_a_clean_board(
    tmp_path: Path,
) -> None:
    """A pre-bootstrap database is unreadable here, and says so instead of lying."""
    database = tmp_path / "legacy.db"
    bootstrap_database(database)
    conn = sqlite3.connect(database)
    conn.execute("DROP TABLE work_contract_write_sets")
    conn.commit()
    conn.close()
    os.chmod(database, 0o444)

    conn = connect_ro(database)
    try:
        report = work_contracts.write_set_overlaps(conn)
    finally:
        conn.close()
        os.chmod(database, 0o644)

    assert report.schema_present is False
    assert report.count == 0


def test_the_bootstrapped_table_matches_the_lazily_created_one(tmp_path: Path) -> None:
    """Two declarations of one table must not drift into two different tables."""
    bootstrapped = tmp_path / "bootstrapped.db"
    bootstrap_database(bootstrapped)

    lazy = tmp_path / "lazy.db"
    lazy_conn = sqlite3.connect(lazy)
    work_contracts.ensure_schema(lazy_conn)

    booted_conn = sqlite3.connect(bootstrapped)
    try:
        for table in work_contracts._SCHEMA_TABLES:
            query = "SELECT sql FROM sqlite_master WHERE type='table' AND name=?"
            booted = booted_conn.execute(query, (table,)).fetchone()
            drifted = lazy_conn.execute(query, (table,)).fetchone()
            assert booted is not None and drifted is not None, table
            assert " ".join(str(booted[0]).split()) == " ".join(
                str(drifted[0]).split()
            ), table
    finally:
        booted_conn.close()
        lazy_conn.close()


# --------------------------------------------------------------------------
# Defect 2: no client could declare a write set or ask what collides
# --------------------------------------------------------------------------


def test_the_cli_offers_the_three_write_set_surfaces(project: Path) -> None:
    """Registered on the parser a shell user actually reaches."""
    top = coord(project, "--help")
    assert top.returncode == 0, top.stderr
    assert "declare-write-set" in top.stdout
    assert "conflicts" in top.stdout

    claim_help = coord(project, "claim", "--help")
    assert claim_help.returncode == 0, claim_help.stderr
    assert "--write-scope" in claim_help.stdout


def test_claiming_with_a_write_scope_declares_it(project: Path) -> None:
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")

    claimed = out(
        coord(
            project,
            "claim",
            "DEMO-CLA-BILLING-A",
            "--step",
            "editing",
            "--write-scope",
            "src/billing/",
            session="claude:alpha",
        )
    )

    # A bare value is a path, and the trailing separator is normalized away.
    assert claimed["write_set"] == [{"kind": "path", "value": "src/billing"}]
    assert claimed["write_set_conflicts"]["count"] == 0
    # The pre-existing claim contract is untouched.
    assert claimed["claim_id"].startswith("clm-")
    assert claimed["claim_fence"]


def test_two_sessions_on_overlapping_paths_are_named_by_conflicts(
    project: Path,
) -> None:
    """The finding names both rows, both claims, both sessions and both scopes."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    row(project, "DEMO-CDX-BILLING-B", session="codex:beta")
    first = out(
        coord(
            project, "claim", "DEMO-CLA-BILLING-A",
            "--write-scope", "src/billing/",
            session="claude:alpha",
        )
    )
    second = out(
        coord(
            project, "claim", "DEMO-CDX-BILLING-B",
            # Narrower than the first scope, and inside it. A path scope is a
            # prefix match in both directions, so the broad claim and the
            # single-file claim have to find each other.
            "--write-scope", "path=src/billing/retries.py",
            session="codex:beta",
        )
    )

    # The second claimant is told at claim time, which is the only moment the
    # warning is still cheap to act on.
    assert second["write_set_conflicts"]["count"] == 1

    report = out(coord(project, "conflicts"))
    assert report["count"] == 1
    finding = report["findings"][0]
    assert finding["kind"] == "path"
    assert {finding["work_a"], finding["work_b"]} == {
        "DEMO-CLA-BILLING-A",
        "DEMO-CDX-BILLING-B",
    }
    assert {finding["claim_a"], finding["claim_b"]} == {
        first["claim_id"],
        second["claim_id"],
    }
    assert {finding["session_a"], finding["session_b"]} == {
        "claude:alpha",
        "codex:beta",
    }
    assert {finding["scope_a"], finding["scope_b"]} == {
        "src/billing",
        "src/billing/retries.py",
    }


def test_asking_who_collides_does_not_change_who_collides(project: Path) -> None:
    """``conflicts`` is a read. It must not write an event or a claim."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    out(
        coord(
            project, "claim", "DEMO-CLA-BILLING-A",
            "--write-scope", "src/billing/",
            session="claude:alpha",
        )
    )
    before = counts(project)

    report = out(coord(project, "conflicts"))

    assert report["ok"] is True
    assert counts(project) == before


def test_a_claim_that_declared_nothing_is_reported_not_assumed_clean(
    project: Path,
) -> None:
    """An undeclared claim is unknown. Omitting it would read as safe."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    row(project, "DEMO-CDX-BILLING-B", session="codex:beta")
    declared = out(
        coord(
            project, "claim", "DEMO-CLA-BILLING-A",
            "--write-scope", "src/billing/",
            session="claude:alpha",
        )
    )
    silent = out(coord(project, "claim", "DEMO-CDX-BILLING-B", session="codex:beta"))

    report = out(coord(project, "conflicts"))

    assert report["count"] == 0
    assert report["scanned_claims"] == 2
    assert report["undeclared_claims"] == [silent["claim_id"]]
    assert declared["claim_id"] not in report["undeclared_claims"]


def test_declare_write_set_attaches_scopes_to_an_existing_claim(
    project: Path,
) -> None:
    """The dedicated verb, for a claim that was taken before the risk was known."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    claimed = out(coord(project, "claim", "DEMO-CLA-BILLING-A", session="claude:alpha"))

    declared = out(
        coord(
            project,
            "declare-write-set",
            claimed["claim_id"],
            "--write-scope",
            "src/billing/",
            "--write-scope",
            "table=invoices",
            session="claude:alpha",
        )
    )

    assert declared["work_id"] == "DEMO-CLA-BILLING-A"
    assert declared["write_set"] == [
        {"kind": "path", "value": "src/billing"},
        {"kind": "table", "value": "invoices"},
    ]
    assert out(coord(project, "conflicts"))["undeclared_claims"] == []


def test_two_sessions_on_the_same_table_collide_too(project: Path) -> None:
    """A write set is not only about files; the table scope has to work as well."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    row(project, "DEMO-CDX-BILLING-B", session="codex:beta")
    out(
        coord(
            project, "claim", "DEMO-CLA-BILLING-A",
            "--write-scope", "table=invoices",
            session="claude:alpha",
        )
    )
    out(
        coord(
            project, "claim", "DEMO-CDX-BILLING-B",
            "--write-scope", "table=Invoices",
            session="codex:beta",
        )
    )

    report = out(coord(project, "conflicts"))
    assert report["count"] == 1
    assert report["findings"][0]["kind"] == "table"


def test_one_session_holding_two_claims_does_not_collide_with_itself(
    project: Path,
) -> None:
    """A collision is between two agents. One agent is its own coordination."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    row(project, "DEMO-CLA-BILLING-C", session="claude:alpha")
    out(
        coord(
            project, "claim", "DEMO-CLA-BILLING-A",
            "--write-scope", "src/billing/",
            session="claude:alpha",
        )
    )
    out(
        coord(
            project, "claim", "DEMO-CLA-BILLING-C",
            "--write-scope", "src/billing/retries.py",
            session="claude:alpha",
        )
    )

    assert out(coord(project, "conflicts"))["count"] == 0


# --------------------------------------------------------------------------
# The ungrantable scopes
# --------------------------------------------------------------------------


def test_a_whole_filesystem_scope_is_refused(project: Path) -> None:
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    claimed = out(coord(project, "claim", "DEMO-CLA-BILLING-A", session="claude:alpha"))

    refused = coord(
        project,
        "declare-write-set",
        claimed["claim_id"],
        "--write-scope",
        "/",
        session="claude:alpha",
    )

    assert refused.returncode != 0
    assert "whole-filesystem path scope" in refused.stderr
    # Refused, not partially recorded.
    assert out(coord(project, "conflicts"))["undeclared_claims"] == [
        claimed["claim_id"]
    ]


def test_the_release_pointer_class_is_refused(project: Path) -> None:
    """The deploy pointer is never anybody's write scope to be handed."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")
    claimed = out(coord(project, "claim", "DEMO-CLA-BILLING-A", session="claude:alpha"))

    refused = coord(
        project,
        "declare-write-set",
        claimed["claim_id"],
        "--write-scope",
        ".coordharness/runtime/current",
        session="claude:alpha",
    )

    assert refused.returncode != 0
    assert "ungrantable" in refused.stderr


def test_an_ungrantable_scope_refuses_the_claim_before_it_is_taken(
    project: Path,
) -> None:
    """Order matters: a refusal after the insert leaves an unwanted claim."""
    row(project, "DEMO-CLA-BILLING-A", session="claude:alpha")

    refused = coord(
        project,
        "claim",
        "DEMO-CLA-BILLING-A",
        "--write-scope",
        "/",
        session="claude:alpha",
    )

    assert refused.returncode != 0
    assert "whole-filesystem path scope" in refused.stderr
    conn = sqlite3.connect(db_path(project))
    try:
        held = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE work_id=?"
            " AND status IN ('running','paused','blocked')",
            ("DEMO-CLA-BILLING-A",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert held == 0


# --------------------------------------------------------------------------
# The MCP surface an agent actually calls
# --------------------------------------------------------------------------


@pytest.fixture
def mcp_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bootstrapped board with two work rows, addressed through MCP."""
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    database = tmp_path / "coord.db"
    bootstrap_database(database)
    from coordharness.coord import coord_db

    conn = connect(database)
    try:
        for work_id, lane in (
            ("DEMO-CLA-MCP-A", "claude"),
            ("DEMO-CDX-MCP-B", "codex"),
        ):
            coord_db.upsert_work(
                conn,
                work_id,
                title=f"{lane} work under an MCP claim",
                assignee=lane,
                module="billing",
                surface="job",
                tier="T1",
                done_signal=f"artifacts/{work_id.lower()}.json",
                acceptance_json='["the edit lands where it was declared"]',
                note="exercise the MCP write-set tools",
                intent_state="queued",
            )
    finally:
        conn.close()
    return database


def test_mcp_claim_declares_and_the_conflict_tool_reports(mcp_board: Path) -> None:
    first = mcp_coord_server._tool_claim(
        work_id="DEMO-CLA-MCP-A",
        actor="claude",
        session_id="claude:alpha",
        db_path=str(mcp_board),
        write_scopes=["src/billing/"],
    )
    second = mcp_coord_server._tool_claim(
        work_id="DEMO-CDX-MCP-B",
        actor="codex",
        session_id="codex:beta",
        db_path=str(mcp_board),
        write_scopes=[{"kind": "path", "value": "src/billing/retries.py"}],
    )

    assert first["write_set"] == [{"kind": "path", "value": "src/billing"}]
    assert second["write_set_conflicts"]["count"] == 1

    report = mcp_coord_server._tool_write_set_conflicts(db_path=str(mcp_board))
    assert report["count"] == 1
    assert report["read_only"] is True
    assert report["schema_present"] is True
    assert {report["findings"][0]["session_a"], report["findings"][0]["session_b"]} == {
        "claude:alpha",
        "codex:beta",
    }


def test_mcp_declare_write_set_refuses_a_peer_sessions_claim(mcp_board: Path) -> None:
    """A write set states what THIS session will edit, so it is not declarable
    on someone else's claim."""
    held = mcp_coord_server._tool_claim(
        work_id="DEMO-CLA-MCP-A",
        actor="claude",
        session_id="claude:alpha",
        db_path=str(mcp_board),
    )

    with pytest.raises(ValueError, match="declare a write set on your own claim"):
        mcp_coord_server._tool_declare_write_set(
            claim_id=held["claim_id"],
            scopes=["src/billing/"],
            actor="codex",
            session_id="codex:beta",
            db_path=str(mcp_board),
        )


def test_mcp_declare_write_set_refuses_an_ungrantable_scope(mcp_board: Path) -> None:
    held = mcp_coord_server._tool_claim(
        work_id="DEMO-CLA-MCP-A",
        actor="claude",
        session_id="claude:alpha",
        db_path=str(mcp_board),
    )

    with pytest.raises(work_contracts.UngrantableScopeError):
        mcp_coord_server._tool_declare_write_set(
            claim_id=held["claim_id"],
            scopes=["/"],
            actor="claude",
            session_id="claude:alpha",
            db_path=str(mcp_board),
        )


def test_the_write_set_tools_are_in_the_served_catalog() -> None:
    """A tool absent from the catalog is not registered on the server."""
    catalog = mcp_coord_server._server_tool_catalog(env={})

    assert "declare_write_set" in catalog["visible"]
    assert "write_set_conflicts" in catalog["visible"]
