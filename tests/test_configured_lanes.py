"""Proof that the coordination lanes are configuration, not vocabulary.

The control plane was written around exactly two agents. ``claude`` and
``codex`` appeared as literals in roughly a hundred membership tests, argparse
``choices`` tuples, SQL ``IN`` lists and "the opposite lane is the other one"
expressions -- which meant a third agent could not claim a row, could not be
handed work, and could not clear anyone's T0, no matter how it was configured.
The names were load-bearing.

``COORD_LANES`` makes the set a deployment decision. These tests assert the two
things that must survive that move:

* a lane named only in configuration is a first-class lane -- it claims, it
  receives a typed handoff, and its verdict on another lane's work counts; and
* the invariants that made two lanes worth having are untouched -- a handoff
  cannot name the actor's own lane as owner, and a lane still cannot PASS its
  own row.

The default environment is asserted separately, because the whole change is
worthless if it moved the behaviour every existing deployment already has.
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

THIRD_LANE = "gemini"
THREE_LANES = f"claude,codex,{THIRD_LANE}"


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
    lanes: str | None = THREE_LANES,
) -> subprocess.CompletedProcess:
    """Run the CLI as one named lane session under a chosen ``COORD_LANES``."""
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
    }
    # The identity under test is built explicitly rather than inherited from
    # whichever agent launched pytest.
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
        "COORD_LANES",
    ):
        env.pop(leaked, None)
    if session is not None:
        env["COORD_ACTOR"] = session.split(":", 1)[0]
        env["COORD_SESSION_ID"] = session
    if lanes is not None:
        env["COORD_LANES"] = lanes
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "coordharness.coord.cli",
            "--db",
            str(project / ".coordharness" / "coord.db"),
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


def authored_row(
    project: Path,
    work_id: str,
    *,
    author: str,
    tier: str = "T0",
    lanes: str | None = THREE_LANES,
) -> None:
    """A row created and claimed by one lane, so its author lane is unambiguous."""
    session = f"{author}:author"
    out(
        coord(
            project,
            "create",
            work_id,
            "--title", "a row that needs independent eyes",
            "--module", "runtime",
            "--tier", tier,
            "--done-signal", f"artifacts/{work_id.lower()}.json",
            "--acceptance", "the reviewing lane confirms the derivation",
            "--note", "exercise the cross-lane review loop",
            session=session,
            lanes=lanes,
        )
    )
    out(
        coord(
            project,
            "claim",
            work_id,
            "--step", "deriving",
            session=session,
            lanes=lanes,
        )
    )


# --------------------------------------------------------------------------
# The configuration reader itself
# --------------------------------------------------------------------------


def test_the_default_lane_set_is_the_pair_the_harness_shipped_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``COORD_LANES`` means exactly the two lanes, in their historical order."""
    from coordharness.coord import config

    monkeypatch.delenv("COORD_LANES", raising=False)
    assert config.configured_lanes() == ("claude", "codex")
    assert config.lane_set() == frozenset({"claude", "codex"})
    # Every refusal message that used to say "claude|codex" renders identically.
    assert config.lanes_display() == "claude|codex"
    assert config.counterpart_lane("claude") == "codex"
    assert config.counterpart_lane("codex") == "claude"


def test_coord_lanes_is_parsed_as_trimmed_lowercase_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordharness.coord import config

    monkeypatch.setenv("COORD_LANES", " Claude , codex ,, GEMINI , codex ")
    assert config.configured_lanes() == ("claude", "codex", "gemini")
    assert config.lanes_display() == "claude|codex|gemini"


