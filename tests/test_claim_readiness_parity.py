"""Two surfaces answered the same claim-readiness question differently.

MCP `claim_work` refused any row missing a descriptive title, a `done_signal`
or T0/T1 acceptance. CLI `coord claim` took the same row without a word. That
is a real divergence -- an agent routed through the MCP could not claim work
its CLI-driven peer had just claimed -- and nothing said so anywhere.

The defaults are deliberately preserved (the MCP is the strict door, the CLI is
the low-friction one), but they now come from one definition and the CLI says
out loud what it is letting through. `COORD_CLAIM_STRICT` is the single knob:
`1` makes both refuse, `0` makes both warn. These four modes are the matrix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import cli, coord_db
from coordharness.coord.config import connect

WORK_ID = "DEMO-CLA-READINESS"
SESSION = "claude:readiness"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.delenv(coord_db.CLAIM_STRICT_ENV, raising=False)
    # The CLI resolves its identity from the environment, not from flags.
    monkeypatch.setenv("COORD_ACTOR", "claude")
    monkeypatch.setenv("COORD_SESSION_ID", SESSION)
    bootstrap_database(tmp_path / ".coordharness" / "coord.db")
    return tmp_path


@pytest.fixture
def db_path(project: Path) -> Path:
    return project / ".coordharness" / "coord.db"


def _seed_not_ready(db_path: Path) -> list[str]:
    """A row with a real title but no proof declared: the ambiguous case."""
    conn = connect(db_path)
    try:
        coord_db.register_session(conn, SESSION, "claude")
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="A work item with no proof declared",
            assignee="claude",
            intent_state="queued",
        )
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        missing = coord_db.claim_readiness(WORK_ID, dict(row), actor="claude")
    finally:
        conn.close()
    assert "done_signal" in missing, missing
    return missing


def _mcp_claim(db_path: Path):
    from coordharness.coord import mcp_coord_server

    return mcp_coord_server._tool_claim(
        WORK_ID,
        step="starting",
        actor="claude",
        session_id=SESSION,
        db_path=str(db_path),
    )


def _cli_claim(db_path: Path) -> int:
    return cli.main(["--db", str(db_path), "claim", WORK_ID, "--step", "starting"])


# -- the readiness definition itself ---------------------------------------

def test_one_definition_backs_both_surfaces(db_path: Path) -> None:
    from coordharness.coord import mcp_coord_server

    conn = connect(db_path)
    try:
        missing = _seed_not_ready(db_path)
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        assert mcp_coord_server._claim_quality_missing(
            WORK_ID, dict(row), "claude"
        ) == missing
    finally:
        conn.close()


def test_a_ready_row_is_missing_nothing(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        coord_db.register_session(conn, SESSION, "claude")
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="A work item that declares its own proof",
            assignee="claude",
            done_signal="artifacts/readiness.json",
            acceptance_json=json.dumps(["the artifact exists"]),
            intent_state="queued",
        )
        row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (WORK_ID,)
        ).fetchone()
        assert coord_db.claim_readiness(WORK_ID, dict(row), actor="claude") == []
    finally:
        conn.close()


# -- the four modes ---------------------------------------------------------

def test_mcp_refuses_by_default(db_path: Path) -> None:
    missing = _seed_not_ready(db_path)
    with pytest.raises(coord_db.ClaimReadinessError) as excinfo:
        _mcp_claim(db_path)
    # Structured, not just prose: the field list is on the exception.
    assert excinfo.value.missing == missing
    assert excinfo.value.work_id == WORK_ID
    assert "done_signal" in str(excinfo.value)
    conn = connect(db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM claims WHERE work_id=?", (WORK_ID,)
        ).fetchone() is None
    finally:
        conn.close()


def test_mcp_warns_and_proceeds_when_strict_is_off(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = _seed_not_ready(db_path)
    monkeypatch.setenv(coord_db.CLAIM_STRICT_ENV, "0")

    result = _mcp_claim(db_path)

    assert result["claim_id"]
    warning = result["claim_readiness"]
    assert warning["missing"] == missing
    assert warning["enforcement"] == coord_db.CLAIM_READINESS_WARN
    assert warning["code"] == "claim_not_ready"


def test_cli_warns_and_proceeds_by_default(
    db_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    missing = _seed_not_ready(db_path)

    with caplog.at_level("WARNING", logger="coord"):
        assert _cli_claim(db_path) == 0

    warnings = [r for r in caplog.records if r.name == "coord"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    line = warnings[0].getMessage()
    assert "\n" not in line, line
    for field in missing:
        assert field in line
    assert coord_db.CLAIM_STRICT_ENV in line
    captured = capsys.readouterr()
    emitted = json.loads(captured.out.strip().splitlines()[-1])
    assert emitted["ok"] is True
    assert emitted["claim_readiness"]["missing"] == missing


def test_cli_warning_reaches_a_real_terminals_stderr(
    db_path: Path, project: Path
) -> None:
    """MEASURED end to end, because the advisory rides the logging channel.

    In-process the warning is a log record; what a shell actually sees is the
    thing under test, so this runs a real process and reads its real stderr.
    stdout has to stay a single parseable JSON document either way.
    """
    _seed_not_ready(db_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from coordharness.coord import cli; raise SystemExit(cli.main("
            f"['--db', {str(db_path)!r}, 'claim', {WORK_ID!r}, '--step', 'starting']))",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "COORD_PROJECT_ROOT": str(project),
            "COORD_HOME": str(project / ".coordharness"),
            "COORD_ACTOR": "claude",
            "COORD_SESSION_ID": SESSION,
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip().count("\n") == 0, proc.stderr
    assert "done_signal" in proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_cli_refuses_when_strict_is_on(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = _seed_not_ready(db_path)
    monkeypatch.setenv(coord_db.CLAIM_STRICT_ENV, "1")

    with pytest.raises(coord_db.ClaimReadinessError) as excinfo:
        _cli_claim(db_path)

    assert excinfo.value.missing == missing
    conn = connect(db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM claims WHERE work_id=?", (WORK_ID,)
        ).fetchone() is None
    finally:
        conn.close()


def test_a_ready_row_warns_on_neither_surface(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = connect(db_path)
    try:
        coord_db.register_session(conn, SESSION, "claude")
        coord_db.upsert_work(
            conn,
            WORK_ID,
            title="A work item that declares its own proof",
            assignee="claude",
            done_signal="artifacts/readiness.json",
            acceptance_json=json.dumps(["the artifact exists"]),
            intent_state="queued",
        )
    finally:
        conn.close()

    assert _cli_claim(db_path) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "claim_readiness" not in json.loads(
        captured.out.strip().splitlines()[-1]
    )


# -- the knob itself --------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", coord_db.CLAIM_READINESS_REFUSE),
        ("0", coord_db.CLAIM_READINESS_WARN),
        ("true", coord_db.CLAIM_READINESS_REFUSE),
        ("off", coord_db.CLAIM_READINESS_WARN),
    ],
)
def test_the_knob_moves_both_defaults(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv(coord_db.CLAIM_STRICT_ENV, value)
    assert coord_db.claim_readiness_enforcement(
        default=coord_db.CLAIM_READINESS_REFUSE
    ) == expected
    assert coord_db.claim_readiness_enforcement(
        default=coord_db.CLAIM_READINESS_WARN
    ) == expected


# -- the entry point that documents the knob --------------------------------
#
# `coord-mcp` had no argument handling at all: `--help` and `--version` both
# fell through and started a stdio server that then sat waiting on a terminal.
# The knob above is only discoverable if the entry point will say what it reads.

def test_entry_point_help_names_the_knob(capsys: pytest.CaptureFixture[str]) -> None:
    from coordharness.coord import mcp_coord_server

    assert mcp_coord_server.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert coord_db.CLAIM_STRICT_ENV in out
    assert "COORD_DB" in out


def test_entry_point_reports_its_version(capsys: pytest.CaptureFixture[str]) -> None:
    from coordharness import __version__
    from coordharness.coord import mcp_coord_server

    assert mcp_coord_server.main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_entry_point_refuses_an_unknown_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from coordharness.coord import mcp_coord_server

    assert mcp_coord_server.main(["--serve-forever"]) == 2
    captured = capsys.readouterr()
    # stdout is the MCP protocol channel; a usage error must never land there.
    assert captured.out == ""
    assert "--serve-forever" in captured.err


def test_entry_point_prints_the_resolved_db_to_stderr(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from coordharness.coord import mcp_coord_server

    monkeypatch.setenv("COORD_DB", str(db_path))

    class _Started(RuntimeError):
        pass

    def _no_server(**_kwargs):
        raise _Started

    monkeypatch.setattr(mcp_coord_server, "build_server", _no_server)
    with pytest.raises(_Started):
        mcp_coord_server.main([])

    assert f"COORD_DB={db_path}" in capsys.readouterr().err


def test_unset_and_unparseable_leave_each_default_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for setter in (
        lambda: monkeypatch.delenv(coord_db.CLAIM_STRICT_ENV, raising=False),
        lambda: monkeypatch.setenv(coord_db.CLAIM_STRICT_ENV, ""),
        lambda: monkeypatch.setenv(coord_db.CLAIM_STRICT_ENV, "maybe"),
    ):
        setter()
        assert coord_db.claim_readiness_enforcement(
            default=coord_db.CLAIM_READINESS_REFUSE
        ) == coord_db.CLAIM_READINESS_REFUSE
        assert coord_db.claim_readiness_enforcement(
            default=coord_db.CLAIM_READINESS_WARN
        ) == coord_db.CLAIM_READINESS_WARN
