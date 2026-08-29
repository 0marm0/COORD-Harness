"""A claim id was authority to mutate someone else's claim.

`_resolve_lifecycle_claim_row` checked the caller against the claim's owner only
`if explicit_actor or explicit_sid` -- so a call carrying nothing but a
`claim_id` was never checked at all. A claim id is not a secret: `claim` returns
it, the board prints it, and handoff payloads carry it. Any peer holding one
could block, park, release or *complete* another session's work.

What makes it worse than reachability alone is that it left no trace. The claim
row keeps the holder's `session_id`, so the persisted result of a peer's block
is byte-identical to the holder blocking its own work. There was nothing to
distinguish "the owner did this" from "somebody else did it for them".

These tests pin the fix at both layers:

  * the database refuses, because the CLI reaches the same three mutators and so
    would any future surface -- `coord_db` is the one place every path goes
    through; and
  * the MCP surface refuses, naming the holder and the asker, before it gets
    that far.

They also pin what must keep working, because a fix that breaks handoff is worse
than the defect: the lease reaper, the zombie-session reaper, typed handoff, and
a sibling session of the same orchestrator all still act on claims they do not
themselves hold.
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
from coordharness.coord import coord_db, mcp_coord_server  # noqa: E402
from coordharness.coord.config import connect  # noqa: E402

WORK_ID = "W-HIJACK-1"
HANDOFF_WORK_ID = "W-HANDOFF-1"
OWNER = "claude:owner-session"
ATTACKER = "codex:attacker-session"


# --------------------------------------------------------------------------
# One row, one holder, one unrelated peer
# --------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    # complete_claim resolves declared proof against HARNESS_ROOT, which is
    # frozen at import. Point it at the throwaway tree so a real completion is
    # reachable without a git repository.
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", tmp_path)
    proof = tmp_path / "artifacts" / "hijack.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text('{"done": true}\n', encoding="utf-8")

    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="a row one session holds",
            assignee="claude",
            module="runtime",
            surface="job",
            tier="T2",
            done_signal="artifacts/hijack.json",
            acceptance_json='["the holder closes it"]',
            note="claim-ownership fixture",
            intent_state="queued",
        )
        # Typed handoff refuses work whose completion proof already resolves,
        # so the handoff cases need a row whose declared proof is still absent.
        coord_db.upsert_work(
            conn,
            HANDOFF_WORK_ID,
            title="a row that is about to change hands",
            assignee="claude",
            module="runtime",
            surface="job",
            tier="T2",
            done_signal="artifacts/handoff-not-written-yet.json",
            acceptance_json='["the new owner closes it"]',
            note="claim-ownership fixture",
            intent_state="queued",
        )
        for session in (OWNER, ATTACKER):
            coord_db.register_session(conn, session, session.split(":", 1)[0])
    finally:
        conn.close()
    return database


@pytest.fixture
def held(board: Path) -> tuple[Path, str]:
    """The owner holds a running claim on WORK_ID."""
    conn = connect(board)
    try:
        claim_id = coord_db.claim_work(conn, OWNER, WORK_ID, step="owner working")
    finally:
        conn.close()
    return board, claim_id


@pytest.fixture
def handoff_held(board: Path) -> tuple[Path, str]:
    """The owner holds a running claim on a row whose proof is still absent."""
    conn = connect(board)
    try:
        claim_id = coord_db.claim_work(
            conn, OWNER, HANDOFF_WORK_ID, step="owner working"
        )
    finally:
        conn.close()
    return board, claim_id


def claim_row(database: Path, claim_id: str) -> dict[str, object]:
    raw = sqlite3.connect(database)
    raw.row_factory = sqlite3.Row
    try:
        row = raw.execute(
            "SELECT session_id, status, release_reason FROM claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
    finally:
        raw.close()
    return dict(row)


# --------------------------------------------------------------------------
# The mutations, as the database sees them
# --------------------------------------------------------------------------
#
# Each entry is (name, callable(conn, claim_id, **identity)). The identity
# kwargs are what the guard reads; everything else is the verb's own contract.

def _db_heartbeat(conn, claim_id, **identity):
    coord_db.heartbeat_claim(conn, claim_id, step="stolen step", **identity)


def _db_release(conn, claim_id, **identity):
    coord_db.release_claim(
        conn, claim_id, status="released", reason="attacker-injected release", **identity
    )


def _db_block(conn, claim_id, **identity):
    coord_db.release_claim(
        conn,
        claim_id,
        status="blocked",
        reason="attacker-injected block",
        resume_when="attacker says so",
        resume_manual=True,
        **identity,
    )


def _db_park(conn, claim_id, **identity):
    coord_db.release_claim(
        conn,
        claim_id,
        status="paused",
        reason="attacker-injected park",
        next_step="whatever the attacker wants next",
        resume_when="attacker says so",
        resume_manual=True,
        **identity,
    )


def _db_complete(conn, claim_id, **identity):
    coord_db.complete_claim(conn, claim_id, **identity)


DB_MUTATIONS = {
    "heartbeat": _db_heartbeat,
    "release": _db_release,
    "block": _db_block,
    "park": _db_park,
    "complete": _db_complete,
}


@pytest.mark.parametrize("verb", sorted(DB_MUTATIONS))
def test_the_database_refuses_a_session_that_does_not_hold_the_claim(
    held: tuple[Path, str], verb: str
) -> None:
    database, claim_id = held
    before = claim_row(database, claim_id)

    conn = connect(database)
    try:
        with pytest.raises(ValueError) as excinfo:
            DB_MUTATIONS[verb](
                conn, claim_id, session_id=ATTACKER, actor="codex"
            )
    finally:
        conn.close()

    message = str(excinfo.value)
    assert "cannot touch a claim this session does not hold" in message
    # The refusal has to name both sides, or the operator reading it cannot tell
    # whose row was reached for and by whom.
    assert OWNER in message
    assert ATTACKER in message
    assert claim_id in message
    assert WORK_ID in message
    # Nothing moved.
    assert claim_row(database, claim_id) == before


@pytest.mark.parametrize("verb", sorted(DB_MUTATIONS))
def test_the_database_refuses_a_call_that_names_no_session_at_all(
    held: tuple[Path, str], verb: str
) -> None:
    database, claim_id = held
    before = claim_row(database, claim_id)

    conn = connect(database)
    try:
        with pytest.raises(ValueError) as excinfo:
            DB_MUTATIONS[verb](conn, claim_id, session_id=None)
    finally:
        conn.close()

    message = str(excinfo.value)
    assert "requires the calling session's identity" in message
    assert OWNER in message
    assert claim_row(database, claim_id) == before


def test_the_holder_still_heartbeats_parks_and_blocks_its_own_claim(
    held: tuple[Path, str],
) -> None:
    database, claim_id = held
    identity = {"session_id": OWNER, "actor": "claude"}

    conn = connect(database)
    try:
        _db_heartbeat(conn, claim_id, **identity)
        assert claim_row(database, claim_id)["status"] == "running"

        _db_block(conn, claim_id, **identity)
        blocked = claim_row(database, claim_id)
        assert blocked["status"] == "blocked"
        assert blocked["release_reason"] == "attacker-injected block"

        _db_park(conn, claim_id, **identity)
        assert claim_row(database, claim_id)["status"] == "paused"

        _db_release(conn, claim_id, **identity)
        assert claim_row(database, claim_id)["status"] == "released"
    finally:
        conn.close()


def test_the_holder_still_completes_its_own_claim(held: tuple[Path, str]) -> None:
    database, claim_id = held
    conn = connect(database)
    try:
        proof = coord_db.complete_claim(
            conn, claim_id, session_id=OWNER, actor="claude"
        )
    finally:
        conn.close()
    assert proof == "artifacts/hijack.json"
    assert claim_row(database, claim_id)["status"] == "completed"


def test_an_actor_that_contradicts_its_own_session_id_is_refused(
    held: tuple[Path, str],
) -> None:
    """Naming the holder's session under the wrong lane is not a way in."""
    database, claim_id = held
    conn = connect(database)
    try:
        with pytest.raises(ValueError, match="requires actor='claude'"):
            _db_release(conn, claim_id, session_id=OWNER, actor="codex")
    finally:
        conn.close()
    assert claim_row(database, claim_id)["status"] == "running"


