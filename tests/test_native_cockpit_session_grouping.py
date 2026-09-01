"""Cockpit rows group by the orchestrating chat, not by lane or by epic.

The cockpit already grouped rows by ``effective_epic`` under ``group_key``, and
the native app renders that field. Session grouping is therefore a SEPARATE
dimension (``session_group_key``) rather than a redefinition of ``group_key``:
the first test in this file pins the epic axis so the added dimension cannot
quietly move it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import cli, coord_db, ingest, native_cockpit
from coordharness.coord.config import connect


def _payload(
    work_id: str,
    *,
    actor: str = "claude",
    session_id: str = "",
    thread_id: str = "",
    worktree_id: str = "",
) -> dict[str, Any]:
    """A minimal cockpit row payload -- only the fields grouping reads."""
    return {
        "dedup_key": work_id,
        "coord_work_id": work_id,
        "owner": actor,
        "owner_session_actor": actor,
        "owner_session_id": session_id,
        "owner_session_label": "",
        "owner_conversation_title": "",
        "owner_external_thread_id": thread_id,
        "owner_worktree_id": worktree_id,
    }


def _session(
    session_id: str,
    *,
    actor: str = "claude",
    parent_session_id: str | None = None,
    thread_id: str = "",
    worktree_id: str = "",
    label: str = "",
    title: str = "",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "actor": actor,
        "parent_session_id": parent_session_id,
        "external_thread_id": thread_id,
        "worktree_id": worktree_id,
        "human_label": label,
        "conversation_title": title,
    }


def _keys(payloads: list[dict[str, Any]]) -> list[str]:
    return [str(p["session_group_key"]) for p in payloads]


# --------------------------------------------------------------------------
# Regression: the pre-existing epic/bucket axis is untouched.
# --------------------------------------------------------------------------


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database = tmp_path / "coord.db"
    monkeypatch.setattr(coord_db, "HARNESS_ROOT", database.parent)
    bootstrap_database(database)
    conn = connect(database)
    try:
        for work_id, module, domain in (
            ("GROUPING-EPIC-A", "coord", "coordination"),
            ("GROUPING-EPIC-B", "board", "board_surface"),
        ):
            coord_db.upsert_work(
                conn,
                work_id,
                title=f"fixture {work_id}",
                assignee="claude",
                assigned_by="claude",
                module=module,
                domain=domain,
                surface="task",
                done_signal=f"artifacts/{work_id.lower()}.json",
                acceptance_json=json.dumps(["proof exists"]),
                note="session grouping fixture",
                intent_state="queued",
            )
    finally:
        conn.close()
    return database


def test_epic_axis_is_unchanged_by_session_grouping(seeded_db: Path) -> None:
    """group_key still names the epic, and the group model still lists epics."""
    conn = connect(seeded_db)
    try:
        native_cockpit.refresh(conn, source_version="session-grouping-regression")
        rows = conn.execute(
            "SELECT group_key, group_label, effective_epic, bucket"
            " FROM native_cockpit_rows ORDER BY dedup_key"
        ).fetchall()
        groups = conn.execute(
            "SELECT group_key, label, count FROM native_cockpit_group_model"
            " ORDER BY sort_order"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "fixture produced no cockpit rows"
    for row in rows:
        # The contract the native app renders: group_key IS the epic key.
        assert row["group_key"] == row["effective_epic"]
        assert row["group_label"] == native_cockpit._humanize_epic_label(
            row["effective_epic"]
        )
        assert not row["group_key"].startswith(native_cockpit._SESSION_GROUP_PREFIX)

    group_keys = [g["group_key"] for g in groups]
    # The four status buckets are still the first four group-model entries.
    assert group_keys[:4] == ["running", "attention", "next", "done"]
    # ... and the epic keys still follow them.
    assert set(r["effective_epic"] for r in rows) <= set(group_keys)
    assert not any(
        key.startswith(native_cockpit._SESSION_GROUP_PREFIX) for key in group_keys
    )


def test_session_groups_are_published_on_their_own_table(seeded_db: Path) -> None:
    """The session axis lives beside the epic axis, never inside it."""
    conn = connect(seeded_db)
    try:
        native_cockpit.refresh(conn, source_version="session-grouping-table")
        rows = conn.execute(
            "SELECT session_group_key, session_group_source FROM native_cockpit_rows"
        ).fetchall()
        session_groups = conn.execute(
            "SELECT group_key, label, count FROM native_cockpit_session_group_model"
            " ORDER BY sort_order"
        ).fetchall()
    finally:
        conn.close()

    assert rows
    for row in rows:
        assert str(row["session_group_key"]).startswith(
            native_cockpit._SESSION_GROUP_PREFIX
        )
    assert session_groups
    assert sum(int(g["count"]) for g in session_groups) == len(rows)


# --------------------------------------------------------------------------
# The added dimension.
# --------------------------------------------------------------------------


def test_two_distinct_sessions_produce_two_groups() -> None:
    payloads = [
        _payload("W-1", session_id="claude:alpha"),
        _payload("W-2", session_id="claude:alpha"),
        _payload("W-3", session_id="claude:beta"),
        _payload("W-4", session_id="claude:beta"),
    ]
    sessions = [_session("claude:alpha"), _session("claude:beta")]

    native_cockpit._apply_session_grouping(payloads, sessions)

    keys = _keys(payloads)
    assert keys[0] == keys[1]
    assert keys[2] == keys[3]
    assert keys[0] != keys[2]
    assert len(set(keys)) == 2
    assert {p["session_group_source"] for p in payloads} == {"session"}


def test_one_chat_under_two_identities_is_one_group() -> None:
    """A raw host id and a namespaced id bridged by external_thread_id."""
    sessions = [
        _session("claude:host-uuid-1", thread_id="host-uuid-1", label="Chat one"),
        _session("claude:semantic-lane", thread_id="host-uuid-1"),
    ]
    payloads = [
        _payload("W-1", session_id="claude:host-uuid-1", thread_id="host-uuid-1"),
        _payload("W-2", session_id="claude:semantic-lane", thread_id="host-uuid-1"),
    ]

    native_cockpit._apply_session_grouping(payloads, sessions)

    keys = _keys(payloads)
    assert len(set(keys)) == 1, keys
    model = native_cockpit._session_group_model(payloads)
    assert len(model) == 1
    assert model[0]["count"] == 2
    assert model[0]["label"] == "Chat one"


def test_worktree_id_also_bridges_two_identities() -> None:
    sessions = [
        _session("codex:one", actor="codex", worktree_id="wt-7"),
        _session("codex:two", actor="codex", worktree_id="wt-7"),
    ]
    payloads = [
        _payload("W-1", actor="codex", session_id="codex:one", worktree_id="wt-7"),
        _payload("W-2", actor="codex", session_id="codex:two", worktree_id="wt-7"),
    ]

    native_cockpit._apply_session_grouping(payloads, sessions)

    assert len(set(_keys(payloads))) == 1


def test_bare_and_lane_namespaced_ids_are_the_same_chat() -> None:
    payloads = [
        _payload("W-1", session_id="claude:alpha"),
        _payload("W-2", session_id="alpha"),
    ]

    native_cockpit._apply_session_grouping(payloads, [])

    assert len(set(_keys(payloads))) == 1


def test_same_bare_id_under_two_lanes_stays_two_groups() -> None:
    """Lane is part of the identity: codex:x is not claude:x."""
    payloads = [
        _payload("W-1", actor="claude", session_id="claude:shared"),
        _payload("W-2", actor="codex", session_id="codex:shared"),
    ]

    native_cockpit._apply_session_grouping(payloads, [])

    assert len(set(_keys(payloads))) == 2


# --------------------------------------------------------------------------
# Subagents roll up; they never mint a top-level row.
# --------------------------------------------------------------------------


def test_subagent_claim_rolls_up_under_its_parent() -> None:
    sessions = [
        _session("claude:orchestrator", label="Orchestrating chat"),
        _session("claude:sub-1", parent_session_id="claude:orchestrator"),
    ]
    payloads = [
        _payload("W-PARENT", session_id="claude:orchestrator"),
        _payload("W-SUB", session_id="claude:sub-1"),
    ]

    native_cockpit._apply_session_grouping(payloads, sessions)

    keys = _keys(payloads)
    assert keys[0] == keys[1]
    assert payloads[1]["session_group_source"] == "parent_session"
    assert payloads[0]["session_group_label"] == "Orchestrating chat"
    assert payloads[1]["session_group_label"] == "Orchestrating chat"


def test_nested_subagents_roll_up_to_the_root_orchestrator() -> None:
    sessions = [
        _session("claude:root", label="Root chat"),
        _session("claude:mid", parent_session_id="claude:root"),
        _session("claude:leaf", parent_session_id="claude:mid"),
    ]
    payloads = [
        _payload("W-ROOT", session_id="claude:root"),
        _payload("W-MID", session_id="claude:mid"),
        _payload("W-LEAF", session_id="claude:leaf"),
    ]

    native_cockpit._apply_session_grouping(payloads, sessions)

    assert len(set(_keys(payloads))) == 1
    assert _keys(payloads)[0].endswith(":root")


def test_a_subagent_never_mints_its_own_top_level_group() -> None:
    """Nothing violates this today; the test keeps it that way."""
    sessions = [
        _session("claude:orchestrator"),
        _session("claude:sub-1", parent_session_id="claude:orchestrator"),
        _session("claude:sub-2", parent_session_id="claude:orchestrator"),
    ]
    payloads = [
        _payload("W-PARENT", session_id="claude:orchestrator"),
        _payload("W-SUB-1", session_id="claude:sub-1"),
        _payload("W-SUB-2", session_id="claude:sub-2"),
    ]

    native_cockpit._apply_session_grouping(payloads, sessions)

    model = native_cockpit._session_group_model(payloads)
    assert len(model) == 1
    assert model[0]["count"] == 3
    assert "sub-1" not in model[0]["group_key"]
    assert "sub-2" not in model[0]["group_key"]


def test_a_subagent_working_alone_still_shows_its_parent_chat() -> None:
    """The orchestrator has no claim of its own; the icon is still the chat."""
    sessions = [
        _session("claude:orchestrator", label="Orchestrating chat"),
        _session("claude:sub-1", parent_session_id="claude:orchestrator"),
    ]
    payloads = [_payload("W-SUB", session_id="claude:sub-1")]

    native_cockpit._apply_session_grouping(payloads, sessions)

    model = native_cockpit._session_group_model(payloads)
    assert len(model) == 1
    assert model[0]["group_key"].endswith(":orchestrator")
    assert model[0]["label"] == "Orchestrating chat"


# --------------------------------------------------------------------------
# Honest degradation when the bridge fields are absent.
# --------------------------------------------------------------------------


def test_absent_bridge_fields_degrade_to_session_id_alone() -> None:
    """No thread id and no registry: group by session id, never by lane."""
    payloads = [
        _payload("W-1", session_id="claude:alpha"),
        _payload("W-2", session_id="claude:beta"),
    ]

    native_cockpit._apply_session_grouping(payloads, [])

    keys = _keys(payloads)
    assert len(set(keys)) == 2, "same-lane sessions must not collapse into one icon"
    assert {p["session_group_source"] for p in payloads} == {"session"}


def test_empty_thread_ids_never_bridge_unrelated_chats() -> None:
    payloads = [
        _payload("W-1", session_id="claude:alpha", thread_id="", worktree_id=""),
        _payload("W-2", session_id="claude:beta", thread_id="", worktree_id=""),
    ]
    sessions = [
        _session("claude:alpha", thread_id="", worktree_id=""),
        _session("claude:beta", thread_id="", worktree_id=""),
    ]

    native_cockpit._apply_session_grouping(payloads, sessions)

    assert len(set(_keys(payloads))) == 2


def test_rows_with_no_session_identity_are_unowned_not_lane_grouped() -> None:
    payloads = [
        _payload("W-1", actor="claude"),
        _payload("W-2", actor="codex"),
    ]

    native_cockpit._apply_session_grouping(payloads, [])

    assert _keys(payloads) == [
        native_cockpit._SESSION_GROUP_UNOWNED,
        native_cockpit._SESSION_GROUP_UNOWNED,
    ]
    assert {p["session_group_source"] for p in payloads} == {"unowned"}


def test_grouping_is_stable_regardless_of_row_order() -> None:
    sessions = [
        _session("claude:host-uuid-1", thread_id="host-uuid-1"),
        _session("claude:semantic", thread_id="host-uuid-1"),
    ]
    forward = [
        _payload("W-1", session_id="claude:host-uuid-1", thread_id="host-uuid-1"),
        _payload("W-2", session_id="claude:semantic", thread_id="host-uuid-1"),
    ]
    reverse = [
        _payload("W-2", session_id="claude:semantic", thread_id="host-uuid-1"),
        _payload("W-1", session_id="claude:host-uuid-1", thread_id="host-uuid-1"),
    ]

    native_cockpit._apply_session_grouping(forward, sessions)
    native_cockpit._apply_session_grouping(reverse, list(reversed(sessions)))

    assert set(_keys(forward)) == set(_keys(reverse))


# --------------------------------------------------------------------------
# The default axis of the agent-oriented surfaces.
#
# The projection can resolve one group per orchestrating chat, but a stranger
# only benefits if the surfaces that are ABOUT agents open on that axis. These
# assertions read the shipped sources: the native app has no runnable test
# target in this repository (generating one writes untracked build state), so
# the alternative to a source assertion is no coverage at all.
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
MENUBAR_SOURCES = REPO_ROOT / "apps" / "menubar" / "Sources"


def _swift_source(*parts: str) -> str:
    return MENUBAR_SOURCES.joinpath(*parts).read_text(encoding="utf-8")


def _agent_session_key_body() -> str:
    text = _swift_source("UI", "ContentStackAndRows.swift")
    assert "func agentSessionKey" in text, "the agent grouping key was renamed"
    after = text.split("func agentSessionKey", 1)[1]
    return after.split("func placeAgentSessionGroups", 1)[0]


def test_menubar_agent_view_groups_by_the_resolved_chat_first() -> None:
    """The running-agents block keys on the projection's chat, by default."""
    body = _agent_session_key_body()
    assert "sessionGroupKey" in body, (
        "the agent view still derives its own key; only the projection can "
        "bridge a chat's identities and roll a subagent up under its parent"
    )
    assert body.index("sessionGroupKey") < body.index("ownerSessionId"), (
        "the locally derived key is consulted before the resolved one"
    )


