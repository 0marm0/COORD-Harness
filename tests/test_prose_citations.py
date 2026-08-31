"""Prose-citation checks: '[doc §4a](target)' must resolve to a real heading."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import validate_documentation as validator  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _fixture(tmp_path: Path, *, target_body: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root / "README.md",
        "See [next steps §2a](next-steps.md) for the tracked follow-up.\n",
    )
    _write(root / "next-steps.md", target_body)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _messages(root: Path) -> list[str]:
    return validator.validate(root).messages


def test_citation_matching_a_real_heading_passes(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        target_body="# Release readiness checklist\n\n### 2a. Known follow-up\n\nBody text.\n",
    )
    messages = _messages(root)
    assert not any("prose citation" in message for message in messages)


def test_citation_with_no_matching_heading_is_reported(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        target_body="# Release readiness checklist\n\n### 3z. Unrelated item\n\nBody text.\n",
    )
    messages = _messages(root)
    assert any(
        "README.md:1: prose citation §2a has no matching heading in next-steps.md" == message
        for message in messages
    )


def test_same_document_anchor_citation_is_not_checked(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root / "README.md",
        "# Title\n\nSee [§2a](#2a-elsewhere) below.\n\n### 3z. Elsewhere\n",
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    assert not any("prose citation" in message for message in _messages(root))


def test_citation_inside_fenced_code_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root / "README.md",
        "```md\n[next steps §2a](next-steps.md)\n```\n",
    )
    _write(root / "next-steps.md", "# Checklist\n\n### 9z. Not the cited section\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    assert not any("prose citation" in message for message in _messages(root))


def test_heading_token_extraction_matches_leading_number_letter() -> None:
    text = "## 4. The accent switch\n\n### 2.4 Two-part heading\n\n#### not-a-token heading\n"
    assert validator._heading_tokens(text) == {"4", "2"}
