from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import subprocess

from coordharness.bootstrap import bootstrap_database
from coordharness.coord.cli import main as coord_main
from coordharness.coord.onboarding import (
    BLOCKED,
    PASS,
    claude_config_text,
    client_registration_command,
    register_clients,
    codex_config_text,
    run_onboarding_doctor,
    write_portable_configs,
)
from coordharness.coord.config import connect


ROOT = Path(__file__).resolve().parents[1]


def test_tracked_configs_are_portable_generator_outputs() -> None:
    assert (ROOT / ".codex" / "config.toml").read_text() == codex_config_text()
    assert (ROOT / ".mcp.json").read_text() == claude_config_text()
    assert (ROOT / ".codex" / "templates" / "codex-config.toml").read_text() == codex_config_text()
    assert (ROOT / ".codex" / "templates" / "claude-mcp.json").read_text() == claude_config_text()
    rendered = codex_config_text() + claude_config_text()
    assert "./scripts/coord-mcp-launch.sh" in rendered
    assert str(ROOT) not in rendered
    assert "COORD_DEPLOYMENT_PROFILE" in rendered
    launcher = (ROOT / "scripts" / "coord-mcp-launch.sh").read_text()
    assert "./.venv/bin/python" in launcher
    assert "coordharness.coord.mcp_coord_server" in launcher
    assert os.access(ROOT / "scripts" / "coord-mcp-launch.sh", os.X_OK)


def test_config_writer_creates_only_missing_files(tmp_path: Path) -> None:
    first = write_portable_configs(tmp_path)
    second = write_portable_configs(tmp_path)
    assert first == {
        "created": [".codex/config.toml", ".mcp.json"],
        "unchanged": [],
        "conflicts": [],
        "ok": True,
    }
    assert second["created"] == []
    assert second["unchanged"] == [".codex/config.toml", ".mcp.json"]
    (tmp_path / ".mcp.json").write_text("{}\n")
    conflict = write_portable_configs(tmp_path)
    assert conflict["ok"] is False
    assert conflict["conflicts"] == [".mcp.json"]


def _materialize_ready_clone(root: Path, *, include_runtime: bool = True) -> None:
    root.mkdir()
    for name in ("AGENTS.md", "CLAUDE.md"):
        shutil.copy2(ROOT / name, root / name)
    for client in (".agents", ".claude"):
        shutil.copytree(ROOT / client, root / client)
    (root / "scripts").mkdir()
    shim = root / "scripts" / "coord-mcp-launch.sh"
    shutil.copy2(ROOT / "scripts" / "coord-mcp-launch.sh", shim)
    shim.chmod(0o755)
    docs = root / "docs"
    docs.mkdir()
    for name in (
        "agent-onboarding.md",
        "agent-protocol.md",
        "context-architecture.md",
        "context-and-memory.md",
        "jobs-and-runs.md",
    ):
        shutil.copy2(ROOT / "docs" / name, docs / name)
    write_portable_configs(root)
    if include_runtime:
        runtime = root / ".venv" / "bin" / "python"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("#!/bin/sh\nexit 0\n")
        runtime.chmod(0o755)


def test_onboarding_doctor_checks_ready_clone_without_optional_probes(tmp_path: Path) -> None:
    project = tmp_path / "clone"
    _materialize_ready_clone(project)
    db = project / ".coordharness" / "coord.db"
    bootstrap_database(db)
    report = run_onboarding_doctor(
        project_root=project,
        db_path=db,
        probe_clients=False,
        probe_mcp=False,
    )
    assert report["status"] == PASS
    findings = {item["id"]: item for item in report["findings"]}
    assert findings["onboarding.instructions_skills"]["status"] == PASS
    assert findings["onboarding.agent_configs"]["status"] == PASS
    assert findings["onboarding.coord_db"]["status"] == PASS


def test_onboarding_doctor_blocks_before_runtime_bootstrap(tmp_path: Path) -> None:
    project = tmp_path / "clone"
    _materialize_ready_clone(project, include_runtime=False)
    db = project / ".coordharness" / "coord.db"
    bootstrap_database(db)
    report = run_onboarding_doctor(
        project_root=project,
        db_path=db,
        probe_clients=False,
        probe_mcp=False,
    )
    assert report["status"] == BLOCKED
    finding = next(item for item in report["findings"] if item["id"] == "onboarding.agent_configs")
    assert finding["details"]["problem_codes"] == [
        "runtime_missing:.codex/config.toml",
        "runtime_missing:.mcp.json",
    ]


