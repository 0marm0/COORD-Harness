"""scripts/setup.sh: the OS-agnostic successor to scripts/setup-macos.sh.

Four things this suite pins:

- both scripts are syntactically valid bash;
- `--dry-run` prints its plan and creates nothing (no `.venv`, no
  `.coordharness/`, no subprocess actually run) regardless of which other
  flags are given;
- `scripts/setup-macos.sh` is a shim that execs `scripts/setup.sh`, forwarding
  argv untouched, rather than carrying its own copy of the setup logic;
- the real-run path refuses early, and names both the interpreter it found
  and the one it needs, when `$COORD_PYTHON` resolves to a Python below this
  project's floor -- stock macOS ships `/usr/bin/python3` at 3.9, well below
  the `requires-python = ">=3.11"` in `pyproject.toml`, and a newcomer on that
  default gets a ~200-line pip resolver dump that never says the word
  "Python" if this guard is not in place.

Real installs (`.venv` creation, `pip install`, `coord onboard`, the native
`apps/install.sh` lane) are exercised manually, not here -- a live pip
install/`coord` invocation is unbounded network/subprocess work with no place
in a <=4-minute suite. `--dry-run` and `--check` are read-only by contract, so
asserting they touch nothing is the meaningful, fast thing to pin. The
version-guard tests below use a fake `python3` shell stub instead of a real
old interpreter, so they do not depend on any particular Python being
installed on the machine running the suite.
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


# --- Python-version guard (real-run path) ---
#
# Stock macOS ships `/usr/bin/python3` at 3.9.6, below this project's
# `requires-python = ">=3.11"`. Without a guard, setup.sh happily builds a
# 3.9 venv, `pip install -e .` then fails deep in dependency resolution with
# a message that never says "Python", and a second run reuses the same
# poisoned venv forever (the `if [[ ! -x "$VENV/bin/python" ]]` guard treats
# "exists" as "usable"). These tests use fake `python3` shell stubs rather
# than a real old interpreter, so they run the same everywhere.

_FAKE_PYPROJECT = '[project]\nname = "fake"\nrequires-python = ">=3.11"\n'


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _real_run_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "setup.sh").write_text(SETUP_SH.read_text(encoding="utf-8"))
    (clone / "scripts" / "setup.sh").chmod(0o755)
    (clone / "pyproject.toml").write_text(_FAKE_PYPROJECT, encoding="utf-8")
    return clone


def test_real_run_refuses_a_python_below_the_floor_and_names_it(tmp_path: Path) -> None:
    clone = _real_run_clone(tmp_path)
    old_python = clone / "fake-old-python3"
    _write_executable(
        old_python,
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-V" ]]; then echo "Python 3.9.6"; exit 0; fi\n'
        'if [[ "$1" == "-c" ]]; then exit 1; fi\n'  # below the floor: version check fails
        "exit 1\n",
    )

    result = subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "setup.sh")],
        cwd=clone,
        env={"PATH": "/usr/bin:/bin", "COORD_PYTHON": str(old_python)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "3.9.6" in combined, combined  # names the interpreter it found
    assert "3.11" in combined, combined  # names the interpreter it needs
    assert "COORD_PYTHON=" in combined, combined  # names the escape hatch
    assert not (clone / ".venv").exists()


def test_real_run_rebuilds_rather_than_reuses_a_too_old_venv(tmp_path: Path) -> None:
    """A `.venv` left behind by a too-old interpreter must not be reused

    forever: the guard has to detect it and rebuild, not just refuse to
    create a *new* one (the sticky-failure mode the pre-push review found).
    """
    clone = _real_run_clone(tmp_path)

    # A poisoned existing .venv: `bin/python` reports 3.9.6 and fails the
    # version check, same as a venv `python3 -m venv` built for real.
    old_marker = clone / ".venv" / "OLD_VENV_MARKER"
    old_marker.parent.mkdir(parents=True)
    old_marker.write_text("poisoned\n", encoding="utf-8")
    _write_executable(
        clone / ".venv" / "bin" / "python",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-V" ]]; then echo "Python 3.9.6"; exit 0; fi\n'
        'if [[ "$1" == "-c" ]]; then exit 1; fi\n'
        "exit 1\n",
    )

    # COORD_PYTHON itself is fine (>= the floor) and can "create" a venv by
    # writing a fresh `bin/python` stub that reports success up to the point
    # where setup.sh would run `pip install` -- which this test intentionally
    # never lets succeed (no network in this suite), catching that instead.
    good_python = clone / "fake-good-python3"
    _write_executable(
        good_python,
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-V" ]]; then echo "Python 3.14.0"; exit 0; fi\n'
        'if [[ "$1" == "-c" ]]; then exit 0; fi\n'
        'if [[ "$1" == "-m" && "$2" == "venv" ]]; then\n'
        '  mkdir -p "$3/bin"\n'
        '  cat > "$3/bin/python" <<INNER\n'
        "#!/usr/bin/env bash\n"
        'if [[ "\\$1" == "-m" ]]; then echo STUB_PIP_INSTALL_REACHED: "\\$@"; exit 42; fi\n'
        "exit 1\n"
        "INNER\n"
        '  chmod +x "$3/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )

    result = subprocess.run(
        ["/bin/bash", str(clone / "scripts" / "setup.sh")],
        cwd=clone,
        env={"PATH": "/usr/bin:/bin", "COORD_PYTHON": str(good_python)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert "rebuilding it" in combined, combined
    assert not old_marker.exists(), "poisoned .venv was reused instead of rebuilt"
    # Control flow reached the (stubbed) pip-install step, proving the venv
    # was actually rebuilt and used, not just deleted.
    assert "STUB_PIP_INSTALL_REACHED" in combined, combined
    assert result.returncode == 42, combined
