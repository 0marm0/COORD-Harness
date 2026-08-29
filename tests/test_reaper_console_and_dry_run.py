"""The reaper had a `main()` and no way to reach it.

`release_expired_claims_batch` -- the only call that sweeps *every* expired
claim, not just the one a caller happens to be touching -- was reachable from
exactly one place: `reaper.run_reaper()`. Nothing in `pyproject.toml` exposed a
console script for it, and nothing scheduled it, so nothing ever called it.
That is a real gap in "status is derived, not stored": `derive_work_status()`
already treats a claim whose lease has lapsed as `attention` rather than
`running` on the very next read (see `docs/architecture.md`), so a *stale
display* self-heals without the reaper. What does not self-heal without it is
the claim itself going back into circulation -- it sits `status='running'`,
unclaimable by anyone else, until either its own work id happens to be
claimed/handed-off again (which opportunistically releases just that one row,
see `coord_db.claim_work`) or something runs the batch sweep. A session whose
process died is worse: nothing re-checks its pid until the reaper does, so a
crashed agent's claim reads as live for up to its full lease (an hour, by
`LEASE_DEFAULT_S`), not just until the next board read.

These tests cover the two things that make an entry point trustworthy:
the console script actually resolves (`test_reaper_console_script.py` proves
that from an installed wheel; the quick check here proves it from this
checkout without waiting on a wheel build), and `--dry-run` really previews
without mutating -- proven against a throwaway database with one claim that
should survive a reap and one that should not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db, reaper
from coordharness.coord.config import connect

REPO = Path(__file__).resolve().parents[1]


def test_pyproject_declares_a_coord_reaper_console_script() -> None:
    """The gap this whole change closes: nothing named the reaper as a script."""
    import tomllib

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts.get("coord-reaper") == "coordharness.coord.reaper:main", (
        "coord-reaper must point at the existing reaper.main(), not a "
        "reimplementation -- the whole point is to expose what already "
        f"exists. Declared scripts: {scripts}"
    )


def test_console_script_resolves_from_this_checkout() -> None:
    """A quick, non-wheel-building sanity check that the entry point loads.

    This does not prove the wheel ships it -- only installing a real wheel
    into a clean environment proves that, which is what
    ``test_reaper_console_script.py`` (marked slow) does. This is the fast
    complement: it fails immediately, with a clear message, if `main` were
    ever renamed or moved without updating `pyproject.toml`.
    """
    import importlib.metadata as metadata

    eps = list(metadata.entry_points(group="console_scripts"))
    match = [e for e in eps if e.name == "coord-reaper"]
    assert match, (
        "no coord-reaper console script registered for this interpreter -- "
        "run `pip install -e .` (or rebuild) after editing pyproject.toml"
    )
    loaded = match[0].load()
    assert loaded is reaper.main


@pytest.fixture
def seeded_db(tmp_path: Path) -> tuple[Path, str, str]:
    """A throwaway board with one already-expired claim and one live claim.

    Returns (db_path, expired_claim_id, live_claim_id).
    """
    db = tmp_path / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(conn, "EXP-1", title="expired work", assignee="claude")
        coord_db.upsert_work(conn, "LIVE-1", title="live work", assignee="claude")
        coord_db.register_session(conn, "claude:expired-sess", "claude", lease_s=600)
        coord_db.register_session(conn, "claude:live-sess", "claude", lease_s=600)
        expired_claim = coord_db.claim_work(conn, "claude:expired-sess", "EXP-1", lease_s=600)
        live_claim = coord_db.claim_work(conn, "claude:live-sess", "LIVE-1", lease_s=600)
        # Backdate only the first claim's lease. This is what "expired" means
        # here: real elapsed time would work too, but would make the test
        # either slow or flaky, and the reaper only ever looks at
        # `expires_at` against "now" -- it does not care how the row got
        # backdated.
        now = coord_db.db_now(conn)
        conn.execute(
            "UPDATE claims SET expires_at=? WHERE claim_id=?",
            (now - 100, expired_claim),
        )
        conn.commit()
    finally:
        conn.close()
    return db, expired_claim, live_claim


def _claim_row(db: Path, claim_id: str) -> dict:
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT status, release_reason, expires_at FROM claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def test_dry_run_releases_nothing(seeded_db: tuple[Path, str, str]) -> None:
    db, expired_claim, live_claim = seeded_db
    before_expired = _claim_row(db, expired_claim)
    before_live = _claim_row(db, live_claim)

    report = reaper.dry_run_reaper(db)

    assert report["dry_run"] is True
    # The preview must say a release WOULD happen -- otherwise this test
    # would pass even if dry-run accidentally previewed nothing at all.
    assert report["expired_claims"]["released_count"] == 1
    assert report["expired_claims"]["released_rows"][0]["claim_id"] == expired_claim

    after_expired = _claim_row(db, expired_claim)
    after_live = _claim_row(db, live_claim)
    assert after_expired == before_expired, (
        "dry_run_reaper must not mutate the real database, but the expired "
        f"claim's row changed: before={before_expired} after={after_expired}"
    )
    assert after_expired["status"] == "running", "still held -- nothing was released"
    assert after_live == before_live
    assert after_live["status"] == "running"


def test_real_run_releases_the_expired_claim_and_spares_the_live_one(
    seeded_db: tuple[Path, str, str],
) -> None:
    db, expired_claim, live_claim = seeded_db

    report = reaper.run_reaper(db, flush_projection=False)

    assert "dry_run" not in report
    assert report["expired_claims"]["released_count"] == 1

    expired_row = _claim_row(db, expired_claim)
    assert expired_row["status"] == "unclaimed"
    assert expired_row["release_reason"] == "expired"

    live_row = _claim_row(db, live_claim)
    assert live_row["status"] == "running", (
        "a claim with a lease still in the future must survive a real reap"
    )


def test_dry_run_report_matches_what_a_real_run_would_release(
    seeded_db: tuple[Path, str, str],
) -> None:
    """The preview is only worth trusting if it cannot drift from reality.

    dry_run_reaper() is deliberately implemented by running the real
    run_reaper() against a disposable snapshot rather than a hand-written
    second copy of the release predicate, precisely so this holds by
    construction. Assert it, rather than just asserting the implementation
    choice in prose.
    """
    db, expired_claim, _live_claim = seeded_db

    preview = reaper.dry_run_reaper(db)
    real = reaper.run_reaper(db, flush_projection=False)

    assert preview["expired_claims"]["released_rows_sha256"] == (
        real["expired_claims"]["released_rows_sha256"]
    )
    assert preview["claims_released"] == real["claims_released"]
    assert preview["runs_finalized"] == real["runs_finalized"]


def test_console_script_dry_run_leaves_db_untouched_end_to_end(
    seeded_db: tuple[Path, str, str],
) -> None:
    """Drive it the way an operator would: the installed script, not the API."""
    db, expired_claim, _live_claim = seeded_db
    coord_reaper = Path(sys.executable).with_name("coord-reaper")
    if not coord_reaper.is_file():
        pytest.skip(f"coord-reaper script not found next to {sys.executable}")

    before = _claim_row(db, expired_claim)
    result = subprocess.run(
        [str(coord_reaper), "--db", str(db), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "[DRY RUN]" in result.stdout
    assert "would release" in result.stdout
    assert _claim_row(db, expired_claim) == before


def test_help_says_it_mutates_and_names_the_dry_run_escape_hatch() -> None:
    """`coord doctor` is read-only; a stranger must be able to tell this apart."""
    result = subprocess.run(
        [sys.executable, "-m", "coordharness.coord.reaper", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    combined = result.stdout + result.stderr
    lowered = combined.lower()
    assert "writes to the database" in lowered
    assert "doctor" in lowered
    assert "--dry-run" in combined


def test_receipt_from_a_dry_run_is_labeled(
    tmp_path: Path, seeded_db: tuple[Path, str, str]
) -> None:
    db, _expired_claim, _live_claim = seeded_db
    receipt = tmp_path / "receipt.json"
    coord_reaper = Path(sys.executable).with_name("coord-reaper")
    if not coord_reaper.is_file():
        pytest.skip(f"coord-reaper script not found next to {sys.executable}")
    result = subprocess.run(
        [str(coord_reaper), "--db", str(db), "--dry-run", "--receipt", str(receipt)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(receipt.read_text())
    assert payload["dry_run"] is True