def test_onboarding_doctor_blocks_missing_instruction_root(tmp_path: Path) -> None:
    db = tmp_path / ".coordharness" / "coord.db"
    bootstrap_database(db)
    report = run_onboarding_doctor(
        project_root=tmp_path,
        db_path=db,
        probe_clients=False,
        probe_mcp=False,
    )
    assert report["status"] == BLOCKED


def test_mac_setup_and_docs_share_clone_authority_and_port() -> None:
    # scripts/setup-macos.sh is now a thin shim (see test_setup_script.py); the
    # venv/db/config + native-app logic this test pins lives in scripts/setup.sh.
    setup = (ROOT / "scripts" / "setup.sh").read_text()
    assert 'DB_PATH="$ROOT/.coordharness/coord.db"' in setup
    assert 'export COORD_DB="$DB_PATH"' in setup
    assert '"$ROOT/apps/install.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}" --db "$DB_PATH"' in setup
    assert "command -v xcodebuild" in setup
    assert "xcodebuild -version" in setup
    assert "command -v xcodegen" in setup

    configs = (ROOT / ".codex" / "config.toml").read_text() + (ROOT / ".mcp.json").read_text()
    assert configs.count('COORD_DB = ".coordharness/coord.db"') == 1
    assert configs.count('"COORD_DB": ".coordharness/coord.db"') == 1

    docs = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "AGENTS.md",
            "CLAUDE.md",
            "docs/agent-onboarding.md",
            "docs/getting-started.md",
            "docs/standalone-setup.md",
        )
    )
    assert "7871" not in docs
    assert "http://127.0.0.1:7870" in docs
    # ./scripts/setup.sh is the current canonical reference (docs/getting-started.md,
    # docs/agent-onboarding.md, AGENTS.md, CLAUDE.md already use it). The old name still
    # appears too, since scripts/setup-macos.sh remains a working shim to it
    # (docs/standalone-setup.md links that path deliberately).
    assert "./scripts/setup.sh" in docs
    assert "XcodeGen" in docs


