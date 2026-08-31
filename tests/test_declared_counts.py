"""MCP tool counts, once wrong, stay wrong silently: nothing breaks when the README
and ``docs/feature-status.json`` drift from the server's actual tool catalog. This
module pins all three to the same source of truth so a future tool addition or
removal that forgets to update the docs fails CI instead of shipping a stale number.

The AST-based decorator count is deliberate, not stylistic: ``grep -c '@mcp.tool()'``
undercounts by one, because one tool is decorated ``@mcp.tool(structured_output=False)``
rather than the bare ``@mcp.tool()`` the naive grep looks for.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp",
    reason="the MCP server surface under test needs the optional [mcp] extra; "
    "without it this module is skipped rather than failing collection for the whole suite",
)

from coordharness.coord import mcp_coord_server  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVER_SOURCE = _REPO_ROOT / "src" / "coordharness" / "coord" / "mcp_coord_server.py"
_FEATURE_STATUS = _REPO_ROOT / "docs" / "feature-status.json"
_README = _REPO_ROOT / "README.md"
_MCP_INTEGRATION_DOC = _REPO_ROOT / "docs" / "mcp-integration.md"
_MODULE_COVERAGE_DOC = _REPO_ROOT / "docs" / "module-coverage.md"


def _count_mcp_tool_decorators(source_path: Path) -> int:
    """Count ``def``/``async def`` functions decorated ``@mcp.tool(...)`` via the AST.

    Not a text grep: ``@mcp.tool(structured_output=False)`` is a real registered
    tool that a literal ``'@mcp.tool()'`` substring match would miss, and a nested
    function inside an ``if tool_visible(...):`` guard is still a real decorator
    application that ``ast.walk`` finds regardless of indentation depth.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                count += 1
                break
    return count


def _feature_status_mcp_entry() -> dict:
    payload = json.loads(_FEATURE_STATUS.read_text(encoding="utf-8"))
    entries = [f for f in payload["features"] if f["id"] == "mcp-stdio"]
    assert len(entries) == 1, "expected exactly one mcp-stdio feature-status entry"
    return entries[0]


def test_grep_c_mcp_tool_parens_undercounts_by_one() -> None:
    """Documents the exact trap this module's AST approach avoids."""
    text = _SERVER_SOURCE.read_text(encoding="utf-8")
    naive = len(re.findall(r"^\s*@mcp\.tool\(\)\s*$", text, flags=re.MULTILINE))
    assert naive == 36
    assert _count_mcp_tool_decorators(_SERVER_SOURCE) == naive + 1


def test_ast_decorator_count_matches_registered_tool_set() -> None:
    assert _count_mcp_tool_decorators(_SERVER_SOURCE) == len(mcp_coord_server._MCP_TOOL_NAMES)


def test_feature_status_registered_count_matches_source() -> None:
    entry = _feature_status_mcp_entry()
    assert entry["mcp_tools_registered"] == len(mcp_coord_server._MCP_TOOL_NAMES)
    assert entry["mcp_tools_registered"] == _count_mcp_tool_decorators(_SERVER_SOURCE)


def test_feature_status_default_exposed_matches_promotion_candidates() -> None:
    entry = _feature_status_mcp_entry()
    registered = mcp_coord_server._MCP_TOOL_NAMES
    deferred = mcp_coord_server._SERVER_PROMOTION_CANDIDATES
    assert deferred <= registered
    assert entry["mcp_tools_default_exposed"] == len(registered) - len(deferred)
    assert entry["mcp_tools_deferred"] == sorted(deferred)


def test_default_build_server_exposes_the_declared_count(tmp_path: Path) -> None:
    """The number a fresh, unattested client actually sees over stdio."""
    db_path = tmp_path / "coord.db"
    knowledge_db = tmp_path / "knowledge.db"

    async def _list_tool_names() -> list[str]:
        server = mcp_coord_server.build_server(str(db_path), knowledge_db=knowledge_db)
        tools = await server.list_tools()
        return sorted(tool.name for tool in tools)

    exposed = asyncio.run(_list_tool_names())
    entry = _feature_status_mcp_entry()

    assert len(exposed) == entry["mcp_tools_default_exposed"]
    assert "handoff_existing" not in exposed
    assert set(exposed) == mcp_coord_server._MCP_TOOL_NAMES - mcp_coord_server._SERVER_PROMOTION_CANDIDATES


