"""Installer coverage for the optional loopback usage-dashboard upstream."""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
import os
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "apps" / "install.sh"


def _extract_board_plist_heredoc() -> str:
    text = INSTALL_SH.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY\n", text, re.S)
    matches = [block for block in blocks if '"COORD_BOARD_URL"' in block]
    assert len(matches) == 1
    return matches[0]


def _generate_plist(tmp_path: Path, usage_url: str, schema: str) -> dict[str, object]:
    script = tmp_path / "generate_board_plist.py"
    script.write_text(_extract_board_plist_heredoc(), encoding="utf-8")
    plist_path = tmp_path / "org.coordharness.board.plist"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(plist_path),
            "org.coordharness.board",
            "/fake/venv/bin/coord-board",
            "/fake/coord.db",
            "/fake/runtime",
            "/fake/logs/stdout.log",
            "/fake/logs/stderr.log",
            "0",
            "/fake/operator-token",
            usage_url,
            schema,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with plist_path.open("rb") as handle:
        return plistlib.load(handle)


def test_usage_upstream_flags_are_documented() -> None:
    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0
    assert "--usage-dashboard-url" in result.stdout
    assert "--usage-upstream-schema" in result.stdout


def test_board_plist_persists_configured_usage_upstream(tmp_path: Path) -> None:
    payload = _generate_plist(
        tmp_path,
        "http://127.0.0.1:8780/api/usage/v1",
        "example.usage-intelligence.v1",
    )
    environment = payload["EnvironmentVariables"]
    assert environment["COORD_USAGE_DASHBOARD_URL"] == (
        "http://127.0.0.1:8780/api/usage/v1"
    )
    assert environment["COORD_USAGE_UPSTREAM_SCHEMA"] == (
        "example.usage-intelligence.v1"
    )


def test_board_plist_omits_usage_upstream_when_not_configured(tmp_path: Path) -> None:
    payload = _generate_plist(
        tmp_path,
        "",
        "coordharness.usage-intelligence.v1",
    )
    environment = payload["EnvironmentVariables"]
    assert "COORD_USAGE_DASHBOARD_URL" not in environment
    assert "COORD_USAGE_UPSTREAM_SCHEMA" not in environment


def test_installer_validates_upstream_with_proxy_policy() -> None:
    source = INSTALL_SH.read_text(encoding="utf-8")
    assert "validate_usage_dashboard_url(url)" in source
    assert 'r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}"' in source


def test_installer_allows_bounded_slow_database_startup() -> None:
    source = INSTALL_SH.read_text(encoding="utf-8")
    assert "for _attempt in {1..240}; do" in source
    assert 'curl --fail --silent --show-error --max-time 2 "$BOARD_URL/healthz"' in source


def test_reinstall_preserves_existing_native_companion_route(tmp_path: Path) -> None:
    source = INSTALL_SH.read_text(encoding="utf-8")
    snippet = source.split('echo "3/7  persist shared native configuration"', 1)[1]
    snippet = snippet.split('"$VENV/bin/python" - "$CONFIG_PATH"', 1)[0]
    shim = tmp_path / "defaults"
    shim.write_text(
        '#!/bin/bash\nfile="$STORE/$2.$3"\n'
        'if [[ "$1" == read ]]; then cat "$file"; else printf "%s" "$5" > "$file"; fi\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    domain = "org.coordharness.menubar"
    (tmp_path / f"{domain}.coordharness.baseURL").write_text("http://127.0.0.1:7871")
    (tmp_path / f"{domain}.coordharness.coordDBPath").write_text("/companion/coord.db")
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STORE": str(tmp_path),
        "DB_EXPLICIT": "0",
        "BOARD_URL": "http://127.0.0.1:7870",
        "DB_PATH": "/coord/coord.db",
    }
    result = subprocess.run(["bash", "-c", snippet], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"{domain}.coordharness.baseURL").read_text() == "http://127.0.0.1:7871"
    assert (tmp_path / f"{domain}.coordharness.coordDBPath").read_text() == "/companion/coord.db"
