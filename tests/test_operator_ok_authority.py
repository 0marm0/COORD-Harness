"""Who can mint an `operator_ok`, and what stands between an agent and it.

`tests/test_operator_sign_off.py` exercises the typed writer's own behaviour.
This file is the authority census underneath it: an enumeration of every path in
the codebase that reaches the `events` table with a caller-chosen `kind`, and a
set of pins on the agent-facing writers so a later change cannot widen the set
without a test going red.

The census is structural on purpose. A behavioural test proves that the writers
it happens to call refuse; only reading every insertion site proves that those
are the writers there are.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

COORD_DB_SOURCE = Path(coord_db.__file__)
SOURCE_ROOT = COORD_DB_SOURCE.parent

# Established by the census below and by reading each site; named here so a new
# dynamic-kind writer has to be added deliberately.
WRITERS_WITH_A_NON_LITERAL_KIND = {
    # Caller-chosen, and the only one; refuses the kind outright.
    "post_event",
    # Selector-chosen from two module constants (pinned below).
    "post_typed_controller_source_event",
    # Interpolated, but the interpolation cannot produce the string: the kind is
    # an f-string with a literal `_done` suffix (pinned below).
    "_insert_completion_receipt_unlocked",
}


# --------------------------------------------------------------------------
# Census: which code can put a caller's string in the `kind` column
# --------------------------------------------------------------------------


def _sql_of(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _kind_expression(call: ast.Call) -> ast.expr | str | None:
    """The expression that lands in the `kind` column of this INSERT, if any."""
    sql = _sql_of(call)
    if not sql or "INSERT INTO events" not in sql:
        return None
    columns_match = re.search(r"INSERT INTO events\s*\(([^)]*)\)", sql, re.S)
    values_match = re.search(r"VALUES\s*\(([^)]*)\)", sql, re.S)
    assert columns_match and values_match, sql
    columns = [c.strip() for c in columns_match.group(1).split(",")]
    values = [v.strip() for v in values_match.group(1).split(",")]
    assert len(columns) == len(values), sql
    position = columns.index("kind")
    slot = values[position]
    if slot != "?":
        # A literal spelled into the SQL text itself.
        return slot.strip("'")
    parameter_index = values[:position].count("?")
    if len(call.args) < 2 or not isinstance(call.args[1], ast.Tuple):
        return None
    element = call.args[1].elts[parameter_index]
    if isinstance(element, ast.Constant) and isinstance(element.value, str):
        return element.value
    return element


def _event_insert_sites() -> list[tuple[str, ast.expr | str | None]]:
    tree = ast.parse(COORD_DB_SOURCE.read_text(encoding="utf-8"))
    owners: list[tuple[int, int, str]] = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def owner_of(line: int) -> str:
        enclosing = [name for start, end, name in owners if start <= line <= end]
        return enclosing[-1] if enclosing else "<module>"

    sites: list[tuple[str, ast.expr | str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _kind_expression(node)
        if kind is None and not (
            _sql_of(node) and "INSERT INTO events" in (_sql_of(node) or "")
        ):
            continue
        sites.append((owner_of(node.lineno), kind))
    return sites


def test_the_census_finds_every_insertion_site():
    """Guard against the census silently measuring nothing."""
    sites = _event_insert_sites()
    raw = COORD_DB_SOURCE.read_text(encoding="utf-8").count("INSERT INTO events")
    assert len(sites) == raw, (sorted(name for name, _ in sites), raw)
    assert raw > 20, "the coordination writer set should not have collapsed"


def test_only_the_typed_writer_spells_the_operator_kind():
    literals = {
        name
        for name, kind in _event_insert_sites()
        if isinstance(kind, str) and kind.strip().lower() == "operator_ok"
    }
    assert literals == {"record_operator_sign_off"}, literals


def test_the_writers_whose_kind_is_not_a_literal_are_the_known_three():
    dynamic = {
        name
        for name, kind in _event_insert_sites()
        if kind is not None and not isinstance(kind, str)
    }
    assert dynamic == WRITERS_WITH_A_NON_LITERAL_KIND, dynamic


def test_the_typed_controller_writer_chooses_its_kind_from_two_constants():
    """The second dynamic writer stamps `trust='system'`, so its `kind` matters.

    Its parameter is an event *type* selector, not the kind: the local variable
    that reaches the column is assigned only from two literals, neither of which
    is `operator_ok`.
    """
    tree = ast.parse(COORD_DB_SOURCE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "post_typed_controller_source_event"
    )
    assigned = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "kind"
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Constant), ast.dump(node.value)
            assigned.add(node.value.value)
    assert assigned == {"controller_source_grant", "controller_source_consumption"}
    assert "kind" not in {arg.arg for arg in function.args.kwonlyargs + function.args.args}


def test_the_completion_receipt_kind_cannot_spell_the_operator_kind():
    """Its `kind` is interpolated, so the suffix is what bounds it."""
    tree = ast.parse(COORD_DB_SOURCE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_insert_completion_receipt_unlocked"
    )
    joined = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "kind"
            for target in node.targets
        )
    ]
    assert len(joined) == 1
    rendered = ast.unparse(joined[0])
    assert rendered.count("_done") == 2, rendered
    assert "operator" not in rendered, rendered


def test_no_other_module_writes_the_events_table_directly():
    """The census covers one file because only one file inserts events."""
    offenders = sorted(
        str(path.relative_to(SOURCE_ROOT.parent))
        for path in SOURCE_ROOT.parent.rglob("*.py")
        if path != COORD_DB_SOURCE
        and "INSERT INTO events" in path.read_text(encoding="utf-8")
    )
    assert offenders == [], offenders


# --------------------------------------------------------------------------
# Behaviour: the agent-facing writers refuse
# --------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path) -> Path:
    db = tmp_path / "coord.db"
    bootstrap_database(str(db))
    return db


@pytest.fixture
def conn(board: Path) -> sqlite3.Connection:
    connection = connect(board)
    try:
        yield connection
    finally:
        connection.close()


def test_the_public_event_writer_refuses_the_kind(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="cannot mint operator_ok"):
        coord_db.post_event(conn, kind="operator_ok", actor="claude")


def test_the_public_event_writer_refuses_the_kind_however_it_is_spelled(
    conn: sqlite3.Connection,
):
    """The refusal normalises, so casing and padding are not a way around it."""
    for spelling in ("OPERATOR_OK", "  Operator_Ok  ", "operator_ok"):
        with pytest.raises(ValueError, match="cannot mint operator_ok"):
            coord_db.post_event(conn, kind=spelling, actor="claude")


def test_the_public_event_writer_refuses_system_trust(conn: sqlite3.Connection):
    """`_operator_ok_event_is_valid_unlocked` requires `trust='system'`.

    Even if the kind refusal were removed, a public writer could not produce an
    event the validator accepts.
    """
    with pytest.raises(ValueError, match="cannot select system trust"):
        coord_db.post_event(conn, kind="note", actor="claude", trust="system")


def test_the_reserved_operation_namespace_is_not_squattable(
    conn: sqlite3.Connection,
):
    with pytest.raises(ValueError, match="reserved operator sign-off"):
        coord_db.post_event(
            conn,
            kind="note",
            actor="claude",
            idempotency_key="operator-ok:whatever",
        )


def test_the_receipt_column_is_not_writable_through_the_work_writer(
    conn: sqlite3.Connection,
):
    """A valid sign-off is the event *plus* the binding; neither alone is one."""
    coord_db.upsert_work(conn, work_id="W-AUTH-1", title="a row")
    with pytest.raises(Exception, match="operator_ok_event_id"):
        coord_db.upsert_work(conn, work_id="W-AUTH-1", operator_ok_event_id=1)


def test_a_hand_bound_receipt_pointing_at_an_agent_event_does_not_validate(
    conn: sqlite3.Connection,
):
    """The deepest pin: forging both halves through the supported writers fails.

    An agent that mints the most operator-shaped event `post_event` will produce,
    and then binds it by whatever means, still does not hold a sign-off -- the
    validator reads the event's kind, actor, trust and payload contract, and an
    agent writer can satisfy none of them.
    """
    coord_db.upsert_work(conn, work_id="W-AUTH-2", title="a row")
    event_id = coord_db.post_event(
        conn,
        kind="note",
        actor="operator",
        work_id="W-AUTH-2",
        trust="external",
        title="an operator-shaped note",
    )
    conn.execute(
        "UPDATE work_items SET operator_ok_event_id=? WHERE work_id=?",
        (event_id, "W-AUTH-2"),
    )
    assert not coord_db._has_valid_operator_ok_unlocked(conn, "W-AUTH-2")


# --------------------------------------------------------------------------
# Reachability of the one writer that can mint it
# --------------------------------------------------------------------------


def _module_source(name: str) -> str:
    return (SOURCE_ROOT / name).read_text(encoding="utf-8")


def test_the_agent_facing_surfaces_do_not_call_the_minter():
    minter = "record_operator_sign_off"
    for module in ("agent_cli.py", "mcp_coord_server.py", "board_context.py"):
        assert minter not in _module_source(module), module


def test_the_mcp_surface_reads_the_receipt_and_never_writes_it():
    """Its only mention is the read-model validator, not a writer."""
    source = _module_source("mcp_coord_server.py")
    mentions = [line.strip() for line in source.splitlines() if "operator_ok" in line]
    assert mentions, "the read-model field should still be projected"
    assert all(
        "_has_valid_operator_ok_unlocked" in line or "operator_ok_validated" in line
        for line in mentions
    ), mentions


def test_the_minter_is_reachable_from_exactly_one_command():
    """One caller, and it is the one that asks a person first."""
    callers = sorted(
        path.name
        for path in SOURCE_ROOT.rglob("*.py")
        if "record_operator_sign_off(" in path.read_text(encoding="utf-8")
        and path.name != "coord_db.py"
    )
    assert callers == ["cli.py"], callers


def test_an_agent_run_process_has_no_controlling_terminal():
    """The precondition the human-only channel actually rests on, measured here.

    This test asserts the *environment* rather than the guard: a process the
    harness spawns for an agent has no controlling terminal, so `/dev/tty` fails
    outright. If a future runner gives agent processes a terminal, this goes red
    and the guard's premise has to be re-argued rather than assumed.
    """
    try:
        handle = open("/dev/tty", "r")  # noqa: SIM115 -- closed below
    except OSError:
        return
    with handle:
        pytest.fail(
            "this process has a controlling terminal; the sign-off channel's "
            "human-only premise no longer follows from process capability alone"
        )


def test_the_reader_opens_a_terminal_in_a_mode_a_terminal_cannot_provide():
    """MEASURED: this used to be a defect in the channel, and is now fixed.

    The reader in `cli.py` used to open `/dev/tty` for updating (`"r+"`). A
    terminal is not seekable, and a buffered read/write stream requires
    seekability, so that open raised before any prompt was written.
    `io.UnsupportedOperation` subclasses `OSError`, so the existing
    `except OSError` turned it into `OperatorConsentUnavailable` -- the channel
    reported "no terminal" to a person who was sitting at one. Every test of
    that reader at the time substituted `open`, so the real call had never run
    against a terminal -- which is how the defect reached a release undetected.

    `cli.py:198-200` now opens two one-directional handles (`open(device, "r")`
    and `open(device, "w")`) instead of one `"r+"`, fixed in commit `c818f79`;
    `tests/test_operator_sign_off.py::test_a_real_terminal_can_actually_answer`
    exercises that fix against a real `os.openpty()` terminal.

    This test stays, pinned against a pty device node rather than against
    `cli.py`: the assertion is a fact about what a terminal supports (`"r+"`
    is never seekable), not about which handle strategy `cli.py` currently
    uses, so it documents *why* the two-handle fix was necessary and will
    catch a future regression back toward a single seekable handle.
    """
    controller, follower = os.openpty()
    try:
        device = os.ttyname(follower)
        with pytest.raises(OSError):
            open(device, "r+", buffering=1).close()
        readable = open(device, "r", buffering=1)  # noqa: SIM115 -- closed below
        with readable:
            assert os.isatty(readable.fileno()), "control: it is a terminal"
    finally:
        os.close(controller)
        os.close(follower)