def test_readme_declared_and_exposed_counts_match_feature_status() -> None:
    entry = _feature_status_mcp_entry()
    text = _README.read_text(encoding="utf-8")

    prose_matches = re.findall(r"declares (\d+) tools and exposes (\d+) by default", text)
    table_matches = re.findall(r"(\d+) tools declared, (\d+) exposed by default", text)

    assert prose_matches, "README lost its 'declares N tools and exposes N by default' sentence"
    assert table_matches, "README lost its maturity-table 'N tools declared, N exposed by default' cell"

    for declared, exposed in prose_matches + table_matches:
        assert int(declared) == entry["mcp_tools_registered"]
        assert int(exposed) == entry["mcp_tools_default_exposed"]

    # No stray reference to the retired 34/33 figures anywhere on the page.
    assert not re.search(r"\b34 tools\b", text)
    assert not re.search(r"\b33 (?:tools|default tools|fail closed)\b", text)


def test_mcp_integration_doc_default_tool_count_matches_feature_status() -> None:
    """``docs/mcp-integration.md`` states the default-exposed tool count outside the
    README's own pinned sentences; it drifted to a stale ``33`` once before (the
    server had grown to 37 declared / 36 default-exposed) and nothing caught it.
    """
    entry = _feature_status_mcp_entry()
    text = _MCP_INTEGRATION_DOC.read_text(encoding="utf-8")

    matches = re.findall(r"All (\d+) default tools answer", text)
    assert matches, "mcp-integration.md lost its 'All N default tools answer' sentence"
    for exposed in matches:
        assert int(exposed) == entry["mcp_tools_default_exposed"]

    # No stray reference to the retired 34/33 figures anywhere on the page.
    assert not re.search(r"\b34 tools\b", text)
    assert not re.search(r"\b33 (?:tools|default tools|fail closed)\b", text)


def test_module_coverage_doc_declared_and_exposed_counts_match_feature_status() -> None:
    """``docs/module-coverage.md``'s convergence row states both the declared and
    default-exposed MCP tool counts and drifted to stale ``34``/``33`` figures
    once before without any test catching it.
    """
    entry = _feature_status_mcp_entry()
    text = _MODULE_COVERAGE_DOC.read_text(encoding="utf-8")

    matches = re.findall(r"declares (\d+) tools and exposes (\d+)", text)
    assert matches, "module-coverage.md lost its 'declares N tools and exposes N' sentence"
    for declared, exposed in matches:
        assert int(declared) == entry["mcp_tools_registered"]
        assert int(exposed) == entry["mcp_tools_default_exposed"]

    # No stray reference to the retired 34/33 figures anywhere on the page.
    assert not re.search(r"\b34 tools\b", text)
    assert not re.search(r"\b33 (?:tools|default tools|fail closed)\b", text)


def _install_section() -> str:
    text = _README.read_text(encoding="utf-8")
    return text.split("## Install", 1)[1].split("\n## ", 1)[0]


_SETUP_SH = _REPO_ROOT / "scripts" / "setup.sh"


def test_readme_install_does_not_claim_automatic_client_registration() -> None:
    """setup.sh defaults ``REGISTER_CLIENTS=0`` (opt-in via ``--register-clients``)
    and its own ``--dry-run`` output says so verbatim. The README's one-command
    path previously claimed it "registers any Claude Code or Codex client it finds
    on the machine" -- MEASURED FALSE against the script it describes, and the
    primary documented path left the agent unwired with no mention of the flag
    needed to actually register a client.
    """
    setup_source = _SETUP_SH.read_text(encoding="utf-8")
    assert re.search(r"^REGISTER_CLIENTS=0\s*$", setup_source, flags=re.MULTILINE), (
        "setup.sh's REGISTER_CLIENTS default changed away from opt-in;"
        " re-check the README's client-registration claim"
    )
    assert "clients NOT registered; pass --register-clients" in setup_source

    section = _install_section()
    assert "registers any Claude Code or Codex client it finds" not in section
    assert "--register-clients" in section


def test_readme_one_command_path_is_not_gated_on_macos_or_xcode() -> None:
    """The venv/db lane of setup.sh (``NATIVE=0`` by default) has no macOS
    dependency and runs on every OS; only the opt-in ``--native`` lane needs
    Xcode/XcodeGen, and it is a no-op notice off Darwin rather than a hard
    requirement. The README previously gated its one-command path on "macOS with
    Xcode's command-line tools and XcodeGen available" -- MEASURED FALSE, and it
    contradicted this same page's own Platform support section.
    """
    setup_source = _SETUP_SH.read_text(encoding="utf-8")
    assert re.search(r"^NATIVE=0\s*$", setup_source, flags=re.MULTILINE), (
        "setup.sh's NATIVE default changed away from opt-in;"
        " re-check the README's platform-gating claim"
    )

    section = _install_section()
    one_command_intro = section.split("**One command**", 1)[1].split("```", 1)[0]
    assert "on any OS" in one_command_intro
    assert "on macos with xcode" not in one_command_intro.lower()

    text = _README.read_text(encoding="utf-8")
    assert "Linux has the CLI and MCP path" in text, (
        "Platform support section moved or reworded;"
        " re-check it still agrees with the Install section"
    )
