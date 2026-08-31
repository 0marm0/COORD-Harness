from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# This lint module runs its whole indexing pass as module-level code on
# import (no main()/__main__ guard), and computes its DOCS root from
# coordharness.config.project_root(), which prefers the COORD_PROJECT_ROOT
# environment variable over cwd. Pinning cwd to a throwaway project is NOT
# enough on its own: a subprocess inherits the parent process's environment
# by default, so if COORD_PROJECT_ROOT is set in the environment running this
# test suite -- the documented normal configuration for an MCP setup -- that
# value wins over cwd and the lint indexes (and writes into) the real repo's
# docs/guide/INDEX.md instead of the fixture. Passing an explicit env= that
# pins COORD_PROJECT_ROOT to the fixture path (rather than merely leaving it
# unset in *this* process) is what actually keeps every run contained to
# project_root, regardless of what the ambient environment carries.
def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "coordharness.lints.docs_findability_index"],
        cwd=project_root,
        env={**os.environ, "COORD_PROJECT_ROOT": str(project_root)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_indexes_live_docs_and_skips_manifest_and_readme(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "guide"
    docs.mkdir(parents=True)
    (docs / "kept.md").write_text("# Kept\n")
    (docs / "_MANIFEST.md").write_text("# Manifest\n")
    (docs / "README.md").write_text("# Readme\n")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    index_text = (docs / "INDEX.md").read_text()
    assert "guide/kept.md" in index_text
    assert "guide/_MANIFEST.md" not in index_text
    assert "guide/README.md" not in index_text
    assert "auto-indexed 1 live docs" in result.stdout


def test_skips_review_subdirectory(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "guide"
    review = tmp_path / "docs" / "_review"
    docs.mkdir(parents=True)
    review.mkdir(parents=True)
    (docs / "kept.md").write_text("# Kept\n")
    (review / "draft.md").write_text("# Draft\n")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    index_text = (docs / "INDEX.md").read_text()
    assert "guide/kept.md" in index_text
    assert "_review/draft.md" not in index_text


def test_archive_docs_go_to_the_archive_index_not_the_live_one(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "guide"
    archive = tmp_path / "docs" / "archive"
    docs.mkdir(parents=True)
    archive.mkdir(parents=True)
    (docs / "kept.md").write_text("# Kept\n")
    (archive / "old.md").write_text("# Old\n")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    live_index = (docs / "INDEX.md").read_text()
    archive_index = (archive / "_INDEX.md").read_text()
    assert "archive/old.md" not in live_index
    assert "archive/old.md" in archive_index


def test_rerun_is_idempotent_not_additive(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "guide"
    docs.mkdir(parents=True)
    (docs / "kept.md").write_text("# Kept\n")

    first = _run(tmp_path)
    assert first.returncode == 0, first.stderr
    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr

    index_text = (docs / "INDEX.md").read_text()
    assert index_text.count("guide/kept.md") == 1


def test_empty_project_with_no_docs_directory_still_runs_clean(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "auto-indexed 0 live docs" in result.stdout
    index_text = (tmp_path / "docs" / "guide" / "INDEX.md").read_text()
    assert "0 curated live docs" in index_text