# --------------------------------------------------------------------------
# The same five verbs, through the MCP tools
# --------------------------------------------------------------------------


def _mcp_heartbeat(database, claim_id, **identity):
    return mcp_coord_server._tool_heartbeat(
        claim_id=claim_id, step="stolen step", db_path=str(database), **identity
    )


def _mcp_release(database, claim_id, **identity):
    return mcp_coord_server._tool_release(
        claim_id=claim_id,
        status="released",
        reason="attacker-injected release",
        db_path=str(database),
        **identity,
    )


def _mcp_block(database, claim_id, **identity):
    return mcp_coord_server._tool_block(
        claim_id=claim_id,
        step="attacker-injected block",
        resume_when="attacker says so",
        resume_manual=True,
        db_path=str(database),
        **identity,
    )


def _mcp_park(database, claim_id, **identity):
    return mcp_coord_server._tool_park(
        claim_id=claim_id,
        step="attacker-injected park",
        next_step="whatever the attacker wants next",
        resume_when="attacker says so",
        resume_manual=True,
        db_path=str(database),
        **identity,
    )


def _mcp_complete(database, claim_id, **identity):
    return mcp_coord_server._tool_complete(
        claim_id=claim_id, db_path=str(database), **identity
    )


MCP_MUTATIONS = {
    "heartbeat": _mcp_heartbeat,
    "release": _mcp_release,
    "block": _mcp_block,
    "park": _mcp_park,
    "complete": _mcp_complete,
}


