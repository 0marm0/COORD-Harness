"""What a clone actually gets when it runs the shell scripts the docs name.

Two failures, both invisible from inside a working checkout:

  * ``set_mode.sh`` and ``mem_governor.sh`` were recorded in the index as
    ``100644`` with no shebang, so every clone got ``Permission denied`` from
    commands the operator's handbook documents as verified end to end. The mode
    that matters is the one in Git's index -- a local ``chmod`` is not published.
  * ``mem_free_gb`` read three Darwin-only binaries with their errors discarded,
    and its awk fallback still printed ``%.1f`` of unset variables. On any other
    machine it returned a confident ``0.0``, and ``mem_governor.sh wait`` held
    forever against a number nobody had measured.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

# The scripts run themselves. lib_safety.sh is sourced, never executed, and is
# correctly non-executable; it is listed here so that stays a decision.
EXECUTABLE_SCRIPTS = ("set_mode.sh", "mem_governor.sh")
SOURCE_ONLY_SCRIPTS = ("lib_safety.sh",)


def _index_mode(relative: str) -> str:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--", relative],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip(f"{relative} is not tracked in a readable index here")
    return result.stdout.split()[0]


@pytest.mark.skipif(shutil.which("git") is None, reason="the published mode lives in Git's index")
@pytest.mark.parametrize("name", EXECUTABLE_SCRIPTS)
def test_a_documented_script_is_published_runnable(name: str) -> None:
    path = SCRIPTS / name
    first_line = path.read_text(encoding="utf-8").splitlines()[0]

    assert first_line.startswith("#!"), f"{name} has no interpreter line"
    assert _index_mode(f"scripts/{name}") == "100755"


@pytest.mark.skipif(shutil.which("git") is None, reason="the published mode lives in Git's index")
@pytest.mark.parametrize("name", SOURCE_ONLY_SCRIPTS)
def test_a_sourced_library_is_not_published_executable(name: str) -> None:
    assert _index_mode(f"scripts/{name}") == "100644"


@pytest.fixture
def path_without_darwin_memory_tools(tmp_path: Path) -> str:
    """A PATH holding ordinary shell tools but none of the Darwin-only readers.

    Built by name rather than by directory so it behaves the same on a host
    where ``sysctl`` happens to exist but ``memory_pressure`` does not.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for tool in ("bash", "sh", "awk", "sed", "grep", "cat", "head", "date", "sleep", "dirname"):
        located = shutil.which(tool)
        if located:
            (stub_bin / tool).symlink_to(located)
    if not (stub_bin / "bash").exists():
        pytest.skip("no bash to run the script with")
    for absent in ("memory_pressure", "vm_stat", "sysctl"):
        assert not (stub_bin / absent).exists()
    return str(stub_bin)


@pytest.mark.parametrize("argv", [["status"], ["check", "8"], ["wait", "8", "regression-job"]])
def test_the_admission_gate_refuses_rather_than_guessing_a_number(
    argv: list[str], path_without_darwin_memory_tools: str
) -> None:
    # A timeout, not a hang: `wait` looping on a fabricated 0.0 is the defect,
    # so the test has to be able to catch it looping.
    result = subprocess.run(
        ["/bin/bash", str(SCRIPTS / "mem_governor.sh"), *argv],
        env={"PATH": path_without_darwin_memory_tools},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 3, result.stdout
    assert "unsupported on this platform" in result.stderr
    assert "0.0" not in result.stdout
    assert "HOLD" not in result.stdout
