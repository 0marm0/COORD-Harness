"""The escape hatch, and what stops an agent walking through it.

`operator_ok` is how a human overrides the cross-lane review gate: the T0 row
whose reviewer is unavailable, and which `complete_claim` otherwise refuses
forever with *"T0 review has not passed and no valid operator-ok event is
bound"*. Until `coord sign-off` existed, every part of that mechanism was
present except the part that writes it -- the column, two validators, three
consumers, and a `post_event` refusal naming a writer that was nowhere in the
tree. A gate with no key is not a gate, it is a wall.

The interesting tests here are not the ones where signing works. They are the
ones that come at the writer from the direction an agent would: without a
terminal, with the answer piped in, with plausible bypass variables set in the
environment, and through the surfaces agents actually hold. Getting that wrong
does not produce a broken feature, it produces a working one -- agent
self-approval wearing an operator's name, which is the exact failure the review
tiers exist to prevent.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from coordharness import entry
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import cli as coord_cli
from coordharness.coord import coord_db, review_integrity
from coordharness.coord.config import connect

WORK_ID = "DEMO-GATE-1"
LANE_SESSION = "claude:sign-off-fixture"
PROOF = "artifacts/gate.json"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One T0 row, claimed, with its proof already on disk.

    T0 is reached through the effect predicates rather than by declaring it, so
    the row is the shape the gate actually fires on. `complete_claim` resolves
    the declared proof against HARNESS_ROOT, frozen at import, so it is pointed
    at the throwaway tree.
    """
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    proof = tmp_path / PROOF
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text('{"done": true}\n', encoding="utf-8")
    # The custody gate asks for every proof in git's index, not only Markdown.
    # This module measures the sign-off gate, so it satisfies the custody one.
    subprocess.run(["git", "add", PROOF], cwd=tmp_path, check=True)

    database = tmp_path / "coord.db"
    bootstrap_database(database)
    conn = connect(database)
    try:
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="irreversible cutover of the refunds projection",
            assignee="claude",
            module="runtime",
            surface="job",
            done_signal=PROOF,
            acceptance_json='["the projection serves the new shape"]',
            note="operator sign-off fixture",
            intent_state="queued",
        )
        coord_db.register_session(conn, LANE_SESSION, "claude")
        assert coord_db.effective_review_tier_for_work(conn, WORK_ID) == "T0"
    finally:
        conn.close()
    return database


@pytest.fixture
def claim(board: Path) -> str:
    conn = connect(board)
    try:
        return str(coord_db.claim_work(conn, LANE_SESSION, WORK_ID, lease_s=600))
    finally:
        conn.close()


