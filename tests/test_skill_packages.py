from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLAUDE_SKILL = ROOT / ".claude" / "skills" / "operating-coordharness"
CODEX_SKILL = ROOT / ".agents" / "skills" / "operating-coordharness"


def relative_files(root: Path) -> set[Path]:
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_claude_and_codex_skill_packages_are_byte_identical() -> None:
    claude_files = relative_files(CLAUDE_SKILL)
    codex_files = relative_files(CODEX_SKILL)
    assert claude_files == codex_files
    assert Path("SKILL.md") in claude_files
    assert Path("agents/openai.yaml") in claude_files

    for relative in sorted(claude_files):
        assert (CLAUDE_SKILL / relative).read_bytes() == (
            CODEX_SKILL / relative
        ).read_bytes()


def test_skill_frontmatter_and_references_are_self_contained() -> None:
    for skill_root in (CLAUDE_SKILL, CODEX_SKILL):
        skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        assert skill.startswith("---\n")
        assert "name: operating-coordharness" in skill
        assert "description:" in skill
        for reference in ("lifecycle.md", "jobs.md", "context.md"):
            assert (skill_root / "references" / reference).is_file()
            assert f"references/{reference}" in skill
