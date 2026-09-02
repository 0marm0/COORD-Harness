"""The operator-declared approved public attribution, and its exact limits.

`_scan_history_field` may skip the privacy findings on a commit's author or
committer NAME when that whole field equals an identity the vocabulary declares
approved. Every test here exists to pin one edge of that allowance, because an
identity allowance that leaked would silently un-flag the very class of finding
the gate is for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "extract"
sys.path.insert(0, str(TOOLS))
import gate  # noqa: E402

# The names are synthetic on purpose. Writing the real maintainer's name into a
# tracked test file would plant the needle this suite hunts: the phrase is still
# forbidden in file CONTENT, so a literal instance here would be a genuine
# finding on this very file.
APPROVED = "Fixtureperson Approved"
IMPOSTOR = "Fixtureperson Someone Else"
PHRASE = "fixtureperson"
REASON = "personal identity"


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, env=env, check=True
    )
    return result.stdout.strip()


def write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def initialize_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    write(root / "tools/extract/manifest.json", '{"files": []}\n')
    write(
        root / "tools/extract/authored.json",
        json.dumps({"files": {name: "test fixture" for name in files}, "derived": {}}) + "\n",
    )
    for rel, data in files.items():
        write(root / rel, data)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "noreply" + "@" + "github.com")
    git(root, "add", "-A")
    return root


def vocabulary(tmp_path: Path, *, approved: list[str] | None) -> Path:
    payload: dict[str, object] = {"forbidden": [[PHRASE, REASON, "i"]]}
    if approved is not None:
        payload["approved_identities"] = approved
    path = tmp_path / "vocabulary.json"
    write(path, json.dumps(payload))
    return path


def authored_repo(tmp_path: Path, name: str, *, message: str = "fixture") -> tuple[Path, str]:
    root = initialize_repo(tmp_path / "repo", {"safe.txt": "safe\n"})
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": "author" + "@" + "users.noreply.github.com",
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": "committer" + "@" + "users.noreply.github.com",
    }
    git(root, "commit", "-q", "-m", message, env=env)
    return root, git(root, "rev-parse", "HEAD")


def name_findings(report: gate.Report, sha: str) -> list[str]:
    return [
        item
        for item in report.history
        if item.startswith(f"history commit {sha} author name")
        or item.startswith(f"history commit {sha} committer name")
    ]


def test_approved_identity_clears_its_own_name_fields(tmp_path: Path) -> None:
    root, sha = authored_repo(tmp_path, APPROVED)
    report = gate.run(
        root, vocabulary_path=vocabulary(tmp_path, approved=[APPROVED]), history=True
    )
    assert name_findings(report, sha) == []


def test_a_different_name_containing_the_approved_one_still_fails(tmp_path: Path) -> None:
    # The impostor's name CONTAINS the approved string. A substring allowance --
    # the obvious wrong implementation -- would pass this commit.
    assert APPROVED.split()[0] in IMPOSTOR
    root, sha = authored_repo(tmp_path, IMPOSTOR)
    report = gate.run(
        root, vocabulary_path=vocabulary(tmp_path, approved=[APPROVED]), history=True
    )
    assert f"history commit {sha} author name: {REASON}" in report.history
    assert f"history commit {sha} committer name: {REASON}" in report.history


def test_approved_name_in_tracked_file_content_still_fails(tmp_path: Path) -> None:
    # A name in file content is usually an absolute home path or an unported
    # source reference: a different leak class, which this allowance never
    # covers.
    root = initialize_repo(tmp_path / "repo", {"safe.txt": f"maintained by {APPROVED}\n"})
    git(root, "commit", "-q", "-m", "fixture")
    report = gate.run(
        root, vocabulary_path=vocabulary(tmp_path, approved=[APPROVED]), history=True
    )
    assert any(item.startswith(f"safe.txt:1: {REASON}") for item in report.patterns)
    assert any("history blob" in item and item.endswith(REASON) for item in report.history)


def test_approved_name_in_a_commit_message_body_still_fails(tmp_path: Path) -> None:
    root, sha = authored_repo(tmp_path, APPROVED, message=f"subject\n\nthanks to {APPROVED}")
    report = gate.run(
        root, vocabulary_path=vocabulary(tmp_path, approved=[APPROVED]), history=True
    )
    assert f"history commit {sha} message: {REASON}" in report.history


def test_vocabulary_without_the_key_behaves_exactly_as_before(tmp_path: Path) -> None:
    # The regression guard: a vocabulary that never heard of the new key must
    # produce the pre-change report, name findings included.
    root, sha = authored_repo(tmp_path, APPROVED)
    report = gate.run(root, vocabulary_path=vocabulary(tmp_path, approved=None), history=True)
    assert sorted(name_findings(report, sha)) == [
        f"history commit {sha} author name: {REASON}",
        f"history commit {sha} committer name: {REASON}",
    ]


def test_an_empty_approved_list_flags_the_same_commit(tmp_path: Path) -> None:
    # Ablation: the allowance is what clears the first test, not some unrelated
    # property of an approved-looking commit. Emptying the list turns it red.
    root, sha = authored_repo(tmp_path, APPROVED)
    cleared = gate.run(
        root, vocabulary_path=vocabulary(tmp_path, approved=[APPROVED]), history=True
    )
    assert name_findings(cleared, sha) == []
    ablated = gate.run(root, vocabulary_path=vocabulary(tmp_path, approved=[]), history=True)
    assert sorted(name_findings(ablated, sha)) == [
        f"history commit {sha} author name: {REASON}",
        f"history commit {sha} committer name: {REASON}",
    ]


def test_a_malformed_approved_list_is_refused_not_ignored(tmp_path: Path) -> None:
    # A typo must not degrade to "no allowance, everything else still applied":
    # the vocabulary is reported invalid and the whole custom list is dropped,
    # which is the loud failure, not the quiet one.
    root, sha = authored_repo(tmp_path, APPROVED)
    path = tmp_path / "vocabulary.json"
    write(path, json.dumps({"forbidden": [[PHRASE, REASON, "i"]], "approved_identities": APPROVED}))
    report = gate.run(root, vocabulary_path=path, history=True)
    assert any(item.startswith("vocabulary: invalid") for item in report.patterns)
    assert name_findings(report, sha) == []
