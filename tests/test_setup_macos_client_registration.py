"""`scripts/setup.sh` must not register MCP clients unless asked by name.

Before the setup-macos.sh -> setup.sh rename, `--register-clients` was the only
way to opt in on the CLI-only path, but the native path used to run
`coord onboard --register-clients` unconditionally -- so any run that installed
the native apps registered this clone with every installed Codex and Claude
client along the way. On the Codex side that writes the user's *global*
configuration -- outside the clone, and outside anything the flag advertised.
`scripts/setup.sh` now makes `--register-clients` opt-in on *every* path,
native included (see the script's own comment block), and this suite pins
that: the default run never registers clients, and `--register-clients` does
so explicitly, on the CLI-only lane exercised here (native is separately
opt-in via `--native` and untouched by this file).

The registration itself is never executed here: `coord` is a stub that records
its argv, so what the script asks for is asserted without the machine being
touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_STUB_COORD = """#!/bin/sh
printf '%s\\n' "$*" >> "$COORD_TEST_LOG"
exit 0
"""

_STUB_PYTHON = """#!/bin/sh
exit 0
"""

_STUB_UNAME = """#!/bin/sh
printf 'Darwin\\n'
"""


def _clone(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A checkout whose `.venv` is stubs, so the script talks to nothing real."""
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "setup.sh", clone / "scripts" / "setup.sh")
    (clone / "scripts" / "setup.sh").chmod(0o755)

    venv_bin = clone / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name, body in (("coord", _STUB_COORD), ("python", _STUB_PYTHON)):
        executable = venv_bin / name
        executable.write_text(body)
        executable.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text(_STUB_UNAME)
    uname.chmod(0o755)

    log = tmp_path / "coord-argv.log"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "COORD_PYTHON": str(venv_bin / "python"),
        "COORD_TEST_LOG": str(log),
    }
    return clone, log, env


def _run(clone: Path, env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "setup.sh"), *argv],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_default_path_does_not_register_clients(tmp_path: Path) -> None:
    """No flags at all: neither native install nor client registration is
    opt-in by default, so this is the direct successor of the old
    `--no-native`-only run."""
    clone, log, env = _clone(tmp_path)

    result = _run(clone, env)

    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "onboard" in invocations, invocations
    assert "--register-clients" not in invocations
    assert "no (pass --register-clients to register them)" in result.stdout


def test_registration_happens_when_it_is_asked_for_by_name(tmp_path: Path) -> None:
    clone, log, env = _clone(tmp_path)

    result = _run(clone, env, "--register-clients")

    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "--register-clients" in invocations
    assert "--write-configs" in invocations
    assert "yes (global Codex/Claude MCP config written)" in result.stdout


def test_the_advertised_flag_is_documented_where_the_path_is_described() -> None:
    """A flag nobody can discover is not an opt-in.

    `docs/getting-started.md` is the current canonical setup walkthrough and
    describes `scripts/setup.sh` (docs/getting-started.md is owned by another
    live lane; this asserts against its current content rather than editing
    it here).
    """
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")

    assert "./scripts/setup.sh --register-clients" in getting_started
    assert (
        "nothing is registered\nwith the Codex/Claude MCP clients on your machine"
        in getting_started
    )


def test_register_clients_flag_is_recognised_by_the_script() -> None:
    source = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    assert "--register-clients)" in source