def test_setup_sh_help_is_side_effect_free(tmp_path: Path) -> None:
    """`--help` must exit before the venv/pip/coord lane runs -- no python
    invocation, no `.venv`, no `.coordharness/`."""
    clone = tmp_path / "clone"
    scripts = clone / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "setup.sh", scripts / "setup.sh")
    (scripts / "setup.sh").chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "python-was-called"
    python3 = fake_bin / "python3"
    python3.write_text("#!/bin/sh\ntouch \"$COORD_MUTATION_MARKER\"\nexit 97\n")
    python3.chmod(0o755)

    home = tmp_path / "home"
    result = subprocess.run(
        ["/bin/bash", str(scripts / "setup.sh"), "--help"],
        cwd=clone,
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "COORD_PYTHON": str(python3),
            "COORD_MUTATION_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: scripts/setup.sh" in result.stdout
    assert "--native" in result.stdout
    assert "--register-clients" in result.stdout
    assert result.stderr == ""
    assert not marker.exists()
    assert not (clone / ".venv").exists()
    assert not (clone / ".coordharness").exists()
    assert not home.exists()


def test_setup_sh_native_help_is_side_effect_free(tmp_path: Path) -> None:
    """`--native --help` must stay side-effect free even though `--native` is
    not the last flag -- this is the closest non-destructive probe a stranger
    without Xcode/XcodeGen is expected to run, and it must never fall through
    into the mutating setup path (venv creation, pip install, the
    Xcode/XcodeGen checks, or apps/install.sh)."""
    clone = tmp_path / "clone"
    scripts = clone / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "setup.sh", scripts / "setup.sh")
    (scripts / "setup.sh").chmod(0o755)
    # No apps/ dir, no xcodebuild/xcodegen on PATH at all: --native --help must
    # never need them.

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "python-was-called"
    python3 = fake_bin / "python3"
    python3.write_text("#!/bin/sh\ntouch \"$COORD_MUTATION_MARKER\"\nexit 97\n")
    python3.chmod(0o755)

    home = tmp_path / "home"
    result = subprocess.run(
        ["/bin/bash", str(scripts / "setup.sh"), "--native", "--help"],
        cwd=clone,
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "COORD_PYTHON": str(python3),
            "COORD_MUTATION_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: scripts/setup.sh" in result.stdout
    assert result.stderr == ""
    assert not marker.exists()
    assert not (clone / ".venv").exists()
    assert not (clone / ".coordharness").exists()
    assert not home.exists()


def test_demo_seed_cannot_commit_in_enclosing_repository(tmp_path: Path) -> None:
    """Regression guard for a live incident: var/demo is a plain subdirectory of
    the repository that contains scripts/demo.sh, not a separate clone. A prior
    version used `git -C "$DEMO" rev-parse --git-dir`, which succeeds by
    searching upward and finding the ENCLOSING repository's own .git -- so
    var/demo/.git was never created, and the seed step's `git add -A` /
    `git commit` (guarded only loosely by a `cd`) ran against the real
    repository, sweeping in whatever else was staged there. This proves the
    current script cannot move the enclosing repository's HEAD or touch what
    was already staged there, using exactly that nested layout."""
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "demo.sh", clone / "scripts" / "demo.sh")
    (clone / "scripts" / "demo.sh").chmod(0o755)

    git_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "git-home")}
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=clone, env=git_env, check=True,
        capture_output=True, text=True,
    )
    run("init", "-q")
    run("config", "user.name", "t")
    run("config", "user.email", "t@example.invalid")
    (clone / "README.md").write_text("hello\n")
    run("add", "README.md")
    run("commit", "-qm", "initial")

    # A staged-but-uncommitted change, standing in for another agent's live
    # work-in-progress elsewhere in the same enclosing repository.
    (clone / "dirty.txt").write_text("wip\n")
    run("add", "dirty.txt")

    before_head = run("rev-parse", "HEAD").stdout.strip()
    before_status = set(run("status", "--porcelain").stdout.splitlines())

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python_stub = fake_bin / "python3"
    python_stub.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$COORD_PROJECT_ROOT/.coordharness"\n'
        'touch "$COORD_PROJECT_ROOT/.coordharness/coord.db"\n'
        "exit 0\n"
    )
    python_stub.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "demo.sh")],
        cwd=clone,
        env={"HOME": str(tmp_path / "home"), "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    after_head = run("rev-parse", "HEAD").stdout.strip()
    after_status = set(run("status", "--porcelain").stdout.splitlines())

    assert after_head == before_head, "demo.sh must never move the enclosing repository's HEAD"
    assert before_status <= after_status, (
        "demo.sh must never alter what was already staged in the enclosing repository"
    )
    assert (clone / "var" / "demo" / ".git").is_dir(), (
        "the demo board must get its own independent repository"
    )


def test_client_registration_commands_are_absolute_and_client_specific(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "coordharness.coord.onboarding.shutil.which",
        lambda client: f"/opt/bin/{client}",
    )
    codex = client_registration_command(ROOT, "codex")
    claude = client_registration_command(ROOT, "claude")
    runtime = str(ROOT / ".venv" / "bin" / "python")

    assert codex[:4] == ["/opt/bin/codex", "mcp", "add", "coordharness"]
    assert claude[:8] == [
        "/opt/bin/claude",
        "mcp",
        "add",
        "--scope",
        "project",
        "--transport",
        "stdio",
        "coordharness",
    ]
    assert runtime in codex
    assert runtime in claude
    assert f"COORD_PROJECT_ROOT={ROOT}" in codex
    assert "COORD_ACTOR=claude" in claude


def test_register_clients_is_idempotent_when_entries_exist(monkeypatch) -> None:
    monkeypatch.setattr(
        "coordharness.coord.onboarding.shutil.which",
        lambda client: f"/opt/bin/{client}",
    )
    monkeypatch.setattr(
        "coordharness.coord.onboarding._client_get",
        lambda root, client: (
            subprocess.CompletedProcess([client], 0, "coordharness", ""),
            "coordharness",
        ),
    )

    def unexpected_run(*args, **kwargs):
        raise AssertionError("existing registration must not be replaced")

    monkeypatch.setattr(
        "coordharness.coord.onboarding.subprocess.run",
        unexpected_run,
    )
    report = register_clients(ROOT)
    assert report["ok"] is True
    assert report["changed"] is False
    assert [item["summary"] for item in report["clients"]] == [
        "registration already present",
        "registration already present",
    ]


def test_cli_typed_handoff_uses_work_context_fences(monkeypatch, capsys, tmp_path: Path) -> None:
    db = tmp_path / ".coordharness" / "coord.db"
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_DB", str(db))
    monkeypatch.setenv("COORD_ACTOR", "codex")
    monkeypatch.setenv("COORD_SESSION_ID", "codex:onboarding-test")

    assert (
        coord_main(
            [
                "--db",
                str(db),
                "create",
                "DEMO-CDX-GENERIC",
                "--title",
                "Generic work",
                "--module",
                "harness",
                "--done-signal",
                "reports/generic.md",
                "--acceptance",
                "typed transfer succeeds",
                "--note",
                "clean room",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert coord_main(["--db", str(db), "work-context", "DEMO-CDX-GENERIC"]) == 0
    context = json.loads(capsys.readouterr().out)
    fences = context["handoff_preconditions"]

    assert (
        coord_main(
            [
                "--db",
                str(db),
                "handoff",
                "DEMO-CDX-GENERIC",
                "--owner-lane",
                "claude",
                "--task",
                "finish generic work",
                "--why",
                "Claude owns the receiver slice",
                "--acceptance",
                "reports/generic.md satisfies acceptance",
                "--operation-id",
                "handoff-generic-0001",
                "--expected-version",
                str(fences["expected_version"]),
                "--expected-assignee",
                fences["expected_assignee"],
                "--ref",
                "docs/agent-onboarding.md",
                "--constraint",
                "preserve coord.db authority",
            ]
        )
        == 0
    )
    handoff = json.loads(capsys.readouterr().out)
    assert handoff["ok"] is True
    assert handoff["owner_lane"] == "claude"
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT assignee FROM work_items WHERE work_id=?",
            ("DEMO-CDX-GENERIC",),
        ).fetchone()
        assert row["assignee"] == "claude"
        assert (
            conn.execute(
                "SELECT kind FROM events WHERE event_id=?", (handoff["event_id"],)
            ).fetchone()["kind"]
            == "handoff"
        )
    finally:
        conn.close()


def test_cli_reassign_snapshots_fences_and_uses_typed_handoff(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    db = tmp_path / ".coordharness" / "coord.db"
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_DB", str(db))
    monkeypatch.setenv("COORD_ACTOR", "codex")
    monkeypatch.setenv("COORD_SESSION_ID", "codex:reassign-test")

    assert coord_main([
        "--db", str(db), "create", "DEMO-CDX-REASSIGN",
        "--title", "Reassign this work",
        "--module", "harness",
        "--done-signal", "reports/reassign.md",
        "--acceptance", "typed transfer succeeds",
        "--note", "exercise the safe convenience command",
    ]) == 0
    capsys.readouterr()

    assert coord_main([
        "--db", str(db), "reassign", "DEMO-CDX-REASSIGN",
        "--owner-lane", "claude",
        "--task", "finish reassigned work",
        "--why", "Claude has the relevant context",
        "--acceptance", "reports/reassign.md exists",
        "--ref", "docs/agent-protocol.md",
        "--constraint", "preserve coord.db authority",
    ]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True
    assert receipt["fresh_snapshot"] is True
    assert receipt["owner_lane"] == "claude"

    conn = connect(db)
    try:
        work = conn.execute(
            "SELECT assignee, assigned_by FROM work_items WHERE work_id=?",
            ("DEMO-CDX-REASSIGN",),
        ).fetchone()
        assert dict(work) == {"assignee": "claude", "assigned_by": "codex"}
        event = conn.execute(
            "SELECT actor, kind, trust FROM events WHERE event_id=?",
            (receipt["event_id"],),
        ).fetchone()
        assert dict(event) == {"actor": "codex", "kind": "handoff", "trust": "agent"}
    finally:
        conn.close()
