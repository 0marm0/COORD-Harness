"""A fresh clone's agent path must fail closed with an instruction, not an ENOENT.

Claude Code and Codex launch the checked-in `.mcp.json` / `.codex/config.toml`
server before either agent reads its own instructions file, so a first-contact
failure has to be legible on its own: `scripts/coord-mcp-launch.sh` execs the
clone's venv Python when it exists, and otherwise prints one stderr line naming
the fix (`./scripts/setup.sh`) and exits 1. These tests pin that shim's two
branches directly (subprocess smoke, no MCP client involved), pin that both
tracked configs actually invoke it, and pin that both agent entrypoints state
the step-0 recovery path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from coordharness.coord.onboarding import _check_configs, _resolve_server_command, write_portable_configs

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "coord-mcp-launch.sh"


def test_launcher_is_tracked_and_executable() -> None:
    assert LAUNCHER.is_file()
    assert os.access(LAUNCHER, os.X_OK)


def test_launcher_execs_venv_python_when_present(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    scripts = clone / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(LAUNCHER, scripts / "coord-mcp-launch.sh")
    (scripts / "coord-mcp-launch.sh").chmod(0o755)

    venv_python = clone / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        "#!/bin/sh\nprintf 'ran: %s\\n' \"$*\"\nprintf 'COORD_ACTOR=%s\\n' \"$COORD_ACTOR\"\n"
    )
    venv_python.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(scripts / "coord-mcp-launch.sh")],
        cwd=clone,
        env={"PATH": os.environ["PATH"], "COORD_ACTOR": "claude"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0
    assert "ran: -m coordharness.coord.mcp_coord_server" in result.stdout
    assert "COORD_ACTOR=claude" in result.stdout
    assert result.stderr == ""


def test_launcher_fails_closed_with_instruction_when_venv_absent(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    scripts = clone / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(LAUNCHER, scripts / "coord-mcp-launch.sh")
    (scripts / "coord-mcp-launch.sh").chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(scripts / "coord-mcp-launch.sh")],
        cwd=clone,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        "coordharness: this clone is not set up yet. Run ./scripts/setup.sh, "
        "then restart this session." in result.stderr
    )
    assert "ENOENT" not in result.stderr
    assert "Traceback" not in result.stderr


def test_tracked_mcp_configs_invoke_the_launcher() -> None:
    mcp_json = (REPO / ".mcp.json").read_text()
    codex_toml = (REPO / ".codex" / "config.toml").read_text()
    assert '"command": "./scripts/coord-mcp-launch.sh"' in mcp_json
    assert 'command = "./scripts/coord-mcp-launch.sh"' in codex_toml
    # The old direct-interpreter command must be gone from both, not merely
    # superseded -- a leftover entry would race the shim for which one launches.
    assert "./.venv/bin/python" not in mcp_json
    assert "./.venv/bin/python" not in codex_toml


def test_template_sources_mirror_the_tracked_configs() -> None:
    assert (REPO / ".codex" / "templates" / "claude-mcp.json").read_text() == (
        REPO / ".mcp.json"
    ).read_text()
    assert (REPO / ".codex" / "templates" / "codex-config.toml").read_text() == (
        REPO / ".codex" / "config.toml"
    ).read_text()


def test_agent_entrypoints_state_step_zero() -> None:
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (REPO / name).read_text()
        assert "./scripts/setup.sh" in text
        assert "scripts/coord-mcp-launch.sh" in text
        assert 'query_core.mode="generic_coord_db"' in text
        assert "coord doctor" in text
        assert "coord onboard" in text


def test_resolve_server_command_rejects_non_executable_shim(tmp_path: Path) -> None:
    """A shim that resolves and exists but lost its exec bit must fail resolution,
    not silently pass through to a launch that dies EACCES.
    """
    root = tmp_path / "clone"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shim = scripts / "coord-mcp-launch.sh"
    shim.write_text("#!/bin/bash\necho hi\n")
    shim.chmod(0o644)

    assert _resolve_server_command(root, "./scripts/coord-mcp-launch.sh") is None

    shim.chmod(0o755)
    resolved = _resolve_server_command(root, "./scripts/coord-mcp-launch.sh")
    assert resolved == shim.resolve()


def test_doctor_agent_configs_blocks_on_non_executable_shim(tmp_path: Path) -> None:
    """The onboarding doctor's agent-configs check must BLOCK with a clear problem
    code when the tracked launcher exists but lacks the executable bit -- the
    exact state that otherwise passes silently while every client launch dies
    EACCES at first contact.
    """
    root = tmp_path / "clone"
    root.mkdir()
    scripts = root / "scripts"
    scripts.mkdir()
    shim = scripts / "coord-mcp-launch.sh"
    shutil.copy2(LAUNCHER, shim)
    shim.chmod(0o755)
    runtime = root / ".venv" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o755)
    write_portable_configs(root)

    finding, _ = _check_configs(root)
    assert finding["status"] == "PASS"
    assert finding["details"]["problem_codes"] == []

    shim.chmod(0o644)
    finding, _ = _check_configs(root)
    assert finding["status"] == "BLOCKED"
    assert any(
        code.startswith("command_unavailable:") for code in finding["details"]["problem_codes"]
    )


def test_agent_entrypoints_cite_a_heading_not_a_drifting_line_number() -> None:
    # A bare `docs/agent-onboarding.md:140`-style citation goes stale the moment
    # that file gains or loses a line above it (it already has once); citing the
    # section heading instead survives edits elsewhere in the target doc.
    onboarding_headings = (REPO / "docs" / "agent-onboarding.md").read_text()
    assert "## 5. Use the bounded MCP reads" in onboarding_headings

    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (REPO / name).read_text()
        assert 'docs/agent-onboarding.md` §5, "Use the bounded MCP reads"' in text
        assert "docs/agent-onboarding.md:" not in text