def test_menubar_agent_view_degrades_when_the_projection_is_older() -> None:
    """An older projection has no session_group_key: fall back, never blank."""
    body = _agent_session_key_body()
    resolved_at = body.index("sessionGroupKey")
    for legacy in ("ownerSessionId", "ownerExternalThreadId", "ownerWorktreeId"):
        assert body.index(legacy) > resolved_at, f"{legacy} fallback was dropped"
    assert ":owner\"" in body, "the last-resort group key was dropped"


def test_menubar_row_carries_the_resolved_chat_from_the_projection() -> None:
    assert "var sessionGroupKey: String?" in _swift_source("Data", "Models.swift")
    client = _swift_source("Data", "HarnessClient.swift")
    assert "self.sessionGroupKey = row.sessionGroupKey" in client


def _saved_view_line(name: str) -> str:
    text = _swift_source("Cockpit", "Core", "CockpitSavedViews.swift")
    return next(line for line in text.splitlines() if f'name: "{name}"' in line)


def test_agent_saved_views_open_on_the_chat_axis() -> None:
    for name in ("Claude", "Codex"):
        assert "groupMode: .agentSession" in _saved_view_line(name), (
            f"the {name} view still opens grouped by the work hierarchy"
        )


def test_epic_grouping_stays_reachable_after_the_default_change() -> None:
    """The epic axis is a default elsewhere and a click away everywhere."""
    presentation = _swift_source(
        "Cockpit", "Presentation", "CockpitPresentationModel.swift"
    )
    assert "case smart" in presentation
    assert "var groupMode: CockpitGroupMode = .smart" in presentation
    root = _swift_source("Cockpit", "UI", "CockpitRootView.swift")
    assert "CockpitGroupMode.allCases" in root, "the group control lost its modes"
    for work_view in ("Now", "Nested", "Backlog"):
        assert "groupMode: .agentSession" not in _saved_view_line(work_view)
    assert "groupMode: .smart" in _saved_view_line("Now")
    # The menu bar's own section is still the work hierarchy.
    assert "self.section = row.groupKey" in _swift_source("Data", "HarnessClient.swift")