@pytest.mark.parametrize("verb", sorted(MCP_MUTATIONS))
def test_the_mcp_tool_refuses_an_unrelated_session(
    held: tuple[Path, str], verb: str
) -> None:
    database, claim_id = held
    before = claim_row(database, claim_id)

    with pytest.raises(ValueError) as excinfo:
        MCP_MUTATIONS[verb](
            database, claim_id, actor="codex", session_id=ATTACKER
        )

    message = str(excinfo.value)
    assert "cannot touch a claim this session does not hold" in message
    assert OWNER in message
    assert ATTACKER in message
    assert claim_row(database, claim_id) == before


@pytest.mark.parametrize("verb", sorted(MCP_MUTATIONS))
def test_the_mcp_tool_refuses_a_bare_claim_id(
    held: tuple[Path, str], verb: str
) -> None:
    """The exact reported hijack: a claim id and nothing else."""
    database, claim_id = held
    before = claim_row(database, claim_id)

    with pytest.raises(ValueError) as excinfo:
        MCP_MUTATIONS[verb](database, claim_id)

    message = str(excinfo.value)
    assert "named no session of its own" in message
    assert OWNER in message
    assert claim_id in message
    assert claim_row(database, claim_id) == before


def test_the_holder_still_drives_its_own_row_through_the_mcp_tools(
    held: tuple[Path, str],
) -> None:
    database, claim_id = held
    identity = {"actor": "claude", "session_id": OWNER}

    renewed = _mcp_heartbeat(database, claim_id, **identity)
    assert renewed["renewed"] is True
    assert renewed["session_id"] == OWNER

    blocked = _mcp_block(database, claim_id, **identity)
    assert blocked["status"] == "blocked"
    assert claim_row(database, claim_id)["status"] == "blocked"

    parked = _mcp_park(database, claim_id, **identity)
    assert parked["status"] == "paused"

    released = _mcp_release(database, claim_id, **identity)
    assert released["status"] == "released"
    assert claim_row(database, claim_id)["status"] == "released"


