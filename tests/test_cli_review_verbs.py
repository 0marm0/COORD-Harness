"""Proof that dual-lane review is reachable from the CLI, not only from MCP.

The review contract says a T0 row is cleared by the lane that did not author it.
Every gate in this repository reported green while that contract was
*unreachable* for anyone driving coord through its command line: the verdict and
audit-request logic existed in ``coord_db``, the MCP server exposed it, and the
CLI -- which is how an agent working in a shell actually coordinates -- had no
verb for either. A review step nobody can invoke is indistinguishable from a
review step nobody skipped.

These tests therefore assert on the whole loop through the shipped console
entry point, and on the four refusals that make a recorded verdict worth
anything: a verdict must name evidence, must be one of the three defined
values, must come from an exact lane identity, and must not clear its own
author's work.
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
    """A throwaway project with a git repository and an empty coord database."""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def coord(
    project: Path,
    *args: str,
    session: str | None = "claude:reviewer",
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the CLI as one named lane session, or with no lane identity at all."""
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
    }
    # Tests run from inside Claude, Codex, ordinary shells, and CI. Build the
    # identity under test explicitly instead of inheriting whichever agent
    # launched pytest. STARSHIP_SESSION_KEY is included because it is a weak
    # Codex fallback when no stronger identity is present.
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
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "coordharness.coord.cli",
         "--db", str(project / ".coordharness" / "coord.db"), *args],
        cwd=project, capture_output=True, text=True, env=env,
    )


