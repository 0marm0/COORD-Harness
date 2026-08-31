"""The shipped agent commands must stay executable, mirrored, and public.

A command file is instructions an agent will follow literally, so the failure
modes are specific: a verb that no longer exists on the CLI, an MCP tool that was
renamed or never existed, one lane's copy drifting from the other's, or a private
path pasted in from the machine the command was written on. Each of those is
silent at authoring time and only shows up as a refusal in a stranger's session.

Every invocation named in a command is therefore checked against the *live*
surfaces -- the installed argparse subcommands and the server's tool catalogue --
not against a list kept here, which would drift with them.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import re
from pathlib import Path

from coordharness.coord import cli as coord_cli
from coordharness.coord import mcp_coord_server

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_COMMANDS = ROOT / ".claude" / "commands"
CODEX_COMMANDS = ROOT / ".agents" / "commands"
CLAUDE_SKILL = ROOT / ".claude" / "skills" / "operating-coordharness"
CODEX_SKILL = ROOT / ".agents" / "skills" / "operating-coordharness"

EXPECTED_COMMANDS = {
    "coord-start.md",
    "coord-claim.md",
    "coord-close.md",
    "coord-handoff.md",
    "coord-recover.md",
}

FENCED_BLOCK = re.compile(r"```[a-z]*\n(.*?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Only code -- fenced or inline -- is treated as an invocation, so prose such as
# "the coordination database" cannot be mistaken for a subcommand named "database".
CLI_INVOCATION = re.compile(r"(?:^|[\s/])coord\s+([a-z][a-z0-9-]*)")
MCP_INVOCATION = re.compile(r"MCP `([a-z_]+)`")


def _relative_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def _live_cli_subcommands() -> set[str]:
    buffer = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buffer):
        coord_cli.main(["--help"])
    help_text = buffer.getvalue()
    positional = help_text.split("positional arguments:", 1)
    assert len(positional) == 2, help_text
    group = re.search(r"\{([a-z0-9,\-_]+)\}", positional[1])
    assert group is not None, help_text
    return set(group.group(1).split(","))


def _code_regions(text: str) -> list[str]:
    regions = FENCED_BLOCK.findall(text)
    without_fences = FENCED_BLOCK.sub("\n", text)
    regions.extend(INLINE_CODE.findall(without_fences))
    return regions


def test_commands_are_mirrored_byte_for_byte() -> None:
    claude_files = _relative_files(CLAUDE_COMMANDS)
    codex_files = _relative_files(CODEX_COMMANDS)
    assert claude_files == codex_files
    assert {path.name for path in claude_files} == EXPECTED_COMMANDS

    for relative in sorted(claude_files):
        assert (CLAUDE_COMMANDS / relative).read_bytes() == (
            CODEX_COMMANDS / relative
        ).read_bytes(), relative


def test_skill_packages_stay_mirrored_alongside_the_commands() -> None:
    # The commands and the skill are one agent-experience package; a mirror check
    # that covers only half of it lets the other half drift.
    claude_files = _relative_files(CLAUDE_SKILL)
    assert claude_files == _relative_files(CODEX_SKILL)
    for relative in sorted(claude_files):
        assert (CLAUDE_SKILL / relative).read_bytes() == (
            CODEX_SKILL / relative
        ).read_bytes(), relative


def test_every_command_declares_a_description() -> None:
    for path in sorted(CLAUDE_COMMANDS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), path.name
        frontmatter = text.split("---\n", 2)[1]
        assert "description:" in frontmatter, path.name
        assert text.count("\n# ", 0, len(text)) >= 1 or text.startswith("# "), path.name


def test_every_named_cli_invocation_is_a_real_subcommand() -> None:
    live = _live_cli_subcommands()
    assert "claim" in live and "done" in live  # the extraction itself is load-bearing

    seen: set[str] = set()
    for path in sorted(CLAUDE_COMMANDS.glob("*.md")):
        for region in _code_regions(path.read_text(encoding="utf-8")):
            for verb in CLI_INVOCATION.findall(region):
                assert verb in live, f"{path.name}: `coord {verb}` is not a subcommand"
                seen.add(verb)
    assert seen, "no CLI invocation was extracted; the parser stopped working"


def test_every_named_mcp_tool_exists_on_the_server() -> None:
    live = set(mcp_coord_server._MCP_TOOL_NAMES)
    seen: set[str] = set()
    for path in sorted(CLAUDE_COMMANDS.glob("*.md")):
        for tool in MCP_INVOCATION.findall(path.read_text(encoding="utf-8")):
            assert tool in live, f"{path.name}: MCP `{tool}` is not a server tool"
            seen.add(tool)
    assert {"preflight", "claim_work", "complete"} <= seen


def test_commands_carry_no_absolute_path_or_personal_identifier() -> None:
    forbidden = (
        "/Users/",
        "/home/",
        "~/",
        "$HOME",
        "/opt/homebrew",
        "/private/tmp",
    )
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    for root in (CLAUDE_COMMANDS, CODEX_COMMANDS):
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                assert needle not in text, f"{path}: {needle}"
            assert not email.search(text), path
            for line in text.splitlines():
                stripped = line.lstrip("- *>#0123456789. ")
                assert not stripped.startswith("/"), f"{path}: {line}"


def test_entrypoints_route_agents_to_the_commands() -> None:
    for name, tree in (("CLAUDE.md", ".claude"), ("AGENTS.md", ".agents")):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert f"{tree}/commands/" in text, name
        for command in sorted(EXPECTED_COMMANDS):
            assert command.removesuffix(".md") in text, (name, command)


CLONE_ESCAPING_FLAGS = ("--register-clients", "--native")


def test_no_shipped_command_invokes_a_clone_escaping_flag() -> None:
    """`coord onboard --register-clients` writes MCP client configuration
    outside the clone, and a `--native` build lane touches the machine's Xcode
    toolchain outside it too -- both are opt-in by design (see README
    `Install`), and every shipped command promises registration stays opt-in.
    A shipped command must never hand an agent a live invocation of either
    flag; naming the flag in prose, or showing it on a commented-out example
    line, is how that opt-in gets documented and stays allowed. Only a live,
    uncommented `coord ...` invocation inside a fenced code block is checked
    -- that is the shape the original regression took."""
    for root in (CLAUDE_COMMANDS, CODEX_COMMANDS):
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for block in FENCED_BLOCK.findall(text):
                for line in block.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("$"):
                        stripped = stripped[1:].strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "coord" not in stripped:
                        continue
                    for flag in CLONE_ESCAPING_FLAGS:
                        assert flag not in stripped, (
                            f"{path.name}: live `coord` invocation carries {flag!r}: {line!r}"
                        )


def test_the_two_entrypoints_differ_only_where_the_client_differs() -> None:
    """CLAUDE.md and AGENTS.md are one document with a per-client seam.

    The seam is the title and the client-specific skill/command paths. Any other
    divergence means one lane silently received different instructions, which is
    exactly the drift the mirrored skill and command trees exist to prevent.
    """
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    codex = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()

    hunks = [
        group
        for group in difflib.SequenceMatcher(None, claude, codex).get_opcodes()
        if group[0] != "equal"
    ]
    assert len(hunks) == 3, hunks

    for tag, i1, i2, j1, j2 in hunks:
        assert tag == "replace", (tag, i1, i2)
        left, right = claude[i1:i2], codex[j1:j2]
        # A seam stays a seam only while it is small and anchored on the client
        # path; an unbounded hunk would let ordinary prose diverge inside it.
        assert len(left) <= 3 and len(right) <= 3, (left, right)
        left_text, right_text = "\n".join(left), "\n".join(right)
        if i1 == 0:
            assert left_text.startswith("# ") and right_text.startswith("# ")
            continue
        assert ".claude/" in left_text and ".agents/" not in left_text, left_text
        assert ".agents/" in right_text and ".claude/" not in right_text, right_text