def test_the_holder_still_completes_through_the_mcp_tool(
    held: tuple[Path, str],
) -> None:
    database, claim_id = held
    completed = _mcp_complete(
        database, claim_id, actor="claude", session_id=OWNER
    )
    assert completed["status"] == "completed"
    assert completed["artifact_path"] == "artifacts/hijack.json"
    assert claim_row(database, claim_id)["status"] == "completed"


def test_the_database_guard_is_not_satisfied_by_the_row_it_just_read(
    held: tuple[Path, str],
) -> None:
    """The surface must forward the *asker*, not the stored holder.

    Passing `row["session_id"]` down would make the database check compare the
    claim against itself and always agree -- a guard that cannot fail is not a
    guard. This asserts the resolution helper hands back the caller identity,
    which is what the mutators are given.
    """
    database, claim_id = held
    conn = connect(database)
    try:
        _row, caller_actor, caller_sid = mcp_coord_server._resolve_lifecycle_claim_row(
            conn,
            verb="heartbeat",
            claim_id=claim_id,
            actor="claude",
            session_id=OWNER,
        )
    finally:
        conn.close()
    assert (caller_actor, caller_sid) == ("claude", OWNER)


# --------------------------------------------------------------------------
# The legitimate not-the-holder callers
# --------------------------------------------------------------------------


def test_the_lease_reaper_still_releases_a_claim_it_does_not_hold(
    held: tuple[Path, str],
) -> None:
    """Expiry is a first-class path with its own SQL, and must stay one."""
    database, claim_id = held
    conn = connect(database)
    try:
        conn.execute(
            "UPDATE claims SET expires_at=1 WHERE claim_id=?", (claim_id,)
        )
        conn.commit()
        report = coord_db.release_expired_claims_batch(conn)
    finally:
        conn.close()
    assert report["released_count"] == 1
    released = claim_row(database, claim_id)
    assert released["status"] == "unclaimed"
    assert released["release_reason"] == "expired"


def test_the_zombie_session_reaper_still_releases_a_claim_it_does_not_hold(
    held: tuple[Path, str],
) -> None:
    database, claim_id = held
    conn = connect(database)
    try:
        conn.execute(
            "UPDATE agent_sessions SET lease_until=1 WHERE session_id=?", (OWNER,)
        )
        conn.commit()
        report = coord_db.reap_zombie_sessions(
            conn, grace_s=0.0, dead_sessions=[OWNER]
        )
    finally:
        conn.close()
    assert OWNER in report["reaped"]
    assert report["claims_released"] == 1
    assert claim_row(database, claim_id)["release_reason"] == "reaped"


def test_typed_handoff_still_closes_the_callers_own_ownership_epoch(
    handoff_held: tuple[Path, str],
) -> None:
    """Ownership transfer is meant to touch a claim; it must keep working."""
    database, claim_id = handoff_held
    conn = connect(database)
    try:
        version = conn.execute(
            "SELECT version FROM work_items WHERE work_id=?", (HANDOFF_WORK_ID,)
        ).fetchone()["version"]
        receipt = coord_db.post_existing_work_handoff(
            conn,
            work_id=HANDOFF_WORK_ID,
            actor="claude",
            session_id=OWNER,
            owner_lane="codex",
            target_intent="queued",
            task="take this row over",
            why="the holder is handing it on",
            acceptance="codex closes it with the declared proof",
            refs=["artifacts/handoff-not-written-yet.json"],
            constraints=["do not change the done_signal"],
            operation_id="handoff-hijack-0001",
            expected_version=int(version),
            expected_assignee="claude",
            expected_head_event_ids=[],
        )
    finally:
        conn.close()
    assert receipt["work_id"] == HANDOFF_WORK_ID
    released = claim_row(database, claim_id)
    assert released["status"] == "released"
    assert "handoff" in str(released["release_reason"])