def test_an_empty_lane_set_is_a_configuration_error_not_a_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty value would refuse every actor; say so instead of guessing."""
    from coordharness.coord import config

    monkeypatch.setenv("COORD_LANES", "  , ,")
    with pytest.raises(ValueError, match="COORD_LANES"):
        config.configured_lanes()


def test_a_lane_token_must_satisfy_the_actor_identifier_grammar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordharness.coord import config

    monkeypatch.setenv("COORD_LANES", "claude,not a lane")
    with pytest.raises(ValueError):
        config.configured_lanes()


def test_the_counterpart_is_never_the_actors_own_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With three lanes the default cross-lane address is still cross-lane."""
    from coordharness.coord import config

    monkeypatch.setenv("COORD_LANES", THREE_LANES)
    for lane in ("claude", "codex", THIRD_LANE):
        counterpart = config.counterpart_lane(lane)
        assert counterpart is not None
        assert counterpart != lane

    monkeypatch.setenv("COORD_LANES", "claude")
    assert config.counterpart_lane("claude") is None


# --------------------------------------------------------------------------
# A configured third lane is a first-class lane
# --------------------------------------------------------------------------


def test_a_configured_third_lane_can_create_and_claim_work(project: Path) -> None:
    work_id = "DEMO-CDX-GEMCLAIMED"
    authored_row(project, work_id, author=THIRD_LANE)

    row = out(coord(project, "work-context", work_id, session=f"{THIRD_LANE}:author"))
    payload = row.get("work", row)
    assert payload["work_id"] == work_id
    assert str(payload.get("assignee") or "").lower() == THIRD_LANE


def test_a_configured_third_lane_receives_a_typed_handoff(project: Path) -> None:
    """The handoff owner lane is validated against configuration, not literals."""
    work_id = "DEMO-CDX-GEMHANDOFF"
    authored_row(project, work_id, author="codex")
    before = out(coord(project, "work-context", work_id, session="codex:author"))
    work = before.get("work", before)

    handed = out(
        coord(
            project,
            "handoff",
            work_id,
            "--owner-lane", THIRD_LANE,
            "--task", "take the derivation to a served number",
            "--why", "the authoring lane is out of headroom",
            "--acceptance", "the receiving lane publishes the artifact",
            "--operation-id", "handoff-gemini-1",
            "--expected-version", str(work["version"]),
            "--expected-assignee", "codex",
            "--ref", "docs/reports/derivation.md",
            "--constraint", "keep the served number bound to served_truth",
            session="codex:author",
        )
    )
    assert handed["ok"] is True

    after = out(coord(project, "work-context", work_id, session="codex:author"))
    assert (after.get("work", after))["assignee"] == THIRD_LANE


def test_a_handoff_to_the_actors_own_lane_is_still_refused(project: Path) -> None:
    """The invariant that survives de-hardcoding: owner_lane must differ from actor."""
    work_id = "DEMO-CDX-GEMSELFHANDOFF"
    authored_row(project, work_id, author=THIRD_LANE)
    work = out(coord(project, "work-context", work_id, session=f"{THIRD_LANE}:author"))
    work = work.get("work", work)

    result = coord(
        project,
        "handoff",
        work_id,
        "--owner-lane", THIRD_LANE,
        "--task", "hand the row to myself",
        "--why", "it should not be possible",
        "--acceptance", "refused",
        "--operation-id", "handoff-gemini-self",
        "--expected-version", str(work["version"]),
        "--expected-assignee", THIRD_LANE,
        "--ref", "docs/reports/derivation.md",
        "--constraint", "this must not land either",
        session=f"{THIRD_LANE}:author",
    )
    assert result.returncode != 0
    assert "owner_lane must differ from actor" in result.stderr


