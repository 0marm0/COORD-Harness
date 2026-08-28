from __future__ import annotations

import json
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
    rendered = codex_config_text() + claude_config_text()
    assert "./.venv/bin/python" in rendered
    assert str(ROOT) not in rendered
    assert "COORD_DEPLOYMENT_PROFILE" in rendered


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
        "command_unavailable:.codex/config.toml",
        "command_unavailable:.mcp.json",
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
    setup = (ROOT / "scripts" / "setup-macos.sh").read_text()
    assert 'DB_PATH="$ROOT/.coordharness/coord.db"' in setup
    assert 'export COORD_DB="$DB_PATH"' in setup
    assert '"$ROOT/apps/install.sh" "$@" --db "$DB_PATH"' in setup
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
    assert "./scripts/setup-macos.sh" in docs
    assert "XcodeGen" in docs


def test_mac_setup_help_is_side_effect_free(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    scripts = clone / "scripts"
    apps = clone / "apps"
    scripts.mkdir(parents=True)
    apps.mkdir()
    shutil.copy2(ROOT / "scripts" / "setup-macos.sh", scripts / "setup-macos.sh")
    shutil.copy2(ROOT / "apps" / "install.sh", apps / "install.sh")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "python-was-called"
    tools = {
        "uname": "#!/bin/sh\nprintf 'Darwin\n'\n",
        "xcodebuild": "#!/bin/sh\nexit 0\n",
        "xcodegen": "#!/bin/sh\nexit 0\n",
        "python3": "#!/bin/sh\ntouch \"$COORD_MUTATION_MARKER\"\nexit 97\n",
    }
    for name, body in tools.items():
        executable = fake_bin / name
        executable.write_text(body)
        executable.chmod(0o755)

    home = tmp_path / "home"
    result = subprocess.run(
        ["/bin/bash", str(scripts / "setup-macos.sh"), "--help"],
        cwd=clone,
        env={
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "COORD_PYTHON": str(fake_bin / "python3"),
            "COORD_MUTATION_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: apps/install.sh" in result.stdout
    assert "--no-launch" in result.stdout
    assert result.stderr == ""
    assert not marker.exists()
    assert not (clone / ".venv").exists()
    assert not (clone / ".coordharness").exists()
    assert not home.exists()


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