def test_typed_handoff_still_refuses_to_steal_someone_elses_claim(
    handoff_held: tuple[Path, str],
) -> None:
    """The pre-existing refusal this fix is modelled on stays exactly as loud."""
    database, claim_id = handoff_held
    conn = connect(database)
    try:
        conn.execute(
            "UPDATE work_items SET assignee='codex', version=version+1"
            " WHERE work_id=?",
            (HANDOFF_WORK_ID,),
        )
        conn.commit()
        version = conn.execute(
            "SELECT version FROM work_items WHERE work_id=?", (HANDOFF_WORK_ID,)
        ).fetchone()["version"]
        with pytest.raises(ValueError, match="cannot steal a held claim"):
            coord_db.post_existing_work_handoff(
                conn,
                work_id=HANDOFF_WORK_ID,
                actor="codex",
                session_id=ATTACKER,
                owner_lane="claude",
                target_intent="queued",
                task="give me that row",
                why="because I have the claim id",
                acceptance="whatever the attacker wants",
                refs=["artifacts/handoff-not-written-yet.json"],
                constraints=["none of the holder's business"],
                operation_id="handoff-hijack-0002",
                expected_version=int(version),
                expected_assignee="codex",
                expected_head_event_ids=[],
            )
    finally:
        conn.close()
    assert claim_row(database, claim_id)["status"] == "running"


def test_a_sibling_session_of_the_same_orchestrator_is_still_the_holder(
    board: Path,
) -> None:
    """One orchestrator, two session rows, one owner.

    `held_claim_id_for_session_family` has always treated a session family as a
    single owner, and the work_id resolution path resolves through it. An exact
    string match on `session_id` in the database guard would have broken
    resumption across a re-registered session, which is a real flow -- so the
    guard resolves the caller's family exactly as that path does.
    """
    sibling = "claude:worktree-b:owner-session"
    conn = connect(board)
    try:
        coord_db.register_session(conn, sibling, "claude")
        claim_id = coord_db.claim_work(conn, sibling, WORK_ID, step="sibling working")
        assert sibling in coord_db.related_session_ids(conn, OWNER, actor="claude")
        coord_db.heartbeat_claim(
            conn,
            claim_id,
            step="the base session renews the family lease",
            session_id=OWNER,
            actor="claude",
        )
    finally:
        conn.close()

    # And through the surface, by work_id -- the route that resolves a family.
    renewed = mcp_coord_server._tool_heartbeat(
        work_id=WORK_ID,
        step="and so does the MCP surface",
        actor="claude",
        session_id=OWNER,
        db_path=str(board),
    )
    assert renewed["claim_id"] == claim_id
    row = claim_row(board, claim_id)
    assert row["status"] == "running"
    assert row["session_id"] == sibling


def test_the_stricter_claim_id_refusal_for_a_sibling_is_left_alone(
    board: Path,
) -> None:
    """Addressing a sibling's claim *by id* was already refused, and still is.

    The claim_id branch has always demanded an exact `session_id` match when the
    caller volunteered one -- stricter than the family rule the work_id branch
    and the database guard apply. That refusal predates this fix and is not
    relaxed to make room for it; the sibling uses `work_id`, as it already did.
    """
    sibling = "claude:worktree-b:owner-session"
    conn = connect(board)
    try:
        coord_db.register_session(conn, sibling, "claude")
        claim_id = coord_db.claim_work(conn, sibling, WORK_ID, step="sibling working")
    finally:
        conn.close()

    with pytest.raises(ValueError, match="does not match stored claim session_id"):
        mcp_coord_server._tool_heartbeat(
            claim_id=claim_id,
            step="by id, from the base session",
            actor="claude",
            session_id=OWNER,
            db_path=str(board),
        )


def test_a_system_caller_needs_a_declared_name(held: tuple[Path, str]) -> None:
    """The door for system paths is enumerated, not a free-text bypass."""
    database, claim_id = held
    conn = connect(database)
    try:
        with pytest.raises(ValueError, match="undeclared system_caller"):
            _db_release(
                conn,
                claim_id,
                session_id=ATTACKER,
                actor="codex",
                system_caller="because I said so",
            )
        assert claim_row(database, claim_id)["status"] == "running"

        _db_release(
            conn,
            claim_id,
            session_id=None,
            system_caller="reaper:expired_lease",
        )
    finally:
        conn.close()
    assert claim_row(database, claim_id)["status"] == "released"


