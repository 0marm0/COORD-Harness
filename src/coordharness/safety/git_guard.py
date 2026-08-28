"""Exact-path Git commits for a shared worktree and shared index."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
import subprocess

from .paths import PathSafetyError, resolve_under_root


class CommitGuardError(RuntimeError):
    """The exact-path commit contract could not be proven."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=False, text=True, capture_output=True
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise CommitGuardError(f"Git command failed: {detail}")
    return result


def _nul_paths(repo: Path, *args: str) -> tuple[str, ...]:
    return tuple(item for item in _git(repo, *args).stdout.split("\0") if item)


def _repository_root(repo: str | Path) -> Path:
    candidate = Path(repo).expanduser().resolve(strict=True)
    result = _git(candidate, "rev-parse", "--show-toplevel")
    root = Path(result.stdout.strip()).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CommitGuardError("working directory is outside the discovered repository") from exc
    return root


def normalize_exact_paths(repo: str | Path, paths: Sequence[str]) -> tuple[str, ...]:
    root = _repository_root(repo)
    if not paths:
        raise CommitGuardError("at least one explicit file path is required")
    normalized: list[str] = []
    for raw in paths:
        candidate = PurePosixPath(str(raw).replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise CommitGuardError("paths must be traversal-free and repository-relative")
        clean = candidate.as_posix().removeprefix("./")
        if not clean or clean == ".":
            raise CommitGuardError("each path must name one file")
        try:
            absolute = resolve_under_root(
                clean, root, must_exist=False, allow_absolute=False, allow_root=False
            )
        except PathSafetyError as exc:
            raise CommitGuardError("an allowlisted path escapes the repository") from exc
        if absolute.is_dir():
            raise CommitGuardError("directories are not accepted")
        normalized.append(clean)
    if len(set(normalized)) != len(normalized):
        raise CommitGuardError("duplicate paths are not accepted")
    return tuple(normalized)


def guarded_commit(
    repo: str | Path,
    *,
    paths: Sequence[str],
    message: str,
    post_stage_hook: Callable[[], None] | None = None,
    pre_commit_hook: Callable[[], None] | None = None,
) -> str:
    """Commit exactly ``paths`` while preserving unrelated staged changes.

    A non-empty shared index is refused before mutation. On failure after this
    call stages files, only the explicit path set is unstaged. ``git commit
    --only`` is the late-race backstop and excludes concurrently staged files.
    Hooks exist solely for adversarial tests.
    """

    root = _repository_root(repo)
    allowed = normalize_exact_paths(root, paths)
    if not str(message).strip():
        raise CommitGuardError("a non-empty commit message is required")

    preexisting = _nul_paths(root, "diff", "--cached", "--name-only", "-z")
    if preexisting:
        raise CommitGuardError("shared index is already populated; refusing to touch it")

    changed = _git(root, "status", "--porcelain=v1", "-z", "--", *allowed).stdout
    if not changed:
        raise CommitGuardError("none of the explicit paths has a worktree change")

    staged_by_call = False
    try:
        _git(root, "add", "--", *allowed)
        staged_by_call = True
        if post_stage_hook is not None:
            post_stage_hook()

        actual = set(_nul_paths(root, "diff", "--cached", "--name-only", "-z"))
        expected = set(allowed)
        if actual != expected:
            raise CommitGuardError("the staged set does not equal the explicit path set")
        if pre_commit_hook is not None:
            pre_commit_hook()

        _git(root, "commit", "--only", "-m", str(message), "--", *allowed)
        staged_by_call = False
    except Exception:
        if staged_by_call:
            _git(root, "reset", "-q", "HEAD", "--", *allowed, check=False)
        raise

    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    committed = set(
        _nul_paths(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
    )
    if committed != set(allowed):
        raise CommitGuardError("the resulting commit does not match the explicit path set")
    return sha
