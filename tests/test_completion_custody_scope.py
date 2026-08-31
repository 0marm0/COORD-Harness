"""`done_signal_custodied` gates completion on git custody -- for Markdown only.

`src/coordharness/jobs/status.py`'s `done_signal_custodied()` decides whether
a declared proof is enough to complete a claim:

    return any(
        path.suffix.lower() != ".md" or completion_proof_is_tracked(path, root)
        for path in valid
    )

Read literally: a candidate proof only has to pass the git-index check
(`completion_proof_is_tracked`, which shells out to `git ls-files`) when its
suffix is `.md`. Every other suffix -- `.txt`, `.json`, `.rst`, anything --
satisfies the `any(...)` on existence alone, tracked by git or not.

This scoping is undocumented and untested upstream of this module: it is not
stated anywhere in the docstrings of `status.py`, and before this file no
test exercised the non-Markdown branch of the `any(...)` at all. This is a
**characterization test**, not a design endorsement -- it pins the behavior
that exists today (see the accompanying doc fix in `docs/comparison.md` and
`docs/threat-model.md` for the same scoping in prose) so that a future change
to which suffixes are custody-gated is a deliberate decision with a failing
test to update, rather than a silent regression (or silent widening) nobody
notices.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordharness.jobs import status


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _write(repo: Path, relpath: str, content: str) -> Path:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _git_add(repo: Path, relpath: str) -> None:
    subprocess.run(["git", "add", relpath], cwd=repo, check=True)


def test_untracked_markdown_proof_is_refused(repo: Path) -> None:
    _write(repo, "proof/a.md", "# proof\n")

    assert status.done_signal_custodied("proof/a.md", repo) is False


def test_tracked_markdown_proof_is_accepted(repo: Path) -> None:
    _write(repo, "proof/a.md", "# proof\n")
    _git_add(repo, "proof/a.md")

    assert status.done_signal_custodied("proof/a.md", repo) is True


def test_untracked_txt_proof_is_accepted(repo: Path) -> None:
    """`.txt` never reaches `completion_proof_is_tracked` -- existence is enough."""
    _write(repo, "proof/b.txt", "proof body\n")

    assert status.done_signal_custodied("proof/b.txt", repo) is True


def test_untracked_json_proof_is_accepted(repo: Path) -> None:
    """`.json` never reaches `completion_proof_is_tracked` -- existence is enough."""
    _write(repo, "proof/c.json", '{"ok": true}')

    assert status.done_signal_custodied("proof/c.json", repo) is True


def test_completion_proof_is_tracked_reflects_git_index_directly(repo: Path) -> None:
    """Same asymmetry, exercised at the lower-level function the doc pages cite."""
    proof = _write(repo, "proof/d.md", "# proof\n")

    assert status.completion_proof_is_tracked(str(proof), repo) is False

    _git_add(repo, "proof/d.md")

    assert status.completion_proof_is_tracked(str(proof), repo) is True