@pytest.fixture
def operator_at_the_keyboard(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the one thing a test process cannot have: a person.

    Substituted at the function, not through a flag or a variable, and
    deliberately so -- a seam a subprocess could reach would be a seam an agent
    could reach. `test_no_environment_variable_unlocks_the_sign_off` is the
    other half of this fixture's argument.
    """

    def _typed_the_work_id(prompt: str) -> str:
        _typed_the_work_id.prompts.append(prompt)
        return WORK_ID

    _typed_the_work_id.prompts = []
    monkeypatch.setattr(
        coord_cli, "_read_controlling_terminal_confirmation", _typed_the_work_id
    )
    return _typed_the_work_id


def _sign_off_argv(board: Path, **overrides: str) -> list[str]:
    argv = {
        "work_id": WORK_ID,
        "--reason": "the reviewing lane is offline and I read the artifact myself",
        "--ref": PROOF,
        "--operation-id": "signoff-2026-08-29-a",
    }
    argv.update(overrides)
    flat = ["--db", str(board), "sign-off", argv.pop("work_id")]
    for flag, value in argv.items():
        flat += [flag, value]
    return flat


def _events(board: Path, kind: str) -> list[dict]:
    conn = connect(board)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM events WHERE kind=? ORDER BY event_id", (kind,)
            )
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The hatch opens
# --------------------------------------------------------------------------


def test_the_gate_is_shut_before_anyone_signs(board: Path, claim: str):
    """The refusal this whole feature exists to answer.

    Asserted first so the tests below are demonstrably clearing a real gate and
    not closing a row that would have closed anyway.
    """
    conn = connect(board)
    try:
        assert coord_db.completion_review_state(conn, WORK_ID)["needs_review"] is True
        with pytest.raises(ValueError, match="no valid operator-ok event is bound"):
            coord_db.complete_claim(
                conn, claim, session_id=LANE_SESSION, actor="claude"
            )
    finally:
        conn.close()


def test_a_sign_off_lets_a_gated_row_close(
    board: Path, claim: str, operator_at_the_keyboard, capsys: pytest.CaptureFixture[str]
):
    assert entry.main(_sign_off_argv(board)) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True
    assert receipt["replayed"] is False
    assert receipt["authority_channel"] == coord_db.OPERATOR_AUTHORITY_CHANNEL

    conn = connect(board)
    try:
        # The readers that were already in the tree accept it -- the point of a
        # typed writer is that nothing downstream has to be taught about it.
        assert coord_db._has_valid_operator_ok_unlocked(conn, WORK_ID) is True
        assert review_integrity.classify_verdict_status(conn, WORK_ID) == {
            "work_id": WORK_ID,
            "reviewed": True,
            "reason": "operator_ok",
        }
        state = coord_db.completion_review_state(conn, WORK_ID)
        assert state["operator_ok"] is True
        assert state["operator_ok_satisfies_review"] is True
        assert state["needs_review"] is False
        assert coord_db.complete_claim(
            conn, claim, session_id=LANE_SESSION, actor="claude"
        )
    finally:
        conn.close()


def test_the_receipt_is_bound_to_the_row_not_merely_written(
    board: Path, operator_at_the_keyboard
):
    """An unbound event is not a sign-off.

    `_has_valid_operator_ok_unlocked` reads `work_items.operator_ok_event_id`
    and validates the event it names. An event minted without that binding
    would be a receipt nothing points at: valid on its face, inert in every
    reader.
    """
    assert entry.main(_sign_off_argv(board)) == 0
    events = _events(board, "operator_ok")
    assert len(events) == 1
    conn = connect(board)
    try:
        row = dict(
            conn.execute(
                "SELECT * FROM work_items WHERE work_id=?", (WORK_ID,)
            ).fetchone()
        )
        assert row["operator_ok_event_id"] == events[0]["event_id"]
        # The stamped fields the public writer is not trusted to choose.
        assert events[0]["actor"] == "operator"
        assert events[0]["trust"] == "system"
        assert events[0]["session_id"] is None, (
            "an operator has no lane session; stamping one would attribute a "
            "human decision to an agent"
        )
        payload = json.loads(events[0]["payload_json"])
        assert payload["work_contract_sha256"] == (
            coord_db.operator_authority_contract_sha256(row)
        )
    finally:
        conn.close()


def test_the_prompt_shows_what_is_being_signed(board: Path, operator_at_the_keyboard):
    """Consent to a work id is not consent to whatever that row now says."""
    assert entry.main(_sign_off_argv(board)) == 0
    prompt = operator_at_the_keyboard.prompts[0]
    for shown in (WORK_ID, "irreversible cutover", "T0", PROOF, "claude"):
        assert shown in prompt, f"{shown!r} missing from the confirmation prompt"


# --------------------------------------------------------------------------
# Coming at it from the direction that would break it
# --------------------------------------------------------------------------


def test_a_process_without_a_controlling_terminal_cannot_sign(
    board: Path, capsys: pytest.CaptureFixture[str]
):
    """The load-bearing test: no fixture, so no person.

    A pytest process is exactly the shape of an agent's process -- no
    controlling terminal -- and the refusal has to survive the boundary as a
    refusal rather than a crash.
    """
    code = entry.main(_sign_off_argv(board))
    captured = capsys.readouterr()

    assert code != 0
    assert "Traceback" not in captured.err
    assert "controlling terminal" in captured.err
    assert captured.out == ""
    assert _events(board, "operator_ok") == []


@pytest.mark.parametrize(
    "variable",
    [
        "COORD_ACTOR",
        "COORD_OPERATOR",
        "COORD_OPERATOR_OK",
        "COORD_ASSUME_YES",
        "COORD_NON_INTERACTIVE",
        "CI",
    ],
)
def test_no_environment_variable_unlocks_the_sign_off(
    board: Path, monkeypatch: pytest.MonkeyPatch, variable: str
):
    """The argument for a terminal, stated as a test.

    An environment variable is a value the caller supplies, and the caller is
    the party this guard exists to stop. If any of these opened the hatch, the
    hatch would be open to every agent that reads its own source. They are
    parametrized rather than asserted in prose so that adding such an escape
    later fails here instead of shipping.
    """
    monkeypatch.setenv(variable, "1")
    assert entry.main(_sign_off_argv(board)) != 0
    assert _events(board, "operator_ok") == []


def test_an_operator_actor_is_not_an_operator(
    board: Path, monkeypatch: pytest.MonkeyPatch
):
    """Naming yourself the operator is not being one."""
    monkeypatch.setenv("COORD_ACTOR", "operator")
    monkeypatch.setenv("COORD_SESSION_ID", "operator:me")
    assert entry.main(_sign_off_argv(board)) != 0
    assert _events(board, "operator_ok") == []


def test_a_piped_answer_does_not_sign(board: Path, monkeypatch: pytest.MonkeyPatch):
    """`yes | coord sign-off` and its heredoc cousins, end to end."""
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{WORK_ID}\n" * 5))
    assert entry.main(_sign_off_argv(board)) != 0
    assert _events(board, "operator_ok") == []


def test_the_reader_takes_the_terminal_and_not_stdin(monkeypatch: pytest.MonkeyPatch):
    """Why the piped answer above is ignored rather than merely wrong.

    Exercised against the reader itself with a terminal standing in, because
    the end-to-end case above passes for the weaker reason -- there is no
    terminal there at all, so stdin would never be reached either way. Here a
    terminal *is* present and stdin holds the string that would have signed; the
    answer has to come off the terminal.
    """
    from coordharness.coord import cli as module

    class _Terminal:
        def __init__(self) -> None:
            self.written: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fileno(self) -> int:
            return 0

        def write(self, text: str) -> None:
            self.written.append(text)

        def flush(self) -> None:
            pass

        def readline(self) -> str:
            return "from-the-terminal\n"

    terminal = _Terminal()
    opened: list[str] = []
    real_open = open

    def _open(path, *args, **kwargs):
        opened.append(str(path))
        if str(path) == "/dev/tty":
            return terminal
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _open)
    monkeypatch.setattr(module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin\n"))

    answer = module._read_controlling_terminal_confirmation("sign? ")

    # Every open goes to the terminal and none to stdin; the reader takes two
    # handles on the device (a read side and a write side) because a terminal
    # is not seekable and a single read/write handle cannot be opened on one.
    assert opened and set(opened) == {"/dev/tty"}
    assert answer == "from-the-terminal"
    assert terminal.written == ["sign? "], "the prompt goes to the terminal too"


def test_a_terminal_that_is_not_a_terminal_is_refused(
    monkeypatch: pytest.MonkeyPatch
):
    """Opening the path is not enough; it has to be a tty."""
    from coordharness.coord import cli as module

    class _NotATerminal:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fileno(self) -> int:
            return 0

    monkeypatch.setattr(
        "builtins.open", lambda path, *a, **k: _NotATerminal()
    )
    monkeypatch.setattr(module.os, "isatty", lambda _fd: False)
    with pytest.raises(module.OperatorConsentUnavailable, match="not a terminal"):
        module._read_controlling_terminal_confirmation("sign? ")


def test_a_wrong_confirmation_records_nothing(
    board: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A terminal is necessary, not sufficient: `yes` is not the work id."""
    monkeypatch.setattr(
        coord_cli, "_read_controlling_terminal_confirmation", lambda prompt: "y"
    )
    code = entry.main(_sign_off_argv(board))
    assert code != 0
    assert "aborted" in capsys.readouterr().err
    assert _events(board, "operator_ok") == []


def test_the_public_event_writer_still_cannot_mint_one(board: Path):
    """The refusal that named this writer stays a refusal.

    Adding the writer must not turn `post_event` into a second way in; the
    typed path exists precisely so the public one can keep saying no.
    """
    conn = connect(board)
    try:
        with pytest.raises(ValueError, match="typed human-only writer"):
            coord_db.post_event(
                conn, kind="operator_ok", actor="operator", work_id=WORK_ID
            )
    finally:
        conn.close()
    assert _events(board, "operator_ok") == []


def test_the_mcp_surface_does_not_carry_the_sign_off():
    """The surface agents actually hold offers nothing.

    Absence from MCP is not the guard -- agents run the CLI too -- but it is a
    necessary half of it, and it is the half a later convenience commit would
    quietly undo.
    """
    pytest.importorskip("mcp", reason="the MCP tool surface needs the [mcp] extra")
    from coordharness.coord import mcp_coord_server

    source = Path(mcp_coord_server.__file__).read_text(encoding="utf-8")
    assert "record_operator_sign_off" not in source
    assert "sign_off" not in source


def test_the_operation_id_namespace_cannot_be_squatted(board: Path):
    """Denying the hatch is as good as forging it, for anyone who is stuck.

    The kind refusal stops a forged sign-off. It does not stop a writer taking
    `operator-ok:<id>` for some other event, which would turn the operator's
    next real sign-off under that id into a replay collision. The two
    neighbouring reserved namespaces exist for this, and this one now joins
    them.
    """
    conn = connect(board)
    try:
        with pytest.raises(ValueError, match="reserved operator sign-off namespace"):
            coord_db.post_event(
                conn,
                kind="note",
                actor="claude",
                work_id=WORK_ID,
                idempotency_key="operator-ok:signoff-2026-08-29-a",
            )
    finally:
        conn.close()


def test_upsert_work_still_cannot_write_the_receipt_field(board: Path):
    conn = connect(board)
    try:
        with pytest.raises(ValueError, match="typed receipt fields"):
            coord_db.upsert_work(conn, WORK_ID, operator_ok_event_id=1)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Replay, drift and the silent no-op
# --------------------------------------------------------------------------


def test_the_same_operation_id_twice_mints_one_event(
    board: Path, operator_at_the_keyboard, capsys: pytest.CaptureFixture[str]
):
    assert entry.main(_sign_off_argv(board)) == 0
    first = json.loads(capsys.readouterr().out)
    assert entry.main(_sign_off_argv(board)) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["event_id"] == first["event_id"]
    assert second["replayed"] is True
    assert first["replayed"] is False
    assert len(_events(board, "operator_ok")) == 1


def test_a_reused_operation_id_for_a_different_request_is_refused(
    board: Path, operator_at_the_keyboard
):
    """Replay safety is not "the second call is free"."""
    assert entry.main(_sign_off_argv(board)) == 0
    code = entry.main(
        _sign_off_argv(board, **{"--reason": "something else entirely"})
    )
    assert code != 0
    assert len(_events(board, "operator_ok")) == 1


def test_signing_a_row_that_moved_under_you_is_refused(
    board: Path, operator_at_the_keyboard
):
    conn = connect(board)
    try:
        version = int(
            conn.execute(
                "SELECT version FROM work_items WHERE work_id=?", (WORK_ID,)
            ).fetchone()["version"]
        )
    finally:
        conn.close()
    code = entry.main(
        _sign_off_argv(board, **{"--expected-version": str(version + 5)})
    )
    assert code != 0
    assert _events(board, "operator_ok") == []


def test_an_open_review_barrier_refuses_instead_of_signing_into_the_void(
    board: Path, operator_at_the_keyboard, capsys: pytest.CaptureFixture[str]
):
    """The failure mode a naive writer would ship.

    `classify_verdict_status` honours `operator_ok` only while the review
    barrier is zero, so a sign-off recorded against an outstanding
    `audit_request` is a *valid event that changes nothing*: the command would
    report success and the row would stay exactly as stuck. Refusing, and
    naming the barrier, is the difference between an escape hatch and a
    placebo.
    """
    conn = connect(board)
    try:
        barrier = coord_db.post_event(
            conn,
            kind="audit_request",
            actor="claude",
            session_id=LANE_SESSION,
            to_selector="actor:codex",
            work_id=WORK_ID,
        )
    finally:
        conn.close()

    code = entry.main(_sign_off_argv(board))
    captured = capsys.readouterr()

    assert code != 0
    assert f"event:{barrier}" in captured.err
    assert "review barrier" in captured.err
    assert _events(board, "operator_ok") == []

    conn = connect(board)
    try:
        assert coord_db.completion_review_state(conn, WORK_ID)["needs_review"] is True
    finally:
        conn.close()


def test_signing_twice_with_a_fresh_id_is_refused(
    board: Path, operator_at_the_keyboard
):
    assert entry.main(_sign_off_argv(board)) == 0
    code = entry.main(_sign_off_argv(board, **{"--operation-id": "signoff-second-b"}))
    assert code != 0
    assert len(_events(board, "operator_ok")) == 1


# --------------------------------------------------------------------------
# The shape of the verb
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dropped", ["--reason", "--ref", "--operation-id"]
)
def test_the_three_required_fields_are_required(
    board: Path, dropped: str, capsys: pytest.CaptureFixture[str]
):
    """A sign-off with no reason, no evidence or no replay id is not one.

    The refusal is asserted by *name*, not by exit code: argparse answers "no
    such subcommand" with the same status 2, so a code-only assertion would go
    on passing against a build where the verb does not exist at all.
    """
    argv = _sign_off_argv(board)
    index = argv.index(dropped)
    del argv[index : index + 2]
    with pytest.raises(SystemExit) as exit_info:
        entry.main(argv)
    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert dropped in stderr and "required" in stderr, stderr


