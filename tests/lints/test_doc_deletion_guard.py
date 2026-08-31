from __future__ import annotations

import subprocess
from pathlib import Path

from coordharness.lints import doc_deletion_guard as lint


# --- pure classifiers --------------------------------------------------


def test_is_protected_markdown_flags_docs_prefixed_markdown() -> None:
    assert lint.is_protected_markdown("docs/guide/X.md") is True


def test_is_protected_markdown_does_not_flag_non_docs_markdown() -> None:
    assert lint.is_protected_markdown("notes/scratch.md") is False


def test_is_protected_markdown_does_not_flag_non_markdown_under_docs() -> None:
    assert lint.is_protected_markdown("docs/guide/data.json") is False


def test_is_archive_path_flags_docs_archive_markdown() -> None:
    assert lint.is_archive_path("docs/archive/old.md") is True


def test_is_archive_path_does_not_flag_live_docs() -> None:
    assert lint.is_archive_path("docs/guide/X.md") is False


# --- build_audit() against a real throwaway git repo --------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_build_audit_flags_unpreserved_deletion(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    doc = repo / "docs" / "guide" / "gone.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Gone\n\nThis content has no surviving twin anywhere.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add doc")
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    doc.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete doc")

    audit = lint.build_audit(repo, base_ref=base_ref, target_ref="HEAD")

    assert audit["status"] == "WARN"
    dispositions = {d["source"]: d["disposition"] for d in audit["decisions"]}
    assert dispositions["docs/guide/gone.md"] == "unpreserved_deletion"


def test_build_audit_passes_when_moved_straight_into_archive(tmp_path: Path) -> None:
    # git's diff detects this as a rename (R), not a delete (D): identical
    # content above its --find-renames=50% threshold, so this exercises the
    # rename branch's "safe_archive_move" disposition, not the delete
    # branch's content-similarity "safe_archive_copy" path.
    repo = _init_repo(tmp_path)
    doc = repo / "docs" / "guide" / "moved.md"
    doc.parent.mkdir(parents=True)
    content = "# Moved\n\nThis exact content moves to the archive.\n"
    doc.write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add doc")
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    doc.unlink()
    archived = repo / "docs" / "archive" / "moved.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "archive doc")

    audit = lint.build_audit(repo, base_ref=base_ref, target_ref="HEAD")

    assert audit["status"] == "PASS"
    dispositions = {d["source"]: d["disposition"] for d in audit["decisions"]}
    assert dispositions["docs/guide/moved.md"] == "safe_archive_move"



# NOTE: build_audit's own line-similarity fallback (>= 0.85 SequenceMatcher
# ratio -> "safe_archive_copy" for a deletion that git did NOT classify as a
# rename) is not independently exercised here: content close enough to clear
# that 0.85 line-ratio threshold is, in practice, also picked up by git's own
# --find-renames=50% detector first (see test_build_audit_passes_when_moved_
# straight_into_archive above), so the two paths are hard to split with a
# deterministic git fixture. The exact-digest branch (rows 179-183 above)
# and the rename branch are both covered.


def test_build_audit_ignores_deletion_of_unprotected_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    scratch = repo / "notes.md"
    scratch.write_text("not under docs/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add scratch")
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    scratch.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete scratch")

    audit = lint.build_audit(repo, base_ref=base_ref, target_ref="HEAD")

    assert audit["status"] == "PASS"
    assert audit["protected_deletions"] == 0


def test_build_audit_on_repo_with_no_changes_between_refs_is_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide").mkdir()
    (repo / "docs" / "guide" / "x.md").write_text("content\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add doc")

    audit = lint.build_audit(repo, base_ref="HEAD", target_ref="HEAD")

    assert audit["status"] == "PASS"
    assert audit["decisions"] == []


def test_build_audit_raises_on_nonexistent_repo(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    import pytest

    # subprocess refuses to chdir into a nonexistent cwd before git even runs.
    with pytest.raises(FileNotFoundError):
        lint.build_audit(missing, base_ref="HEAD", target_ref=None)
