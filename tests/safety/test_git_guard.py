from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from coordharness.safety.git_guard import CommitGuardError, guarded_commit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Safety Test")
    _git(tmp_path, "config", "user.email", "safety@example.invalid")
    (tmp_path / "owned.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "owned.txt", "other.txt")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_prepopulated_shared_index_is_untouched(repository: Path) -> None:
    (repository / "owned.txt").write_text("two\n", encoding="utf-8")
    (repository / "other.txt").write_text("two\n", encoding="utf-8")
    _git(repository, "add", "--", "other.txt")
    before = _git(repository, "diff", "--cached", "--binary")

    with pytest.raises(CommitGuardError, match="shared index"):
        guarded_commit(repository, paths=["owned.txt"], message="owned change")

    assert _git(repository, "diff", "--cached", "--binary") == before


def test_exact_commit_excludes_concurrently_staged_path(repository: Path) -> None:
    (repository / "owned.txt").write_text("two\n", encoding="utf-8")
    (repository / "other.txt").write_text("two\n", encoding="utf-8")

    def stage_other() -> None:
        _git(repository, "add", "--", "other.txt")

    sha = guarded_commit(
        repository,
        paths=["owned.txt"],
        message="owned change",
        pre_commit_hook=stage_other,
    )

    assert _git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", sha) == (
        "owned.txt"
    )
    assert _git(repository, "diff", "--cached", "--name-only") == "other.txt"


def test_git_guard_rejects_traversal_and_symlink_escape(repository: Path, tmp_path: Path) -> None:
    outside = repository.parent / f"{repository.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (repository / "escape.txt").symlink_to(outside)

    with pytest.raises(CommitGuardError):
        guarded_commit(repository, paths=["../outside.txt"], message="bad")
    with pytest.raises(CommitGuardError):
        guarded_commit(repository, paths=["escape.txt"], message="bad")

    assert _git(repository, "diff", "--cached", "--name-only") == ""
    assert outside.read_text(encoding="utf-8") == "outside\n"
