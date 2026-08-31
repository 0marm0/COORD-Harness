"""Structural checks for the Claude Code plugin manifests.

These do not invoke Claude Code itself (no client is available in CI); they
check the two JSON files are well-formed, agree with pyproject.toml on
version, reference only paths that exist in this repository, and carry no
absolute path or personal identifier -- the repository is public.
"""

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = ROOT / ".claude-plugin" / "marketplace.json"
PYPROJECT = ROOT / "pyproject.toml"

EXPECTED_COMMANDS = (
    "coord-start.md",
    "coord-claim.md",
    "coord-close.md",
    "coord-handoff.md",
    "coord-recover.md",
)

# Generic, pattern-shaped guards: an email-shaped token, an absolute path, or a
# private work-id grammar. None of these should appear in a manifest meant for
# public distribution.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
WORK_ID_RE = re.compile(r"\bN0\d{3}\b")

# A caller with additional private names to screen for supplies them here as a
# comma-separated list. They are deliberately not written down: a literal kept
# in this file would publish the very string the guard exists to keep out.
EXTRA_FORBIDDEN_ENV = "COORD_FORBIDDEN_SUBSTRINGS"


def _extra_forbidden() -> list[str]:
    raw = os.environ.get(EXTRA_FORBIDDEN_ENV, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "pyproject.toml has no top-level version"
    return match.group(1)


def _manifest_paths(manifest: dict[str, Any]) -> list[str]:
    """Collect string values that look like repo-relative paths."""
    candidates: list[str] = []

    skills = manifest.get("skills")
    if isinstance(skills, str):
        candidates.append(skills)
    elif isinstance(skills, list):
        candidates.extend(item for item in skills if isinstance(item, str))

    for key in ("commands", "agents", "hooks", "mcpServers", "lspServers"):
        value = manifest.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, str))

    return candidates


def test_manifests_are_valid_json() -> None:
    plugin = _load(PLUGIN_MANIFEST)
    marketplace = _load(MARKETPLACE_MANIFEST)
    assert isinstance(plugin, dict)
    assert isinstance(marketplace, dict)


def test_plugin_version_matches_pyproject() -> None:
    version = _pyproject_version()
    plugin = _load(PLUGIN_MANIFEST)
    assert plugin["version"] == version

    marketplace = _load(MARKETPLACE_MANIFEST)
    listed = marketplace["plugins"][0]
    assert listed["name"] == plugin["name"]
    assert listed["version"] == version


def test_manifest_paths_exist_on_disk() -> None:
    plugin = _load(PLUGIN_MANIFEST)
    for raw in _manifest_paths(plugin):
        relative = raw[2:] if raw.startswith("./") else raw
        resolved = ROOT / relative
        assert resolved.exists(), f"plugin.json references missing path: {raw}"

    marketplace = _load(MARKETPLACE_MANIFEST)
    for entry in marketplace["plugins"]:
        source = entry["source"]
        assert isinstance(source, str), "same-repo source must be a relative path string"
        relative = source[2:] if source.startswith("./") else source
        resolved = (ROOT / relative).resolve()
        assert resolved == ROOT or resolved.is_relative_to(ROOT)
        assert resolved.exists()


def test_manifests_declare_the_operating_skill() -> None:
    plugin = _load(PLUGIN_MANIFEST)
    skills_path = plugin["skills"]
    relative = skills_path[2:] if skills_path.startswith("./") else skills_path
    skill_root = ROOT / relative / "operating-coordharness" / "SKILL.md"
    assert skill_root.is_file()

    # The command files sit outside the plugin-root ``commands/`` default, so a
    # plugin-only install carries them only while the manifest declares them.
    commands_path = plugin["commands"]
    relative = commands_path[2:] if commands_path.startswith("./") else commands_path
    command_root = ROOT / relative
    for name in EXPECTED_COMMANDS:
        assert (command_root / name).is_file(), f"plugin.json omits command: {name}"


def test_manifests_carry_no_absolute_path_or_personal_identifier() -> None:
    for path in (PLUGIN_MANIFEST, MARKETPLACE_MANIFEST):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text, f"{path} contains an absolute user path"
        assert not EMAIL_RE.search(text), f"{path} contains an email-shaped token"
        assert not WORK_ID_RE.search(text), f"{path} contains internal work-id grammar"
        lowered = text.lower()
        for forbidden in _extra_forbidden():
            assert forbidden not in lowered, f"{path} contains a screened substring"

        data = _load(path)
        identity = data.get("author") or data.get("owner")
        if identity:
            name = identity.get("name", "")
            assert " " not in name.strip(), (
                f"{path} author/owner name {name!r} looks like a person's name, "
                "not a GitHub handle"
            )


def test_this_guard_holds_no_private_name_of_its_own() -> None:
    """The screen must not be written out of the thing it screens for.

    A public file that lists the private names it forbids discloses them itself,
    so the screened list is supplied at run time and only pattern-shaped guards
    are declared here.
    """
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    literal_lists = (ast.Tuple, ast.List, ast.Set)
    for node in module.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if not any("FORBIDDEN" in name for name in targets):
            continue
        assert not isinstance(node.value, literal_lists), (
            f"{targets}: forbidden substrings are hard-coded into a public file"
        )