def test_an_unknown_row_is_refused_before_anyone_is_asked(
    board: Path, operator_at_the_keyboard
):
    """No prompt for a row that does not exist."""
    assert entry.main(_sign_off_argv(board, work_id="DEMO-NOPE")) != 0
    assert operator_at_the_keyboard.prompts == []


def test_a_real_terminal_can_actually_answer():
    """The one test here that does not monkeypatch `open`.

    Every other terminal test in this file substitutes `builtins.open`, and
    that is precisely how a defect that refused EVERY real terminal survived:
    the reader opened `/dev/tty` with mode `"r+"`, which builds a
    `BufferedRandom` and demands a seekable stream, so on a genuine tty it
    raised `io.UnsupportedOperation` -- an `OSError` subclass, caught by the
    handler for "this process has no controlling terminal" and reported to a
    real, present human as their own absence. `coord sign-off` was
    unreachable by anyone.

    So this one allocates an actual terminal and reads an actual answer
    through it. A mock cannot fail the way the device did.
    """
    import os

    from coordharness.coord import cli as module

    master, subordinate = os.openpty()
    try:
        os.write(master, b"the-typed-answer\n")
        answer = module._read_controlling_terminal_confirmation(
            "sign? ", device=os.ttyname(subordinate)
        )
    finally:
        os.close(master)
        os.close(subordinate)

    assert answer == "the-typed-answer"
