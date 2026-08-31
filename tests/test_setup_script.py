"""scripts/setup.sh: the OS-agnostic successor to scripts/setup-macos.sh.

Three things this suite pins:

- both scripts are syntactically valid bash;
- `--dry-run` prints its plan and creates nothing (no `.venv`, no
  `.coordharness/`, no subprocess actually run) regardless of which other
  flags are given;
- `scripts/setup-macos.sh` is a shim that execs `scripts/setup.sh`, forwarding
  argv untouched, rather than carrying its own copy of the setup logic.

Real installs (`.venv` creation, `pip install`, `coord onboard`, the native
`apps/install.sh` lane) are exercised manually, not here -- a live pip
install/`coord` invocation is unbounded network/subprocess work with no place
in a <=4-minute suite. `--dry-run` and `--check` are read-only by contract, so
asserting they touch nothing is the meaningful, fast thing to pin.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP_SH = ROOT / "scripts" / "setup.sh"
SETUP_MACOS_SH = ROOT / "scripts" / "setup-macos.sh"


def _bash_n(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_setup_sh_is_syntactically_valid_bash() -> None:
    result = _bash_n(SETUP_SH)
    assert result.returncode == 0, result.stderr


def test_setup_macos_sh_is_syntactically_valid_bash() -> None:
    result = _bash_n(SETUP_MACOS_SH)
    assert result.returncode == 0, result.stderr


def _empty_clone(tmp_path: Path) -> Path:
    """A bare clone containing only scripts/setup.sh -- enough for --dry-run,
    which by contract never touches apps/install.sh, coord, or python."""
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "setup.sh").write_text(SETUP_SH.read_text(encoding="utf-8"))
    (clone / "scripts" / "setup.sh").chmod(0o755)
    return clone


def _snapshot(clone: Path) -> set[str]:
    return {str(p.relative_to(clone)) for p in clone.rglob("*")}


def _run_dry(clone: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "setup.sh"), "--dry-run", *argv],
        cwd=clone,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_dry_run_prints_a_plan_and_exits_zero(tmp_path: Path) -> None:
    clone = _empty_clone(tmp_path)

    result = _run_dry(clone)

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert str(clone / ".venv") in result.stdout
    assert "clients NOT registered" in result.stdout
    assert "Native apps:" in result.stdout


def test_dry_run_creates_nothing(tmp_path: Path) -> None:
    clone = _empty_clone(tmp_path)
    before = _snapshot(clone)

    result = _run_dry(clone, "--native", "--register-clients")

    assert result.returncode == 0, result.stderr
    after = _snapshot(clone)
    assert after == before, f"dry-run created/modified paths: {after - before}"
    assert not (clone / ".venv").exists()
    assert not (clone / ".coordharness").exists()


def test_dry_run_register_clients_flag_is_reflected(tmp_path: Path) -> None:
    clone = _empty_clone(tmp_path)

    result = _run_dry(clone, "--register-clients")

    assert result.returncode == 0, result.stderr
    assert "registers this clone with installed Codex/Claude MCP clients" in result.stdout
    assert "clients NOT registered" not in result.stdout


def test_dry_run_native_flag_is_reflected(tmp_path: Path) -> None:
    clone = _empty_clone(tmp_path)

    result = _run_dry(clone, "--native")

    assert result.returncode == 0, result.stderr
    if platform.system() == "Darwin":
        assert "Would check for Xcode command-line tools and XcodeGen" in result.stdout
    else:
        assert "would be skipped (macOS only" in result.stdout


def test_check_reports_missing_install_without_creating_anything(tmp_path: Path) -> None:
    clone = _empty_clone(tmp_path)
    before = _snapshot(clone)

    result = subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "setup.sh"), "--check"],
        cwd=clone,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "venv: MISSING" in result.stdout
    assert "db:   MISSING" in result.stdout
    assert _snapshot(clone) == before


def test_shim_execs_setup_sh(tmp_path: Path) -> None:
    """The shim must hand off to setup.sh (and its argv), not reimplement it."""
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "setup-macos.sh").write_text(
        SETUP_MACOS_SH.read_text(encoding="utf-8")
    )
    (clone / "scripts" / "setup-macos.sh").chmod(0o755)
    # A stub setup.sh that proves it was reached, with the original argv intact.
    stub = "#!/usr/bin/env bash\nprintf 'STUB_SETUP_SH_REACHED:%s\\n' \"$*\"\n"
    (clone / "scripts" / "setup.sh").write_text(stub)
    (clone / "scripts" / "setup.sh").chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "setup-macos.sh"), "--dry-run", "--native"],
        cwd=clone,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "STUB_SETUP_SH_REACHED:--dry-run --native" in result.stdout


def test_shim_is_a_short_delegating_file() -> None:
    """Not a rewrite of the logic -- a thin exec, so the two never drift apart."""
    lines = [
        line
        for line in SETUP_MACOS_SH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(lines) <= 2, lines
    assert any("exec" in line and "setup.sh" in line for line in lines)


@pytest.mark.parametrize("flag", ["--native", "--register-clients", "--dry-run", "--check"])
def test_setup_sh_recognises_every_new_flag(flag: str) -> None:
    source = SETUP_SH.read_text(encoding="utf-8")
    assert f"{flag})" in source
