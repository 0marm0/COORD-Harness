"""Guard the things that only break once the package is installed.

Running the test suite from a source checkout with the source directory on the
path hides a whole category of defect, because every file is present and every
relative path resolves. An independent review of this extraction found four real
bugs that no checkout-based test could see:

  * a runtime JSON resource was missing from `package-data`, so the wheel shipped
    without it and the MCP server failed to import;
  * the database path was computed by counting parent directories up from
    `__file__`, which lands inside site-packages once installed;
  * the CLI never created its schema, so a new user's first command crashed with
    `no such table: v_work_owner`;
  * migrations shipped but were never applied, leaving optional tables absent.

These tests build a real wheel and inspect it. The end-to-end install test is
marked slow because it creates a virtual environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "coordharness"


def _runtime_resources() -> set[str]:
    """Every non-Python file under the package that code reads at runtime."""
    out: set[str] = set()
    for path in SRC.rglob("*"):
        if path.is_file() and path.suffix in {
            ".sql",
            ".json",
            ".html",
            ".css",
            ".js",
            ".png",
        }:
            out.add(path.relative_to(SRC.parent).as_posix())
    return out


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(REPO)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the source distribution through the declared isolated frontend."""
    out = tmp_path_factory.mktemp("sdist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(out), str(REPO)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    archives = list(out.glob("*.tar.gz"))
    assert len(archives) == 1, f"expected one sdist, got {archives}"
    return archives[0]


def test_sdist_contains_the_complete_test_suite(sdist: Path) -> None:
    """Never publish the misleading partial test suite setuptools once inferred."""
    expected = {path.relative_to(REPO).as_posix() for path in (REPO / "tests").rglob("*.py")}
    with tarfile.open(sdist, "r:gz") as archive:
        actual = set()
        for member in archive.getnames():
            parts = member.split("/", 1)
            if len(parts) == 2 and parts[1].startswith("tests/") and parts[1].endswith(".py"):
                actual.add(parts[1])
    assert actual == expected, (
        f"sdist test inventory differs: missing={sorted(expected - actual)}, "
        f"unexpected={sorted(actual - expected)}"
    )


def test_wheel_contains_every_runtime_resource(wheel: Path) -> None:
    """A resource the code reads but the wheel omits fails only after install."""
    packaged = set(zipfile.ZipFile(wheel).namelist())
    expected = _runtime_resources()
    assert expected, "no runtime resources found -- the check would pass vacuously"
    missing = sorted(name for name in expected if name not in packaged)
    assert not missing, (
        "these files are read at runtime but are absent from the wheel; "
        f"add them to [tool.setuptools.package-data]: {missing}"
    )


def test_no_path_resolution_by_parent_counting() -> None:
    """`parents[N]` on __file__ encodes a source layout that installing destroys."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "__file__" in line and ".parents[" in line:
                # parents[0]/[1] stay inside the package and are safe.
                index = line.split(".parents[", 1)[1].split("]", 1)[0]
                if index.isdigit() and int(index) > 1:
                    offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert not offenders, (
        "these resolve paths by walking above the package, which breaks once "
        "installed:\n" + "\n".join(offenders)
    )


def _env_without_checkout(**overrides: str) -> dict[str, str]:
    """Environment with PYTHONPATH removed.

    The suite runs with the source directory on PYTHONPATH, and subprocesses
    inherit it. That defeats the entire point of this test: pip sees the package
    already importable and skips the install, so the console script is never
    created and nothing is actually exercised from the wheel.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(overrides)
    return env


@pytest.mark.slow
def test_installed_wheel_runs_outside_the_checkout(wheel: Path, tmp_path: Path) -> None:
    """The real check: install into a clean environment and drive the CLI."""
    env_dir = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True, capture_output=True)
    python = env_dir / "bin" / "python"
    coord = env_dir / "bin" / "coord"
    coord_board = env_dir / "bin" / "coord-board"
    coord_jobs = env_dir / "bin" / "coord-jobs"
    coord_mcp = env_dir / "bin" / "coord-mcp"
    coord_models = env_dir / "bin" / "coord-models"

    install = subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        capture_output=True,
        text=True,
        env=_env_without_checkout(),
    )
    assert install.returncode == 0, install.stderr[-2000:]
    assert coord.is_file(), (
        "the wheel installed but produced no `coord` console script.\n"
        f"installed: {sorted(q.name for q in (env_dir / 'bin').iterdir())}\n"
        f"pip said:\n{install.stdout[-1500:]}"
    )
    assert coord_board.is_file()
    assert coord_jobs.is_file()
    assert coord_mcp.is_file()
    assert coord_models.is_file()

    models = subprocess.run(
        [str(coord_models), "list"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_env_without_checkout(HOME=str(tmp_path)),
    )
    assert models.returncode == 0, models.stderr[-2000:]
    assert json.loads(models.stdout)["configured"] is False
    model_check = subprocess.run(
        [str(coord_models), "check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_env_without_checkout(HOME=str(tmp_path)),
    )
    assert model_check.returncode == 1, model_check.stderr[-2000:]
    check_payload = json.loads(model_check.stdout)
    assert check_payload["configured"] is False
    assert check_payload["hardware_available"] is False

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    env = _env_without_checkout(COORD_PROJECT_ROOT=str(project), HOME=str(tmp_path))

    # Doctor must fail closed before bootstrap and leave a missing state tree absent.
    missing_db = project / ".coordharness" / "coord.db"
    missing = subprocess.run(
        [str(coord), "--db", str(missing_db), "doctor"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    assert missing.returncode == 2, missing.stderr[-2000:]
    assert json.loads(missing.stdout)["status"] == "BLOCKED"
    assert not missing_db.parent.exists()

    # A brand-new user's first command must succeed, not crash on a missing schema.
    board = subprocess.run(
        [str(coord), "board"], cwd=project, capture_output=True, text=True, env=env
    )
    assert board.returncode == 0, board.stderr[-2000:]
    assert '"rows"' in board.stdout

    # Migrations must have been applied, not merely shipped.
    db = project / ".coordharness" / "coord.db"
    assert db.is_file()
    names = subprocess.run(
        [
            str(python),
            "-c",
            f"import sqlite3;print([r[0] for r in sqlite3.connect({str(db)!r})"
            '.execute("select name from schema_migrations")])',
        ],
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    assert "002_exact_authority.sql" in names, f"migrations were not applied: {names}"

    # The installed console script exposes the read-only machine-readable doctor.
    doctor = subprocess.run(
        [str(coord), "doctor", "--now", "2000000000"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    assert doctor.returncode == 0, doctor.stderr[-2000:]
    doctor_payload = json.loads(doctor.stdout)
    assert doctor_payload["schema"] == "coordharness.doctor.v1"
