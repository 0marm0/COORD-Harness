"""The documentation validator now sees deletions, not only what survived.

Every other check in ``tools/validate_documentation.py`` reads the tree that is
present: links, JSON, SVG accessibility, image provenance. A document that has
been removed is absent from all of them, so hard-deleting one passed the whole
validator. The custody guard it now drives diffs the worktree against HEAD and
classifies each removed ``docs/*.md``.

Only a deletion with no surviving copy is an error. A document that moved is a
note: the content still exists, and a validator that refuses renames is one
maintainers learn to skip.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import validate_documentation as validator  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    """A repository holding one protected document and one committed revision."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# Root\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text(
        "# Guide\n\nContent worth keeping.\n", encoding="utf-8"
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _custody(root: Path) -> validator.Report:
    report = validator.Report()
    validator._check_doc_custody(root.resolve(), report)
    return report


def test_an_unchanged_tree_reports_nothing(tmp_path: Path) -> None:
    report = _custody(_repo(tmp_path))

    assert report.total == 0
    assert report.notes == []


def test_a_repository_with_no_commit_yet_is_not_an_error(tmp_path: Path) -> None:
    """Nothing can have been deleted from a history that does not exist."""
    root = tmp_path / "fresh"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")

    report = _custody(root)

    assert report.total == 0


def test_deleting_a_protected_document_is_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "rm", "-q", "docs/guide.md")

    report = _custody(root)

    assert report.total == 1
    message = report.messages[0]
    assert message.startswith("docs/guide.md: ")
    assert "no copy survives" in message
    # The remediation has to name a commit the reader can actually recover from.
    assert "docs/archive/" in message


def test_archiving_the_same_document_instead_is_clean(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "docs" / "archive").mkdir()
    _git(root, "mv", "docs/guide.md", "docs/archive/guide.md")

    report = _custody(root)

    assert report.total == 0
    assert report.notes == []


def test_relocating_a_document_is_a_note_and_not_a_failure(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "mv", "docs/guide.md", "guide.md")

    report = _custody(root)

    assert report.total == 0
    assert len(report.notes) == 1
    assert "guide.md" in report.notes[0]


def test_a_deletion_whose_content_survives_elsewhere_is_only_a_note(tmp_path: Path) -> None:
    """Copy-then-delete preserves the content, so it is not a loss."""
    root = _repo(tmp_path)
    kept = root / "docs" / "guide.md"
    (root / "handbook.md").write_text(kept.read_text(encoding="utf-8"), encoding="utf-8")
    _git(root, "add", "handbook.md")
    _git(root, "rm", "-q", "docs/guide.md")

    report = _custody(root)

    assert report.total == 0
    assert len(report.notes) == 1
    assert "handbook.md" in report.notes[0]


def test_deleting_an_unprotected_markdown_file_is_ignored(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "rm", "-q", "README.md")

    report = _custody(root)

    assert report.total == 0
    assert report.notes == []


def test_a_guard_that_cannot_run_is_reported_rather_than_read_as_clean(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repo(tmp_path)

    def _boom(_root: Path):
        raise ImportError("coordharness is not installed")

    monkeypatch.setattr(validator, "_load_doc_deletion_guard", _boom)
    report = _custody(root)

    assert report.total == 1
    assert "not importable" in report.messages[0]