def test_every_projected_row_can_be_grouped_by_chat(seeded_db: Path) -> None:
    """The default axis never renders a row with no group to sit in."""
    conn = connect(seeded_db)
    try:
        native_cockpit.refresh(conn, source_version="session-grouping-default")
        rows = conn.execute(
            "SELECT session_group_key FROM native_cockpit_rows"
        ).fetchall()
    finally:
        conn.close()

    assert rows
    assert all(str(row["session_group_key"]).strip() for row in rows)


# --------------------------------------------------------------------------
# The worktree half of the bridge, on this harness's own registration path.
# --------------------------------------------------------------------------


def _claude_env(**extra: str) -> dict[str, str]:
    return {"CLAUDE_CODE_SESSION_ID": "host-uuid-1", **extra}


def test_a_linked_worktree_becomes_the_claude_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(
        ingest,
        "_git_worktree_dirs",
        lambda cwd: (os.path.join(cwd, ".git", "worktrees", "agent-a"),
                     os.path.join(cwd, ".git")),
    )

    ident = ingest.resolve_identity(_claude_env())

    assert str(ident["worktree_id"]).startswith("worktree:agent-a:")


def test_the_primary_worktree_leaves_the_bridge_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every chat in a checkout shares it, so it names a place, not a chat."""
    monkeypatch.chdir(tmp_path)
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(
        ingest, "_git_worktree_dirs", lambda cwd: (os.path.join(cwd, ".git"), ".git")
    )

    assert ingest.resolve_identity(_claude_env())["worktree_id"] is None


def test_the_bridge_is_never_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(
        ingest,
        "_git_worktree_dirs",
        lambda cwd: (os.path.join(cwd, ".git", "worktrees", "agent-a"),
                     os.path.join(cwd, ".git")),
    )

    worktree_id = str(ingest.resolve_identity(_claude_env())["worktree_id"])

    assert os.getcwd() not in worktree_id
    assert str(tmp_path) not in worktree_id
    assert os.sep not in worktree_id


def test_two_chats_in_one_checkout_do_not_share_a_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collapse this refuses to build: one cwd, two chats, one group."""
    monkeypatch.chdir(tmp_path)
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(
        ingest, "_git_worktree_dirs", lambda cwd: (os.path.join(cwd, ".git"), ".git")
    )

    first = ingest.resolve_identity(_claude_env(COORD_SESSION_ID="claude:one"))
    second = ingest.resolve_identity(_claude_env(COORD_SESSION_ID="claude:two"))
    payloads = [
        _payload("W-1", session_id=first["session_id"],
                 worktree_id=first["worktree_id"] or ""),
        _payload("W-2", session_id=second["session_id"],
                 worktree_id=second["worktree_id"] or ""),
    ]

    native_cockpit._apply_session_grouping(payloads, [])

    assert len(set(_keys(payloads))) == 2


