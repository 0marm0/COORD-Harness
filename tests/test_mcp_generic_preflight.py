from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.bootstrap import bootstrap_database  # noqa: E402
from coordharness.coord import locked_paths, mcp_coord_server  # noqa: E402


def test_generic_preflight_reads_a_fresh_standalone_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / ".coordharness" / "coord.db"
    bootstrap_database(db)
    monkeypatch.setenv("COORD_DEPLOYMENT_PROFILE", "generic")
    monkeypatch.setattr(
        locked_paths,
        "assert_locked_data_local",
        lambda _root: pytest.fail("generic preflight invoked the private locked-layout guard"),
    )

    payload = mcp_coord_server._tool_preflight(
        actor="codex",
        session_id="codex:fresh-preflight",
        db_path=str(db),
    )

    assert payload["actor"] == "codex"
    assert payload["assigned_open_count"] == 0
    assert payload["next_work_ids"] == []
    assert payload["coord_board_count"] == 0
    assert payload["capability_handshake"]["lifecycle_authority"] == "coord.db"


def test_strict_profile_retains_locked_layout_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COORD_DEPLOYMENT_PROFILE", "strict")
    monkeypatch.setattr(
        locked_paths,
        "locked_data_local_findings",
        lambda _root: ["strict locked-layout sentinel"],
    )

    with pytest.raises(RuntimeError, match="strict locked-layout sentinel"):
        locked_paths.assert_deployment_locked_data_local(tmp_path)


def test_generic_core_reads_are_empty_or_structured_on_a_fresh_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / ".coordharness" / "coord.db"
    bootstrap_database(db)
    monkeypatch.setenv("COORD_DEPLOYMENT_PROFILE", "generic")

    board = mcp_coord_server._tool_board(db_path=str(db))
    next_work = mcp_coord_server._tool_next_work(actor="codex", db_path=str(db))
    missing = mcp_coord_server._tool_work_context(
        "MISSING", actor="codex", db_path=str(db)
    )

    assert board["rows"] == []
    assert board["query_core"]["mode"] == "generic_coord_db"
    assert next_work["items"] == []
    assert next_work["query_core"]["lifecycle_authority"] == "coord.db"
    assert missing["ok"] is False
    assert missing["error"]["code"] == "work_not_found"


def test_generic_board_context_reads_coord_db_without_strict_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coordharness.coord import board_context, coord_db
    from coordharness.coord.config import connect

    db = tmp_path / ".coordharness" / "coord.db"
    bootstrap_database(db)
    monkeypatch.setenv("COORD_DEPLOYMENT_PROFILE", "generic")
    conn = connect(db)
    try:
        coord_db.upsert_work(
            conn,
            "GENERIC-1",
            title="Generic work",
            intent_state="queued",
            assignee="codex",
            module="harness",
            done_signal="reports/generic.md",
        )
    finally:
        conn.close()

    rows = board_context.load_rows(db)
    focus = board_context.build_focus(rows, "GENERIC-1")

    assert [row["work_id"] for row in rows] == ["GENERIC-1"]
    assert rows[0]["query_core_mode"] == "generic_coord_db"
    assert focus["work_id"] == "GENERIC-1"
