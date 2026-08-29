"""The scheduling story for the reaper: opt-in, and correctly shaped.

`apps/install.sh` already has a working pattern for turning a Python
executable into a `launchd` LaunchAgent -- it does exactly that for
`coord-board` (a persistent HTTP service: `RunAtLoad` + `KeepAlive`). The
reaper is a run-to-completion batch job, not a service, so it needs the other
launchd primitive (`RunAtLoad` + `StartInterval`) or `KeepAlive` would treat
every clean exit as a crash and respawn it in a tight loop. This test proves
the template `apps/install.sh` generates uses the right one, without
installing anything: it extracts the embedded Python plist-generation script
and runs it standalone, the same way `install.sh` would invoke it with
`"$VENV/bin/python" - args <<'PY'`.

It also proves the feature is opt-in, per this change's explicit constraint:
`--install-reaper-agent` must never fire as a side effect of a plain
`apps/install.sh --help` or `apps/install.sh` (no such flag) invocation, and
must never write into a real `~/Library/LaunchAgents` from a test -- which is
why this test never runs `apps/install.sh` itself with `--install-reaper-agent`
(that requires xcodegen/xcodebuild/codesign, which this suite does not assume
are present, and would `launchctl bootstrap` for real). It tests the plist
template in isolation instead, and separately proves the full script's
`--help` path is unaffected by the new flags' mere presence.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "apps" / "install.sh"


def _extract_reaper_plist_heredoc() -> str:
    """Pull out the one Python heredoc that builds the reaper's plist.

    `install.sh` has three `<<'PY'` heredocs (a JSON config rewrite, the
    `coord-board` plist, and this one); selecting by content
    (``StartInterval`` appears in none of the others) is more robust against
    unrelated edits shifting line numbers than hardcoding an offset.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY\n", text, re.S)
    matches = [block for block in blocks if "StartInterval" in block]
    assert len(matches) == 1, (
        f"expected exactly one StartInterval-bearing heredoc in {INSTALL_SH}, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_install_sh_never_schedules_keepalive_for_a_batch_job() -> None:
    """Guard against the easy mistake: copying the board's KeepAlive service
    pattern wholesale for a job that is supposed to exit, not stay up.

    The template's comment explains *why* KeepAlive is the wrong choice here,
    so it legitimately contains the word -- checking for the literal
    dict-key spelling (as the board's own plist-builder writes it,
    ``"KeepAlive":``) is what actually distinguishes prose from a payload key.
    """
    snippet = _extract_reaper_plist_heredoc()
    assert '"KeepAlive":' not in snippet
    assert "StartInterval" in snippet
    assert "RunAtLoad" in snippet


def test_reaper_plist_template_produces_a_valid_periodic_agent(tmp_path: Path) -> None:
    snippet = _extract_reaper_plist_heredoc()
    script = tmp_path / "generate_plist.py"
    script.write_text(snippet, encoding="utf-8")

    plist_path = tmp_path / "org.coordharness.reaper.plist"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(plist_path),
            "org.coordharness.reaper",
            "/fake/venv/bin/coord-reaper",
            "/fake/coord.db",
            "/fake/runtime",
            "300",
            "/fake/logs/coord-reaper.stdout.log",
            "/fake/logs/coord-reaper.stderr.log",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == "org.coordharness.reaper"
    assert payload["ProgramArguments"] == [
        "/fake/venv/bin/coord-reaper",
        "--db",
        "/fake/coord.db",
    ]
    assert payload["EnvironmentVariables"]["COORD_DB"] == "/fake/coord.db"
    assert payload["WorkingDirectory"] == "/fake/runtime"
    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 300
    assert "KeepAlive" not in payload
    assert payload["StandardOutPath"] == "/fake/logs/coord-reaper.stdout.log"
    assert payload["StandardErrorPath"] == "/fake/logs/coord-reaper.stderr.log"


def test_usage_documents_the_opt_in_flag_and_removal_steps() -> None:
    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0
    assert "--install-reaper-agent" in result.stdout
    assert "--reaper-interval" in result.stdout
    assert "launchctl bootout" in result.stdout
    assert "org.coordharness.reaper" in result.stdout
    # Still documents the pre-existing flag -- a regression here would mean
    # the usage rewrite silently dropped something a stranger already relies on.
    assert "--no-launch" in result.stdout


@pytest.mark.parametrize(
    "extra_args",
    [
        [],
        ["--install-reaper-agent"],
        ["--install-reaper-agent", "--reaper-interval", "60"],
    ],
)
def test_help_stays_side_effect_free_regardless_of_reaper_flags(
    tmp_path: Path, extra_args: list[str]
) -> None:
    """`--help` must exit before doing anything, no matter what precedes it.

    This does not exercise the full installer (that needs xcodegen/xcodebuild,
    which this suite does not assume are present) -- it only proves the
    argument-parsing loop still reaches the `-h|--help` case and exits 0
    without creating any of the directories the real install path would.
    """
    home = tmp_path / "home"
    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SH), *extra_args, "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert not home.exists(), "--help must never create anything under $HOME"


def test_reaper_interval_is_validated_before_any_installation_step(
    tmp_path: Path,
) -> None:
    """A garbage interval must fail fast and in plain language -- before
    xcodegen/xcodebuild/launchctl ever run -- not surface as an opaque
    launchd error after the rest of the install already happened.

    Path resolution for the *other* installer options (``--db``,
    ``--app-dir``, ...) already creates their parent directories under
    ``$HOME`` earlier in the script, ahead of every argument-validity check
    including the pre-existing ones -- so this does not assert `$HOME` stays
    untouched (it doesn't, even for `install.sh`'s own prior checks); it
    asserts the failure is immediate, named, and never reaches
    xcodegen/xcodebuild/launchctl.
    """
    home = tmp_path / "home"
    result = subprocess.run(
        [
            "/bin/bash", str(INSTALL_SH),
            "--install-reaper-agent", "--reaper-interval", "not-a-number",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "reaper-interval" in result.stderr.lower()
    assert "xcodegen" not in result.stdout.lower()
    assert "launchctl" not in result.stdout.lower()