def test_no_shipped_caller_bypasses_the_holder_check(held: tuple[Path, str]) -> None:
    """`system_caller` is a named door, and nothing in this tree walks through it.

    Every system path that mutates a claim it does not hold -- lease expiry,
    zombie reaping, typed handoff -- is its own typed operation with its own SQL
    and its own guard. The enum exists so a future refactor onto these three
    mutators arrives by name instead of by omitted argument.
    """
    sources = [
        Path(coord_db.__file__).parent / name
        for name in ("mcp_coord_server.py", "cli.py")
    ]
    sources.append(Path(coord_db.__file__))
    for path in sources:
        assert "system_caller=" not in path.read_text(encoding="utf-8").replace(
            "system_caller=system_caller", ""
        ).replace("system_caller: str | None = None", ""), path


# --------------------------------------------------------------------------
# The CLI reaches the same three mutators
# --------------------------------------------------------------------------


# The package directory this test module actually imported. A subprocess that
# resolves `coordharness` anywhere else is testing a different tree -- which is
# precisely how an editable install silently answers for a copy under ablation.
SRC_ROOT = Path(coord_db.__file__).parents[2]


def coord_cli(
    database: Path, *args: str, session: str
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_ROOT),
        "COORD_PROJECT_ROOT": str(database.parent),
        "COORD_HOME": str(database.parent / ".coordharness"),
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
        [sys.executable, "-m", "coordharness.coord.cli", "--db", str(database), *args],
        cwd=database.parent,
        capture_output=True,
        text=True,
        env=env,
    )


def test_the_cli_subprocess_runs_the_tree_under_test(held: tuple[Path, str]) -> None:
    """Guard the guard: prove the CLI subprocess imports the same source.

    Without this, an editable install answers for whatever tree the tests were
    copied out of, and the two CLI refusals below would pass against code that
    never had the fix.
    """
    database, _claim_id = held
    result = coord_cli(database, "--help", session=OWNER)
    assert result.returncode == 0, result.stderr
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from coordharness.coord import coord_db; print(coord_db.__file__)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        cwd=database.parent,
    )
    assert probe.stdout.strip() == str(Path(coord_db.__file__)), probe.stdout


def test_the_cli_refuses_to_release_a_claim_this_shell_does_not_hold(
    held: tuple[Path, str],
) -> None:
    """The reason the fix belongs in the database and not only in the server.

    `coord release` and `coord heartbeat-claim` take a claim id straight off the
    command line. Neither ever compared it to the caller, and a server-side fix
    would not have reached either of them.
    """
    database, claim_id = held
    before = claim_row(database, claim_id)

    result = coord_cli(
        database,
        "release",
        claim_id,
        "--status",
        "paused",
        "--next-step",
        "whatever the attacker wants next",
        "--resume-when",
        "attacker says so",
        "--resume-manual",
        session=ATTACKER,
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "cannot touch a claim this session does not hold" in combined
    assert OWNER in combined
    assert ATTACKER in combined
    assert claim_row(database, claim_id) == before


def test_the_cli_refuses_to_heartbeat_a_claim_this_shell_does_not_hold(
    held: tuple[Path, str],
) -> None:
    database, claim_id = held
    result = coord_cli(
        database, "heartbeat-claim", claim_id, "--step", "stolen step", session=ATTACKER
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "cannot touch a claim this session does not hold" in combined


def test_the_cli_holder_still_releases_its_own_claim(held: tuple[Path, str]) -> None:
    database, claim_id = held
    result = coord_cli(
        database,
        "release",
        claim_id,
        "--status",
        "released",
        session=OWNER,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True
    assert claim_row(database, claim_id)["status"] == "released"
