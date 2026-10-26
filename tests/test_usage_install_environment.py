"""Execute the installer plist generator without starting any native service."""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "apps" / "install.sh"


def _generate(tmp_path: Path, *, optional_settings: bool) -> tuple[dict, Path, Path]:
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY\n", INSTALLER.read_text(), re.S)
    generators = [block for block in blocks if '"COORD_BOARD_URL"' in block]
    assert len(generators) == 1, "board plist generator must be unambiguous"
    home = tmp_path / "synthetic-home"
    home.mkdir()
    runtime = tmp_path / "synthetic-runtime"
    database = tmp_path / "synthetic-authority.sqlite"
    destination = tmp_path / "board.plist"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            generators[0],
            str(destination),
            "org.example.board",
            str(runtime / "venv/bin/coord-board"),
            str(database),
            str(runtime),
            str(tmp_path / "stdout.log"),
            str(tmp_path / "stderr.log"),
            "1" if optional_settings else "0",
            str(tmp_path / "operator-token"),
            "http://127.0.0.1:9876/api/usage/v1" if optional_settings else "",
            "example.usage.v1",
        ],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return plistlib.loads(destination.read_bytes()), home, database


@pytest.mark.parametrize("optional_settings", [False, True])
def test_board_plist_adds_provider_cli_path_and_preserves_configured_environment(
    tmp_path: Path, optional_settings: bool
) -> None:
    payload, home, database = _generate(tmp_path, optional_settings=optional_settings)
    environment = payload["EnvironmentVariables"]
    paths = environment["PATH"].split(":")
    assert paths[0] == str(home / ".local/bin")
    assert "/opt/homebrew/bin" in paths
    assert "/usr/bin" in paths
    assert len(paths) == len(set(paths))
    expected = {
        "COORD_DB": str(database),
        "COORD_BOARD_URL": "http://127.0.0.1:7870",
        "PATH": environment["PATH"],
    }
    if optional_settings:
        expected.update({
            "COORD_USAGE_DASHBOARD_URL": "http://127.0.0.1:9876/api/usage/v1",
            "COORD_USAGE_UPSTREAM_SCHEMA": "example.usage.v1",
            "COORD_NATIVE_OPERATOR_WRITES": "1",
            "COORD_NATIVE_OPERATOR_TOKEN_FILE": str(tmp_path / "operator-token"),
        })
    assert environment == expected
    assert payload["ProgramArguments"][2] == str(database)
    assert payload["WorkingDirectory"] == str(tmp_path / "synthetic-runtime")
    assert not (tmp_path / "board.plist.coord-install.tmp").exists()
