"""Tests for apps/uninstall.sh.

Reviewer-disclosed gap: this script had zero test coverage. These pin (1)
that it is at least syntactically valid bash, and (2) that a real run against
a fake `HOME`/layout in a temp dir removes exactly what it claims -- the
board LaunchAgent plist, the optional reaper plist, the two `~/Applications`
bundles, the isolated runtime directory, and the two service log files --
while refusing cleanly (nonzero exit, nothing removed) when the plist at the
expected path carries a foreign `Label`.

Safety note on how the behavioral tests are isolated: `apps/uninstall.sh`
takes `--app-dir`/`$COORD_APP_DIR`, `$COORD_RUNTIME_ROOT`, `$COORD_LOG_DIR`,
and `$COORD_LAUNCH_AGENT_DIR` overrides for every path it touches by *path*,
which is what lets these tests point it at a throwaway `tmp_path` layout
instead of the real one. But its first four lines are not path-scoped at
all -- `osascript -e 'quit app "COORD"'`, `osascript -e 'quit app "COORD
Cockpit"'`, and two `pkill -x` calls act on *any* process with that name on
the real machine, and `launchctl bootout "gui/$UID/org.coordharness.board"`
acts on the real LaunchAgent by label regardless of which plist path was
passed. On the machine this repo is actually developed on, all four of those
are live: a real `COORD.app`, a real `COORD Cockpit.app`, and a real
`org.coordharness.board` LaunchAgent are running the actual board. Running
the unmodified script here would quit/kill/unload the real, live install as
a side effect of a test. So these tests prepend a small stub directory ahead
of `$PATH` supplying no-op `osascript`, `pkill`, `launchctl`, and `defaults`
executables -- the four commands the script invokes that are not scoped to
the fixture's paths -- while leaving every real path-scoped tool (`bash`,
`rm`, `dirname`, `basename`, `mkdir`, `tr`, and `/usr/libexec/PlistBuddy`,
which only ever touches the exact plist/bundle paths these tests create) to
run for real. That is what actually exercises the guard logic under test
(label matching, exact-path removal, refusal-on-mismatch) without any risk
to the developer's running instance.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "apps" / "uninstall.sh"

PLISTBUDDY = Path("/usr/libexec/PlistBuddy")
_PLISTBUDDY_AVAILABLE = PLISTBUDDY.exists()

_NOOP_STUB = "#!/bin/sh\nexit 0\n"
# Commands uninstall.sh invokes that are NOT scoped by the path overrides
# below -- they act on the real machine by app/service name. Stubbed out so
# the behavioral tests cannot touch the real running install.
_UNSCOPED_COMMANDS = ("osascript", "pkill", "launchctl", "defaults")


def test_uninstall_script_is_syntactically_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _write_plist(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/true"]})
    )


def _write_bundle(path: Path, bundle_id: str) -> None:
    contents = path / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id})
    )


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """A directory of no-op stubs for the commands uninstall.sh cannot be
    allowed to run for real in a test (see module docstring)."""
    stub_dir = tmp_path / "_stub_bin"
    stub_dir.mkdir()
    for name in _UNSCOPED_COMMANDS:
        stub = stub_dir / name
        stub.write_text(_NOOP_STUB)
        stub.chmod(0o755)
    return stub_dir


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "fake_home"
    app_dir = home / "Applications"
    runtime_root = home / "Library" / "Application Support" / "COORD"
    log_dir = home / "Library" / "Logs" / "COORD"
    launch_agent_dir = home / "Library" / "LaunchAgents"
    for d in (app_dir, runtime_root, log_dir, launch_agent_dir):
        d.mkdir(parents=True, exist_ok=True)

    _write_bundle(app_dir / "COORD.app", "org.coordharness.menubar")
    _write_bundle(app_dir / "COORD Cockpit.app", "org.coordharness.cockpit.window")

    (runtime_root / ".coord-install-marker").write_text("org.coordharness.board\n")
    (runtime_root / "venv-payload").write_text("pretend runtime bytes\n")

    (log_dir / "coord-board.stdout.log").write_text("stdout\n")
    (log_dir / "coord-board.stderr.log").write_text("stderr\n")

    _write_plist(
        launch_agent_dir / "org.coordharness.board.plist", "org.coordharness.board"
    )
    _write_plist(
        launch_agent_dir / "org.coordharness.reaper.plist", "org.coordharness.reaper"
    )

    return {
        "home": home,
        "app_dir": app_dir,
        "runtime_root": runtime_root,
        "log_dir": log_dir,
        "launch_agent_dir": launch_agent_dir,
    }


def _run_uninstall(
    layout: dict[str, Path], stub_bin: Path, *, coord_db: Path
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    env["HOME"] = str(layout["home"])
    env["COORD_APP_DIR"] = str(layout["app_dir"])
    env["COORD_RUNTIME_ROOT"] = str(layout["runtime_root"])
    env["COORD_LOG_DIR"] = str(layout["log_dir"])
    env["COORD_LAUNCH_AGENT_DIR"] = str(layout["launch_agent_dir"])
    # Always set: this is what skips the `defaults read` fallback branch
    # entirely, on top of `defaults` itself being stubbed above.
    env["COORD_DB"] = str(coord_db)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(
    not _PLISTBUDDY_AVAILABLE,
    reason="apps/uninstall.sh shells out to /usr/libexec/PlistBuddy, which only "
    "exists on macOS",
)
def test_uninstall_removes_exactly_what_it_claims(
    layout: dict[str, Path], stub_bin: Path, tmp_path: Path
) -> None:
    result = _run_uninstall(layout, stub_bin, coord_db=tmp_path / "unused-coord.db")
    assert result.returncode == 0, result.stderr

    assert not (layout["launch_agent_dir"] / "org.coordharness.board.plist").exists()
    assert not (layout["launch_agent_dir"] / "org.coordharness.reaper.plist").exists()
    assert not (layout["app_dir"] / "COORD.app").exists()
    assert not (layout["app_dir"] / "COORD Cockpit.app").exists()
    assert not layout["runtime_root"].exists()
    assert not (layout["log_dir"] / "coord-board.stdout.log").exists()
    assert not (layout["log_dir"] / "coord-board.stderr.log").exists()

    # Lifecycle data and user configuration are preserved intentionally --
    # the script must say so, not just silently leave them alone.
    assert "Preserved database" in result.stdout
    assert "Preserved state/config" in result.stdout


@pytest.mark.skipif(
    not _PLISTBUDDY_AVAILABLE,
    reason="apps/uninstall.sh shells out to /usr/libexec/PlistBuddy, which only "
    "exists on macOS",
)
def test_uninstall_removes_the_reaper_plist_only_when_present(
    layout: dict[str, Path], stub_bin: Path, tmp_path: Path
) -> None:
    """The reaper LaunchAgent is optional (only installed with
    --install-reaper-agent). Its absence must not be an error.
    """
    (layout["launch_agent_dir"] / "org.coordharness.reaper.plist").unlink()

    result = _run_uninstall(layout, stub_bin, coord_db=tmp_path / "unused-coord.db")
    assert result.returncode == 0, result.stderr
    assert not (layout["launch_agent_dir"] / "org.coordharness.board.plist").exists()


@pytest.mark.skipif(
    not _PLISTBUDDY_AVAILABLE,
    reason="apps/uninstall.sh shells out to /usr/libexec/PlistBuddy, which only "
    "exists on macOS",
)
def test_uninstall_refuses_a_foreign_board_plist_and_removes_nothing(
    layout: dict[str, Path], stub_bin: Path, tmp_path: Path
) -> None:
    # A plist at the expected filename but carrying a Label this script
    # never installed -- the guard exists so uninstall can never delete
    # someone else's LaunchAgent that happens to share the filename.
    _write_plist(
        layout["launch_agent_dir"] / "org.coordharness.board.plist",
        "com.example.unrelated",
    )

    result = _run_uninstall(layout, stub_bin, coord_db=tmp_path / "unused-coord.db")
    assert result.returncode != 0
    assert "refusing to remove foreign plist" in result.stderr

    # Nothing downstream of the failed guard ran.
    assert (layout["launch_agent_dir"] / "org.coordharness.board.plist").exists()
    assert (layout["launch_agent_dir"] / "org.coordharness.reaper.plist").exists()
    assert (layout["app_dir"] / "COORD.app").exists()
    assert (layout["app_dir"] / "COORD Cockpit.app").exists()
    assert layout["runtime_root"].exists()
    assert (layout["log_dir"] / "coord-board.stdout.log").exists()


@pytest.mark.skipif(
    not _PLISTBUDDY_AVAILABLE,
    reason="apps/uninstall.sh shells out to /usr/libexec/PlistBuddy, which only "
    "exists on macOS",
)
def test_uninstall_refuses_a_foreign_reaper_plist_and_removes_nothing_after_it(
    layout: dict[str, Path], stub_bin: Path, tmp_path: Path
) -> None:
    _write_plist(
        layout["launch_agent_dir"] / "org.coordharness.reaper.plist",
        "com.example.unrelated-reaper",
    )

    result = _run_uninstall(layout, stub_bin, coord_db=tmp_path / "unused-coord.db")
    assert result.returncode != 0
    assert "refusing to remove foreign plist" in result.stderr

    # The board plist runs first and is a real match, so it is legitimately
    # gone; the reaper mismatch must still stop everything after it.
    assert not (layout["launch_agent_dir"] / "org.coordharness.board.plist").exists()
    assert (layout["launch_agent_dir"] / "org.coordharness.reaper.plist").exists()
    assert (layout["app_dir"] / "COORD.app").exists()
    assert layout["runtime_root"].exists()


@pytest.mark.skipif(
    not _PLISTBUDDY_AVAILABLE,
    reason="apps/uninstall.sh shells out to /usr/libexec/PlistBuddy, which only "
    "exists on macOS",
)
def test_uninstall_refuses_an_unmarked_runtime_root(
    layout: dict[str, Path], stub_bin: Path, tmp_path: Path
) -> None:
    (layout["runtime_root"] / ".coord-install-marker").unlink()

    result = _run_uninstall(layout, stub_bin, coord_db=tmp_path / "unused-coord.db")
    assert result.returncode != 0
    assert "refusing to remove unmarked runtime" in result.stderr
    assert layout["runtime_root"].exists()