def test_a_configured_third_lane_posts_a_cross_lane_verdict(project: Path) -> None:
    """A gemini PASS on codex-authored work lands as an audit_verdict event."""
    work_id = "DEMO-CDX-GEMREVIEWED"
    authored_row(project, work_id, author="codex")

    recorded = out(
        coord(
            project,
            "verdict",
            work_id,
            "--verdict", "PASS",
            "--ref", "docs/reports/derivation.md",
            session=f"{THIRD_LANE}:reviewer",
        )
    )
    assert recorded["ok"] is True
    assert recorded["verdict"] == "PASS"
    assert recorded["reviewer"] == THIRD_LANE
    # Addressed back to the authoring lane, which coord_db derives from the
    # claim history rather than trusting the caller.
    assert recorded["to_selector"] == "actor:codex"

    import sqlite3

    conn = sqlite3.connect(project / ".coordharness" / "coord.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT kind, actor, verdict, to_selector FROM events WHERE event_id=?",
            (recorded["event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["kind"] == "audit_verdict"
    assert row["actor"] == THIRD_LANE
    assert row["verdict"] == "PASS"
    assert row["to_selector"] == "actor:codex"


def test_a_same_lane_pass_is_still_forbidden_for_a_configured_lane(
    project: Path,
) -> None:
    """The lane that authored the work still cannot be the lane that clears it."""
    work_id = "DEMO-CDX-GEMSELFPASS"
    authored_row(project, work_id, author=THIRD_LANE)

    result = coord(
        project,
        "verdict",
        work_id,
        "--verdict", "PASS",
        "--ref", "docs/reports/derivation.md",
        "--to-lane", "codex",
        session=f"{THIRD_LANE}:author",
    )
    assert result.returncode != 0
    assert "same-lane PASS is forbidden" in result.stderr

    import sqlite3

    conn = sqlite3.connect(project / ".coordharness" / "coord.db")
    try:
        posted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='audit_verdict' AND work_id=?",
            (work_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert posted == 0


def test_an_unconfigured_lane_is_refused_and_the_message_names_the_config(
    project: Path,
) -> None:
    """Under the default pair, a third actor is not a lane at all."""
    result = coord(
        project,
        "verdict",
        "DEMO-CDX-GEMUNKNOWN",
        "--verdict", "PASS",
        "--ref", "docs/reports/derivation.md",
        session=f"{THIRD_LANE}:reviewer",
        lanes=None,
    )
    assert result.returncode != 0
    assert "exact coordination lane" in result.stderr
    assert "claude|codex" in result.stderr


def test_argparse_choices_follow_the_configured_lanes(project: Path) -> None:
    """The parser's ``choices`` are computed at build time, not frozen literals."""
    three = coord(project, "handoff", "--help")
    assert three.returncode == 0, three.stderr
    assert THIRD_LANE in three.stdout

    two = coord(project, "handoff", "--help", lanes=None)
    assert two.returncode == 0, two.stderr
    assert THIRD_LANE not in two.stdout
    assert "claude" in two.stdout and "codex" in two.stdout


# --------------------------------------------------------------------------
# The default deployment is unmoved
# --------------------------------------------------------------------------


def test_the_default_two_lane_review_loop_is_unchanged(project: Path) -> None:
    """With no ``COORD_LANES`` set, the shipped cross-lane loop behaves as before."""
    work_id = "DEMO-CDX-DEFAULT"
    authored_row(project, work_id, author="codex", lanes=None)

    recorded = out(
        coord(
            project,
            "verdict",
            work_id,
            "--verdict", "PASS",
            "--ref", "docs/reports/derivation.md",
            session="claude:reviewer",
            lanes=None,
        )
    )
    assert recorded["reviewer"] == "claude"
    assert recorded["to_selector"] == "actor:codex"

    refused = coord(
        project,
        "verdict",
        work_id,
        "--verdict", "PASS",
        "--ref", "docs/reports/derivation.md",
        session="codex:author",
        lanes=None,
    )
    assert refused.returncode != 0
    assert "same-lane PASS is forbidden" in refused.stderr


# --------------------------------------------------------------------------
# A configured lane's verdict must COUNT, and its own must not
# --------------------------------------------------------------------------


def _classify(project: Path, work_id: str, lanes: str = THREE_LANES) -> dict:
    """Ask review_integrity whether the row is reviewed, under a lane config."""
    import importlib

    saved = os.environ.get("COORD_LANES")
    os.environ["COORD_LANES"] = lanes
    try:
        config = importlib.import_module("coordharness.coord.config")
        review_integrity = importlib.import_module(
            "coordharness.coord.review_integrity"
        )
        conn = config.connect_ro(project / ".coordharness" / "coord.db")
        try:
            return review_integrity.classify_verdict_status(conn, work_id)
        finally:
            conn.close()
    finally:
        if saved is None:
            os.environ.pop("COORD_LANES", None)
        else:
            os.environ["COORD_LANES"] = saved


def test_a_third_lane_verdict_counts_as_independent_review(project: Path) -> None:
    """A gemini PASS on a claude-authored row clears it, exactly as codex would."""
    work_id = "DEMO-CDX-GEMINDEPENDENT"
    authored_row(project, work_id, author="claude")
    out(
        coord(
            project,
            "verdict",
            work_id,
            "--verdict", "PASS",
            "--ref", "docs/reports/derivation.md",
            session=f"{THIRD_LANE}:reviewer",
        )
    )

    status = _classify(project, work_id)
    assert status["reviewed"] is True, status
    assert status["reason"] == "independent_verdict", status
    assert status["verdict_actor"] == THIRD_LANE


def test_a_third_lane_verdict_on_its_own_row_never_counts(project: Path) -> None:
    """The self-verdict guard is lane-inequality, so it binds new lanes too.

    A FLAG is used rather than a PASS because the PASS writer refuses outright;
    this asserts the *counting* rule underneath it -- even a verdict that lands
    in the event log leaves its own author's row unreviewed.
    """
    work_id = "DEMO-CDX-GEMSELFREVIEW"
    authored_row(project, work_id, author=THIRD_LANE)
    out(
        coord(
            project,
            "verdict",
            work_id,
            "--verdict", "FLAG",
            "--ref", "docs/reports/derivation.md",
            "--to-lane", "codex",
            session=f"{THIRD_LANE}:author",
        )
    )

    status = _classify(project, work_id)
    assert status["reviewed"] is False, status
    assert status["reason"] == "self_verdict", status
    assert status["verdict_actor"] == THIRD_LANE
    assert status["author_lane"] == THIRD_LANE


def test_the_claim_kind_list_is_derived_the_same_way_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sites query ``<lane>_claim``; they must agree on the lane list.

    ``create_schema``'s request-consumption backfill and ``review_integrity``'s
    never-claimed check both build an ``IN`` list of per-lane claim kinds. If
    they drift, a row is claimed for one and unclaimed for the other -- so the
    agreement is asserted rather than assumed.
    """
    import importlib

    monkeypatch.setenv("COORD_LANES", THREE_LANES)
    config = importlib.import_module("coordharness.coord.config")
    expected = tuple(f"{lane}_claim" for lane in config.configured_lanes())
    assert expected == ("claude_claim", "codex_claim", f"{THIRD_LANE}_claim")

    for module_name in (
        "coordharness.coord.create_schema",
        "coordharness.coord.review_integrity",
        "coordharness.coord.coord_db",
    ):
        module = importlib.import_module(module_name)
        # Each site reaches the same reader, so the derived list is one list.
        assert module._configured_lanes() == config.configured_lanes()


def test_a_third_lane_claim_is_the_author_lane_coord_db_derives(
    project: Path,
) -> None:
    """The claim-kind query finds a gemini claim, so authorship resolves."""
    import importlib

    work_id = "DEMO-CDX-GEMAUTHORLANE"
    authored_row(project, work_id, author=THIRD_LANE)

    saved = os.environ.get("COORD_LANES")
    os.environ["COORD_LANES"] = THREE_LANES
    try:
        config = importlib.import_module("coordharness.coord.config")
        coord_db = importlib.import_module("coordharness.coord.coord_db")
        conn = config.connect_ro(project / ".coordharness" / "coord.db")
        try:
            assert (
                coord_db._latest_claim_author_lane_unlocked(conn, work_id)
                == THIRD_LANE
            )
        finally:
            conn.close()
    finally:
        if saved is None:
            os.environ.pop("COORD_LANES", None)
        else:
            os.environ["COORD_LANES"] = saved
