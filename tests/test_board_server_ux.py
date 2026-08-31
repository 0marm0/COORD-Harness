"""Two startup failures that used to reach the terminal as raw tracebacks.

Both `make_server` failures below happen for a caller who ran `coord-board`
by hand: a port already held by something else, and a `--db` that points at
a file SQLite will open but that carries none of this board's tables. Before
this file, `main()` caught only `FileNotFoundError` (no file at all) and
`ValueError` (a rejected brand name); an `OSError` from the bind or a
`sqlite3.OperationalError` from the first real query on a schema-only file
propagated straight out of `main()` as a nine-frame traceback with no host,
port, or path named anywhere in it.
"""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest

from coordharness import demo
from coordharness.board import server as board_server


def test_eaddrinuse_exits_cleanly_and_names_host_port_and_remedies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A port already in use is a clean exit=2 with actionable text, not a traceback."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        db = tmp_path / "coord.db"
        demo.seed(db, quiet=True)

        rc = board_server.main(
            ["--host", "127.0.0.1", "--port", str(port), "--db", str(db)]
        )
    finally:
        holder.close()

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert f"127.0.0.1:{port}" in captured.err
    assert "--port" in captured.err
    assert "COORD_BOARD_PORT" in captured.err
    assert f"lsof -i :{port}" in captured.err
    # Not the raw exception text: a caller reading this should never need to
    # know the errno name to understand what happened.
    assert "Traceback" not in captured.err
    assert "EADDRINUSE" not in captured.err


def test_eaddrinuse_is_the_only_oserror_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main()` narrows its OSError catch to EADDRINUSE and re-raises anything else.

    A blanket `except OSError` would also swallow a bind refused for some
    other reason (a privileged port, a bad address) behind the same
    port-conflict message, which would send a reader looking for the wrong
    fix. This pins the handler to the one errno the task names.
    """
    db = tmp_path / "coord.db"
    demo.seed(db, quiet=True)
    other = OSError("simulated non-bind failure")
    other.errno = None

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise other

    monkeypatch.setattr(board_server, "make_server", _explode)
    with pytest.raises(OSError) as excinfo:
        board_server.main(["--port", "0", "--db", str(db)])
    assert excinfo.value is other


def test_schemaless_database_exits_cleanly_with_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file that opens as SQLite but has none of the board's tables fails closed."""
    db = tmp_path / "coord.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    rc = board_server.main(["--port", "0", "--db", str(db)])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert str(db) in captured.err
    assert "is not a coord-board database" in captured.err
    assert "seed a demo board" in captured.err
    assert "coord-board --db" in captured.err
    assert "Traceback" not in captured.err
    assert "OperationalError" not in captured.err


def test_schemaless_message_is_distinct_from_the_missing_file_message(
    tmp_path: Path,
) -> None:
    """The two diagnostics never claim the same thing about the filesystem.

    `_missing_database_message` says "no coordination database at X", which
    is false of a file that exists and opens; `_foreign_database_message`
    must not repeat that claim, only the shared remediation.
    """
    missing = board_server._missing_database_message(str(tmp_path / "absent.db"))
    present = tmp_path / "present.db"
    sqlite3.connect(str(present)).close()
    foreign = board_server._foreign_database_message(str(present))

    assert "no coordination database at" in missing
    assert "no coordination database at" not in foreign
    assert "is not a coord-board database" in foreign
    # Same remedy either way.
    assert "python -m coordharness.demo" in missing
    assert "python -m coordharness.demo" in foreign
