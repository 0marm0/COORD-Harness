"""The first command anyone types must not write to disk.

``coord --help`` called ``ensure_database()`` before argparse ever saw the argv,
so printing usage created ``.coordharness/coord.db``. In a directory this
process cannot write -- a mounted checkout, a container rootfs, someone else's
clone, the wrong directory -- the very first command a new user types failed
with a storage error instead of printing usage.

Both halves are asserted, because either one alone can pass while the defect
stands: a writable directory hides the failure, and an exit code hides the file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordharness import entry


@pytest.fixture
def ambient_state_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the state tree from the working directory, as a new clone does.

    Without this the developer's own exported ``COORD_DB`` decides where the
    database would have been created, and the assertions below would be looking
    in the wrong directory for a file that was written somewhere else.
    """
    for name in ("COORD_HOME", "COORD_DB", "COORD_COORD_DB", "COORD_KNOWLEDGE_DB"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [(["--help"], 0), (["-h"], 0), ([], 2)],
    ids=["--help", "-h", "bare coord"],
)
def test_an_invocation_that_reaches_no_handler_creates_no_state(
    argv: list[str],
    expected_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambient_state_unset: None,
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(clone))
    monkeypatch.chdir(clone)

    with pytest.raises(SystemExit) as exited:
        entry.main(argv)

    assert exited.value.code == expected_code
    assert sorted(path.name for path in clone.iterdir()) == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes anywhere, so an unwritable directory would not be unwritable",
)
def test_help_prints_usage_in_a_directory_that_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ambient_state_unset: None,
) -> None:
    clone = tmp_path / "read-only-clone"
    clone.mkdir()
    clone.chmod(0o500)
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(clone))
    monkeypatch.chdir(clone)
    try:
        # Prove the premise before trusting the result: a directory that turns
        # out to be writable would let this test pass against the defect.
        with pytest.raises(OSError):
            (clone / "writability-probe").mkdir()

        with pytest.raises(SystemExit) as exited:
            entry.main(["--help"])

        assert exited.value.code == 0
        assert "usage: coord" in capsys.readouterr().out
    finally:
        clone.chmod(0o700)


def test_a_verb_that_reaches_a_handler_still_bootstraps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambient_state_unset: None,
) -> None:
    """The exemption must stay an exemption.

    ``coord board`` on a new machine printing an empty board instead of ``no
    such table: v_work_owner`` is what the eager bootstrap was for, and a
    predicate that skipped everything would pass the two tests above.
    """
    clone = tmp_path / "fresh-clone"
    clone.mkdir()
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(clone))
    monkeypatch.chdir(clone)

    assert entry.main(["board"]) == 0
    assert (clone / ".coordharness" / "coord.db").is_file()