def out(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def authored_row(project: Path, work_id: str, *, tier: str = "T0") -> None:
    """A row created and claimed by codex, so its author lane is unambiguous."""
    out(coord(
        project, "create", work_id,
        "--title", "a row that needs independent eyes",
        "--module", "runtime",
        "--tier", tier,
        "--done-signal", f"artifacts/{work_id.lower()}.json",
        "--acceptance", "the reviewing lane confirms the derivation",
        "--note", "exercise the cross-lane review loop",
        session="codex:author",
    ))
    out(coord(project, "claim", work_id, "--step", "deriving", session="codex:author"))


def test_review_verbs_are_registered(project: Path) -> None:
    """Both verbs exist on the parser a shell user actually reaches."""
    result = coord(project, "--help")
    assert result.returncode == 0, result.stderr
    assert "verdict" in result.stdout
    assert "request-audit" in result.stdout

    verdict_help = coord(project, "verdict", "--help")
    assert verdict_help.returncode == 0, verdict_help.stderr
    for flag in ("--verdict", "--severity", "--ref", "--to-lane", "--session"):
        assert flag in verdict_help.stdout, flag


def test_cross_lane_verdict_round_trips_into_the_database(project: Path) -> None:
    """A claude PASS on codex-authored work lands as an audit_verdict event."""
    work_id = "DEMO-CDX-REVIEWED"
    authored_row(project, work_id)

    recorded = out(coord(
        project, "verdict", work_id,
        "--verdict", "PASS",
        "--severity", "low",
        "--ref", "docs/reports/derivation.md",
        session="claude:reviewer",
    ))
    assert recorded["ok"] is True
    assert recorded["verdict"] == "PASS"
    assert recorded["reviewer"] == "claude"
    # Addressed back to the authoring lane, which coord_db derives from the
    # claim history rather than trusting the caller.
    assert recorded["to_selector"] == "actor:codex"
    assert recorded["replayed"] is False
    assert recorded["work"]["rubric_verdict"] == "pass"

    import sqlite3

    conn = sqlite3.connect(project / ".coordharness" / "coord.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT kind, actor, verdict, to_selector, refs_json, payload_json"
            " FROM events WHERE event_id=?",
            (recorded["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["kind"] == "audit_verdict"
    assert row["actor"] == "claude"
    assert row["verdict"] == "PASS"
    assert row["to_selector"] == "actor:codex"
    # Pointer-based: the refs are the review, not a prose body.
    assert json.loads(row["refs_json"]) == ["docs/reports/derivation.md"]
    payload = json.loads(row["payload_json"])
    assert payload["receiver_lane"] == "codex"
    assert payload["operation_request_sha256"]


def test_cli_verdict_payload_uses_the_mcp_size_bound(project: Path) -> None:
    """Large evidence lists keep their digest without creating huge event payloads."""
    work_id = "DEMO-CDX-BOUNDED-PAYLOAD"
    authored_row(project, work_id)
    refs = [f"docs/evidence-{index}-" + ("x" * 1_900) for index in range(32)]
    args = ["verdict", work_id, "--verdict", "PASS"]
    for ref in refs:
        args.extend(("--ref", ref))

    posted = out(coord(project, *args, session="claude:reviewer"))

    import sqlite3

    conn = sqlite3.connect(project / ".coordharness" / "coord.db")
    try:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_id=?",
            (posted["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    payload_text = str(row[0])
    assert len(payload_text.encode("utf-8")) <= 12_000
    payload = json.loads(payload_text)
    assert payload["_truncated"] is True
    assert payload["_original_bytes"] > 12_000
    assert len(payload["_sha256"]) == 64


def test_repeating_the_identical_verdict_replays_instead_of_double_posting(project: Path) -> None:
    """The derived operation id makes a re-run idempotent, not a second verdict."""
    work_id = "DEMO-CDX-REPLAY"
    authored_row(project, work_id)

    args = ("verdict", work_id, "--verdict", "PASS", "--ref", "docs/reports/derivation.md")
    first = out(coord(project, *args, session="claude:reviewer"))
    second = out(coord(project, *args, session="claude:reviewer"))
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["event_id"] == first["event_id"]


def test_verdict_rejects_an_undefined_verdict_value(project: Path) -> None:
    work_id = "DEMO-CDX-BADVALUE"
    authored_row(project, work_id)

    result = coord(
        project, "verdict", work_id, "--verdict", "MAYBE",
        "--ref", "docs/reports/derivation.md", session="claude:reviewer",
    )
    assert result.returncode != 0
    assert "invalid choice: 'MAYBE'" in result.stderr
    assert "PASS" in result.stderr and "FLAG" in result.stderr and "BLOCKED" in result.stderr


def test_verdict_requires_evidence(project: Path) -> None:
    """A verdict with nothing to point at is an opinion, and is refused."""
    work_id = "DEMO-CDX-NOREFS"
    authored_row(project, work_id)

    result = coord(
        project, "verdict", work_id, "--verdict", "PASS", session="claude:reviewer",
    )
    assert result.returncode != 0
    assert "--ref" in result.stderr


def test_same_lane_pass_is_refused(project: Path) -> None:
    """The lane that authored the work cannot be the lane that clears it.

    This is the guard the whole verb exists to make reachable, so it is asserted
    against the real author lane -- codex claimed this row -- rather than
    against a caller-supplied label that a reviewer could simply set correctly.
    """
    work_id = "DEMO-CDX-SELFPASS"
    authored_row(project, work_id)

    result = coord(
        project, "verdict", work_id, "--verdict", "PASS",
        "--ref", "docs/reports/derivation.md",
        "--to-lane", "claude",
        session="codex:author",
    )
    assert result.returncode != 0
    assert "same-lane PASS is forbidden" in result.stderr

    import sqlite3

    conn = sqlite3.connect(project / ".coordharness" / "coord.db")
    try:
        posted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE work_id=? AND kind='audit_verdict'",
            (work_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert posted == 0, "a refused verdict must leave no verdict event behind"


def test_verdict_refuses_naming_your_own_lane_as_the_author(project: Path) -> None:
    """--to-lane cannot be used to address a verdict to yourself."""
    work_id = "DEMO-CDX-SELFADDRESS"
    authored_row(project, work_id)

    result = coord(
        project, "verdict", work_id, "--verdict", "FLAG",
        "--ref", "docs/reports/derivation.md",
        "--to-lane", "claude",
        session="claude:reviewer",
    )
    assert result.returncode != 0
    assert "independent cross-lane review" in result.stderr


@pytest.mark.parametrize("verb_args", [
    ("verdict", "DEMO-CDX-IDENTITY", "--verdict", "PASS", "--ref", "docs/proof.md"),
    ("request-audit", "DEMO-CDX-IDENTITY", "--task", "check it", "--why", "T0",
     "--ref", "docs/proof.md"),
])
def test_review_verbs_refuse_an_inexact_lane_identity(project: Path, verb_args) -> None:
    """Without a lane, review attribution would be a guess that can self-approve.

    ``resolve_identity`` falls back to actor ``local`` with a pid-derived session
    when no session variable is set. Both review verbs derive the counterpart
    lane by inverting the actor, so that fallback must be refused rather than
    resolved to whichever lane the inversion happens to produce.
    """
    authored_row(project, "DEMO-CDX-IDENTITY")

    result = coord(project, *verb_args, session=None)
    assert result.returncode != 0
    assert "exact coordination lane" in result.stderr
    assert "'local'" in result.stderr


def test_asserted_session_must_match_the_running_process(project: Path) -> None:
    """--session is an assertion about this process, never an override of it."""
    work_id = "DEMO-CDX-SESSION"
    authored_row(project, work_id)

    mismatched = coord(
        project, "verdict", work_id, "--verdict", "PASS",
        "--ref", "docs/proof.md", "--session", "claude:someone-else",
        session="claude:reviewer",
    )
    assert mismatched.returncode != 0
    assert "must match this process" in mismatched.stderr

    matched = out(coord(
        project, "verdict", work_id, "--verdict", "PASS",
        "--ref", "docs/proof.md", "--session", "claude:reviewer",
        session="claude:reviewer",
    ))
    assert matched["verdict"] == "PASS"


@pytest.mark.parametrize("verb_args", [
    ("verdict", "DEMO-CDX-STARSHIP", "--verdict", "PASS", "--ref", "docs/proof.md"),
    ("request-audit", "DEMO-CDX-STARSHIP", "--task", "check it", "--why", "T0",
     "--ref", "docs/proof.md"),
])
def test_review_verbs_refuse_starship_only_identity(project: Path, verb_args) -> None:
    """A shell-prompt token is not proof that this process is Codex."""
    authored_row(project, "DEMO-CDX-STARSHIP")

    result = coord(
        project,
        *verb_args,
        session=None,
        env_overrides={"STARSHIP_SESSION_KEY": "ordinary-shell"},
    )
    assert result.returncode != 0
    assert "exact client session identity" in result.stderr


def test_mcp_review_identity_refuses_starship_only_session() -> None:
    """The stdio surface must apply the same strong-session requirement."""
    from coordharness.coord.mcp_coord_server import _resolve_process_bound_identity

    with pytest.raises(ValueError, match="exact client process identity"):
        _resolve_process_bound_identity(
            actor=None,
            session_id=None,
            env={"STARSHIP_SESSION_KEY": "ordinary-shell"},
            action="verdict",
        )


@pytest.mark.parametrize(
    ("actor", "session_id"),
    [("local", "local:manual"), ("reviewer", "reviewer:manual")],
)
def test_mcp_review_identity_refuses_non_lane_actor(actor, session_id) -> None:
    from coordharness.coord.mcp_coord_server import _resolve_process_bound_identity

    with pytest.raises(ValueError, match="exact client process identity"):
        _resolve_process_bound_identity(
            actor=actor,
            session_id=session_id,
            env={"COORD_ACTOR": actor, "COORD_SESSION_ID": session_id},
            action="verdict",
        )


def test_request_audit_posts_a_cross_lane_request(project: Path) -> None:
    """The author asks the other lane for eyes; the row's owner does not change."""
    work_id = "DEMO-CDX-ASKED"
    authored_row(project, work_id)

    asked = out(coord(
        project, "request-audit", work_id,
        "--task", "check the derivation against the source",
        "--why", "T0 served number",
        "--ref", "docs/reports/derivation.md",
        "--acceptance", "reviewer confirms the served binding",
        session="codex:author",
    ))
    assert asked["ok"] is True
    assert asked["target_lane"] == "claude"
    assert asked["to_selector"] == "actor:claude"
    assert asked["assignee_unchanged"] == "codex"

    import sqlite3

    conn = sqlite3.connect(project / ".coordharness" / "coord.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT kind, actor, to_selector, payload_json FROM events WHERE event_id=?",
            (asked["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["kind"] == "audit_request"
    assert row["actor"] == "codex"
    assert row["to_selector"] == "actor:claude"
    payload = json.loads(row["payload_json"])
    assert payload["task"] == "check the derivation against the source"
    assert payload["schema_version"] == 1


def test_request_audit_refuses_to_ask_a_lane_to_audit_its_own_work(project: Path) -> None:
    """A claude request on a codex row would target codex: its own author."""
    work_id = "DEMO-CDX-SELFAUDIT"
    authored_row(project, work_id)

    result = coord(
        project, "request-audit", work_id,
        "--task", "check it", "--why", "because", "--ref", "docs/proof.md",
        session="claude:reviewer",
    )
    assert result.returncode != 0
    assert "self-target refused" in result.stderr


def test_the_whole_t0_loop_runs_through_the_cli(project: Path) -> None:
    """Ask for review, get a FLAG, then get a PASS -- all from the command line.

    A FLAG must not read as a clearance: it drives the row back to queued and
    records the reviewer's evidence, and only the later PASS sets the rubric.
    """
    work_id = "DEMO-CDX-T0LOOP"
    authored_row(project, work_id)

    out(coord(
        project, "request-audit", work_id,
        "--task", "verify the served binding", "--why", "T0 external number",
        "--ref", "docs/reports/derivation.md", session="codex:author",
    ))
    flagged = out(coord(
        project, "verdict", work_id, "--verdict", "FLAG", "--severity", "high",
        "--ref", "evidence/gap.md", session="claude:reviewer",
    ))
    assert flagged["work"]["rubric_verdict"] == "flag"
    assert flagged["work"]["intent_state"] == "queued"

    passed = out(coord(
        project, "verdict", work_id, "--verdict", "PASS",
        "--ref", "evidence/gap-closed.md", session="claude:reviewer",
    ))
    assert passed["work"]["rubric_verdict"] == "pass"
    assert passed["event_id"] != flagged["event_id"]
