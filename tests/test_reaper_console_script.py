"""Prove `coord-reaper` resolves from an installed wheel, not only a checkout.

`tests/test_packaging.py` documents why this class of test exists: a source
checkout with the source directory on `sys.path`/`PYTHONPATH` hides defects
that only surface once a package is actually installed, because every file is
present and pip never gets a chance to skip the real install. A console
script is exactly that kind of thing -- it is a `pyproject.toml` declaration
that setuptools turns into an executable shim at install time, and nothing
about running pytest from this checkout exercises that machinery. This test
does: it builds a real wheel, installs it into a brand-new virtualenv with the
checkout removed from `PYTHONPATH`, and drives the resulting `coord-reaper`
executable end to end -- registration, a dry run that changes nothing, and a
real run that does.

Kept in its own file (rather than added to test_packaging.py) because this
change's edit boundary is scoped to reaper.py, pyproject.toml, apps/install.sh,
one documentation file, and new test files -- not the existing packaging
suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _env_without_checkout(**overrides: str) -> dict[str, str]:
    """Environment with PYTHONPATH removed.

    Without this, the checkout's `src/` stays importable even inside the
    fresh venv, pip sees the package as already satisfied, the wheel install
    becomes a no-op, and the console script this test exists to check is
    never actually produced.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(overrides)
    return env


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("reaper-wheel")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(REPO)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


@pytest.mark.slow
def test_coord_reaper_resolves_and_runs_from_an_installed_wheel(
    wheel: Path, tmp_path: Path
) -> None:
    env_dir = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True, capture_output=True)
    python = env_dir / "bin" / "python"
    coord_reaper = env_dir / "bin" / "coord-reaper"

    install = subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        capture_output=True,
        text=True,
        env=_env_without_checkout(),
    )
    assert install.returncode == 0, install.stderr[-2000:]
    assert coord_reaper.is_file(), (
        "the wheel installed but produced no `coord-reaper` console script.\n"
        f"installed: {sorted(q.name for q in (env_dir / 'bin').iterdir())}\n"
        f"pip said:\n{install.stdout[-1500:]}"
    )

    env = _env_without_checkout(HOME=str(tmp_path))

    # --help must work without a database at all: it is the first thing
    # anyone runs, and it must be loud about mutating state before they run
    # anything else.
    help_result = subprocess.run(
        [str(coord_reaper), "--help"], capture_output=True, text=True, env=env
    )
    assert help_result.returncode == 0, help_result.stderr[-2000:]
    assert "writes to the database" in help_result.stdout.lower()
    assert "--dry-run" in help_result.stdout

    # Build a throwaway board using the installed package (not the checkout)
    # to prove the whole installed closure -- schema, bootstrap, coord_db --
    # works from site-packages, then exercise the installed script for real.
    project = tmp_path / "project"
    project.mkdir()
    setup = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from pathlib import Path;"
                "from coordharness.bootstrap import bootstrap_database;"
                "from coordharness.coord import coord_db;"
                "from coordharness.coord.config import connect;"
                "db = Path(%r);"
                "bootstrap_database(db);"
                "conn = connect(db);"
                "coord_db.upsert_work(conn, 'EXP-1', title='expired', assignee='claude');"
                "coord_db.register_session(conn, 'claude:s1', 'claude', lease_s=600);"
                "cid = coord_db.claim_work(conn, 'claude:s1', 'EXP-1', lease_s=600);"
                "now = coord_db.db_now(conn);"
                "conn.execute('UPDATE claims SET expires_at=? WHERE claim_id=?', (now - 100, cid));"
                "conn.commit(); conn.close();"
                "print(cid)"
            )
            % str(project / "coord.db"),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert setup.returncode == 0, setup.stderr[-2000:]
    claim_id = setup.stdout.strip()
    assert claim_id

    db_path = project / "coord.db"
    dry_run = subprocess.run(
        [str(coord_reaper), "--db", str(db_path), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert dry_run.returncode == 0, dry_run.stderr[-2000:]
    assert "[DRY RUN]" in dry_run.stdout
    assert "would release" in dry_run.stdout
    assert "1 expired claim" in dry_run.stdout

    check_status = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from coordharness.coord.config import connect;"
                "conn = connect(%r);"
                "print(conn.execute('SELECT status FROM claims WHERE claim_id=?', (%r,))"
                ".fetchone()[0])"
            )
            % (str(db_path), claim_id),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert check_status.stdout.strip() == "running", (
        "a dry run through the installed console script must not have "
        f"released the claim: {check_status.stdout!r} {check_status.stderr!r}"
    )

    real_run = subprocess.run(
        [str(coord_reaper), "--db", str(db_path), "--defer-projection",
         "--receipt", str(project / "receipt.json")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert real_run.returncode == 0, real_run.stderr[-2000:]
    assert "[DRY RUN]" not in real_run.stdout
    assert "released 0 zombie claim(s) + 1 expired claim(s)" in real_run.stdout

    receipt_payload = json.loads((project / "receipt.json").read_text())
    assert receipt_payload.get("dry_run") is not True

    check_status_after = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from coordharness.coord.config import connect;"
                "conn = connect(%r);"
                "row = conn.execute('SELECT status, release_reason FROM claims WHERE claim_id=?',"
                " (%r,)).fetchone();"
                "print(row['status'], row['release_reason'])"
            )
            % (str(db_path), claim_id),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert check_status_after.stdout.strip() == "unclaimed expired", check_status_after.stdout