def test_no_git_leaves_the_bridge_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(ingest, "_git_worktree_dirs", lambda cwd: None)

    assert ingest.resolve_identity(_claude_env())["worktree_id"] is None


def test_an_explicit_worktree_id_wins_over_the_derived_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(ingest, "_git_worktree_dirs", lambda cwd: None)

    ident = ingest.resolve_identity(_claude_env(COORD_WORKTREE_ID="wt-9"))

    assert ident["worktree_id"] == "wt-9"


def test_registration_writes_the_claude_worktree_bridge(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap this closes: the column was NULL on every Claude registration."""
    ingest._linked_worktree_id.cache_clear()
    monkeypatch.setattr(ingest, "_git_worktree_dirs", lambda cwd: None)
    ident = ingest.resolve_identity(_claude_env(COORD_WORKTREE_ID="wt-registered"))
    conn = connect(seeded_db)
    try:
        cli._register_identity_session(conn, ident)
        row = conn.execute(
            "SELECT worktree_id, external_thread_id FROM agent_sessions"
            " WHERE session_id=?",
            (str(ident["session_id"]),),
        ).fetchone()
    finally:
        conn.close()

    assert row["worktree_id"] == "wt-registered"
    assert row["external_thread_id"] == "host-uuid-1"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_real_git_tells_a_linked_worktree_from_the_primary_one(
    tmp_path: Path,
) -> None:
    """Proven against git itself, not against a stubbed answer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def _git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.name=coord", "-c", "user.email=coord@example.invalid",
             *args],
            cwd=str(cwd), env=env, check=True, capture_output=True, timeout=60,
        )

    _git("init", "-q", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "seed", cwd=repo)
    _git("worktree", "add", "-q", str(tmp_path / "agent-a"), "-b", "agent-a", cwd=repo)

    ingest._linked_worktree_id.cache_clear()
    assert ingest._linked_worktree_id(str(repo)) is None
    linked = ingest._linked_worktree_id(str(tmp_path / "agent-a"))
    assert linked is not None and linked.startswith("worktree:agent-a:")
    assert str(tmp_path) not in linked
