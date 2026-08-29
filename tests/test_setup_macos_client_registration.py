"""`--no-native` must not register MCP clients on the machine.

`scripts/setup-macos.sh` ran `coord onboard --register-clients` before the
`--no-native` early exit, so the lightweight CLI-only path registered this clone
with every installed Codex and Claude client. On the Codex side that writes the
user's *global* configuration -- outside the clone, and outside anything the flag
advertises.

The registration itself is never executed here: `coord` is a stub that records
its argv, so what the script asks for is asserted without the machine being
touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

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
    (clone / "apps").mkdir()
    shutil.copy2(ROOT / "scripts" / "setup-macos.sh", clone / "scripts" / "setup-macos.sh")
    shutil.copy2(ROOT / "apps" / "install.sh", clone / "apps" / "install.sh")

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
        ["/bin/bash", str(clone / "scripts" / "setup-macos.sh"), *argv],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_cli_only_path_does_not_register_clients(tmp_path: Path) -> None:
    clone, log, env = _clone(tmp_path)

    result = _run(clone, env, "--no-native")

    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "onboard" in invocations, invocations
    assert "--register-clients" not in invocations
    assert "not registered" in result.stdout


def test_registration_happens_when_it_is_asked_for_by_name(tmp_path: Path) -> None:
    clone, log, env = _clone(tmp_path)

    result = _run(clone, env, "--no-native", "--register-clients")

    assert result.returncode == 0, result.stderr
    invocations = log.read_text()
    assert "--register-clients" in invocations
    assert "--write-configs" in invocations


def test_the_advertised_flag_is_documented_where_the_path_is_described() -> None:
    """A flag nobody can discover is not an opt-in, and the old sentence was false.

    `docs/getting-started.md` described `--no-native` as doing "the same setup as
    the manual CLI steps", which never registered a client.
    """
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")

    assert "./scripts/setup-macos.sh --no-native --register-clients" in getting_started
    assert "the same `.venv` and\n`.coordharness/coord.db` setup as the manual CLI steps above, in one command:" not in (
        getting_started
    )


@pytest.mark.parametrize("flag", ["--no-native", "--register-clients"])
def test_both_flags_are_recognised_by_the_script(flag: str) -> None:
    source = (ROOT / "scripts" / "setup-macos.sh").read_text(encoding="utf-8")

    assert f"{flag})" in source
